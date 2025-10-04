# trading_bot_smart_1_2.py
# - Smart Momentum + Volatility Compression Breakout
# - SYMBOLS=ALL / AUTO_FUTURES: حمّل كل عقود swap/linear تلقائيًا
# - Failover بين البورصات
# - Telegram: إشارات + ردود TP/SL
# - SQLite logging كامل + أوامر Telegram: /stats /reasons /last /open /export

import os, json, asyncio, time, traceback, io, csv, sqlite3
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
import ccxt
from fastapi import FastAPI
import uvicorn

# ============================ ENV ============================
EXCHANGE_NAME = os.getenv("EXCHANGE", "bybit").lower()
TIMEFRAME     = os.getenv("TIMEFRAME", "5m")
SYMBOLS_ENV   = os.getenv("SYMBOLS", "ALL")   # ALL / AUTO_FUTURES / list
MAX_SYMBOLS   = int(os.getenv("MAX_SYMBOLS", "60"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
OHLCV_LIMIT   = int(os.getenv("OHLCV_LIMIT", "300"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()

HTTP_PROXY     = os.getenv("HTTP_PROXY") or None
HTTPS_PROXY    = os.getenv("HTTPS_PROXY") or None

# Strategy params
COOLDOWN_CANDLES     = int(os.getenv("COOLDOWN_CANDLES", "3"))
MIN_ENTRY_CHANGE_PCT = float(os.getenv("MIN_ENTRY_CHANGE_PCT", "0.15"))
BB_BANDWIDTH_MAX     = float(os.getenv("BB_BANDWIDTH_MAX", "0.04"))
ATR_SL_MULT          = float(os.getenv("ATR_SL_MULT", "1.2"))
TP_PCTS              = [float(x) for x in (os.getenv("TP_PCTS", "0.25,0.5,1.0,1.5")).split(",")]
DEFAULT_LEVERAGE     = int(os.getenv("DEFAULT_LEVERAGE", "10"))

# Logging & commands
LOG_DB_PATH          = os.getenv("LOG_DB_PATH", "bot_stats.db")
SEND_NO_SIGNAL_SUMMARY = os.getenv("SEND_NO_SIGNAL_SUMMARY", "false").lower() == "true"
POLL_COMMANDS        = os.getenv("POLL_COMMANDS", "true").lower() == "true"
POLL_INTERVAL        = int(os.getenv("POLL_INTERVAL", "10"))

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise SystemExit("TELEGRAM_TOKEN و CHAT_ID مطلوبتان.")

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
SEND_URL = TG_API + "/sendMessage"
DOC_URL  = TG_API + "/sendDocument"
GET_UPDATES_URL = TG_API + "/getUpdates"

# ============================ Telegram ============================
def send_telegram(text: str, reply_to_message_id: Optional[int] = None) -> Optional[int]:
    try:
        resp = requests.post(
            SEND_URL,
            data={
                "chat_id": CHAT_ID,
                "text": text,
                "disable_web_page_preview": True,
                **({"reply_to_message_id": reply_to_message_id} if reply_to_message_id else {})
            },
            timeout=25
        )
        data = resp.json()
        if not data.get("ok"):
            print(f"Telegram error: {data}")
            return None
        return data["result"]["message_id"]
    except Exception as e:
        print(f"Telegram send error: {type(e).__name__}: {e}")
        return None

def send_document(filename: str, file_bytes: bytes, caption: str = "") -> bool:
    try:
        files = {"document": (filename, io.BytesIO(file_bytes))}
        data = {"chat_id": CHAT_ID, "caption": caption}
        r = requests.post(DOC_URL, data=data, files=files, timeout=60)
        j = r.json()
        if not j.get("ok"):
            print("send_document error:", j)
            return False
        return True
    except Exception as e:
        print("send_document exception:", e)
        return False

# ============================ Indicators ============================
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def rsi(s: pd.Series, n=14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    ma_up = up.ewm(com=n-1, adjust=False).mean()
    ma_dn = dn.ewm(com=n-1, adjust=False).mean()
    rs = ma_up / (ma_dn.replace(0, 1e-12))
    return 100 - (100 / (1 + rs))

def macd(s: pd.Series, fast=12, slow=26, signal_n=9) -> Tuple[pd.Series, pd.Series]:
    ef = ema(s, fast); es = ema(s, slow)
    line = ef - es
    sig = ema(line, signal_n)
    return line, sig

def bollinger(s: pd.Series, n=20, k=2.0):
    ma = s.rolling(n).mean()
    sd = s.rolling(n).std(ddof=0)
    upper = ma + k * sd
    lower = ma - k * sd
    bandwidth = (upper - lower) / ma
    return ma, upper, lower, bandwidth

def atr(df: pd.DataFrame, n=14):
    h,l,c = df["high"], df["low"], df["close"]
    tr = pd.concat([(h-l).abs(), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def pct_diff(a: float, b: float) -> float:
    return abs(a - b) / max(b, 1e-12) * 100.0

# ============================ CCXT / Exchanges ============================
EXCHANGE_CLASS_MAP = {
    "bybit": ccxt.bybit,
    "okx": ccxt.okx,
    "kucoinfutures": ccxt.kucoinfutures,
    "bitget": ccxt.bitget,
    "gate": ccxt.gate,
    "binance": ccxt.binance,
    "krakenfutures": ccxt.krakenfutures,
}

def make_exchange(name: str):
    klass = EXCHANGE_CLASS_MAP.get(name, ccxt.bybit)
    cfg = {
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "swap", "defaultSubType": "linear"},
    }
    if HTTP_PROXY or HTTPS_PROXY:
        cfg["proxies"] = {"http": HTTP_PROXY, "https": HTTPS_PROXY}
    return klass(cfg)

def load_markets_linear_only(ex):
    backoffs = [1.5, 3.0, 6.0]
    last = None
    for i, b in enumerate(backoffs, 1):
        try:
            ex.load_markets(reload=True, params={"category": "linear", "type": "swap"})
            return
        except Exception as e:
            print(f"[load_markets attempt {i}] {type(e).__name__}: {str(e)[:200]}")
            last = e
            if "403" in str(e) or "451" in str(e) or "Forbidden" in str(e):
                break
            time.sleep(b)
    raise last

def try_build_exchange_with_failover(primary: str, cands: List[str]) -> Tuple[ccxt.Exchange, str]:
    order = [primary] + [c for c in cands if c != primary]
    last = None
    for name in order:
        try:
            ex = make_exchange(name)
            load_markets_linear_only(ex)
            print(f"[failover] using {name}")
            return ex, name
        except Exception as e:
            print(f"[failover] {name} failed: {type(e).__name__}: {str(e)[:200]}")
            last = e
    raise last or Exception("No exchanges available")

# ============================ Symbols ============================
def normalize_symbols_for_exchange(ex, syms: List[str]) -> List[str]:
    if ex.id == "bybit":
        out = []
        for s in syms:
            if s.endswith("/USDT") and ":USDT" not in s:
                out.append(s + ":USDT")
            else:
                out.append(s)
        return out
    return syms

def list_all_futures_symbols(ex) -> List[str]:
    syms = []
    for m in ex.markets.values():
        if m.get("contract") and (m.get("future") or m.get("swap")) and m.get("active", True) is not False:
            syms.append(m["symbol"])
    syms = sorted(set(syms))
    if MAX_SYMBOLS and MAX_SYMBOLS > 0:
        syms = syms[:MAX_SYMBOLS]
    return normalize_symbols_for_exchange(ex, syms)

def parse_symbols_from_env(ex, env_value: str) -> List[str]:
    key = (env_value or "").strip().upper()
    if key in ("ALL", "AUTO_FUTURES", "AUTO", "AUTO_SWAP", "AUTO_LINEAR"):
        return list_all_futures_symbols(ex)
    syms = [s.strip() for s in (env_value or "").split(",") if s.strip()]
    syms = normalize_symbols_for_exchange(ex, syms)
    if MAX_SYMBOLS and MAX_SYMBOLS > 0:
        syms = syms[:MAX_SYMBOLS]
    return syms

# ============================ Data Fetch ============================
async def fetch_ohlcv_safe(ex, symbol: str, timeframe: str, limit: int):
    try:
        params = {}
        if ex.id == "bybit":
            params = {"category": "linear"}
        elif ex.id == "okx":
            params = {"instType": "SWAP"}
        ohlcv = await asyncio.to_thread(ex.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit, params=params)
        if not ohlcv or len(ohlcv) < 30:
            return None
        df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.set_index("ts", inplace=True)
        return df
    except Exception as e:
        return f"خطأ المنصة: {ex.id} {type(e).__name__} {str(e)[:200]}"

async def fetch_ticker_price(ex, symbol: str) -> Optional[float]:
    try:
        t = await asyncio.to_thread(ex.fetch_ticker, symbol)
        return float(t.get("last") or t.get("close") or t.get("info", {}).get("lastPrice"))
    except Exception:
        return None

# ============================ Strategy ============================
def smart_breakout_strategy(df: pd.DataFrame) -> Tuple[Optional[Dict], Dict]:
    reasons = {}
    if df is None or len(df) < 60:
        reasons["insufficient_data"] = f"candles={0 if df is None else len(df)} (<60)"
        return None, reasons

    c = df["close"]; h = df["high"]; l = df["low"]
    ma, bb_up, bb_dn, bb_bw = bollinger(c, 20, 2.0)
    macd_line, macd_sig = macd(c, 12, 26, 9)
    r = rsi(c, 14)
    atr14 = atr(df, 14)

    i2, i1 = -3, -2
    try:
        c_prev, c_now = float(c.iloc[i2]), float(c.iloc[i1])
        up_prev, up_now = float(bb_up.iloc[i2]), float(bb_up.iloc[i1])
        dn_prev, dn_now = float(bb_dn.iloc[i2]), float(bb_dn.iloc[i1])
        bw_now = float(bb_bw.iloc[i1])
        macd_now = float(macd_line.iloc[i1]); sig_now = float(macd_sig.iloc[i1])
        r14 = float(r.iloc[i1])
        atr_now = float(atr14.iloc[i1])
    except Exception:
        reasons["index_error"] = True
        return None, reasons

    is_squeeze = bw_now <= BB_BANDWIDTH_MAX
    crossed_up   = (c_prev <= up_prev) and (c_now > up_now)
    crossed_down = (c_prev >= dn_prev) and (c_now < dn_now)

    long_ok  = is_squeeze and crossed_up   and (macd_now > sig_now) and (45 < r14 < 70)
    short_ok = is_squeeze and crossed_down and (macd_now < sig_now) and (30 < r14 < 55)

    if not (long_ok or short_ok):
        reasons.update({
            "squeeze": is_squeeze,
            "cross_up": crossed_up,
            "cross_down": crossed_down,
            "macd_vs_signal": f"{round(macd_now,4)} vs {round(sig_now,4)}",
            "rsi14": round(r14,2),
            "note": "no edge-triggered breakout on closed candle"
        })
        return None, reasons

    entry = c_now
    sl_dist = ATR_SL_MULT * max(atr_now, 1e-12)
    if long_ok:
        sl = entry - sl_dist
        tps = [entry * (1 + p/100.0) for p in TP_PCTS]
        side = "LONG"
    else:
        sl = entry + sl_dist
        tps = [entry * (1 - p/100.0) for p in TP_PCTS]
        side = "SHORT"

    return ({
        "side": side,
        "entry": float(entry),
        "sl": float(sl),
        "tps": [float(x) for x in tps],
        "candle_i1_ts": int(df.index[i1].value // 1e9)
    }, {})

def symbol_pretty(s: str) -> str:
    return s.replace(":USDT", "")

# ============================ State & Logging ============================
open_trades: Dict[str, Dict] = {}      # symbol -> {side, entry, sl, tps[], hit[], msg_id}
signal_state: Dict[str, Dict] = {}     # symbol -> {last_entry, last_side, last_candle_idx, cooldown_until_idx}

def db_conn():
    con = sqlite3.connect(LOG_DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    return con

def db_init():
    con = db_conn()
    con.execute("""
    CREATE TABLE IF NOT EXISTS signals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER,
        exchange TEXT,
        symbol TEXT,
        side TEXT,
        entry REAL,
        sl REAL,
        tp1 REAL, tp2 REAL, tp3 REAL, tp4 REAL,
        msg_id INTEGER
    );
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS outcomes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER,
        ts INTEGER,
        event TEXT,   -- TP1..TP4 or SL
        idx INTEGER,  -- 0..3 for TP, -1 for SL
        price REAL,
        FOREIGN KEY(signal_id) REFERENCES signals(id)
    );
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS nosignal_reasons(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER,
        exchange TEXT,
        symbol TEXT,
        reasons TEXT
    );
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS errors(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER,
        exchange TEXT,
        symbol TEXT,
        message TEXT
    );
    """)
    con.commit(); con.close()

def db_insert_signal(ts:int, exchange:str, symbol:str, side:str, entry:float, sl:float, tps:List[float], msg_id:int) -> int:
    con = db_conn()
    cur = con.cursor()
    cur.execute("INSERT INTO signals(ts,exchange,symbol,side,entry,sl,tp1,tp2,tp3,tp4,msg_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (ts, exchange, symbol, side, entry, sl, tps[0], tps[1], tps[2], tps[3], msg_id))
    con.commit()
    sid = cur.lastrowid
    con.close()
    return sid

def db_insert_outcome(signal_id:int, ts:int, event:str, idx:int, price:float):
    con = db_conn()
    con.execute("INSERT INTO outcomes(signal_id,ts,event,idx,price) VALUES(?,?,?,?,?)",
                (signal_id, ts, event, idx, price))
    con.commit(); con.close()

def db_insert_nosignal(ts:int, exchange:str, symbol:str, reasons:Dict):
    con = db_conn()
    con.execute("INSERT INTO nosignal_reasons(ts,exchange,symbol,reasons) VALUES(?,?,?,?)",
                (ts, exchange, symbol, json.dumps(reasons, ensure_ascii=False)))
    con.commit(); con.close()

def db_insert_error(ts:int, exchange:str, symbol:str, message:str):
    con = db_conn()
    con.execute("INSERT INTO errors(ts,exchange,symbol,message) VALUES(?,?,?,?)",
                (ts, exchange, symbol, message))
    con.commit(); con.close()

def unix_now() -> int:
    return int(datetime.now(timezone.utc).timestamp())

# ============================ TP/SL Checking ============================
def crossed_levels(side: str, price: float, tps: List[float], sl: float, hit: List[bool]) -> Optional[Tuple[str, int]]:
    if price is None: return None
    if side == "LONG" and price <= sl: return ("SL", -1)
    if side == "SHORT" and price >= sl: return ("SL", -1)
    for idx, (tp, was_hit) in enumerate(zip(tps, hit)):
        if was_hit: continue
        if side == "LONG" and price >= tp: return ("TP", idx)
        if side == "SHORT" and price <= tp: return ("TP", idx)
    return None

# ============================ FastAPI ============================
app = FastAPI()

@app.get("/")
def root():
    return {
        "ok": True,
        "exchange": getattr(app.state, "exchange_id", EXCHANGE_NAME),
        "timeframe": TIMEFRAME,
        "symbols_mode": SYMBOLS_ENV,
        "max_symbols": MAX_SYMBOLS,
        "scan_interval": SCAN_INTERVAL,
        "open_trades": len(open_trades),
        "symbols_count": len(getattr(app.state, "symbols", [])),
    }

# ============================ Core Scan ============================
async def fetch_and_signal(ex, symbol: str, timeframe: str, holder: Dict[str, Optional[int]]):
    out = await fetch_ohlcv_safe(ex, symbol, timeframe, OHLCV_LIMIT)
    if isinstance(out, str):
        db_insert_error(unix_now(), ex.id, symbol, out)
        return ("error", symbol, out)
    if out is None or len(out) < 60:
        db_insert_nosignal(unix_now(), ex.id, symbol, {"insufficient_data": True})
        return ("no_data", symbol, {"insufficient_data": True})

    if symbol in open_trades:
        return ("open_trade", symbol, {})

    sig, reasons = smart_breakout_strategy(out)
    if not sig:
        if reasons:
            db_insert_nosignal(unix_now(), ex.id, symbol, reasons)
        return ("no_signal", symbol, reasons or {"note": "no setup"})

    # Cooldown / dedupe
    st = signal_state.get(symbol, {})
    last_entry = st.get("last_entry")
    last_side  = st.get("last_side")
    last_idx   = st.get("last_candle_idx", -1)
    cooldown_until = st.get("cooldown_until_idx", -999999)
    closed_idx = len(out) - 2

    if closed_idx < cooldown_until:
        return ("cooldown", symbol, {"cooldown_until_idx": cooldown_until})

    if last_side == sig["side"] and last_entry is not None:
        if pct_diff(sig["entry"], float(last_entry)) < MIN_ENTRY_CHANGE_PCT:
            return ("near_dupe", symbol, {"last_entry": last_entry, "new_entry": sig["entry"]})

    # Compose message
    pretty = symbol_pretty(symbol)
    side_txt = "طويل 🟢" if sig["side"] == "LONG" else "قصير 🔴"
    entry, sl, tps = sig["entry"], sig["sl"], sig["tps"]
    lev = DEFAULT_LEVERAGE
    msg = (
        f"#{pretty} - {side_txt}\n\n"
        f"نقطة الدخول: {entry}\n"
        f"وقف الخسارة: {sl}\n\n"
        f"الهدف 1: {tps[0]}\n"
        f"الهدف 2: {tps[1]}\n"
        f"الهدف 3: {tps[2]}\n"
        f"الهدف 4: {tps[3]}\n\n"
        f"الرفع: x{lev}"
    )

    mid = send_telegram(msg, reply_to_message_id=holder.get("id"))
    if mid:
        open_trades[symbol] = {"side": sig["side"], "entry": entry, "sl": sl, "tps": tps, "hit": [False]*4, "msg_id": mid, "signal_id": None}
        signal_state[symbol] = {"last_entry": entry, "last_side": sig["side"], "last_candle_idx": closed_idx, "cooldown_until_idx": closed_idx + COOLDOWN_CANDLES}
        # سجل في DB
        sid = db_insert_signal(unix_now(), ex.id, symbol, sig["side"], entry, sl, tps, mid)
        open_trades[symbol]["signal_id"] = sid

    return ("signal", symbol, sig)

async def check_open_trades(ex, holder):
    for sym, pos in list(open_trades.items()):
        price = await fetch_ticker_price(ex, sym)
        res = crossed_levels(pos["side"], price, pos["tps"], pos["sl"], pos["hit"])
        if not res: continue
        kind, idx = res
        ts = unix_now()
        if kind == "SL":
            txt = (
                f"🛑 SL تحقق لـ {symbol_pretty(sym)}\n"
                f"نوع: {pos['side']}\n"
                f"دخول: {pos['entry']}\n"
                f"ستوب: {pos['sl']}\n"
                f"السعر الحالي: {price}"
            )
            send_telegram(txt, reply_to_message_id=pos["msg_id"])
            if pos.get("signal_id"): db_insert_outcome(pos["signal_id"], ts, "SL", -1, price or 0.0)
            del open_trades[sym]
        else:
            pos["hit"][idx] = True
            tp_price = pos["tps"][idx]
            txt = (
                f"🎯 TP{idx+1} تحقق لـ {symbol_pretty(sym)}\n"
                f"نوع: {pos['side']}\n"
                f"دخول: {pos['entry']}\n"
                f"الهدف {idx+1}: {tp_price}\n"
                f"ستوب: {pos['sl']}\n"
                f"السعر الحالي: {price}"
            )
            send_telegram(txt, reply_to_message_id=pos["msg_id"])
            if pos.get("signal_id"): db_insert_outcome(pos["signal_id"], ts, f"TP{idx+1}", idx, price or tp_price)
            if all(pos["hit"]): del open_trades[sym]

async def scan_once(ex, symbols: List[str], holder):
    await check_open_trades(ex, holder)
    if not symbols: return
    sem = asyncio.Semaphore(2)
    async def worker(sym):
        async with sem:
            await fetch_and_signal(ex, sym, TIMEFRAME, holder)
    await asyncio.gather(*[asyncio.create_task(worker(s)) for s in symbols])
    if SEND_NO_SIGNAL_SUMMARY:
        send_telegram("> ℹ️ لا توجد إشارات في هذه الدورة. (تُسجل الأسباب في القاعدة)", reply_to_message_id=holder.get("id"))

# ============================ Telegram Commands ============================
last_update_offset = {"offset": 0}

def parse_cmd(text: str) -> Tuple[str, List[str]]:
    parts = (text or "").strip().split()
    if not parts: return "", []
    return parts[0].lower(), parts[1:]

def db_fetch_stats(days: Optional[int]):
    con = db_conn(); cur = con.cursor()
    ts_min = 0
    if days and days > 0:
        ts_min = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())

    # إشارات
    cur.execute("""SELECT COUNT(*) FROM signals WHERE ts>=?""", (ts_min,))
    total = cur.fetchone()[0] or 0

    # TP/SL
    cur.execute("""SELECT event, COUNT(*) FROM outcomes o 
                   JOIN signals s ON s.id=o.signal_id 
                   WHERE s.ts>=? GROUP BY event""", (ts_min,))
    counts = {row[0]: row[1] for row in cur.fetchall()}

    # أفضل/أسوأ أزواج (حسب عدد TP4 و SL)
    cur.execute("""
        SELECT s.symbol, 
               SUM(CASE WHEN o.event LIKE 'TP%' THEN 1 ELSE 0 END) as tp_hits,
               SUM(CASE WHEN o.event='SL' THEN 1 ELSE 0 END) as sl_hits
        FROM signals s LEFT JOIN outcomes o ON s.id=o.signal_id
        WHERE s.ts>=?
        GROUP BY s.symbol
        ORDER BY tp_hits DESC, sl_hits ASC
        LIMIT 5
    """, (ts_min,))
    top_rows = cur.fetchall()

    cur.execute("""
        SELECT s.symbol, 
               SUM(CASE WHEN o.event='SL' THEN 1 ELSE 0 END) as sl_hits,
               SUM(CASE WHEN o.event LIKE 'TP%' THEN 1 ELSE 0 END) as tp_hits
        FROM signals s LEFT JOIN outcomes o ON s.id=o.signal_id
        WHERE s.ts>=?
        GROUP BY s.symbol
        ORDER BY sl_hits DESC, tp_hits ASC
        LIMIT 5
    """, (ts_min,))
    worst_rows = cur.fetchall()

    # متوسط زمن الإغلاق (أول outcome لكل signal)
    cur.execute("""
        SELECT AVG(first_close_ts - ts)
        FROM (
            SELECT s.id, s.ts,
                   MIN(o.ts) as first_close_ts
            FROM signals s LEFT JOIN outcomes o ON s.id=o.signal_id
            WHERE s.ts>=?
            GROUP BY s.id
            HAVING first_close_ts IS NOT NULL
        )
    """, (ts_min,))
    avg_secs = cur.fetchone()[0]

    con.close()
    return {
        "total_signals": total,
        "counts": counts,
        "top_symbols": top_rows,
        "worst_symbols": worst_rows,
        "avg_close_seconds": int(avg_secs) if avg_secs else None
    }

def db_fetch_reasons(days: Optional[int], limit: int = 12):
    con = db_conn(); cur = con.cursor()
    ts_min = 0
    if days and days > 0:
        ts_min = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    # نفكك حقول JSON ونعدّ المفاتيح
    cur.execute("""
        SELECT reasons FROM nosignal_reasons WHERE ts>=?
    """, (ts_min,))
    rows = cur.fetchall()
    counts = {}
    for (j,) in rows:
        try:
            d = json.loads(j)
            for k, v in d.items():
                key = k if isinstance(v, (bool, int, float, str)) else k
                counts[key] = counts.get(key, 0) + 1
        except Exception:
            counts["parse_error"] = counts.get("parse_error", 0) + 1
    con.close()
    items = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]
    return items

def db_fetch_last(n: int = 10):
    con = db_conn(); cur = con.cursor()
    cur.execute("""SELECT id, ts, exchange, symbol, side, entry, sl, tp1,tp2,tp3,tp4, msg_id
                   FROM signals ORDER BY id DESC LIMIT ?""", (n,))
    sigs = cur.fetchall()
    # Outcomes لكل إشارة
    out_map = {}
    if sigs:
        ids = tuple([r[0] for r in sigs])
        q = f"SELECT signal_id, event, ts FROM outcomes WHERE signal_id IN ({','.join(['?']*len(ids))}) ORDER BY ts"
        cur.execute(q, ids)
        for sid, event, ts in cur.fetchall():
            out_map.setdefault(sid, []).append((event, ts))
    con.close()
    return sigs, out_map

def db_export_csv(days: Optional[int]) -> bytes:
    con = db_conn(); cur = con.cursor()
    ts_min = 0
    if days and days > 0:
        ts_min = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())

    cur.execute("""SELECT id,ts,exchange,symbol,side,entry,sl,tp1,tp2,tp3,tp4,msg_id
                   FROM signals WHERE ts>=? ORDER BY id""", (ts_min,))
    sigs = cur.fetchall()

    cur.execute("""SELECT signal_id,ts,event,idx,price FROM outcomes
                   WHERE signal_id IN (SELECT id FROM signals WHERE ts>=?) ORDER BY id""", (ts_min,))
    outs = cur.fetchall()
    con.close()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["signal_id","ts","exchange","symbol","side","entry","sl","tp1","tp2","tp3","tp4","msg_id"])
    for r in sigs: w.writerow(list(r))
    w.writerow([])
    w.writerow(["signal_id","ts","event","idx","price"])
    for r in outs: w.writerow(list(r))
    return buf.getvalue().encode("utf-8")

async def poll_telegram_commands():
    if not POLL_COMMANDS:
        return
    while True:
        try:
            params = {"timeout": 0, "offset": last_update_offset["offset"]+1}
            r = requests.get(GET_UPDATES_URL, params=params, timeout=25)
            j = r.json()
            if j.get("ok") and j.get("result"):
                for upd in j["result"]:
                    last_update_offset["offset"] = upd["update_id"]
                    msg = upd.get("message") or {}
                    if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                        continue
                    text = msg.get("text") or ""
                    cmd, args = parse_cmd(text)
                    if cmd == "/stats":
                        days = int(args[0]) if args[:1] and args[0].isdigit() else None
                        st = db_fetch_stats(days)
                        avg = st["avg_close_seconds"]
                        avg_txt = f"{avg//60}m{avg%60}s" if avg is not None else "—"
                        counts = st["counts"]
                        msg_txt = (
                            f"📊 Stats{f' (last {days}d)' if days else ''}\n"
                            f"- Signals: {st['total_signals']}\n"
                            f"- Hits: TP1={counts.get('TP1',0)}, TP2={counts.get('TP2',0)}, TP3={counts.get('TP3',0)}, TP4={counts.get('TP4',0)}, SL={counts.get('SL',0)}\n"
                            f"- Avg time to close: {avg_txt}\n"
                            f"- Top symbols: " + ", ".join([f"{symbol_pretty(s)}({tp} TP/{sl} SL)" for s,tp,sl in st["top_symbols"]]) + "\n"
                            f"- Worst symbols: " + ", ".join([f"{symbol_pretty(s)}({sl} SL/{tp} TP)" for s,sl,tp in st["worst_symbols"]])
                        )
                        send_telegram(msg_txt)
                    elif cmd == "/reasons":
                        days = int(args[0]) if args[:1] and args[0].isdigit() else None
                        items = db_fetch_reasons(days)
                        body = "\n".join([f"- {k}: {v}" for k,v in items]) or "—"
                        send_telegram(f"🧭 Top 'no-signal' reasons{f' (last {days}d)' if days else ''}:\n{body}")
                    elif cmd == "/last":
                        n = int(args[0]) if args[:1] and args[0].isdigit() else 10
                        sigs, out_map = db_fetch_last(min(max(n,1),50))
                        lines = []
                        for sid, ts, ex, sym, side, entry, sl, tp1,tp2,tp3,tp4, mid in sigs:
                            when = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                            outs = out_map.get(sid, [])
                            res = ", ".join([ev for ev,_ in outs]) if outs else "—"
                            lines.append(f"{sid} | {when} | {symbol_pretty(sym)} | {side} | entry {entry} | SL {sl} | TPs[{tp1},{tp2},{tp3},{tp4}] | outcome: {res}")
                        txt = "🗂 آخر الإشارات:\n" + ("\n".join(lines[-15:]) if lines else "—")
                        send_telegram(txt)
                    elif cmd == "/open":
                        if not open_trades:
                            send_telegram("لا توجد صفقات مفتوحة")
                        else:
                            lines = []
                            for sym, pos in open_trades.items():
                                lines.append(f"{symbol_pretty(sym)} | {pos['side']} | entry {pos['entry']} | SL {pos['sl']} | TPs {pos['tps']}")
                            send_telegram("الصفقات المفتوحة:\n" + "\n".join(lines))
                    elif cmd == "/export":
                        days = int(args[0]) if args[:1] and args[0].isdigit() else None
                        data = db_export_csv(days)
                        send_document(f"signals_export{('_'+str(days)+'d') if days else ''}.csv", data, caption="CSV: signals + outcomes")
            await asyncio.sleep(POLL_INTERVAL)
        except Exception as e:
            print("poll_telegram_commands error:", e)
            await asyncio.sleep(POLL_INTERVAL)

# ============================ Startup / Runner ============================
async def attempt_reload_symbols(state):
    fallbacks = ["okx", "kucoinfutures", "bitget", "gate", "binance"]
    try:
        ex, used = try_build_exchange_with_failover(EXCHANGE_NAME, fallbacks)
        syms = parse_symbols_from_env(ex, SYMBOLS_ENV)
        state.exchange = ex
        state.exchange_id = used
        state.symbols = syms
        print(f"[reload] {used}, symbols={len(syms)}")
    except Exception as e:
        print(f"[reload] failed: {type(e).__name__}: {str(e)[:220]}")

@app.on_event("startup")
async def _startup():
    db_init()
    head = (f"> توصيات تداول Ai:\n"
            f"✅ البوت اشتغل\n"
            f"Exchange: (initializing)\nTF: {TIMEFRAME}\n"
            f"Pairs: (loading…)")
    status_id = send_telegram(head)
    app.state.status_msg_id_holder = {"id": status_id}
    app.state.exchange = make_exchange(EXCHANGE_NAME)
    app.state.exchange_id = EXCHANGE_NAME
    app.state.symbols = []
    await attempt_reload_symbols(app.state)

    ex_id = getattr(app.state, "exchange_id", EXCHANGE_NAME)
    syms = getattr(app.state, "symbols", [])
    upd = (f"> تحديث الإقلاع:\nExchange: {ex_id}\nTF: {TIMEFRAME}\n"
           f"Pairs: {', '.join([symbol_pretty(s) for s in syms[:10]])}"
           f"{'' if len(syms) <= 10 else f' …(+{len(syms)-10})'}")
    send_telegram(upd, reply_to_message_id=status_id)

    asyncio.create_task(runner())
    if POLL_COMMANDS:
        asyncio.create_task(poll_telegram_commands())

async def runner():
    holder = app.state.status_msg_id_holder
    while True:
        try:
            ex = app.state.exchange
            syms = app.state.symbols
            if not syms:
                send_telegram("> لا توجد رموز مُحمّلة بعد. إعادة المحاولة…", reply_to_message_id=holder.get("id"))
                await attempt_reload_symbols(app.state)
            else:
                await scan_once(ex, syms, holder)
        except Exception as e:
            err = f"Loop error: {type(e).__name__} {e}\n{traceback.format_exc()}"
            print(err)
            send_telegram(f"⚠️ Loop error:\n{err[:3500]}", reply_to_message_id=holder.get("id"))
        await asyncio.sleep(SCAN_INTERVAL)

# Health
@app.get("/health")
def health():
    return {
        "ok": True,
        "exchange": getattr(app.state, "exchange_id", EXCHANGE_NAME),
        "symbols": len(getattr(app.state, "symbols", [])),
        "open_trades": len(open_trades),
        "db": LOG_DB_PATH
    }

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
