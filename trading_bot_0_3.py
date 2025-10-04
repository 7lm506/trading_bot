# trading_bot_hard_1_7.py
# إعدادات داخل الكود بالكامل (بدون Environment)
# المزايا:
# - استراتيجية مرنة أقل تعجيزاً (RSI/بولنجر/ترند قابل للتخفيف)
# - قائمة /start (إحصائيات، أسباب، آخر الإشارات، المفتوحة، تصدير CSV)
# - تسجيل كل شيء في SQLite (bot_stats.db)
# - منع سبام "لا توجد إشارات"
# - keepalive اختياري
# - فحص TP/SL برسائل تلقائية
# المتطلبات: pip install ccxt fastapi uvicorn pandas requests

import os, json, asyncio, time, io, csv, sqlite3, random
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone

import requests
import pandas as pd
import ccxt
from fastapi import FastAPI
import uvicorn

# ========================== [ عدّل هنا فقط ] ==========================
TELEGRAM_TOKEN = "PASTE_YOUR_TOKEN_HERE"   # ← ضع توكن البوت
CHAT_ID        = "PASTE_YOUR_CHAT_ID_HERE" # ← ضع Chat ID

# المنصّة/الأزواج/الإطار
EXCHANGE_NAME = "okx"           # okx / kucoinfutures / bybit / bitget / gate / binance
TIMEFRAME     = "5m"
SYMBOLS_MODE  = "ALL"           # "ALL" = كل عقود السواب/اللينيير. أو قائمة: "BTC/USDT,ETH/USDT"

# حدود موثوقية/سيولة/تذبذب (مرنة)
MIN_CONFIDENCE         = 55     # 55-65 بداية جيدة، ارفع لتقليل الإشارات
MIN_ATR_PCT            = 0.10   # الحد الأدنى لحركة ATR% (0.10 = 0.1%)
MIN_AVG_VOL_USDT       = 50_000 # الحد الأدنى للسيولة (USDT) على 30 شمعة

# RSI (مرن)
RSI_LONG_MIN,  RSI_LONG_MAX  = 40, 72
RSI_SHORT_MIN, RSI_SHORT_MAX = 28, 60

# بولنجر: نطاق صارم + نطاق ألين
BB_BANDWIDTH_MAX       = 0.045  # صارم
BB_BANDWIDTH_MAX_SOFT  = 0.08   # ألين
ALLOW_NO_SQUEEZE       = True   # اسمح بإشارة إذا كان الباند ضمن soft حتى لو تجاوز الصارم

# فلتر الترند (EMA50 مقابل EMA200)
REQUIRE_TREND          = False  # True = إشارات مع اتجاه فقط

# أهداف/وقف
TP_PCTS                = [0.25, 0.5, 1.0, 1.5]  # بالنسب المئوية
ATR_SL_MULT            = 1.5
SL_LOOKBACK            = 12
MIN_SL_PCT, MAX_SL_PCT = 0.30, 3.00

# تبريد ومنع سبام
SCAN_INTERVAL                 = 60     # ثانية بين الدورات
MIN_SIGNAL_GAP_SEC            = 6      # فاصلة زمنية بين الرسائل
MAX_ALERTS_PER_CYCLE          = 6      # أقصى إشارات لكل دورة
COOLDOWN_PER_SYMBOL_CANDLES   = 8      # تبريد لكل رمز بالشموع المغلقة
MAX_SYMBOLS                   = 120    # حد أقصى للأزواج (0 = بدون حد)

# ملخّص "لا توجد إشارات" (افتراضياً معطّل)
NO_SIG_EVERY_N_CYCLES         = 0      # 0 = تعطيل
NO_SIG_EVERY_MINUTES          = 0      # 0 = تعطيل

# keepalive (اختياري)
KEEPALIVE_URL      = ""               # مثال: "https://your-service.onrender.com/"
KEEPALIVE_INTERVAL = 240              # ثواني

