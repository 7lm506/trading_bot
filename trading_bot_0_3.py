# trading_bot_optimized_v2.0.0.py
# النسخة المحسّنة - استراتيجية أقوى وأذكى 🚀
# المتطلبات: pip install ccxt fastapi uvicorn pandas requests ta

import os, json, asyncio, time, io, csv, sqlite3, random, math, traceback
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone

import requests
import pandas as pd
import ccxt
from fastapi import FastAPI
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

# ✨ إعدادات الفلترة المحسّنة - أكثر ذكاءً
MIN_CONFIDENCE         = 48  # خفّضنا الحد لزيادة الفرص
MIN_ATR_PCT            = 0.08  # قبول تقلبات أقل
MIN_AVG_VOL_USDT       = 30_000  # حجم أقل للمزيد من الفرص

# نطاقات RSI محسّنة لصيد الفرص الذهبية
RSI_LONG_MIN,  RSI_LONG_MAX  = 35, 75  # نطاق أوسع
RSI_SHORT_MIN, RSI_SHORT_MAX = 25, 65

# Bollinger Bands - أكثر مرونة
BB_BANDWIDTH_MAX       = 0.055  # قبول انضغاطات أوسع قليلاً
BB_BANDWIDTH_MAX_SOFT  = 0.10
ALLOW_NO_SQUEEZE       = True

REQUIRE_TREND          = False  # نتداول مع وضد الترند

# ✨ أهداف وستوب أفضل - نسب ربح/خسارة محسّنة
TP_PCTS                = [0.4, 0.8, 1.4, 2.2]  # أهداف أقرب وأكثر واقعية
ATR_SL_MULT            = 2.2  # ستوب أوسع قليلاً لتجنب الاهتزازات
SL_LOOKBACK            = 18  # نظرة أعمق للسوينغات
MIN_SL_PCT, MAX_SL_PCT = 0.25, 2.5  # ستوب أكثر مرونة

# إعدادات المسح
SCAN_INTERVAL                 = 45  # مسح أسرع
MIN_SIGNAL_GAP_SEC            = 4
MAX_ALERTS_PER_CYCLE          = 10  # المزيد من الإشارات
COOLDOWN_PER_SYMBOL_CANDLES   = 6  # كولداون أقصر
MAX_SYMBOLS                   = 150  # المزيد من الأزواج

NO_SIG_EVERY_N_CYCLES         = 0
NO_SIG_EVERY_MINUTES          = 0

KEEPALIVE_URL      = ""
KEEPALIVE_INTERVAL = 240

BUILD_UTC     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
APP_VERSION   = f"2.0.0-OPTIMIZED ({BUILD_UTC})"
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
        ["📊 الإحصائيات", "📈 تحليل متقدم"],
        ["📄 الأسباب", "📜 آخر الإشارات"],
        ["📌 المفتوحة", "⬇️ تصدير CSV"],
        ["📋 تصدير تحليلي", "🔁 تحديث القائمة"]
    ])

def send_start_menu():
    send_telegram("القائمة الرئيسية:", reply_markup=start_menu_markup())

# ================== مؤشرات محسّنة ==================
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

# ✨ مؤشر جديد: Stochastic للزخم
def stochastic(df: pd.DataFrame, k_period=14, d_period=3):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    
    lowest_low = low.rolling(window=k_period).min()
    highest_high = high.rolling(window=k_period).max()
    
    k = 100 * (close - lowest_low) / (highest_high - lowest_low).replace(0, 1e-12)
    d = k.rolling(window=d_period).mean()
    
    return k, d

# ✨ مؤشر جديد: ADX للقوة
def adx(df: pd.DataFrame, period=14):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    
    plus_dm = high.diff()
    minus_dm = -low.diff()
    
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0
    
    tr = atr(df, 1)
    atr_period = tr.rolling(window=period).mean()
    
    plus_di = 100 * (plus_dm.rolling(window=period).mean() / atr_period)
    minus_di = 100 * (minus_dm.rolling(window=period).mean() / atr_period)
    
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, 1e-12)
    adx_val = dx.rolling(window=period).mean()
    
    return adx_val, plus_di, minus_di

def clamp(x,a,b): 
    return max(a, min(b,x))

# ================== CCXT & Symbols ==================
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

# ================== جلب البيانات ==================
async def fetch_ohlcv_safe(ex, symbol:str, timeframe:str, limit:int):
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
        df["ts"]=pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.set_index("ts", inplace=True)
        return df
    except Exception as e:
        return f"خطأ المنصة: {ex.id} {type(e).__name__} {str(e)[:200]}"

async def fetch_ticker_price(ex, symbol:str)->Optional[float]:
    try:
        t=await asyncio.to_thread(ex.fetch_ticker, symbol)
        v=t.get("last") or t.get("close") or t.get("info",{}).get("lastPrice")
        return float(v) if v is not None else None
    except Exception: 
        return None

# ================== DB/Helpers ==================
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

# ================== ✨ الاستراتيجية المحسّنة والجبارة ==================
def _f(x)->float:
    v=float(x)
    if math.isnan(v) or math.isinf(v): 
        raise ValueError("nan/inf")
    return v

