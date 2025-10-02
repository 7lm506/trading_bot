# -*- coding: utf-8 -*-
"""
trading_bot_0_3.py  —  v7_plus (Render, single-file)

• OKX فقط (لتجاوز حجب Binance/Bybit) + RateLimiter قوي + Cache + Jitter + Backoff
• Scan على دفعات مع تدوير (Rotating Batches) حتى لا نضرب Rate-Limit: 120 زوج يتم تغطيتها على دفعات صغيرة
• إستراتيجية v7_plus:
    - recent EMA20/50 cross (حتى 12 شمعة) أو alignment (ADX قوي + ارتداد قريب من EMA20 + حجم)
    - منع مطاردة السعر: امتداد عن EMA20 ≤ 1.2×ATR (يسترخي قليلاً تلقائيًا عند كثرة الرفض)
    - SL: max(1.25×ATR, خلف EMA50 بـ 0.35×ATR)
    - TP: 1.0, 2.0, 3.2 × ATR، بعد TP1: SL→BE، بعد TP2: تتبع EMA20
• Heartbeat ثابت + Diag دوري حتى لو لا توجد توصيات
• /healthz لإبقاء الخدمة حيّة، مع Self-Ping داخلي قابل للتعطيل

بيئة مفيدة (Render → Environment):
  TG_TOKEN, TG_CHAT_ID
  TIMEFRAME=5m
  SCAN_TOP=120
  SCAN_BATCH=35          # حجم الدفعة لكل دورة
  SLEEP_BETWEEN=60       # ثواني بين الدورات
  MIN_DOLLAR_VOLUME=200000
  VOL_SPIKE_MIN=1.1
  REQ_MIN_INTERVAL_S=1.0 # لو بقي 50011 ارفعها تدريجياً
  OHLCV_CACHE_TTL_S=60
  TICKERS_TTL_S=600
  USE_ENTRY_WINDOW=off
  KEEPALIVE=on           # Self-Ping /healthz كل 4 دقائق
"""

import os, time, random, logging, threading, requests, signal
from datetime import datetime, timezone
import pandas as pd, numpy as np
from openpyxl import Workbook, load_workbook
import ccxt
from fastapi import FastAPI
import uvicorn

# ========= Telegram =========
TG_TOKEN   = os.getenv("TG_TOKEN",   "8130568386:AAGmpxKQw1XhqNjtj2OBzJ_-e3_vn0FE5Bs")
TG_CHAT_ID = int(os.getenv("TG_CHAT_ID", "8429537293"))
TG_API     = f"https://api.telegram.org/bot{TG_TOKEN}"

def tg(text: str):
    try:
        requests.post(f"{TG_API}/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
                      timeout=12)
    except Exception as e:
        logging.error(f"TG err: {e}")

# ========= إعدادات عامة =========
TIMEFRAME           = os.getenv("TIMEFRAME", "5m")
BARS                = int(os.getenv("BARS", "500"))

SCAN_TOP            = int(os.getenv("SCAN_TOP", "120"))     # إجمالي الكون
SCAN_BATCH          = int(os.getenv("SCAN_BATCH", "35"))    # الدفعة لكل دورة
SLEEP_BETWEEN       = int(os.getenv("SLEEP_BETWEEN", "60"))

MIN_DOLLAR_VOLUME   = float(os.getenv("MIN_DOLLAR_VOLUME", "200000"))
MAX_SPREAD_PCT      = float(os.getenv("MAX_SPREAD_PCT", str(0.20/100)))
COOLDOWN_HOURS      = float(os.getenv("COOLDOWN_HOURS", "2.5"))
MIN_RR              = float(os.getenv("MIN_RR", "1.18"))
ADX_MIN             = float(os.getenv("ADX_MIN", "18"))
VOL_SPIKE_MIN       = float(os.getenv("VOL_SPIKE_MIN", "1.1"))
EXTENSION_MAX_ATR   = float(os.getenv("EXTENSION_MAX_ATR", "1.2"))
CROSS_MAX_AGE       = int(os.getenv("CROSS_MAX_AGE", "12"))
CROSS_MODE          = os.getenv("CROSS_MODE", "recent_or_alignment")  # أو recent_only
USE_ENTRY_WINDOW    = os.getenv("USE_ENTRY_WINDOW", "off").lower()    # on/off

