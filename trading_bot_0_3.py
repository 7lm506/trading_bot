# trading_bot_smart_1_3b.py
# - Futures ALL support (SYMBOLS=ALL/AUTO_FUTURES)
# - Failover exchanges
# - Strict filters + confidence score
# - Realistic SL (swing + ATR + min/max % bounds)
# - Anti-spam + SQLite logging
# - TP message shows profit% and elapsed time

import os, json, asyncio, time, traceback, io, csv, sqlite3, random
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
import ccxt
from fastapi import FastAPI
import uvicorn

# ================= ENV =================
EXCHANGE_NAME = os.getenv("EXCHANGE","okx").lower()
TIMEFRAME     = os.getenv("TIMEFRAME","5m")
SYMBOLS_ENV   = os.getenv("SYMBOLS","ALL")
MAX_SYMBOLS   = int(os.getenv("MAX_SYMBOLS","100"))
OHLCV_LIMIT   = int(os.getenv("OHLCV_LIMIT","300"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL","60"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN","").strip()
CHAT_ID        = os.getenv("CHAT_ID","").strip()
if not TELEGRAM_TOKEN or not CHAT_ID: raise SystemExit("TELEGRAM_TOKEN & CHAT_ID required.")

HTTP_PROXY = os.getenv("HTTP_PROXY") or None
HTTPS_PROXY= os.getenv("HTTPS_PROXY") or None

# Strategy / filters
COOLDOWN_CANDLES         = int(os.getenv("COOLDOWN_CANDLES","3"))
BB_BANDWIDTH_MAX         = float(os.getenv("BB_BANDWIDTH_MAX","0.035"))
ATR_SL_MULT              = float(os.getenv("ATR_SL_MULT","1.5"))     # أقوى
USE_TREND_FILTER         = os.getenv("USE_TREND_FILTER","true").lower()=="true"
MIN_ATR_PCT              = float(os.getenv("MIN_ATR_PCT","0.20"))
MIN_AVG_VOL_USDT         = float(os.getenv("MIN_AVG_VOL_USDT","150000"))
VOL_LOOKBACK             = int(os.getenv("VOL_LOOKBACK","30"))
TP_PCTS                  = [float(x) for x in os.getenv("TP_PCTS","0.25,0.5,1.0,1.5").split(",")]

# واقعية الستوب
SL_LOOKBACK              = int(os.getenv("SL_LOOKBACK","12"))
MIN_SL_PCT               = float(os.getenv("MIN_SL_PCT","0.30"))
MAX_SL_PCT               = float(os.getenv("MAX_SL_PCT","3.00"))

# Anti-spam
MIN_CONFIDENCE           = int(os.getenv("MIN_CONFIDENCE","70"))
MAX_ALERTS_PER_CYCLE     = int(os.getenv("MAX_ALERTS_PER_CYCLE","3"))
MIN_SIGNAL_GAP_SEC       = int(os.getenv("MIN_SIGNAL_GAP_SEC","10"))
COOLDOWN_PER_SYMBOL_CANDLES = int(os.getenv("COOLDOWN_PER_SYMBOL_CANDLES","12"))

# Logging & commands
LOG_DB_PATH              = os.getenv("LOG_DB_PATH","bot_stats.db")
POLL_COMMANDS            = os.getenv("POLL_COMMANDS","true").lower()=="true"
POLL_INTERVAL            = int(os.getenv("POLL_INTERVAL","10"))
SEND_NO_SIGNAL_SUMMARY   = os.getenv("SEND_NO_SIGNAL_SUMMARY","false").lower()=="true"

TG_API   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
SEND_URL = TG_API + "/sendMessage"
DOC_URL  = TG_API + "/sendDocument"
GET_UPDATES_URL = TG_API + "/getUpdates"

# ================= Telegram =================
_last_send_ts = 0
def send_telegram(text: str, reply_to_message_id: Optional[int]=None) -> Optional[int]:
    global _last_send_ts
    now = time.time()
    if now - _last_send_ts < MIN_SIGNAL_GAP_SEC:
        time.sleep(max(0, MIN_SIGNAL_GAP_SEC - (now - _last_send_ts)))
    try:
        resp = requests.post(SEND_URL, data={
            "chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True,
            **({"reply_to_message_id": reply_to_message_id} if reply_to_message_id else {})
        }, timeout=25)
        data = resp.json()
        if not data.get("ok"):
            print("Telegram error:", data)
            return None
        _last_send_ts = time.time()
        return data["result"]["message_id"]
    except Exception as e:
        print("Telegram send error:", e)
        return None

def send_document(filename: str, file_bytes: bytes, caption: str="") -> bool:
    try:
        files={"document":(filename, io.BytesIO(file_bytes))}
        data={"chat_id": CHAT_ID, "caption": caption}
        r=requests.post(DOC_URL, data=data, files=files, timeout=60)
        j=r.json()
        if not j.get("ok"):
            print("send_document error:", j); return False
        return True
    except Exception as e:
        print("send_document exception:", e); return False

# ================= Indicators =================
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

# ================= CCXT & Symbols =================
EXC = {
    "bybit": ccxt.bybit, "okx": ccxt.okx, "kucoinfutures": ccxt.kucoinfutures,
    "bitget": ccxt.bitget, "gate": ccxt.gate, "binance": ccxt.binance, "krakenfutures": ccxt.krakenfutures
}
def make_exchange(name:str):
    klass=EXC.get(name, ccxt.okx)
    cfg={"enableRateLimit":True, "timeout":20000, "options":{"defaultType":"swap","defaultSubType":"linear"}}
    if HTTP_PROXY or HTTPS_PROXY: cfg["proxies"]={"http":HTTP_PROXY,"https":HTTPS_PROXY}
    return klass(cfg)
def load_markets_linear_only(ex):
    last=None
    for i,b in enumerate([1.5,3,6],1):
        try:
            ex.load_markets(reload=True, params={"category":"linear","type":"swap"}); return
        except Exception as e:
            last=e; print(f"[load_markets {i}] {type(e).__name__}: {str(e)[:180]}")
            if "403" in str(e) or "451" in str(e): break
            time.sleep(b)
    raise last
def try_failover(primary:str)->Tuple[ccxt.Exchange,str]:
    last=None
    for name in [primary,"okx","kucoinfutures","bitget","gate","binance"]:
        try:
            ex=make_exchange(name); load_markets_linear_only(ex); return ex,name
        except Exception as e:
            last=e; print("[failover]", name, "failed:", type(e).__name__, str(e)[:120])
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

def parse_symbols_from_env(ex, val:str)->List[str]:
    key=(val or "").strip().upper()
    if key in ("ALL","AUTO_FUTURES","AUTO","AUTO_SWAP","AUTO_LINEAR"):
        return list_all_futures_symbols(ex)
    syms=[s.strip() for s in (val or "").split(",") if s.strip()]
    syms=normalize_symbols_for_exchange(ex, syms)
    if MAX_SYMBOLS>0: syms=syms[:MAX_SYMBOLS]
    return syms

# ================= Data Fetch =================
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

# ================= Helpers/DB =================
def symbol_pretty(s:str)->str: return s.replace(":USDT","")
open_trades: Dict[str,Dict]={}   # includes: opened_ts
signal_state: Dict[str,Dict]={}
_last_cycle_alerts=0

def unix_now()->int: return int(datetime.now(timezone.utc).timestamp())

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

# ================= Strategy / Confidence =================
def compute_confidence(df: pd.DataFrame, side:str, bb_bw_now:float, c_prev:float, c_now:float,
                       band_prev:float, band_now:float, macd_now:float, macd_sig:float,
                       r14:float, atr_now:float) -> int:
    # tightness
    tight = clamp((BB_BANDWIDTH_MAX - bb_bw_now)/max(BB_BANDWIDTH_MAX,1e-9), 0, 1)
    # breakout
    breakout = clamp(((c_now - band_now) if side=="LONG" else (band_now - c_now))/max(atr_now,1e-9), 0, 1)
    # momentum
    mom = clamp(abs(macd_now - macd_sig)/max(atr_now,1e-9), 0, 1)
    # rsi
    rsi_target = 60 if side=="LONG" else 40
    rsi_score = clamp(1 - abs(r14 - rsi_target)/20.0, 0, 1)
    conf = 100 * (0.25*tight + 0.35*breakout + 0.2*mom + 0.2*rsi_score)
    return int(round(conf))

def smart_signal(df: pd.DataFrame) -> Tuple[Optional[Dict], Dict]:
    if df is None or len(df)<60:
        return None, {"insufficient_data":True}
    c=df["close"]; h=df["high"]; l=df["low"]
    ma20, bb_up, bb_dn, bb_bw = bollinger(c,20,2.0)
    macd_line, macd_sig = macd(c,12,26,9)
    r = rsi(c,14)
    atr14 = atr(df,14)
    ema50 = ema(c,50); ema200=ema(c,200)

    i2, i1 = -3, -2
    try:
        c_prev, c_now = float(c.iloc[i2]), float(c.iloc[i1])
        up_prev, up_now = float(bb_up.iloc[i2]), float(bb_up.iloc[i1])
        dn_prev, dn_now = float(bb_dn.iloc[i2]), float(bb_dn.iloc[i1])
        bw_now = float(bb_bw.iloc[i1])
        macd_now = float(macd_line.iloc[i1]); sig_now = float(macd_sig.iloc[i1])
        r14 = float(r.iloc[i1]); atr_now = float(atr14.iloc[i1])
        e50=float(ema50.iloc[i1]); e200=float(ema200.iloc[i1])
    except Exception:
        return None, {"index_error":True}

    # base filters
    atr_pct = 100*atr_now/max(c_now,1e-9)
    squeeze_ok = bw_now <= BB_BANDWIDTH_MAX
    crossed_up   = (c_prev <= up_prev) and (c_now > up_now)
    crossed_down = (c_prev >= dn_prev) and (c_now < dn_now)
    trend_up = e50>e200; trend_down = e50<e200

    # volume
    try:
        avg_usdt=float((df["volume"]*c).tail(VOL_LOOKBACK).mean())
    except Exception:
        avg_usdt=0.0

    if atr_pct < MIN_ATR_PCT or avg_usdt < MIN_AVG_VOL_USDT or not squeeze_ok:
        return None, {
            "atr_pct":round(atr_pct,3), "avg_vol_usdt":int(avg_usdt), "squeeze":squeeze_ok
        }

    long_ok  = crossed_up   and (macd_now>sig_now) and (45<r14<70)  and (not USE_TREND_FILTER or trend_up)
    short_ok = crossed_down and (macd_now<sig_now) and (30<r14<55)  and (not USE_TREND_FILTER or trend_down)
    if not (long_ok or short_ok):
        return None, {
            "cross_up":crossed_up,"cross_down":crossed_down,
            "macd_vs_signal":f"{round(macd_now,4)} vs {round(sig_now,4)}",
            "rsi14":round(r14,2),
            "trend": "up" if trend_up else ("down" if trend_down else "flat"),
            "bw_now":round(bw_now,5)
        }

    side="LONG" if long_ok else "SHORT"
    band_now = up_now if side=="LONG" else dn_now
    conf = compute_confidence(df, side, bw_now, c_prev, c_now, (up_prev if side=="LONG" else dn_prev),
                              band_now, macd_now, sig_now, r14, atr_now)

    # ----- Realistic SL -----
    # swing lows/highs في آخر SL_LOOKBACK
    recent_lows  = float(l.tail(SL_LOOKBACK).min())
    recent_highs = float(h.tail(SL_LOOKBACK).max())
    entry=c_now
    atr_dist = ATR_SL_MULT*max(atr_now,1e-12)
    if side=="LONG":
        sl_swing = recent_lows
        sl_atr   = entry - atr_dist
        sl_raw   = min(sl_swing, sl_atr)  # أبعد للأسفل
        min_gap  = entry*(MIN_SL_PCT/100.0)
        max_gap  = entry*(MAX_SL_PCT/100.0)
        gap = entry - sl_raw
        if gap < min_gap: sl_raw = entry - min_gap
        if gap > max_gap: sl_raw = entry - max_gap
    else:
        sl_swing = recent_highs
        sl_atr   = entry + atr_dist
        sl_raw   = max(sl_swing, sl_atr)
        min_gap  = entry*(MIN_SL_PCT/100.0)
        max_gap  = entry*(MAX_SL_PCT/100.0)
        gap = sl_raw - entry
        if gap < min_gap: sl_raw = entry + min_gap
        if gap > max_gap: sl_raw = entry + max_gap

    sl = float(sl_raw)
    tps = [entry*(1+p/100.0) for p in TP_PCTS] if side=="LONG" else [entry*(1-p/100.0) for p in TP_PCTS]

    return ({
        "side":side, "entry":float(entry), "sl":sl, "tps":[float(x) for x in tps],
        "confidence":int(conf)
    }, {})

# ================= TP/SL & profit messages =================
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
    if side=="LONG":  return (exit_price/entry - 1.0)*100.0
    else:             return (1.0 - exit_price/entry)*100.0

def elapsed_text(start_ts:int, end_ts:int)->str:
    secs = max(0, end_ts - start_ts)
    mins = secs // 60
    if mins < 60: return f"{mins} دقيقة"
    hrs = mins // 60
    rem = mins % 60
    return f"{hrs} ساعة {rem} دقيقة"

# ================= FastAPI =================
app=FastAPI()
@app.get("/")
def root():
    return {"ok":True,"exchange":getattr(app.state,"exchange_id",EXCHANGE_NAME),
            "tf":TIMEFRAME,"symbols":len(getattr(app.state,"symbols",[])),
            "min_confidence":MIN_CONFIDENCE}

# ================= Core Loop =================
async def fetch_and_signal(ex, symbol:str, holder):
    global _last_cycle_alerts
    out=await fetch_ohlcv_safe(ex, symbol, TIMEFRAME, OHLCV_LIMIT)
    if isinstance(out,str):
        db_insert_error(unix_now(),ex.id,symbol,out); return
    if out is None or len(out)<60:
        db_insert_nosignal(unix_now(),ex.id,symbol,{"insufficient_data":True}); return

    # cooldown per symbol
    st = signal_state.get(symbol, {})
    closed_idx = len(out)-2
    if closed_idx < st.get("cooldown_until_idx",-999999):
        return
    if symbol in open_trades: return

    sig, reasons = smart_signal(out)
    if not sig:
        db_insert_nosignal(unix_now(), ex.id, symbol, reasons or {"note":"no_setup"}); return

    if sig["confidence"] < MIN_CONFIDENCE:
        db_insert_nosignal(unix_now(), ex.id, symbol, {**reasons, "confidence_lt_min":sig["confidence"]}); return
    if _last_cycle_alerts >= MAX_ALERTS_PER_CYCLE:
        db_insert_nosignal(unix_now(), ex.id, symbol, {"cycle_cap_reached":True, "confidence":sig["confidence"]}); return

    pretty=symbol_pretty(symbol)
    side_txt="طويل 🟢" if sig["side"]=="LONG" else "قصير 🔴"
    entry,sl,tps,conf = sig["entry"],sig["sl"],sig["tps"],sig["confidence"]
    msg=(f"#{pretty} - {side_txt}\n\n"
         f"نقطة الدخول: {entry}\n"
         f"وقف الخسارة: {sl}\n\n"
         f"الهدف 1: {tps[0]}\nالهدف 2: {tps[1]}\nالهدف 3: {tps[2]}\nالهدف 4: {tps[3]}\n\n"
         f"نسبة الثقة: {conf}%")

    mid=send_telegram(msg, reply_to_message_id=holder.get("id"))
    if mid:
        ts_now = unix_now()
        open_trades[symbol]={"side":sig["side"],"entry":entry,"sl":sl,"tps":tps,"hit":[False]*4,
                             "msg_id":mid,"signal_id":None,"opened_ts":ts_now}
        signal_state[symbol]={"last_entry":entry,"last_side":sig["side"],
                              "last_candle_idx":closed_idx,
                              "cooldown_until_idx":closed_idx+COOLDOWN_PER_SYMBOL_CANDLES}
        sid=db_insert_signal(ts_now, ex.id, symbol, sig["side"], entry, sl, tps, conf, mid)
        open_trades[symbol]["signal_id"]=sid
        _last_cycle_alerts += 1

async def check_open_trades(ex):
    for sym, pos in list(open_trades.items()):
        price=await fetch_ticker_price(ex,sym)
        res=crossed_levels(pos["side"],price,pos["tps"],pos["sl"],pos["hit"])
        if not res: continue
        kind,idx=res; ts=unix_now()
        if kind=="SL":
            pr = pct_profit(pos["side"], pos["entry"], price or pos["sl"])
            elapsed = elapsed_text(pos.get("opened_ts",ts), ts)
            send_telegram(
                f"#{symbol_pretty(sym)}\nتم ضرب وقف الخسارة ❌\n"
                f"النتيجة: {round(pr,4)}% 📉\n"
                f"في: {elapsed} ⏰",
                reply_to_message_id=pos["msg_id"]
            )
            if pos.get("signal_id"): db_insert_outcome(pos["signal_id"],ts,"SL",-1,price or 0.0)
            del open_trades[sym]
        else:
            pos["hit"][idx]=True; tp=pos["tps"][idx]
            pr = pct_profit(pos["side"], pos["entry"], tp if price is None else price)
            elapsed = elapsed_text(pos.get("opened_ts",ts), ts)
            send_telegram(
                f"#{symbol_pretty(sym)}\nتم الوصول إلى الهدف {idx+1} ✅\n"
                f"الربح: {round(pr,4)}% 📈\n"
                f"في: {elapsed} ⏰",
                reply_to_message_id=pos["msg_id"]
            )
            if pos.get("signal_id"): db_insert_outcome(pos["signal_id"],ts,f"TP{idx+1}",idx,price or tp)
            if all(pos["hit"]): del open_trades[sym]

# ================= Scan/Loop =================
async def scan_once(ex, symbols:List[str], holder):
    global _last_cycle_alerts
    _last_cycle_alerts = 0
    await check_open_trades(ex)
    if not symbols: return
    random.shuffle(symbols)
    sem=asyncio.Semaphore(3)
    async def worker(s): 
        async with sem: await fetch_and_signal(ex,s,holder)
    await asyncio.gather(*[asyncio.create_task(worker(s)) for s in symbols])
    if SEND_NO_SIGNAL_SUMMARY and _last_cycle_alerts==0:
        send_telegram("> ℹ️ لا توجد إشارات بهذه الدورة (الأسباب محفوظة في القاعدة).", reply_to_message_id=holder.get("id"))

# ================= Commands (اختصار) =================
# إن كنت تستخدم 1_2 سابقًا، بلوك الأوامر /stats /reasons /last /open /export يعمل دون تعديل.
# لتقليل طول الملف هنا، يمكن نسخ نفس poll_telegram_commands من 1_2 كما هو.

# ================= Startup / Runner =================
app=FastAPI()
app.state.exchange=None; app.state.exchange_id=EXCHANGE_NAME; app.state.symbols=[]
@app.get("/")
def health():
    return {"ok":True,"exchange":getattr(app.state,"exchange_id",EXCHANGE_NAME),
            "tf":TIMEFRAME,"symbols":len(getattr(app.state,"symbols",[]))}

def attempt_build():
    ex,used = try_failover(EXCHANGE_NAME)
    syms = parse_symbols_from_env(ex, SYMBOLS_ENV)
    app.state.exchange, app.state.exchange_id, app.state.symbols = ex, used, syms

@app.on_event("startup")
async def _startup():
    db_init()
    status_id = send_telegram(f"> توصيات تداول Ai:\n✅ البوت اشتغل\nExchange: (initializing)\nTF: {TIMEFRAME}\nPairs: (loading…)")
    app.state.status_msg_id_holder={"id":status_id}
    attempt_build()
    syms=app.state.symbols; ex_id=app.state.exchange_id
    send_telegram(f"> تحديث الإقلاع:\nExchange: {ex_id}\nTF: {TIMEFRAME}\nPairs: {', '.join([symbol_pretty(s) for s in syms[:10]])}{'' if len(syms)<=10 else f' …(+{len(syms)-10})'}", reply_to_message_id=status_id)
    asyncio.create_task(runner())

async def runner():
    holder=app.state.status_msg_id_holder
    while True:
        try:
            if not app.state.symbols: attempt_build()
            await scan_once(app.state.exchange, app.state.symbols, holder)
        except Exception as e:
            send_telegram(f"⚠️ Loop error: {type(e).__name__} {str(e)[:180]}", reply_to_message_id=holder.get("id"))
        await asyncio.sleep(SCAN_INTERVAL)

if __name__=="__main__":
    port=int(os.getenv("PORT","10000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