def compute_confidence_advanced(side:str, bw_now:float, c_prev:float, c_now:float, 
                                band_now:float, macd_now:float, macd_sig:float, 
                                r14:float, atr_now:float, stoch_k:float, stoch_d:float,
                                adx_val:float, plus_di:float, minus_di:float,
                                volume_ratio:float)->int:
    """
    حساب الثقة المحسّن مع مؤشرات إضافية
    """
    # 1. انضغاط البولينجر (كلما أقل كلما أفضل)
    tight = clamp((BB_BANDWIDTH_MAX_SOFT - bw_now)/max(BB_BANDWIDTH_MAX_SOFT,1e-9), 0, 1)
    
    # 2. قوة الاختراق
    breakout = clamp(((c_now - band_now) if side=="LONG" else (band_now - c_now))/max(atr_now,1e-9), 0, 1.5)
    breakout = min(breakout, 1.0)
    
    # 3. زخم MACD
    mom = clamp(abs(macd_now - macd_sig)/max(atr_now,1e-9), 0, 1)
    
    # 4. RSI المثالي
    rsi_target = 60 if side=="LONG" else 40
    rsi_score = clamp(1 - abs(r14 - rsi_target)/25.0, 0, 1)
    
    # 5. ✨ Stochastic - هل في زخم؟
    if side == "LONG":
        stoch_score = clamp((stoch_k - 30) / 40.0, 0, 1)  # نبي فوق 30 للشراء
    else:
        stoch_score = clamp((70 - stoch_k) / 40.0, 0, 1)  # نبي تحت 70 للبيع
    
    # 6. ✨ ADX - قوة الترند
    adx_score = clamp((adx_val - 20) / 30.0, 0, 1)  # أفضل لو ADX فوق 20
    
    # 7. ✨ اتجاه DI
    if side == "LONG":
        di_score = clamp((plus_di - minus_di) / 20.0, 0, 1)
    else:
        di_score = clamp((minus_di - plus_di) / 20.0, 0, 1)
    
    # 8. ✨ الحجم - هل السوق نشط؟
    volume_score = clamp(volume_ratio - 0.8, 0, 1)  # نبي حجم فوق المعدل
    
    # الأوزان المحسّنة
    confidence = int(round(100 * (
        0.18 * tight +          # انضغاط
        0.22 * breakout +       # اختراق
        0.12 * mom +            # MACD
        0.12 * rsi_score +      # RSI
        0.12 * stoch_score +    # Stochastic جديد
        0.10 * adx_score +      # ADX جديد
        0.08 * di_score +       # DI جديد
        0.06 * volume_score     # Volume جديد
    )))
    
    return max(0, min(100, confidence))

