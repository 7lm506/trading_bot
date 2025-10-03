# trading_bot_0_3.py
import os, time, math, asyncio, traceback
from typing import List, Optional, Tuple, Dict
import aiohttp, ccxt
from fastapi import FastAPI
import uvicorn

app = FastAPI(title="trading_bot")

# ========= الإعدادات =========
# طلبت هاردكود: (تحذير: غير آمن لو الكود عام)
HARDCODE_BOT_TOKEN = "8130568386:AAGmpxKQw1XhqNjtj2OBzJ_-e3_vn0FE5Bs"
HARDCODE_CHAT_ID   = "8429537293"
HARDCODE_TOPIC_ID  = ""       # اختياري (message_thread_id)
HARDCODE_ROOT_REPLY_MESSAGE_ID = ""  # اختياري: نخلي كل الرسائل Reply على رسالة ثابتة

BOT_TOKEN = os.getenv("BOT_TOKEN", HARDCODE_BOT_TOKEN).strip()
CHAT_ID   = os.getenv("CHAT_ID", HARDCODE_CHAT_ID).strip()
TOPIC_ID  = os.getenv("TOPIC_ID", HARDCODE_TOPIC_ID).strip()
ROOT_REPLY_MESSAGE_ID = os.getenv("ROOT_REPLY_MESSAGE_ID", HARDCODE_ROOT_REPLY_MESSAGE_ID).strip()

EXCHANGE_ID = os.getenv("EXCHANGE", "binance").strip()
TIMEFRAME   = os.getenv("TIMEFRAME", "5m").strip()
SYMBOLS     = [s.strip() for s in os.getenv("SYMBOLS", "BTC/USDT,ETH/USDT").split(",") if s.strip()]
LOOKBACK    = int(os.getenv("LOOKBACK", "200"))
MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT", "0.002"))  # 0.2%
TP_PCT      = float(os.getenv("TP_PCT", "0.01"))        # 1%
SL_PCT      = float(os.getenv("SL_PCT", "0.005"))       # 0.5%
SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "30"))
NO_SIGNAL_EVERY_MIN = int(os.getenv("NO_SIGNAL_EVERY_MIN", "10"))

# ========= مؤشرات بسيطة =========
def ema(series: List[float], period: int) -> List[float]:
    k = 2.0 / (period + 1)
    out, e = [], None
    for v in series:
        e = v if e is None else (v - e) * k + e
        out.append(e)
    return out

def rsi(series: List[float], period: int = 14) -> List[Optional[float]]:
    rsis = [None]*len(series)
    gains, losses = [], []
    for i in range(1, len(series)):
        ch = series[i] - series[i-1]
        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))
        if i >= period:
            g = sum(gains[i-period:i]) / period
            l = sum(losses[i-period:i]) / period
            rsis[i] = 100.0 if l == 0 else 100 - 100/(1 + g/l)
    return rsis

def atr(high: List[float], low: List[float], close: List[float], period: int = 14) -> List[Optional[float]]:
    trs, out = [], []
    for i in range(len(close)):
        tr = (high[i]-low[i]) if i==0 else max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
        trs.append(tr)
        out.append(None if i < period else sum(trs[i-period+1:i+1]) / period)
    return out

# ========= Telegram =========
async def tg_send(text: str, reply_to: Optional[int] = None) -> Optional[int]:
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram OFF (BOT_TOKEN/CHAT_ID مفقود)")
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if TOPIC_ID:
        payload["message_thread_id"] = int(TOPIC_ID)
    if reply_to:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = True
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload, timeout=15) as r:
            data = await r.json()
            if not data.get("ok"):
                print("Telegram error:", data)
                return None
            return data["result"]["message_id"]

# ========= حالة الصفقات =========
class Trade:
    def __init__(self, symbol: str, entry: float, sl: float, tp: float, msg_id: Optional[int]):
        self.symbol = symbol
        self.entry, self.sl, self.tp = entry, sl, tp
        self.msg_id = msg_id
        self.active = True

trades: Dict[str, Trade] = {}
last_no_signal_ts = 0.0
last_no_signal_reason = ""

# ========= بيانات + إستراتيجية =========
def fetch_candles(exchange, symbol: str, tf: str, limit: int):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
    o = [x[1] for x in ohlcv]; h = [x[2]]*0 or [x[2] for x in ohlcv]  # أسرع من نسخ إضافي
    l = [x[3] for x in ohlcv]; c = [x[4] for x in ohlcv]
    return o, h, l, c

def analyze_signal(opens, highs, lows, closes) -> Tuple[bool, str]:
    if len(closes) < 50: return False, "بيانات غير كافية"
    ema9, ema21 = ema(closes, 9), ema(closes, 21)
    rs, _atr = rsi(closes, 14), atr(highs, lows, closes, 14)
    c, a = closes[-1], _atr[-1]
    if a is None or (a/max(c,1e-9)) < MIN_ATR_PCT: return False, f"ATR منخفض ({(a or 0)/max(c,1e-9):.4%} < {MIN_ATR_PCT:.2%})"
    if not (ema9[-2] < ema21[-2] and ema9[-1] > ema21[-1]): return False, "لا يوجد تقاطع EMA9↑EMA21"
    if rs[-1] is None or rs[-1] <= 50: return False, f"RSI <= 50 ({rs[-1]:.1f} إن وُجد)"
    return True, ""

