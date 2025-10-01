# trading_bot_0_3.py
# -*- coding: utf-8 -*-
"""
بوت توصيات + خادم ويب (ملف واحد):
- FastAPI + uvicorn لفتح بورت (يعالج "No open ports" على Render Web Service).
- تشغيل خيط البوت تلقائياً عند بدء الخادم (بدون Background Worker مدفوع).
- Fallback تلقائي لمنصات عدة + تهدئة Rate Limit (OKX 50011).
- الاستراتيجية: EMA20/50 Cross + Volume Spike + فلاتر مومنتُم.
- إدارة أهداف: TP=1.2/2.2/3.5×ATR | SL=1.15×ATR (تقليل SL المبكر).
- تخزين السجلات في signals_web.xlsx (openpyxl).
"""

import os, time, threading, logging, requests
import pandas as pd, numpy as np
from datetime import datetime, timezone
from fastapi import FastAPI
import uvicorn
from openpyxl import Workbook, load_workbook
import ccxt

# ================== إعدادات من البيئة ==================
TG_TOKEN   = os.getenv("TG_TOKEN", "").strip()
CHAT_ID    = os.getenv("TG_CHAT_ID", "").strip()
API_URL    = f"https://api.telegram.org/bot{TG_TOKEN}" if TG_TOKEN else None

PRIMARY_EXCHANGE = os.getenv("EXCHANGE", "okx").strip()
FALLBACKS = [s.strip() for s in os.getenv(
    "FALLBACKS", "bitget,bingx,mexc,gateio,kucoin,kucoinfutures,binance,bybit"
).split(",") if s.strip()]
DERIVATIVES = int(os.getenv("DERIVATIVES", "1"))

TIMEFRAME = os.getenv("TIMEFRAME", "5m").strip()
BARS = int(os.getenv("BARS", "500"))
SCAN_TOP = int(os.getenv("SCAN_TOP", "120"))
MAX_SCAN_PER_LOOP = int(os.getenv("MAX_SCAN_PER_LOOP", "40"))
PAUSE_MS = int(os.getenv("OKX_PAUSE_MS", "600"))

MIN_DOLLAR_VOLUME = float(os.getenv("MIN_DOLLAR_VOLUME", "600000"))    # آخر 30 شمعة
MAX_SPREAD_PCT    = float(os.getenv("MAX_SPREAD_PCT", "0.25")) / 100.0 # نسبة (٪)
MIN_CONFIDENCE    = float(os.getenv("MIN_CONFIDENCE", "78"))
MIN_RR_RATIO      = float(os.getenv("MIN_RR", "1.25"))
MIN_VOLUME_SPIKE  = float(os.getenv("MIN_VOLUME_SPIKE", "1.5"))

ATR_TP = (
    float(os.getenv("ATR_TP1", "1.2")),
    float(os.getenv("ATR_TP2", "2.2")),
    float(os.getenv("ATR_TP3", "3.5")),
)
ATR_SL = float(os.getenv("ATR_SL", "1.15"))

SLEEP_BETWEEN = int(os.getenv("SLEEP_BETWEEN", "25"))
COOLDOWN_HOURS = float(os.getenv("COOLDOWN_HOURS", "2.5"))
MAX_SIGNALS = int(os.getenv("MAX_SIGNALS", "3"))

EXCEL = os.path.join(os.path.dirname(__file__), "signals_web.xlsx")

# ================== FastAPI ==================
app = FastAPI(title="AI Trading Bot (Single File)")

_bot_thread = None
_bot_started_at = None
_bot_errors = 0