def smart_signal_optimized(df: pd.DataFrame) -> Tuple[Optional[Dict], Dict]:
    """
    الاستراتيجية المحسّنة مع مؤشرات إضافية
    """
    if df is None or len(df)<60: 
        return None, {"insufficient_data":True}

    c=df["close"]
    h=df["high"]
    l=df["low"]
    v=df["volume"]
    
    # المؤشرات الأساسية
    ma20, bb_up, bb_dn, bb_bw = bollinger(c, 20, 2.0)
    macd_line, macd_sig = macd(c, 12, 26, 9)
    r = rsi(c, 14)
    atr14 = atr(df, 14)
    ema50 = ema(c, 50)
    ema200 = ema(c, 200)
    
    # ✨ المؤشرات الجديدة
    stoch_k, stoch_d = stochastic(df, 14, 3)
    adx_val, plus_di, minus_di = adx(df, 14)
    
    # نسبة الحجم الحالي للمتوسط
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
        ma20_prev = _f(ma20.iloc[i2])
        
        # ✨ المؤشرات الجديدة
        stoch_k_now = _f(stoch_k.iloc[i1])
        stoch_d_now = _f(stoch_d.iloc[i1])
        adx_now = _f(adx_val.iloc[i1])
        plus_di_now = _f(plus_di.iloc[i1])
        minus_di_now = _f(minus_di.iloc[i1])
        
    except Exception:
        return None, {"index_or_nan": True}

    atr_pct = 100 * atr_now / max(c_now, 1e-9)
    
    try:
        avg_usdt = float((df["volume"] * c).tail(30).mean())
        if math.isnan(avg_usdt) or math.isinf(avg_usdt): 
            avg_usdt = 0.0
    except Exception:
        avg_usdt = 0.0

    # ✨ فلترة أقل صرامة
    if (atr_pct < MIN_ATR_PCT) or (avg_usdt < MIN_AVG_VOL_USDT):
        return None, {
            "atr_pct": round(atr_pct, 3), 
            "avg_vol_usdt": int(avg_usdt), 
            "note": "low_vol_or_range"
        }

    # شروط Bollinger
    squeeze_strict = bw_now <= BB_BANDWIDTH_MAX
    squeeze_soft = bw_now <= BB_BANDWIDTH_MAX_SOFT
    squeeze_ok = bool(squeeze_strict or (ALLOW_NO_SQUEEZE and squeeze_soft))

    # اتجاهات الترند
    trend_up = e50 > e200
    trend_down = e50 < e200
    trend_ok_long = (not REQUIRE_TREND) or trend_up
    trend_ok_short = (not REQUIRE_TREND) or trend_down

    # اختراقات
    crossed_up = bool((c_prev <= up_prev) and (c_now > up_now))
    crossed_down = bool((c_prev >= dn_prev) and (c_now < dn_now))

    # ✨ شروط السعر المحسّنة - أكثر مرونة
    long_price_ok = bool(
        crossed_up or 
        ((c_now > up_now) and (c_prev > ma20_prev)) or
        (c_now > ma20_prev and c_now > e50)  # شرط إضافي
    )
    
    short_price_ok = bool(
        crossed_down or 
        ((c_now < dn_now) and (c_prev < ma20_prev)) or
        (c_now < ma20_prev and c_now < e50)  # شرط إضافي
    )

    # ✨ زخم محسّن مع Stochastic
    long_momentum = bool(
        (macd_now > sig_now) or 
        (c_now > e50) or
        (stoch_k_now > stoch_d_now and stoch_k_now > 25)  # Stochastic صاعد
    )
    
    short_momentum = bool(
        (macd_now < sig_now) or 
        (c_now < e50) or
        (stoch_k_now < stoch_d_now and stoch_k_now < 75)  # Stochastic نازل
    )

    # RSI
    rsi_long_ok = bool(RSI_LONG_MIN < r14 < RSI_LONG_MAX)
    rsi_short_ok = bool(RSI_SHORT_MIN < r14 < RSI_SHORT_MAX)
    
    # ✨ ADX - نتأكد أن السوق فيه قوة
    adx_ok = adx_now > 18  # ADX فوق 18 يعني فيه قوة حركة

    # ✨ الشروط النهائية المحسّنة
    long_ok = bool(
        squeeze_ok and 
        long_price_ok and 
        long_momentum and 
        rsi_long_ok and 
        trend_ok_long and
        (adx_ok or volume_ratio > 1.2)  # إما ADX قوي أو حجم عالي
    )
    
    short_ok = bool(
        squeeze_ok and 
        short_price_ok and 
        short_momentum and 
        rsi_short_ok and 
        trend_ok_short and
        (adx_ok or volume_ratio > 1.2)
    )

    if not (long_ok or short_ok):
        return None, {
            "squeeze": squeeze_ok,
            "bw_now": round(bw_now, 5),
            "rsi14": round(r14, 2),
            "stoch_k": round(stoch_k_now, 2),
            "adx": round(adx_now, 2),
            "volume_ratio": round(volume_ratio, 2),
            "cross_up": crossed_up,
            "cross_down": crossed_down,
            "macd_vs_signal": f"{round(macd_now,4)} vs {round(sig_now,4)}",
            "trend": "up" if trend_up else ("down" if trend_down else "flat"),
        }

    side = "LONG" if long_ok else "SHORT"
    band_now = up_now if side == "LONG" else dn_now
    
    # ✨ حساب الثقة المحسّن
    conf = compute_confidence_advanced(
        side, bw_now, c_prev, c_now, band_now, 
        macd_now, sig_now, r14, atr_now,
        stoch_k_now, stoch_d_now, adx_now,
        plus_di_now, minus_di_now, volume_ratio
    )

    # ✨ ستوب لوس ديناميكي أذكى
    recent_lows = float(l.tail(SL_LOOKBACK).min())
    recent_highs = float(h.tail(SL_LOOKBACK).max())
    entry = c_now
    atr_dist = ATR_SL_MULT * max(atr_now, 1e-12)
    
    if side == "LONG":
        # ستوب لوس للشراء
        sl_atr = entry - atr_dist
        sl_swing = recent_lows - (0.1 * atr_now)  # تحت السوينغ بقليل
        sl_raw = max(sl_atr, sl_swing)  # نختار الأبعد للأمان
        
        min_gap = entry * (MIN_SL_PCT / 100.0)
        max_gap = entry * (MAX_SL_PCT / 100.0)
        gap = entry - sl_raw
        
        if gap < min_gap: 
            sl_raw = entry - min_gap
        if gap > max_gap: 
            sl_raw = entry - max_gap
    else:
        # ستوب لوس للبيع
        sl_atr = entry + atr_dist
        sl_swing = recent_highs + (0.1 * atr_now)  # فوق السوينغ بقليل
        sl_raw = min(sl_atr, sl_swing)  # نختار الأبعد للأمان
        
        min_gap = entry * (MIN_SL_PCT / 100.0)
        max_gap = entry * (MAX_SL_PCT / 100.0)
        gap = sl_raw - entry
        
        if gap < min_gap: 
            sl_raw = entry + min_gap
        if gap > max_gap: 
            sl_raw = entry + max_gap
    
    sl = float(sl_raw)
    
    # ✨ أهداف محسّنة
    if side == "LONG":
        tps = [entry * (1 + p/100.0) for p in TP_PCTS]
    else:
        tps = [entry * (1 - p/100.0) for p in TP_PCTS]
    
    return (
        {
            "side": side,
            "entry": float(entry),
            "sl": sl,
            "tps": [float(x) for x in tps],
            "confidence": int(conf),
            "adx": round(adx_now, 2),
            "volume_ratio": round(volume_ratio, 2),
            "stoch": round(stoch_k_now, 2)
        }, 
        {}
    )

# ================== TP/SL ==================
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
        return f"{mins} دقيقة"
    else:
        return f"{mins//60} ساعة {mins%60} دقيقة"

# ================== FastAPI ==================
app = FastAPI()

@app.get("/")
def root():
    return {
        "ok": True,
        "version": APP_VERSION,
        "exchange": getattr(app.state, "exchange_id", EXCHANGE_NAME),
        "tf": TIMEFRAME,
        "symbols": len(getattr(app.state, "symbols", [])),
        "open_trades": len(open_trades)
    }

@app.get("/stats")
def stats():
    """إحصائيات سريعة عبر API"""
    return {
        "open_trades": len(open_trades),
        "cycle_count": getattr(app.state, "cycle_count", 0),
        "symbols_count": len(getattr(app.state, "symbols", [])),
        "exchange": getattr(app.state, "exchange_id", ""),
    }

