# -*- coding: utf-8 -*-
"""
Unified Trading Bot (Web Service + Background Loop)
- Auto exchange fallback (default OKX), cache markets with TTL, TG error throttling
- Strategy: recent EMA20-50 cross + Volume Spike ≥ 1.5x + momentum checks
- Dynamic ATR targets: TP1=1.0×ATR, TP2=2.0×ATR, TP3=3.2×ATR | SL=0.8×ATR
- Liquidity & spread filters, cooldown per symbol, Excel logging
- FastAPI server binds to $PORT so Render free "Web Service" works
!! تحذير: مفاتيح تيليجرام حساسة — لا تُشارك الملف علنًا.
"""

import os, time, json, logging, threading, requests
from datetime import datetime, timezone
import pandas as pd, numpy as np
from openpyxl import Workbook, load_workbook
import ccxt

# ============================= TELEGRAM TOKEN / CHAT =============================
# مدموجة مباشرة + يمكن تجاوزها بمتغيرات بيئة إن وُجدت
TG_TOKEN   = os.getenv("TG_TOKEN")   or "8130568386:AAGmpxKQw1XhqNjtj2OBzJ_-e3_vn0FE5Bs"
TG_CHAT_ID = os.getenv("TG_CHAT_ID") or "8429537293"  # اكتبها كنص

API_URL = f"https://api.telegram.org/bot{TG_TOKEN}"

# ============================= ENV & DEFAULTS =============================
# Exchange selection
MARKET_TYPE = os.getenv("MARKET_TYPE", "spot").strip().lower()  # 'spot' أو 'swap'
PRIMARY_EX  = os.getenv("EXCHANGE", "okx").strip().lower()
FALLBACKS   = [x.strip().lower() for x in os.getenv("FALLBACKS", "bitget,mexc,gate,kucoin,kraken").split(",") if x.strip()]
ALL_EX      = [PRIMARY_EX] + [x for x in FALLBACKS if x and x != PRIMARY_EX]

# Scan/limits
TIMEFRAME          = os.getenv("TIMEFRAME", "5m")
BARS               = int(os.getenv("BARS", "500"))
SLEEP_BETWEEN      = int(os.getenv("SLEEP_BETWEEN", "30"))
SCAN_TOP           = int(os.getenv("SCAN_TOP", "200"))
MIN_DOLLAR_VOLUME  = float(os.getenv("MIN_DOLLAR_VOLUME", "1000000"))  # last 30 bars
MAX_SPREAD_PCT     = float(os.getenv("MAX_SPREAD_PCT", str(0.20 / 100)))
COOLDOWN_HOURS     = float(os.getenv("COOLDOWN_HOURS", "3"))
MAX_SIGNALS        = int(os.getenv("MAX_SIGNALS", "2"))

# ATR Multipliers
ATR_MULT_TP = tuple(float(x) for x in os.getenv("ATR_TP", "1.0,2.0,3.2").split(","))
ATR_MULT_SL = float(os.getenv("ATR_SL", "0.8"))

# Telegram throttling for repeated errors
TG_COOLDOWN_SEC = int(os.getenv("TG_ERROR_COOLDOWN_SEC", "1800"))  # 30 min
_LAST_NOTIF: dict = {}

# OKX rate-limit relief
OKX_PAUSE_MS       = int(os.getenv("OKX_PAUSE_MS", "350"))  # sleep after each OHLCV to avoid 50011
MAX_SCAN_PER_LOOP  = int(os.getenv("MAX_SCAN_PER_LOOP", "90"))

# Markets cache (TTL seconds)
MARKETS_TTL = int(os.getenv("MARKETS_TTL", "900"))  # 15 min
MARKETS_CACHE_PATH = os.path.join(os.path.dirname(__file__), "markets_cache.json")

# Excel file
EXCEL = os.path.join(os.path.dirname(__file__), "signals.xlsx")

