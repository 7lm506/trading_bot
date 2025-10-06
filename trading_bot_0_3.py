# trading_bot_improved_v4.0.py
# نسخة محسّنة - معالجة أخطاء + keepalive + أداء أفضل
# المتطلبات: pip install ccxt fastapi uvicorn pandas requests

import os, json, asyncio, time, io, csv, sqlite3, random, math, traceback
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone

import requests
import pandas as pd
import ccxt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

# ========================== [ ENV ] ==========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()
EXCHANGE_ENV   = os.getenv("EXCHANGE", "").strip().lower()
SYMBOLS_ENV    = os.getenv("SYMBOLS", "").strip()
TIMEFRAME_ENV  = os.getenv("TIMEFRAME", "").strip()

# ========================== [ إعدادات محسّنة ] ==========================
EXCHANGE_NAME = EXCHANGE_ENV or "okx"
TIMEFRAME     = TIMEFRAME_ENV or "5m"
SYMBOLS_MODE  = SYMBOLS_ENV or "ALL"

MIN_CONFIDENCE         = 52
MIN_ATR_PCT            = 0.11
MIN_AVG_VOL_USDT       = 40_000

RSI_LONG_MIN,  RSI_LONG_MAX  = 33, 72
RSI_SHORT_MIN, RSI_SHORT_MAX = 28, 67

BB_BANDWIDTH_MAX       = 0.048
BB_BANDWIDTH_MIN       = 0.005
ALLOW_NO_SQUEEZE       = False

REQUIRE_TREND          = True

TP_PCTS                = [0.7, 1.4, 2.2, 3.2]
ATR_SL_MULT            = 1.85
SL_LOOKBACK            = 14
MIN_SL_PCT, MAX_SL_PCT = 0.3, 2.2

SCAN_INTERVAL                 = 45
MIN_SIGNAL_GAP_SEC            = 5
MAX_ALERTS_PER_CYCLE          = 10
COOLDOWN_PER_SYMBOL_CANDLES   = 6
MAX_SYMBOLS                   = 120

NO_SIG_EVERY_N_CYCLES         = 0
NO_SIG_EVERY_MINUTES          = 0

KEEPALIVE_URL      = os.getenv("RENDER_EXTERNAL_URL", "")
KEEPALIVE_INTERVAL = 180

BUILD_UTC     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
APP_VERSION   = f"4.0.0-IMPROVED ({BUILD_UTC})"
POLL_COMMANDS = True
POLL_INTERVAL = 10

LOG_DB_PATH = "bot_stats.db"

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise SystemExit("ENV مفقودة: TELEGRAM_TOKEN و CHAT_ID مطلوبة.")

TG_API   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
SEND_URL = TG_API + "/sendMessage"
DOC_URL  = TG_API + "/sendDocument"
TG_GET_UPDATES    = TG_API + "/getUpdates"
TG_DELETE_WEBHOOK = TG_API + "/deleteWebhook"

_last_send_ts = 0
_error_bucket = []
_error_last_flush = 0
ERROR_FLUSH_EVERY = 300

def _reply_kb(rows: List[List[str]]) -> str:
    return json.dumps({
        "keyboard":[[{"text":t} for t in row] for row in rows],
        "resize_keyboard": True,
        "is_persistent": True
    })

def send_telegram(text: str, reply_to_message_id: Optional[int]=None,
                  reply_markup: Optional[str]=None) -> Optional[int]:
    global _last_send_ts
    now = time.time()
    if now - _last_send_ts < MIN_SIGNAL_GAP_SEC:
        time.sleep(max(0, MIN_SIGNAL_GAP_SEC - (now - _last_send_ts)))
    data = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}
    if reply_to_message_id: data["reply_to_message_id"] = reply_to_message_id
    if reply_markup: data["reply_markup"] = reply_markup
    try:
        r = requests.post(SEND_URL, data=data, timeout=25).json()
        if not r.get("ok"):
            print("Telegram error:", r)
            return None
        _last_send_ts = time.time()
        return r["result"]["message_id"]
    except Exception as e:
        print("Telegram send error:", e)
        return None

def send_document(filename: str, file_bytes: bytes, caption: str="") -> bool:
    try:
        files={"document":(filename, io.BytesIO(file_bytes))}
        data={"chat_id": CHAT_ID, "caption": caption}
        r=requests.post(DOC_URL, data=data, files=files, timeout=60).json()
        if not r.get("ok"): print("send_document error:", r); return False
        return True
    except Exception as e:
        print("send_document exception:", e); return False

def start_menu_markup() -> str:
    return _reply_kb([
        ["الإحصائيات", "تحليل متقدم"],
        ["الأسباب", "آخر الإشارات"],
        ["المفتوحة", "تصدير CSV"],
        ["تصدير تحليلي", "تحديث القائمة"]
    ])

def send_start_menu():
    send_telegram("القائمة الرئيسية:", reply_markup=start_menu_markup())

def ema(s: pd.Series, n:int)->pd.Series: 
    return s.ewm(span=n, adjust=False).mean()