# ================== Scan & Trade ==================
async def fetch_and_signal(ex, symbol:str):
    global _last_cycle_alerts
    
    out = await fetch_ohlcv_safe(ex, symbol, TIMEFRAME, 300)
    
    if isinstance(out, str): 
        db_insert_error(unix_now(), ex.id, symbol, out)
        return
    
    if out is None or len(out) < 60: 
        db_insert_nosignal(unix_now(), ex.id, symbol, {"insufficient_data": True})
        return

    st = signal_state.get(symbol, {})
    closed_idx = len(out) - 2
    
    if closed_idx < st.get("cooldown_until_idx", -999999): 
        return
    
    if symbol in open_trades: 
        return

    try:
        sig, reasons = smart_signal_optimized(out)
    except Exception as e:
        _error_bucket.append(f"{symbol}: {type(e).__name__} {str(e)}")
        return

    if not sig:
        db_insert_nosignal(unix_now(), ex.id, symbol, reasons or {"note": "no_setup"})
        return
    
    if sig["confidence"] < MIN_CONFIDENCE:
        db_insert_nosignal(unix_now(), ex.id, symbol, {
            **reasons, 
            "confidence_lt_min": sig["confidence"]
        })
        return
    
    if _last_cycle_alerts >= MAX_ALERTS_PER_CYCLE:
        db_insert_nosignal(unix_now(), ex.id, symbol, {"cycle_cap_reached": True})
        return

    # ✨ رسالة محسّنة مع معلومات أكثر
    pretty = symbol_pretty(symbol)
    side_txt = "طويل 🟢" if sig["side"] == "LONG" else "قصير 🔴"
    entry, sl, tps, conf = sig["entry"], sig["sl"], sig["tps"], sig["confidence"]
    
    # حساب نسبة المخاطرة/الربح
    risk = abs(entry - sl)
    reward = abs(tps[2] - entry)  # نستخدم TP3 كهدف رئيسي
    rr_ratio = reward / risk if risk > 0 else 0
    
    msg_text = (
        f"🎯 إشارة جديدة - #{pretty}\n"
        f"{'━' * 30}\n\n"
        f"الاتجاه: {side_txt}\n"
        f"الثقة: {conf}% {'🔥' if conf >= 65 else '⭐' if conf >= 55 else '💫'}\n\n"
        f"📍 نقطة الدخول: {entry:.6f}\n"
        f"🛑 وقف الخسارة: {sl:.6f}\n\n"
        f"🎯 الأهداف:\n"
        f"  TP1: {tps[0]:.6f} ({TP_PCTS[0]}%)\n"
        f"  TP2: {tps[1]:.6f} ({TP_PCTS[1]}%)\n"
        f"  TP3: {tps[2]:.6f} ({TP_PCTS[2]}%) ⭐\n"
        f"  TP4: {tps[3]:.6f} ({TP_PCTS[3]}%)\n\n"
        f"📊 نسبة R/R: 1:{rr_ratio:.2f}\n"
        f"📈 ADX: {sig.get('adx', 0):.1f} | Vol: {sig.get('volume_ratio', 1):.1f}x\n"
        f"{'━' * 30}"
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
    """فحص الصفقات المفتوحة وتحديث حالتها"""
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
                f"{'━' * 25}\n"
                f"تم ضرب وقف الخسارة\n\n"
                f"📉 الخسارة: {round(pr, 2)}%\n"
                f"⏰ المدة: {elapsed_text(pos['opened_ts'], ts)}\n"
                f"💡 السعر: {price or pos['sl']:.6f}"
            )
            
            send_telegram(msg)
            
            if pos.get("signal_id"): 
                db_insert_outcome(pos["signal_id"], ts, "SL", -1, price or 0.0)
            
            del open_trades[sym]
            
        else:
            pos["hit"][idx] = True
            tp = pos["tps"][idx]
            pr = pct_profit(pos["side"], pos["entry"], tp if price is None else price)
            
            # رموز مختلفة حسب الهدف
            emoji_map = ["🎯", "⭐", "🔥", "💎"]
            emoji = emoji_map[idx] if idx < len(emoji_map) else "✅"
            
            msg = (
                f"{emoji} #{symbol_pretty(sym)}\n"
                f"{'━' * 25}\n"
                f"تم الوصول إلى الهدف {idx+1}\n\n"
                f"📈 الربح: +{round(pr, 2)}%\n"
                f"⏰ المدة: {elapsed_text(pos['opened_ts'], ts)}\n"
                f"💰 السعر: {price or tp:.6f}"
            )
            
            send_telegram(msg)
            
            if pos.get("signal_id"): 
                db_insert_outcome(pos["signal_id"], ts, f"TP{idx+1}", idx, price or tp)
            
            # إذا كل الأهداف اتحققت، نغلق الصفقة
            if all(pos["hit"]): 
                total_profit = sum([
                    pct_profit(pos["side"], pos["entry"], pos["tps"][i]) 
                    for i in range(4)
                ]) / 4
                
                final_msg = (
                    f"🎉 #{symbol_pretty(sym)}\n"
                    f"{'━' * 25}\n"
                    f"تم إغلاق الصفقة بنجاح!\n\n"
                    f"📊 متوسط الربح: +{round(total_profit, 2)}%\n"
                    f"⏰ إجمالي المدة: {elapsed_text(pos['opened_ts'], ts)}"
                )
                
                send_telegram(final_msg)
                del open_trades[sym]

