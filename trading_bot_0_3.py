# bot_v7_smart_vwap.py
# Futures Signals — Smart VWAP Multi-Setup Strategy
# v7.0.0 — COMPLETE OVERHAUL: More signals + Better quality
# 🚀 استراتيجية جديدة كلياً:
#    ✅ 5 أنماط دخول مختلفة (بدل نمط واحد)
#    ✅ كشف ذكي للسوق (ترند قوي/ضعيف/رينج)
#    ✅ فلاتر مرنة (سيولة/تذبذب)
#    ✅ تأكيد بالزخم
#    ✅ حساب ثقة ديناميكي
#    ✅ معايير تكيّفية

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

# ============================== CONFIG ==============================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()
EXCHANGE_NAME  = os.getenv("EXCHANGE", "okx").strip().lower()
SYMBOLS_VAL    = os.getenv("SYMBOLS", "ALL").strip()
TIMEFRAME      = os.getenv("TIMEFRAME", "15m").strip()
RENDER_URL     = os.getenv("RENDER_EXTERNAL_URL", "").strip()

# 🎯 معايير الاستراتيجية - محسّنة للإشارات الكثيرة + الجودة
MIN_CONFIDENCE       = int(os.getenv("MIN_CONFIDENCE", "55"))
MIN_ATR_PCT          = float(os.getenv("MIN_ATR_PCT", "0.10"))
MIN_AVG_VOL_USDT     = float(os.getenv("MIN_AVG_VOL_USDT", "60000"))
ADX_STRONG_TREND     = float(os.getenv("ADX_STRONG_TREND", "25"))
ADX_WEAK_TREND       = float(os.getenv("ADX_WEAK_TREND", "15"))
VWAP_STD_MULT_1      = float(os.getenv("VWAP_STD_MULT_1", "1.0"))
VWAP_STD_MULT_2      = float(os.getenv("VWAP_STD_MULT_2", "1.8"))
MIN_VOLUME_RATIO     = float(os.getenv("MIN_VOLUME_RATIO", "1.0"))
MIN_VOLUME_SPIKE     = float(os.getenv("MIN_VOLUME_SPIKE", "1.3"))
MAX_BODY_ATR_RATIO   = float(os.getenv("MAX_BODY_ATR_RATIO", "2.5"))

TP_PCTS = [
    float(os.getenv("TP1_PCT", "0.8")),
    float(os.getenv("TP2_PCT", "1.6")),
    float(os.getenv("TP3_PCT", "2.8")),
    float(os.getenv("TP4_PCT", "4.5")),
]
ATR_SL_MULT          = float(os.getenv("ATR_SL_MULT", "1.4"))
MIN_SL_PCT           = float(os.getenv("MIN_SL_PCT", "0.5"))
MAX_SL_PCT           = float(os.getenv("MAX_SL_PCT", "2.8"))
MIN_RR_RATIO         = float(os.getenv("MIN_RR_RATIO", "1.2"))

SCAN_INTERVAL               = int(os.getenv("SCAN_INTERVAL", "40"))
MAX_ALERTS_PER_CYCLE        = int(os.getenv("MAX_ALERTS_PER_CYCLE", "8"))
COOLDOWN_PER_SYMBOL_CANDLES = int(os.getenv("COOLDOWN_PER_SYMBOL_CANDLES", "6"))
MAX_SYMBOLS                 = int(os.getenv("MAX_SYMBOLS", "180"))
MIN_SIGNAL_GAP_SEC          = int(os.getenv("MIN_SIGNAL_GAP_SEC", "4"))

LOG_DB_PATH          = os.getenv("LOG_DB_PATH", "bot_stats.db")
KEEPALIVE_URL        = RENDER_URL
KEEPALIVE_INTERVAL   = int(os.getenv("KEEPALIVE_INTERVAL", "180"))
POLL_COMMANDS        = os.getenv("POLL_COMMANDS", "true").lower()=="true"
POLL_INTERVAL        = int(os.getenv("POLL_INTERVAL", "10"))

APP_VERSION          = f"7.0.0-SmartVWAP ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')})"

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise SystemExit("ENV مفقودة: TELEGRAM_TOKEN و CHAT_ID")

# ============================== UTILS ==============================
def unix_now() -> int:
    return int(datetime.now(timezone.utc).timestamp())

def clamp(x, a, b):
    return max(a, min(b, x))

def symbol_pretty(s: str) -> str:
    return s.replace(":USDT","").replace(":USDC","").replace(":BTC","").replace(":USD","")

