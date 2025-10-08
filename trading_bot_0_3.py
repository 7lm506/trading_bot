# bot_v6_vwap_regime_single.py
# Futures Signals — Regime + VWAP Bands (single file)
# v6.0.0 — one-file build

import os, asyncio, time, io, csv, json, math, random, sqlite3
from typing import Dict, List, Optional, Tuple, Callable
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np
import ccxt
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn

# ============================== CONFIG (ENV) ==============================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()

EXCHANGE_NAME  = os.getenv("EXCHANGE", "okx").strip().lower()
SYMBOLS_VAL    = os.getenv("SYMBOLS", "ALL").strip()
TIMEFRAME      = os.getenv("TIMEFRAME", "15m").strip()
RENDER_URL     = os.getenv("RENDER_EXTERNAL_URL", "").strip()

# Strategy params
MIN_CONFIDENCE       = int(os.getenv("MIN_CONFIDENCE", "55"))
MIN_ATR_PCT          = float(os.getenv("MIN_ATR_PCT", "0.12"))
MIN_AVG_VOL_USDT     = float(os.getenv("MIN_AVG_VOL_USDT", "60000"))

ADX_TRENDING_MIN     = float(os.getenv("ADX_TRENDING_MIN", "25"))
ADX_RANGING_MAX      = float(os.getenv("ADX_RANGING_MAX", "20"))
EMA_SLOPE_THRESHOLD  = float(os.getenv("EMA_SLOPE_THRESHOLD", "0.0015"))

VWAP_STD_MULT_1      = float(os.getenv("VWAP_STD_MULT_1", "1.0"))
VWAP_STD_MULT_2      = float(os.getenv("VWAP_STD_MULT_2", "2.0"))
VWAP_PERIOD          = os.getenv("VWAP_PERIOD", "session")  # placeholder anchor label

REQUIRE_HTF_ALIGNMENT= os.getenv("REQUIRE_HTF_ALIGNMENT", "false").lower()=="true"
HTF_TIMEFRAME        = os.getenv("HTF_TIMEFRAME", "1h")
MIN_VOLUME_RATIO     = float(os.getenv("MIN_VOLUME_RATIO", "1.2"))
MAX_EXTENDED_ATR_MULT= float(os.getenv("MAX_EXTENDED_ATR_MULT", "1.8"))

TP_PCTS              = [
    float(os.getenv("TP1_PCT", "1.0")),
    float(os.getenv("TP2_PCT", "2.0")),
    float(os.getenv("TP3_PCT", "3.5")),
    float(os.getenv("TP4_PCT", "5.5")),
]
ATR_SL_MULT          = float(os.getenv("ATR_SL_MULT", "1.5"))
MIN_SL_PCT           = float(os.getenv("MIN_SL_PCT", "0.7"))
MAX_SL_PCT           = float(os.getenv("MAX_SL_PCT", "2.5"))
MIN_RR_RATIO         = float(os.getenv("MIN_RR_RATIO", "1.2"))

# Scan/runtime
SCAN_INTERVAL               = int(os.getenv("SCAN_INTERVAL", "45"))
MAX_ALERTS_PER_CYCLE        = int(os.getenv("MAX_ALERTS_PER_CYCLE", "6"))
COOLDOWN_PER_SYMBOL_CANDLES = int(os.getenv("COOLDOWN_PER_SYMBOL_CANDLES", "8"))
MAX_SYMBOLS                 = int(os.getenv("MAX_SYMBOLS", "120"))
MIN_SIGNAL_GAP_SEC          = int(os.getenv("MIN_SIGNAL_GAP_SEC", "5"))

LOG_DB_PATH          = os.getenv("LOG_DB_PATH", "bot_stats.db")
KEEPALIVE_URL        = RENDER_URL
KEEPALIVE_INTERVAL   = int(os.getenv("KEEPALIVE_INTERVAL", "180"))
POLL_COMMANDS        = os.getenv("POLL_COMMANDS", "true").lower()=="true"
POLL_INTERVAL        = int(os.getenv("POLL_INTERVAL", "10"))

APP_VERSION          = f"6.0.0-VWAP-Regime ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})"

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise SystemExit("ENV مفقودة: TELEGRAM_TOKEN و CHAT_ID مطلوبة.")

# ============================== UTILS ==============================
def unix_now() -> int:
    return int(datetime.now(timezone.utc).timestamp())

def clamp(x, a, b):
    return max(a, min(b, x))

def symbol_pretty(s: str) -> str:
    return s.replace(":USDT","")

def _f(x)->float:
    v=float(x)
    if math.isnan(v) or math.isinf(v) or v < 0:
        raise ValueError("invalid")
    return v

# ============================== TELEGRAM ==============================
TG_API   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
SEND_URL = TG_API + "/sendMessage"
DOC_URL  = TG_API + "/sendDocument"
GET_UPD  = TG_API + "/getUpdates"
DEL_WH   = TG_API + "/deleteWebhook"

_last_send_ts = 0
TG_OFFSET = 0

def tg_menu_markup() -> str:
    rows = [
        ["الإحصائيات", "تحليل متقدم"],
        ["الأسباب", "آخر الإشارات"],
        ["المفتوحة", "تصدير CSV"],
        ["تصدير تحليلي", "تحديث القائمة"]
    ]
    return json.dumps({
        "keyboard":[[{"text":t} for t in row] for row in rows],
        "resize_keyboard": True,
        "is_persistent": True
    })