# ============================= TELEGRAM =============================
def tg(msg: str, key: str | None = None, force: bool = False):
    """
    Send Telegram message with optional throttling via `key`.
    If `key` is set, we send at most once / TG_COOLDOWN_SEC unless force=True.
    """
    try:
        if key and not force:
            ts = _LAST_NOTIF.get(key, 0)
            if time.time() - ts < TG_COOLDOWN_SEC:
                return
            _LAST_NOTIF[key] = time.time()
        requests.post(f"{API_URL}/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                      timeout=10)
    except Exception as e:
        logging.error(f"TG err: {e}")

def now():
    return datetime.now(timezone.utc)

# ============================= EXCEL =============================
def init_excel():
    if not os.path.exists(EXCEL):
        wb = Workbook(); ws = wb.active; ws.title = "Signals"
        ws.append(["#", "Pair", "Side", "Entry", "TP1", "TP2", "TP3", "SL",
                   "Result", "EntryTime", "ExitTime", "DurationMin", "Points",
                   "EntryReason", "ExitReason"])
        wb.save(EXCEL)

def xl_append(sig: dict, res: str, exit_t: str, dur: float, pts: float, exit_rsn: str):
    try:
        wb = load_workbook(EXCEL); ws = wb.active
        ws.append([ws.max_row, sig["symbol"].replace("/USDT:USDT", "/USDT"),
                   sig["side"], sig["entry"], *sig["tps"], sig["sl"],
                   res, sig["start_time"], exit_t, round(dur, 1), round(pts, 5),
                   " • ".join(sig.get("reasons", [])[:3]), exit_rsn])
        wb.save(EXCEL)
    except Exception as e:
        logging.error(f"XL err: {e}")

init_excel()

# ============================= TA =============================
def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def rsi(s, n=14):
    d = s.diff(); u, d = np.maximum(d, 0), np.maximum(-d, 0)
    rs = u.ewm(span=n, adjust=False).mean() / (d.ewm(span=n, adjust=False).mean() + 1e-12)
    return 100 - 100 / (1 + rs)
def macd(s, f=12, sl=26): return ema(s, f) - ema(s, sl)
def macd_sig(m): return ema(m, 9)
def tr(h, l, c):
    pc = c.shift(1)
    return pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
def atr(h, l, c, n=14): return tr(h, l, c).ewm(span=n, adjust=False).mean()

# ============================= EXCHANGE & MARKETS =============================
def _build_exchange(ex_id: str):
    default_type = "spot" if MARKET_TYPE == "spot" else "swap"
    opts = {"defaultType": default_type}
    if ex_id in {"okx"}:
        opts["defaultSubType"] = "linear"
    ex = getattr(ccxt, ex_id)({
        "enableRateLimit": True,
        "timeout": 30000,
        "options": opts,
    })
    return ex

def _save_markets_cache(exchange_id: str, symbols: list[str]):
    try:
        with open(MARKETS_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"ts": int(time.time()), "exchange": exchange_id, "symbols": symbols}, f)
    except Exception as e:
        logging.error(f"save cache err: {e}")