# إدارة المخاطر
ATR_MULT_TP         = (1.0, 2.0, 3.2)
SL_ATR_BASE         = 1.25
SL_BEHIND_EMA50_ATR = 0.35

# مكافحة Rate Limit
REQ_MIN_INTERVAL_S  = float(os.getenv("REQ_MIN_INTERVAL_S", "1.0"))
BACKOFF_BASE_S      = float(os.getenv("BACKOFF_BASE_S", "0.8"))
OHLCV_CACHE_TTL_S   = int(os.getenv("OHLCV_CACHE_TTL_S", "60"))
TICKERS_TTL_S       = int(os.getenv("TICKERS_TTL_S", "600"))
JITTER_MIN_S        = float(os.getenv("JITTER_MIN_S", "0.25"))
JITTER_MAX_S        = float(os.getenv("JITTER_MAX_S", "0.6"))

# نبض وتشخيص
DIAG_EVERY_MIN      = int(os.getenv("DIAG_EVERY_MIN", "10"))
HEARTBEAT_EVERY_MIN = int(os.getenv("HEARTBEAT_EVERY_MIN", "30"))

# Self Ping
KEEPALIVE           = os.getenv("KEEPALIVE", "on").lower()  # on/off

# ========= ملفات Excel =========
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

def xl_append_signal(sig, res, exit_t, dur_min, pl_pct, exit_rsn):
    try:
        wb = load_workbook(SIG_XLSX); ws = wb.active
        ws.append([ws.max_row,
                   sig["symbol"].replace("/USDT:USDT", "/USDT"),
                   sig["side"], sig["entry"], *sig["tps"], sig["sl"],
                   res, sig["start_time"], exit_t, round(dur_min,1), round(pl_pct,2),
                   " • ".join(sig.get("reasons", [])[:6]), exit_rsn])
        wb.save(SIG_XLSX)
    except Exception as e:
        logging.error(f"XL signal err: {e}")