def tg_send(text: str, reply_to: Optional[int]=None, reply_markup: Optional[str]=None)->Optional[int]:
    global _last_send_ts
    now=time.time()
    if now-_last_send_ts < MIN_SIGNAL_GAP_SEC:
        time.sleep(MIN_SIGNAL_GAP_SEC-(now-_last_send_ts))
    data={"chat_id":CHAT_ID,"text":text,"disable_web_page_preview":True}
    if reply_to: data["reply_to_message_id"]=reply_to
    if reply_markup: data["reply_markup"]=reply_markup
    try:
        r=requests.post(SEND_URL,data=data,timeout=25).json()
        if not r.get("ok"):
            print("TG error:",r); return None
        _last_send_ts=time.time()
        return r["result"]["message_id"]
    except Exception as e:
        print("TG send err:",e); return None

def tg_send_doc(filename:str, bytes_:bytes, caption:str="")->bool:
    try:
        files={"document":(filename, io.BytesIO(bytes_))}
        data={"chat_id":CHAT_ID,"caption":caption}
        r=requests.post(DOC_URL,data=data,files=files,timeout=60).json()
        return bool(r.get("ok"))
    except Exception as e:
        print("TG doc err:",e); return False

def tg_delete_webhook():
    try:
        requests.post(DEL_WH, data={"drop_pending_updates": False}, timeout=10)
    except:
        pass

def parse_cmd(text:str)->Tuple[str,str]:
    t=(text or "").strip()
    if t.startswith("/"):
        parts=t.split(maxsplit=1)
        cmd=parts[0].lower()
        arg=parts[1].strip() if len(parts)>1 else ""
        if "@" in cmd: cmd=cmd.split("@",1)[0]
        return cmd,arg
    return t,""

async def poll_commands(handler:Callable[[str,str], asyncio.Future]):
    if not POLL_COMMANDS: return
    tg_delete_webhook()
    global TG_OFFSET
    while True:
        try:
            r=requests.get(GET_UPD, params={"timeout":25,"offset":TG_OFFSET+1}, timeout=35).json()
            if r.get("ok"):
                for upd in r.get("result",[]):
                    TG_OFFSET=max(TG_OFFSET, upd["update_id"])
                    msg=upd.get("message") or upd.get("edited_message")
                    if not msg: continue
                    if str(msg.get("chat",{}).get("id")) != str(CHAT_ID): continue
                    text=msg.get("text","")
                    cmd,arg = parse_cmd(text)
                    await handler(cmd,arg)
        except Exception as e:
            print("[poll]",e)
        await asyncio.sleep(POLL_INTERVAL)