# ================== Utils ==================
def tg(msg: str):
    if not TG_TOKEN or not CHAT_ID:
        print("TG>", msg)
        return
    try:
        requests.post(
            f"{API_URL}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        logging.error(f"TG err: {e}")

def now_utc():
    return datetime.now(timezone.utc)

def init_excel():
    if not os.path.exists(EXCEL):
        wb = Workbook(); ws = wb.active; ws.title = "Signals"
        ws.append(["#", "Pair", "Side", "Entry", "TP1", "TP2", "TP3", "SL",
                   "Result", "EntryTime", "ExitTime", "DurationMin", "ProfitPct",
                   "EntryReason", "ExitReason", "Confidence", "RR"])
        wb.save(EXCEL)

def xl_append(sig: dict, res: str, exit_t: str, dur_min: float, pnl_pct: float, exit_rsn: str):
    try:
        wb = load_workbook(EXCEL); ws = wb.active
        ws.append([
            ws.max_row,
            sig["symbol"].replace("/USDT:USDT", "/USDT"),
            sig["side"], sig["entry"], *sig["tps"], sig["sl"],
            res, sig["start_time"], exit_t, round(dur_min, 1), round(pnl_pct, 2),
            " • ".join(sig.get("reasons", [])[:4]), exit_rsn,
            round(sig.get("conf", 0), 1), round(sig.get("rr", 0), 3)
        ])
        wb.save(EXCEL)
    except Exception as e:
        logging.error(f"Excel append err: {e}")

init_excel()

# ================== مؤشرات فنية ==================
def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def macd(s, f=12, sl=26): return ema(s, f) - ema(s, sl)
def macd_sig(m): return ema(m, 9)
def rsi(s, n=14):
    d = s.diff(); u, d = np.maximum(d, 0), np.maximum(-d, 0)
    rs = u.ewm(span=n, adjust=False).mean() / (d.ewm(span=n, adjust=False).mean() + 1e-12)
    return 100 - 100 / (1 + rs)
def tr(h, l, c):
    pc = c.shift(1)
    return pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
def atr(h, l, c, n=14): return tr(h, l, c).ewm(span=n, adjust=False).mean()

# ================== تهيئة منصّة بتبديل تلقائي ==================
def try_init(ex_id: str):
    try:
        opts = {"enableRateLimit": True, "timeout": 20000}
        ex = getattr(ccxt, ex_id)(opts)
        # نوع السوق
        try:
            ex.options = getattr(ex, "options", {}) or {}
            ex.options["defaultType"] = "swap" if DERIVATIVES else "spot"
        except Exception:
            pass
        ex.load_markets()
        return ex
    except Exception as e:
        logging.error(f"init_exchange {ex_id} err: {e}")
        return None

def init_exchange():
    order = [PRIMARY_EXCHANGE] + [x for x in FALLBACKS if x != PRIMARY_EXCHANGE]
    for ex_id in order:
        ex = try_init(ex_id)
        if ex is not None:
            logging.info(f"✅ using exchange: {ex_id}")
            return ex
    raise RuntimeError("No exchange available (all blocked or failed).")

# ================== دوال سوق ==================
def ohlcv(ex, sym, lim=BARS):
    try:
        d = ex.fetch_ohlcv(sym, TIMEFRAME, limit=lim)
        if not d or len(d) < 120:
            return None
        df = pd.DataFrame(d, columns=["ts", "o", "h", "l", "c", "v"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        for col in ["o", "h", "l", "c", "v"]:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        msg = str(e)
        if "50011" in msg or "Too Many Requests" in msg:
            time.sleep(PAUSE_MS / 1000.0)
        logging.error(f"ohlcv err {sym}: {e}")
        return None

def last_price(ex, sym):
    try:
        return float(ex.fetch_ticker(sym)["last"])
    except Exception:
        return None

def spread_pct(ex, sym):
    try:
        ob = ex.fetch_order_book(sym, limit=5)
        bid, ask = ob["bids"][0][0], ob["asks"][0][0]
        return (ask - bid) / ask if ask else 1.0
    except Exception:
        return 1.0

def dollar_vol(df):
    try:
        return float((df["c"].iloc[-30:] * df["v"].iloc[-30:]).sum())
    except Exception:
        return 0.0

# ================== الاستراتيجية ==================
def strat(df):
    c, h, l, v = df["c"], df["h"], df["l"], df["v"]
    e20, e50 = ema(c, 20), ema(c, 50)
    macd_v = macd(c); macd_s = macd_sig(macd_v)
    rsi14 = rsi(c)
    atr14 = float(atr(h, l, c).iloc[-1])

    # اتجاه حسب EMA
    if e20.iloc[-1] > e50.iloc[-1]:
        side = "LONG"
    elif e20.iloc[-1] < e50.iloc[-1]:
        side = "SHORT"
    else:
        return None, {"reasons": ["no_trend"]}

    # تقاطع حديث ≤ 5 شمعات
    crossed = False
    for i in range(1, 6):
        if side == "LONG" and e20.iloc[-i] > e50.iloc[-i] and (e20.iloc[-i-1] <= e50.iloc[-i-1]):
            crossed = True; break
        if side == "SHORT" and e20.iloc[-i] < e50.iloc[-i] and (e20.iloc[-i-1] >= e50.iloc[-i-1]):
            crossed = True; break
    if not crossed:
        return None, {"reasons": ["no_recent_cross"]}

    # سبايك حجم
    vol_spike = v.iloc[-1] / (v.rolling(20).mean().iloc[-1] + 1e-12)
    if vol_spike < MIN_VOLUME_SPIKE:
        return None, {"reasons": [f"low_vol_{vol_spike:.1f}x"]}

    # مومنتُم + شمعة تأكيد
    if side == "LONG":
        if not (macd_v.iloc[-1] > macd_s.iloc[-1] and rsi14.iloc[-1] < 70):
            return None, {"reasons": ["weak_mom_long"]}
        if c.iloc[-1] <= c.iloc[-2]:
            return None, {"reasons": ["no_conf_candle_long"]}
    else:
        if not (macd_v.iloc[-1] < macd_s.iloc[-1] and rsi14.iloc[-1] > 30):
            return None, {"reasons": ["weak_mom_short"]}
        if c.iloc[-1] >= c.iloc[-2]:
            return None, {"reasons": ["no_conf_candle_short"]}

    # ثقة بسيطة
    conf = 70.0
    conf += min(15.0, (MIN_VOLUME_SPIKE - 1.0) * 10.0 + max(0.0, (vol_spike - MIN_VOLUME_SPIKE) * 8))
    conf += 8.0  # عبور حديث
    conf = min(conf, 100.0)

    return side, {
        "atr": atr14,
        "reasons": ["ema20-50_cross", f"vol_spike_{vol_spike:.1f}x", "macd_ok", "rsi_ok", "conf_candle"],
        "conf": conf
    }

def make_targets(entry, atr_val, side):
    t1, t2, t3 = ATR_TP
    slm = ATR_SL
    if side == "LONG":
        tps = [round(entry + atr_val * t1, 6), round(entry + atr_val * t2, 6), round(entry + atr_val * t3, 6)]
        sl  = round(entry - atr_val * slm, 6)
    else:
        tps = [round(entry - atr_val * t1, 6), round(entry - atr_val * t2, 6), round(entry - atr_val * t3, 6)]
        sl  = round(entry + atr_val * slm, 6)
    rr = abs(tps[0] - entry) / (abs(entry - sl) + 1e-12)  # R/R عند TP1
    return tps, sl, rr

# ================== مسح السوق + إرسال ==================
cooldown = {}
open_trades = {}
_cached_syms = None

def build_symbol_list(ex):
    syms = []
    mkts = ex.markets or ex.load_markets()
    for s, d in mkts.items():
        try:
            quote = str(d.get("quote", "")).upper()
            active = bool(d.get("active", True))
            if not active or not quote.startswith("USDT"):
                continue
            if DERIVATIVES:
                if d.get("linear") or d.get("swap") or "SWAP" in str(d.get("type","")).upper():
                    syms.append(s)
            else:
                if d.get("spot", False) or str(d.get("type","")).lower()=="spot":
                    syms.append(s)
        except Exception:
            continue
    # ترتيب حسب حجم الكوت إن توفّر
    try:
        ticks = ex.fetch_tickers()
        syms.sort(key=lambda x: float(ticks.get(x, {}).get("quoteVolume", 0) or 0), reverse=True)
    except Exception:
        pass
    return syms[:SCAN_TOP]

def scan(ex):
    global _cached_syms
    try:
        if _cached_syms is None:
            _cached_syms = build_symbol_list(ex)
    except Exception as e:
        logging.error(f"load markets error: {e}")
        return []

    picks, checked = [], 0
    for sym in _cached_syms:
        if checked >= MAX_SCAN_PER_LOOP:
            break
        checked += 1

        try:
            if cooldown.get(sym, 0) > time.time():
                continue

            df = ohlcv(ex, sym, BARS)
            if df is None:
                continue

            if dollar_vol(df) < MIN_DOLLAR_VOLUME:
                continue

            if spread_pct(ex, sym) > MAX_SPREAD_PCT:
                continue

            lp = last_price(ex, sym)
            if not lp:
                continue

            side, meta = strat(df)
            if side is None:
                continue

            tps, sl, rr = make_targets(float(lp), meta["atr"], side)
            if rr < MIN_RR_RATIO:
                continue

            conf = meta.get("conf", 70.0)
            if conf < MIN_CONFIDENCE:
                continue

            picks.append({
                "symbol": sym,
                "side": side,
                "entry": float(lp),
                "tps": tps,
                "sl": sl,
                "conf": conf,
                "rr": rr,
                "start_time": now_utc().strftime("%Y-%m-%d %H:%M"),
                "reasons": meta.get("reasons", [])[:5]
            })

            time.sleep(PAUSE_MS/1000.0)  # تهدئة

        except Exception as e:
            logging.error(f"scan {sym}: {e}")

    picks.sort(key=lambda x: (x["conf"], x["rr"]), reverse=True)
    return picks[:MAX_SIGNALS]

def send(sig):
    txt = (f"<b>⚡ توصيات تداول Ai</b>\n"
           f"{'🚀 LONG' if sig['side']=='LONG' else '🔻 SHORT'} <code>{sig['symbol'].replace('/USDT:USDT','/USDT')}</code>\n\n"
           f"💰 الدخول: <code>{sig['entry']}</code>\n"
           f"🎯 TP1: <code>{sig['tps'][0]}</code> | TP2: <code>{sig['tps'][1]}</code> | TP3: <code>{sig['tps'][2]}</code>\n"
           f"🛑 SL: <code>{sig['sl']}</code>\n\n"
           f"📊 Confidence: <b>{sig['conf']:.0f}%</b> • ⚖️ R/R: <b>{sig['rr']:.2f}</b>\n"
           f"🔎 Signals: <i>{' • '.join(sig.get('reasons', [])[:4])}</i>\n"
           f"🛡️ Risk: SL→BE بعد TP1 • تتبع EMA20 بعد TP2")
    tg(txt)
    open_trades[sig["symbol"]] = sig
    cooldown[sig["symbol"]] = time.time() + COOLDOWN_HOURS * 3600

def track(ex):
    for sym, st in list(open_trades.items()):
        if st.get("closed"):
            continue
        lp = last_price(ex, sym)
        if not lp:
            continue
        side, tps, sl, entry = st["side"], st["tps"], st["sl"], st["entry"]
        res, exit_rsn = None, ""
        if side == "LONG":
            if lp <= sl:                     res, exit_rsn = "SL", "Stop Loss"
            elif lp >= tps[2]:               res, exit_rsn = "TP3", "Target 3 🎯"
            elif lp >= tps[1]:               res, exit_rsn = "TP2", "Target 2 ✓"
            elif lp >= tps[0]:               res, exit_rsn = "TP1", "Target 1 ✓"
        else:
            if lp >= sl:                     res, exit_rsn = "SL", "Stop Loss"
            elif lp <= tps[2]:               res, exit_rsn = "TP3", "Target 3 🎯"
            elif lp <= tps[1]:               res, exit_rsn = "TP2", "Target 2 ✓"
            elif lp <= tps[0]:               res, exit_rsn = "TP1", "Target 1 ✓"

        if res:
            exit_t = now_utc().strftime("%Y-%m-%d %H:%M")
            dur = (datetime.strptime(exit_t, "%Y-%m-%d %H:%M") -
                   datetime.strptime(st["start_time"], "%Y-%m-%d %H:%M")).total_seconds()/60
            pnl_pct = ((lp - entry)/entry*100.0) if side=="LONG" else ((entry - lp)/entry*100.0)
            xl_append(st, res, exit_t, dur, pnl_pct, exit_rsn)
            tg(f"{'🏆' if res=='TP3' else '✅' if res.startswith('TP') else '🛑'} <b>{res}</b> {sym.replace('/USDT:USDT','/USDT')} @ {lp}\n"
               f"P/L: <b>{pnl_pct:+.2f}%</b> • {exit_rsn}")
            st["closed"] = True
            cooldown[sym] = time.time() + (COOLDOWN_HOURS * (2.0 if res=='SL' else 1.0)) * 3600

# ================== الحلقة الرئيسية ==================
def bot_main():
    global _bot_started_at, _bot_errors
    _bot_started_at = time.time()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    try:
        tg("🤖 <b>Bot (Web Mode) started!</b>\n"
           f"⏱️ TF: {TIMEFRAME} • 🔝 Top: {SCAN_TOP} • 💤 {SLEEP_BETWEEN}s • 📊 MinVol ${MIN_DOLLAR_VOLUME:,.0f}\n"
           f"🎯 ATR: {ATR_TP[0]}/{ATR_TP[1]}/{ATR_TP[2]} • 🛑 SL: {ATR_SL} ATR • ⚖️ RR≥{MIN_RR_RATIO} • ✅ conf≥{MIN_CONFIDENCE}%")
        ex = init_exchange()
        while True:
            try:
                for s in scan(ex):
                    send(s)
                    time.sleep(2)
                track(ex)
                time.sleep(SLEEP_BETWEEN)
            except Exception as loop_err:
                logging.error(f"main loop: {loop_err}")
                time.sleep(5)
    except Exception as e:
        _bot_errors += 1
        logging.error(f"BOT FATAL: {e}")

# ================== ربط البوت مع السيرفر ==================
@app.on_event("startup")
def _startup():
    global _bot_thread
    if _bot_thread is None or not _bot_thread.is_alive():
        _bot_thread = threading.Thread(target=bot_main, daemon=True)
        _bot_thread.start()

@app.get("/")
def root():
    return {
        "ok": True,
        "uptime_sec": int(time.time() - (_bot_started_at or time.time())),
        "exchange": PRIMARY_EXCHANGE,
        "derivatives": bool(DERIVATIVES),
        "tf": TIMEFRAME,
        "open_trades": sum(1 for v in open_trades.values() if not v.get("closed")),
    }

@app.get("/health")
def health():
    return {"ok": True, "errors": _bot_errors}

# ================== تشغيل uvicorn ==================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("trading_bot_0_3:app", host="0.0.0.0", port=port, reload=False)
