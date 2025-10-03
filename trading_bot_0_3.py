# trading_bot_0_3.py
import os
import asyncio
import logging
from typing import List, Dict, Any, Optional

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

import ccxt.async_support as ccxt_async  # النسخة غير المتزامنة
# ملاحظة: نحن لا نستخدم ccxt.pro (ويب سوكِت)، فقط HTTP مع تمكين تحديد المعدل


# =========================
# إعدادات عامة وتهيئة لوجز
# =========================
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("trading_bot")

DEFAULT_EXCHANGE = os.getenv("EXCHANGE", "binance")
DEFAULT_QUOTE = os.getenv("QUOTE", "USDT")
DEFAULT_TIMEFRAME = os.getenv("TIMEFRAME", "1h")
DEFAULT_SCAN_LIMIT = int(os.getenv("SCAN_LIMIT", "120"))  # حد أقصى للنتائج الراجعة
DEFAULT_MIN_QUOTE_VOL = float(os.getenv("MIN_QUOTE_VOL", "100000"))  # بالـ USDT
REQUEST_TIMEOUT_MS = int(os.getenv("REQUEST_TIMEOUT_MS", "20000"))  # 20 ثانية

# =========================
# FastAPI App
# =========================
app = FastAPI(title="trading_bot", version="0.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================
# أدوات مساعدة
# =========================
def safe_div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    try:
        if b == 0:
            return None
        return a / b
    except Exception:
        return None


def compute_indicators_from_ohlcv(ohlcv: List[List[float]]) -> Dict[str, Any]:
    """
    ohlcv = [[timestamp, open, high, low, close, volume], ...]
    يعيد آخر قيمة لمؤشرات شائعة: EMA20/50/200 و RSI14 بالإضافة إلى الإغلاق الأخير.
    """
    if not ohlcv or len(ohlcv) < 50:  # نحتاج بيانات كافية على الأقل للمؤشرات
        return {"close": None, "ema20": None, "ema50": None, "ema200": None, "rsi14": None}

    df = pd.DataFrame(
        ohlcv,
        columns=["ts", "open", "high", "low", "close", "volume"],
    )

    close = df["close"].astype(float)

    # EMA
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean() if len(close) >= 200 else pd.Series([np.nan] * len(close))

    # RSI 14
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    roll_up = gain.rolling(window=14, min_periods=14).mean()
    roll_down = loss.rolling(window=14, min_periods=14).mean()
    rs = roll_up / roll_down
    rsi14 = 100.0 - (100.0 / (1.0 + rs))

    return {
        "close": float(close.iloc[-1]) if pd.notna(close.iloc[-1]) else None,
        "ema20": float(ema20.iloc[-1]) if pd.notna(ema20.iloc[-1]) else None,
        "ema50": float(ema50.iloc[-1]) if pd.notna(ema50.iloc[-1]) else None,
        "ema200": float(ema200.iloc[-1]) if pd.notna(ema200.iloc[-1]) else None,
        "rsi14": float(rsi14.iloc[-1]) if pd.notna(rsi14.iloc[-1]) else None,
    }


async def make_exchange(exchange_id: str):
    """
    إنشاء كائن البورصة من ccxt (نسخة async)، تمكين تحديد المعدل، وضبط المهلة.
    """
    if not hasattr(ccxt_async, exchange_id):
        raise HTTPException(status_code=400, detail=f"Exchange '{exchange_id}' is not supported by ccxt.")

    exchange_class = getattr(ccxt_async, exchange_id)
    exchange = exchange_class(
        {
            "enableRateLimit": True,
            "timeout": REQUEST_TIMEOUT_MS,
            "options": {
                "defaultType": "spot",  # نعتمد السوق الفوري
            },
        }
    )
    return exchange


def is_spot_usdt_symbol(symbol: str, quote: str = "USDT") -> bool:
    """
    نستبعد رموز العقود الدائمة مثل 'BTC/USDT:USDT' (وجود نقطتين ':')
    ونحصرها في أزواج سبوت وتنتهي بـ '/USDT'
    """
    if ":" in symbol:
        return False
    return symbol.upper().endswith(f"/{quote.upper()}")


def ticker_24h_change(t: Dict[str, Any]) -> Optional[float]:
    """
    يحسب نسبة التغير 24 ساعة إن أمكن، يعتمد على (last, open) أو نسبة 'percentage' إن وجدت.
    """
    last = t.get("last")
    open_ = t.get("open")
    pct = t.get("percentage")

    if pct is not None:
        try:
            return float(pct)
        except Exception:
            pass

    val = safe_div((last - open_) if (last is not None and open_ is not None) else None, open_)
    return float(val * 100) if val is not None else None


def ticker_quote_volume(t: Dict[str, Any]) -> Optional[float]:
    """
    يقدّر حجم التداول بالعملة المقابلة (Quote Volume).
    يفضّل 'quoteVolume'، وإلا يحاول baseVolume * last.
    """
    qv = t.get("quoteVolume")
    if qv is not None:
        try:
            return float(qv)
        except Exception:
            pass

    base_vol = t.get("baseVolume")
    last = t.get("last")
    if base_vol is not None and last is not None:
        try:
            return float(base_vol) * float(last)
        except Exception:
            return None
    return None


# =========================
# REST Endpoints
# =========================
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return {
        "name": "trading_bot",
        "version": "0.3.0",
        "endpoints": {
            "health": "/health",
            "scan": "/scan?quote=USDT&limit=60&min_vol=100000",
            "indicators": "/indicators?symbol=BTC/USDT&timeframe=1h&limit=300",
        },
        "defaults": {
            "exchange": DEFAULT_EXCHANGE,
            "quote": DEFAULT_QUOTE,
            "timeframe": DEFAULT_TIMEFRAME,
            "scan_limit": DEFAULT_SCAN_LIMIT,
            "min_quote_volume": DEFAULT_MIN_QUOTE_VOL,
        },
        "note": "هذا السيرفر لا يفتح صفقات حقيقية. فقط مسح وإرجاع بيانات/إشارات مبسّطة.",
    }


@app.get("/scan")
async def scan_market(
    quote: str = Query(DEFAULT_QUOTE, description="العملة المقابلة، مثال: USDT"),
    limit: int = Query(DEFAULT_SCAN_LIMIT, ge=1, le=500, description="عدد النتائج كحد أقصى"),
    min_vol: float = Query(DEFAULT_MIN_QUOTE_VOL, ge=0.0, description="أقل حجم تداول بالـ Quote ليتم تضمين الزوج"),
    exchange_id: str = Query(DEFAULT_EXCHANGE, description="معرّف البورصة في ccxt (مثل binance)"),
) -> Dict[str, Any]:
    """
    يمسح سوق البورصة المحددة، يُرجع أفضل الرابحين والخاسرين حسب نسبة التغير 24 ساعة مع فلترة حسب حجم التداول.
    """
    exchange = await make_exchange(exchange_id)
    try:
        await exchange.load_markets()
        # نجلب كل التيكرز دفعة واحدة (أسرع على Binance)
        tickers = await exchange.fetch_tickers()

        rows = []
        for sym, t in tickers.items():
            if not is_spot_usdt_symbol(sym, quote):
                continue
            pct = ticker_24h_change(t)
            qv = ticker_quote_volume(t)
            last = t.get("last")
            if qv is None or last is None:
                continue
            if qv < min_vol:
                continue
            rows.append(
                {
                    "symbol": sym,
                    "last": float(last),
                    "pct24h": pct if pct is not None else None,
                    "quoteVolume": float(qv),
                }
            )

        # تحويل لقائمة مرتبة
        df = pd.DataFrame(rows)
        if df.empty:
            return {"exchange": exchange_id, "quote": quote, "count": 0, "gainers": [], "losers": []}

        # قد تكون pct24h فيها None؛ نستبدل بـ -inf للترتيب ثم نعيدها None في الإخراج لو أردت
        df["pct24h_f"] = df["pct24h"].astype(float)
        df["pct24h_f"] = df["pct24h_f"].fillna(-np.inf)

        top_gainers = (
            df.sort_values(["pct24h_f", "quoteVolume"], ascending=[False, False])
            .head(limit)
            .drop(columns=["pct24h_f"])
            .to_dict(orient="records")
        )
        top_losers = (
            df.sort_values(["pct24h_f", "quoteVolume"], ascending=[True, False])
            .head(limit)
            .drop(columns=["pct24h_f"])
            .to_dict(orient="records")
        )

        return {
            "exchange": exchange_id,
            "quote": quote,
            "count": int(df.shape[0]),
            "gainers": top_gainers,
            "losers": top_losers,
        }
    except Exception as e:
        logger.exception("scan error: %s", e)
        raise HTTPException(status_code=503, detail=f"scan failed: {e}")
    finally:
        try:
            await exchange.close()
        except Exception:
            pass


@app.get("/indicators")
async def indicators(
    symbol: str = Query(..., description="مثال: BTC/USDT"),
    timeframe: str = Query(DEFAULT_TIMEFRAME, description="الإطار الزمني مثل: 1h, 4h, 15m, 1d"),
    limit: int = Query(300, ge=50, le=1500, description="عدد الشموع المطلوب"),
    exchange_id: str = Query(DEFAULT_EXCHANGE, description="معرّف البورصة في ccxt (مثل binance)"),
) -> Dict[str, Any]:
    """
    يجلب OHLCV ويحسب مؤشرات أساسية: EMA20/50/200 و RSI14.
    """
    if ":" in symbol:
        # لتجنُّب مشاكل العقود الدائمة ذات الصيغة BTC/USDT:USDT
        raise HTTPException(status_code=400, detail="هذا المسار يدعم أزواج السبوت فقط (بدون نقطتين في الرمز).")

    exchange = await make_exchange(exchange_id)

    try:
        await exchange.load_markets()
        markets = exchange.markets or {}
        if symbol not in markets:
            raise HTTPException(status_code=404, detail=f"symbol '{symbol}' not found on {exchange_id}")

        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not ohlcv:
            raise HTTPException(status_code=404, detail="no OHLCV returned")

        indic = compute_indicators_from_ohlcv(ohlcv)
        return {
            "exchange": exchange_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "limit": limit,
            "last": indic["close"],
            "ema20": indic["ema20"],
            "ema50": indic["ema50"],
            "ema200": indic["ema200"],
            "rsi14": indic["rsi14"],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("indicators error: %s", e)
        raise HTTPException(status_code=503, detail=f"indicators failed: {e}")
    finally:
        try:
            await exchange.close()
        except Exception:
            pass


# =========================
# تشغيل Uvicorn محليًا أو على Render
# =========================
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    # ملاحظة مهمة: نشغّل الكائن app مباشرة (بدون سلسلة 'module:app') لتفادي خطأ الاستيراد.
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False, workers=1)
