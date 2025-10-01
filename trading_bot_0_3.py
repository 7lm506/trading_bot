# -*- coding: utf-8 -*-
"""
Trading Bot v7_plus (Render-ready, single file)
- OKX swap/USDT + RateLimit/Backoff/Cache
- Wider cross window + softer volume filters (configurable)
- Telegram heartbeat + error alerts
- Web server /healthz for Render
"""

import os, time, math, random, logging, threading, requests
from datetime import datetime, timezone
import pandas as pd
import numpy as np
from openpyxl import Workbook, load_workbook
import ccxt
from fastapi import FastAPI
import uvicorn

# =============== Telegram ===============
TG_TOKEN   = os.getenv("TG_TOKEN",   "8130568386:AAGmpxKQw1XhqNjtj2OBzJ_-e3_vn0FE5Bs")
TG_CHAT_ID = int(os.getenv("TG_CHAT_ID", "8429537293"))
TG_API     = f"https://api.telegram.org/bot{TG_TOKEN}"

_last_tg_err = 0
def tg(text: str):
    """send telegram with light throttling on errors"""
    global _last_tg_err
    try:
        requests.post(f"{TG_API}/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
                      timeout=12)
    except Exception as e:
        now = time.time()
        if now - _last_tg_err > 120:
            logging.error(f"TG err: {e}")
            _last_tg_err = now

# =============== Config (env override) ===============
TIMEFRAME          = os.getenv("TIMEFRAME", "5m")
BARS               = int(os.getenv("BARS", "500"))
SCAN_TOP           = int(os.getenv("SCAN_TOP", "120"))
SLEEP_BETWEEN      = int(os.getenv("SLEEP_BETWEEN", "60"))   # دقيقة بين الدورات
MIN_DOLLAR_VOLUME  = float(os.getenv("MIN_DOLLAR_VOLUME", "300000"))  # على آخر 30 شمعة 5م
MAX_SPREAD_PCT     = float(os.getenv("MAX_SPREAD_PCT", str(0.20/100)))
COOLDOWN_HOURS     = float(os.getenv("COOLDOWN_HOURS", "3"))
MIN_RR             = float(os.getenv("MIN_RR", "1.18"))
ADX_MIN            = float(os.getenv("ADX_MIN", "18"))
VOL_SPIKE_MIN      = float(os.getenv("VOL_SPIKE_MIN", "1.2"))
EXTENSION_MAX_ATR  = float(os.getenv("EXTENSION_MAX_ATR", "1.2"))
CROSS_MAX_AGE      = int(os.getenv("CROSS_MAX_AGE", "12"))   # عدد الشمعات المسموح تقاطع خلالها

ATR_MULT_TP        = (1.0, 2.0, 3.2)
SL_ATR_BASE        = 1.25
SL_BEHIND_EMA50_ATR= 0.35

# Rate limit / backoff
OHLCV_SLEEP_MIN_S  = float(os.getenv("OHLCV_SLEEP_MIN_S", "0.45"))
OHLCV_SLEEP_MAX_S  = float(os.getenv("OHLCV_SLEEP_MAX_S", "0.75"))
OHLCV_CACHE_TTL_S  = int(os.getenv("OHLCV_CACHE_TTL_S", "30"))
TICKERS_TTL_S      = int(os.getenv("TICKERS_TTL_S", "60"))

# Reporting cadences
DIAG_EVERY_MIN       = int(os.getenv("DIAG_EVERY_MIN", "10"))
HEARTBEAT_EVERY_MIN  = int(os.getenv("HEARTBEAT_EVERY_MIN", "30"))

BASE_DIR = os.path.dirname(__file__)
SIG_XLSX = os.path.join(BASE_DIR, "signals_v7_plus.xlsx")
REJ_XLSX = os.path.join(BASE_DIR, "reject_v7_plus.xlsx")

