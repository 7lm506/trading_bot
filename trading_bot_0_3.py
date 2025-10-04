# trading_bot_smart_1_0.py
# إستراتيجية: Smart Momentum + Volatility Compression Breakout
# - يدعم كل عقود الفيوتشرز (AUTO_FUTURES) مع Failover تلقائي بين المنصات
# - رسائل تيليجرام بأسلوب القنوات: دخول/وقف/4 أهداف + الرافعة
# - إشارات edge-triggered على الشمعة المغلقة (i-1) + تبريد + منع تكرار
# - تتبع TP1..TP4 و SL مع Replies على رسالة الإشارة الأصلية

import os, json, asyncio, time, traceback
from typing import Dict, List, Optional, Tuple

import requests
import pandas as pd
import ccxt
from fastapi import FastAPI
import uvicorn

# ==================== إعدادات عامة ====================
EXCHANGE_NAME = os.getenv("EXCHANGE", "bybit").lower()
TIMEFRAME     = os.getenv("TIMEFRAME", "5m")
SYMBOLS_ENV   = os.getenv("SYMBOLS", "AUTO_FUTURES")
MAX_SYMBOLS   = int(os.getenv("MAX_SYMBOLS", "60"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
OHLCV_LIMIT   = int(os.getenv("OHLCV_LIMIT", "300"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()
HTTP_PROXY     = os.getenv("HTTP_PROXY") or None
HTTPS_PROXY    = os.getenv("HTTPS_PROXY") or None

# استراتيجية: باراميترات قابلة للتعديل من البيئة
COOLDOWN_CANDLES     = int(os.getenv("COOLDOWN_CANDLES", "3"))
MIN_ENTRY_CHANGE_PCT = float(os.getenv("MIN_ENTRY_CHANGE_PCT", "0.15"))  # %
BB_BANDWIDTH_MAX     = float(os.getenv("BB_BANDWIDTH_MAX", "0.04"))
ATR_SL_MULT          = float(os.getenv("ATR_SL_MULT", "1.2"))
TP_PCTS              = [float(x) for x in (os.getenv("TP_PCTS", "0.25,0.5,1.0,1.5")).split(",")]
DEFAULT_LEVERAGE     = int(os.getenv("DEFAULT_LEVERAGE", "10"))

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise SystemExit("TELEGRAM_TOKEN و CHAT_ID مطلوبتان كمتغيرات بيئة.")

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

# ==================== Telegram ====================
def send_telegram(text: str, reply_to_message_id: Optional[int] = None) -> Optional[int]:
    try:
        resp = requests.post(
            TG_API,
            data={
                "chat_id": CHAT_ID,
                "text": text,
                "disable_web_page_preview": True,
                **({"reply_to_message_id": reply_to_message_id} if reply_to_message_id else {}),
            },
            timeout=25,
        )
        data = resp.json()
        if not data.get("ok"):
            print(f"Telegram error: {data}")
            return None
        return data["result"]["message_id"]
    except Exception as e:
        print(f"Telegram send error: {type(e).__name__}: {e}")
        return None

# ==================== مؤشرات مساعدة ====================
def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()

def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    d = series.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    ma_up = up.ewm(com=n-1, adjust=False).mean()
    ma_dn = dn.ewm(com=n-1, adjust=False).mean()
    rs = ma_up / (ma_dn.replace(0, 1e-12))
    return 100 - (100 / (1 + rs))

def macd(series: pd.Series, fast=12, slow=26, signal_n=9) -> Tuple[pd.Series, pd.Series]:
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal = ema(macd_line, signal_n)
    return macd_line, signal

def bollinger(series: pd.Series, n=20, k=2.0) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    ma = series.rolling(n).mean()
    sd = series.rolling(n).std(ddof=0)
    upper = ma + k * sd
    lower = ma - k * sd
    bandwidth = (upper - lower) / ma
    return ma, upper, lower, bandwidth

def atr(df: pd.DataFrame, n=14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([(h - l).abs(), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def pct_diff(a: float, b: float) -> float:
    return abs(a - b) / max(b, 1e-12) * 100.0

# ==================== CCXT / Exchanges ====================
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
            "defaultSubType": "linear",  # لبايبت
        },
    }
    if HTTP_PROXY or HTTPS_PROXY:
        cfg["proxies"] = {"http": HTTP_PROXY, "https": HTTPS_PROXY}
    return klass(cfg)

def load_markets_linear_only(exchange) -> None:
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
    if last_err:
        raise last_err
    raise Exception("No exchanges available")

# ==================== رموز الفيوتشرز ====================
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
    markets = exchange.markets
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

# ==================== جلب البيانات ====================
async def fetch_ohlcv_safe(exchange, symbol: str, timeframe: str, limit: int):
    try:
        params = {}
        if exchange.id == "bybit":
            params = {"category": "linear"}
        elif exchange.id == "okx":
            params = {"instType": "SWAP"}
        ohlcv = await asyncio.to_thread(
            exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit, params=params
        )
        if not ohlcv or len(ohlcv) < 30:
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

# ==================== حالة الإشارات والصفقات ====================
open_trades: Dict[str, Dict] = {}   # symbol -> {side, entry, sl, tps[], hit[], msg_id}
signal_state: Dict[str, Dict] = {}  # symbol -> {last_entry, last_side, last_candle_idx, cooldown_until_idx}

def symbol_pretty(sym: str) -> str:
    # إزالة لاحقة :USDT من Bybit للعرض
    return sym.replace(":USDT", "")

# ==================== الإستراتيجية (Edge-Triggered) ====================
def smart_breakout_strategy(df: pd.DataFrame) -> Tuple[Optional[Dict], Dict]:
    """
    LONG:
      - ضيق بولنجر (bandwidth <= BB_BANDWIDTH_MAX)
      - إغلاق الشمعة (i-1) فوق الحد العلوي، وكان (i-2) داخل/تحته
      - MACD(i-1) > Signal(i-1)
      - 45 < RSI(i-1) < 70
    SHORT:
      - ضيق بولنجر
      - إغلاق (i-1) تحت الحد السفلي، وكان (i-2) داخل/فوقه
      - MACD < Signal
      - 30 < RSI < 55
    SL = ATR(14) * ATR_SL_MULT
    TP4..TP1 حسب TP_PCTS%
    """
    reasons = {}
    if df is None or len(df) < 60:
        reasons["insufficient_data"] = f"candles={0 if df is None else len(df)} (<60)"
        return None, reasons

    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    ma, bb_up, bb_dn, bb_bw = bollinger(close, n=20, k=2.0)
    macd_line, macd_sig = macd(close, 12, 26, 9)
    r = rsi(close, 14)
    atr14 = atr(df, 14)

    # نعمل على الشمعة المغلقة الأخيرة i-1 والإحدى قبلها i-2
    i2, i1 = -3, -2
    try:
        c_prev, c_now = float(close.iloc[i2]), float(close.iloc[i1])
        up_prev, up_now = float(bb_up.iloc[i2]), float(bb_up.iloc[i1])
        dn_prev, dn_now = float(bb_dn.iloc[i2]), float(bb_dn.iloc[i1])
        bw_now = float(bb_bw.iloc[i1])
        macd_now = float(macd_line.iloc[i1]); sig_now = float(macd_sig.iloc[i1])
        r14 = float(r.iloc[i1])
        atr_now = float(atr14.iloc[i1])
    except Exception:
        reasons["index_error"] = True
        return None, reasons

    # ضيق
    is_squeeze = bw_now <= BB_BANDWIDTH_MAX

    # عبور
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

    # إعداد SL و TPs
    entry = c_now
    sl_dist = ATR_SL_MULT * max(atr_now, 1e-12)
    if long_ok:
        sl = entry - sl_dist
        tps = [entry * (1 + p/100.0) for p in TP_PCTS]  # تصاعدي
        side = "LONG"
    else:
        sl = entry + sl_dist
        # للأهداف التنازلية نطرح %
        tps = [entry * (1 - p/100.0) for p in TP_PCTS]  # تنازلي
        side = "SHORT"

    return ({
        "side": side,
        "entry": float(entry),
        "sl": float(sl),
        "tps": [float(x) for x in tps],
        "candle_i1_ts": int(df.index[i1].value // 1e9)  # للتهدئة
    }, {})

# ==================== تحقُّق الأهداف/الستوب ====================
def crossed_levels(side: str, price: float, tps: List[float], sl: float, hit: List[bool]) -> Optional[Tuple[str, int]]:
    if price is None:
        return None
    # SL أولًا
    if side == "LONG" and price <= sl:
        return ("SL", -1)
    if side == "SHORT" and price >= sl:
        return ("SL", -1)
    # تحقق TP بالترتيب
    for idx, (tp, was_hit) in enumerate(zip(tps, hit)):
        if was_hit:
            continue
        if side == "LONG" and price >= tp:
            return ("TP", idx)
        if side == "SHORT" and price <= tp:
            return ("TP", idx)
    return None

# ==================== FastAPI ====================
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
        "params": {
            "BB_BANDWIDTH_MAX": BB_BANDWIDTH_MAX,
            "ATR_SL_MULT": ATR_SL_MULT,
            "TP_PCTS": TP_PCTS,
            "COOLDOWN_CANDLES": COOLDOWN_CANDLES,
            "MIN_ENTRY_CHANGE_PCT": MIN_ENTRY_CHANGE_PCT
        }
    }

# ==================== حلقة المسح ====================
async def fetch_and_signal(exchange, symbol: str, timeframe: str, holder: Dict[str, Optional[int]]):
    out = await fetch_ohlcv_safe(exchange, symbol, timeframe, OHLCV_LIMIT)
    if isinstance(out, str):
        return ("error", symbol, out)
    if out is None or len(out) < 60:
        return ("no_data", symbol, {"insufficient_data": True})

    # إذا هناك صفقة مفتوحة، لا تعطي إشارة جديدة بنفس الاتجاه
    if symbol in open_trades:
        return ("open_trade", symbol, {})

    sig, reasons = smart_breakout_strategy(out)
    if not sig:
        # نضمن أسباب غير فارغة
        if not reasons:
            reasons = {"note": "no setup"}
        return ("no_signal", symbol, reasons)

    # تبريد/منع تكرار
    st = signal_state.get(symbol, {})
    last_entry = st.get("last_entry")
    last_side  = st.get("last_side")
    last_idx   = st.get("last_candle_idx", -1)
    cooldown_until = st.get("cooldown_until_idx", -999999)

    # الشمعة المغلقة نعتبرها index = len(out)-2
    closed_idx = len(out) - 2

    # تبريد
    if closed_idx < cooldown_until:
        return ("cooldown", symbol, {"cooldown_until_idx": cooldown_until})

    # لو نفس الاتجاه وفارق الدخول قريب، تجاهل
    if last_side == sig["side"] and last_entry is not None:
        if pct_diff(sig["entry"], float(last_entry)) < MIN_ENTRY_CHANGE_PCT:
            return ("near_dupe", symbol, {"last_entry": last_entry, "new_entry": sig["entry"]})

    # جهّز رسالة القناة
    pretty = symbol_pretty(symbol)
    side_txt = "طويل 🟢" if sig["side"] == "LONG" else "قصير 🔴"
    header = f"#{pretty} - {side_txt}"
    entry = sig["entry"]; sl = sig["sl"]; tps = sig["tps"]
    lev = DEFAULT_LEVERAGE

    msg = (
        f"{header}\n\n"
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
        open_trades[symbol] = {
            "side": sig["side"],
            "entry": entry,
            "sl": sl,
            "tps": tps,
            "hit": [False, False, False, False],
            "msg_id": mid
        }
        # سجل حالة الإشارة
        signal_state[symbol] = {
            "last_entry": entry,
            "last_side": sig["side"],
            "last_candle_idx": closed_idx,
            "cooldown_until_idx": closed_idx + COOLDOWN_CANDLES
        }

    return ("signal", symbol, sig)

async def check_open_trades(exchange, holder: Dict[str, Optional[int]]):
    # تحقق TP/SL لكل صفقة مفتوحة
    for sym, pos in list(open_trades.items()):
        price = await fetch_ticker_price(exchange, sym)
        res = crossed_levels(pos["side"], price, pos["tps"], pos["sl"], pos["hit"])
        if not res:
            continue
        kind, idx = res
        if kind == "SL":
            txt = (
                f"🛑 SL تحقق لـ {symbol_pretty(sym)}\n"
                f"نوع: {pos['side']}\n"
                f"دخول: {pos['entry']}\n"
                f"ستوب: {pos['sl']}\n"
                f"السعر الحالي: {price}"
            )
            send_telegram(txt, reply_to_message_id=pos["msg_id"])
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
            # إغلاق الصفقة بعد TP4
            if all(pos["hit"]):
                del open_trades[sym]

# ==================== Runner ====================
async def scan_once(exchange, symbols: List[str], holder: Dict[str, Optional[int]]):
    await check_open_trades(exchange, holder)

    if not symbols:
        return ("no_symbols", {})

    sem = asyncio.Semaphore(2)  # تقليل التوازي لتفادي RateLimit
    results = {"signals": {}, "no_signals": {}, "errors": {}}

    async def worker(sym: str):
        async with sem:
            res_type, s, payload = await fetch_and_signal(exchange, sym, TIMEFRAME, holder)
            if res_type == "signal":
                results["signals"][s] = payload
            elif res_type == "error":
                results["errors"][s] = payload
            elif res_type in ("no_signal", "no_data", "open_trade", "cooldown", "near_dupe"):
                results["no_signals"][s] = payload
            # else: ignore

    await asyncio.gather(*[asyncio.create_task(worker(s)) for s in symbols])

    # لو لا إشارات جديدة: أرسل أسباب مختصرة (عينة)
    if not results["signals"]:
        bundle = {}
        # خذ أول 15 رمز من أسباب "no_signals"
        for k, v in list(results["no_signals"].items())[:15]:
            bundle[k] = v
        # وأول 8 أخطاء
        for k, v in list(results["errors"].items())[:8]:
            bundle[k] = str(v)[:200]
        send_telegram(
            f"> توصيات تداول Ai:\nℹ️ لا توجد إشارات حاليًا – الأسباب\n{json.dumps(bundle, ensure_ascii=False, indent=2) if bundle else '—'}",
            reply_to_message_id=holder.get("id")
        )

    return ("done", results)

# ==================== Startup / Failover ====================
app = FastAPI()

@app.on_event("startup")
async def _startup():
    # رسالة تشغيل مرة واحدة
    head = (f"> توصيات تداول Ai:\n"
            f"✅ البوت اشتغل\n"
            f"Exchange: (initializing)\nTF: {TIMEFRAME}\n"
            f"Pairs: (loading…)")
    status_id = send_telegram(head)

    app.state.status_msg_id_holder = {"id": status_id}
    app.state.exchange = make_exchange(EXCHANGE_NAME)  # placeholder
    app.state.exchange_id = EXCHANGE_NAME
    app.state.symbols = []

    # Failover أولي
    await attempt_reload_symbols(app.state)

    # تحديث الرأسية
    ex_id = getattr(app.state, "exchange_id", EXCHANGE_NAME)
    syms = getattr(app.state, "symbols", [])
    upd = (f"> تحديث الإقلاع:\n"
           f"Exchange: {ex_id}\nTF: {TIMEFRAME}\n"
           f"Pairs: {', '.join([symbol_pretty(s) for s in syms[:10]])}" +
           ("" if len(syms) <= 10 else f" …(+{len(syms)-10})"))
    send_telegram(upd, reply_to_message_id=status_id)

    asyncio.create_task(runner())

async def attempt_reload_symbols(app_state) -> None:
    fallbacks = ["okx", "kucoinfutures", "bitget", "gate", "binance"]
    try:
        ex, used = try_build_exchange_with_failover(EXCHANGE_NAME, fallbacks)
        syms = parse_symbols_from_env(ex, SYMBOLS_ENV)
        app_state.exchange = ex
        app_state.exchange_id = used
        app_state.symbols = syms
        print(f"[reload] success on {used}, symbols={len(syms)}")
    except Exception as e:
        print(f"[reload] failed: {type(e).__name__}: {str(e)[:220]}")

async def runner():
    holder = app.state.status_msg_id_holder
    empty_notify_every = 5
    empty_counter = 0
    while True:
        try:
            ex = app.state.exchange
            syms = app.state.symbols
            if not syms:
                empty_counter += 1
                if empty_counter % empty_notify_every == 1:
                    send_telegram(
                        "> ملاحظة: لا توجد رموز مُحمّلة بعد. سأحاول إعادة تحميل الأسواق تلقائيًا (failover).",
                        reply_to_message_id=holder.get("id")
                    )
                await attempt_reload_symbols(app.state)
            else:
                await scan_once(ex, syms, holder)
        except Exception as e:
            err = f"Loop error: {type(e).__name__} {e}\n{traceback.format_exc()}"
            print(err)
            send_telegram(f"⚠️ Loop error:\n{err[:3500]}", reply_to_message_id=holder.get("id"))
        await asyncio.sleep(SCAN_INTERVAL)

# نقطة صحة إضافية
@app.get("/health")
def health():
    return {
        "ok": True,
        "exchange": getattr(app.state, "exchange_id", EXCHANGE_NAME),
        "symbols": len(getattr(app.state, "symbols", [])),
        "open_trades": len(open_trades),
    }

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
