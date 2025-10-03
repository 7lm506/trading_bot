# trading_bot.py
# =========================
# تشغيل محلّي:
#   uvicorn trading_bot:app --host 0.0.0.0 --port 8000 --workers 1
#
# على Render:
#   اضبط Command إلى:
#   uvicorn trading_bot:app --host 0.0.0.0 --port $PORT --workers 1
#
# متطلبات:
#   pip install fastapi uvicorn ccxt pandas numpy
#
# ملاحظات:
# - يعالج خطأ pandas "not in index" عبر عدم استخدام قائمة العوائد للفهرسة مطلقًا.
# - يحسب العوائد، ينظّف NaN/Inf، ويحوّلها إلى numpy vector للاستدلال/القياس.
# - يوفّر مسار /health ومسار /scan مع بارامترات للتحكم.

import os
import time
import logging
from typing import List, Optional, Dict, Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

import ccxt

# إعداد اللوجينغ
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("trading_bot")

# إعدادات افتراضية عبر المتغيرات البيئية
DEFAULT_EXCHANGE = os.getenv("EXCHANGE", "bybit")  # مثال: bybit | binanceusdm | okx | gateio
DEFAULT_TIMEFRAME = os.getenv("TIMEFRAME", "1m")
DEFAULT_OHLCV_LIMIT = int(os.getenv("OHLCV_LIMIT", "500"))
DEFAULT_VECTOR_LENGTH = int(os.getenv("VECTOR_LENGTH", "120"))
DEFAULT_MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "60"))


# ---------- أدوات مساعدة ----------

def make_exchange(name: str):
    """
    يبني كائن ccxt Exchange مع معدل طلبات مفعّل.
    لتبديل البورصة استعمل متغير البيئة EXCHANGE.
    """
    name = (name or DEFAULT_EXCHANGE).lower()
    if not hasattr(ccxt, name):
        raise ValueError(f"Exchange '{name}' غير مدعوم في ccxt")
    klass = getattr(ccxt, name)
    exchange = klass({
        "enableRateLimit": True,
        "options": {},
    })

    # تلميحات لبعض البورصات للعقود الدائمة
    # Bybit: type swap تلقائيًا
    if name in ("binanceusdm", "binance"):
        # binanceusdm = عقود USDT-M؛ binance (spot) لا يحتوي :USDT
        exchange.options["defaultType"] = "swap"
    if name == "okx":
        exchange.options["defaultType"] = "swap"
    if name == "gateio":
        exchange.options["defaultType"] = "swap"

    return exchange


def list_linear_usdt_symbols(exchange) -> List[str]:
    """
    يُرجع قائمة بالرموز من نوع عقود دائمة خطية (USDT-settled)،
    بصيغة ccxt مثل: BTC/USDT:USDT.
    """
    markets = exchange.load_markets()
    syms: List[str] = []
    for m in markets.values():
        try:
            if not m.get("active", True):
                continue
            if not m.get("swap", False):
                continue
            if m.get("quote") != "USDT":
                continue
            # نفضّل التسوية بالـ USDT (Linear)
            if m.get("settle") and m.get("settle") != "USDT":
                continue
            symbol = m.get("symbol")
            if not symbol:
                continue
            # صيغة ccxt للعقود الخطية غالباً تحوي ':USDT'
            # لكن ليس شرطاً في كل البورصات—لذا لا نفرضه.
            syms.append(symbol)
        except Exception:
            # تجاهل أي سوق غريب
            continue

    # فرز لتناسق النتائج
    syms = sorted(set(syms))
    return syms


def fetch_ohlcv_df(exchange, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    """
    يسحب OHLCV ويُرجعه DataFrame مرتب تصاعدياً بالوقت.
    """
    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    if not raw or len(raw[0]) < 6:
        raise ValueError(f"لا توجد بيانات OHLCV كافية للرمز {symbol}")
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)
    # تأكد أن الأسعار float
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def vectorize_returns(df: pd.DataFrame, n: int) -> Optional[np.ndarray]:
    """
    يُحوّل سلسلة أسعار الإغلاق إلى متجه عوائد بطول n.
    لا يقوم بأي فهرسة DataFrame باستخدام هذه القائمة — فقط يُرجع numpy array.
    """
    close = df["close"].astype(float)
    # احسب العوائد كنسبة مئوية بين الشموع
    ret = close.pct_change()
    # نظّف القيم غير الصالحة
    ret = ret.replace([np.inf, -np.inf], np.nan).dropna()

    if len(ret) < n:
        return None

    # لا تفهرس df بهذه القائمة! فقط أعدها كمصفوفة لاستخدامها في الحسابات
    vec = ret.iloc[-n:].to_numpy(dtype=float)
    return vec


