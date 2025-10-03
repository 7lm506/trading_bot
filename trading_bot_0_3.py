# trading_bot_0_4.py
import os, time, math, asyncio, random, logging
from typing import List, Dict, Tuple, Optional
import ccxt
import pandas as pd
import numpy as np
from fastapi import FastAPI
import uvicorn
import httpx

# -------------------- إعدادات عامة --------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

TIMEFRAME = os.getenv("TIMEFRAME", "5m")
SCAN_TOP = int(os.getenv("SCAN_TOP", "120"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "35"))
HEARTBEAT_MIN = int(os.getenv("HEARTBEAT_MIN", "30"))
DIAG_MIN = int(os.getenv("DIAG_MIN", "10"))

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

# شروط الاستراتيجية (v7_plus)
VOL_MIN = float(os.getenv("VOL_MULT", "1.1"))      # vol ≥ 1.1x
ADX_MIN = float(os.getenv("ADX_MIN", "18.0"))      # ADX ≥ 18
RR_MIN  = float(os.getenv("RR_MIN",  "1.18"))      # RR ≥ 1.18
ATR_EXT_MAX = float(os.getenv("ATR_EXT_MAX", "1.2"))  # امتداد من EMA20 ≤ 1.2×ATR
CROSS_LOOKBACK = int(os.getenv("CROSS_LOOKBACK", "36"))  # خلال آخر 36 شمعة

SL_ATR = 1.25
SL_BEHIND_EMA50_ATR = 0.35

# -------------------- تليغرام --------------------
async def tg_send(text: str):
    if not TG_TOKEN or not TG_CHAT:
        log.warning("TELEGRAM env vars missing -> skip send")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            await client.post(url, json=payload)
        except Exception as e:
            log.error(f"telegram error: {e}")

def human(x: float) -> str:
    if abs(x) >= 1:
        return f"{x:.2f}"
    return f"{x:.4f}"

# -------------------- مؤشرات --------------------
def build_df(ohlcv: List[List[float]]) -> Optional[pd.DataFrame]:
    if not ohlcv or len(ohlcv) < 100:
        return None
    cols = ["ts","open","high","low","close","volume"]
    df = pd.DataFrame(ohlcv, columns=cols)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    df = df.set_index("ts")
    return df

def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()

def atr_adx(df: pd.DataFrame, period: int = 14) -> Tuple[pd.Series, pd.Series]:
    # True Range
    close_prev = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - close_prev).abs(),
        (df["low"] - close_prev).abs()
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, adjust=False).mean()

    # +DM / -DM
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1/period, adjust=False).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1/period, adjust=False).mean() / atr
    dx = ( (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) ) * 100
    adx = dx.ewm(alpha=1/period, adjust=False).mean()
    return atr, adx

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema20"] = ema(df["close"], 20)
    df["ema50"] = ema(df["close"], 50)
    df["vol_med20"] = df["volume"].rolling(20).median()
    df["vol_x"] = df["volume"] / df["vol_med20"]
    atr, adx = atr_adx(df, 14)
    df["atr"] = atr
    df["adx"] = adx
    # عوائد معلوماتية — لا تُستخدم للفهرسة
    df["ret"] = df["close"].pct_change().fillna(0)
    return df

# -------------------- شروط الإستراتيجية --------------------
def last_cross_bars(df: pd.DataFrame) -> Optional[int]:
    ema20 = df["ema20"]
    ema50 = df["ema50"]
    cross_up = (ema20 > ema50) & (ema20.shift(1) <= ema50.shift(1))
    cross_dn = (ema20 < ema50) & (ema20.shift(1) >= ema50.shift(1))
    cross_idx = df.index[cross_up | cross_dn]
    if len(cross_idx) == 0:
        return None
    bars = len(df) - (df.index.get_indexer([cross_idx[-1]])[0] + 1)
    return bars

def rr_estimate(side: str, close: float, ema50: float, atr: float) -> float:
    sl_dist = max(SL_ATR*atr, max(close - ema50, 0) + SL_BEHIND_EMA50_ATR*atr) if side == "LONG" \
              else max(SL_ATR*atr, max(ema50 - close, 0) + SL_BEHIND_EMA50_ATR*atr)
    # هدف بسيط = 1.5 × ATR
    tp_dist = 1.5 * atr
    if sl_dist <= 0: 
        return 0
    return tp_dist / sl_dist