# واجهة/نسخة
APP_VERSION        = "1.7-hard"
POLL_COMMANDS      = True
POLL_INTERVAL      = 10   # فحص أوامر التلغرام كل n ثانية

# ======================== [ لا تعدّل تحت غالباً ] ========================
LOG_DB_PATH = "bot_stats.db"

TG_API   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
SEND_URL = TG_API + "/sendMessage"
DOC_URL  = TG_API + "/sendDocument"
TG_GET_UPDATES    = TG_API + "/getUpdates"
TG_DELETE_WEBHOOK = TG_API + "/deleteWebhook"

_last_send_ts = 0

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
        if not r.get("ok"): print("Telegram error:", r); return None
        _last_send_ts = time.time()
        return r["result"]["message_id"]
    except Exception as e:
        print("Telegram send error:", e); return None

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
        ["📊 الإحصائيات", "📄 الأسباب"],
        ["📜 آخر الإشارات", "📌 المفتوحة"],
        ["⬇️ تصدير CSV", "🔁 تحديث القائمة"]
    ])

def send_start_menu():
    send_telegram("القائمة الرئيسية:", reply_markup=start_menu_markup())

# ================== مؤشرات ==================
def ema(s: pd.Series, n:int)->pd.Series: return s.ewm(span=n, adjust=False).mean()
def rsi(s: pd.Series, n=14)->pd.Series:
    d=s.diff(); up=d.clip(lower=0); dn=-d.clip(upper=0)
    ma_up=up.ewm(com=n-1, adjust=False).mean(); ma_dn=dn.ewm(com=n-1, adjust=False).mean()
    rs=ma_up/(ma_dn.replace(0,1e-12)); return 100-(100/(1+rs))
def macd(s: pd.Series, f=12, sl=26, sig=9)->Tuple[pd.Series,pd.Series]:
    ef, es = ema(s,f), ema(s,sl); line=ef-es; return line, ema(line,sig)
def bollinger(s: pd.Series, n=20, k=2.0):
    ma=s.rolling(n).mean(); sd=s.rolling(n).std(ddof=0)
    up=ma+k*sd; dn=ma-k*sd; bw=(up-dn)/ma; return ma, up, dn, bw
