# trading_bot_0_3.py  -- إصدار مع تشخيص الأسباب
# استراتيجية: تقاطع EMA20/EMA50 + فلتر RSI + ملخص "لا إشارات"
# يعمل FastAPI ويُرسل إشعارات تيليجرام

import os
import time
import math
import asyncio
import logging
from typing import Dict, List, Optional, Tuple

import ccxt
import pandas as pd
import requests
from fastapi import FastAPI

# ============== عَدِّل هُنا ==================
BOT_TOKEN: str = "123456:ABCDEF_PUT_YOUR_TOKEN"   # توكن تيليجرام
CHAT_ID: str   = "-1001234567890"                 # آي-دي قناة/مجموعة/شخص

SYMBOLS: List[str] = [
    "BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT","XRP/USDT","DOGE/USDT"
]
TIMEFRAME: str = "5m"
SCAN_EVERY_SEC: int = 60
EMA_FAST: int = 20
EMA_SLOW: int = 50
RSI_LEN: int = 14
RSI_BUY_MIN: float = 50.0      # فلتر شراء
RSI_SELL_MAX: float = 50.0      # فلتر بيع

# كم مرّة نرسل ملخص "لا إشارات"
NO_SIGNAL_SUMMARY_EVERY_SEC: int = 600
# ============================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
app = FastAPI(title="trading_bot", version="0.3")

exchange = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})

# ذاكرة لمنع التكرار والمتابعة
last_side: Dict[str, Optional[str]] = {}
last_price: Dict[str, float] = {}
last_rsi: Dict[str, float] = {}
last_time: Dict[str, int] = {}
last_summary_ts: float = 0.0
no_signal_streak: int = 0

# ---------- أدوات ----------
def tg_send(text: str) -> bool:
    """إرسال تيليجرام مع لوج خطأ واضح إذا المعرّفات ناقصة."""
    if "PUT_YOUR_TOKEN" in BOT_TOKEN or not BOT_TOKEN or not CHAT_ID:
        logging.error("❌ BOT_TOKEN/CHAT_ID مفقود. عدّل القيم داخل الملف.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=12,
        )
        ok = r.status_code == 200 and r.json().get("ok", False)
        if not ok:
            logging.error(f"Telegram error {r.status_code}: {r.text}")
        return ok
    except Exception as e:
        logging.exception(f"Telegram exception: {e}")
        return False

def safe_df(ohlcv: List[List[float]]) -> Optional[pd.DataFrame]:
    if not ohlcv or len(ohlcv) < max(EMA_SLOW, RSI_LEN) + 2:
        return None
    df = pd.DataFrame(ohlcv, columns=["t","o","h","l","c","v"]).dropna().reset_index(drop=True)
    return df if len(df) >= max(EMA_SLOW, RSI_LEN) + 2 else None

def calc_rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0).ewm(alpha=1/length, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1/length, adjust=False).mean().replace(0, 1e-12)
    rs = up / dn
    return 100 - (100 / (1 + rs))

def analyze(df: pd.DataFrame) -> Dict:
    c = df["c"].astype(float)
    ema_fast = c.ewm(span=EMA_FAST, adjust=False).mean()
    ema_slow = c.ewm(span=EMA_SLOW, adjust=False).mean()
    rsi = calc_rsi(c, RSI_LEN)

    prev_up = ema_fast.iloc[-2] <= ema_slow.iloc[-2]
    now_up  = ema_fast.iloc[-1] >  ema_slow.iloc[-1]
    prev_dn = ema_fast.iloc[-2] >= ema_slow.iloc[-2]
    now_dn  = ema_fast.iloc[-1] <  ema_slow.iloc[-1]

    cross_up = prev_up and now_up
    cross_dn = prev_dn and now_dn

    price = float(c.iloc[-1])
    r = float(rsi.iloc[-1])
    ef = float(ema_fast.iloc[-1])

    rsi_ok_buy  = r >= RSI_BUY_MIN
    rsi_ok_sell = r <= RSI_SELL_MAX
    above_ef    = price > ef
    below_ef    = price < ef

    buy  = cross_up and rsi_ok_buy and above_ef
    sell = cross_dn and rsi_ok_sell and below_ef

    reasons: List[str] = []
    cross_label = "up" if cross_up else ("down" if cross_dn else "none")

    # تشخيص دقيق لعدم وجود إشارة
    if not buy and not sell:
        if not cross_up and not cross_dn:
            reasons.append("no_cross")
        if cross_up and not rsi_ok_buy:
            reasons.append(f"RSI<{RSI_BUY_MIN}")
        if cross_dn and not rsi_ok_sell:
            reasons.append(f"RSI>{RSI_SELL_MAX}")
        if cross_up and rsi_ok_buy and not above_ef:
            reasons.append("price<=EMA_fast")
        if cross_dn and rsi_ok_sell and not below_ef:
            reasons.append("price>=EMA_fast")
        if not reasons:
            reasons.append("filters_blocked")

    return {
        "buy": buy, "sell": sell, "price": price, "rsi": r,
        "ema_fast": ef, "ema_slow": float(ema_slow.iloc[-1]),
        "cross": cross_label, "reasons": reasons,
        "flags": {
            "cross_up": cross_up, "cross_down": cross_dn,
            "rsi_ok_buy": rsi_ok_buy, "rsi_ok_sell": rsi_ok_sell,
            "above_ema_fast": above_ef, "below_ema_fast": below_ef,
        }
    }

def fmt_price(p: float) -> str:
    if p == 0 or math.isnan(p): return "0"
    if p >= 1000: return f"{p:,.0f}"
    if p >= 1:    return f"{p:,.2f}"
    return f"{p:.6f}"