def _f(x):
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
    return json.dumps({
        "keyboard":[[{"text":t} for t in r] for r in [
            ["الإحصائيات","تحليل متقدم"],
            ["الأسباب","آخر الإشارات"],
            ["المفتوحة","تصدير CSV"],
            ["تصدير تحليلي","تحديث القائمة"]
        ]],
        "resize_keyboard":True,
        "is_persistent":True
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
            print("TG err:",r)
            return None
        _last_send_ts=time.time()
        return r["result"]["message_id"]
    except Exception as e:
        print("TG:",e)
        return None

def tg_send_doc(filename:str, bytes_:bytes, caption:str="")->bool:
    try:
        r=requests.post(
            DOC_URL,
            data={"chat_id":CHAT_ID,"caption":caption},
            files={"document":(filename,io.BytesIO(bytes_))},
            timeout=60
        ).json()
        return bool(r.get("ok"))
    except Exception:
        return False

def tg_delete_webhook():
    try:
        requests.post(DEL_WH, data={"drop_pending_updates":False}, timeout=10)
    except Exception:
        pass

def parse_cmd(text:str)->Tuple[str,str]:
    t=(text or "").strip()
    if t.startswith("/"):
        parts=t.split(maxsplit=1)
        cmd=parts[0].lower()
        arg=parts[1].strip() if len(parts)>1 else ""
        if "@" in cmd:
            cmd=cmd.split("@",1)[0]
        return cmd,arg
    return t,""

async def tg_send_async(text: str, reply_to: Optional[int]=None, reply_markup: Optional[str]=None) -> Optional[int]:
    return await asyncio.to_thread(tg_send, text, reply_to, reply_markup)

async def tg_send_doc_async(filename: str, bytes_: bytes, caption: str="") -> bool:
    return await asyncio.to_thread(tg_send_doc, filename, bytes_, caption)

async def poll_commands(handler: Callable[[str,str], asyncio.Future]):
    if not POLL_COMMANDS:
        return
    await asyncio.to_thread(tg_delete_webhook)
    global TG_OFFSET
    while True:
        try:
            r=(await asyncio.to_thread(
                requests.get,
                GET_UPD,
                params={"timeout":25,"offset":TG_OFFSET+1},
                timeout=35
            )).json()
            if r.get("ok"):
                for upd in r.get("result",[]):
                    TG_OFFSET=max(TG_OFFSET,upd["update_id"])
                    msg=upd.get("message") or upd.get("edited_message")
                    if msg and str(msg.get("chat",{}).get("id"))==str(CHAT_ID):
                        cmd,arg=parse_cmd(msg.get("text",""))
                        await handler(cmd,arg)
        except Exception as e:
            print("[poll]",e)
        await asyncio.sleep(POLL_INTERVAL)

# ============================== DB ==============================
def db_conn():
    c=sqlite3.connect(LOG_DB_PATH)
    c.execute("PRAGMA journal_mode=WAL;")
    return c

def db_init():
    c=db_conn()
    c.execute("""CREATE TABLE IF NOT EXISTS signals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER,exchange TEXT,symbol TEXT,side TEXT,
        entry REAL,sl REAL,tp1 REAL,tp2 REAL,tp3 REAL,tp4 REAL,
        confidence INTEGER,msg_id INTEGER,
        strategy TEXT DEFAULT 'smart_vwap'
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS outcomes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER,ts INTEGER,event TEXT,idx INTEGER,price REAL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS nosignal_reasons(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER,exchange TEXT,symbol TEXT,reasons TEXT,
        strategy TEXT DEFAULT 'smart_vwap'
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS errors(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER,exchange TEXT,symbol TEXT,message TEXT
    )""")
    c.commit()
    c.close()

def db_insert_signal(ts,ex,sym,side,entry,sl,tps,conf,mid)->int:
    c=db_conn()
    cur=c.cursor()
    cur.execute(
        "INSERT INTO signals(ts,exchange,symbol,side,entry,sl,tp1,tp2,tp3,tp4,confidence,msg_id)"
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts,ex,sym,side,entry,sl,tps[0],tps[1],tps[2],tps[3],conf,mid)
    )
    c.commit()
    sid=cur.lastrowid
    c.close()
    return sid

def db_insert_outcome(sid,ts,ev,idx,pr):
    c=db_conn()
    c.execute(
        "INSERT INTO outcomes(signal_id,ts,event,idx,price)VALUES(?,?,?,?,?)",
        (sid,ts,ev,idx,pr)
    )
    c.commit()
    c.close()

def db_insert_nosignal(ts,ex,sym,rsn:Dict):
    c=db_conn()
    c.execute(
        "INSERT INTO nosignal_reasons(ts,exchange,symbol,reasons)VALUES(?,?,?,?)",
        (ts,ex,sym,json.dumps(rsn,ensure_ascii=False))
    )
    c.commit()
    c.close()

def db_insert_error(ts,ex,sym,msg):
    c=db_conn()
    c.execute(
        "INSERT INTO errors(ts,exchange,symbol,message)VALUES(?,?,?,?)",
        (ts,ex,sym,msg)
    )
    c.commit()
    c.close()

def db_text_stats(days:int=1)->str:
    try:
        c=db_conn()
        cur=c.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM signals WHERE ts>=strftime('%s','now',?)",
            (f"-{days} day",)
        )
        total=cur.fetchone()[0] or 0
        cur.execute(
            "SELECT SUM(CASE WHEN event LIKE 'TP%' THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN event='SL' THEN 1 ELSE 0 END)"
            "FROM outcomes WHERE ts>=strftime('%s','now',?)",
            (f"-{days} day",)
        )
        tp,sl=(cur.fetchone() or (0,0))
        c.close()
        if total==0:
            return "لا بيانات"
        win=(tp/(tp+sl)*100) if (tp+sl)>0 else 0
        return (
            f"إحصائيات ({days}ي):\n"
            f"━━━━━━━━━━━━━\n"
            f"إشارات:{total}\n"
            f"أهداف:{tp}|ستوب:{sl}\n"
            f"نجاح:{win:.1f}%"
        )
    except Exception as e:
        return f"خطأ:{e}"