def evaluate_symbol(df: pd.DataFrame) -> Tuple[Optional[Dict], Dict[str, int]]:
    """
    يعيد توصية أو None + قاموس أسباب الرفض
    """
    reject: Dict[str,int] = {}
    if df is None or len(df) < 100:
        reject["no_data"] = 1
        return None, reject

    # نأخذ آخر 200 شمعة بطريقة سليمة (بدون loc بقيم)
    df = df.iloc[-200:].copy()

    df = compute_indicators(df)
    df = df.dropna()
    if df.empty:
        reject["nan_all"] = 1
        return None, reject

    row = df.iloc[-1]
    # حجم
    if row["vol_x"] < VOL_MIN:
        reject[f"low_dvol"] = 1
        return None, reject
    # ADX
    if row["adx"] < ADX_MIN:
        reject[f"adx_{int(row['adx'])}"] = 1
        return None, reject

    # امتداد عن EMA20
    ext_atr = abs(row["close"] - row["ema20"]) / max(row["atr"], 1e-9)
    if ext_atr > ATR_EXT_MAX:
        reject["extended_from_ema20"] = 1
        return None, reject

    # تقاطع حديث
    bars = last_cross_bars(df)
    if bars is None or bars > CROSS_LOOKBACK:
        reject["no_recent_cross"] = 1
        return None, reject

    # اتجاه
    side = "LONG" if row["ema20"] >= row["ema50"] else "SHORT"
    rr = rr_estimate(side, row["close"], row["ema50"], row["atr"])
    if rr < RR_MIN:
        reject["poor_rr"] = 1
        return None, reject

    sig = {
        "side": side,
        "close": float(row["close"]),
        "ema20": float(row["ema20"]),
        "ema50": float(row["ema50"]),
        "atr": float(row["atr"]),
        "adx": float(row["adx"]),
        "volx": float(row["vol_x"]),
        "rr": float(rr),
        "bars_since_cross": bars,
    }
    return sig, reject

# -------------------- OKX + جلب البيانات --------------------
def okx() -> ccxt.okx:
    return ccxt.okx({
        "enableRateLimit": True,
        "rateLimit": 150,  # احترازي
        "timeout": 20000,
        "options": {"defaultType": "spot"},
    })

def is_usdt_symbol(m: Dict) -> bool:
    return (m.get("quote") == "USDT") and (m.get("active") is True)

def top_symbols_by_quotevol(exchange: ccxt.okx, top_n: int) -> List[str]:
    markets = exchange.load_markets()
    syms = [s for s,m in markets.items() if is_usdt_symbol(m)]
    tickers = exchange.fetch_tickers(syms)
    scored = []
    for s, t in tickers.items():
        qv = t.get("quoteVolume") or 0
        scored.append((s, qv))
    scored.sort(key=lambda x: x[1] or 0, reverse=True)
    return [s for s,_ in scored[:top_n]]

def safe_fetch_ohlcv(exchange: ccxt.okx, symbol: str, timeframe: str, limit: int = 300) -> Optional[List[List[float]]]:
    for attempt in range(6):
        try:
            return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception as e:
            msg = str(e)
            # كود OKX rate limit
            if "50011" in msg or "Too Many Requests" in msg:
                sleep_s = 0.8 + random.random()*0.6
                time.sleep(sleep_s)
                continue
            # مؤقت أو الشبكة
            time.sleep(0.4 + 0.2*attempt)
    return None

