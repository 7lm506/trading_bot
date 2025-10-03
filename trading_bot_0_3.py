# trading_bot_0_3.py
import os, time, asyncio, json, html, logging
from typing import Dict, Any, Optional, List

import requests
import pandas as pd
import numpy as np
import ccxt
from fastapi import FastAPI
from fastapi.responses import JSONResponse

# ================== إعدادات عامة ==================
# تقدر تغيّرها من هنا. تقرأ من ENV لو موجود، وإلا تستخدم الثابت.
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8130568386:AAGmpxKQw1XhqNjtj2OBzJ_-e3_vn0FE5Bs")
CHAT_ID = int(os.getenv("CHAT_ID", "8429537293"))

EXCHANGE_NAME = os.getenv("EXCHANGE", "bybit")  # تجنّب Binance بسبب الحظر
TIMEFRAME = os.getenv("TF", "5m")
SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", "BTC/USDT,ETH/USDT").split(",") if s.strip()]

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "45"))  # كل كم ثانية نفحص
NO_SIGNAL_NOTIFY_EVERY = int(os.getenv("NO_SIGNAL_EVERY", "300"))  # كل كم ثانية نرسل "لا يوجد إشارات"

# فلتر ATR (كنسبة من السعر) + إعدادات الهدف والستوب
ATR_MIN = float(os.getenv("ATR_MIN", "0.003"))   # 0.3%
ATR_MAX = float(os.getenv("ATR_MAX", "0.02"))    # 2%
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
TP_ATR = float(os.getenv("TP_ATR", "1.5"))       # الهدف = 1.5 * ATR
SL_ATR = float(os.getenv("SL_ATR", "1.0"))       # الستوب = 1.0 * ATR

PORT = int(os.getenv("PORT", "10000"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ================== Telegram Safe Sender ==================
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

def _post_tg(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        r = requests.post(f"{TELEGRAM_API}/sendMessage", data=payload, timeout=15)
        if "application/json" in r.headers.get("content-type", ""):
            data = r.json()
        else:
            data = {"ok": False, "description": r.text}
        if not data.get("ok"):
            logging.warning(f"Telegram error: {data}")
        return data
    except Exception as e:
        logging.exception("Telegram send exception")
        return {"ok": False, "description": str(e)}

def tg_send(text: str, reply_to_message_id: Optional[int] = None, allow_html: bool = True) -> Optional[int]:
    """يرسل الرسالة بأمان، ويعاود الإرسال عند أخطاء HTML."""
    if len(text) > 4096:
        text = text[:4090] + "…"
    base = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_to_message_id:
        base["reply_to_message_id"] = reply_to_message_id

    # 1) HTML أولاً
    if allow_html:
        p = dict(base)
        p["parse_mode"] = "HTML"
        d = _post_tg(p)
        if d.get("ok"):
            return d["result"]["message_id"]
        desc = (d.get("description") or "").lower()
        if "parse entities" in desc or "unsupported start tag" in desc:
            # 2) تهريب الأقواس وإعادة الإرسال
            p = dict(base)
            p["parse_mode"] = "HTML"
            p["text"] = html.escape(text)
            d = _post_tg(p)
            if d.get("ok"):
                return d["result"]["message_id"]
    # 3) إرسال بدون parse_mode
    d = _post_tg(dict(base))
    if d.get("ok"):
        return d["result"]["message_id"]
    return None

def tg_code(title: str, obj: Any, reply_to_message_id: Optional[int] = None) -> Optional[int]:
    """إرسال JSON/تفاصيل داخل <pre> بعد تهريبها."""
    try:
        if not isinstance(obj, str):
            obj = json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        obj = str(obj)
    body = f"<b>{html.escape(title)}</b>\n<pre>{html.escape(str(obj))[:3800]}</pre>"
    return tg_send(body, reply_to_message_id=reply_to_message_id, allow_html=True)

# ================== أدوات المؤشرات ==================
def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()

def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    delta = series.diff()
    up = np.where(delta > 0, delta, 0.0)
    down = np.where(delta < 0, -delta, 0.0)
    roll_up = pd.Series(up, index=series.index).ewm(alpha=1/n, adjust=False).mean()
    roll_down = pd.Series(down, index=series.index).ewm(alpha=1/n, adjust=False).mean()
    rs = roll_up / (roll_down + 1e-9)
    return 100 - (100 / (1 + rs))

def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

# ================== المنصة & الاستراتيجية ==================
def make_exchange():
    cls = getattr(ccxt, EXCHANGE_NAME)
    ex = cls({"enableRateLimit": True})
    ex.options = {"adjustForTimeDifference": True}
    return ex

def fetch_ohlcv(ex, symbol: str, limit: int = 300) -> pd.DataFrame:
    data = ex.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=limit)
    df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "volume"])
    return df