def xl_append_reject(sym, reason, adx=None, vsp=None, ext=None, spr=None, dvol=None):
    try:
        wb = load_workbook(REJ_XLSX); ws = wb.active
        ws.append([ws.max_row, sym.replace("/USDT:USDT","/USDT"), reason,
                   round(adx or 0,1), round(vsp or 0,2), round(ext or 0,2),
                   round((spr or 0)*100,3), round(dvol or 0,0),
                   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")])
        wb.save(REJ_XLSX)
    except Exception as e:
        logging.error(f"XL reject err: {e}")

# ========= OKX =========
def init_okx():
    ex = ccxt.okx({
        "enableRateLimit": True,
        "timeout": 25000,
        "options": {"defaultType": "swap"},
    })
    ex.load_markets()
    return ex

ex = init_okx()
logging.info("✅ Using OKX swap/USDT")

# ========= RateLimiter + Cache =========
_last_req_ts = 0.0
_global_penalty_s = 0.0
_symbol_penalty = {}
tickers_cache = {"ts": 0, "data": {}}
ohlcv_cache   = {}  # sym -> {"ts": epoch, "df": DataFrame}

def _guard_rate(symbol=None):
    global _last_req_ts
    wait = REQ_MIN_INTERVAL_S + _global_penalty_s + ( _symbol_penalty.get(symbol, 0.0) if symbol else 0.0 )
    delta = time.time() - _last_req_ts
    if delta < wait:
        time.sleep(wait - delta)
    _last_req_ts = time.time()

def _penalize(symbol=None, attempt=1):
    global _global_penalty_s
    _global_penalty_s = min(6.0, BACKOFF_BASE_S * (2 ** max(0, attempt-1)))
    if symbol:
        _symbol_penalty[symbol] = min(3.0, _symbol_penalty.get(symbol, 0.0) + 0.6)

def _relax_penalty(symbol=None):
    global _global_penalty_s
    _global_penalty_s = max(0.0, _global_penalty_s * 0.6)
    if symbol and symbol in _symbol_penalty:
        _symbol_penalty[symbol] = max(0.0, _symbol_penalty[symbol] * 0.6)

def _jitter():
    time.sleep(random.uniform(JITTER_MIN_S, JITTER_MAX_S))

def fetch_tickers_cached():
    now = time.time()
    if now - tickers_cache["ts"] < TICKERS_TTL_S and tickers_cache["data"]:
        return tickers_cache["data"]
    for attempt in range(1, 5):
        try:
            _guard_rate()
            data = ex.fetch_tickers()
            tickers_cache["ts"], tickers_cache["data"] = now, data
            _relax_penalty()
            return data
        except ccxt.RateLimitExceeded as e:
            logging.error(f"rate limit tickers: {e}"); _penalize(attempt=attempt)
        except Exception as e:
            logging.error(f"tickers err: {e}"); time.sleep(1.2)
    return {}

def fetch_ohlcv(symbol, timeframe, limit):
    now = time.time()
    cached = ohlcv_cache.get(symbol)
    if cached and now - cached["ts"] < OHLCV_CACHE_TTL_S:
        return cached["df"]
    last_err = None
    for attempt in range(1, 6):
        try:
            _guard_rate(symbol)
            rows = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
            if not rows or len(rows) < 200:
                last_err = Exception("empty ohlcv")
                _penalize(symbol, attempt); continue
            df = pd.DataFrame(rows, columns=["ts","o","h","l","c","v"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            for col in ["o","h","l","c","v"]:
                df[col] = df[col].astype(float)
            ohlcv_cache[symbol] = {"ts": now, "df": df}
            _relax_penalty(symbol); _jitter()
            return df
        except ccxt.RateLimitExceeded as e:
            logging.error(f"rate limit {symbol}: {e}")
            _penalize(symbol, attempt)
        except ccxt.ExchangeError as e:
            if "50011" in str(e) or "Too Many Requests" in str(e):
                logging.error(f"OKX 50011 {symbol} attempt {attempt}")
                _penalize(symbol, attempt)
            else:
                logging.error(f"ex err {symbol}: {e}"); _jitter()
        except Exception as e:
            last_err = e; logging.error(f"ohlcv err {symbol}: {e}"); _jitter()
    raise last_err or Exception("ohlcv failed")

def last_price(symbol):
    try:
        _guard_rate(symbol)
        t = ex.fetch_ticker(symbol)
        _relax_penalty(symbol)
        return float(t["last"])
    except Exception:
        _penalize(symbol, 1); return None

def spread_pct(symbol):
    try:
        _guard_rate(symbol)
        ob = ex.fetch_order_book(symbol, limit=5)
        _relax_penalty(symbol)
        bid, ask = ob["bids"][0][0], ob["asks"][0][0]
        return (ask - bid) / ask if ask else 1.0
    except Exception:
        _penalize(symbol, 1); return 1.0

# ========= Universe =========
FALLBACK_UNIVERSE = [
    "BTC/USDT:USDT","ETH/USDT:USDT","SOL/USDT:USDT","BNB/USDT:USDT","XRP/USDT:USDT",
    "DOGE/USDT:USDT","ADA/USDT:USDT","TON/USDT:USDT","LINK/USDT:USDT","AVAX/USDT:USDT",
    "NEAR/USDT:USDT","ARB/USDT:USDT","OP/USDT:USDT","ATOM/USDT:USDT","LTC/USDT:USDT",
    "APT/USDT:USDT","SUI/USDT:USDT","FIL/USDT:USDT","ETC/USDT:USDT","PEPE/USDT:USDT",
    "UNI/USDT:USDT","AAVE/USDT:USDT","DOT/USDT:USDT","MATIC/USDT:USDT","SEI/USDT:USDT",
    "INJ/USDT:USDT","TIA/USDT:USDT","PYTH/USDT:USDT","WIF/USDT:USDT","BONK/USDT:USDT",
    "SATS/USDT:USDT","ORDI/USDT:USDT","JTO/USDT:USDT","JUP/USDT:USDT","RNDR/USDT:USDT",
    "KAS/USDT:USDT","TRX/USDT:USDT","FET/USDT:USDT","ENS/USDT:USDT","RUNE/USDT:USDT",
]

def top_symbols(limit):
    data = fetch_tickers_cached()
    syms = []
    for s, m in ex.markets.items():
        if not m.get("active"): continue
        if m.get("type") != "swap": continue
        if m.get("quote") != "USDT": continue
        syms.append(s)
    if not data:
        pref = [s for s in FALLBACK_UNIVERSE if s in syms]
        rest = [s for s in syms if s not in pref]
        return (pref + rest)[:limit]

    def qv(t):
        if not t: return 0.0
        v = t.get("quoteVolume") or t.get("baseVolume")
        if v: return float(v or 0)
        info = t.get("info") or {}
        return float(info.get("volCcy24h") or 0)
    syms.sort(key=lambda s: qv(data.get(s, {})), reverse=True)
    return syms[:limit]

# ========= المؤشرات =========
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

# ========= الإستراتيجية =========
def strat(sym, df, scout=False):
    c,h,l,v = df["c"], df["h"], df["l"], df["v"]
    last = float(c.iloc[-1])
    e20, e50 = ema(c,20), ema(c,50)
    atr14 = float(atr(h,l,c).iloc[-1])
    adx14, _, _ = adx(h,l,c)
    adx_cur = float(adx14.iloc[-1])

    v_med = v.rolling(20).median().iloc[-1] + 1e-12
    vsp_now = float(v.iloc[-1] / v_med)
    vsp_5   = float(v.iloc[-5:].max() / v_med)
    vsp = max(vsp_now, vsp_5)

    side = "LONG" if e20.iloc[-1] > e50.iloc[-1] else "SHORT" if e20.iloc[-1] < e50.iloc[-1] else None
    if not side:
        return None, {"reasons":["no_trend"], "atr":atr14}

    def crossed_recent(f, s, side, max_age=CROSS_MAX_AGE):
        for i in range(1, max_age+1):
            prev_ok = f.iloc[-i-1] <= s.iloc[-i-1] if side=="LONG" else f.iloc[-i-1] >= s.iloc[-i-1]
            now_ok  = f.iloc[-i]   >  s.iloc[-i]   if side=="LONG" else f.iloc[-i]   <  s.iloc[-i]
            if prev_ok and now_ok: return True, i
        return False, None
    cross_ok, cross_age = crossed_recent(e20, e50, side)

    ext_atr = abs(last - e20.iloc[-1]) / (atr14 + 1e-12)
    ext_limit = 1.5 if scout else EXTENSION_MAX_ATR
    if ext_atr > ext_limit:
        return None, {"reasons":["extended_from_ema20"], "atr":atr14}

    alignment_ok = False
    if CROSS_MODE != "recent_only" and not cross_ok:
        strong_adx = adx_cur >= (max(ADX_MIN+5, 23) if not scout else max(ADX_MIN+3, 21))
        near_ema20 = ext_atr <= (0.7 if not scout else 0.9)
        vol_ok     = vsp >= (VOL_SPIKE_MIN if not scout else max(1.0, VOL_SPIKE_MIN-0.1))
        with_trend_candle = (c.iloc[-1] > c.iloc[-2]) if side=="LONG" else (c.iloc[-1] < c.iloc[-2])
        alignment_ok = all([strong_adx, near_ema20, vol_ok, with_trend_candle])

    if not cross_ok and not alignment_ok:
        return None, {"reasons":["no_recent_cross"], "atr":atr14}

    if adx_cur < ADX_MIN:
        return None, {"reasons":[f"adx_{int(adx_cur)}"], "atr":atr14}

    if side=="LONG" and not (c.iloc[-1] > c.iloc[-2]):  return None, {"reasons":["no_confirm_long"], "atr":atr14}
    if side=="SHORT" and not (c.iloc[-1] < c.iloc[-2]): return None, {"reasons":["no_confirm_short"], "atr":atr14}

    r = float(rsi(c).iloc[-1])
    if (side=="LONG" and r>=71) or (side=="SHORT" and r<=29):
        return None, {"reasons":[f"rsi_extreme_{int(r)}"], "atr":atr14}

    if vsp < (VOL_SPIKE_MIN if not scout else max(1.0, VOL_SPIKE_MIN-0.1)):
        return None, {"reasons":[f"low_vol_{vsp:.1f}x"], "atr":atr14}

    reasons = []
    reasons.append(f"ema20/50_cross_{cross_age}c" if cross_ok else "alignment_adx_pullback")
    reasons += [f"vol_{vsp:.1f}x", f"adx_{int(adx_cur)}", "confirm", "rsi_ok"]
    return side, {"atr":atr14, "reasons": reasons}

def build_sl(entry, side, atr_val, ema50_last):
    if side=="LONG":
        return round(min(entry - SL_ATR_BASE*atr_val, ema50_last - SL_BEHIND_EMA50_ATR*atr_val), 6)
    else:
        return round(max(entry + SL_ATR_BASE*atr_val, ema50_last + SL_BEHIND_EMA50_ATR*atr_val), 6)

def targets(entry, atr_val, side):
    m1, m2, m3 = ATR_MULT_TP
    if side=="LONG":
        return [round(entry + m1*atr_val,6), round(entry + m2*atr_val,6), round(entry + m3*atr_val,6)]
    else:
        return [round(entry - m1*atr_val,6), round(entry - m2*atr_val,6), round(entry - m3*atr_val,6)]

def dollar_vol(df):
    try: return float((df["c"].iloc[-30:] * df["v"].iloc[-30:]).sum())
    except: return 0.0

def entry_window_ok():
    if USE_ENTRY_WINDOW != "on": return True
    now = datetime.now(timezone.utc)
    sec = (now.minute % 5)*60 + now.second
    return 30 <= sec <= 150

# ========= Scan/Send/Track =========
cooldown     = {}
open_trades  = {}
reject_count = {}
_last_diag   = 0
_last_hb     = 0
_scan_cursor = 0  # لتدوير الدُفعات

def _current_batch_symbols(all_syms):
    global _scan_cursor
    if not all_syms: return []
    step = max(1, SCAN_BATCH)
    start = (_scan_cursor * step) % len(all_syms)
    end   = min(len(all_syms), start + step)
    batch = all_syms[start:end]
    _scan_cursor = (_scan_cursor + 1) % max(1, (len(all_syms)+step-1)//step)
    return batch

def scan_cycle():
    global reject_count
    reject_count = {}

    try:
        all_syms = top_symbols(SCAN_TOP)
    except Exception as e:
        logging.error(f"load symbols: {e}"); all_syms = FALLBACK_UNIVERSE[:SCAN_TOP]

    batch = _current_batch_symbols(all_syms)
    # Scout Mode: فعّله إذا كان سبب الرفض السابق أحد هذين بكثرة
    scout = (reject_count.get("no_recent_cross",0) + reject_count.get("extended_from_ema20",0)) > 10

    picks = []
    for sym in batch:
        try:
            if cooldown.get(sym, 0) > time.time():
                reject_count["cooldown"] = reject_count.get("cooldown",0)+1
                continue

            spr = spread_pct(sym)
            if spr > MAX_SPREAD_PCT:
                reject_count["high_spread"] = reject_count.get("high_spread",0)+1
                continue

            df = fetch_ohlcv(sym, TIMEFRAME, BARS)
            dvol = dollar_vol(df)
            if dvol < MIN_DOLLAR_VOLUME:
                reject_count["low_dvol"] = reject_count.get("low_dvol",0)+1
                continue

            lp = float(df["c"].iloc[-1])
            side, meta = strat(sym, df, scout=scout)
            if side is None:
                reason = (meta.get("reasons") or ["unknown"])[0]
                reject_count[reason] = reject_count.get(reason,0)+1
                xl_append_reject(sym, reason, None, None, None, spr, dvol)
                continue

            e50 = float(ema(df["c"],50).iloc[-1])
            sl = build_sl(lp, side, meta["atr"], e50)
            tps = targets(lp, meta["atr"], side)

            rr = abs((tps[0]-lp) / (sl-lp)) if (sl-lp) != 0 else 0
            if rr < MIN_RR:
                reject_count["poor_rr"] = reject_count.get("poor_rr",0)+1
                xl_append_reject(sym, "poor_rr", None, None, None, spr, dvol)
                continue

            picks.append({
                "symbol": sym, "side": side, "entry": lp, "tps": tps, "sl": sl,
                "atr": meta["atr"], "reasons": meta["reasons"],
                "start_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "state": "OPEN", "tp_stage": 0,
                "last_bar_time": int(df["ts"].iloc[-1].timestamp())
            })
            if len(picks) >= 3:  # أقصى إرسال بالدورة
                break
        except Exception as e:
            logging.error(f"scan {sym}: {e}")
    return picks

def send_signal(sig):
    emoji = "🚀" if sig["side"]=="LONG" else "🔻"
    txt = (f"<b>⚡ v7_plus</b>\n"
           f"{emoji} <b>{sig['side']}</b> <code>{sig['symbol'].replace('/USDT:USDT','/USDT')}</code>\n\n"
           f"💰 Entry: <code>{sig['entry']}</code>\n"
           f"🎯 TP1: <code>{sig['tps'][0]}</code> | TP2: <code>{sig['tps'][1]}</code> | TP3: <code>{sig['tps'][2]}</code>\n"
           f"🛑 SL: <code>{sig['sl']}</code>\n\n"
           f"🔎 Signals: <i>{' • '.join(sig['reasons'][:6])}</i>\n"
           f"🛡️ بعد TP1: SL→BE • بعد TP2: تتبع EMA20")
    tg(txt)
    open_trades[sig["symbol"]] = sig
    cooldown[sig["symbol"]] = time.time() + COOLDOWN_HOURS*3600

def send_diag_if_due():
    global _last_diag
    now = time.time()
    if now - _last_diag < DIAG_EVERY_MIN*60: return
    reasons_sorted = sorted(reject_count.items(), key=lambda x: x[1], reverse=True)
    lines = [f"📋 <b>لماذا لا توجد توصيات (الدورة)</b>",
             f"عتبات: ATRx≤{EXTENSION_MAX_ATR} • ADX≥{ADX_MIN} • RR≥{MIN_RR} • vol≥{VOL_SPIKE_MIN}×",
             f"دفعة المسح: {SCAN_BATCH}/{SCAN_TOP}"]
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
    pl_pct = ((price - st["entry"]) / st["entry"] * 100) if st["side"]=="LONG" else ((st["entry"] - price)/st["entry"]*100)
    xl_append_signal(st, result, exit_t, dur, pl_pct, exit_reason)
    emoji = {"SL":"🛑","TP1":"✅","TP2":"🎉","TP3":"🏆"}.get(result, "✅")
    tg(f"{emoji} <b>{result}</b> {sym.replace('/USDT:USDT','/USDT')} @ <code>{price}</code>\n"
       f"P/L: <b>{pl_pct:+.2f}%</b> • مدة: {dur:.0f}m\n{exit_reason}")
    st["state"] = "CLOSED"

def maybe_update_trailing(st):
    sym = st["symbol"]
    try:
        df = fetch_ohlcv(sym, TIMEFRAME, 120)
        last_bar = int(df["ts"].iloc[-1].timestamp())
        if last_bar == st.get("last_bar_time"): return
        st["last_bar_time"] = last_bar
        e20 = float(ema(df["c"],20).iloc[-1])
        if st["side"]=="LONG": st["sl"] = max(st["sl"], e20)
        else:                   st["sl"] = min(st["sl"], e20)
    except Exception as e:
        logging.error(f"trail {sym}: {e}")

def track_cycle():
    for sym, st in list(open_trades.items()):
        if st.get("state") != "OPEN": continue
        lp = last_price(sym)
        if lp is None: continue
        tps, sl, entry = st["tps"], st["sl"], st["entry"]
        if st["side"]=="LONG":
            if lp <= sl: res_exit(sym, st, "SL", lp, "Stop Loss"); continue
            if st["tp_stage"] < 1 and lp >= tps[0]:
                st["tp_stage"] = 1; st["sl"] = entry
                tg(f"✅ TP1 • SL→BE • {sym.replace('/USDT:USDT','/USDT')} @ <code>{lp}</code>")
            elif st["tp_stage"] < 2 and lp >= tps[1]:
                st["tp_stage"] = 2; maybe_update_trailing(st)
                tg(f"🎯 TP2 • تتبع SL≈<code>{st['sl']:.6f}</code> • {sym.replace('/USDT:USDT','/USDT')}")
            elif lp >= tps[2]:
                res_exit(sym, st, "TP3", lp, "Target 3 🎯")
        else:
            if lp >= sl: res_exit(sym, st, "SL", lp, "Stop Loss"); continue
            if st["tp_stage"] < 1 and lp <= tps[0]:
                st["tp_stage"] = 1; st["sl"] = entry
                tg(f"✅ TP1 • SL→BE • {sym.replace('/USDT:USDT','/USDT')} @ <code>{lp}</code>")
            elif st["tp_stage"] < 2 and lp <= tps[1]:
                st["tp_stage"] = 2; maybe_update_trailing(st)
                tg(f"🎯 TP2 • تتبع SL≈<code>{st['sl']:.6f}</code> • {sym.replace('/USDT:USDT','/USDT')}")
            elif lp <= tps[2]:
                res_exit(sym, st, "TP3", lp, "Target 3 🎯")

def heartbeat_if_due():
    global _last_hb
    now = time.time()
    if now - _last_hb < HEARTBEAT_EVERY_MIN*60: return
    open_cnt = sum(1 for s in open_trades.values() if s.get("state")=="OPEN")
    tg(f"💓 <b>Heartbeat</b>\n⏱️ TF: {TIMEFRAME} • 🔝 Top: {SCAN_TOP} • Batch: {SCAN_BATCH}\nصفقات مفتوحة: {open_cnt}\nDiag كل {DIAG_EVERY_MIN}m • HB كل {HEARTBEAT_EVERY_MIN}m")
    _last_hb = now

# ========= بوت =========
_running = True
def handle_sig(signum, frame):
    global _running
    _running = False
signal.signal(signal.SIGINT, handle_sig)
signal.signal(signal.SIGTERM, handle_sig)

def bot_loop():
    logging.info("🚀 v7_plus bot running")
    tg("🤖 <b>Bot Started • v7_plus</b>\n"
       f"⏱️ TF: {TIMEFRAME} • 🔝 Top: {SCAN_TOP} • Batch: {SCAN_BATCH}\n"
       f"🛑 SL: max({SL_ATR_BASE}×ATR, خلف EMA50 {SL_BEHIND_EMA50_ATR}×ATR)\n"
       f"📈 شروط: EMA20/50 + Vol≥{VOL_SPIKE_MIN}× + ADX≥{ADX_MIN} • RR≥{MIN_RR}")

    while _running:
        try:
            picks = scan_cycle()
            if picks and not entry_window_ok():
                tg("⏳ نافذة الدخول مغلّقة (USE_ENTRY_WINDOW=on) — تجاهلت إشارات هذه الدورة.")
                picks = []
            for s in picks:
                send_signal(s); time.sleep(2)

            loops = max(1, SLEEP_BETWEEN // 5)
            for _ in range(loops):
                if not _running: break
                track_cycle()
                time.sleep(5)

            send_diag_if_due()
            heartbeat_if_due()
        except Exception as e:
            logging.error(f"main loop: {e}")
            tg(f"⚠️ Runtime error: <code>{str(e)[:180]}</code>")
            time.sleep(3)

# ========= Web + KeepAlive =========
app = FastAPI()

@app.get("/")
@app.get("/healthz")
def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}

def self_keepalive():
    # يطلب /healthz كل 4 دقائق حتى لا تنام النسخة المجانية قدر الإمكان
    if KEEPALIVE != "on": return
    url = os.getenv("KEEPALIVE_URL") or f"http://127.0.0.1:{int(os.getenv('PORT','10000'))}/healthz"
    while _running:
        try:
            requests.get(url, timeout=5)
        except Exception:
            pass
        time.sleep(240)

def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=self_keepalive, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

if __name__ == "__main__":
    run()
