# trading_bot_0_5.py
# يدعم: كل عقود الفيوتشرز/السواب تلقائيًا عبر SYMBOLS=AUTO_FUTURES
# يصلّح Bybit (رموز :USDT + category=linear)، ويوقف تكرار رسالة التشغيل، ويرسل Replies فقط لاحقًا.
# FastAPI للصحة + Loop async.
import os, time, json, traceback, asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
import requests
import ccxt
import pandas as pd

from fastapi import FastAPI
import uvicorn

# ==================== إعدادات عامة من البيئة ====================
EXCHANGE_NAME = os.getenv("EXCHANGE", "bybit").lower()
TIMEFRAME     = os.getenv("TIMEFRAME", "5m")
SYMBOLS_ENV   = os.getenv("SYMBOLS", "BTC/USDT,ETH/USDT")
MAX_SYMBOLS   = int(os.getenv("MAX_SYMBOLS", "0"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
OHLCV_LIMIT   = int(os.getenv("OHLCV_LIMIT", "300"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()

HTTP_PROXY  = os.getenv("HTTP_PROXY") or None
HTTPS_PROXY = os.getenv("HTTPS_PROXY") or None

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise SystemExit("TELEGRAM_TOKEN و CHAT_ID مطلوبتان كمتغيرات بيئة.")

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

# ==================== Telegram Helper ====================
def send_telegram(text: str, reply_to_message_id: Optional[int] = None) -> Optional[int]:
    try:
        resp = requests.post(
            TG_API,
            data={
                "chat_id": CHAT_ID,
                "text": text,
                "disable_web_page_preview": True,
                **({"reply_to_message_id": reply_to_message_id} if reply_to_message_id else {})
            },
            timeout=20
        )
        data = resp.json()
        if not data.get("ok"):
            print(f"Telegram error: {data}")
            return None
        return data["result"]["message_id"]
    except Exception as e:
        print(f"Telegram send error: {type(e).__name__}: {e}")
        return None

# ==================== إنشاء المنصة عبر CCXT ====================
def make_exchange(name: str):
    klass = {
        "bybit": ccxt.bybit,
        "okx": ccxt.okx,
        "binance": ccxt.binance,
        "kucoinfutures": ccxt.kucoinfutures,
        "krakenfutures": ccxt.krakenfutures,
        "bitget": ccxt.bitget,
        "gate": ccxt.gate
    }.get(name, ccxt.bybit)

    cfg = {
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {
            "defaultType": "swap",        # افتراضيًا: عقود دائمة
            "defaultSubType": "linear",   # مهم لبايبت
        },
    }
    # بروكسي اختياري
    if HTTP_PROXY or HTTPS_PROXY:
        cfg["proxies"] = {"http": HTTP_PROXY, "https": HTTPS_PROXY}

    exchange = klass(cfg)
    return exchange

# ==================== أدوات المؤشرات ====================
def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()

def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ma_up = up.ewm(com=n-1, adjust=False).mean()
    ma_down = down.ewm(com=n-1, adjust=False).mean()
    rs = ma_up / (ma_down.replace(0, 1e-10))
    return 100 - (100 / (1 + rs))

def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.Series:
    hl = (df["high"] - df["low"]).abs()
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, adjust=False).mean()

    hl2 = (df["high"] + df["low"]) / 2
    upperband = hl2 + multiplier * atr
    lowerband = hl2 - multiplier * atr

    st = pd.Series(index=df.index, dtype=float)
    dir_up = True
    prev_st = None
    for i in range(len(df)):
        if i == 0:
            st.iloc[i] = upperband.iloc[i]
            prev_st = st.iloc[i]
            continue
        if df["close"].iloc[i] > upperband.iloc[i-1]:
            dir_up = True
        elif df["close"].iloc[i] < lowerband.iloc[i-1]:
            dir_up = False

        if dir_up:
            st.iloc[i] = max(lowerband.iloc[i], prev_st if prev_st else lowerband.iloc[i])
        else:
            st.iloc[i] = min(upperband.iloc[i], prev_st if prev_st else upperband.iloc[i])

        prev_st = st.iloc[i]
    return st

# ==================== منطق الإشارة ====================
def build_signal(df: pd.DataFrame) -> Tuple[Optional[Dict], Dict]:
    reasons = {}
    if df is None or len(df) < 60:
        reasons["insufficient_data"] = f"candles={0 if df is None else len(df)} (<60)"
        return None, reasons

    close = df["close"]
    ema50 = ema(close, 50)
    ema200 = ema(close, 200)
    r = rsi(close, 14)
    st = supertrend(df, 10, 3.0)

    last = df.index[-1]
    c = float(close.loc[last])
    e50 = float(ema50.loc[last])
    e200 = float(ema200.loc[last])
    r14 = float(r.loc[last])
    stv = float(st.loc[last])

    trend_up = e50 > e200
    trend_down = e50 < e200
    above_st = c > stv
    below_st = c < stv

    long_ok  = trend_up and above_st and (45 < r14 < 75)
    short_ok = trend_down and below_st and (25 < r14 < 55)

    if long_ok:
        entry = c
        sl = float(min(df["low"].tail(10)))
        tp = entry * 1.01
        return ({"side": "LONG", "entry": entry, "tp": tp, "sl": sl}, {})
    if short_ok:
        entry = c
        sl = float(max(df["high"].tail(10)))
        tp = entry * 0.99
        return ({"side": "SHORT", "entry": entry, "tp": tp, "sl": sl}, {})

    reasons["trend_up"] = trend_up
    reasons["trend_down"] = trend_down
    reasons["above_supertrend"] = above_st
    reasons["below_supertrend"] = below_st
    reasons["rsi14"] = round(r14, 2)
    reasons["ema50_vs_ema200"] = f"{round(e50,2)} vs {round(e200,2)}"
    return None, reasons

# ==================== رموز الفيوتشرز ====================
def list_all_futures_symbols(exchange) -> List[str]:
    markets = exchange.load_markets()
    syms = []
    for m in markets.values():
        if m.get("contract") and (m.get("future") or m.get("swap")):
            if m.get("active") is False:
                continue
            syms.append(m["symbol"])
    syms = sorted(set(syms))
    if MAX_SYMBOLS and MAX_SYMBOLS > 0:
        syms = syms[:MAX_SYMBOLS]
    return syms

def normalize_symbols_for_exchange(exchange, symbols: List[str]) -> List[str]:
    # Bybit: نضمن :USDT للـ linear swap
    if exchange.id == "bybit":
        out = []
        for s in symbols:
            if s.endswith("/USDT") and ":USDT" not in s:
                out.append(s + ":USDT")
            else:
                out.append(s)
        return out
    return symbols

def parse_symbols_from_env(exchange, env_value: str) -> List[str]:
    if env_value.strip().upper().startswith("AUTO_FUTURES"):
        syms = list_all_futures_symbols(exchange)
    else:
        syms = [s.strip() for s in env_value.split(",") if s.strip()]
    syms = normalize_symbols_for_exchange(exchange, syms)
    if MAX_SYMBOLS and MAX_SYMBOLS > 0:
        syms = syms[:MAX_SYMBOLS]
    return syms

# ==================== جلب البيانات ====================
async def fetch_ohlcv_safe(exchange, symbol: str, timeframe: str, limit: int):
    try:
        params = {}
        if exchange.id == "bybit":
            # يجبر Bybit على linear swap
            params = {"category": "linear"}
        elif exchange.id == "okx":
            # OKX سواب
            params = {"instType": "SWAP"}
        elif exchange.id in ("binance", "bitget", "gate"):
            params = {}  # CCXT يحلها عادة
        elif exchange.id == "kucoinfutures":
            params = {}
        elif exchange.id == "krakenfutures":
            params = {}

        ohlcv = await asyncio.to_thread(
            exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit, params=params
        )
        if not ohlcv or len(ohlcv) < 10:
            return None
        df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.set_index("ts", inplace=True)
        return df
    except Exception as e:
        return f"خطأ المنصة: {exchange.id} {type(e).__name__} {str(e)[:160]}"

async def fetch_ticker_price(exchange, symbol: str) -> Optional[float]:
    try:
        t = await asyncio.to_thread(exchange.fetch_ticker, symbol)
        return float(t.get("last") or t.get("close") or t.get("info", {}).get("lastPrice"))
    except Exception:
        return None

# ==================== حالة الإشارات المفتوحة ====================
open_trades: Dict[str, Dict] = {}   # symbol -> {side, entry, tp, sl, msg_id}

def crossed(side: str, price: Optional[float], tp: float, sl: float) -> Optional[str]:
    if price is None:
        return None
    if side == "LONG":
        if price >= tp: return "TP"
        if price <= sl: return "SL"
    else:
        if price <= tp: return "TP"
        if price >= sl: return "SL"
    return None

# ==================== المسح ====================
async def scan_once(exchange, symbols: List[str], status_msg_id_holder: Dict[str, Optional[int]]):
    no_signal_reasons: Dict[str, Dict] = {}
    new_signals: List[Tuple[str, Dict]] = {}
    errors: Dict[str, str] = {}

    # 1) فحص TP/SL
    for sym, pos in list(open_trades.items()):
        price = await fetch_ticker_price(exchange, sym)
        flag = crossed(pos["side"], price, pos["tp"], pos["sl"])
        if flag:
            txt = (
                f"🎯 {flag} تحقق لـ {sym}\n"
                f"نوع: {pos['side']}\n"
                f"سعر الدخول: {pos['entry']}\n"
                f"الهدف: {pos['tp']}\n"
                f"الستوب: {pos['sl']}\n"
                f"السعر الحالي: {price}"
            )
            send_telegram(txt, reply_to_message_id=pos.get("msg_id"))
            del open_trades[sym]

    # 2) فحص إشارات جديدة
    sem = asyncio.Semaphore(2)  # تقليل التوازي لتفادي Rate Limit

    # نستخدم dict بدل list للتفادي التكرار
    new_signals = {}

    async def process_symbol(symbol: str):
        async with sem:
            out = await fetch_ohlcv_safe(exchange, symbol, TIMEFRAME, OHLCV_LIMIT)
            if isinstance(out, str):
                errors[symbol] = out
                return
            if out is None:
                no_signal_reasons[symbol] = {"insufficient_data": True}
                return
            sig, reasons = build_signal(out)
            if sig:
                new_signals[symbol] = sig
            else:
                no_signal_reasons[symbol] = reasons

    tasks = [asyncio.create_task(process_symbol(s)) for s in symbols]
    await asyncio.gather(*tasks)

    status_id = status_msg_id_holder.get("id")

    # 3) إرسال النتائج
    if new_signals:
        for sym, s in new_signals.items():
            txt = (
                f"🚀 إشارة جديدة\n"
                f"زوج: {sym}\n"
                f"نوع: {s['side']}\n"
                f"دخول: {s['entry']}\n"
                f"هدف: {s['tp']}\n"
                f"ستوب: {s['sl']}\n"
                f"TF: {TIMEFRAME}\n"
                f"الرجاء إدارة المخاطر."
            )
            mid = send_telegram(txt, reply_to_message_id=status_id)
            if mid:
                open_trades[sym] = {**s, "msg_id": mid}
    else:
        bundle = {}
        if no_signal_reasons:
            # لا نطوّل جدًا
            subset = list(no_signal_reasons.items())[:20]
            bundle.update({k: v for k, v in subset})
        if errors:
            e_subset = list(errors.items())[:10]
            bundle.update({k: (v[:200]) for k, v in e_subset})
        send_telegram(
            f"> توصيات تداول Ai:\nℹ️ لا توجد إشارات حاليًا – الأسباب\n{json.dumps(bundle, ensure_ascii=False, indent=2)}",
            reply_to_message_id=status_id
        )

# ==================== FastAPI & Runner ====================
app = FastAPI()

@app.get("/")
def root():
    # مخرجات صحة سريعة
    info = {
        "ok": True,
        "exchange": getattr(app.state, "exchange_id", EXCHANGE_NAME),
        "timeframe": TIMEFRAME,
        "symbols_mode": SYMBOLS_ENV,
        "max_symbols": MAX_SYMBOLS,
        "scan_interval": SCAN_INTERVAL
    }
    # عدّاد صفقات مفتوحة
    info["open_trades"] = len(open_trades)
    return info

async def runner():
    exchange = app.state.exchange
    symbols = app.state.symbols
    status_msg_id_holder = app.state.status_msg_id_holder

    while True:
        try:
            await scan_once(exchange, symbols, status_msg_id_holder)
        except Exception as e:
            err = f"Loop error: {type(e).__name__} {e}\n{traceback.format_exc()}"
            print(err)
            send_telegram(f"⚠️ Loop error:\n{err[:3500]}", reply_to_message_id=status_msg_id_holder.get("id"))
        await asyncio.sleep(SCAN_INTERVAL)

@app.on_event("startup")
async def _startup():
    # 1) إنشاء المنصة
    exchange = make_exchange(EXCHANGE_NAME)

    # 2) load_markets مع معالجة أخطاء الحجب
    try:
        exchange.load_markets()
    except Exception as e:
        msg = (f"⚠️ فشل load_markets على {EXCHANGE_NAME}: {type(e).__name__} {str(e)[:180]}\n"
               f"إذا ظهر 403/451 جرّب تغيير EXCHANGE لمنصة غير محجوبة أو فعّل بروكسي.")
        print(msg)
        send_telegram(msg)

    # 3) تحضير الرموز
    symbols = parse_symbols_from_env(exchange, SYMBOLS_ENV)
    if not symbols:
        send_telegram("⚠️ لم أجد أي أزواج. تأكد من SYMBOLS أو AUTO_FUTURES.")
        # نكمل التشغيل لكن المسح سيُظهر عدم وجود رموز
    # 4) رسالة تشغيل مرة واحدة
    head = (f"> توصيات تداول Ai:\n"
            f"✅ البوت اشتغل\n"
            f"Exchange: {exchange.id}\nTF: {TIMEFRAME}\n"
            f"Pairs: {', '.join(symbols[:10])}" + ("" if len(symbols) <= 10 else f" …(+{len(symbols)-10})"))
    status_id = send_telegram(head)

    # 5) حفظ الحالة في app.state
    app.state.exchange = exchange
    app.state.exchange_id = exchange.id
    app.state.symbols = symbols
    app.state.status_msg_id_holder = {"id": status_id}

    # 6) تشغيل اللوب
    asyncio.create_task(runner())

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