async def scan_once(ex, symbols:List[str]):
    """دورة مسح واحدة"""
    global _last_cycle_alerts, _error_last_flush, _error_bucket
    
    _last_cycle_alerts = 0
    
    # فحص الصفقات المفتوحة أولاً
    await check_open_trades(ex)
    
    if not symbols: 
        return
    
    # خلط عشوائي للرموز لتوزيع الفحص
    random.shuffle(symbols)
    
    # سيمافور للتحكم بالطلبات المتزامنة
    sem = asyncio.Semaphore(5)  # زدنا من 3 إلى 5
    
    async def worker(s):
        async with sem: 
            await fetch_and_signal(ex, s)
    
    await asyncio.gather(*[asyncio.create_task(worker(s)) for s in symbols])

    # flush errors بشكل دوري
    now = time.time()
    if _error_bucket and (now - _error_last_flush >= ERROR_FLUSH_EVERY):
        sample = "\n".join(_error_bucket[:8])
        send_telegram(f"⚠️ ملخص أخطاء ({len(_error_bucket)}):\n{sample}")
        _error_bucket.clear()
        _error_last_flush = now

# ================== تقارير/أوامر محسّنة ==================
def db_text_stats(days:int=1)->str:
    """إحصائيات أساسية سريعة"""
    try:
        con = db_conn()
        cur = con.cursor()
        
        cur.execute(
            "SELECT COUNT(*) FROM signals WHERE ts >= strftime('%s','now', ?)", 
            (f"-{days} day",)
        )
        total = cur.fetchone()[0] or 0
        
        cur.execute("""
            SELECT SUM(CASE WHEN event LIKE 'TP%' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN event='SL' THEN 1 ELSE 0 END)
            FROM outcomes WHERE ts >= strftime('%s','now', ?)
        """, (f"-{days} day",))
        
        row = cur.fetchone() or (0, 0)
        tp, sl = row[0] or 0, row[1] or 0
        
        # حساب نسبة النجاح
        if total > 0:
            success_rate = (tp / (tp + sl) * 100) if (tp + sl) > 0 else 0
        else:
            success_rate = 0
        
        con.close()
        
        if total == 0 and tp == 0 and sl == 0: 
            return "لا توجد بيانات كافية بعد."
        
        return (
            f"📊 إحصائيات آخر {days} يوم:\n"
            f"{'━' * 25}\n"
            f"📝 إجمالي الإشارات: {total}\n"
            f"✅ أهداف محققة: {tp}\n"
            f"❌ ستوب لوس: {sl}\n"
            f"📈 نسبة النجاح: {success_rate:.1f}%\n"
            f"{'━' * 25}"
        )
    except Exception as e:
        return f"⚠️ خطأ الإحصائيات: {e}"