def _load_markets_cache():
    try:
        if not os.path.exists(MARKETS_CACHE_PATH): return None
        with open(MARKETS_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if int(time.time()) - int(data.get("ts", 0)) <= MARKETS_TTL:
            return data
    except Exception:
        return None
    return None

EX = None
EX_ID = None
MARKET_SYMBOLS: list[str] = []

def _filter_symbols(mkts: dict) -> list[str]:
    out = []
    for sym, d in mkts.items():
        if not d.get("active", True): continue
        try:
            quote = d.get("quote") or d.get("quoteId") or ""
            typ   = d.get("type") or d.get("spot") and "spot" or d.get("swap") and "swap" or ""
            if quote.upper() != "USDT": continue
            if MARKET_TYPE == "spot" and typ != "spot": continue
            if MARKET_TYPE == "swap" and typ != "swap": continue
            out.append(sym)
        except Exception:
            continue
    return out

def init_exchange_and_markets():
    global EX, EX_ID, MARKET_SYMBOLS
    cache = _load_markets_cache()
    tried = []
    for ex_id in [x for x in ALL_EX if x]:
        try:
            ex = _build_exchange(ex_id)
            mkts = ex.load_markets()
            symbols = _filter_symbols(mkts)
            if not symbols:
                raise Exception("no USDT symbols after filter")
            EX, EX_ID, MARKET_SYMBOLS = ex, ex.id, symbols
            _save_markets_cache(EX_ID, symbols)
            if tried:
                tg(f"✅ تم اختيار بورصة بديلة: <b>{EX_ID}</b> (بدلاً من {', '.join(tried)})", key="ex_switched", force=True)
            logging.info(f"Using exchange: {EX_ID}, symbols: {len(MARKET_SYMBOLS)}")
            return
        except Exception as e:
            tried.append(ex_id)
            logging.error(f"init_exchange {ex_id} err: {e}")
    if cache and cache.get("symbols"):
        try:
            ex_id = cache.get("exchange", "okx")
            ex = _build_exchange(ex_id)
            try:
                ex.load_markets()
            except Exception as e:
                logging.error(f"load_markets on cached exchange {ex_id} err: {e}")
                raise
            EX, EX_ID, MARKET_SYMBOLS = ex, ex.id, cache["symbols"]
            tg("⚠️ استخدمنا كاش الرموز مؤقتًا بسبب فشل التحميل من البورصات.", key="mkts_error")
            return
        except Exception as e:
            logging.error(f"cache fallback err: {e}")
    tg("⚠️ خطأ في تحميل قائمة العملات — سيتم إعادة المحاولة لاحقًا. (قد يكون حجب من البورصات)", key="mkts_error")

# ============================= MARKET HELPERS =============================
def _safe_sleep_after_call():
    if EX_ID == "okx":
        time.sleep(OKX_PAUSE_MS / 1000.0)

def ohlcv(sym, lim=BARS) -> pd.DataFrame | None:
    try:
        data = EX.fetch_ohlcv(sym, TIMEFRAME, limit=lim)
        _safe_sleep_after_call()
        if not data or len(data) < 200:
            return None
        df = pd.DataFrame(data, columns=["ts", "o", "h", "l", "c", "v"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        for col in ["o", "h", "l", "c", "v"]:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        logging.error(f"ohlcv err {sym}: {e}")
        return None

def fetch_tickers_safe() -> dict:
    try:
        t = EX.fetch_tickers()
        _safe_sleep_after_call()
        return t or {}
    except Exception as e:
        logging.error(f"fetch_tickers err: {e}")
        return {}

def spread_pct(sym) -> float:
    try:
        ob = EX.fetch_order_book(sym, limit=5)
        _safe_sleep_after_call()
        bid = ob["bids"][0][0] if ob.get("bids") else None
        ask = ob["asks"][0][0] if ob.get("asks") else None
        if not bid or not ask: return 1.0
        return (ask - bid) / ask
    except Exception:
        return 1.0

def dollar_vol_last30(df: pd.DataFrame) -> float:
    try:
        return float((df["c"].iloc[-30:] * df["v"].iloc[-30:]).sum())
    except Exception:
        return 0.0

# ============================= STRATEGY =============================
def strat(sym: str, df: pd.DataFrame):
    c, h, l, v = df["c"], df["h"], df["l"], df["v"]
    e20, e50 = ema(c, 20), ema(c, 50)
    m_val = macd(c); m_sig = macd_sig(m_val)
    r14 = rsi(c)
    a14 = float(atr(h, l, c).iloc[-1])

    def cross_recent(_fast, _slow, _side):
        if _side == "LONG":
            return (_fast.iloc[-1] > _slow.iloc[-1]) and (_fast.iloc[-6:-1] <= _slow.iloc[-6:-1]).any()
        else:
            return (_fast.iloc[-1] < _slow.iloc[-1]) and (_fast.iloc[-6:-1] >= _slow.iloc[-6:-1]).any()

    if e20.iloc[-1] > e50.iloc[-1]:
        side = "LONG"
    elif e20.iloc[-1] < e50.iloc[-1]:
        side = "SHORT"
    else:
        return None, {"atr": a14, "reasons": ["no_ema_cross"]}

    if not cross_recent(e20, e50, side):
        return None, {"atr": a14, "reasons": ["no_recent_cross"]}

    vol_spike = v.iloc[-1] / (v.rolling(20).mean().iloc[-1] + 1e-12)
    if vol_spike < 1.5:
        return None, {"atr": a14, "reasons": ["low_volume"]}

    if side == "LONG" and (m_val.iloc[-1] <= m_sig.iloc[-1] or r14.iloc[-1] >= 70):
        return None, {"atr": a14, "reasons": ["weak_mom_long"]}
    if side == "SHORT" and (m_val.iloc[-1] >= m_sig.iloc[-1] or r14.iloc[-1] <= 30):
        return None, {"atr": a14, "reasons": ["weak_mom_short"]}

    if side == "LONG" and c.iloc[-1] <= c.iloc[-2]:
        return None, {"atr": a14, "reasons": ["no_conf_long"]}
    if side == "SHORT" and c.iloc[-1] >= c.iloc[-2]:
        return None, {"atr": a14, "reasons": ["no_conf_short"]}

    return side, {"atr": a14, "reasons": [
        "ema_cross", f"vol_spike_{vol_spike:.1f}x",
        "macd_ok", "rsi_ok", "conf_candle"
    ]}

def targets(entry, atr_val, side):
    m1, m2, m3 = ATR_MULT_TP; slm = ATR_MULT_SL
    if side == "LONG":
        return [round(entry + atr_val * m1, 6),
                round(entry + atr_val * m2, 6),
                round(entry + atr_val * m3, 6)], round(entry - atr_val * slm, 6)
    else:
        return [round(entry - atr_val * m1, 6),
                round(entry - atr_val * m2, 6),
                round(entry - atr_val * m3, 6)], round(entry + atr_val * slm, 6)

def entry_window() -> bool:
    # 5m TF: نافذة دخول 90..150 ثانية بعد بداية الشمعة
    t = now()
    sec = (t.minute % 5) * 60 + t.second
    return 90 <= sec <= 150

# ============================= SCAN/SEND/TRACK =============================
cooldown: dict[str, float] = {}
open_trades: dict[str, dict] = {}

def _rank_symbols_by_volume(symbols: list[str]) -> list[str]:
    tickers = fetch_tickers_safe()
    if not tickers:
        return symbols[:SCAN_TOP]
    def _qv(sym):
        t = tickers.get(sym) or {}
        vq = t.get("quoteVolume")
        if vq is None:
            try:
                bv = float(t.get("baseVolume") or 0)
                last = float(t.get("last") or t.get("close") or 0)
                vq = bv * last
            except Exception:
                vq = 0
        try:
            return float(vq or 0)
        except Exception:
            return 0.0
    symbols_sorted = sorted(symbols, key=_qv, reverse=True)
    return symbols_sorted[:SCAN_TOP]

def scan() -> list[dict]:
    if EX is None:
        init_exchange_and_markets()
        if EX is None:
            return []
    picks = []
    symbols = _rank_symbols_by_volume(MARKET_SYMBOLS)
    symbols = symbols[:MAX_SCAN_PER_LOOP]

    for sym in symbols:
        try:
            if cooldown.get(sym, 0) > time.time(): 
                continue
            df = ohlcv(sym, BARS)
            if df is None: 
                continue
            dv = dollar_vol_last30(df)
            if dv < MIN_DOLLAR_VOLUME: 
                continue
            if spread_pct(sym) > MAX_SPREAD_PCT:
                continue

            entry = float(df["c"].iloc[-1])
            side, meta = strat(sym, df)
            if side is None:
                continue

            tps, sl = targets(entry, meta["atr"], side)
            picks.append({
                "symbol": sym, "side": side, "entry": entry, "tps": tps, "sl": sl,
                "conf": 78.0, "atr": meta["atr"],
                "start_time": now().strftime("%Y-%m-%d %H:%M"),
                "reasons": meta["reasons"]
            })

            if len(picks) >= MAX_SIGNALS:
                break
        except Exception as e:
            logging.error(f"scan sym err {sym}: {e}")

    if picks and not entry_window():
        logging.info("⏳ خارج نافذة الدخول — سيتم تجاهل الإشارات الآن.")
        return []
    return picks

def send(sig: dict):
    txt = (f"<b>🔥 توصية ذكية</b> • <i>{EX_ID.upper() if EX_ID else ''}</i>\n"
           f"{'🚀 LONG' if sig['side']=='LONG' else '🔻 SHORT'} <code>{sig['symbol'].replace('/USDT:USDT','/USDT')}</code>\n"
           f"💰 Entry: <code>{sig['entry']}</code>\n"
           f"🎯 TP1: <code>{sig['tps'][0]}</code> | TP2: <code>{sig['tps'][1]}</code> | TP3: <code>{sig['tps'][2]}</code>\n"
           f"🛑 SL: <code>{sig['sl']}</code>\n"
           f"🔎 Reasons: <i>{' • '.join(sig.get('reasons', [])[:3])}</i>")
    tg(txt)
    open_trades[sig["symbol"]] = sig
    cooldown[sig["symbol"]] = time.time() + COOLDOWN_HOURS * 3600

def _last_close(sym: str) -> float | None:
    df = ohlcv(sym, 3)
    if df is None or df.empty: return None
    return float(df["c"].iloc[-1])

def track():
    for sym, st in list(open_trades.items()):
        if st.get("closed"): 
            continue
        lp = _last_close(sym)
        if lp is None: 
            continue
        side, tps, sl, entry = st["side"], st["tps"], st["sl"], st["entry"]
        res, exit_rsn = None, ""
        if side == "LONG":
            if lp <= sl:
                res, exit_rsn = "SL", "اختراق كاذب"
            elif lp >= tps[2]:
                res, exit_rsn = "TP3", "استمرار الترند"
        else:
            if lp >= sl:
                res, exit_rsn = "SL", "اختراق كاذب"
            elif lp <= tps[2]:
                res, exit_rsn = "TP3", "استمرار الترند"

        if res:
            exit_t = now().strftime("%Y-%m-%d %H:%M")
            dur = (datetime.strptime(exit_t, "%Y-%m-%d %H:%M") -
                   datetime.strptime(st["start_time"], "%Y-%m-%d %H:%M")).total_seconds() / 60
            pts = (abs(lp - entry) if res.startswith("TP") else -abs(lp - entry))
            xl_append(st, res, exit_t, dur, pts, exit_rsn)
            emoji = {"SL": "🛑", "TP3": "🏆"}.get(res, "✅")
            tg(f"{emoji} <b>{res}</b> {sym.replace('/USDT:USDT','/USDT')} @ {lp}\n"
               f"سبب الدخول: {' • '.join(st.get('reasons', [])[:3])}\n"
               f"سبب الخروج: {exit_rsn}")
            st["closed"] = True
            cooldown[sym] = time.time() + COOLDOWN_HOURS * 3600

# ============================= DIAG REPORT =============================
def diag_report() -> str:
    try:
        df = pd.read_excel(EXCEL, sheet_name="Signals")
        if df.empty:
            return "لا توجد بيانات بعد."
        sl_df = df[df["Result"] == "SL"]
        tp_df = df[df["Result"] == "TP3"]
        top_loss_reason = sl_df["EntryReason"].mode().iloc[0] if not sl_df.empty else "—"
        top_exit_loss  = sl_df["ExitReason"].mode().iloc[0] if not sl_df.empty else "—"
        worst_pair = sl_df["Pair"].value_counts().index[0] if not sl_df.empty else "—"
        total, tp3, sl = len(df), len(tp_df), len(sl_df)
        wr = (tp3 / total * 100) if total else 0
        avg_dur_sl = float(sl_df["DurationMin"].mean()) if not sl_df.empty else 0
        avg_dur_tp = float(tp_df["DurationMin"].mean()) if not tp_df.empty else 0
        return (
            f"<b>📊 تقرير التشخيص</b>\n"
            f"الإشارات: {total} 🏆 TP3: {tp3} 🛑 SL: {sl}\n"
            f"نسبة الفوز: {wr:.1f}%\n"
            f"متوسط SL: {avg_dur_sl:.0f} دقيقة 🏆 TP3: {avg_dur_tp:.0f} دقيقة\n\n"
            f"أسوأ زوج: <code>{worst_pair}</code>\n"
            f"أكثر سبب دخول خسر: <code>{top_loss_reason}</code>\n"
            f"أكثر سبب خروج خسر: <code>{top_exit_loss}</code>"
        )
    except Exception:
        return "تعذّر قراءة Excel."

def report_job():
    if now().minute in {0, 2, 4}:
        tg(diag_report(), key="diag", force=True)

# ============================= MAIN LOOP (thread) =============================
def bot_loop():
    logging.info("🚀 Bot loop started")
    while True:
        try:
            if EX is None:
                init_exchange_and_markets()
            if EX is not None:
                for s in scan():
                    send(s)
                for _ in range(max(1, SLEEP_BETWEEN // 5)):
                    track()
                    report_job()
                    time.sleep(5)
            else:
                time.sleep(15)
        except Exception as e:
            logging.error(f"main loop err: {e}")
            time.sleep(10)

# ============================= FASTAPI SERVER =============================
try:
    from fastapi import FastAPI
    import uvicorn

    app = FastAPI()

    @app.get("/")
    def root():
        return {"ok": True, "exchange": EX_ID, "symbols": len(MARKET_SYMBOLS)}

    @app.get("/health")
    def health():
        return {"status": "ok", "time": now().isoformat(), "ex": EX_ID}

    @app.post("/diag")
    def diag():
        return {"report": diag_report()}

    def run_server():
        port = int(os.getenv("PORT", "10000"))
        uvicorn.run(app, host="0.0.0.0", port=port)

    def start_bot_thread():
        t = threading.Thread(target=bot_loop, daemon=True)
        t.start()
        return t

    if __name__ == "__main__":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
        tg("🤖 Bot Started - Unified (fallback+cache+throttle)", key="boot", force=True)
        start_bot_thread()
        run_server()

except Exception as e:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    logging.warning(f"FastAPI/uvicorn not available or failed ({e}), running bot loop directly.")
    if __name__ == "__main__":
        tg("🤖 Bot Started - Headless mode", key="boot", force=True)
        bot_loop()