def build_signal(df: pd.DataFrame) -> Dict[str, Any]:
    out: Dict[str, Any] = {"side": None, "reasons": []}
    if len(df) < max(ATR_PERIOD, RSI_PERIOD, 200):
        out["reasons"].append("بيانات غير كافية")
        return out

    df["ema50"] = ema(df["close"], 50)
    df["ema200"] = ema(df["close"], 200)
    df["rsi"] = rsi(df["close"], RSI_PERIOD)
    df["atr"] = atr(df, ATR_PERIOD)

    c1, c2 = df.iloc[-2], df.iloc[-1]  # آخر شمعتين
    close = float(c2["close"]); a = float(c2["atr"])
    ema50, ema200 = float(c2["ema50"]), float(c2["ema200"])
    rsi_prev, rsi_now = float(c1["rsi"]), float(c2["rsi"])
    atr_pct = a / (close + 1e-9)

    trend_up = ema50 > ema200
    trend_down = ema50 < ema200
    rsi_up = (rsi_prev <= 50) and (rsi_now > 50)
    rsi_down = (rsi_prev >= 50) and (rsi_now < 50)
    atr_ok = ATR_MIN <= atr_pct <= ATR_MAX

    if atr_ok and trend_up and rsi_up and (close > ema50):
        out["side"] = "long"
        out["entry"] = close
        out["sl"] = round(close - SL_ATR * a, 2)
        out["tp"] = round(close + TP_ATR * a, 2)
        out["reason"] = f"EMA50>200 + RSI↗ فوق 50 + ATR {atr_pct:.2%}"
        return out
    if atr_ok and trend_down and rsi_down and (close < ema50):
        out["side"] = "short"
        out["entry"] = close
        out["sl"] = round(close + SL_ATR * a, 2)
        out["tp"] = round(close - TP_ATR * a, 2)
        out["reason"] = f"EMA50<200 + RSI↘ تحت 50 + ATR {atr_pct:.2%}"
        return out

    # أسباب عدم وجود إشارة
    if not atr_ok:
        out["reasons"].append(f"ATR خارج المدى ({atr_pct:.2%})")
    if not (trend_up or trend_down):
        out["reasons"].append("اتجاه غير واضح (EMA50≈EMA200)")
    if not (rsi_up or rsi_down):
        out["reasons"].append("لا يوجد كروس RSI حول 50")
    if (trend_up and not (rsi_up and close > ema50)) or (trend_down and not (rsi_down and close < ema50)):
        out["reasons"].append("شروط الدخول لم تكتمل")
    return out

# ================== إدارة الصفقات ==================
open_trades: Dict[str, Dict[str, Any]] = {}
last_no_signal_sent = 0.0

def fmt_price(p: float) -> str:
    return f"{p:.2f}"

def send_entry(symbol: str, sig: Dict[str, Any]) -> Optional[int]:
    side = "🟢 شراء (LONG)" if sig["side"] == "long" else "🔴 بيع (SHORT)"
    msg = (
        f"<b>{symbol}</b>\n"
        f"{side}\n"
        f"السعر: <b>{fmt_price(sig['entry'])}</b>\n"
        f"TP: <b>{fmt_price(sig['tp'])}</b> | SL: <b>{fmt_price(sig['sl'])}</b>\n"
        f"السبب: {html.escape(sig['reason'])}"
    )
    return tg_send(msg, allow_html=True)