def db_detailed_stats(days:int=7)->str:
    try:
        c=db_conn()
        cur=c.cursor()
        cur.execute(
            "SELECT COUNT(*),AVG(confidence)FROM signals "
            "WHERE ts>=strftime('%s','now',?)",
            (f"-{days} day",)
        )
        total,avg=(cur.fetchone())
        total=total or 0
        avg=avg or 0
        cur.execute(
            "SELECT side,COUNT(*)FROM signals "
            "WHERE ts>=strftime('%s','now',?)GROUP BY side",
            (f"-{days} day",)
        )
        sides=dict(cur.fetchall())
        cur.execute(
            "SELECT o.event,COUNT(*)FROM outcomes o "
            "JOIN signals s ON o.signal_id=s.id "
            "WHERE o.ts>=strftime('%s','now',?)GROUP BY o.event",
            (f"-{days} day",)
        )
        outs=dict(cur.fetchall())
        c.close()
        if total==0:
            return "لا بيانات"
        tp=sum(v for k,v in outs.items() if k.startswith("TP"))
        sl=outs.get("SL",0)
        win=(tp/(tp+sl)*100) if (tp+sl)>0 else 0
        txt=[f"تحليل ({days}ي)", "═"*20, f"إشارات:{total}", f"ثقة:{avg:.1f}%", "", "اتجاهات:"]
        for s,cnt in sides.items():
            txt.append(f"  {s}:{cnt}")
        txt.append("")
        txt.append(f"نجاح:{win:.1f}%")
        for k,v in sorted(outs.items()):
            txt.append(f"  {k}:{v}")
        return "\n".join(txt)
    except Exception as e:
        return f"خطأ:{e}"

def db_text_reasons(window:str="1d")->str:
    unit=window[-1].lower()
    num=int(''.join([ch for ch in window if ch.isdigit()]) or 1)
    sql=f"-{num} {'hour' if unit=='h' else 'day'}"
    try:
        c=db_conn()
        cur=c.cursor()
        cur.execute(
            "SELECT reasons FROM nosignal_reasons WHERE ts>=strftime('%s','now',?)",
            (sql,)
        )
        rows=cur.fetchall()
        c.close()
        if not rows:
            return "لا أسباب"
        from collections import Counter
        items=[]
        for (js,) in rows:
            try:
                d=json.loads(js) if isinstance(js,str) else {}
                if isinstance(d,dict):
                    for k,v in d.items():
                        if isinstance(v,(int,float)):
                            items.append(f"{k}={v}")
                        elif isinstance(v,bool):
                            items.append(k if v else f"!{k}")
                        else:
                            items.append(f"{k}:{v}")
            except Exception:
                pass
        cnt=Counter(items)
        txt=[f"أسباب ({window}):","━"*30]
        for i,(k,v) in enumerate(cnt.most_common(10),1):
            txt.append(f"{i}. {k}:{v}")
        return "\n".join(txt)
    except Exception as e:
        return f"خطأ:{e}"

def db_text_last(limit:int=10)->str:
    try:
        c=db_conn()
        cur=c.cursor()
        cur.execute(
            "SELECT s.symbol,s.side,s.confidence,"
            "(SELECT event FROM outcomes o WHERE o.signal_id=s.id ORDER BY o.ts LIMIT 1)"
            "FROM signals s ORDER BY s.id DESC LIMIT ?",
            (limit,)
        )
        rows=cur.fetchall()
        c.close()
        if not rows:
            return "لا إشارات"
        out=["آخر الإشارات:","━"*15]
        for r in rows:
            res=r[3] or "قيد التنفيذ"
            emoji="✅" if (res and res.startswith("TP")) else ("❌" if res=="SL" else "⏳")
            out.append(f"{emoji} {symbol_pretty(r[0])} {r[1]} {r[2]}%→{res}")
        return "\n".join(out)
    except Exception as e:
        return f"خطأ:{e}"

def export_csv_bytes(days:int=14)->bytes:
    c=db_conn()
    c.execute(
        "SELECT s.id,s.ts,s.exchange,s.symbol,s.side,s.entry,s.sl,s.tp1,s.tp2,s.tp3,s.tp4,"
        "s.confidence,"
        "(SELECT GROUP_CONCAT(event||':'||price,'|')FROM outcomes o WHERE o.signal_id=s.id)"
        "FROM signals s WHERE s.ts>=strftime('%s','now',?)ORDER BY s.id DESC",
        (f"-{days} day",)
    )
    rows=c.fetchall()
    c.close()
    out=io.StringIO()
    w=csv.writer(out)
    w.writerow([
        "id","ts","exchange","symbol","side","entry","sl",
        "tp1","tp2","tp3","tp4","confidence","outcomes"
    ])
    for r in rows:
        w.writerow(r)
    return out.getvalue().encode("utf-8")

def export_analysis_csv(days:int=14)->bytes:
    c=db_conn()
    c.execute(
        "SELECT s.id,datetime(s.ts,'unixepoch'),s.exchange,s.symbol,s.side,s.entry,s.sl,"
        "s.tp1,s.tp2,s.tp3,s.tp4,s.confidence,"
        "(s.entry-s.sl)/s.entry*100,"
        "(SELECT event FROM outcomes o WHERE o.signal_id=s.id ORDER BY o.ts LIMIT 1),"
        "(SELECT price FROM outcomes o WHERE o.signal_id=s.id ORDER BY o.ts LIMIT 1),"
        "CASE WHEN EXISTS(SELECT 1 FROM outcomes o WHERE o.signal_id=s.id AND o.event='SL')"
        "THEN'LOSS'WHEN EXISTS(SELECT 1 FROM outcomes o WHERE o.signal_id=s.id AND o.event LIKE'TP%')"
        "THEN'WIN'ELSE'OPEN'END "
        "FROM signals s WHERE s.ts>=strftime('%s','now',?)ORDER BY s.ts DESC",
        (f"-{days} day",)
    )
    rows=c.fetchall()
    c.close()
    out=io.StringIO()
    w=csv.writer(out)
    w.writerow([
        "ID","Time","Exchange","Symbol","Side","Entry","SL",
        "TP1","TP2","TP3","TP4","Conf","Risk%","Outcome","Price","Status"
    ])
    for r in rows:
        w.writerow(r)
    return out.getvalue().encode("utf-8")

# ============================== INDICATORS ==============================
def ema(s: pd.Series, n:int) -> pd.Series:
    return s.ewm(span=n,adjust=False).mean()