async def fetch_ohlcv(sym: str) -> Optional[pd.DataFrame]:
    try:
        data = await asyncio.to_thread(exchange.fetch_ohlcv, sym, timeframe=TIMEFRAME, limit=200)
        return safe_df(data)
    except Exception as e:
        logging.warning(f"fetch_ohlcv {sym} err: {e}")
        return None

def build_signal_msg(side: str, sym: str, d: Dict) -> str:
    return (
        f"📣 *{side}* {sym}\n"
        f"سعر: `{fmt_price(d['price'])}`  ·  RSI({RSI_LEN}): `{round(d['rsi'],1)}`\n"
        f"EMA{EMA_FAST}/{EMA_SLOW}  ·  TF: `{TIMEFRAME}`"
    )

def summarize_no_signals(results: Dict[str, Dict]) -> str:
    # عدّاد لأسباب عدم الإشارة
    counts: Dict[str, int] = {}
    lines: List[str] = []
    for sym, d in results.items():
        if not d.get("ok"): 
            counts["no_data"] = counts.get("no_data", 0) + 1
            lines.append(f"- {sym}: no_data")
            continue
        if d["buy"] or d["sell"]:
            continue
        rs = d.get("reasons", []) or ["unknown"]
        for r in rs:
            counts[r] = counts.get(r, 0) + 1
        rsi = round(d["rsi"],1) if "rsi" in d else "?"
        ef = d.get("ema_fast")
        hint = f"RSI={rsi}, price {('>' if d['flags'].get('above_ema_fast') else '<=')} EMA{EMA_FAST}" if ef is not None else f"RSI={rsi}"
        lines.append(f"- {sym}: {', '.join(rs)} ({hint})")

    # أعلى 3 أسباب
    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:3]
    top_txt = ", ".join([f"{k}={v}" for k,v in top]) if top else "n/a"

    # لا نزيد الطول كثير: نعرض أول 6 أسطر أمثلة
    sample = "\n".join(lines[:6]) if lines else "- لا توجد أزواج مفحوصة"
    return (
        "ℹ️ *لا إشارات في هذه الدورة*\n"
        f"الأسباب الشائعة: {top_txt}\n"
        f"أمثلة:\n{sample}"
    )

# ---------- حلقة العمل ----------
async def scan_once() -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for sym in SYMBOLS:
        df = await fetch_ohlcv(sym)
        if df is None:
            out[sym] = {"ok": False, "reason": "no_data"}
            continue
        try:
            d = analyze(df)
            d["ok"] = True
            out[sym] = d
        except Exception as e:
            logging.warning(f"analyze {sym} err: {e}")
            out[sym] = {"ok": False, "reason": "analyze_err"}
    return out

async def worker_loop():
    global last_summary_ts, no_signal_streak
    await asyncio.sleep(2)

    # رسالة تشغيل مفصّلة
    started = (
        f"✅ البوت اشتغل.\n"
        f"TF: `{TIMEFRAME}` · EMA: `{EMA_FAST}/{EMA_SLOW}` · RSI_len: `{RSI_LEN}`\n"
        f"فلتر شراء: RSI≥{RSI_BUY_MIN} · فلتر بيع: RSI≤{RSI_SELL_MAX}\n"
        f"أزواج: {', '.join(SYMBOLS)}\n"
        f"سأرسل ملخص *لا إشارات* كل ~{NO_SIGNAL_SUMMARY_EVERY_SEC//60} دقائق إذا لم تظهر إشارات."
    )
    tg_send(started)

    while True:
        t0 = time.time()
        res = await scan_once()

        any_signal = False
        for sym, d in res.items():
            if not d.get("ok"): 
                logging.info(f"{sym}: {d.get('reason')}")
                continue

            last_price[sym] = float(d["price"])
            last_rsi[sym]   = float(d["rsi"])

            side = "BUY" if d["buy"] else ("SELL" if d["sell"] else None)
            if side and last_side.get(sym) != side:
                any_signal = True
                last_side[sym] = side
                last_time[sym] = int(time.time())
                tg_send(build_signal_msg(side, sym, d))

        if any_signal:
            no_signal_streak = 0
        else:
            no_signal_streak += 1
            # ملخص “لا إشارات” بمعدل محدد
            now = time.time()
            if now - last_summary_ts >= NO_SIGNAL_SUMMARY_EVERY_SEC:
                summary = summarize_no_signals(res)
                tg_send(summary + f"\n(سلسلة بلا إشارات: {no_signal_streak} دورة)")
                last_summary_ts = now

        # انتظار حتى يكمل المعدّل
        elapsed = time.time() - t0
        await asyncio.sleep(max(1, SCAN_EVERY_SEC - int(elapsed)))

# ---------- FastAPI ----------
@app.on_event("startup")
async def on_startup():
    asyncio.create_task(worker_loop())

@app.get("/")
def root():
    return {
        "ok": True,
        "service": "trading_bot",
        "symbols": SYMBOLS,
        "tf": TIMEFRAME,
        "ema": [EMA_FAST, EMA_SLOW],
        "rsi_len": RSI_LEN,
        "filters": {"buy_min": RSI_BUY_MIN, "sell_max": RSI_SELL_MAX},
    }

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/force")
async def force_scan():
    """مسح فوري مع إرجاع الأسباب/الأعلام لكل زوج (لا يرسل تيليجرام)."""
    res_raw = await scan_once()
    return {"ok": True, "results": res_raw}

# ---------- التشغيل ----------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