def db_detailed_stats(days:int=7)->str:
    """إحصائيات تفصيلية متقدمة للتطوير"""
    try:
        con = db_conn()
        cur = con.cursor()
        
        # 1. إحصائيات عامة
        cur.execute("""
            SELECT COUNT(*), 
                   AVG(confidence),
                   COUNT(DISTINCT symbol)
            FROM signals 
            WHERE ts >= strftime('%s','now', ?)
        """, (f"-{days} day",))
        
        total_signals, avg_conf, unique_symbols = cur.fetchone()
        total_signals = total_signals or 0
        avg_conf = avg_conf or 0
        unique_symbols = unique_symbols or 0
        
        # 2. تفاصيل LONG vs SHORT
        cur.execute("""
            SELECT side, COUNT(*), AVG(confidence)
            FROM signals 
            WHERE ts >= strftime('%s','now', ?)
            GROUP BY side
        """, (f"-{days} day",))
        
        side_stats = {}
        for row in cur.fetchall():
            side_stats[row[0]] = {"count": row[1], "avg_conf": row[2] or 0}
        
        # 3. نتائج حسب TP/SL
        cur.execute("""
            SELECT o.event, COUNT(*), AVG(s.confidence)
            FROM outcomes o
            JOIN signals s ON o.signal_id = s.id
            WHERE o.ts >= strftime('%s','now', ?)
            GROUP BY o.event
        """, (f"-{days} day",))
        
        outcome_stats = {}
        for row in cur.fetchall():
            outcome_stats[row[0]] = {"count": row[1], "avg_conf": row[2] or 0}
        
        # 4. أفضل وأسوأ الأزواج
        cur.execute("""
            SELECT s.symbol,
                   COUNT(*) as total,
                   SUM(CASE WHEN o.event LIKE 'TP%' THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN o.event = 'SL' THEN 1 ELSE 0 END) as losses
            FROM signals s
            LEFT JOIN outcomes o ON s.id = o.signal_id
            WHERE s.ts >= strftime('%s','now', ?)
            GROUP BY s.symbol
            HAVING total >= 2
            ORDER BY (CAST(wins AS FLOAT) / NULLIF(wins + losses, 0)) DESC
            LIMIT 5
        """, (f"-{days} day",))
        
        best_pairs = cur.fetchall()
        
        # 5. توزيع الثقة
        cur.execute("""
            SELECT 
                CASE 
                    WHEN confidence < 50 THEN '40-50'
                    WHEN confidence < 55 THEN '50-55'
                    WHEN confidence < 60 THEN '55-60'
                    WHEN confidence < 65 THEN '60-65'
                    WHEN confidence < 70 THEN '65-70'
                    ELSE '70+'
                END as conf_range,
                COUNT(*) as count,
                SUM(CASE WHEN o.event LIKE 'TP%' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN o.event = 'SL' THEN 1 ELSE 0 END) as losses
            FROM signals s
            LEFT JOIN outcomes o ON s.id = o.signal_id
            WHERE s.ts >= strftime('%s','now', ?)
            GROUP BY conf_range
            ORDER BY conf_range
        """, (f"-{days} day",))
        
        conf_distribution = cur.fetchall()
        
        # 6. متوسط وقت الصفقات
        cur.execute("""
            SELECT AVG(o.ts - s.ts) / 60.0 as avg_minutes
            FROM signals s
            JOIN outcomes o ON s.id = o.signal_id
            WHERE s.ts >= strftime('%s','now', ?)
        """, (f"-{days} day",))
        
        avg_duration = cur.fetchone()[0] or 0
        
        # 7. أسباب عدم الإشارات (أهم 5)
        cur.execute("""
            SELECT reasons
            FROM nosignal_reasons
            WHERE ts >= strftime('%s','now', ?)
        """, (f"-{days} day",))
        
        from collections import Counter
        reason_counter = Counter()
        for (js,) in cur.fetchall():
            try:
                d = json.loads(js) if isinstance(js, str) else {}
                if isinstance(d, dict):
                    for k in d.keys():
                        reason_counter[k] += 1
            except:
                pass
        
        top_reasons = reason_counter.most_common(5)
        
        con.close()
        
        # بناء التقرير
        if total_signals == 0:
            return "لا توجد بيانات كافية للتحليل التفصيلي."
        
        output = [
            f"📊 تقرير تحليلي متقدم ({days} يوم)",
            "═" * 40,
            "",
            "▶️ نظرة عامة:",
            f"  • إجمالي الإشارات: {total_signals}",
            f"  • متوسط الثقة: {avg_conf:.1f}%",
            f"  • أزواج مختلفة: {unique_symbols}",
            f"  • متوسط مدة الصفقة: {avg_duration:.0f} دقيقة",
            "",
            "▶️ توزيع الاتجاهات:"
        ]
        
        for side, data in side_stats.items():
            output.append(f"  • {side}: {data['count']} إشارة (ثقة: {data['avg_conf']:.1f}%)")
        
        output.append("")
        output.append("▶️ النتائج حسب النوع:")
        
        total_tp = sum(v["count"] for k, v in outcome_stats.items() if k.startswith("TP"))
        total_sl = outcome_stats.get("SL", {}).get("count", 0)
        
        if total_tp + total_sl > 0:
            win_rate = (total_tp / (total_tp + total_sl)) * 100
            output.append(f"  • نسبة النجاح الكلية: {win_rate:.1f}%")
        
        for event, data in sorted(outcome_stats.items()):
            output.append(f"  • {event}: {data['count']} (ثقة متوسطة: {data['avg_conf']:.1f}%)")
        
        output.append("")
        output.append("▶️ توزيع الثقة vs النتائج:")
        
        for conf_range, count, wins, losses in conf_distribution:
            wins = wins or 0
            losses = losses or 0
            if wins + losses > 0:
                wr = (wins / (wins + losses)) * 100
                output.append(f"  • {conf_range}%: {count} إشارات → WR: {wr:.1f}%")
            else:
                output.append(f"  • {conf_range}%: {count} إشارات → قيد التنفيذ")
        
        if best_pairs:
            output.append("")
            output.append("▶️ أفضل 5 أزواج:")
            for symbol, total, wins, losses in best_pairs:
                wins = wins or 0
                losses = losses or 0
                if wins + losses > 0:
                    wr = (wins / (wins + losses)) * 100
                    output.append(f"  • {symbol_pretty(symbol)}: {wins}W/{losses}L ({wr:.0f}%)")
        
        if top_reasons:
            output.append("")
            output.append("▶️ أهم أسباب عدم الإشارة:")
            for reason, count in top_reasons:
                output.append(f"  • {reason}: {count}")
        
        output.append("")
        output.append("═" * 40)
        output.append("💡 استخدم هذه البيانات لضبط الإعدادات")
        
        return "\n".join(output)
        
    except Exception as e:
        return f"⚠️ خطأ في التحليل التفصيلي: {e}\n{traceback.format_exc()}"