# -------------------- ماسح التوصيات --------------------
async def scan_once() -> Tuple[List[Tuple[str,Dict]], Dict[str,int]]:
    ex = okx()
    try:
        symbols = top_symbols_by_quotevol(ex, SCAN_TOP)
    except Exception as e:
        log.error(f"load markets/tickers error: {e}")
        ex.close()
        return [], {"load_err":1}

    approved: List[Tuple[str,Dict]] = []
    rejects: Dict[str,int] = {}

    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i+BATCH_SIZE]
        for s in batch:
            try:
                ohlcv = safe_fetch_ohlcv(ex, s, TIMEFRAME, 300)
                if not ohlcv:
                    rejects["no_data"] = rejects.get("no_data",0)+1
                    continue
                df = build_df(ohlcv)
                sig, r = evaluate_symbol(df)
                # اجمع أسباب الرفض
                for k,v in r.items():
                    rejects[k] = rejects.get(k,0)+v
                if sig:
                    approved.append((s, sig))
            except Exception as e:
                # أهم نقطة: لا نطبع قوائم floats؛ بس نوع الخطأ
                log.error(f"scan error {s}: {type(e).__name__}: {e}")
                rejects["scan_err"] = rejects.get("scan_err",0)+1

        # تهدئة بسيطة بين الدُفعات
        await asyncio.sleep(0.4)

    ex.close()
    return approved, rejects

def fmt_start_msg() -> str:
    return (
        f"🤖 Bot Started • v7_plus\n"
        f"⏱️ TF: {TIMEFRAME} • 🔝 Top: {SCAN_TOP} • Batch: {BATCH_SIZE}\n"
        f"🛑 SL: max({SL_ATR}×ATR, خلف EMA50 {SL_BEHIND_EMA50_ATR}×ATR)\n"
        f"📈 شروط: EMA20/50 + Vol≥{VOL_MIN:.1f}× + ADX≥{ADX_MIN:.1f} • RR≥{RR_MIN:.2f}"
    )

def fmt_diag(rejects: Dict[str,int]) -> str:
    parts = sorted(rejects.items(), key=lambda x: -x[1])
    top = "\n".join([f"• {k}: {v}" for k,v in parts[:6]]) if parts else "• -"
    return (
        "📋 لماذا لا توجد توصيات (الدورة)\n"
        f"عتبات: ATRx≤{ATR_EXT_MAX} • ADX≥{ADX_MIN:.1f} • RR≥{RR_MIN:.2f} • vol≥{VOL_MIN:.1f}×\n"
        f"دفعة المسح: {BATCH_SIZE}/{SCAN_TOP}\n"
        f"أكثر أسباب الرفض:\n{top}"
    )

def fmt_signal(sym: str, s: Dict) -> str:
    return (
        f"✅ {sym} • {s['side']}\n"
        f"⏩ RR ~ {s['rr']:.2f} • ADX {s['adx']:.1f} • Vol× {s['volx']:.2f}\n"
        f"EMA20 {human(s['ema20'])} • EMA50 {human(s['ema50'])}\n"
        f"ATR {human(s['atr'])} • آخر تقاطع: {s['bars_since_cross']} شموع"
    )

def fmt_hb() -> str:
    return f"💓 Heartbeat\n⏱️ TF: {TIMEFRAME} • 🔝 Top: {SCAN_TOP} • Batch: {BATCH_SIZE}\nصفقات مفتوحة: 0\nDiag كل {DIAG_MIN}m • HB كل {HEARTBEAT_MIN}m"

# -------------------- المهام الدورية --------------------
async def runner():
    await tg_send("> توصيات تداول Ai:\n" + fmt_start_msg())
    last_diag = 0.0
    last_hb = 0.0
    while True:
        start = time.time()
        approved, rejects = await scan_once()

        if approved:
            txt = "> توصيات تداول Ai:\n" + "\n\n".join([fmt_signal(sym, s) for sym,s in approved])
            await tg_send(txt)
        else:
            now = time.time()
            if now - last_diag > DIAG_MIN*60:
                await tg_send("> توصيات تداول Ai:\n" + fmt_diag(rejects))
                last_diag = now

        # Heartbeat
        if time.time() - last_hb > HEARTBEAT_MIN*60:
            await tg_send("> توصيات تداول Ai:\n" + fmt_hb())
            last_hb = time.time()

        # دورة كل 60 ثانية (على فاصل 5m هذا خفيف)
        took = time.time() - start
        await asyncio.sleep(max(5.0, 60.0 - took))

# -------------------- FastAPI --------------------
app = FastAPI()

@app.on_event("startup")
async def _startup():
    asyncio.create_task(runner())

@app.get("/")
def root():
    return {"status":"ok","timeframe":TIMEFRAME,"top":SCAN_TOP,"batch":BATCH_SIZE}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT","10000")))