def sma(s: pd.Series, n:int) -> pd.Series:
    return s.rolling(n).mean()

def rsi(s: pd.Series, n:int=14) -> pd.Series:
    d=s.diff()
    up=d.clip(lower=0)
    dn=-d.clip(upper=0)
    mu=up.ewm(com=n-1,adjust=False).mean()
    md=dn.ewm(com=n-1,adjust=False).mean()
    return 100-(100/(1+mu/(md.replace(0,1e-12))))

def atr(df: pd.DataFrame, n:int=14) -> pd.Series:
    h,l,c=df["high"],df["low"],df["close"]
    tr=pd.concat([(h-l).abs(),(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/n,adjust=False).mean()

def adx(df: pd.DataFrame, n:int=14) -> pd.Series:
    h,l,c=df["high"],df["low"],df["close"]
    up=h.diff()
    dn=-l.diff()
    pdm=np.where((up>dn)&(up>0),up,0.0)
    mdm=np.where((dn>up)&(dn>0),dn,0.0)
    tr=pd.concat([(h-l).abs(),(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    av=tr.ewm(alpha=1/n,adjust=False).mean()
    pdi=(pd.Series(pdm,index=df.index).ewm(alpha=1/n,adjust=False).mean()/av.replace(0,1e-12))*100
    mdi=(pd.Series(mdm,index=df.index).ewm(alpha=1/n,adjust=False).mean()/av.replace(0,1e-12))*100
    return ((abs(pdi-mdi)/(pdi+mdi).replace(0,1e-12))*100).ewm(alpha=1/n,adjust=False).mean()

def vwap_anchored(df: pd.DataFrame) -> Tuple[pd.Series,pd.Series,pd.Series]:
    tp=(df["high"]+df["low"]+df["close"])/3
    vs=(tp*df["volume"]).cumsum()
    vols=df["volume"].cumsum()
    vw=vs/vols.replace(0,1e-12)
    var=(((tp-vw)**2)*df["volume"]).cumsum()/vols.replace(0,1e-12)
    return vw, np.sqrt(var), tp

# ============================== EXCHANGES ==============================
EXC={
    "bybit":ccxt.bybit,
    "okx":ccxt.okx,
    "kucoinfutures":ccxt.kucoinfutures,
    "bitget":ccxt.bitget,
    "gate":ccxt.gate,
    "binance":ccxt.binance
}

def make_exchange(name:str):
    return EXC.get(name,ccxt.okx)({
        "enableRateLimit":True,
        "timeout":20000,
        "options":{"defaultType":"swap","defaultSubType":"linear"}
    })

def load_markets_linear_only(ex):
    for i,b in enumerate([1.5,3,6],1):
        try:
            ex.load_markets(reload=True,params={"category":"linear","type":"swap"})
            return
        except Exception as e:
            print(f"[load {i}]",type(e).__name__)
            time.sleep(b)

def try_failover(primary:str)->Tuple[ccxt.Exchange,str]:
    last=None
    for name in [primary,"okx","bybit","bitget","kucoinfutures","gate","binance"]:
        try:
            ex=make_exchange(name)
            load_markets_linear_only(ex)
            print("✓",name)
            return ex,name
        except Exception as e:
            last=e
            print("[fail]",name,type(e).__name__)
    raise last or SystemExit("No exchange")

def normalize_symbols(ex, syms:List[str])->List[str]:
    if ex.id=="bybit":
        return [s + (":USDT" if s.endswith("/USDT") and ":USDT" not in s else "") for s in syms]
    return syms

def list_all_futures_symbols(ex)->List[str]:
    syms=[m["symbol"] for m in ex.markets.values()
          if m.get("contract") and (m.get("swap") or m.get("future")) and m.get("active",True) is not False]
    syms=sorted(set(syms))
    if MAX_SYMBOLS>0:
        syms=syms[:MAX_SYMBOLS]
    return normalize_symbols(ex, syms)

def parse_symbols(ex, val:str)->List[str]:
    key=(val or "").strip().upper()
    if key in ("ALL","AUTO","AUTO_FUTURES","AUTO_SWAP","AUTO_LINEAR"):
        return list_all_futures_symbols(ex)
    syms=[s.strip() for s in (val or "").split(",") if s.strip()]
    syms=normalize_symbols(ex, syms)
    if MAX_SYMBOLS>0:
        syms=syms[:MAX_SYMBOLS]
    return syms

async def fetch_ohlcv_safe(ex, symbol:str, timeframe:str, limit:int)->Optional[pd.DataFrame]:
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
            df=pd.DataFrame(ohlcv,columns=["ts","open","high","low","close","volume"])
            if df["close"].isna().any() or (df["close"]==0).any():
                if attempt==0:
                    await asyncio.sleep(1)
                    continue
                return None
            df["ts"]=pd.to_datetime(df["ts"],unit="ms",utc=True)
            df.set_index("ts",inplace=True)
            return df
        except Exception:
            if attempt==0:
                await asyncio.sleep(1)
                continue
            return None
    return None

async def fetch_ticker_price(ex, symbol:str)->Optional[float]:
    try:
        t=await asyncio.to_thread(ex.fetch_ticker, symbol)
        v=t.get("last") or t.get("close") or t.get("info",{}).get("lastPrice")
        return float(v) if v is not None else None
    except Exception:
        return None

# ============================== STRATEGY الجديدة ==============================
def detect_regime(df:pd.DataFrame, adx_now:float)->str:
    """🎯 كشف حالة السوق: strong_trend / weak_trend / ranging"""
    c=df["close"]
    e50=ema(c,50)
    e200=ema(c,200)
    diff_pct=abs(e50.iloc[-2]-e200.iloc[-2])/e200.iloc[-2]*100
    if adx_now>=ADX_STRONG_TREND and diff_pct>=0.5:
        return "strong_trend"
    elif adx_now>=ADX_WEAK_TREND:
        return "weak_trend"
    else:
        return "ranging"

def calc_momentum(df:pd.DataFrame, i:int)->float:
    """🎯 حساب جودة الزخم: 0.0 (ضعيف) → 1.0 (قوي)"""
    c,v=df["close"],df["volume"]
    e20,e50=ema(c,20),ema(c,50)
    pm=(c.iloc[i]-e20.iloc[i])/e20.iloc[i]
    em=(e20.iloc[i]-e50.iloc[i])/e50.iloc[i]
    vr=v.iloc[i]/v.tail(20).mean()
    vs=clamp((vr-1.0)/0.5,0,1)
    return clamp(abs(pm)*30+abs(em)*50+vs*20,0,1)

def setup_trend_pullback(df:pd.DataFrame, i:int, vwap:float, vl1:float, vu1:float, regime:str)->Optional[Dict]:
    """🎯 نمط 1: تراجع الترند إلى دعم/مقاومة"""
    c,l,h,o=df["close"],df["low"],df["high"],df["open"]
    cn=c.iloc[i]
    e50=ema(c,50).iloc[i]
    e200=ema(c,200).iloc[i]
    if e50>e200:
        near_vwap = cn<=vwap*1.015 and cn>=vl1*0.98
        near_ema  = abs(cn-e50)/e50<0.02
        if (near_vwap or near_ema) and (cn>o.iloc[i] or cn>c.iloc[i-1]):
            return {"side":"LONG","setup":"trend_pullback","reason":"pullback_support"}
    elif e50<e200:
        near_vwap = cn>=vwap*0.985 and cn<=vu1*1.02
        near_ema  = abs(cn-e50)/e50<0.02
        if (near_vwap or near_ema) and (cn<o.iloc[i] or cn<c.iloc[i-1]):
            return {"side":"SHORT","setup":"trend_pullback","reason":"pullback_resist"}
    return None

def setup_range_reversal(df:pd.DataFrame, i:int, vl1:float, vu1:float, rsi_val:float)->Optional[Dict]:
    """🎯 نمط 2: انعكاس في الرينج عند الأطراف"""
    c,o=df["close"],df["open"]
    cn=c.iloc[i]
    if cn<=vl1*1.03 and rsi_val<38:
        if cn>o.iloc[i] or rsi_val<30:
            return {"side":"LONG","setup":"range_reversal","reason":"oversold_bounce"}
    if cn>=vu1*0.97 and rsi_val>62:
        if cn<o.iloc[i] or rsi_val>70:
            return {"side":"SHORT","setup":"range_reversal","reason":"overbought_reject"}
    return None

def setup_breakout(df:pd.DataFrame, i:int, vwap:float, vol_ratio:float, atr_val:float)->Optional[Dict]:
    """🎯 نمط 3: كسر VWAP مع حجم عالي"""
    if vol_ratio<MIN_VOLUME_SPIKE:
        return None
    c,h,l=df["close"],df["high"],df["low"]
    cn=c.iloc[i]
    recent_range=(c.tail(10).max()-c.tail(10).min())/c.iloc[i-10]
    if recent_range<0.015:
        if cn>vwap*1.005 and l.iloc[i]<=vwap:
            return {"side":"LONG","setup":"breakout","reason":"vwap_break_up"}
        if cn<vwap*0.995 and h.iloc[i]>=vwap:
            return {"side":"SHORT","setup":"breakout","reason":"vwap_break_down"}
    return None

def setup_momentum_cont(df:pd.DataFrame, i:int, vwap:float, mom_score:float, regime:str)->Optional[Dict]:
    """🎯 نمط 4: استمرار الزخم القوي"""
    if regime!="strong_trend" or mom_score<0.6:
        return None
    c,v=df["close"],df["volume"]
    cn=c.iloc[i]
    dist=(cn-vwap)/vwap
    if 0.005<dist<0.025 and v.iloc[i]>v.tail(10).mean():
        return {"side":"LONG","setup":"momentum_cont","reason":"strong_up_momentum"}
    elif -0.025<dist<-0.005 and v.iloc[i]>v.tail(10).mean():
        return {"side":"SHORT","setup":"momentum_cont","reason":"strong_down_momentum"}
    return None

def setup_ema_cross(df:pd.DataFrame, i:int, rsi_val:float)->Optional[Dict]:
    """🎯 نمط 5: تقاطع EMA مع تأكيد"""
    c=df["close"]
    e20,e50=ema(c,20),ema(c,50)
    cross=(e20.iloc[i-1]-e50.iloc[i-1])*(e20.iloc[i-3]-e50.iloc[i-3])
    if cross<=0:
        cn,e20n,e50n=c.iloc[i],e20.iloc[i],e50.iloc[i]
        if e20n>e50n and 35<rsi_val<65 and cn>e20n:
            return {"side":"LONG","setup":"ema_cross","reason":"bullish_ema_cross"}
        elif e20n<e50n and 35<rsi_val<65 and cn<e20n:
            return {"side":"SHORT","setup":"ema_cross","reason":"bearish_ema_cross"}
    return None

def calc_sl(df:pd.DataFrame, side:str, entry:float, atr_val:float,
            vwap:float, vl1:float, vu1:float)->float:
    """🎯 ستوب ديناميكي حسب هيكل السوق"""
    h,l=df["high"],df["low"]
    if side=="LONG":
        rlow=float(l.tail(15).min())
        struct=min(rlow,vl1*0.998)
        atr_sl=entry-ATR_SL_MULT*atr_val
        sl=max(struct,atr_sl)
    else:
        rhigh=float(h.tail(15).max())
        struct=max(rhigh,vu1*1.002)
        atr_sl=entry+ATR_SL_MULT*atr_val
        sl=min(struct,atr_sl)
    gap=abs(entry-sl)
    mingap=entry*(MIN_SL_PCT/100)
    maxgap=entry*(MAX_SL_PCT/100)
    if gap<mingap:
        sl=entry-mingap if side=="LONG" else entry+mingap
    if gap>maxgap:
        sl=entry-maxgap if side=="LONG" else entry+maxgap
    return float(sl)

def calc_tps(side:str, entry:float)->List[float]:
    return [entry*(1+p/100) for p in TP_PCTS] if side=="LONG" else [entry*(1-p/100) for p in TP_PCTS]

def calc_confidence(setup:Dict, regime:str, vol_ratio:float, rsi_val:float,
                    rr:float, mom:float, adx_val:float)->int:
    """🎯 حساب الثقة الديناميكي"""
    score = {
        "trend_pullback":0.28,
        "range_reversal":0.24,
        "breakout":0.30,
        "momentum_cont":0.32,
        "ema_cross":0.26
    }.get(setup["setup"],0.25)
    score += {
        "strong_trend":0.20,
        "weak_trend":0.15,
        "ranging":0.10
    }.get(regime,0.10)
    score += 0.15*clamp((vol_ratio-1.0)/1.0,0,1)
    score += 0.15*mom
    score += 0.10*clamp((rr-1.0)/1.5,0,1)
    score += 0.05*(1.0-abs(rsi_val-50)/50.0)
    score += 0.05*clamp(adx_val/35.0,0,1)
    return int(round(clamp(score*100,0,100)))

async def analyze_signal(ex, symbol:str, df:pd.DataFrame)->Tuple[Optional[Dict],Dict]:
    """🎯 محلل الإشارات متعدد الأنماط"""
    if df is None or len(df)<100:
        return None, {"insufficient_data":True}
    c,h,l,v,o=df["close"],df["high"],df["low"],df["volume"],df["open"]
    atr14=atr(df,14)
    adx14=adx(df,14)
    rsi14=rsi(c,14)
    vwap_s,vstd,tp=vwap_anchored(df)
    vu1=vwap_s+VWAP_STD_MULT_1*vstd
    vl1=vwap_s-VWAP_STD_MULT_1*vstd
    i=-2
    try:
        cn=_f(c.iloc[i])
        atrn=_f(atr14.iloc[i])
        adxn=float(adx14.iloc[i])
        rsin=float(rsi14.iloc[i])
        vwn=float(vwap_s.iloc[i])
        vu1n=float(vu1.iloc[i])
        vl1n=float(vl1.iloc[i])
    except Exception as e:
        return None, {"data_error":str(e)[:50]}
    avg_usdt=float((v*c).tail(30).mean())
    if avg_usdt<MIN_AVG_VOL_USDT:
        return None, {"low_liquidity":int(avg_usdt)}
    atr_pct=100*atrn/max(cn,1e-9)
    if atr_pct<MIN_ATR_PCT:
        return None, {"atr_low":round(atr_pct,3)}
    body=abs(cn-o.iloc[i])
    if body>MAX_BODY_ATR_RATIO*atrn:
        return None, {"extended":True}
    regime=detect_regime(df, adxn)
    vol_ratio=float(v.iloc[i]/max(v.tail(30).mean(),1e-9))
    mom=calc_momentum(df, i)
    setup=None
    if regime in ["strong_trend","weak_trend"]:
        setup=setup_trend_pullback(df,i,vwn,vl1n,vu1n,regime)
    if not setup and regime=="ranging":
        setup=setup_range_reversal(df,i,vl1n,vu1n,rsin)
    if not setup:
        setup=setup_breakout(df,i,vwn,vol_ratio,atrn)
    if not setup and regime=="strong_trend":
        setup=setup_momentum_cont(df,i,vwn,mom,regime)
    if not setup:
        setup=setup_ema_cross(df,i,rsin)
    if not setup:
        return None, {"no_setup":"no_match"}
    side=setup["side"]
    entry=float(cn)
    sl=calc_sl(df, side, entry, atrn, vwn, vl1n, vu1n)
    tps=[float(x) for x in calc_tps(side,entry)]
    risk=abs(entry-sl)
    reward=abs(tps[2]-entry)
    rr=reward/risk if risk>0 else 0
    if rr<MIN_RR_RATIO:
        return None, {"rr_low":round(rr,2)}
    conf=calc_confidence(setup,regime,vol_ratio,rsin,rr,mom,adxn)
    if conf<MIN_CONFIDENCE:
        return None, {"conf_low":conf}
    return {
        "side":side,
        "entry":entry,
        "sl":float(sl),
        "tps":tps,
        "confidence":int(conf),
        "rr":round(rr,2),
        "vol":round(vol_ratio,2),
        "adx":round(adxn,1),
        "regime":regime,
        "setup":setup["setup"],
        "reason":setup["reason"],
        "mom":round(mom,2)
    }, {}

# ============================== ENGINE ==============================
open_trades: Dict[str,Dict]={}
signal_state: Dict[str,Dict]={}
_last_cycle_alerts=0

async def crossed_levels(side:str, price:float, tps:List[float], sl:float, hit:List[bool]):
    if price is None:
        return None
    if side=="LONG" and price<=sl:
        return ("SL",-1)
    if side=="SHORT" and price>=sl:
        return ("SL",-1)
    for idx,(tp,ok) in enumerate(zip(tps,hit)):
        if ok:
            continue
        if side=="LONG" and price>=tp:
            return ("TP",idx)
        if side=="SHORT" and price<=tp:
            return ("TP",idx)
    return None

def pct_profit(side:str, entry:float, exit_price:float)->float:
    return (exit_price/entry-1)*100.0 if side=="LONG" else (1-exit_price/entry)*100.0

def elapsed_text(start:int, end:int)->str:
    mins=max(0,end-start)//60
    return f"{mins}د" if mins<60 else f"{mins//60}س {mins%60}د"

async def fetch_and_signal(ex, symbol:str):
    global _last_cycle_alerts
    df=await fetch_ohlcv_safe(ex, symbol, TIMEFRAME, 400)
    if df is None or len(df)<100:
        return
    st=signal_state.get(symbol,{})
    closed_idx=len(df)-2
    if closed_idx<st.get("cooldown_until_idx",-999999):
        return
    if symbol in open_trades:
        return
    try:
        sig,reasons=await analyze_signal(ex, symbol, df)
    except Exception as e:
        db_insert_error(unix_now(), ex.id, symbol, f"analyze:{type(e).__name__}")
        return
    if not sig:
        db_insert_nosignal(unix_now(), ex.id, symbol, reasons or {})
        return
    if _last_cycle_alerts>=MAX_ALERTS_PER_CYCLE:
        return
    side_txt="شراء" if sig["side"]=="LONG" else "بيع"
    side_emoji="🟢" if sig["side"]=="LONG" else "🔴"
    entry,sl,tps,conf,rr = sig["entry"],sig["sl"],sig["tps"],sig["confidence"],sig["rr"]
    if conf>=75:
        tier,emoji="💎 Premium","🔥🔥🔥"
    elif conf>=68:
        tier,emoji="⭐ High","🔥🔥"
    elif conf>=62:
        tier,emoji="✨ Good","🔥"
    else:
        tier,emoji="📊 Standard","⭐"
    pretty=symbol_pretty(symbol)
    regime_ar={
        "strong_trend":"ترند قوي",
        "weak_trend":"ترند ضعيف",
        "ranging":"رينج"
    }.get(sig["regime"],"غير محدد")
    setup_ar={
        "trend_pullback":"تراجع ترند",
        "range_reversal":"انعكاس رينج",
        "breakout":"كسر",
        "momentum_cont":"استمرار زخم",
        "ema_cross":"تقاطع EMA"
    }.get(sig["setup"],"غير محدد")
    msg = (
        f"{side_emoji} {side_txt} - #{pretty}\n"
        f"━━━━━━━━━━━━━━━━━\n\n"
        f"ثقة: {conf}% {emoji}\n"
        f"فئة: {tier}\n"
        f"نمط: {setup_ar}\n"
        f"نظام: {regime_ar} | ADX: {sig['adx']}\n"
        f"R/R: 1:{rr} | زخم: {sig['mom']:.2f}\n\n"
        f"دخول: {entry:.6f}\n"
        f"ستوب: {sl:.6f}\n\n"
        f"أهداف:\n"
        f"  TP1: {tps[0]:.6f} ({TP_PCTS[0]:.1f}%)\n"
        f"  TP2: {tps[1]:.6f} ({TP_PCTS[1]:.1f}%)\n"
        f"  TP3: {tps[2]:.6f} ({TP_PCTS[2]:.1f}%)\n"
        f"  TP4: {tps[3]:.6f} ({TP_PCTS[3]:.1f}%)\n\n"
        f"حجم: {sig['vol']:.1f}x"
    )
    mid=await tg_send_async(msg)
    if mid:
        ts=unix_now()
        open_trades[symbol]={
            "side":sig["side"],
            "entry":entry,
            "sl":sl,
            "tps":tps,
            "hit":[False]*4,
            "msg_id":mid,
            "signal_id":None,
            "opened_ts":ts
        }
        signal_state[symbol]={"cooldown_until_idx":closed_idx+COOLDOWN_PER_SYMBOL_CANDLES}
        sid=db_insert_signal(ts, ex.id, symbol, sig["side"], entry, sl, tps, conf, mid)
        open_trades[symbol]["signal_id"]=sid
        _last_cycle_alerts+=1

async def check_open_trades(ex):
    for sym,pos in list(open_trades.items()):
        price=await fetch_ticker_price(ex, sym)
        res=await crossed_levels(pos["side"], price, pos["tps"], pos["sl"], pos["hit"])
        if not res:
            continue
        kind,idx=res
        ts=unix_now()
        if kind=="SL":
            pr=pct_profit(pos["side"], pos["entry"], price or pos["sl"])
            await tg_send_async(
                f"❌ #{symbol_pretty(sym)}\n"
                f"━━━━━━━━━━━━━\n"
                f"ستوب لوس\n\n"
                f"خسارة: {round(pr,2)}%\n"
                f"مدة: {elapsed_text(pos['opened_ts'], ts)}"
            )
            if pos.get("signal_id"):
                db_insert_outcome(pos["signal_id"], ts, "SL", -1, price or 0.0)
            del open_trades[sym]
        else:
            pos["hit"][idx]=True
            tp_val=pos["tps"][idx]
            pr=pct_profit(pos["side"], pos["entry"], tp_val if price is None else price)
            emoji=["✅","⭐","🔥","💎"][idx] if idx<4 else "✅"
            await tg_send_async(
                f"{emoji} #{symbol_pretty(sym)}\n"
                f"━━━━━━━━━━━━━\n"
                f"هدف {idx+1}\n\n"
                f"ربح: +{round(pr,2)}%\n"
                f"مدة: {elapsed_text(pos['opened_ts'], ts)}"
            )
            if pos.get("signal_id"):
                db_insert_outcome(pos["signal_id"], ts, f"TP{idx+1}", idx, price or tp_val)
            if all(pos["hit"]):
                del open_trades[sym]

async def scan_cycle(ex, symbols:List[str]):
    global _last_cycle_alerts
    _last_cycle_alerts=0
    await check_open_trades(ex)
    if not symbols:
        return
    random.shuffle(symbols)
    sem=asyncio.Semaphore(10)
    async def worker(s):
        async with sem:
            await fetch_and_signal(ex, s)
    await asyncio.gather(*[asyncio.create_task(worker(s)) for s in symbols], return_exceptions=True)

# ============================== FASTAPI ==============================
app = FastAPI(title="Smart VWAP Signal Engine", version="7.0.0")
app.state.exchange     = None
app.state.exchange_id  = EXCHANGE_NAME
app.state.symbols      = []
app.state.cycle_count  = 0
app.state.last_scan_time = 0
app.state.start_time   = unix_now()

@app.get("/")
@app.head("/")
async def root():
    return {
        "ok":True,
        "status":"running",
        "version":APP_VERSION,
        "exchange":app.state.exchange_id,
        "timeframe":TIMEFRAME,
        "symbols":len(app.state.symbols),
        "open_trades":len(open_trades),
        "uptime":unix_now()-app.state.start_time
    }

@app.get("/health")
@app.head("/health")
async def health():
    return {"status":"healthy","ts":unix_now()}

@app.get("/stats")
def stats():
    return {
        "open_trades":len(open_trades),
        "cycle_count":app.state.cycle_count,
        "symbols_count":len(app.state.symbols),
        "exchange":app.state.exchange_id,
        "last_scan":app.state.last_scan_time
    }

async def handle_command(cmd:str, arg:str):
    if cmd in ("/start","تحديث القائمة"):
        await tg_send_async("القائمة الرئيسية:", reply_markup=tg_menu_markup())
    elif cmd in ("الإحصائيات","/stats"):
        days=int(arg) if arg.isdigit() else 1
        await tg_send_async(db_text_stats(days))
    elif cmd in ("تحليل متقدم","/analysis"):
        days=int(arg) if arg.isdigit() else 7
        await tg_send_async(db_detailed_stats(days))
    elif cmd in ("الأسباب","/reasons"):
        await tg_send_async(db_text_reasons(arg or "1d"))
    elif cmd in ("آخر الإشارات","/last"):
        limit=int(arg) if arg.isdigit() else 10
        await tg_send_async(db_text_last(limit))
    elif cmd in ("المفتوحة","/open"):
        if not open_trades:
            await tg_send_async("لا صفقات مفتوحة")
        else:
            txt=["صفقات مفتوحة:","━"*15]
            for s,p in open_trades.items():
                txt.append(f"{symbol_pretty(s)} {p['side']} | {sum(p['hit'])}/4")
            await tg_send_async("\n".join(txt))
    elif cmd in ("تصدير CSV","/export"):
        days=int(arg) if arg.isdigit() else 14
        await tg_send_doc_async("signals.csv", export_csv_bytes(days))
    elif cmd in ("تصدير تحليلي","/export_analysis"):
        days=int(arg) if arg.isdigit() else 14
        await tg_send_doc_async("analysis.csv", export_analysis_csv(days))
    elif cmd == "/version":
        await tg_send_async(APP_VERSION)
    else:
        await tg_send_async("القائمة الرئيسية:", reply_markup=tg_menu_markup())

async def keepalive_task():
    if not KEEPALIVE_URL:
        return
    url = KEEPALIVE_URL.rstrip("/") + "/health"
    print(f"[Keepalive] {url}")
    while True:
        try:
            resp = await asyncio.to_thread(requests.head, url, timeout=6)
            print("[Keepalive]", resp.status_code)
        except Exception as e:
            print("[Keepalive err]", e)
        await asyncio.sleep(KEEPALIVE_INTERVAL + random.randint(-10, 10))

def attempt_build():
    ex, used = try_failover(EXCHANGE_NAME)
    syms = parse_symbols(ex, SYMBOLS_VAL)
    app.state.exchange   = ex
    app.state.exchange_id= used
    app.state.symbols    = syms

@app.on_event("startup")
async def startup():
    db_init()
    await tg_send_async(
        f"بوت {APP_VERSION}\n━━━━━━━━━━━━━\n"
        f"جاري التحميل...\n\n"
        f"✨ Smart VWAP Multi-Setup\n"
        f"• ثقة دنيا: {MIN_CONFIDENCE}%\n"
        f"• ATR دنيا: {MIN_ATR_PCT}%\n"
        f"• R/R دنيا: {MIN_RR_RATIO}:1",
        reply_markup=tg_menu_markup()
    )
    attempt_build()
    syms = app.state.symbols
    ex_id= app.state.exchange_id
    preview=", ".join([symbol_pretty(s) for s in syms[:8]])
    more=f"... +{len(syms)-8}" if len(syms)>8 else ""
    await tg_send_async(
        f"✅ اكتمل!\n"
        f"━━━━━━━━━━━━━\n"
        f"{ex_id} | {TIMEFRAME}\n"
        f"أزواج: {len(syms)}\n"
        f"Cooldown: {COOLDOWN_PER_SYMBOL_CANDLES} شمعة\n"
        f"━━━━━━━━━━━━━\n"
        f"{preview}{more}"
    )
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
            db_insert_error(
                unix_now(),
                app.state.exchange_id,
                "ALL",
                f"runner:{type(e).__name__}:{str(e)[:80]}"
            )
            print("[Runner]", e)
        await asyncio.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    port=int(os.getenv("PORT","10000"))
    print(f"Starting Smart VWAP Signal Engine on port {port} …")
    uvicorn.run(app, host="0.0.0.0", port=port)