def db_export_analysis_csv(days:int=14)->bytes:
    """تصدير CSV متقدم للتحليل الخارجي"""
    con = db_conn()
    cur = con.cursor()
    
    # استعلام شامل يجمع كل البيانات المهمة
    cur.execute("""
        SELECT 
            s.id,
            datetime(s.ts, 'unixepoch') as signal_time,
            s.exchange,
            s.symbol,
            s.side,
            s.entry,
            s.sl,
            s.tp1, s.tp2, s.tp3, s.tp4,
            s.confidence,
            (s.entry - s.sl) / s.entry * 100 as risk_pct,
            (SELECT event FROM outcomes o WHERE o.signal_id = s.id ORDER BY o.ts LIMIT 1) as first_outcome,
            (SELECT price FROM outcomes o WHERE o.signal_id = s.id ORDER BY o.ts LIMIT 1) as outcome_price,
            (SELECT (o.ts - s.ts) / 60.0 FROM outcomes o WHERE o.signal_id = s.id ORDER BY o.ts LIMIT 1) as duration_minutes,
            (SELECT COUNT(*) FROM outcomes o WHERE o.signal_id = s.id AND o.event LIKE 'TP%') as tp_hits,
            CASE 
                WHEN EXISTS(SELECT 1 FROM outcomes o WHERE o.signal_id = s.id AND o.event = 'SL') THEN 'LOSS'
                WHEN EXISTS(SELECT 1 FROM outcomes o WHERE o.signal_id = s.id AND o.event LIKE 'TP%') THEN 'WIN'
                ELSE 'OPEN'
            END as status,
            (SELECT GROUP_CONCAT(event || '@' || price, '|') FROM outcomes o WHERE o.signal_id = s.id) as all_outcomes
        FROM signals s
        WHERE s.ts >= strftime('%s', 'now', ?)
        ORDER BY s.ts DESC
    """, (f"-{days} day",))
    
    rows = cur.fetchall()
    con.close()
    
    out = io.StringIO()
    w = csv.writer(out)
    
    # Headers
    w.writerow([
        "ID", "Signal_Time", "Exchange", "Symbol", "Side", 
        "Entry", "SL", "TP1", "TP2", "TP3", "TP4", 
        "Confidence", "Risk_%", "First_Outcome", "Outcome_Price", 
        "Duration_Minutes", "TP_Hits", "Status", "All_Outcomes"
    ])
    
    for r in rows:
        w.writerow(r)
    
    return out.getvalue().encode("utf-8")

def db_text_reasons(window:str="1d")->str:
    unit = window[-1].lower()
    num = int(''.join([ch for ch in window if ch.isdigit()]) or 1)
    sql_win = f"-{num} {'hour' if unit=='h' else 'day'}"
    
    try:
        con = db_conn()
        cur = con.cursor()
        cur.execute(
            "SELECT reasons FROM nosignal_reasons WHERE ts >= strftime('%s','now', ?)", 
            (sql_win,)
        )
        rows = cur.fetchall()
        con.close()
        
        if not rows: 
            return "لا توجد أسباب مسجلة في هذه المدة."
        
        from collections import Counter
        cnt = Counter()
        
        for (js,) in rows:
            try:
                d = json.loads(js) if isinstance(js, str) else {}
                if isinstance(d, dict):
                    for k in d.keys(): 
                        cnt[k] += 1
                else: 
                    cnt["other"] += 1
            except: 
                cnt["other"] += 1
        
        lines = [f"📄 أهم الأسباب ({window}):"]
        lines.append("━" * 25)
        lines.extend([f"{i+1}. {k}: {v}" for i, (k, v) in enumerate(cnt.most_common(10))])
        
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ خطأ قراءة الأسباب: {e}"