def simple_score(vec: np.ndarray) -> float:
    """
    درجة مبسطة لقياس الزخم/التذبذب — للتوضيح فقط.
    (يمكن استبدالها بنموذجك الحقيقي).
    """
    if vec.size == 0:
        return float("nan")
    last = vec[-1]
    vol = np.std(vec) or 1e-9
    # Momentum-to-Volatility
    score = float(last / vol)
    return score


def scan_symbols(
    exchange,
    symbols: List[str],
    timeframe: str,
    limit: int,
    n: int,
    max_symbols: int,
) -> List[Dict[str, Any]]:
    """
    يمسح مجموعة رموز، يُرجع قائمة نتائج تحتوي الدرجة وآخر عائد، إلخ.
    """
    results: List[Dict[str, Any]] = []
    count = 0
    for symbol in symbols:
        if count >= max_symbols:
            break
        try:
            df = fetch_ohlcv_df(exchange, symbol, timeframe=timeframe, limit=limit)
            vec = vectorize_returns(df, n=n)
            if vec is None:
                logger.info(f"تجاوز {symbol}: بيانات العوائد غير كافية (n={n})")
                continue

            score = simple_score(vec)
            res = {
                "symbol": symbol,
                "score": round(score, 6),
                "last_return": round(float(vec[-1]), 8),
                "vector_len": int(len(vec)),
                "last_ts": df["timestamp"].iloc[-1].isoformat(),
            }
            results.append(res)
            count += 1

        except ccxt.NetworkError as e:
            logger.warning(f"مشكلة شبكة للرمز {symbol}: {e}")
        except ccxt.RateLimitExceeded as e:
            logger.warning(f"تخطّي حد المعدّل، سننام قليلًا... {e}")
            time.sleep(1.0)
        except Exception:
            # هذه السطور تستبدل رسائل "not in index" بمكدس مفيد يذكر السطر المسبب
            logger.exception(f"scan error {symbol}")

    return results


# ---------- تطبيق FastAPI ----------

app = FastAPI(title="trading_bot", version="0.4.0")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/scan")
def scan(
    exchange_name: str = Query(DEFAULT_EXCHANGE, description="اسم البورصة في ccxt، مثل bybit أو binanceusdm"),
    timeframe: str = Query(DEFAULT_TIMEFRAME, description="الإطار الزمني لـ OHLCV، مثل 1m/5m/15m/1h"),
    limit: int = Query(DEFAULT_OHLCV_LIMIT, ge=100, le=1500, description="عدد شموع OHLCV المطلوب سحبها"),
    n: int = Query(DEFAULT_VECTOR_LENGTH, ge=10, le=1000, description="طول متجه العوائد"),
    max_symbols: int = Query(DEFAULT_MAX_SYMBOLS, ge=1, le=500, description="أقصى عدد رموز في المسح"),
):
    """
    يشغّل مسحًا سريعًا ويُرجع النتائج JSON.
    مثال:
      GET /scan?exchange_name=bybit&timeframe=1m&limit=500&n=120&max_symbols=50
    """
    try:
        ex = make_exchange(exchange_name)
        symbols = list_linear_usdt_symbols(ex)
        if not symbols:
            return JSONResponse(
                status_code=400,
                content={"error": f"لا توجد رموز swap USDT في '{exchange_name}'"},
            )
        results = scan_symbols(
            ex,
            symbols=symbols,
            timeframe=timeframe,
            limit=limit,
            n=n,
            max_symbols=max_symbols,
        )
        payload = {
            "exchange": exchange_name,
            "timeframe": timeframe,
            "limit": limit,
            "vector_n": n,
            "scanned": len(results),
            "results": results,
        }
        return payload
    except Exception as e:
        logger.exception("فشل مسار /scan")
        return JSONResponse(status_code=500, content={"error": str(e)})


# تشغيل محلي/على Render
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("trading_bot:app", host="0.0.0.0", port=port, reload=False, workers=1)