def init_excels():
    if not os.path.exists(SIG_XLSX):
        wb = Workbook(); ws = wb.active; ws.title = "Signals"
        ws.append(["#", "Pair", "Side", "Entry", "TP1", "TP2", "TP3", "SL",
                   "Result", "EntryTime", "ExitTime", "DurationMin", "ProfitPct",
                   "EntryReason", "ExitReason"])
        wb.save(SIG_XLSX)
    if not os.path.exists(REJ_XLSX):
        wb = Workbook(); ws = wb.active; ws.title = "Rejects"
        ws.append(["#", "Pair", "Reason", "ADX", "VolSpike", "ExtATR", "Spread%", "DVol", "Time"])
        wb.save(REJ_XLSX)
init_excels()

def xl_append_signal(sig: dict, res: str, exit_t: str, dur_min: float, pl_pct: float, exit_rsn: str):
    try:
        wb = load_workbook(SIG_XLSX); ws = wb.active
        ws.append([ws.max_row,
                   sig["symbol"].replace("/USDT:USDT", "/USDT"),
                   sig["side"], sig["entry"], *sig["tps"], sig["sl"],
                   res, sig["start_time"], exit_t, round(dur_min,1), round(pl_pct,2),
                   " • ".join(sig.get("reasons", [])[:5]), exit_rsn])
        wb.save(SIG_XLSX)
    except Exception as e:
        logging.error(f"XL signal err: {e}")

