# trading_bot_0_7.py
# ميزات:
# - Failover تلقائي بين عدة منصات إذا فشل load_markets (403/451/WAF)
# - تقييد Bybit على linear swap + تطبيع الرموز :USDT
# - عدم إرسال JSON فاضي؛ ولو مافيه رموز، يرسل تنبيه ذكي ويحاول إعادة التحميل دورياً
# - رسالة التشغيل تُرسل مرة واحدة، وكل الرسائل لاحقًا Replies عليها
# - AUTO_FUTURES + MAX_SYMBOLS + بارامترات OHLCV الصحيحة لكل منصة
# - تقليل التوازي لتفادي RateLimit

import os, json, asyncio, traceback, time
from typing import Dict, List, Optional, Tuple

import requests
import ccxt
import pandas as pd

from fastapi import FastAPI
import uvicorn

# ===================== الإعدادات =====================
EXCHANGE_NAME = os.getenv("EXCHANGE", "bybit").lower()
TIMEFRAME     = os.getenv("TIMEFRAME", "5m")
SYMBOLS_ENV   = os.getenv("SYMBOLS", "AUTO_FUTURES")
MAX_SYMBOLS   = int(os.getenv("MAX_SYMBOLS", "50"))   # جرّب 50 كبداية
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
OHLCV_LIMIT   = int(os.getenv("OHLCV_LIMIT", "300"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()
HTTP_PROXY     = os.getenv("HTTP_PROXY") or None
HTTPS_PROXY    = os.getenv("HTTPS_PROXY") or None

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise SystemExit("TELEGRAM_TOKEN و CHAT_ID مطلوبتان كمتغيرات بيئة.")

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

# ===================== Telegram =====================
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

# ===================== CCXT / Exchanges =====================
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
        "options": {
            "defaultType": "swap",
            "defaultSubType": "linear",  # مهم لبايبت
        },
    }
    if HTTP_PROXY or HTTPS_PROXY:
        cfg["proxies"] = {"http": HTTP_PROXY, "https": HTTPS_PROXY}
    return klass(cfg)

def load_markets_linear_only(exchange) -> None:
    """
    يحاول تحميل الأسواق مقيدة للفيوتشرز/سواب (linear) مع backoff قصير.
    يتوقف مبكرًا لو الخطأ 403/451/WAF.
    """
    backoffs = [1.5, 3.0, 6.0]
    last_exc = None
    for i, wait in enumerate(backoffs, start=1):
        try:
            exchange.load_markets(reload=True, params={"category": "linear", "type": "swap"})
            return
        except Exception as e:
            last_exc = e
            emsg = str(e)
            print(f"[load_markets attempt {i}] {type(e).__name__}: {emsg[:220]}")
            if "403" in emsg or "451" in emsg or "Forbidden" in emsg:
                break
            time.sleep(wait)
    raise last_exc

def try_build_exchange_with_failover(primary: str, candidates: List[str]) -> Tuple[ccxt.Exchange, str]:
    order = [primary] + [c for c in candidates if c != primary]
    last_err = None
    for name in order:
        try:
            ex = make_exchange(name)
            load_markets_linear_only(ex)
            print(f"[failover] using exchange: {name}")
            return ex, name
        except Exception as e:
            last_err = e
            print(f"[failover] {name} failed: {type(e).__name__}: {str(e)[:220]}")
            # جرّب التالي
    if last_err:
        raise last_err
    raise Exception("No exchanges available")

# ===================== مؤشرات =====================
def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()

def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    d = series.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    ma_up = up.ewm(com=n-1, adjust=False).mean()
    ma_dn = dn.ewm(com=n-1, adjust=False).mean()
    rs = ma_up / (ma_dn.replace(0, 1e-10))
    return 100 - (100 / (1 + rs))

def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.Series:
    hl = (df["high"] - df["low"]).abs()
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    mid = (df["high"] + df["low"]) / 2
    ub = mid + mult * atr
    lb = mid - mult * atr
    st = pd.Series(index=df.index, dtype=float)
    dir_up = True
    prev = None
    for i in range(len(df)):
        if i == 0:
            st.iloc[i] = ub.iloc[i]
            prev = st.iloc[i]
            continue
        if df["close"].iloc[i] > ub.iloc[i-1]:
            dir_up = True
        elif df["close"].iloc[i] < lb.iloc[i-1]:
            dir_up = False
        st.iloc[i] = max(lb.iloc[i], prev) if dir_up else min(ub.iloc[i], prev)
        prev = st.iloc[i]
    return st

# ===================== منطق الإشارة =====================
def build_signal(df: pd.DataFrame) -> Tuple[Optional[Dict], Dict]:
    reasons = {}
    if df is None or len(df) < 60:
        reasons["insufficient_data"] = f"candles={0 if df is None else len(df)} (<60)"
        return None, reasons

    close = df["close"]
    e50 = float(ema(close, 50).iloc[-1])
    e200 = float(ema(close, 200).iloc[-1])
    r14 = float(rsi(close, 14).iloc[-1])
    stv = float(supertrend(df, 10, 3.0).iloc[-1])
    c = float(close.iloc[-1])

    trend_up = e50 > e200
    trend_down = e50 < e200
    above_st = c > stv
    below_st = c < stv

    long_ok  = trend_up and above_st and (45 < r14 < 75)
    short_ok = trend_down and below_st and (25 < r14 < 55)

    if long_ok:
        entry = c
        sl = float(df["low"].tail(10).min())
        tp = entry * 1.01
        return ({"side": "LONG", "entry": entry, "tp": tp, "sl": sl}, {})
    if short_ok:
        entry = c
        sl = float(df["high"].tail(10).max())
        tp = entry * 0.99
        return ({"side": "SHORT", "entry": entry, "tp": tp, "sl": sl}, {})

    reasons.update({
        "trend_up": trend_up,
        "trend_down": trend_down,
        "above_supertrend": above_st,
        "below_supertrend": below_st,
        "rsi14": round(r14, 2),
        "ema50_vs_ema200": f"{round(e50,2)} vs {round(e200,2)}"
    })
    return None, reasons

# ===================== رموز الفيوتشرز =====================
def normalize_symbols_for_exchange(exchange, symbols: List[str]) -> List[str]:
    if exchange.id == "bybit":
        out = []
        for s in symbols:
            if s.endswith("/USDT") and ":USDT" not in s:
                out.append(s + ":USDT")
            else:
                out.append(s)
        return out
    return symbols

def list_all_futures_symbols(exchange) -> List[str]:
    markets = exchange.markets  # بعد load_markets
    syms = []
    for m in markets.values():
        if m.get("contract") and (m.get("future") or m.get("swap")) and m.get("active", True) is not False:
            syms.append(m["symbol"])
    syms = sorted(set(syms))
    if MAX_SYMBOLS and MAX_SYMBOLS > 0:
        syms = syms[:MAX_SYMBOLS]
    return normalize_symbols_for_exchange(exchange, syms)

def parse_symbols_from_env(exchange, env_value: str) -> List[str]:
    if env_value.strip().upper().startswith("AUTO_FUTURES"):
        return list_all_futures_symbols(exchange)
    syms = [s.strip() for s in env_value.split(",") if s.strip()]
    syms = normalize_symbols_for_exchange(exchange, syms)
    if MAX_SYMBOLS and MAX_SYMBOLS > 0:
        syms = syms[:MAX_SYMBOLS]
    return syms

# ===================== جلب البيانات =====================
async def fetch_ohlcv_safe(exchange, symbol: str, timeframe: str, limit: int):
    try:
        params = {}
        if exchange.id == "bybit":
            params = {"category": "linear"}
        elif exchange.id == "okx":
            params = {"instType": "SWAP"}
        # باقي المنصات غالبًا تعمل بدون params إضافية
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
        return f"خطأ المنصة: {exchange.id} {type(e).__name__} {str(e)[:200]}"

async def fetch_ticker_price(exchange, symbol: str) -> Optional[float]:
    try:
        t = await asyncio.to_thread(exchange.fetch_ticker, symbol)
        return float(t.get("last") or t.get("close") or t.get("info", {}).get("lastPrice"))
    except Exception:
        return None

# ===================== حالة الصفقات =====================
open_trades: Dict[str, Dict] = {}  # symbol -> {side, entry, tp, sl, msg_id}

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

# ===================== FastAPI =====================
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
        "failover_used": getattr(app.state, "exchange_id", EXCHANGE_NAME),
    }