def db_text_last(limit:int=10)->str:
    try:
        con = db_conn()
        cur = con.cursor()
        cur.execute("""
            SELECT s.id, datetime(s.ts,'unixepoch'), s.symbol, s.side, s.entry, s.sl, s.confidence,
                   (SELECT event FROM outcomes o WHERE o.signal_id=s.id ORDER BY o.ts LIMIT 1)
            FROM signals s ORDER BY s.id DESC LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        con.close()
        
        if not rows: 
            return "لا توجد إشارات مسجلة بعد."
        
        out = ["📜 آخر الإشارات:"]
        out.append("━" * 30)
        
        for r in rows:
            result = r[7] or "قيد التنفيذ..."
            emoji = "✅" if result and result.startswith("TP") else ("❌" if result == "SL" else "⏳")
            out.append(f"{emoji} #{symbol_pretty(r[2])} {r[3]} | ثقة:{r[6]}% → {result}")
        
        return "\n".join(out)
    except Exception as e:
        return f"⚠️ خطأ قراءة السجل: {e}"

def db_text_open()->str:
    if not open_trades: 
        return "لا توجد صفقات مفتوحة حالياً."
    
    out = ["📌 الصفقات المفتوحة:"]
    out.append("━" * 30)
    
    for s, p in open_trades.items():
        hit_count = sum(p["hit"])
        out.append(
            f"#{symbol_pretty(s)} {p['side']}\n"
            f"  دخول: {p['entry']:.6f} | SL: {p['sl']:.6f}\n"
            f"  أهداف محققة: {hit_count}/4"
        )
    
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
    w.writerow([
        "id", "ts", "exchange", "symbol", "side", "entry", "sl", 
        "tp1", "tp2", "tp3", "tp4", "confidence", "outcomes"
    ])
    
    for r in rows: 
        w.writerow(r)
    
    return out.getvalue().encode("utf-8")

# ================== Telegram Polling ==================
TG_OFFSET = 0

def tg_delete_webhook():
    try: 
        requests.post(TG_DELETE_WEBHOOK, data={"drop_pending_updates": False}, timeout=10)
    except Exception as e: 
        print("deleteWebhook error:", e)

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
            r = requests.get(
                TG_GET_UPDATES, 
                params={"timeout": 25, "offset": TG_OFFSET + 1}, 
                timeout=35
            ).json()
            
            if r.get("ok"):
                for upd in r.get("result", []):
                    TG_OFFSET = max(TG_OFFSET, upd["update_id"])
                    msg = upd.get("message") or upd.get("edited_message")
                    
                    if not msg or str(msg.get("chat", {}).get("id")) != str(CHAT_ID): 
                        continue
                    
                    text = msg.get("text", "")
                    cmd, arg = parse_cmd(text)

                    if cmd in ("/start", "🔁 تحديث القائمة"): 
                        send_start_menu()
                    
                    elif cmd in ("📊 الإحصائيات", "/stats"):
                        days = int(arg) if arg.isdigit() else 1
                        send_telegram(db_text_stats(days))
                    
                    elif cmd in ("📈 تحليل متقدم", "/analysis", "/detailed"):
                        days = int(arg) if arg.isdigit() else 7
                        send_telegram(db_detailed_stats(days))
                    
                    elif cmd in ("📄 الأسباب", "/reasons", "/reason"):
                        win = arg or "1d"
                        send_telegram(db_text_reasons(win))
                    
                    elif cmd in ("📜 آخر الإشارات", "/last"):
                        lim = int(arg) if arg.isdigit() else 10
                        send_telegram(db_text_last(lim))
                    
                    elif cmd in ("📌 المفتوحة", "/open"): 
                        send_telegram(db_text_open())
                    
                    elif cmd in ("⬇️ تصدير CSV", "/export"):
                        days = int(arg) if arg.isdigit() else 14
                        send_document(
                            f"signals_{days}d.csv", 
                            export_csv_bytes(days), 
                            caption=f"تصدير الإشارات ({days} يوم)"
                        )
                    
                    elif cmd in ("📋 تصدير تحليلي", "/export_analysis", "/analysis_csv"):
                        days = int(arg) if arg.isdigit() else 14
                        send_document(
                            f"analysis_{days}d.csv",
                            db_export_analysis_csv(days),
                            caption=f"تصدير تحليلي شامل ({days} يوم)\nللاستخدام في Excel/Python"
                        )
                    
                    elif cmd in ("/version", "نسخة", "إصدار"):
                        send_telegram(f"🤖 الإصدار: v{APP_VERSION}")
                    
                    else:
                        send_start_menu()
        
        except Exception as e:
            print("poll error:", e)
        
        await asyncio.sleep(POLL_INTERVAL)

# ================== Keepalive ==================
async def keepalive_task():
    if not KEEPALIVE_URL: 
        return
    
    while True:
        try: 
            requests.get(KEEPALIVE_URL, timeout=10)
        except Exception as e: 
            print("keepalive error:", e)
        
        await asyncio.sleep(max(60, KEEPALIVE_INTERVAL))

# ================== Startup / Runner ==================
app.state.exchange = None
app.state.exchange_id = EXCHANGE_NAME
app.state.symbols = []
app.state.cycle_count = 0
app.state.last_no_sig_ts = 0

def attempt_build():
    ex, used = try_failover(EXCHANGE_NAME)
    syms = parse_symbols(ex, SYMBOLS_MODE)
    app.state.exchange, app.state.exchange_id, app.state.symbols = ex, used, syms

@app.on_event("startup")
async def _startup():
    db_init()
    
    send_telegram(
        f"🚀 بوت التوصيات الجبار v{APP_VERSION}\n"
        f"{'━' * 30}\n"
        f"✅ البوت يعمل الآن\n"
        f"⏳ جاري تحميل الأسواق...\n"
        f"📊 Timeframe: {TIMEFRAME}\n"
        f"💪 وضع: محسّن وأقوى",
        reply_markup=start_menu_markup()
    )
    
    attempt_build()
    
    syms = app.state.symbols
    ex_id = app.state.exchange_id
    
    head = (
        f"✅ التحميل اكتمل بنجاح!\n"
        f"{'━' * 30}\n"
        f"📡 المنصة: {ex_id}\n"
        f"⏱ الإطار الزمني: {TIMEFRAME}\n"
        f"📊 عدد الأزواج: {len(syms)}\n"
        f"🎯 الثقة الدنيا: {MIN_CONFIDENCE}%\n"
        f"{'━' * 30}\n"
        f"الأزواج المراقبة:\n"
        f"{', '.join([symbol_pretty(s) for s in syms[:15]])}"
        f"{f'... (+{len(syms)-15} أخرى)' if len(syms) > 15 else ''}"
    )
    
    send_telegram(head)
    
    asyncio.create_task(runner())
    asyncio.create_task(poll_telegram_commands())
    asyncio.create_task(keepalive_task())

async def maybe_send_no_signal_summary():
    if _last_cycle_alerts > 0: 
        return
    
    now = time.time()
    ok_cycles = (NO_SIG_EVERY_N_CYCLES > 0 and 
                 app.state.cycle_count % NO_SIG_EVERY_N_CYCLES == 0)
    ok_minutes = (NO_SIG_EVERY_MINUTES > 0 and 
                  (now - app.state.last_no_sig_ts) >= NO_SIG_EVERY_MINUTES * 60)
    
    if ok_cycles or ok_minutes:
        send_telegram(
            "ℹ️ لا توجد فرص تداول مناسبة في الدورة الحالية.\n"
            "البوت يراقب الأسواق باستمرار..."
        )
        app.state.last_no_sig_ts = now

async def runner():
    """الحلقة الرئيسية للبوت"""
    while True:
        try:
            if not app.state.symbols: 
                attempt_build()
            
            await scan_once(app.state.exchange, app.state.symbols)
            app.state.cycle_count += 1
            
            await maybe_send_no_signal_summary()
        
        except Exception as e:
            _error_bucket.append(
                f"Loop: {type(e).__name__} {str(e)}\n"
                f"{traceback.format_exc().splitlines()[-1]}"
            )
        
        await asyncio.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