async def scan_loop():
    global last_no_signal_ts, last_no_signal_reason
    ex = getattr(ccxt, EXCHANGE_ID)({'enableRateLimit': True})
    root_reply = int(ROOT_REPLY_MESSAGE_ID) if ROOT_REPLY_MESSAGE_ID else None

    try:
        await tg_send(
            f"✅ <b>البوت اشتغل</b>\n"
            f"Exchange: <code>{EXCHANGE_ID}</code>\nTF: <code>{TIMEFRAME}</code>\n"
            f"Pairs: <code>{', '.join(SYMBOLS)}</code>",
            reply_to=root_reply
        )

        while True:
            had_signal, reasons = False, []
            for sym in SYMBOLS:
                try:
                    o,h,l,c = await asyncio.to_thread(fetch_candles, ex, sym, TIMEFRAME, LOOKBACK)
                    signal, reason = analyze_signal(o,h,l,c)
                    price = c[-1]

                    # مراقبة صفقات قائمة
                    tr = trades.get(sym)
                    if tr and tr.active:
                        if price >= tr.tp:
                            tr.active = False
                            await tg_send(
                                f"🥳 <b>TP hit</b> {sym}\nEntry: {tr.entry:.6g}\nTP: {tr.tp:.6g}\n"
                                f"الربح: {(tr.tp-tr.entry)/tr.entry:.2%}", reply_to=tr.msg_id
                            )
                        elif price <= tr.sl:
                            tr.active = False
                            await tg_send(
                                f"🛑 <b>SL hit</b> {sym}\nEntry: {tr.entry:.6g}\nSL: {tr.sl:.6g}\n"
                                f"الخسارة: {(tr.sl-tr.entry)/tr.entry:.2%}", reply_to=tr.msg_id
                            )

                    # فتح صفقة جديدة عند الإشارة
                    if (not tr or not tr.active) and signal:
                        entry = price
                        sl = entry * (1 - SL_PCT)
                        tp = entry * (1 + TP_PCT)
                        msg_id = await tg_send(
                            "📈 <b>إشارة شراء</b> {0}\nEntry: <code>{1:.6g}</code>\n"
                            "SL: <code>{2:.6g}</code> ({3:.2%})\nTP: <code>{4:.6g}</code> ({5:.2%})\n"
                            "فلتر: EMA9↑EMA21 + RSI>50 + ATR≥{6:.2%}".format(
                                sym, entry, sl, -SL_PCT, tp, TP_PCT, MIN_ATR_PCT
                            ),
                            reply_to=root_reply
                        )
                        trades[sym] = Trade(sym, entry, sl, tp, msg_id)
                        had_signal = True
                    elif not signal:
                        reasons.append(f"{sym}: {reason}")
                except Exception as e:
                    reasons.append(f"{sym}: خطأ {e}")

            # لا إشارات؟ أرسل أسباب — كل X دقائق أو عند تغيّر السبب
            now = time.time()
            if not had_signal and reasons:
                text = "\n".join(reasons[:8])
                if (now - last_no_signal_ts) >= NO_SIGNAL_EVERY_MIN*60 and text != last_no_signal_reason:
                    await tg_send("ℹ️ <b>لا توجد إشارات حالياً</b>\n" + text, reply_to=root_reply)
                    last_no_signal_ts, last_no_signal_reason = now, text

            await asyncio.sleep(SCAN_SECONDS)
    except Exception:
        err = traceback.format_exc()
        print("FATAL LOOP ERROR:\n", err)
        await tg_send("❌ خطأ في حلقة المسح:\n<code>"+(err[-3500:])+"</code>", reply_to=root_reply)
    finally:
        try: ex.close()
        except Exception: pass

# ========= FastAPI =========
@app.on_event("startup")
async def _startup():
    print("Starting bot… Telegram:", "ON" if BOT_TOKEN and CHAT_ID else "OFF")
    asyncio.create_task(scan_loop())

@app.get("/")
def home():
    return {"ok": True, "exchange": EXCHANGE_ID, "symbols": SYMBOLS, "tf": TIMEFRAME}

@app.get("/health")
def health(): return {"status": "ok"}

@app.post("/test")
async def test():
    mid = await tg_send("🔔 Test message from /test", reply_to=(int(ROOT_REPLY_MESSAGE_ID) if ROOT_REPLY_MESSAGE_ID else None))
    return {"sent": bool(mid), "message_id": mid}

if __name__ == "__main__":
    uvicorn.run("trading_bot_0_3:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