def check_tp_sl(symbol: str, price: float):
    trade = open_trades.get(symbol)
    if not trade:
        return
    side = trade["side"]
    mid = trade["entry_msg_id"]
    if side == "long":
        if price >= trade["tp"]:
            tg_send(f"🎯 <b>{symbol}</b> تم تحقيق الهدف عند {fmt_price(trade['tp'])}", reply_to_message_id=mid)
            open_trades.pop(symbol, None)
        elif price <= trade["sl"]:
            tg_send(f"⛔️ <b>{symbol}</b> تفعيل وقف الخسارة عند {fmt_price(trade['sl'])}", reply_to_message_id=mid)
            open_trades.pop(symbol, None)
    else:  # short
        if price <= trade["tp"]:
            tg_send(f"🎯 <b>{symbol}</b> تم تحقيق الهدف (SHORT) عند {fmt_price(trade['tp'])}", reply_to_message_id=mid)
            open_trades.pop(symbol, None)
        elif price >= trade["sl"]:
            tg_send(f"⛔️ <b>{symbol}</b> تفعيل وقف الخسارة (SHORT) عند {fmt_price(trade['sl'])}", reply_to_message_id=mid)
            open_trades.pop(symbol, None)

# ================== حلقة السوق ==================
async def market_loop():
    global last_no_signal_sent
    tg_send(
        "✅ <b>البوت اشتغل</b>\n"
        f"Exchange: {EXCHANGE_NAME}\nTF: {TIMEFRAME}\nPairs: {', '.join(SYMBOLS)}",
        allow_html=True,
    )
    ex = make_exchange()

    while True:
        had_signal = False
        reasons: Dict[str, List[str]] = {}

        for sym in SYMBOLS:
            try:
                df = await asyncio.to_thread(fetch_ohlcv, ex, sym, 300)
            except Exception as e:
                logging.warning(f"{sym}: fetch error: {e}")
                reasons[sym] = [f"خطأ المنصة: {str(e)[:120]}"]
                await asyncio.sleep(0)  # تسليم للحدث
                continue

            sig = build_signal(df)
            last_price = float(df["close"].iloc[-1])

            # راقب أهداف/ستوبات للصفقات المفتوحة
            check_tp_sl(sym, last_price)

            if sig["side"]:
                had_signal = True
                if sym not in open_trades:
                    mid = send_entry(sym, sig)
                    if mid:
                        open_trades[sym] = {
                            "side": sig["side"],
                            "entry": sig["entry"],
                            "tp": sig["tp"],
                            "sl": sig["sl"],
                            "entry_msg_id": mid,
                            "time": time.time(),
                        }
                # لو فيه صفقة قديمة على نفس الزوج، تجاهل إشارة جديدة حتى تغلق
            else:
                reasons[sym] = sig.get("reasons", ["غير محدد"])

            await asyncio.sleep(0.2)  # تهدئة بسيطة بين الأزواج

        now = time.time()
        if (not had_signal) and (now - last_no_signal_sent >= NO_SIGNAL_NOTIFY_EVERY):
            # جهز تقرير أسباب عدم وجود إشارات
            report = {}
            for k, v in reasons.items():
                report[k] = "، ".join(v) if v else "—"
            tg_code("ℹ️ لا توجد إشارات حالياً – الأسباب", report)
            last_no_signal_sent = now

        await asyncio.sleep(SCAN_INTERVAL)

# ================== FastAPI ==================
app = FastAPI()

@app.get("/")
def root():
    return JSONResponse({"ok": True, "service": "trading_bot", "tf": TIMEFRAME, "symbols": SYMBOLS})

@app.get("/scan")
async def manual_scan():
    # فحص يدوي سريع لكل الأزواج (بدون إرسال تيليجرام)
    try:
        ex = make_exchange()
        out = {}
        for s in SYMBOLS:
            try:
                df = await asyncio.to_thread(fetch_ohlcv, ex, s, 300)
                sig = build_signal(df)
                out[s] = sig
            except Exception as e:
                out[s] = {"error": str(e)}
        return out
    except Exception as e:
        return {"error": str(e)}

@app.on_event("startup")
async def on_start():
    asyncio.create_task(market_loop())
    logging.info("Starting bot… Telegram: ON")

# ================== تشغيل محلي/Render ==================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