# ===================== المسح =====================
async def scan_once(exchange, symbols: List[str], status_msg_id_holder: Dict[str, Optional[int]]):
    # لو مافيه رموز، لا نرسل JSON فاضي؛ نترك runner يحاول إعادة التحميل
    if not symbols:
        # أرسل تذكير ذكي مرة كل عدة دورات فقط (يُدار في runner)
        return

    no_signal_reasons: Dict[str, Dict] = {}
    new_signals: Dict[str, Dict] = {}
    errors: Dict[str, str] = {}

    # تحقق TP/SL
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

    # إشارات جديدة
    sem = asyncio.Semaphore(2)  # تقليل التوازي

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

    await asyncio.gather(*[asyncio.create_task(process_symbol(s)) for s in symbols])

    status_id = status_msg_id_holder.get("id")

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
            for k, v in list(no_signal_reasons.items())[:20]:
                bundle[k] = v
        if errors:
            for k, v in list(errors.items())[:10]:
                bundle[k] = v[:200]
        send_telegram(
            f"> توصيات تداول Ai:\nℹ️ لا توجد إشارات حاليًا – الأسباب\n{json.dumps(bundle, ensure_ascii=False, indent=2) if bundle else '—'}",
            reply_to_message_id=status_id
        )

# ===================== Runner + Startup =====================
async def attempt_reload_symbols(app_state) -> None:
    """
    يحاول بناء منصة مع Failover ثم تحميل الأسواق واستخراج الرموز.
    يحدّث app.state عند النجاح.
    """
    fallbacks = ["okx", "kucoinfutures", "bitget", "gate", "binance"]
    try:
        ex, used = try_build_exchange_with_failover(EXCHANGE_NAME, fallbacks)
        syms = parse_symbols_from_env(ex, SYMBOLS_ENV)
        app_state.exchange = ex
        app_state.exchange_id = used
        app_state.symbols = syms
        print(f"[reload] success on {used}, symbols={len(syms)}")
    except Exception as e:
        # أبقِ الحالة كما هي؛ المحاولة القادمة لاحقاً
        print(f"[reload] failed: {type(e).__name__}: {str(e)[:220]}")