# ============================== DB ==============================
def db_conn():
    con=sqlite3.connect(LOG_DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    return con

def db_init():
    con=db_conn()
    con.execute("""CREATE TABLE IF NOT EXISTS signals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, exchange TEXT, symbol TEXT, side TEXT,
        entry REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL, tp4 REAL,
        confidence INTEGER, msg_id INTEGER, strategy TEXT DEFAULT 'vwap_regime'
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS outcomes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER, ts INTEGER, event TEXT, idx INTEGER, price REAL
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS nosignal_reasons(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, exchange TEXT, symbol TEXT, reasons TEXT, strategy TEXT DEFAULT 'vwap_regime'
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS errors(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, exchange TEXT, symbol TEXT, message TEXT
    )""")
    con.commit(); con.close()

def db_insert_signal(ts,ex,sym,side,entry,sl,tps,conf,msg_id)->int:
    con=db_conn(); cur=con.cursor()
    cur.execute("""INSERT INTO signals(ts,exchange,symbol,side,entry,sl,tp1,tp2,tp3,tp4,confidence,msg_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ts,ex,sym,side,entry,sl,tps[0],tps[1],tps[2],tps[3],conf,msg_id))
    con.commit(); sid=cur.lastrowid; con.close(); return sid

def db_insert_outcome(signal_id,ts,event,idx,price):
    con=db_conn()
    con.execute("INSERT INTO outcomes(signal_id,ts,event,idx,price) VALUES(?,?,?,?,?)",
                (signal_id,ts,event,idx,price))
    con.commit(); con.close()

def db_insert_nosignal(ts,ex,sym,rsn:Dict):
    con=db_conn()
    con.execute("INSERT INTO nosignal_reasons(ts,exchange,symbol,reasons) VALUES(?,?,?,?)",
                (ts,ex,sym,json.dumps(rsn,ensure_ascii=False)))
    con.commit(); con.close()

def db_insert_error(ts,ex,sym,msg):
    con=db_conn()
    con.execute("INSERT INTO errors(ts,exchange,symbol,message) VALUES(?,?,?,?)",
                (ts,ex,sym,msg))
    con.commit(); con.close()

def db_text_stats(days:int=1)->str:
    try:
        con=db_conn(); cur=con.cursor()
        cur.execute("SELECT COUNT(*) FROM signals WHERE ts >= strftime('%s','now', ?)", (f"-{days} day",))
        total=cur.fetchone()[0] or 0
        cur.execute("""SELECT SUM(CASE WHEN event LIKE 'TP%' THEN 1 ELSE 0 END),
                              SUM(CASE WHEN event='SL' THEN 1 ELSE 0 END)
                       FROM outcomes WHERE ts >= strftime('%s','now', ?)""", (f"-{days} day",))
        row=cur.fetchone() or (0,0)
        tp,sl = row[0] or 0, row[1] or 0
        con.close()
        if total==0: return "لا توجد بيانات بعد."
        success = (tp/(tp+sl)*100) if (tp+sl)>0 else 0
        return f"إحصائيات ({days}ي):\n━━━━━━━━━━━━━\nإشارات: {total}\nأهداف: {tp} | ستوب: {sl}\nنجاح: {success:.1f}%"
    except Exception as e:
        return f"خطأ: {e}"

def db_detailed_stats(days:int=7)->str:
    try:
        con=db_conn(); cur=con.cursor()
        cur.execute("SELECT COUNT(*), AVG(confidence) FROM signals WHERE ts >= strftime('%s','now', ?)", (f"-{days} day",))
        total, avg_conf = cur.fetchone(); total=total or 0; avg_conf=avg_conf or 0
        cur.execute("SELECT side, COUNT(*) FROM signals WHERE ts >= strftime('%s','now', ?) GROUP BY side",(f"-{days} day",))
        side_stats=dict(cur.fetchall())
        cur.execute("""SELECT o.event, COUNT(*) FROM outcomes o
                       JOIN signals s ON o.signal_id=s.id
                       WHERE o.ts >= strftime('%s','now', ?) GROUP BY o.event""",(f"-{days} day",))
        outcome_stats=dict(cur.fetchall()); con.close()
        if total==0: return "لا توجد بيانات."
        total_tp=sum(v for k,v in outcome_stats.items() if k.startswith("TP"))
        total_sl=outcome_stats.get("SL",0)
        win=(total_tp/(total_tp+total_sl)*100) if (total_tp+total_sl)>0 else 0
        lines=[f"تحليل ({days}ي)","═"*20,f"إشارات: {total}",f"ثقة: {avg_conf:.1f}%","","اتجاهات:"]
        for s,c in side_stats.items(): lines.append(f"  {s}: {c}")
        lines.append(""); lines.append(f"نجاح: {win:.1f}%")
        for k,v in sorted(outcome_stats.items()): lines.append(f"  {k}: {v}")
        return "\n".join(lines)
    except Exception as e:
        return f"خطأ: {e}"

def db_text_reasons(window:str="1d")->str:
    unit=window[-1].lower()
    num=int(''.join([ch for ch in window if ch.isdigit()]) or 1)
    sql_win=f"-{num} {'hour' if unit=='h' else 'day'}"
    try:
        con=db_conn(); cur=con.cursor()
        cur.execute("SELECT reasons FROM nosignal_reasons WHERE ts >= strftime('%s','now', ?)", (sql_win,))
        rows=cur.fetchall(); con.close()
        if not rows: return "لا توجد أسباب."
        from collections import Counter
        items=[]
        for (js,) in rows:
            try:
                d=json.loads(js) if isinstance(js,str) else {}
                if isinstance(d,dict):
                    for k,v in d.items():
                        if isinstance(v,(int,float)): items.append(f"{k}={v}")
                        elif isinstance(v,bool): items.append(k if v else f"!{k}")
                        else: items.append(f"{k}:{v}")
            except: pass
        cnt=Counter(items)
        lines=[f"أسباب ({window}):","━"*30]
        for i,(k,v) in enumerate(cnt.most_common(10),1):
            lines.append(f"{i}. {k}: {v}")
        return "\n".join(lines)
    except Exception as e:
        return f"خطأ: {e}"

def db_text_last(limit:int=10)->str:
    try:
        con=db_conn(); cur=con.cursor()
        cur.execute("""SELECT s.symbol, s.side, s.confidence,
                       (SELECT event FROM outcomes o WHERE o.signal_id=s.id ORDER BY o.ts LIMIT 1)
                       FROM signals s ORDER BY s.id DESC LIMIT ?""",(limit,))
        rows=cur.fetchall(); con.close()
        if not rows: return "لا إشارات."
        out=["آخر الإشارات:","━"*15]
        for r in rows:
            result=r[3] or "قيد التنفيذ"
            emoji="✅" if (result and result.startswith("TP")) else ("❌" if result=="SL" else "⏳")
            out.append(f"{emoji} {symbol_pretty(r[0])} {r[1]} {r[2]}% → {result}")
        return "\n".join(out)
    except Exception as e:
        return f"خطأ: {e}"

def export_csv_bytes(days:int=14)->bytes:
    con=db_conn(); cur=con.cursor()
    cur.execute("""SELECT s.id,s.ts,s.exchange,s.symbol,s.side,s.entry,s.sl,
                          s.tp1,s.tp2,s.tp3,s.tp4,s.confidence,
                          (SELECT GROUP_CONCAT(event||':'||price,'|') FROM outcomes o WHERE o.signal_id=s.id)
                   FROM signals s WHERE s.ts >= strftime('%s','now', ?) ORDER BY s.id DESC""",(f"-{days} day",))
    rows=cur.fetchall(); con.close()
    out=io.StringIO(); w=csv.writer(out)
    w.writerow(["id","ts","exchange","symbol","side","entry","sl","tp1","tp2","tp3","tp4","confidence","outcomes"])
    for r in rows: w.writerow(r)
    return out.getvalue().encode("utf-8")

def export_analysis_csv(days:int=14)->bytes:
    con=db_conn(); cur=con.cursor()
    cur.execute("""SELECT s.id, datetime(s.ts,'unixepoch'), s.exchange, s.symbol, s.side,
                          s.entry, s.sl, s.tp1, s.tp2, s.tp3, s.tp4, s.confidence,
                          (s.entry - s.sl)/s.entry*100 as risk_pct,
                          (SELECT event FROM outcomes o WHERE o.signal_id=s.id ORDER BY o.ts LIMIT 1),
                          (SELECT price FROM outcomes o WHERE o.signal_id=s.id ORDER BY o.ts LIMIT 1),
                          CASE WHEN EXISTS(SELECT 1 FROM outcomes o WHERE o.signal_id=s.id AND o.event='SL') THEN 'LOSS'
                               WHEN EXISTS(SELECT 1 FROM outcomes o WHERE o.signal_id=s.id AND o.event LIKE 'TP%') THEN 'WIN'
                               ELSE 'OPEN' END
                   FROM signals s WHERE s.ts >= strftime('%s','now', ?) ORDER BY s.ts DESC""",(f"-{days} day",))
    rows=cur.fetchall(); con.close()
    out=io.StringIO(); w=csv.writer(out)
    w.writerow(["ID","Time","Exchange","Symbol","Side","Entry","SL","TP1","TP2","TP3","TP4","Conf","Risk%","Outcome","Price","Status"])
    for r in rows: w.writerow(r)
    return out.getvalue().encode("utf-8")

# ============================== INDICATORS ==============================
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def sma(s: pd.Series, n:int)->pd.Series:
    return s.rolling(n).mean()

def rsi(s: pd.Series, n:int=14)->pd.Series:
    d=s.diff(); up=d.clip(lower=0); dn=-d.clip(upper=0)
    ma_up=up.ewm(com=n-1, adjust=False).mean()
    ma_dn=dn.ewm(com=n-1, adjust=False).mean()
    rs=ma_up/(ma_dn.replace(0,1e-12))
    return 100-(100/(1+rs))

def atr(df: pd.DataFrame, n:int=14)->pd.Series:
    h,l,c=df["high"],df["low"],df["close"]
    tr=pd.concat([(h-l).abs(),(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def adx(df: pd.DataFrame, n:int=14)->pd.Series:
    h,l,c=df["high"],df["low"],df["close"]
    up=h.diff(); dn=-l.diff()
    plus_dm=np.where((up>dn)&(up>0), up, 0.0)
    minus_dm=np.where((dn>up)&(dn>0), dn, 0.0)
    tr=pd.concat([(h-l).abs(),(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    atrv=tr.ewm(alpha=1/n, adjust=False).mean()
    pdi=(pd.Series(plus_dm,index=df.index).ewm(alpha=1/n,adjust=False).mean()/atrv.replace(0,1e-12))*100
    mdi=(pd.Series(minus_dm,index=df.index).ewm(alpha=1/n,adjust=False).mean()/atrv.replace(0,1e-12))*100
    dx=(abs(pdi-mdi)/(pdi+mdi).replace(0,1e-12))*100
    return dx.ewm(alpha=1/n,adjust=False).mean()

def bollinger(s: pd.Series, n:int=20, k:float=2.0)->Tuple[pd.Series,pd.Series,pd.Series,pd.Series]:
    ma=s.rolling(n).mean(); sd=s.rolling(n).std(ddof=0)
    up=ma+k*sd; dn=ma-k*sd; bw=(up-dn)/ma.replace(0,1e-12)
    return ma,up,dn,bw

def vwap_anchored(df: pd.DataFrame, anchor:str="session")->Tuple[pd.Series,pd.Series,pd.Series]:
    tp=(df["high"]+df["low"]+df["close"])/3
    v_sum=(tp*df["volume"]).cumsum()
    vol_sum=df["volume"].cumsum()
    vwap=v_sum/vol_sum.replace(0,1e-12)
    sq=((tp-vwap)**2)*df["volume"]
    var=sq.cumsum()/vol_sum.replace(0,1e-12)
    std=np.sqrt(var)
    return vwap, std, tp

# ============================== EXCHANGES (CCXT) ==============================
EXC={"bybit":ccxt.bybit,"okx":ccxt.okx,"kucoinfutures":ccxt.kucoinfutures,"bitget":ccxt.bitget,"gate":ccxt.gate,"binance":ccxt.binance}

def make_exchange(name:str):
    klass=EXC.get(name, ccxt.okx)
    cfg={"enableRateLimit":True,"timeout":20000,"options":{"defaultType":"swap","defaultSubType":"linear"}}
    return klass(cfg)

def load_markets_linear_only(ex):
    last=None
    for i,b in enumerate([1.5,3,6],1):
        try:
            ex.load_markets(reload=True, params={"category":"linear","type":"swap"})
            return
        except Exception as e:
            last=e; print(f"[load_markets {i}] {type(e).__name__}: {str(e)[:140]}"); time.sleep(b)
    raise last

def try_failover(primary:str)->Tuple[ccxt.Exchange,str]:
    last=None
    for name in [primary,"okx","bybit","bitget","kucoinfutures","gate","binance"]:
        try:
            ex=make_exchange(name); load_markets_linear_only(ex); print("✓",name); return ex,name
        except Exception as e:
            last=e; print("[fail]",name,type(e).__name__)
    raise last or SystemExit("No exchange available")

def normalize_symbols(ex, syms:List[str])->List[str]:
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
    return normalize_symbols(ex, syms)

def parse_symbols(ex, val:str)->List[str]:
    key=(val or "").strip().upper()
    if key in ("ALL","AUTO","AUTO_FUTURES","AUTO_SWAP","AUTO_LINEAR"):
        return list_all_futures_symbols(ex)
    syms=[s.strip() for s in (val or "").split(",") if s.strip()]
    syms=normalize_symbols(ex, syms)
    if MAX_SYMBOLS>0: syms=syms[:MAX_SYMBOLS]
    return syms

async def fetch_ohlcv_safe(ex, symbol:str, timeframe:str, limit:int)->Optional[pd.DataFrame]:
    for attempt in range(2):
        try:
            params={}
            if ex.id=="bybit": params={"category":"linear"}
            elif ex.id=="okx": params={"instType":"SWAP"}
            ohlcv=await asyncio.to_thread(ex.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit, params=params)
            if not ohlcv or len(ohlcv)<60: return None
            df=pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
            if df["close"].isna().any() or (df["close"]==0).any():
                if attempt==0:
                    await asyncio.sleep(1); continue
                return None
            df["ts"]=pd.to_datetime(df["ts"], unit="ms", utc=True)
            df.set_index("ts", inplace=True)
            return df
        except Exception:
            if attempt==0:
                await asyncio.sleep(1); continue
            return None
    return None

async def fetch_ticker_price(ex, symbol:str)->Optional[float]:
    try:
        t=await asyncio.to_thread(ex.fetch_ticker, symbol)
        v=t.get("last") or t.get("close") or t.get("info",{}).get("lastPrice")
        return float(v) if v is not None else None
    except Exception:
        return None

# ============================== STRATEGY: VWAP + REGIME ==============================
async def htf_alignment_ok(ex, symbol:str, regime:str)->Optional[bool]:
    if not REQUIRE_HTF_ALIGNMENT:
        return True
    df=await fetch_ohlcv_safe(ex, symbol, HTF_TIMEFRAME, 400)
    if df is None: return None
    c=df["close"]
    e50=float(ema(c,50).iloc[-2]); e200=float(ema(c,200).iloc[-2])
    if regime=="trending":
        return e50>e200
    return True

def detect_regime(df:pd.DataFrame, adx_now:float, atr_now:float)->str:
    c=df["close"]
    ema200_series=ema(c,200)
    slope=(ema200_series.iloc[-1]-ema200_series.iloc[-10])/max(1e-12,abs(ema200_series.iloc[-10]))
    atr_pct = atr_now / max(c.iloc[-2],1e-12)
    is_trending = (adx_now >= ADX_TRENDING_MIN) and (abs(slope) >= EMA_SLOPE_THRESHOLD) and (atr_pct >= 0.015)
    return "trending" if is_trending else "ranging"

def calc_sl(df, side, entry, atr_now, vwap_u1, vwap_l1)->float:
    h=df["high"]; l=df["low"]
    if side=="LONG":
        recent_low=float(l.tail(20).min())
        sl_raw=min(recent_low, vwap_l1) - ATR_SL_MULT*atr_now
    else:
        recent_high=float(h.tail(20).max())
        sl_raw=max(recent_high, vwap_u1) + ATR_SL_MULT*atr_now
    gap=abs(entry-sl_raw)
    min_gap=entry*(MIN_SL_PCT/100.0)
    max_gap=entry*(MAX_SL_PCT/100.0)
    if gap<min_gap: sl_raw = entry - min_gap if side=="LONG" else entry + min_gap
    if gap>max_gap: sl_raw = entry - max_gap if side=="LONG" else entry + max_gap
    return float(sl_raw)

def calc_tps(side, entry)->List[float]:
    return [entry*(1+p/100.0) for p in TP_PCTS] if side=="LONG" else [entry*(1-p/100.0) for p in TP_PCTS]

def compute_confidence(side:str, regime:str, htf_ok:bool, adx_now:float, volume_ratio:float,
                       rsi_now:float, rr_ratio:float, c_now:float, vwap_now:float, e50:float)->int:
    # weights tuned roughly
    w = {
        "htf":0.28, "trend":0.22, "vol":0.14, "rr":0.12, "rsi":0.10, "price_pos":0.08, "vwap_bias":0.06
    }
    score=0.0
    score += w["htf"]*(1.0 if (htf_ok is True) else (0.5 if htf_ok is None else 0.0))
    trend_score = clamp((adx_now-20)/(35-20),0,1)
    score += w["trend"]*trend_score
    vol_score = clamp((volume_ratio-1.0)/0.8,0,1)
    score += w["vol"]*vol_score
    rr_score = clamp((rr_ratio-1.0)/1.0,0,1)
    score += w["rr"]*rr_score
    target=55 if side=="LONG" else 45
    rsi_score=clamp(1-abs(rsi_now-target)/25.0,0,1)
    score += w["rsi"]*rsi_score
    price_pos = (c_now/e50-1) if side=="LONG" else (1-c_now/e50)
    score += w["price_pos"]*clamp(price_pos,0,1)
    vwap_bias = (c_now/vwap_now-1) if side=="LONG" else (1-c_now/vwap_now)
    score += w["vwap_bias"]*clamp(vwap_bias,0,1)
    return int(round(score*100))

async def analyze_signal(ex, symbol:str, df:pd.DataFrame)->Tuple[Optional[Dict],Dict]:
    if df is None or len(df)<80: return None, {"insufficient_data":True}
    c=df["close"]; h=df["high"]; l=df["low"]; v=df["volume"]
    ema50_s=ema(c,50); ema200_s=ema(c,200)
    atr14=atr(df,14); adx14=adx(df,14); rsi14=rsi(c,14)
    vwap, vstd, tp=vwap_anchored(df, VWAP_PERIOD)
    v_u1=vwap + VWAP_STD_MULT_1*vstd
    v_l1=vwap - VWAP_STD_MULT_1*vstd
    v_u2=vwap + VWAP_STD_MULT_2*vstd
    v_l2=vwap - VWAP_STD_MULT_2*vstd

    i=-2
    try:
        c_now=_f(c.iloc[i]); atr_now=_f(atr14.iloc[i]); adx_now=float(adx14.iloc[i]); rsi_now=float(rsi14.iloc[i])
        e50=_f(ema50_s.iloc[i]); e200=_f(ema200_s.iloc[i])
        vwap_now=float(vwap.iloc[i]); vu1=float(v_u1.iloc[i]); vl1=float(v_l1.iloc[i]); vu2=float(v_u2.iloc[i]); vl2=float(v_l2.iloc[i])
    except Exception as e:
        return None, {"data_error":str(e)[:50]}

    avg_usdt=float((v*c).tail(30).mean())
    if avg_usdt < MIN_AVG_VOL_USDT:
        return None, {"low_liquidity":int(avg_usdt), "th":MIN_AVG_VOL_USDT}
    atr_pct=100*atr_now/max(c_now,1e-9)
    if atr_pct < MIN_ATR_PCT:
        return None, {"atr_pct_low":round(atr_pct,3)}

    regime=detect_regime(df, adx_now, atr_now)
    htf_ok = await htf_alignment_ok(ex, symbol, regime)
    if REQUIRE_HTF_ALIGNMENT and htf_ok is None:
        return None, {"htf_missing":True}

    # extended candle filter
    body=abs(df["close"].iloc[i]-df["open"].iloc[i])
    if body > MAX_EXTENDED_ATR_MULT*atr_now:
        return None, {"extended_candle":True}

    volume_ratio=float(v.iloc[i]/max(v.tail(30).mean(),1e-9))

    side=None
    if regime=="trending":
        if e50>e200:
            touched_lower = (l.iloc[i] <= vl1 and c_now >= vl1)
            if not touched_lower: return None, {"no_pullback_to_vwap_band":"lower","regime":regime}
            if volume_ratio < MIN_VOLUME_RATIO: return None, {"volume_ratio_low":round(volume_ratio,2),"regime":regime}
            if 30 <= rsi_now <= 70: side="LONG"
            else: return None, {"rsi":round(rsi_now,1)}
        elif e50<e200:
            touched_upper = (h.iloc[i] >= vu1 and c_now <= vu1)
            if not touched_upper: return None, {"no_pullback_to_vwap_band":"upper","regime":regime}
            if volume_ratio < MIN_VOLUME_RATIO: return None, {"volume_ratio_low":round(volume_ratio,2),"regime":regime}
            if 30 <= rsi_now <= 70: side="SHORT"
            else: return None, {"rsi":round(rsi_now,1)}
        else:
            return None, {"no_clear_trend":True}
    else: # ranging mean-reversion
        if (c_now <= vl2 and rsi_now < 35) and (volume_ratio >= MIN_VOLUME_RATIO):
            side="LONG"
        elif (c_now >= vu2 and rsi_now > 65) and (volume_ratio >= MIN_VOLUME_RATIO):
            side="SHORT"
        else:
            return None, {"range_no_extreme":True, "vol_ratio":round(volume_ratio,2)}

    entry=float(c_now)
    sl=calc_sl(df, side, entry, atr_now, vu1, vl1)
    tps=[float(x) for x in calc_tps(side, entry)]
    risk=abs(entry-sl); reward=abs(tps[2]-entry); rr= reward/risk if risk>0 else 0
    if rr < MIN_RR_RATIO:
        return None, {"rr_low":round(rr,2),"min":MIN_RR_RATIO}

    conf=compute_confidence(side, regime, htf_ok if htf_ok is not None else False, adx_now, volume_ratio,
                            rsi_now, rr, c_now, vwap_now, e50)
    if conf < MIN_CONFIDENCE:
        return None, {"conf_low":conf,"min":MIN_CONFIDENCE}

    return {
        "side": side, "entry": entry, "sl": float(sl), "tps": tps,
        "confidence": int(conf), "rr_ratio": round(rr,2),
        "volume_ratio": round(volume_ratio,2), "adx": round(adx_now,1), "regime": regime
    }, {}

# ============================== ENGINE ==============================
open_trades: Dict[str,Dict]={}
signal_state: Dict[str,Dict]={}
_last_cycle_alerts=0

async def crossed_levels(side:str, price:float, tps:List[float], sl:float, hit:List[bool]):
    if price is None: return None
    if side=="LONG" and price<=sl: return ("SL",-1)
    if side=="SHORT" and price>=sl: return ("SL",-1)
    for idx,(tp,ok) in enumerate(zip(tps,hit)):
        if ok: continue
        if side=="LONG" and price>=tp: return ("TP",idx)
        if side=="SHORT" and price<=tp: return ("TP",idx)
    return None

def pct_profit(side:str, entry:float, exit_price:float)->float:
    return (exit_price/entry-1)*100.0 if side=="LONG" else (1-exit_price/entry)*100.0

def elapsed_text(start_ts:int, end_ts:int)->str:
    mins=max(0,end_ts-start_ts)//60
    return f"{mins}د" if mins<60 else f"{mins//60}س {mins%60}د"

async def fetch_and_signal(ex, symbol:str):
    global _last_cycle_alerts
    df=await fetch_ohlcv_safe(ex, symbol, TIMEFRAME, 360)
    if df is None or len(df)<80: return
    st=signal_state.get(symbol,{})
    closed_idx=len(df)-2
    if closed_idx < st.get("cooldown_until_idx",-999999): return
    if symbol in open_trades: return
    try:
        sig, reasons = await analyze_signal(ex, symbol, df)
    except Exception as e:
        db_insert_error(unix_now(), ex.id, symbol, f"analyze:{type(e).__name__}")
        return
    if not sig:
        db_insert_nosignal(unix_now(), ex.id, symbol, reasons or {})
        return
    if _last_cycle_alerts >= MAX_ALERTS_PER_CYCLE: return

    side_txt = "شراء" if sig["side"]=="LONG" else "بيع"
    side_emoji = "🟢" if sig["side"]=="LONG" else "🔴"
    entry, sl, tps, conf, rr = sig["entry"], sig["sl"], sig["tps"], sig["confidence"], sig["rr_ratio"]
    strength = "🔥🔥" if conf>=70 else ("🔥" if conf>=60 else "⭐")
    pretty = symbol_pretty(symbol)
    msg = (
        f"{side_emoji} {side_txt} - #{pretty}\n"
        f"━━━━━━━━━━━━━━━━━\n\n"
        f"ثقة: {conf}% {strength} | R/R: 1:{rr}\n\n"
        f"دخول: {entry:.6f}\n"
        f"ستوب: {sl:.6f}\n\n"
        f"أهداف:\n"
        f"  TP1: {tps[0]:.6f} ({TP_PCTS[0]}%)\n"
        f"  TP2: {tps[1]:.6f} ({TP_PCTS[1]}%)\n"
        f"  TP3: {tps[2]:.6f} ({TP_PCTS[2]}%)\n"
        f"  TP4: {tps[3]:.6f} ({TP_PCTS[3]}%)\n\n"
        f"حجم: {sig.get('volume_ratio',1):.1f}x | ADX: {sig.get('adx',0):.1f} | نظام: {sig.get('regime','?')}"
    )
    mid = tg_send(msg)
    if mid:
        ts=unix_now()
        open_trades[symbol]={"side":sig["side"],"entry":entry,"sl":sl,"tps":tps,"hit":[False]*4,"msg_id":mid,"signal_id":None,"opened_ts":ts}
        signal_state[symbol]={"cooldown_until_idx":closed_idx+COOLDOWN_PER_SYMBOL_CANDLES}
        sid=db_insert_signal(ts, ex.id, symbol, sig["side"], entry, sl, tps, conf, mid)
        open_trades[symbol]["signal_id"]=sid
        _last_cycle_alerts+=1

async def check_open_trades(ex):
    for sym, pos in list(open_trades.items()):
        price=await fetch_ticker_price(ex, sym)
        res=await crossed_levels(pos["side"], price, pos["tps"], pos["sl"], pos["hit"])
        if not res: continue
        kind, idx = res; ts=unix_now()
        if kind=="SL":
            pr = pct_profit(pos["side"], pos["entry"], price or pos["sl"])
            tg_send(f"❌ #{symbol_pretty(sym)}\n━━━━━━━━━━━━━\nستوب لوس\n\nخسارة: {round(pr,2)}%\nمدة: {elapsed_text(pos['opened_ts'], ts)}")
            if pos.get("signal_id"): db_insert_outcome(pos["signal_id"], ts, "SL", -1, price or 0.0)
            del open_trades[sym]
        else:
            pos["hit"][idx]=True
            tp=pos["tps"][idx]
            pr = pct_profit(pos["side"], pos["entry"], tp if price is None else price)
            emoji=["✅","⭐","🔥","💎"][idx] if idx<4 else "✅"
            tg_send(f"{emoji} #{symbol_pretty(sym)}\n━━━━━━━━━━━━━\nهدف {idx+1}\n\nربح: +{round(pr,2)}%\nمدة: {elapsed_text(pos['opened_ts'], ts)}")
            if pos.get("signal_id"): db_insert_outcome(pos["signal_id"], ts, f"TP{idx+1}", idx, price or tp)
            if all(pos["hit"]): del open_trades[sym]

async def scan_cycle(ex, symbols:List[str]):
    global _last_cycle_alerts
    _last_cycle_alerts=0
    await check_open_trades(ex)
    if not symbols: return
    random.shuffle(symbols)
    sem=asyncio.Semaphore(6)
    async def worker(s):
        async with sem:
            await fetch_and_signal(ex, s)
    await asyncio.gather(*[asyncio.create_task(worker(s)) for s in symbols])

# ============================== FASTAPI + STARTUP ==============================
app = FastAPI(title="Futures Signal Engine", version="6.0.0")
app.state.exchange=None
app.state.exchange_id=EXCHANGE_NAME
app.state.symbols=[]
app.state.cycle_count=0
app.state.last_scan_time=0
app.state.start_time=unix_now()

@app.get("/")
@app.head("/")
async def root():
    return {
        "ok": True,
        "status": "running",
        "version": APP_VERSION,
        "exchange": getattr(app.state,"exchange_id","unknown"),
        "timeframe": TIMEFRAME,
        "symbols": len(getattr(app.state,"symbols",[])),
        "open_trades": len(open_trades),
        "uptime": unix_now()-app.state.start_time
    }

@app.get("/health")
@app.head("/health")
async def health():
    return {"status":"healthy","timestamp":unix_now()}

@app.get("/stats")
def stats():
    return {
        "open_trades": len(open_trades),
        "cycle_count": app.state.cycle_count,
        "symbols_count": len(getattr(app.state,"symbols",[])),
        "exchange": getattr(app.state,"exchange_id",""),
        "last_scan": app.state.last_scan_time
    }

async def handle_command(cmd:str, arg:str):
    if cmd in ("/start","تحديث القائمة"):
        tg_send("القائمة الرئيسية:", reply_markup=tg_menu_markup())
    elif cmd in ("الإحصائيات","/stats"):
        days=int(arg) if arg.isdigit() else 1
        tg_send(db_text_stats(days))
    elif cmd in ("تحليل متقدم","/analysis"):
        days=int(arg) if arg.isdigit() else 7
        tg_send(db_detailed_stats(days))
    elif cmd in ("الأسباب","/reasons"):
        tg_send(db_text_reasons(arg or "1d"))
    elif cmd in ("آخر الإشارات","/last"):
        limit=int(arg) if arg.isdigit() else 10
        tg_send(db_text_last(limit))
    elif cmd in ("المفتوحة","/open"):
        if not open_trades: tg_send("لا صفقات مفتوحة.")
        else:
            lines=["صفقات مفتوحة:","━"*15]
            for s,p in open_trades.items(): lines.append(f"{symbol_pretty(s)} {p['side']} | أهداف: {sum(p['hit'])}/4")
            tg_send("\n".join(lines))
    elif cmd in ("تصدير CSV","/export"):
        days=int(arg) if arg.isdigit() else 14
        tg_send_doc("signals.csv", export_csv_bytes(days))
    elif cmd in ("تصدير تحليلي","/export_analysis"):
        days=int(arg) if arg.isdigit() else 14
        tg_send_doc("analysis.csv", export_analysis_csv(days))
    elif cmd=="/version":
        tg_send(APP_VERSION)
    else:
        tg_send("القائمة الرئيسية:", reply_markup=tg_menu_markup())

async def keepalive_task():
    if not KEEPALIVE_URL: return
    print(f"[Keepalive] {KEEPALIVE_URL}")
    while True:
        try:
            r=requests.get(KEEPALIVE_URL,timeout=10)
            print("[Keepalive]", r.status_code)
        except Exception as e:
            print("[Keepalive err]", e)
        await asyncio.sleep(KEEPALIVE_INTERVAL)

def attempt_build():
    ex, used = try_failover(EXCHANGE_NAME)
    syms = parse_symbols(ex, SYMBOLS_VAL)
    app.state.exchange = ex
    app.state.exchange_id = used
    app.state.symbols = syms

@app.on_event("startup")
async def startup():
    db_init()
    tg_send(f"بوت {APP_VERSION}\nجاري التحميل...", reply_markup=tg_menu_markup())
    attempt_build()
    syms=app.state.symbols; ex_id=app.state.exchange_id
    preview=", ".join([symbol_pretty(s) for s in syms[:10]])
    more=f"... +{len(syms)-10}" if len(syms)>10 else ""
    tg_send(f"اكتمل!\n━━━━━━━━━━━━━\n{ex_id} | {TIMEFRAME}\nأزواج: {len(syms)}\nثقة: {MIN_CONFIDENCE}%+\nCooldown: {COOLDOWN_PER_SYMBOL_CANDLES} شمعات\n━━━━━━━━━━━━━\n{preview}{more}")
    asyncio.create_task(run_loop())
    asyncio.create_task(poll_commands(handle_command))
    asyncio.create_task(keepalive_task())

async def run_loop():
    while True:
        try:
            await scan_cycle(app.state.exchange, app.state.symbols)
            app.state.cycle_count += 1
            app.state.last_scan_time = unix_now()
        except Exception as e:
            db_insert_error(unix_now(), app.state.exchange_id, "ALL", f"runner:{type(e).__name__}:{str(e)[:80]}")
            print("[Runner]", e)
        await asyncio.sleep(SCAN_INTERVAL)

if __name__=="__main__":
    port=int(os.getenv("PORT","10000"))
    print(f"Starting server on {port} …")
    uvicorn.run(app, host="0.0.0.0", port=port)
