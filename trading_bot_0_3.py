# trading_bot_0_3.py
# -------------------
# FastAPI + Telegram (توكن و Chat ID داخل الملف)
# شغّال على Render ويقرأ منفذ PORT من المتغير البيئي لو موجود.

import os
import logging
import asyncio
from typing import Optional

import aiohttp
from fastapi import FastAPI, HTTPException, Query, Body, Header
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

app = FastAPI(title="trading_bot")

# =========[ إعدادات تُعدلها هنا ]=========
# حط قيمك الفعلية هنا (نسخ/لصق من BotFather و من تيليجرام)
TELEGRAM_BOT_TOKEN: str = "123456:ABCDEF-your-token-here"   # <-- عدّلها
TELEGRAM_CHAT_ID: str = "-1001234567890"                    # <-- عدّلها (خاص = رقم موجب, جروب سوبر/قناة = يبدأ -100)
TELEGRAM_ENABLED: bool = True                               # عطلها لو حاب
TELEGRAM_TEST_SECRET: str = "test123"                       # سر بسيط لحماية مسار الاختبار

# رسالة بدء بسيطة تُرسل عند الإقلاع (اختياري)
SEND_STARTUP_PING: bool = True

# ==========================================


async def send_telegram(
    text: str,
    parse_mode: Optional[str] = None,
    disable_web_page_preview: bool = True,
    disable_notification: bool = False,
) -> bool:
    """إرسال رسالة تيليجرام باستخدام التوكن والـ chat_id أعلاه."""
    if not TELEGRAM_ENABLED:
        logging.info("Telegram disabled; not sending.")
        return False

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or "your-token-here" in TELEGRAM_BOT_TOKEN:
        logging.error("Telegram config missing or not set in code.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": disable_web_page_preview,
        "disable_notification": disable_notification,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                status = resp.status
                try:
                    data = await resp.json()
                except Exception:
                    data = {"raw": await resp.text()}
                if status != 200 or not data.get("ok", False):
                    logging.error(f"Telegram send failed: status={status}, body={data}")
                    return False
                mid = data.get("result", {}).get("message_id")
                logging.info(f"Telegram sent ✓ (message_id={mid})")
                return True
    except Exception as e:
        logging.exception(f"Telegram exception: {e}")
        return False


@app.on_event("startup")
async def _startup():
    cfg = {
        "enabled": TELEGRAM_ENABLED,
        "token_set": bool(TELEGRAM_BOT_TOKEN and "your-token-here" not in TELEGRAM_BOT_TOKEN),
        "chat_id_set": bool(TELEGRAM_CHAT_ID),
    }
    logging.info(f"Startup | Telegram cfg: {cfg}")
    if SEND_STARTUP_PING and cfg["enabled"] and cfg["token_set"] and cfg["chat_id_set"]:
        # نرسل إشعار بدء بسيط
        await send_telegram("✅ البوت اشتغل على Render.")


@app.get("/")
async def root():
    return {"status": "ok", "service": "trading_bot", "telegram_enabled": TELEGRAM_ENABLED}


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/test_telegram")
async def test_telegram(
    secret: str = Query(..., description="لازم يطابق TELEGRAM_TEST_SECRET"),
    text: str = Query("🚀 Test from Render"),
):
    if secret != TELEGRAM_TEST_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    ok = await send_telegram(text)
    return {"ok": ok}


@app.post("/notify")
async def notify(
    text: str = Body(..., embed=True),
    secret: str = Header(None, convert_underscores=False),  # استخدم هيدر: x-secret
    x_secret: Optional[str] = Header(None, alias="x-secret"),
):
    # نقبل إما Header باسم 'secret' أو 'x-secret'
    provided = secret or x_secret
    if provided != TELEGRAM_TEST_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    ok = await send_telegram(text)
    return {"ok": ok}


# مثال إشعار دخول/خروج (وهمية كدليل استخدام):
@app.post("/signal")
async def signal(
    side: str = Query(..., regex="^(buy|sell)$"),
    symbol: str = Query("BTC/USDT"),
    price: float = Query(0.0),
    secret: str = Query(...),
):
    if secret != TELEGRAM_TEST_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    msg = f"📣 إشارة **{side.upper()}**\nزوج: {symbol}\nالسعر: {price}"
    ok = await send_telegram(msg, parse_mode="Markdown")
    return {"ok": ok}


# تشغيل Uvicorn محليًا أو على Render:
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