def atr(df: pd.DataFrame, n=14):
    h,l,c = df["high"], df["low"], df["close"]
    tr=pd.concat([(h-l).abs(),(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()
def clamp(x,a,b): return max(a, min(b,x))

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
        try: ex.load_markets(reload=True, params={"category":"linear","type":"swap"}); return
        except Exception as e:
            last=e; print(f"[load_markets {i}] {type(e).__name__}: {str(e)[:160]}"); time.sleep(b)
    raise last

def try_failover(primary:str)->Tuple[ccxt.Exchange,str]:
    last=None
    for name in [primary,"okx","kucoinfutures","bitget","gate","binance"]:
        try:
            ex=make_exchange(name); load_markets_linear_only(ex); return ex,name
        except Exception as e:
            last=e; print("[failover]", name, "failed:", type(e).__name__, str(e)[:100])
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
    if MAX_SYMBOLS>0: syms=syms[:MAX_SYMBOLS]
    return normalize_symbols_for_exchange(ex, syms)

def parse_symbols(ex, val:str)->List[str]:
    key=(val or "").strip().upper()
    if key in ("ALL","AUTO_FUTURES","AUTO","AUTO_SWAP","AUTO_LINEAR"):
        return list_all_futures_symbols(ex)
    syms=[s.strip() for s in (val or "").split(",") if s.strip()]
    syms=normalize_symbols_for_exchange(ex, syms)
    if MAX_SYMBOLS>0 and MAX_SYMBOLS>0: syms=syms[:MAX_SYMBOLS]
    return syms

# ================== جلب البيانات ==================
async def fetch_ohlcv_safe(ex, symbol:str, timeframe:str, limit:int):
    try:
        params={}
        if ex.id=="bybit": params={"category":"linear"}
        elif ex.id=="okx": params={"instType":"SWAP"}
        ohlcv=await asyncio.to_thread(ex.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit, params=params)
        if not ohlcv or len(ohlcv)<60: return None
        df=pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
        df["ts"]=pd.to_datetime(df["ts"], unit="ms", utc=True); df.set_index("ts", inplace=True)
        return df
    except Exception as e:
        return f"خطأ المنصة: {ex.id} {type(e).__name__} {str(e)[:200]}"

async def fetch_ticker_price(ex, symbol:str)->Optional[float]:
    try:
        t=await asyncio.to_thread(ex.fetch_ticker, symbol)
        return float(t.get("last") or t.get("close") or t.get("info",{}).get("lastPrice"))
    except Exception: return None

# ================== DB/Helpers ==================
def unix_now()->int: return int(datetime.now(timezone.utc).timestamp())
def symbol_pretty(s:str)->str: return s.replace(":USDT","")

open_trades: Dict[str,Dict]={}
signal_state: Dict[str,Dict]={}
_last_cycle_alerts=0

def db_conn(): con=sqlite3.connect(LOG_DB_PATH); con.execute("PRAGMA journal_mode=WAL;"); return con
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
    con.commit(); con.close()

def db_insert_signal(ts,ex,sym,side,entry,sl,tps,conf,msg_id)->int:
    con=db_conn(); cur=con.cursor()
    cur.execute("INSERT INTO signals(ts,exchange,symbol,side,entry,sl,tp1,tp2,tp3,tp4,confidence,msg_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts,ex,sym,side,entry,sl,tps[0],tps[1],tps[2],tps[3],conf,msg_id))
    con.commit(); sid=cur.lastrowid; con.close(); return sid

def db_insert_outcome(signal_id,ts,event,idx,price):
    con=db_conn(); con.execute("INSERT INTO outcomes(signal_id,ts,event,idx,price) VALUES(?,?,?,?,?)",
                               (signal_id,ts,event,idx,price)); con.commit(); con.close()

def db_insert_nosignal(ts,ex,sym,rsn:Dict):
    con=db_conn(); con.execute("INSERT INTO nosignal_reasons(ts,exchange,symbol,reasons) VALUES(?,?,?,?)",
                               (ts,ex,sym,json.dumps(rsn,ensure_ascii=False))); con.commit(); con.close()

def db_insert_error(ts,ex,sym,msg):
    con=db_conn(); con.execute("INSERT INTO errors(ts,exchange,symbol,message) VALUES(?,?,?,?)",
                               (ts,ex,sym,msg)); con.commit(); con.close()

# ================== الاستراتيجية (مرنة) ==================
def compute_confidence(df,side,bb_bw_now,c_prev,c_now,band_now,macd_now,macd_sig,r14,atr_now)->int:
    tight = clamp((BB_BANDWIDTH_MAX - bb_bw_now)/max(BB_BANDWIDTH_MAX,1e-9), 0, 1)
    breakout = clamp(((c_now - band_now) if side=="LONG" else (band_now - c_now))/max(atr_now,1e-9), 0, 1)
    mom = clamp(abs(macd_now - macd_sig)/max(atr_now,1e-9), 0, 1)
    rsi_target = 60 if side=="LONG" else 40
    rsi_score = clamp(1 - abs(r14 - rsi_target)/20.0, 0, 1)
    return int(round(100 * (0.25*tight + 0.35*breakout + 0.2*mom + 0.2*rsi_score)))

def smart_signal(df: pd.DataFrame) -> Tuple[Optional[Dict], Dict]:
    if df is None or len(df)<60: return None, {"insufficient_data":True}

    c=df["close"]; h=df["high"]; l=df["low"]
    ma20, bb_up, bb_dn, bb_bw = bollinger(c,20,2.0)
    macd_line, macd_sig = macd(c,12,26,9)
    r = rsi(c,14); atr14 = atr(df,14)
    ema50 = ema(c,50); ema200=ema(c,200)

    i2,i1=-3,-2
    try:
        c_prev, c_now = float(c.iloc[i2]), float(c.iloc[i1])
        up_prev, up_now = float(bb_up.iloc[i2]), float(bb_up.iloc[i1])
        dn_prev, dn_now = float(bb_dn.iloc[i2]), float(bb_dn.iloc[i1])
        bw_now = float(bb_bw.iloc[i1])
        macd_now = float(macd_line.iloc[i1]); sig_now = float(macd_sig.iloc[i1])
        r14 = float(r.iloc[i1]); atr_now = float(atr14.iloc[i1])
        e50=float(ema50.iloc[i1]); e200=float(ema200.iloc[i1])
        ma20_prev=float(ma20.iloc[i2])
    except Exception:
        return None, {"index_error":True}

    atr_pct = 100*atr_now/max(c_now,1e-9)

    squeeze_strict = bw_now <= BB_BANDWIDTH_MAX
    squeeze_soft   = bw_now <= BB_BANDWIDTH_MAX_SOFT
    squeeze_ok     = squeeze_strict or (ALLOW_NO_SQUEEZE and squeeze_soft)

    # سيولة وحركة دنيا
    try: avg_usdt=float((df["volume"]*c).tail(30).mean())
    except Exception: avg_usdt=0.0
    if (atr_pct < MIN_ATR_PCT) or (avg_usdt < MIN_AVG_VOL_USDT):
        return None, {"atr_pct":round(atr_pct,3), "avg_vol_usdt":int(avg_usdt), "squeeze":squeeze_ok}

    trend_up = e50>e200
    trend_down = e50<e200
    trend_ok_long = (not REQUIRE_TREND) or trend_up
    trend_ok_short= (not REQUIRE_TREND) or trend_down

    crossed_up   = (c_prev <= up_prev) and (c_now > up_now)
    crossed_down = (c_prev >= dn_prev) and (c_now < dn_now)

    long_price_ok  = crossed_up   or ((c_now > up_now) and (c_prev > ma20_prev))
    short_price_ok = crossed_down or ((c_now < dn_now) and (c_prev < ma20_prev))

    long_momentum  = (macd_now > sig_now) or (c_now > e50)
    short_momentum = (macd_now < sig_now) or (c_now < e50)

    rsi_long_ok  = (RSI_LONG_MIN  < r14 < RSI_LONG_MAX)
    rsi_short_ok = (RSI_SHORT_MIN < r14 < RSI_SHORT_MAX)

    long_ok  = squeeze_ok and long_price_ok  and long_momentum  and rsi_long_ok  and trend_ok_long
    short_ok = squeeze_ok and short_price_ok and short_momentum and rsi_short_ok and trend_ok_short

    if not (long_ok or short_ok):
        return None, {
            "squeeze": squeeze_ok, "bw_now": round(bw_now,5),
            "rsi14": round(r14,2),
            "cross_up": crossed_up, "cross_down": crossed_down,
            "macd_vs_signal": f"{round(macd_now,4)} vs {round(sig_now,4)}",
            "trend": "up" if trend_up else ("down" if trend_down else "flat"),
        }

    side = "LONG" if long_ok else "SHORT"
    band_now = up_now if side=="LONG" else dn_now
    conf = compute_confidence(df, side, bw_now, c_prev, c_now, band_now, macd_now, sig_now, r14, atr_now)

    # SL واقعي بحدود دنيا/عليا
    recent_lows  = float(l.tail(SL_LOOKBACK).min())
    recent_highs = float(h.tail(SL_LOOKBACK).max())
    entry=c_now; atr_dist=ATR_SL_MULT*max(atr_now,1e-12)
    if side=="LONG":
        sl_raw = min(recent_lows, entry - atr_dist)
        min_gap = entry*(MIN_SL_PCT/100.0); max_gap=entry*(MAX_SL_PCT/100.0)
        gap = entry - sl_raw
        if gap < min_gap: sl_raw = entry - min_gap
        if gap > max_gap: sl_raw = entry - max_gap
    else:
        sl_raw = max(recent_highs, entry + atr_dist)
        min_gap = entry*(MIN_SL_PCT/100.0); max_gap=entry*(MAX_SL_PCT/100.0)
        gap = sl_raw - entry
        if gap < min_gap: sl_raw = entry + min_gap
        if gap > max_gap: sl_raw = entry + max_gap
    sl=float(sl_raw)
    tps=[entry*(1+p/100.0) for p in TP_PCTS] if side=="LONG" else [entry*(1-p/100.0) for p in TP_PCTS]
    return ({"side":side,"entry":float(entry),"sl":sl,"tps":[float(x) for x in tps],
             "confidence":int(conf)}, {})

# ================== TP/SL ==================
def crossed_levels(side:str, price:float, tps:List[float], sl:float, hit:List[bool]):
    if price is None: return None
    if side=="LONG" and price<=sl: return ("SL",-1)
    if side=="SHORT" and price>=sl: return ("SL",-1)
    for idx,(tp,was_hit) in enumerate(zip(tps,hit)):
        if was_hit: continue
        if side=="LONG" and price>=tp: return ("TP",idx)
        if side=="SHORT" and price<=tp: return ("TP",idx)
    return None
def pct_profit(side:str, entry:float, exit_price:float)->float:
    return (exit_price/entry-1.0)*100.0 if side=="LONG" else (1.0-exit_price/entry)*100.0
def elapsed_text(start_ts:int, end_ts:int)->str:
    mins=max(0,end_ts-start_ts)//60
    return f"{mins} دقيقة" if mins<60 else f"{mins//60} ساعة {mins%60} دقيقة"

# ================== FastAPI ==================
app=FastAPI()
@app.get("/")
def root():
    return {"ok":True,"version":APP_VERSION,"exchange":getattr(app.state,"exchange_id",EXCHANGE_NAME),
            "tf":TIMEFRAME,"symbols":len(getattr(app.state,"symbols",[]))}

# ================== Scan & Trade ==================
async def fetch_and_signal(ex, symbol:str):
    global _last_cycle_alerts
    out=await fetch_ohlcv_safe(ex, symbol, TIMEFRAME, 300)
    if isinstance(out,str): db_insert_error(unix_now(),ex.id,symbol,out); return
    if out is None or len(out)<60: db_insert_nosignal(unix_now(),ex.id,symbol,{"insufficient_data":True}); return

    st = signal_state.get(symbol,{})
    closed_idx = len(out)-2
    if closed_idx < st.get("cooldown_until_idx",-999999): return
    if symbol in open_trades: return

    sig, reasons = smart_signal(out)
    if not sig:
        db_insert_nosignal(unix_now(),ex.id,symbol,reasons or {"note":"no_setup"}); return
    if sig["confidence"] < MIN_CONFIDENCE:
        db_insert_nosignal(unix_now(),ex.id,symbol,{**reasons,"confidence_lt_min":sig["confidence"]}); return
    if _last_cycle_alerts >= MAX_ALERTS_PER_CYCLE:
        db_insert_nosignal(unix_now(),ex.id,symbol,{"cycle_cap_reached":True}); return

    pretty=symbol_pretty(symbol); side_txt="طويل 🟢" if sig["side"]=="LONG" else "قصير 🔴"
    entry,sl,tps,conf=sig["entry"],sig["sl"],sig["tps"],sig["confidence"]
    mid=send_telegram(
        f"#{pretty} - {side_txt}\n\n"
        f"نقطة الدخول: {entry}\nوقف الخسارة: {sl}\n\n"
        f"الهدف 1: {tps[0]}\nالهدف 2: {tps[1]}\nالهدف 3: {tps[2]}\nالهدف 4: {tps[3]}\n\n"
        f"نسبة الثقة: {conf}%"
    )
    if mid:
        ts_now=unix_now()
        open_trades[symbol]={"side":sig["side"],"entry":entry,"sl":sl,"tps":tps,"hit":[False]*4,
                             "msg_id":mid,"signal_id":None,"opened_ts":ts_now}
        signal_state[symbol]={"cooldown_until_idx":closed_idx+COOLDOWN_PER_SYMBOL_CANDLES}
        sid=db_insert_signal(ts_now, app.state.exchange.id, symbol, sig["side"], entry, sl, tps, conf, mid)
        open_trades[symbol]["signal_id"]=sid
        _last_cycle_alerts+=1

async def check_open_trades(ex):
    for sym, pos in list(open_trades.items()):
        price=await fetch_ticker_price(ex,sym)
        res=crossed_levels(pos["side"],price,pos["tps"],pos["sl"],pos["hit"])
        if not res: continue
        kind,idx=res; ts=unix_now()
        if kind=="SL":
            pr=pct_profit(pos["side"],pos["entry"],price or pos["sl"])
            send_telegram(f"#{symbol_pretty(sym)}\nتم ضرب وقف الخسارة ❌\nالنتيجة: {round(pr,4)}% 📉\nفي: {elapsed_text(pos['opened_ts'],ts)} ⏰")
            if pos.get("signal_id"): db_insert_outcome(pos["signal_id"],ts,"SL",-1,price or 0.0)
            del open_trades[sym]
        else:
            pos["hit"][idx]=True; tp=pos["tps"][idx]
            pr=pct_profit(pos["side"],pos["entry"],tp if price is None else price)
            send_telegram(f"#{symbol_pretty(sym)}\nتم الوصول إلى الهدف {idx+1} ✅\nالربح: {round(pr,4)}% 📈\nفي: {elapsed_text(pos['opened_ts'],ts)} ⏰")
            if pos.get("signal_id"): db_insert_outcome(pos["signal_id"],ts,f"TP{idx+1}",idx,price or tp)
            if all(pos["hit"]): del open_trades[sym]

async def scan_once(ex, symbols:List[str]):
    global _last_cycle_alerts
    _last_cycle_alerts = 0
    await check_open_trades(ex)
    if not symbols: return
    random.shuffle(symbols)
    sem=asyncio.Semaphore(3)
    async def worker(s):
        async with sem: await fetch_and_signal(ex,s)
    await asyncio.gather(*[asyncio.create_task(worker(s)) for s in symbols])

# ================== تقارير/أوامر ==================
def db_text_stats(days:int=1)->str:
    try:
        con=db_conn(); cur=con.cursor()
        cur.execute("SELECT COUNT(*) FROM signals WHERE ts >= strftime('%s','now', ?)", (f"-{days} day",))
        total=cur.fetchone()[0] or 0
        cur.execute("""SELECT SUM(CASE WHEN event LIKE 'TP%' THEN 1 ELSE 0 END),
                              SUM(CASE WHEN event='SL' THEN 1 ELSE 0 END)
                       FROM outcomes WHERE ts >= strftime('%s','now', ?)""",(f"-{days} day",))
        row=cur.fetchone() or (0,0); tp,sl=row[0] or 0, row[1] or 0
        con.close()
        if total==0 and tp==0 and sl==0: return "لا توجد بيانات كافية بعد."
        return f"📊 إحصائيات آخر {days} يوم:\n- إشارات: {total}\n- TP: {tp}\n- SL: {sl}"
    except Exception as e:
        return f"⚠️ خطأ الإحصائيات: {e}"

def db_text_reasons(window:str="1d")->str:
    unit=window[-1].lower(); num=int(''.join([ch for ch in window if ch.isdigit()]) or 1)
    sql_win=f"-{num} {'hour' if unit=='h' else 'day'}"
    try:
        con=db_conn(); cur=con.cursor()
        cur.execute("SELECT reasons FROM nosignal_reasons WHERE ts >= strftime('%s','now', ?)", (sql_win,))
        rows=cur.fetchall(); con.close()
        if not rows: return "لا توجد أسباب مسجلة في هذه المدة."
        from collections import Counter
        cnt=Counter()
        for (js,) in rows:
            try:
                d=json.loads(js) if isinstance(js,str) else {}
                if isinstance(d,dict):
                    for k in d.keys(): cnt[k]+=1
                else: cnt["other"]+=1
            except: cnt["other"]+=1
        lines=[f"📄 أهم الأسباب ({window}):"]+[f"- {k}: {v}" for k,v in cnt.most_common(10)]
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ خطأ قراءة الأسباب: {e}"

def db_text_last(limit:int=10)->str:
    try:
        con=db_conn(); cur=con.cursor()
        cur.execute("""SELECT s.id, datetime(s.ts,'unixepoch'), s.symbol, s.side, s.entry, s.sl, s.confidence,
                              (SELECT event FROM outcomes o WHERE o.signal_id=s.id ORDER BY o.ts LIMIT 1)
                       FROM signals s ORDER BY s.id DESC LIMIT ?""",(limit,))
        rows=cur.fetchall(); con.close()
        if not rows: return "لا توجد إشارات مسجلة بعد."
        out=["📜 آخر الإشارات:"]+[f"#{symbol_pretty(r[2])} {r[3]} conf:{r[6]} → {r[7] or '—'} @ {r[1]}" for r in rows]
        return "\n".join(out)
    except Exception as e:
        return f"⚠️ خطأ قراءة السجل: {e}"

def db_text_open()->str:
    if not open_trades: return "لا توجد صفقات مفتوحة."
    out=["📌 صفقات مفتوحة:"]+[f"#{symbol_pretty(s)} {p['side']} دخول:{p['entry']} SL:{p['sl']}" for s,p in open_trades.items()]
    return "\n".join(out)

def export_csv_bytes(days:int=14)->bytes:
    con=db_conn(); cur=con.cursor()
    cur.execute("""SELECT s.id, s.ts, s.exchange, s.symbol, s.side, s.entry, s.sl, s.tp1, s.tp2, s.tp3, s.tp4, s.confidence,
                          (SELECT GROUP_CONCAT(event||':'||price,'|') FROM outcomes o WHERE o.signal_id=s.id)
                   FROM signals s WHERE s.ts >= strftime('%s','now', ?) ORDER BY s.id DESC""",(f"-{days} day",))
    rows=cur.fetchall(); con.close()
    out=io.StringIO(); w=csv.writer(out); w.writerow(
        ["id","ts","exchange","symbol","side","entry","sl","tp1","tp2","tp3","tp4","confidence","outcomes"])
    for r in rows: w.writerow(r)
    return out.getvalue().encode("utf-8")

# ================== Telegram Polling ==================
TG_OFFSET=0
def tg_delete_webhook():
    try: requests.post(TG_DELETE_WEBHOOK, data={"drop_pending_updates": False}, timeout=10)
    except Exception as e: print("deleteWebhook error:", e)

def parse_cmd(text:str)->Tuple[str,str]:
    t=(text or "").strip()
    if t.startswith("/"):
        parts=t.split(maxsplit=1); cmd=parts[0].lower(); arg=parts[1].strip() if len(parts)>1 else ""
        if "@" in cmd: cmd=cmd.split("@",1)[0]
        return cmd,arg
    return t,""

async def poll_telegram_commands():
    if not POLL_COMMANDS: return
    tg_delete_webhook()
    global TG_OFFSET
    while True:
        try:
            r=requests.get(TG_GET_UPDATES, params={"timeout":25,"offset":TG_OFFSET+1}, timeout=35).json()
            if r.get("ok"):
                for upd in r.get("result",[]):
                    TG_OFFSET=max(TG_OFFSET, upd["update_id"])
                    msg=upd.get("message") or upd.get("edited_message")
                    if not msg or str(msg.get("chat",{}).get("id"))!=str(CHAT_ID): continue
                    text=msg.get("text","")
                    cmd,arg=parse_cmd(text)

                    if cmd in ("/start","🔁 تحديث القائمة"): send_start_menu()
                    elif cmd in ("📊 الإحصائيات","/stats"):
                        days=int(arg) if arg.isdigit() else 1; send_telegram(db_text_stats(days))
                    elif cmd in ("📄 الأسباب","/reasons","/reason"):
                        win=arg or "1d"; send_telegram(db_text_reasons(win))
                    elif cmd in ("📜 آخر الإشارات","/last"):
                        lim=int(arg) if arg.isdigit() else 10; send_telegram(db_text_last(lim))
                    elif cmd in ("📌 المفتوحة","/open"): send_telegram(db_text_open())
                    elif cmd in ("⬇️ تصدير CSV","/export"):
                        days=int(arg) if arg.isdigit() else 14
                        send_document(f"signals_{days}d.csv", export_csv_bytes(days), caption="تصدير الإشارات")
                    elif cmd in ("/version","نسخة","إصدار"):
                        send_telegram(f"الإصدار: v{APP_VERSION}")
                    else:
                        send_start_menu()
        except Exception as e:
            print("poll error:", e)
        await asyncio.sleep(POLL_INTERVAL)

# ================== Keepalive ==================
async def keepalive_task():
    if not KEEPALIVE_URL: return
    while True:
        try: requests.get(KEEPALIVE_URL, timeout=10)
        except Exception as e: print("keepalive error:", e)
        await asyncio.sleep(max(60, KEEPALIVE_INTERVAL))

# ================== Startup / Runner ==================
app.state.exchange=None; app.state.exchange_id=EXCHANGE_NAME; app.state.symbols=[]
app.state.cycle_count=0; app.state.last_no_sig_ts=0

def attempt_build():
    ex,used = try_failover(EXCHANGE_NAME)
    syms = parse_symbols(ex, SYMBOLS_MODE)
    app.state.exchange, app.state.exchange_id, app.state.symbols = ex, used, syms

@app.on_event("startup")
async def _startup():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise SystemExit("ضع TELEGRAM_TOKEN و CHAT_ID في أعلى الملف.")
    db_init()
    send_telegram(f"> توصيات تداول Ai v{APP_VERSION}:\n✅ البوت اشتغل\nExchange: (initializing)\nTF: {TIMEFRAME}\nPairs: (loading…)",
                  reply_markup=start_menu_markup())
    attempt_build()
    syms=app.state.symbols; ex_id=app.state.exchange_id
    head=f"> تحديث الإقلاع:\nExchange: {ex_id}\nTF: {TIMEFRAME}\nPairs: {', '.join([symbol_pretty(s) for s in syms[:10]])}{'' if len(syms)<=10 else f' …(+{len(syms)-10})'}"
    send_telegram(head)
    asyncio.create_task(runner())
    asyncio.create_task(poll_telegram_commands())
    asyncio.create_task(keepalive_task())

async def maybe_send_no_signal_summary():
    if _last_cycle_alerts>0: return
    now=time.time()
    ok_cycles = (NO_SIG_EVERY_N_CYCLES>0 and app.state.cycle_count % NO_SIG_EVERY_N_CYCLES == 0)
    ok_minutes= (NO_SIG_EVERY_MINUTES>0 and (now - app.state.last_no_sig_ts) >= NO_SIG_EVERY_MINUTES*60)
    if ok_cycles or ok_minutes:
        send_telegram("ℹ️ لا توجد إشارات في الفترة الحالية (الأسباب تُسجّل في القاعدة).")
        app.state.last_no_sig_ts = now

async def runner():
    while True:
        try:
            if not app.state.symbols: attempt_build()
            await scan_once(app.state.exchange, app.state.symbols)
            app.state.cycle_count += 1
            await maybe_send_no_signal_summary()
        except Exception as e:
            send_telegram(f"⚠️ Loop error: {type(e).__name__} {str(e)[:160]}")
        await asyncio.sleep(SCAN_INTERVAL)

if __name__=="__main__":
    port=int(os.getenv("PORT","10000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