def xl_append_reject(sym: str, reason: str, adx: float|None, vsp: float|None, ext: float|None, spr: float|None, dvol: float|None):
    try:
        wb = load_workbook(REJ_XLSX); ws = wb.active
        ws.append([ws.max_row, sym.replace("/USDT:USDT","/USDT"), reason,
                   round(adx or 0,1), round(vsp or 0,2), round(ext or 0,2), round((spr or 0)*100,3),
                   round(dvol or 0,0), datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")])
        wb.save(REJ_XLSX)
    except Exception as e:
        logging.error(f"XL reject err: {e}")

# =============== Exchange ===============
def init_exchange_okx():
    ex = ccxt.okx({
        "enableRateLimit": True,
        "timeout": 25000,
        "options": {"defaultType": "swap"},  # USDT perps
    })
    return ex

ex = None
def ensure_exchange():
    global ex
    if ex is None:
        try:
            _ex = init_exchange_okx()
            _ex.load_markets()
            ex = _ex
            logging.info("✅ Using exchange: okx")
        except Exception as e:
            logging.error(f"init okx failed: {e}")
            raise
ensure_exchange()

# =============== Caches & Guards ===============
tickers_cache = {"ts": 0, "data": {}}
ohlcv_cache   = {}  # sym -> {"ts": epoch, "df": DataFrame}

def sleep_jitter():
    time.sleep(random.uniform(OHLCV_SLEEP_MIN_S, OHLCV_SLEEP_MAX_S))

def backoff_sleep(attempt):
    time.sleep(0.6 * (2 ** max(0, attempt-1)))  # 0.6,1.2,2.4,4.8,...

def fetch_tickers_cached():
    now = time.time()
    if now - tickers_cache["ts"] < TICKERS_TTL_S and tickers_cache["data"]:
        return tickers_cache["data"]
    data = ex.fetch_tickers()
    tickers_cache["data"] = data
    tickers_cache["ts"] = now
    return data

def fetch_ohlcv_with_backoff(sym, timeframe, limit):
    now = time.time()
    cached = ohlcv_cache.get(sym)
    if cached and now - cached["ts"] < OHLCV_CACHE_TTL_S:
        return cached["df"]

    last_err = None
    for attempt in range(1, 6):
        try:
            df_raw = ex.fetch_ohlcv(sym, timeframe, limit=limit)
            if not df_raw or len(df_raw) < 200:
                last_err = Exception("empty ohlcv")
                backoff_sleep(attempt); continue
            df = pd.DataFrame(df_raw, columns=["ts","o","h","l","c","v"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            for col in ["o","h","l","c","v"]:
                df[col] = df[col].astype(float)
            ohlcv_cache[sym] = {"ts": now, "df": df}
            sleep_jitter()
            return df
        except ccxt.RateLimitExceeded as e:
            last_err = e; logging.error(f"rate limit {sym}: {e}"); backoff_sleep(attempt)
        except ccxt.ExchangeError as e:
            last_err = e
            if "50011" in str(e) or "Too Many Requests" in str(e):
                logging.error(f"OKX 50011 {sym}: backoff attempt {attempt}")
                backoff_sleep(attempt)
            else:
                logging.error(f"ohlcv ex err {sym}: {e}"); sleep_jitter()
        except Exception as e:
            last_err = e; logging.error(f"ohlcv err {sym}: {e}"); sleep_jitter()
    raise last_err or Exception("ohlcv failed")

# =============== TA ===============
def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def rsi(s, n=14):
    d = s.diff(); up, dn = np.maximum(d,0), np.maximum(-d,0)
    rs = up.ewm(span=n, adjust=False).mean() / (dn.ewm(span=n, adjust=False).mean() + 1e-12)
    return 100 - 100 / (1 + rs)
def tr(h, l, c):
    pc = c.shift(1)
    return pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
def atr(h, l, c, n=14): return tr(h,l,c).ewm(span=n, adjust=False).mean()
def adx(h, l, c, n=14):
    up = h.diff(); dn = -l.diff()
    up[up < 0] = 0; dn[dn < 0] = 0
    trv = tr(h,l,c); atrv = trv.ewm(span=n, adjust=False).mean()
    pdi = 100 * (up.ewm(span=n, adjust=False).mean() / (atrv + 1e-12))
    mdi = 100 * (dn.ewm(span=n, adjust=False).mean() / (atrv + 1e-12))
    dx  = 100 * (abs(pdi - mdi) / (pdi + mdi + 1e-12))
    return dx.ewm(span=n, adjust=False).mean(), pdi, mdi

# =============== Market utils ===============
def last_price(sym):
    try:
        return float(ex.fetch_ticker(sym)["last"])
    except Exception:
        return None

def spread_pct(sym):
    try:
        ob = ex.fetch_order_book(sym, limit=5)
        bid, ask = ob["bids"][0][0], ob["asks"][0][0]
        if not ask: return 1.0
        return (ask - bid) / ask
    except Exception:
        return 1.0

def dollar_vol(df):
    try:
        return float((df["c"].iloc[-30:] * df["v"].iloc[-30:]).sum())
    except Exception:
        return 0.0

def top_symbols(limit=SCAN_TOP):
    ticks = fetch_tickers_cached()
    syms = []
    for s, m in ex.markets.items():
        if not m.get("active"): continue
        if m.get("type") != "swap": continue
        if m.get("quote") != "USDT": continue
        syms.append(s)
    def qv(t):
        if not t: return 0.0
        v = t.get("quoteVolume") or t.get("baseVolume")
        if v: return float(v or 0)
        info = t.get("info") or {}
        return float(info.get("volCcy24h") or 0)
    syms.sort(key=lambda s: qv(ticks.get(s, {})), reverse=True)
    return syms[:limit]

# =============== Strategy ===============
reject_counter = {}
def strat(sym, df):
    c,h,l,v = df["c"], df["h"], df["l"], df["v"]
    last = float(c.iloc[-1])
    e20, e50 = ema(c,20), ema(c,50)
    atr14 = float(atr(h,l,c).iloc[-1])
    adx14, _, _ = adx(h,l,c)
    adx_cur = float(adx14.iloc[-1])
    vsp = float(v.iloc[-1] / (v.rolling(20).median().iloc[-1] + 1e-12))

    # trend
    if e20.iloc[-1] > e50.iloc[-1]:
        side = "LONG"
    elif e20.iloc[-1] < e50.iloc[-1]:
        side = "SHORT"
    else:
        return None, {"reasons":["no_trend"], "atr":atr14}

    # recent cross window (configurable)
    def crossed_recent(f, s, side):
        for i in range(1, CROSS_MAX_AGE+1):
            prev_ok = f.iloc[-i-1] <= s.iloc[-i-1] if side=="LONG" else f.iloc[-i-1] >= s.iloc[-i-1]
            now_ok  = f.iloc[-i]   >  s.iloc[-i]   if side=="LONG" else f.iloc[-i]   <  s.iloc[-i]
            if prev_ok and now_ok: return True, i
        return False, None

    cross_ok, cross_age = crossed_recent(e20, e50, side)
    if not cross_ok:
        return None, {"reasons":["no_recent_cross"], "atr":atr14}

    if vsp < VOL_SPIKE_MIN:
        return None, {"reasons":[f"low_vol_{vsp:.1f}x"], "atr":atr14}

    if adx_cur < ADX_MIN:
        return None, {"reasons":[f"weak_adx_{int(adx_cur)}"], "atr":atr14}

    # anti-chase
    ext_atr = abs(last - e20.iloc[-1]) / (atr14 + 1e-12)
    if ext_atr > EXTENSION_MAX_ATR:
        return None, {"reasons":["extended_from_ema20"], "atr":atr14}

    # confirm candle with trend
    if side=="LONG" and not (c.iloc[-1] > c.iloc[-2]): 
        return None, {"reasons":["no_confirm_long"], "atr":atr14}
    if side=="SHORT" and not (c.iloc[-1] < c.iloc[-2]):
        return None, {"reasons":["no_confirm_short"], "atr":atr14}

    # RSI extremes
    r = float(rsi(c).iloc[-1])
    if (side=="LONG" and r>=70) or (side=="SHORT" and r<=30):
        return None, {"reasons":[f"rsi_extreme_{int(r)}"], "atr":atr14}

    reasons = [f"ema20/50_cross_{cross_age}c", f"vol_spike_{vsp:.1f}x",
               f"adx_{int(adx_cur)}", "confirm_candle", "rsi_ok"]
    return side, {"atr":atr14, "reasons": reasons}

def build_sl(entry, side, atr_val, ema50_last):
    if side=="LONG":
        return round(min(entry - SL_ATR_BASE*atr_val,
                         ema50_last - SL_BEHIND_EMA50_ATR*atr_val), 6)
    else:
        return round(max(entry + SL_ATR_BASE*atr_val,
                         ema50_last + SL_BEHIND_EMA50_ATR*atr_val), 6)

def targets(entry, atr_val, side):
    m1, m2, m3 = ATR_MULT_TP
    if side=="LONG":
        return [round(entry + m1*atr_val,6), round(entry + m2*atr_val,6), round(entry + m3*atr_val,6)]
    else:
        return [round(entry - m1*atr_val,6), round(entry - m2*atr_val,6), round(entry - m3*atr_val,6)]

def entry_window_ok():
    now = datetime.now(timezone.utc)
    sec = (now.minute % 5)*60 + now.second
    return 30 <= sec <= 150

# =============== Scan/Send/Track ===============
cooldown    = {}   # sym -> epoch
open_trades = {}   # sym -> state
_last_diag  = 0
_last_hb    = 0

def scan_cycle():
    global reject_counter
    reject_counter = {}
    try:
        syms = top_symbols(SCAN_TOP)
    except Exception as e:
        tg("⚠️ خطأ في تحميل قائمة العملات — سيعاد المحاولة تلقائيًا")
        logging.error(f"load markets error: {e}")
        return []

    picks = []
    for sym in syms:
        try:
            if cooldown.get(sym, 0) > time.time():
                reject_counter["cooldown"] = reject_counter.get("cooldown",0)+1
                continue

            spr = spread_pct(sym)
            if spr > MAX_SPREAD_PCT:
                reject_counter["high_spread"] = reject_counter.get("high_spread",0)+1
                continue

            df = fetch_ohlcv_with_backoff(sym, TIMEFRAME, BARS)
            dvol = dollar_vol(df)
            if dvol < MIN_DOLLAR_VOLUME:
                reject_counter["low_dvol"] = reject_counter.get("low_dvol",0)+1
                continue

            lp = float(df["c"].iloc[-1])

            side, meta = strat(sym, df)
            if side is None:
                reason = (meta.get("reasons") or ["unknown"])[0]
                reject_counter[reason] = reject_counter.get(reason,0)+1
                xl_append_reject(sym, reason, None, None, None, spr, dvol)
                continue

            e50 = float(ema(df["c"],50).iloc[-1])
            sl = build_sl(lp, side, meta["atr"], e50)
            tps = targets(lp, meta["atr"], side)

            rr = abs((tps[0]-lp) / (sl-lp)) if (sl-lp) != 0 else 0
            if rr < MIN_RR:
                reject_counter["poor_rr"] = reject_counter.get("poor_rr",0)+1
                xl_append_reject(sym, "poor_rr", None, None, None, spr, dvol)
                continue

            picks.append({
                "symbol": sym, "side": side, "entry": lp, "tps": tps, "sl": sl,
                "atr": meta["atr"], "reasons": meta["reasons"],
                "start_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "state": "OPEN", "tp_stage": 0,
                "last_bar_time": int(df["ts"].iloc[-1].timestamp())
            })
        except Exception as e:
            logging.error(f"scan {sym}: {e}")
    return picks[:2]

def send_signal(sig):
    emoji = "🚀" if sig["side"]=="LONG" else "🔻"
    txt = (f"<b>⚡ Grade B • Ultra Precise v7+</b>\n"
           f"{emoji} <b>{sig['side']}</b> <code>{sig['symbol'].replace('/USDT:USDT','/USDT')}</code>\n\n"
           f"💰 Entry: <code>{sig['entry']}</code>\n"
           f"🎯 TP1: <code>{sig['tps'][0]}</code> | TP2: <code>{sig['tps'][1]}</code> | TP3: <code>{sig['tps'][2]}</code>\n"
           f"🛑 SL: <code>{sig['sl']}</code>\n\n"
           f"🔎 Signals: <i>{' • '.join(sig['reasons'][:5])}</i>\n"
           f"🛡️ Risk: SL→BE بعد TP1 • تتبع EMA20 بعد TP2")
    tg(txt)
    open_trades[sig["symbol"]] = sig
    cooldown[sig["symbol"]] = time.time() + COOLDOWN_HOURS*3600

def send_cycle_diag_if_due():
    global _last_diag
    now = time.time()
    if now - _last_diag < DIAG_EVERY_MIN * 60:
        return
    reasons_sorted = sorted(reject_counter.items(), key=lambda x: x[1], reverse=True)
    lines = [f"📋 <b>لماذا لا توجد توصيات (الدورة)</b>",
             f"عتبات: ADX≥{ADX_MIN} • RR≥{MIN_RR} • vol≥{VOL_SPIKE_MIN}× • امتداد≤{EXTENSION_MAX_ATR}×ATR"]
    if reasons_sorted:
        lines.append("أكثر أسباب الرفض:")
        for k,v in reasons_sorted[:6]:
            lines.append(f"• {k}: {v}")
    tg("\n".join(lines))
    _last_diag = now

def res_exit(sym, st, result, price, exit_reason):
    exit_t = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    dur = (datetime.strptime(exit_t, "%Y-%m-%d %H:%M")
           - datetime.strptime(st["start_time"], "%Y-%m-%d %H:%M")).total_seconds()/60
    if st["side"]=="LONG":
        pl_pct = (price - st["entry"]) / st["entry"] * 100
    else:
        pl_pct = (st["entry"] - price) / st["entry"] * 100
    xl_append_signal(st, result, exit_t, dur, pl_pct, exit_reason)
    emoji = {"SL":"🛑", "TP1":"✅", "TP2":"🎉", "TP3":"🏆"}.get(result, "✅")
    tg(f"{emoji} <b>{result}</b> {sym.replace('/USDT:USDT','/USDT')} @ <code>{price}</code>\n"
       f"P/L: <b>{pl_pct:+.2f}%</b> • مدة: {dur:.0f}m\n"
       f"خروج: {exit_reason}")
    st["state"] = "CLOSED"

def maybe_update_trailing(st):
    sym = st["symbol"]
    try:
        df = fetch_ohlcv_with_backoff(sym, TIMEFRAME, 120)
        last_bar = int(df["ts"].iloc[-1].timestamp())
        if last_bar == st.get("last_bar_time"):
            return
        st["last_bar_time"] = last_bar
        e20 = float(ema(df["c"],20).iloc[-1])
        if st["side"]=="LONG":
            st["sl"] = max(st["sl"], e20)
        else:
            st["sl"] = min(st["sl"], e20)
    except Exception as e:
        logging.error(f"trail update {sym}: {e}")

def track_cycle():
    for sym, st in list(open_trades.items()):
        if st.get("state") != "OPEN": continue
        lp = last_price(sym)
        if lp is None: continue
        side = st["side"]; tps = st["tps"]; sl = st["sl"]; entry = st["entry"]

        if side=="LONG":
            if lp <= sl:
                res_exit(sym, st, "SL", lp, "Stop Loss"); continue
            if st["tp_stage"] < 1 and lp >= tps[0]:
                st["tp_stage"] = 1; st["sl"] = entry
                tg(f"✅ TP1 • SL→BE • {sym.replace('/USDT:USDT','/USDT')} @ <code>{lp}</code>")
            elif st["tp_stage"] < 2 and lp >= tps[1]:
                st["tp_stage"] = 2; maybe_update_trailing(st)
                tg(f"🎯 TP2 • تتبع SL عند EMA20≈<code>{st['sl']:.6f}</code> • {sym.replace('/USDT:USDT','/USDT')}")
            elif lp >= tps[2]:
                res_exit(sym, st, "TP3", lp, "Target 3 🎯")
        else:
            if lp >= sl:
                res_exit(sym, st, "SL", lp, "Stop Loss"); continue
            if st["tp_stage"] < 1 and lp <= tps[0]:
                st["tp_stage"] = 1; st["sl"] = entry
                tg(f"✅ TP1 • SL→BE • {sym.replace('/USDT:USDT','/USDT')} @ <code>{lp}</code>")
            elif st["tp_stage"] < 2 and lp <= tps[1]:
                st["tp_stage"] = 2; maybe_update_trailing(st)
                tg(f"🎯 TP2 • تتبع SL عند EMA20≈<code>{st['sl']:.6f}</code> • {sym.replace('/USDT:USDT','/USDT')}")
            elif lp <= tps[2]:
                res_exit(sym, st, "TP3", lp, "Target 3 🎯")

def heartbeat_if_due():
    global _last_hb
    now = time.time()
    if now - _last_hb < HEARTBEAT_EVERY_MIN * 60:
        return
    open_cnt = sum(1 for s in open_trades.values() if s.get("state")=="OPEN")
    tg(f"💓 <b>Heartbeat</b>\n"
       f"⏱️ TF: {TIMEFRAME} • 🔝 Top: {SCAN_TOP}\n"
       f"صفقات مفتوحة: {open_cnt}\n"
       f"Diag كل {DIAG_EVERY_MIN}m • HB كل {HEARTBEAT_EVERY_MIN}m")
    _last_hb = now

# =============== Bot Loop ===============
def bot_loop():
    logging.info("🚀 Trading Bot v7+ started")
    tg("🤖 <b>Bot Started • v7_plus</b>\n"
       f"⏱️ TF: {TIMEFRAME} • 🔝 Top: {SCAN_TOP}\n"
       f"🛑 SL: max({SL_ATR_BASE}×ATR, خلف EMA50 بـ {SL_BEHIND_EMA50_ATR}×ATR)\n"
       f"📈 شروط: EMA20/50 + Vol≥{VOL_SPIKE_MIN}× + ADX≥{ADX_MIN} • RR≥{MIN_RR}")

    while True:
        try:
            picks = scan_cycle()
            if picks and not entry_window_ok():
                tg("⏳ لسنا في نافذة دخول (0:30–2:30 لكل شمعة 5م). تجاهلت الإشارات لهذه الدورة.")
                picks = []
            for s in picks:
                send_signal(s); time.sleep(2)

            # tracking
            for _ in range(max(1, SLEEP_BETWEEN // 5)):
                track_cycle()
                time.sleep(5)

            send_cycle_diag_if_due()
            heartbeat_if_due()

        except Exception as e:
            logging.error(f"main loop err: {e}")
            tg(f"⚠️ Runtime error: <code>{str(e)[:180]}</code>")
            time.sleep(5)

# =============== Web server (Render) ===============
app = FastAPI()

@app.get("/")
@app.get("/healthz")
def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}

def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

if __name__ == "__main__":
    run()