def rsi(s: pd.Series, n=14)->pd.Series:
    d=s.diff()
    up=d.clip(lower=0)
    dn=-d.clip(upper=0)
    ma_up=up.ewm(com=n-1, adjust=False).mean()
    ma_dn=dn.ewm(com=n-1, adjust=False).mean()
    rs=ma_up/(ma_dn.replace(0,1e-12))
    return 100-(100/(1+rs))

def macd(s: pd.Series, f=12, sl=26, sig=9)->Tuple[pd.Series,pd.Series]:
    ef, es = ema(s,f), ema(s,sl)
    line=ef-es
    return line, ema(line,sig)

def bollinger(s: pd.Series, n=20, k=2.0):
    ma=s.rolling(n).mean()
    sd=s.rolling(n).std(ddof=0)
    up=ma+k*sd
    dn=ma-k*sd
    bw=(up-dn)/ma.replace(0, 1e-12)
    return ma, up, dn, bw

def atr(df: pd.DataFrame, n=14):
    h,l,c = df["high"], df["low"], df["close"]
    tr=pd.concat([(h-l).abs(),(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def clamp(x,a,b): 
    return max(a, min(b,x))

EXC={"bybit":ccxt.bybit,"okx":ccxt.okx,"kucoinfutures":ccxt.kucoinfutures,"bitget":ccxt.bitget,
     "gate":ccxt.gate,"binance":ccxt.binance,"krakenfutures":ccxt.krakenfutures}

def make_exchange(name:str):
    klass=EXC.get(name, ccxt.okx)
    cfg={"enableRateLimit":True,"timeout":20000,
         "options":{"defaultType":"swap","defaultSubType":"linear"}}
    return klass(cfg)

def load_markets_linear_only(ex):
    last=None
    for i,b in enumerate([1.5,3,6],1):
        try: 
            ex.load_markets(reload=True, params={"category":"linear","type":"swap"})
            return
        except Exception as e:
            last=e
            print(f"[load_markets {i}] {type(e).__name__}: {str(e)[:160]}")
            time.sleep(b)
    raise last

def try_failover(primary:str)->Tuple[ccxt.Exchange,str]:
    last=None
    for name in [primary,"okx","kucoinfutures","bitget","gate","binance"]:
        try:
            ex=make_exchange(name)
            load_markets_linear_only(ex)
            return ex,name
        except Exception as e:
            last=e
            print("[failover]", name, "failed:", type(e).__name__, str(e)[:100])
    raise last or SystemExit("No exchange available.")

def normalize_symbols_for_exchange(ex, syms:List[str])->List[str]:
    if ex.id=="bybit":
        return [s + (":USDT" if s.endswith("/USDT") and ":USDT" not in s else "") for s in syms]
    return syms

def list_all_futures_symbols(ex)->List[str]:
    syms=[]
    for m in ex.markets.values():
        if m.get("contract") and (m.get("swap") or m.get("future")) and m.get("active",True) is not False:
            syms.append(m["symbol"])
    syms=sorted(set(syms))
    if MAX_SYMBOLS>0: 
        syms=syms[:MAX_SYMBOLS]
    return normalize_symbols_for_exchange(ex, syms)

def parse_symbols(ex, val:str)->List[str]:
    key=(val or "").strip().upper()
    if key in ("ALL","AUTO_FUTURES","AUTO","AUTO_SWAP","AUTO_LINEAR"):
        return list_all_futures_symbols(ex)
    syms=[s.strip() for s in (val or "").split(",") if s.strip()]
    syms=normalize_symbols_for_exchange(ex, syms)
    if MAX_SYMBOLS>0: 
        syms=syms[:MAX_SYMBOLS]
    return syms

async def fetch_ohlcv_safe(ex, symbol:str, timeframe:str, limit:int):
    for attempt in range(2):
        try:
            params={}
            if ex.id=="bybit": 
                params={"category":"linear"}
            elif ex.id=="okx": 
                params={"instType":"SWAP"}
            
            ohlcv=await asyncio.to_thread(ex.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit, params=params)
            
            if not ohlcv or len(ohlcv)<60: 
                return None
            
            df=pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
            
            if df["close"].isna().any() or (df["close"] == 0).any():
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                return None
            
            df["ts"]=pd.to_datetime(df["ts"], unit="ms", utc=True)
            df.set_index("ts", inplace=True)
            return df
        except Exception as e:
            if attempt == 0:
                await asyncio.sleep(1)
                continue
            return f"خطأ: {ex.id} {type(e).__name__}"
    return None

async def fetch_ticker_price(ex, symbol:str)->Optional[float]:
    try:
        t=await asyncio.to_thread(ex.fetch_ticker, symbol)
        v=t.get("last") or t.get("close") or t.get("info",{}).get("lastPrice")
        return float(v) if v is not None else None
    except Exception: 
        return None

def unix_now()->int: 
    return int(datetime.now(timezone.utc).timestamp())

def symbol_pretty(s:str)->str: 
    return s.replace(":USDT","")

open_trades: Dict[str,Dict]={}
signal_state: Dict[str,Dict]={}
_last_cycle_alerts=0

def db_conn(): 
    con=sqlite3.connect(LOG_DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    return con

def db_init():
    con=db_conn()
    con.execute("""CREATE TABLE IF NOT EXISTS signals(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, exchange TEXT, symbol TEXT,
      side TEXT, entry REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL, tp4 REAL, confidence INTEGER, msg_id INTEGER);""")
    con.execute("""CREATE TABLE IF NOT EXISTS outcomes(
      id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, ts INTEGER, event TEXT, idx INTEGER, price REAL);""")
    con.execute("""CREATE TABLE IF NOT EXISTS nosignal_reasons(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, exchange TEXT, symbol TEXT, reasons TEXT);""")
    con.execute("""CREATE TABLE IF NOT EXISTS errors(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, exchange TEXT, symbol TEXT, message TEXT);""")
    con.commit()
    con.close()

def db_insert_signal(ts,ex,sym,side,entry,sl,tps,conf,msg_id)->int:
    con=db_conn()
    cur=con.cursor()
    cur.execute("INSERT INTO signals(ts,exchange,symbol,side,entry,sl,tp1,tp2,tp3,tp4,confidence,msg_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts,ex,sym,side,entry,sl,tps[0],tps[1],tps[2],tps[3],conf,msg_id))
    con.commit()
    sid=cur.lastrowid
    con.close()
    return sid

def db_insert_outcome(signal_id,ts,event,idx,price):
    con=db_conn()
    con.execute("INSERT INTO outcomes(signal_id,ts,event,idx,price) VALUES(?,?,?,?,?)",
                (signal_id,ts,event,idx,price))
    con.commit()
    con.close()

def db_insert_nosignal(ts,ex,sym,rsn:Dict):
    con=db_conn()
    con.execute("INSERT INTO nosignal_reasons(ts,exchange,symbol,reasons) VALUES(?,?,?,?)",
                (ts,ex,sym,json.dumps(rsn,ensure_ascii=False)))
    con.commit()
    con.close()

def db_insert_error(ts,ex,sym,msg):
    con=db_conn()
    con.execute("INSERT INTO errors(ts,exchange,symbol,message) VALUES(?,?,?,?)",
                (ts,ex,sym,msg))
    con.commit()
    con.close()

def _f(x)->float:
    v=float(x)
    if math.isnan(v) or math.isinf(v) or v < 0:
        raise ValueError("invalid value")
    return v

def compute_confidence_improved(side:str, bw_now:float, c_now:float, 
                                band_now:float, macd_now:float, macd_sig:float, 
                                r14:float, atr_now:float, ema50:float, ema200:float,
                                volume_ratio:float)->int:
    
    if bw_now > BB_BANDWIDTH_MAX or bw_now < BB_BANDWIDTH_MIN:
        return 0
    
    squeeze_score = clamp((BB_BANDWIDTH_MAX - bw_now) / BB_BANDWIDTH_MAX, 0, 1)
    
    breakout_dist = abs(c_now - band_now)
    breakout_score = clamp(breakout_dist / max(atr_now, 1e-9), 0, 1.3)
    breakout_score = min(breakout_score, 1.0)
    
    if breakout_score < 0.18:
        return 0
    
    macd_diff = abs(macd_now - macd_sig)
    macd_score = clamp(macd_diff / max(atr_now, 1e-9), 0, 1)
    
    rsi_target = 55 if side == "LONG" else 45
    rsi_dev = abs(r14 - rsi_target)
    rsi_score = clamp(1.0 - (rsi_dev / 22.0), 0, 1)
    
    trend_strength = abs(ema50 - ema200) / max(ema200, 1e-9)
    trend_score = clamp(trend_strength * 70, 0, 1)
    
    if side == "LONG" and ema50 <= ema200:
        trend_score *= 0.6
    if side == "SHORT" and ema50 >= ema200:
        trend_score *= 0.6
    
    volume_score = clamp((volume_ratio - 0.65) * 1.4, 0, 1)
    
    if volume_ratio < 0.55:
        return 0
    
    price_score = 1.0 if ((side == "LONG" and c_now > ema50) or (side == "SHORT" and c_now < ema50)) else 0.4
    
    confidence = int(round(100 * (
        0.22 * squeeze_score +
        0.21 * breakout_score +
        0.15 * macd_score +
        0.13 * rsi_score +
        0.12 * trend_score +
        0.11 * volume_score +
        0.06 * price_score
    )))
    
    return max(0, min(100, confidence))

def smart_signal_improved(df: pd.DataFrame) -> Tuple[Optional[Dict], Dict]:
    if df is None or len(df)<65: 
        return None, {"insufficient_data":True}

    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]
    
    ma20, bb_up, bb_dn, bb_bw = bollinger(c, 20, 2.0)
    macd_line, macd_sig = macd(c, 12, 26, 9)
    r = rsi(c, 14)
    atr14 = atr(df, 14)
    ema50 = ema(c, 50)
    ema200 = ema(c, 200)
    
    avg_volume = v.tail(30).mean()
    current_volume = v.iloc[-2]
    volume_ratio = current_volume / max(avg_volume, 1e-9)

    i2, i1 = -3, -2
    try:
        c_prev = _f(c.iloc[i2])
        c_now = _f(c.iloc[i1])
        up_prev = _f(bb_up.iloc[i2])
        up_now = _f(bb_up.iloc[i1])
        dn_prev = _f(bb_dn.iloc[i2])
        dn_now = _f(bb_dn.iloc[i1])
        bw_now = _f(bb_bw.iloc[i1])
        macd_now = _f(macd_line.iloc[i1])
        sig_now = _f(macd_sig.iloc[i1])
        r14 = _f(r.iloc[i1])
        atr_now = _f(atr14.iloc[i1])
        e50 = _f(ema50.iloc[i1])
        e200 = _f(ema200.iloc[i1])
        ma20_now = _f(ma20.iloc[i1])
    except Exception as e:
        return None, {"data_error": str(e)[:50]}

    atr_pct = 100 * atr_now / max(c_now, 1e-9)
    
    try:
        avg_usdt = float((df["volume"] * c).tail(30).mean())
        if math.isnan(avg_usdt) or math.isinf(avg_usdt) or avg_usdt < 0:
            avg_usdt = 0.0
    except Exception:
        avg_usdt = 0.0

    if atr_pct < MIN_ATR_PCT:
        return None, {"atr_pct_low": round(atr_pct, 3)}
    
    if avg_usdt < MIN_AVG_VOL_USDT:
        return None, {"avg_vol_usdt_low": int(avg_usdt)}
    
    if not (BB_BANDWIDTH_MIN <= bw_now <= BB_BANDWIDTH_MAX):
        return None, {"bw_out_of_range": round(bw_now, 5)}
    
    trend_up = e50 > e200 * 1.001
    trend_down = e50 < e200 * 0.999
    
    crossed_up = bool((c_prev <= up_prev) and (c_now > up_now))
    crossed_down = bool((c_prev >= dn_prev) and (c_now < dn_now))
    
    macd_bullish = macd_now > sig_now
    macd_bearish = macd_now < sig_now

    long_ok = bool(
        (trend_up or not REQUIRE_TREND) and
        (crossed_up or (c_now > up_now and c_now > ma20_now)) and
        macd_bullish and
        (RSI_LONG_MIN < r14 < RSI_LONG_MAX) and
        volume_ratio > 0.6
    )
    
    short_ok = bool(
        (trend_down or not REQUIRE_TREND) and
        (crossed_down or (c_now < dn_now and c_now < ma20_now)) and
        macd_bearish and
        (RSI_SHORT_MIN < r14 < RSI_SHORT_MAX) and
        volume_ratio > 0.6
    )

    if not (long_ok or short_ok):
        return None, {
            "bw": round(bw_now, 5),
            "rsi": round(r14, 2),
            "vol_ratio": round(volume_ratio, 2),
            "trend": "up" if trend_up else ("down" if trend_down else "flat")
        }

    side = "LONG" if long_ok else "SHORT"
    band_now = up_now if side == "LONG" else dn_now
    
    conf = compute_confidence_improved(
        side, bw_now, c_now, band_now, 
        macd_now, sig_now, r14, atr_now,
        e50, e200, volume_ratio
    )

    recent_lows = float(l.tail(SL_LOOKBACK).min())
    recent_highs = float(h.tail(SL_LOOKBACK).max())
    entry = c_now
    atr_dist = ATR_SL_MULT * atr_now
    
    if side == "LONG":
        sl_atr = entry - atr_dist
        sl_swing = recent_lows - (0.08 * atr_now)
        sl_raw = max(sl_atr, sl_swing)
        
        min_gap = entry * (MIN_SL_PCT / 100.0)
        max_gap = entry * (MAX_SL_PCT / 100.0)
        gap = entry - sl_raw
        
        if gap < min_gap: 
            sl_raw = entry - min_gap
        if gap > max_gap: 
            sl_raw = entry - max_gap
    else:
        sl_atr = entry + atr_dist
        sl_swing = recent_highs + (0.08 * atr_now)
        sl_raw = min(sl_atr, sl_swing)
        
        min_gap = entry * (MIN_SL_PCT / 100.0)
        max_gap = entry * (MAX_SL_PCT / 100.0)
        gap = sl_raw - entry
        
        if gap < min_gap: 
            sl_raw = entry + min_gap
        if gap > max_gap: 
            sl_raw = entry + max_gap
    
    sl = float(sl_raw)
    
    if side == "LONG":
        tps = [entry * (1 + p/100.0) for p in TP_PCTS]
    else:
        tps = [entry * (1 - p/100.0) for p in TP_PCTS]
    
    risk = abs(entry - sl)
    reward = abs(tps[2] - entry)
    rr_ratio = reward / risk if risk > 0 else 0
    
    if rr_ratio < 1.15:
        return None, {"poor_rr": round(rr_ratio, 2)}
    
    return (
        {
            "side": side,
            "entry": float(entry),
            "sl": sl,
            "tps": [float(x) for x in tps],
            "confidence": int(conf),
            "rr_ratio": round(rr_ratio, 2),
            "volume_ratio": round(volume_ratio, 2)
        }, 
        {}
    )

def crossed_levels(side:str, price:float, tps:List[float], sl:float, hit:List[bool]):
    if price is None: 
        return None
    
    if side == "LONG" and price <= sl: 
        return ("SL", -1)
    if side == "SHORT" and price >= sl: 
        return ("SL", -1)
    
    for idx, (tp, was_hit) in enumerate(zip(tps, hit)):
        if was_hit: 
            continue
        if side == "LONG" and price >= tp: 
            return ("TP", idx)
        if side == "SHORT" and price <= tp: 
            return ("TP", idx)
    
    return None

def pct_profit(side:str, entry:float, exit_price:float)->float:
    if side == "LONG":
        return (exit_price / entry - 1.0) * 100.0
    else:
        return (1.0 - exit_price / entry) * 100.0

def elapsed_text(start_ts:int, end_ts:int)->str:
    mins = max(0, end_ts - start_ts) // 60
    if mins < 60:
        return f"{mins}د"
    else:
        return f"{mins//60}س {mins%60}د"

app = FastAPI()

@app.get("/")
@app.head("/")
async def root():
    return JSONResponse({
        "ok": True,
        "status": "running",
        "version": APP_VERSION,
        "exchange": getattr(app.state, "exchange_id", EXCHANGE_NAME),
        "tf": TIMEFRAME,
        "symbols": len(getattr(app.state, "symbols", [])),
        "open_trades": len(open_trades),
        "uptime": int(time.time() - getattr(app.state, "start_time", time.time()))
    })

@app.get("/health")
@app.head("/health")
async def health():
    return JSONResponse({"status": "healthy", "timestamp": unix_now()})

@app.get("/stats")
def stats():
    return {
        "open_trades": len(open_trades),
        "cycle_count": getattr(app.state, "cycle_count", 0),
        "symbols_count": len(getattr(app.state, "symbols", [])),
        "exchange": getattr(app.state, "exchange_id", ""),
        "last_scan": getattr(app.state, "last_scan_time", 0)
    }

async def fetch_and_signal(ex, symbol:str):
    global _last_cycle_alerts
    
    out = await fetch_ohlcv_safe(ex, symbol, TIMEFRAME, 300)
    
    if isinstance(out, str): 
        db_insert_error(unix_now(), ex.id, symbol, out)
        return
    
    if out is None or len(out) < 65: 
        return

    st = signal_state.get(symbol, {})
    closed_idx = len(out) - 2
    
    if closed_idx < st.get("cooldown_until_idx", -999999): 
        return
    
    if symbol in open_trades: 
        return

    try:
        sig, reasons = smart_signal_improved(out)
    except Exception as e:
        _error_bucket.append(f"{symbol}: {type(e).__name__}")
        return

    if not sig:
        db_insert_nosignal(unix_now(), ex.id, symbol, reasons or {})
        return
    
    if sig["confidence"] < MIN_CONFIDENCE:
        db_insert_nosignal(unix_now(), ex.id, symbol, {**reasons, "conf_low": sig["confidence"]})
        return
    
    if _last_cycle_alerts >= MAX_ALERTS_PER_CYCLE:
        return

    pretty = symbol_pretty(symbol)
    side_txt = "شراء" if sig["side"] == "LONG" else "بيع"
    side_emoji = "🟢" if sig["side"] == "LONG" else "🔴"
    entry, sl, tps, conf = sig["entry"], sig["sl"], sig["tps"], sig["confidence"]
    rr = sig["rr_ratio"]
    
    if conf >= 70:
        strength_emoji = "🔥🔥"
    elif conf >= 60:
        strength_emoji = "🔥"
    else:
        strength_emoji = "⭐"
    
    msg_text = (
        f"{side_emoji} {side_txt} - #{pretty}\n"
        f"━━━━━━━━━━━━━━━━━\n\n"
        f"ثقة: {conf}% {strength_emoji} | R/R: 1:{rr}\n\n"
        f"دخول: {entry:.6f}\n"
        f"ستوب: {sl:.6f}\n\n"
        f"أهداف:\n"
        f"  TP1: {tps[0]:.6f} ({TP_PCTS[0]}%)\n"
        f"  TP2: {tps[1]:.6f} ({TP_PCTS[1]}%)\n"
        f"  TP3: {tps[2]:.6f} ({TP_PCTS[2]}%)\n"
        f"  TP4: {tps[3]:.6f} ({TP_PCTS[3]}%)\n\n"
        f"حجم: {sig.get('volume_ratio', 1):.1f}x"
    )
    
    mid = send_telegram(msg_text)
    
    if mid:
        ts_now = unix_now()
        open_trades[symbol] = {
            "side": sig["side"],
            "entry": entry,
            "sl": sl,
            "tps": tps,
            "hit": [False] * 4,
            "msg_id": mid,
            "signal_id": None,
            "opened_ts": ts_now
        }
        
        signal_state[symbol] = {
            "cooldown_until_idx": closed_idx + COOLDOWN_PER_SYMBOL_CANDLES
        }
        
        sid = db_insert_signal(
            ts_now, app.state.exchange.id, symbol, 
            sig["side"], entry, sl, tps, conf, mid
        )
        
        open_trades[symbol]["signal_id"] = sid
        _last_cycle_alerts += 1

async def check_open_trades(ex):
    for sym, pos in list(open_trades.items()):
        price = await fetch_ticker_price(ex, sym)
        res = crossed_levels(pos["side"], price, pos["tps"], pos["sl"], pos["hit"])
        
        if not res: 
            continue
        
        kind, idx = res
        ts = unix_now()
        
        if kind == "SL":
            pr = pct_profit(pos["side"], pos["entry"], price or pos["sl"])
            
            msg = (
                f"❌ #{symbol_pretty(sym)}\n"
                f"━━━━━━━━━━━━━\n"
                f"ستوب لوس\n\n"
                f"خسارة: {round(pr, 2)}%\n"
                f"مدة: {elapsed_text(pos['opened_ts'], ts)}"
            )
            
            send_telegram(msg)
            
            if pos.get("signal_id"): 
                db_insert_outcome(pos["signal_id"], ts, "SL", -1, price or 0.0)
            
            del open_trades[sym]
            
        else:
            pos["hit"][idx] = True
            tp = pos["tps"][idx]
            pr = pct_profit(pos["side"], pos["entry"], tp if price is None else price)
            
            emoji_map = ["✅", "⭐", "🔥", "💎"]
            emoji = emoji_map[idx] if idx < len(emoji_map) else "✅"
            
            msg = (
                f"{emoji} #{symbol_pretty(sym)}\n"
                f"━━━━━━━━━━━━━\n"
                f"هدف {idx+1}\n\n"
                f"ربح: +{round(pr, 2)}%\n"
                f"مدة: {elapsed_text(pos['opened_ts'], ts)}"
            )
            
            send_telegram(msg)
            
            if pos.get("signal_id"): 
                db_insert_outcome(pos["signal_id"], ts, f"TP{idx+1}", idx, price or tp)
            
            if all(pos["hit"]): 
                del open_trades[sym]

async def scan_once(ex, symbols:List[str]):
    global _last_cycle_alerts, _error_last_flush, _error_bucket
    
    _last_cycle_alerts = 0
    
    await check_open_trades(ex)
    
    if not symbols: 
        return
    
    random.shuffle(symbols)
    
    sem = asyncio.Semaphore(6)
    
    async def worker(s):
        async with sem: 
            await fetch_and_signal(ex, s)
    
    await asyncio.gather(*[asyncio.create_task(worker(s)) for s in symbols])
    
    app.state.last_scan_time = unix_now()

    now = time.time()
    if _error_bucket and (now - _error_last_flush >= ERROR_FLUSH_EVERY):
        sample = "\n".join(_error_bucket[:3])
        send_telegram(f"⚠️ أخطاء ({len(_error_bucket)}):\n{sample}")
        _error_bucket.clear()
        _error_last_flush = now

def db_text_stats(days:int=1)->str:
    try:
        con = db_conn()
        cur = con.cursor()
        
        cur.execute("SELECT COUNT(*) FROM signals WHERE ts >= strftime('%s','now', ?)", (f"-{days} day",))
        total = cur.fetchone()[0] or 0
        
        cur.execute("""
            SELECT SUM(CASE WHEN event LIKE 'TP%' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN event='SL' THEN 1 ELSE 0 END)
            FROM outcomes WHERE ts >= strftime('%s','now', ?)
        """, (f"-{days} day",))
        
        row = cur.fetchone() or (0, 0)
        tp, sl = row[0] or 0, row[1] or 0
        
        success_rate = (tp / (tp + sl) * 100) if (tp + sl) > 0 else 0
        
        con.close()
        
        if total == 0:
            return "لا توجد بيانات بعد."
        
        return (
            f"إحصائيات ({days}ي):\n"
            f"━━━━━━━━━━━━━\n"
            f"إشارات: {total}\n"
            f"أهداف: {tp} | ستوب: {sl}\n"
            f"نجاح: {success_rate:.1f}%"
        )
    except Exception as e:
        return f"خطأ: {e}"

def db_detailed_stats(days:int=7)->str:
    try:
        con = db_conn()
        cur = con.cursor()
        
        cur.execute("SELECT COUNT(*), AVG(confidence) FROM signals WHERE ts >= strftime('%s','now', ?)", (f"-{days} day",))
        total, avg_conf = cur.fetchone()
        total = total or 0
        avg_conf = avg_conf or 0
        
        cur.execute("""
            SELECT side, COUNT(*) FROM signals WHERE ts >= strftime('%s','now', ?) GROUP BY side
        """, (f"-{days} day",))
        side_stats = dict(cur.fetchall())
        
        cur.execute("""
            SELECT o.event, COUNT(*) FROM outcomes o
            JOIN signals s ON o.signal_id = s.id
            WHERE o.ts >= strftime('%s','now', ?) GROUP BY o.event
        """, (f"-{days} day",))
        outcome_stats = dict(cur.fetchall())
        
        con.close()
        
        if total == 0:
            return "لا توجد بيانات."
        
        total_tp = sum(v for k, v in outcome_stats.items() if k.startswith("TP"))
        total_sl = outcome_stats.get("SL", 0)
        win_rate = (total_tp / (total_tp + total_sl) * 100) if (total_tp + total_sl) > 0 else 0
        
        output = [
            f"تحليل ({days}ي)",
            "═" * 20,
            f"إشارات: {total}",
            f"ثقة: {avg_conf:.1f}%",
            "",
            "اتجاهات:"
        ]
        
        for side, count in side_stats.items():
            output.append(f"  {side}: {count}")
        
        output.append("")
        output.append(f"نجاح: {win_rate:.1f}%")
        
        for event, count in sorted(outcome_stats.items()):
            output.append(f"  {event}: {count}")
        
        return "\n".join(output)
    except Exception as e:
        return f"خطأ: {e}"

def db_text_reasons(window:str="1d")->str:
    unit = window[-1].lower()
    num = int(''.join([ch for ch in window if ch.isdigit()]) or 1)
    sql_win = f"-{num} {'hour' if unit=='h' else 'day'}"
    
    try:
        con = db_conn()
        cur = con.cursor()
        cur.execute("SELECT reasons FROM nosignal_reasons WHERE ts >= strftime('%s','now', ?)", (sql_win,))
        rows = cur.fetchall()
        con.close()
        
        if not rows:
            return "لا توجد أسباب."
        
        from collections import Counter
        cnt = Counter()
        
        for (js,) in rows:
            try:
                d = json.loads(js) if isinstance(js, str) else {}
                if isinstance(d, dict):
                    for k in d.keys():
                        cnt[k] += 1
            except:
                pass
        
        lines = [f"أسباب ({window}):"]
        lines.append("━" * 15)
        lines.extend([f"{i+1}. {k}: {v}" for i, (k, v) in enumerate(cnt.most_common(6))])
        
        return "\n".join(lines)
    except Exception as e:
        return f"خطأ: {e}"

def db_text_last(limit:int=10)->str:
    try:
        con = db_conn()
        cur = con.cursor()
        cur.execute("""
            SELECT s.symbol, s.side, s.confidence,
                   (SELECT event FROM outcomes o WHERE o.signal_id=s.id ORDER BY o.ts LIMIT 1)
            FROM signals s ORDER BY s.id DESC LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        con.close()
        
        if not rows:
            return "لا إشارات."
        
        out = ["آخر الإشارات:"]
        out.append("━" * 15)
        
        for r in rows:
            result = r[3] or "قيد التنفيذ"
            emoji = "✅" if result and result.startswith("TP") else ("❌" if result == "SL" else "⏳")
            out.append(f"{emoji} {symbol_pretty(r[0])} {r[1]} {r[2]}% → {result}")
        
        return "\n".join(out)
    except Exception as e:
        return f"خطأ: {e}"

def db_text_open()->str:
    if not open_trades:
        return "لا صفقات مفتوحة."
    
    out = ["صفقات مفتوحة:"]
    out.append("━" * 15)
    
    for s, p in open_trades.items():
        hit_count = sum(p["hit"])
        out.append(f"{symbol_pretty(s)} {p['side']} | أهداف: {hit_count}/4")
    
    return "\n".join(out)

def export_csv_bytes(days:int=14)->bytes:
    con = db_conn()
    cur = con.cursor()
    cur.execute("""
        SELECT s.id, s.ts, s.exchange, s.symbol, s.side, s.entry, s.sl, 
               s.tp1, s.tp2, s.tp3, s.tp4, s.confidence,
               (SELECT GROUP_CONCAT(event||':'||price,'|') FROM outcomes o WHERE o.signal_id=s.id)
        FROM signals s WHERE s.ts >= strftime('%s','now', ?) ORDER BY s.id DESC
    """, (f"-{days} day",))
    rows = cur.fetchall()
    con.close()
    
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id","ts","exchange","symbol","side","entry","sl","tp1","tp2","tp3","tp4","confidence","outcomes"])
    for r in rows:
        w.writerow(r)
    
    return out.getvalue().encode("utf-8")

def db_export_analysis_csv(days:int=14)->bytes:
    con = db_conn()
    cur = con.cursor()
    
    cur.execute("""
        SELECT s.id, datetime(s.ts, 'unixepoch'), s.exchange, s.symbol, s.side, 
               s.entry, s.sl, s.tp1, s.tp2, s.tp3, s.tp4, s.confidence,
               (s.entry - s.sl) / s.entry * 100 as risk_pct,
               (SELECT event FROM outcomes o WHERE o.signal_id = s.id ORDER BY o.ts LIMIT 1),
               (SELECT price FROM outcomes o WHERE o.signal_id = s.id ORDER BY o.ts LIMIT 1),
               CASE 
                   WHEN EXISTS(SELECT 1 FROM outcomes o WHERE o.signal_id = s.id AND o.event = 'SL') THEN 'LOSS'
                   WHEN EXISTS(SELECT 1 FROM outcomes o WHERE o.signal_id = s.id AND o.event LIKE 'TP%') THEN 'WIN'
                   ELSE 'OPEN'
               END
        FROM signals s WHERE s.ts >= strftime('%s', 'now', ?) ORDER BY s.ts DESC
    """, (f"-{days} day",))
    
    rows = cur.fetchall()
    con.close()
    
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ID","Time","Exchange","Symbol","Side","Entry","SL","TP1","TP2","TP3","TP4","Conf","Risk%","Outcome","Price","Status"])
    for r in rows:
        w.writerow(r)
    
    return out.getvalue().encode("utf-8")

TG_OFFSET = 0

def tg_delete_webhook():
    try:
        requests.post(TG_DELETE_WEBHOOK, data={"drop_pending_updates": False}, timeout=10)
    except:
        pass

def parse_cmd(text:str)->Tuple[str,str]:
    t = (text or "").strip()
    if t.startswith("/"):
        parts = t.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if "@" in cmd:
            cmd = cmd.split("@", 1)[0]
        return cmd, arg
    return t, ""

async def poll_telegram_commands():
    if not POLL_COMMANDS:
        return
    
    tg_delete_webhook()
    global TG_OFFSET
    
    while True:
        try:
            r = requests.get(TG_GET_UPDATES, params={"timeout": 25, "offset": TG_OFFSET + 1}, timeout=35).json()
            
            if r.get("ok"):
                for upd in r.get("result", []):
                    TG_OFFSET = max(TG_OFFSET, upd["update_id"])
                    msg = upd.get("message") or upd.get("edited_message")
                    
                    if not msg or str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                        continue
                    
                    text = msg.get("text", "")
                    cmd, arg = parse_cmd(text)

                    if cmd in ("/start", "تحديث القائمة"):
                        send_start_menu()
                    elif cmd in ("الإحصائيات", "/stats"):
                        send_telegram(db_text_stats(int(arg) if arg.isdigit() else 1))
                    elif cmd in ("تحليل متقدم", "/analysis"):
                        send_telegram(db_detailed_stats(int(arg) if arg.isdigit() else 7))
                    elif cmd in ("الأسباب", "/reasons"):
                        send_telegram(db_text_reasons(arg or "1d"))
                    elif cmd in ("آخر الإشارات", "/last"):
                        send_telegram(db_text_last(int(arg) if arg.isdigit() else 10))
                    elif cmd in ("المفتوحة", "/open"):
                        send_telegram(db_text_open())
                    elif cmd in ("تصدير CSV", "/export"):
                        send_document(f"signals.csv", export_csv_bytes(int(arg) if arg.isdigit() else 14))
                    elif cmd in ("تصدير تحليلي", "/export_analysis"):
                        send_document(f"analysis.csv", db_export_analysis_csv(int(arg) if arg.isdigit() else 14))
                    elif cmd == "/version":
                        send_telegram(f"v{APP_VERSION}")
                    else:
                        send_start_menu()
        except Exception as e:
            print("poll error:", e)
        await asyncio.sleep(POLL_INTERVAL)

async def keepalive_task():
    if not KEEPALIVE_URL:
        return
    
    print(f"[Keepalive] Starting with URL: {KEEPALIVE_URL}")
    
    while True:
        try:
            r = requests.get(KEEPALIVE_URL, timeout=10)
            print(f"[Keepalive] Ping successful: {r.status_code}")
        except Exception as e:
            print(f"[Keepalive] Error: {e}")
        
        await asyncio.sleep(KEEPALIVE_INTERVAL)

app.state.exchange = None
app.state.exchange_id = EXCHANGE_NAME
app.state.symbols = []
app.state.cycle_count = 0
app.state.last_no_sig_ts = 0
app.state.start_time = time.time()
app.state.last_scan_time = 0

def attempt_build():
    ex, used = try_failover(EXCHANGE_NAME)
    syms = parse_symbols(ex, SYMBOLS_MODE)
    app.state.exchange, app.state.exchange_id, app.state.symbols = ex, used, syms

@app.on_event("startup")
async def startup_event():
    db_init()
    
    send_telegram(f"بوت v{APP_VERSION}\nجاري التحميل...", reply_markup=start_menu_markup())
    
    attempt_build()
    
    syms = app.state.symbols
    ex_id = app.state.exchange_id
    
    send_telegram(
        f"اكتمل!\n━━━━━━━━━━━━━\n"
        f"{ex_id} | {TIMEFRAME}\n"
        f"أزواج: {len(syms)}\n"
        f"ثقة: {MIN_CONFIDENCE}%+\n"
        f"Cooldown: {COOLDOWN_PER_SYMBOL_CANDLES} شمعات\n"
        f"━━━━━━━━━━━━━\n"
        f"{', '.join([symbol_pretty(s) for s in syms[:10]])}"
        f"{f'... +{len(syms)-10}' if len(syms) > 10 else ''}"
    )
    
    asyncio.create_task(runner())
    asyncio.create_task(poll_telegram_commands())
    asyncio.create_task(keepalive_task())

async def runner():
    while True:
        try:
            if not app.state.symbols:
                attempt_build()
            
            await scan_once(app.state.exchange, app.state.symbols)
            app.state.cycle_count += 1
        except Exception as e:
            _error_bucket.append(f"{type(e).__name__}: {str(e)[:50]}")
            print(f"[Runner Error] {e}")
        
        await asyncio.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    print(f"Starting server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