async def runner():
    holder = app.state.status_msg_id_holder
    empty_symbols_notify_every = 5   # كل 5 دورات
    empty_counter = 0

    while True:
        try:
            ex = app.state.exchange
            syms = app.state.symbols

            if not syms:
                empty_counter += 1
                # أرسل تنبيه كل N دورات فقط
                if empty_counter % empty_symbols_notify_every == 1:
                    send_telegram(
                        "> ملاحظة: لا توجد رموز مُحمّلة بعد.\n"
                        "سأعيد محاولة تحميل الأسواق/المنصة تلقائيًا (failover) خلال الدورات القادمة.",
                        reply_to_message_id=holder.get("id")
                    )
                # جرّب إعادة التحميل
                await attempt_reload_symbols(app.state)
            else:
                # عند وجود رموز، نفّذ دورة المسح
                await scan_once(ex, syms, holder)

        except Exception as e:
            err = f"Loop error: {type(e).__name__} {e}\n{traceback.format_exc()}"
            print(err)
            send_telegram(f"⚠️ Loop error:\n{err[:3500]}", reply_to_message_id=holder.get("id"))

        await asyncio.sleep(SCAN_INTERVAL)

app = FastAPI()

@app.on_event("startup")
async def _startup():
    # إعداد رسالة التشغيل (نرسلها مهما كان)
    head = (f"> توصيات تداول Ai:\n"
            f"✅ البوت اشتغل\n"
            f"Exchange: (initializing)\nTF: {TIMEFRAME}\n"
            f"Pairs: (loading…)")
    status_id = send_telegram(head)

    # جهّز حالة التطبيق
    app.state.status_msg_id_holder = {"id": status_id}
    app.state.exchange = make_exchange(EXCHANGE_NAME)  # placeholder
    app.state.exchange_id = EXCHANGE_NAME
    app.state.symbols = []

    # أول محاولة تحميل + Failover
    await attempt_reload_symbols(app.state)

    # بعد أول محاولة، عدّل الرسالة الرأسية بمعلومة أوضح (Reply تحديثي صغير)
    ex_id = getattr(app.state, "exchange_id", EXCHANGE_NAME)
    syms = getattr(app.state, "symbols", [])
    upd = (f"> تحديث الإقلاع:\n"
           f"Exchange: {ex_id}\nTF: {TIMEFRAME}\n"
           f"Pairs: {', '.join(syms[:10])}" + ("" if len(syms) <= 10 else f" …(+{len(syms)-10})"))
    send_telegram(upd, reply_to_message_id=status_id)

    # شغّل الحلقة
    asyncio.create_task(runner())

# نقطة صحة
@app.get("/health")
def health():
    return {"ok": True, "exchange": getattr(app.state, "exchange_id", EXCHANGE_NAME), "symbols": len(getattr(app.state, "symbols", []))}

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
