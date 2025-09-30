# -*- coding: utf-8 -*-
"""
trading_bot 0.3.py — Enhanced Strategy (USDT-M Perps) + Telegram + Render-friendly
- يحاول Binance USDT-M أولًا، ثم Bybit USDT Perps تلقائيًا إذا طلع حجب 451
- ما يخرج من البرنامج لو فشل تحميل الأزواج (يعيد المحاولة)
- ويب صغير (FastAPI) على PORT لإبقاء الخدمة شغّالة على Render
"""

import os, sys, time, traceback, logging, requests, threading
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple

import numpy as np
import pandas as pd
import ccxt

# ====================== TELEGRAM CONFIG (نفس الموجود) ======================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8130568386:AAGmpxKQw1XhqNjtj2OBzJ_-e3_vn0FE5Bs")
CHAT_ID        = int(os.getenv("CHAT_ID", "8429537293"))
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
TG_TIMEOUT     = 15
NET_RETRIES    = 3
RETRY_SLEEP    = 2.0

def tg_send(text, reply_to=None, disable_preview=True) -> Optional[int]:
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview
    }
    if reply_to:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = True
    for i in range(NET_RETRIES):
        try:
            r = requests.post(TG_API, json=payload, timeout=TG_TIMEOUT)
            data = r.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            if r.status_code == 429:
                ra = data.get("parameters", {}).get("retry_after", 2)
                time.sleep(ra + 1)
                continue
            logging.info(f"❌ TG error: {data}")
            return None
        except requests.RequestException:
            time.sleep(RETRY_SLEEP * (2 ** i))
    return None

def now_utc_str():  return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

# ====================== LOGGING ======================
LOG_FILE = os.path.join(os.path.dirname(__file__), "bot.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOG_FILE, encoding="utf-8")]
)
def log(msg): logging.info(msg)

# ====================== ENHANCED PRESETS =========================
MODE = "quality"  # أولوية للجودة والأمان
PRESETS = {
    "quality": {
        "TIMEFRAME": "5m",
        "BARS": 500,
        "SLEEP": 45,               
        "SCAN_TOP": 200,           
        "MIN_DOLLAR_VOLUME": 500_000,
        "MAX_SPREAD_PCT": 0.25/100,
        "MIN_CONFIDENCE": 68.0,
        "MAX_SIGNALS": 4,
        "COOLDOWN_MIN": 45,
        "RR_MIN": 1.28,
        "ATR_PERIOD": 14,
        "ATR_TP": (1.0, 2.0, 3.5),
        "ATR_SL": 0.95
    },
    "balanced": {
        "TIMEFRAME": "5m",
        "BARS": 450,
        "SLEEP": 60,
        "SCAN_TOP": 180,
        "MIN_DOLLAR_VOLUME": 350_000,
        "MAX_SPREAD_PCT": 0.30/100,
        "MIN_CONFIDENCE": 64.0,
        "MAX_SIGNALS": 5,
        "COOLDOWN_MIN": 35,
        "RR_MIN": 1.22,
        "ATR_PERIOD": 14,
        "ATR_TP": (0.9, 1.8, 2.8),
        "ATR_SL": 1.00
    }
}

CFG = PRESETS[MODE]
TIMEFRAME          = CFG["TIMEFRAME"]
BARS               = CFG["BARS"]
SLEEP_BETWEEN      = CFG["SLEEP"]
SCAN_TOP           = CFG["SCAN_TOP"]
MIN_DOLLAR_VOLUME  = CFG["MIN_DOLLAR_VOLUME"]
MAX_SPREAD_PCT     = CFG["MAX_SPREAD_PCT"]
MIN_CONFIDENCE     = CFG["MIN_CONFIDENCE"]
MAX_SIGNALS        = CFG["MAX_SIGNALS"]
COOLDOWN_MIN       = CFG["COOLDOWN_MIN"]
RR_MIN             = CFG["RR_MIN"]
ATR_PERIOD         = CFG["ATR_PERIOD"]
ATR_MULT_TP        = CFG["ATR_TP"]
ATR_MULT_SL        = CFG["ATR_SL"]

HEAVY = ["BTC/USDT:USDT","ETH/USDT:USDT","BNB/USDT:USDT","SOL/USDT:USDT",
         "XRP/USDT:USDT","DOGE/USDT:USDT","ADA/USDT:USDT","TON/USDT:USDT",
         "AVAX/USDT:USDT","MATIC/USDT:USDT","DOT/USDT:USDT","LINK/USDT:USDT"]

PING_EACH_SCAN = False

# ====================== Exchange Adapter (Binance → Bybit fallback) ======================
class ExchangeAdapter:
    def __init__(self):
        self.name = None
        self.ex = None
        self.ensure()

    def _mk_binance(self):
        return ccxt.binanceusdm({"enableRateLimit": True, "options": {"defaultType": "future"}, "timeout": 25_000})

    def _mk_bybit(self):
        # Bybit linear USDT perpetuals
        return ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap", "hedgeMode": False}, "timeout": 25_000})

    def ensure(self):
        """Try Binance first; if restricted (451) or fails, switch to Bybit."""
        # If already set, test quickly
        if self.ex and self.name:
            try:
                self.ex.timeframes  # touch
                return
            except Exception:
                pass

        # Try Binance
        try:
            ex = self._mk_binance()
            ex.load_markets()
            self.ex, self.name = ex, "binanceusdm"
            log("✅ Using Binance USDT-M")
            return
        except ccxt.ExchangeError as e:
            em = str(e)
            if "451" in em or "restricted location" in em.lower() or "Eligibility" in em:
                log("⛔ Binance restricted from current region — will use Bybit")
            else:
                log(f"⚠️ Binance init error: {em}")

        # Try Bybit
        try:
            ex = self._mk_bybit()
            ex.load_markets()
            self.ex, self.name = ex, "bybit"
            log("✅ Using Bybit USDT Perps")
            return
        except Exception as e:
            log(f"❌ Bybit init error: {e}")
            self.ex, self.name = None, None

    # thin wrappers with retries
    def load_markets(self):
        for _ in range(2):
            try:
                self.ensure()
                return self.ex.load_markets()
            except Exception as e:
                log(f"load_markets err: {e}")
                time.sleep(1.5)
        return {}

    def fetch_tickers(self):
        for _ in range(2):
            try:
                self.ensure()
                return self.ex.fetch_tickers()
            except Exception as e:
                log(f"fetch_tickers err: {e}")
                time.sleep(1.5)
        return {}

    def fetch_ohlcv(self, *a, **k):
        for _ in range(2):
            try:
                self.ensure()
                return self.ex.fetch_ohlcv(*a, **k)
            except Exception as e:
                log(f"fetch_ohlcv err: {e}")
                time.sleep(1.5)
        return None

    def fetch_order_book(self, *a, **k):
        try:
            self.ensure()
            return self.ex.fetch_order_book(*a, **k)
        except Exception:
            return {}

    def fetch_ticker(self, *a, **k):
        try:
            self.ensure()
            return self.ex.fetch_ticker(*a, **k)
        except Exception:
            return {}

    @property
    def id(self): return self.name

EX = ExchangeAdapter()

# ====================== STATE ================================
cooldown_until: Dict[str, float] = {}
open_signals: Dict[str, dict] = {}
no_pick_streak = 0

# ====================== TA HELPERS ===========================
def ema(v, n): return v.ewm(span=n, adjust=False).mean()

def rsi(series, n=14):
    delta = series.diff()
    up = np.maximum(delta, 0.0)
    down = np.maximum(-delta, 0.0)
    roll_up = pd.Series(up, index=series.index).ewm(span=n, adjust=False).mean()
    roll_dn = pd.Series(down, index=series.index).ewm(span=n, adjust=False).mean()
    rs = roll_up / (roll_dn + 1e-12)
    return 100.0 - (100.0 / (1.0 + rs))

def macd_line(series, fast=12, slow=26): return ema(series, fast) - ema(series, slow)
def macd_signal(macd, n=9): return ema(macd, n)

def true_range(h, l, c):
    prev_c = c.shift(1)
    tr1 = h - l
    tr2 = (h - prev_c).abs()
    tr3 = (l - prev_c).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

def atr(h, l, c, n=14): return true_range(h, l, c).ewm(span=n, adjust=False).mean()

def fmt(entry, x):
    if entry >= 100: return round(x, 2)
    if entry >= 1:   return round(x, 4)
    if entry >= 0.1: return round(x, 5)
    return round(x, 8)

# ====================== UNIVERSE =============================
def fetch_tickers_safely():
    return EX.fetch_tickers() or {}

def build_universe_top_by_volume(scan_top:int) -> List[str]:
    markets = EX.load_markets() or {}
    # نختار USDT linear swaps
    symbols = [
        m for m, d in markets.items()
        if d.get("active")
        and d.get("quote") == "USDT"
        and (d.get("linear") or d.get("contract"))
        and (d.get("swap") or d.get("type") in ("swap","future"))
    ]
    tickers = fetch_tickers_safely()
    def qvol(sym):
        t = tickers.get(sym, {})
        v = t.get("quoteVolume")
        if v is None and isinstance(t.get("info"), dict):
            info = t["info"]
            v = info.get("quoteVolume") or info.get("turnover24h") or info.get("quoteVolume24h")
        try: return float(v) if v is not None else 0.0
        except: return 0.0
    symbols.sort(key=lambda s: qvol(s), reverse=True)
    logging.info(f"build_universe[{EX.id}]: filtered={len(symbols)}")
    return symbols[:max(scan_top, 150)]

# ====================== DATA FETCH ===========================
def fetch_ohlcv(symbol, limit=400):
    df_raw = EX.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=limit)
    if not df_raw or len(df_raw) < 150:
        return None
    df = pd.DataFrame(df_raw, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].ffill().bfill().astype(float)
    return df

def dvol_5m_or_fallback(symbol, df:Optional[pd.DataFrame]) -> float:
    try:
        dv = float((df["close"].iloc[-30:]*df["volume"].iloc[-30:]).sum()) if df is not None else 0.0
    except Exception:
        dv = 0.0
    if dv > 0: return dv
    try:
        t = EX.fetch_ticker(symbol) or {}
        qv = t.get("quoteVolume")
        if qv is None and isinstance(t.get("info"), dict):
            info = t["info"]
            qv = info.get("quoteVolume") or info.get("turnover24h") or info.get("quoteVolume24h")
        qv = float(qv) if qv is not None else 0.0
        return (qv/288.0)*30.0
    except Exception:
        return 0.0

def fetch_market_meta(symbol) -> Tuple[float,float,Optional[float]]:
    spr = 999.0; last=None
    try:
        ob = EX.fetch_order_book(symbol, limit=10) or {}
        bids = ob.get("bids") or []; asks = ob.get("asks") or []
        bid = bids[0][0] if bids else None; ask = asks[0][0] if asks else None
        if bid and ask:
            mid = (bid+ask)/2.0
            if mid>0: spr = (ask-bid)/mid
    except Exception:
        pass
    try:
        t = EX.fetch_ticker(symbol) or {}
        last = t.get("last", None)
    except Exception:
        pass
    df = fetch_ohlcv(symbol, limit=80)
    dv = dvol_5m_or_fallback(symbol, df)
    return dv, spr, last

# ====================== STRATEGY (نفس منطقك السابق مختصرًا) ======================
STRAT = {
    "EMA_TREND_MIN_DIST": 0.005,
    "EMA_MOMENTUM_PERIODS": 5,
    "RSI_BULL_ZONE": (45, 66),
    "RSI_BEAR_ZONE": (34, 55),
    "RSI_EXTREME_AVOID": (20, 80),
    "MACD_MIN_SEPARATION": 0.0007,
    "MACD_MOMENTUM_CONFIRM": True,
    "BREAKOUT_BUFFER": 0.0015,
    "PULLBACK_MAX_DEPTH": 0.018,
    "PULLBACK_RECOVERY_MIN": 0.002,
    "VOLUME_SPIKE_MIN": 1.6,
    "VOLATILITY_FILTER": True,
    "PRICE_ACTION_CONFIRM": True,
    "CONFIDENCE_BASE": 50.0,
    "CONFIDENCE_MULTIPLIERS": {
        "strong_trend": 8.0,
        "macd_momentum": 6.0,
        "rsi_optimal": 5.0,
        "volume_spike": 4.0,
        "clean_breakout": 7.0,
        "perfect_pullback": 6.0,
        "volatility_good": 3.0
    }
}

def check_trend_strength(ema20, ema50, ema200):
    e20, e50, e200 = ema20.iloc[-1], ema50.iloc[-1], ema200.iloc[-1]
    if e20 > e50 > e200:
        dist1 = abs(e20 - e50) / e50
        dist2 = abs(e50 - e200) / e200
        if dist1 >= STRAT["EMA_TREND_MIN_DIST"] and dist2 >= STRAT["EMA_TREND_MIN_DIST"]:
            ema20_slope = (ema20.iloc[-1] - ema20.iloc[-STRAT["EMA_MOMENTUM_PERIODS"]]) / max(1e-12, ema20.iloc[-STRAT["EMA_MOMENTUM_PERIODS"]])
            if ema20_slope > 0.0018:
                return {"valid": True,"direction": "LONG","strength": 85.0,"reasons": ["strong_uptrend"]}
    if e20 < e50 < e200:
        dist1 = abs(e50 - e20) / e50
        dist2 = abs(e200 - e50) / e200
        if dist1 >= STRAT["EMA_TREND_MIN_DIST"] and dist2 >= STRAT["EMA_TREND_MIN_DIST"]:
            ema20_slope = (ema20.iloc[-STRAT["EMA_MOMENTUM_PERIODS"]] - ema20.iloc[-1]) / max(1e-12, ema20.iloc[-1])
            if ema20_slope > 0.0018:
                return {"valid": True,"direction": "SHORT","strength": 85.0,"reasons": ["strong_downtrend"]}
    return {"valid": False, "reasons": ["weak_trend"]}

def check_momentum(macd_v, macds, rsi14):
    mc = macd_v.iloc[-1]; ms = macds.iloc[-1]; r = rsi14.iloc[-1]
    if mc > ms and mc > 0 and abs(mc - ms) >= STRAT["MACD_MIN_SEPARATION"]:
        if STRAT["MACD_MOMENTUM_CONFIRM"] and (mc - macd_v.iloc[-3]) <= 0: 
            return {"valid": False, "reasons": ["macd_no_momentum"]}
        if STRAT["RSI_BULL_ZONE"][0] <= r <= STRAT["RSI_BULL_ZONE"][1] and r < STRAT["RSI_EXTREME_AVOID"][1]:
            return {"valid": True, "side":"LONG", "strength":80.0, "reasons":["macd_bullish","rsi_optimal_long"]}
    if mc < ms and mc < 0 and abs(mc - ms) >= STRAT["MACD_MIN_SEPARATION"]:
        if STRAT["MACD_MOMENTUM_CONFIRM"] and (macd_v.iloc[-3] - mc) <= 0:
            return {"valid": False, "reasons": ["macd_no_momentum"]}
        if STRAT["RSI_BEAR_ZONE"][0] <= r <= STRAT["RSI_BEAR_ZONE"][1] and r > STRAT["RSI_EXTREME_AVOID"][0]:
            return {"valid": True, "side":"SHORT","strength":80.0, "reasons":["macd_bearish","rsi_optimal_short"]}
    return {"valid": False, "reasons":["momentum_insufficient"]}

def check_entry_points(df, ema20, last, direction):
    cl, hi, lo = df["close"], df["high"], df["low"]
    if direction == "LONG":
        recent_high = hi.rolling(50).max().iloc[-2]
        if last >= recent_high * (1 + STRAT["BREAKOUT_BUFFER"]):
            vr = df["volume"].iloc[-1] / (df["volume"].rolling(20).mean().iloc[-1] + 1e-12)
            if vr >= 1.4: return {"valid": True,"side":"LONG","type":"breakout","strength":70.0,"reasons":["clean_breakout_long"]}
        lowest_recent = lo.rolling(10).min().iloc[-3:-1].min()
        if cl.iloc[-2] <= ema20.iloc[-1] * (1 + STRAT["PULLBACK_MAX_DEPTH"]):
            recovery = (last - lowest_recent) / max(1e-12, lowest_recent)
            if recovery >= STRAT["PULLBACK_RECOVERY_MIN"] and last >= ema20.iloc[-1] * (1 + 0.0008):
                return {"valid": True,"side":"LONG","type":"pullback","strength":65.0,"reasons":["perfect_pullback_long"]}
    else:
        recent_low = lo.rolling(50).min().iloc[-2]
        if last <= recent_low * (1 - STRAT["BREAKOUT_BUFFER"]):
            vr = df["volume"].iloc[-1] / (df["volume"].rolling(20).mean().iloc[-1] + 1e-12)
            if vr >= 1.4: return {"valid": True,"side":"SHORT","type":"breakout","strength":70.0,"reasons":["clean_breakout_short"]}
        highest_recent = hi.rolling(10).max().iloc[-3:-1].max()
        if cl.iloc[-2] >= ema20.iloc[-1] * (1 - STRAT["PULLBACK_MAX_DEPTH"]):
            recovery = (highest_recent - last) / max(1e-12, highest_recent)
            if recovery >= STRAT["PULLBACK_RECOVERY_MIN"] and last <= ema20.iloc[-1] * (1 - 0.0008):
                return {"valid": True,"side":"SHORT","type":"pullback","strength":65.0,"reasons":["perfect_pullback_short"]}
    return {"valid": False, "reasons":["no_clean_entry"]}

def check_confirmations(vol, atr_v, cl):
    reasons=[]
    vr = vol.iloc[-1] / (vol.rolling(20).mean().iloc[-1] + 1e-12)
    if vr >= STRAT["VOLUME_SPIKE_MIN"]: reasons.append(f"volume_spike_{vr:.1f}x")
    if STRAT["VOLATILITY_FILTER"]:
        current = atr_v.iloc[-1]; avg = atr_v.rolling(20).mean().iloc[-1]
        ratio = current / max(1e-12, avg)
        if 0.75 <= ratio <= 2.2: reasons.append("volatility_good")
    pm = (cl.iloc[-1] - cl.iloc[-5]) / max(1e-12, cl.iloc[-5])
    if abs(pm) > 0.0018: reasons.append(f"price_momentum_{abs(pm):.3f}")
    return {"reasons": reasons}

def calculate_enhanced_confidence(trend, momentum, entry, confirmations):
    c = STRAT["CONFIDENCE_BASE"]; mult = STRAT["CONFIDENCE_MULTIPLIERS"]
    if "strong_uptrend" in trend["reasons"] or "strong_downtrend" in trend["reasons"]: c += mult["strong_trend"]
    if any("momentum" in r for r in momentum["reasons"]): c += mult["macd_momentum"]
    if any("rsi_optimal" in r for r in momentum["reasons"]): c += mult["rsi_optimal"]
    if any("clean_breakout" in r for r in entry["reasons"]): c += mult["clean_breakout"]
    if any("perfect_pullback" in r for r in entry["reasons"]): c += mult["perfect_pullback"]
    if any("volume_spike" in r for r in confirmations["reasons"]): c += mult["volume_spike"]
    if "volatility_good" in confirmations["reasons"]): c += mult["volatility_good"]
    return min(95.0, max(50.0, c))

def enhanced_strategy_signals(df: pd.DataFrame):
    cl = df["close"]; hi = df["high"]; lo = df["low"]; vol = df["volume"]
    last = float(cl.iloc[-1])
    ema20 = ema(cl, 20); ema50 = ema(cl, 50); ema200 = ema(cl, 200)
    macd_v = macd_line(cl); macds = macd_signal(macd_v)
    rsi14  = rsi(cl, 14); atr14 = atr(hi, lo, cl, ATR_PERIOD)

    trend = check_trend_strength(ema20, ema50, ema200)
    if not trend["valid"]:
        return None, {"confidence": 40.0, "atr": float(atr14.iloc[-1]), "reasons": ["weak_trend"]}

    mom = check_momentum(macd_v, macds, rsi14)
    if not mom["valid"]:
        return None, {"confidence": 45.0, "atr": float(atr14.iloc[-1]), "reasons": ["weak_momentum"]}

    entry = check_entry_points(df, ema20, last, trend["direction"])
    if not entry["valid"]:
        return None, {"confidence": 50.0, "atr": float(atr14.iloc[-1]), "reasons": ["no_entry_signal"]}

    confs = check_confirmations(vol, atr14, cl)
    conf = calculate_enhanced_confidence(trend, mom, entry, confs)
    if trend["direction"] != entry["side"]:
        return None, {"confidence": conf, "atr": float(atr14.iloc[-1]), "reasons": ["direction_mismatch"]}

    return trend["direction"], {
        "confidence": conf,
        "atr": float(atr14.iloc[-1]),
        "reasons": (trend["reasons"] + mom["reasons"] + entry["reasons"] + confs["reasons"])
    }

# ====================== TARGETS / RISK =======================
def build_targets(entry, atr_val, side):
    if atr_val <= 0: atr_val = max(1e-6, entry*0.006)
    m1,m2,m3 = ATR_MULT_TP; slm = ATR_MULT_SL
    if side=="LONG":
        t1=entry+atr_val*m1; t2=entry+atr_val*m2; t3=entry+atr_val*m3; sl=entry-atr_val*slm
    else:
        t1=entry-atr_val*m1; t2=entry-atr_val*m2; t3=entry-atr_val*m3; sl=entry+atr_val*slm
    return [fmt(entry,t1),fmt(entry,t2),fmt(entry,t3)], fmt(entry,sl)

def rr_ok(entry, t1, sl, side, rr_min=1.28):
    if side=="LONG":
        risk=entry-sl; reward=t1-entry
    else:
        risk=sl-entry; reward=entry-t1
    if risk<=0: return False
    return (reward/risk) >= rr_min

# ====================== SEND / TRACK =========================
def _badge_for_tier(tier:str, conf:float):
    return "💎" if tier=="A" else ("⭐" if tier=="B" else "✅")

def send_signal(sig):
    side_emoji = "🚀" if sig["side"] == "LONG" else "🔻"
    badge = _badge_for_tier(sig.get("tier","A"), sig["conf"])
    tier_text = {"A":"Strict","B":"Relaxed","C":"HeavyFlex"}.get(sig.get("tier","A"),"Strict")
    text = (
        f"<b>{badge} توصية ({tier_text}) • {MODE} • {EX.id}</b>\n"
        f"{side_emoji} <b>{ 'شراء طويل (LONG)' if sig['side']=='LONG' else 'بيع قصير (SHORT)' }</b>\n"
        f"العملة: <code>{sig['symbol'].replace('/USDT:USDT', '/USDT')}</code>\n"
        f"التوقيت: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC\n\n"
        f"💰 نقطة الدخول: <code>{sig['entry']}</code>\n"
        f"🎯 الهدف 1: <code>{sig['tps'][0]}</code>\n"
        f"🎯 الهدف 2: <code>{sig['tps'][1]}</code>\n"
        f"🎯 الهدف 3: <code>{sig['tps'][2]}</code>\n"
        f"🛑 وقف الخسارة: <code>{sig['sl']}</code>\n\n"
        f"⚡ الثقة: <b>{sig['conf']:.1f}%</b>\n"
        f"📊 RR min: <b>1:{RR_MIN:.2f}</b>\n"
        f"⏰ TF: <b>{TIMEFRAME}</b>\n"
        f"💹 السيولة (تقديري): <b>${sig['dvol']:,.0f}</b>"
    )
    msg_id = tg_send(text)
    if msg_id:
        open_signals[sig["symbol"]] = {
            "msg_id": msg_id, "side": sig["side"], "entry": sig["entry"],
            "tps": sig["tps"], "sl": sig["sl"], "hit": 0, "closed": False,
            "timestamp": time.time()
        }
        cooldown_until[sig["symbol"]] = time.time() + COOLDOWN_MIN * 60
        log(f"Signal[{sig.get('tier','A')}]: {sig['symbol']} {sig['side']} conf={sig['conf']:.1f}%")

def fetch_last_price(symbol):
    try:
        return EX.fetch_ticker(symbol).get("last", None)
    except Exception:
        return None

def track_open_signals():
    if not open_signals: return
    for sym, st in list(open_signals.items()):
        if st.get("closed"): continue
        last = fetch_last_price(sym)
        if not last: continue
        side = st["side"]; tps = st["tps"]; sl = st["sl"]; msg_id = st["msg_id"]
        if side == "LONG":
            if last <= sl and st["hit"] >= 0:
                tg_send(f"🛑 <b>وقف خسارة مفعل</b>\nالسعر: <code>{last}</code>\n<code>{sym.replace('/USDT:USDT', '/USDT')}</code>", reply_to=msg_id)
                st["hit"] = -1; st["closed"] = True; continue
            if st["hit"] < 1 and last >= tps[0]:
                st["hit"] = 1; tg_send(f"✅ <b>الهدف الأول محقق!</b>\nالسعر: <code>{last}</code>\n<code>{sym.replace('/USDT:USDT', '/USDT')}</code>", reply_to=msg_id)
            if st["hit"] < 2 and last >= tps[1]:
                st["hit"] = 2; tg_send(f"🎯 <b>الهدف الثاني محقق!</b>", reply_to=msg_id)
            if st["hit"] < 3 and last >= tps[2]:
                st["hit"] = 3; st["closed"] = True
                tg_send(f"🏆 <b>جميع الأهداف محققة!</b>\n<code>{sym.replace('/USDT:USDT', '/USDT')}</code>\n\n💎 <b>تم إغلاق الصفقة</b>", reply_to=msg_id)
        else:
            if last >= sl and st["hit"] >= 0:
                tg_send(f"🛑 <b>وقف خسارة مفعل</b>\nالسعر: <code>{last}</code>\n<code>{sym.replace('/USDT:USDT', '/USDT')}</code>", reply_to=msg_id)
                st["hit"] = -1; st["closed"] = True; continue
            if st["hit"] < 1 and last <= tps[0]:
                st["hit"] = 1; tg_send(f"✅ <b>الهدف الأول محقق!</b>", reply_to=msg_id)
            if st["hit"] < 2 and last <= tps[1]:
                st["hit"] = 2; tg_send(f"🎯 <b>الهدف الثاني محقق!</b>", reply_to=msg_id)
            if st["hit"] < 3 and last <= tps[2]:
                st["hit"] = 3; st["closed"] = True
                tg_send(f"🏆 <b>جميع الأهداف محققة!</b>\n<code>{sym.replace('/USDT:USDT', '/USDT')}</code>\n\n💎 <b>تم إغلاق الصفقة</b>", reply_to=msg_id)

# ====================== RELAX LOGIC / SCAN ======================
def _relaxed_thresholds(level:int):
    if level <= 0:  return MIN_CONFIDENCE, RR_MIN, MIN_DOLLAR_VOLUME, MAX_SPREAD_PCT
    if level == 1:  return max(60.0, MIN_CONFIDENCE - 6.0), max(1.12, RR_MIN*0.9), MIN_DOLLAR_VOLUME*0.8,  MAX_SPREAD_PCT*1.15
    return max(58.0, MIN_CONFIDENCE - 10.0), max(1.05, RR_MIN*0.85), MIN_DOLLAR_VOLUME*0.65, MAX_SPREAD_PCT*1.25

def _tier_of_level(level:int) -> str:
    return "A" if level<=0 else ("B" if level==1 else "C")

def scan_symbols(symbols: List[str], relax_level:int=0) -> List[dict]:
    stats = {"dvol":0,"spr":0,"last":0,"ohlcv":0,"strategy":0,"rr":0,"conf":0,"cool":0}
    picks = []
    min_conf, rr_min, min_vol, max_spread = _relaxed_thresholds(relax_level)

    for sym in symbols[:SCAN_TOP]:
        try:
            if cooldown_until.get(sym, 0) > time.time():
                stats["cool"] += 1; continue

            dvol, spread, last_px = fetch_market_meta(sym)
            if dvol < min_vol:
                if sym not in HEAVY or relax_level < 2:
                    stats["dvol"] += 1; continue
                else:
                    dvol = min_vol * 1.3
            if spread > max_spread: stats["spr"] += 1; continue
            if not last_px or last_px <= 0: stats["last"] += 1; continue

            df = fetch_ohlcv(sym, limit=BARS)
            if df is None or len(df) < 200: stats["ohlcv"] += 1; continue

            side, meta = enhanced_strategy_signals(df)
            if side is None: stats["strategy"] += 1; continue

            entry = float(last_px)
            tps, sl = build_targets(entry, meta["atr"], side)

            rr_pass = rr_ok(entry, tps[0], sl, side, rr_min=rr_min)
            conf_pass = meta["confidence"] >= min_conf

            if not (rr_pass and conf_pass):
                if sym in HEAVY and dvol >= min_vol and meta["confidence"] >= (min_conf - 2) and rr_ok(entry, tps[0], sl, side, rr_min=max(1.02, rr_min*0.95)):
                    pass
                else:
                    if not rr_pass: stats["rr"] += 1
                    else: stats["conf"] += 1
                    continue

            picks.append({
                "symbol": sym, "entry": entry, "tps": tps, "sl": sl,
                "conf": meta["confidence"], "side": side, "dvol": dvol,
                "reasons": meta["reasons"], "tier": _tier_of_level(relax_level)
            })

        except Exception as e:
            log(f"Error scanning {sym}: {e}")

    if not picks:
        print(f"\n📊 [DIAG] level={relax_level} {EX.id} من أصل {len(symbols[:SCAN_TOP])} عملة:")
        print(f"   dvol:{stats['dvol']} spr:{stats['spr']} last:{stats['last']} ohlcv:{stats['ohlcv']} strategy:{stats['strategy']} rr:{stats['rr']} conf:{stats['conf']} cool:{stats['cool']}")
    picks.sort(key=lambda x: (x["conf"], x["dvol"]), reverse=True)
    return picks[:MAX_SIGNALS]

def premium_fallback(symbols: List[str], relax_level:int=2) -> Optional[dict]:
    min_conf, rr_min, min_vol, max_spread = _relaxed_thresholds(relax_level)
    best = None; best_score = -1
    for sym in symbols[:60]:
        try:
            if cooldown_until.get(sym, 0) > time.time(): continue
            dvol, spread, last_px = fetch_market_meta(sym)
            if dvol < min_vol or (spread > max_spread*1.1) or not last_px: continue
            df = fetch_ohlcv(sym, limit=BARS)
            if df is None: continue
            side, meta = enhanced_strategy_signals(df)
            if side is None or meta["confidence"] < (min_conf - 2): continue
            entry = float(last_px)
            tps, sl = build_targets(entry, meta["atr"], side)
            if not rr_ok(entry, tps[0], sl, side, rr_min=max(1.02, rr_min*0.95)): continue
            score = meta["confidence"]*0.72 + min(3.0, dvol/1_000_000)*0.28
            if score > best_score:
                best_score = score
                best = {"symbol": sym,"entry": entry,"tps": tps,"sl": sl,"conf": meta["confidence"],
                        "side": side,"dvol": dvol,"tier": _tier_of_level(relax_level)}
        except Exception:
            continue
    return best

# ====================== KEEP-ALIVE WEB (Render) ======================
def start_web():
    try:
        from fastapi import FastAPI
        import uvicorn
        app = FastAPI()
        @app.get("/")
        def home(): return {"ok": True, "exchange": EX.id, "time": now_utc_str()}
        port = int(os.getenv("PORT", "10000"))
        threading.Thread(target=lambda: uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning"), daemon=True).start()
    except Exception as e:
        log(f"Web server not started: {e}")

# ====================== MAIN LOOP ===========================
def main():
    global no_pick_streak
    banner = f"{now_utc_str()} - 🚀 Enhanced Trading Bot ({MODE} mode) starting..."
    print(banner, flush=True); log(banner)
    start_web()  # إبقاء الخدمة حية في Render

    # حل مشكلة الانطفاء: لو فشل تحميل الأزواج نستمر بالدوران والمحاولة
    symbols = []
    while not symbols:
        try:
            symbols = build_universe_top_by_volume(SCAN_TOP)
        except Exception as e:
            log(f"load_markets error: {e}")
        if not symbols:
            tg_send("⚠️ خطأ في تحميل قائمة العملات — سيعاد المحاولة تلقائيًا (قد يكون حجب Binance، سيتم استخدام Bybit)")
            time.sleep(20)

    print(f"{now_utc_str()} - ✅ تم تحميل {len(symbols)} زوج للفحص عبر {EX.id}", flush=True)
    log(f"Loaded {len(symbols)} trading pairs via {EX.id}")
    tg_send(f"🤖 <b>Enhanced Bot Started</b>\nMode: <b>{MODE}</b> (High Quality)\nTF: <b>{TIMEFRAME}</b>\nPairs: <b>{len(symbols)}</b>\nEX: <b>{EX.id}</b>\nConf≥<b>{MIN_CONFIDENCE}%</b> RR≥<b>1:{RR_MIN}</b>\n{now_utc_str()}")

    cycle = 0
    while True:
        try:
            cycle += 1
            if PING_EACH_SCAN or cycle % 10 == 1:
                tg_send(f"🔍 Scanning cycle #{cycle} • {now_utc_str()} • {EX.id}")
            print(f"\n{now_utc_str()} - 🔎 دورة الفحص #{cycle} - أعلى {SCAN_TOP} ({EX.id})...", flush=True)
            log(f"Scan cycle #{cycle} [{EX.id}]")

            # مستويات A ثم B ثم C
            picks = scan_symbols(symbols, relax_level=0) or scan_symbols(symbols, relax_level=1) or scan_symbols(symbols, relax_level=2)

            if not picks:
                no_pick_streak += 1
                print(f"⏳ لا توجد فرص ضمن المعايير. streak={no_pick_streak}", flush=True)
                fallback = premium_fallback(symbols, relax_level=2)
                if fallback:
                    print(f"💡 بديل: {fallback['symbol']} - ثقة {fallback['conf']:.1f}%", flush=True)
                    send_signal(fallback); no_pick_streak = 0
            else:
                no_pick_streak = 0
                print(f"🎯 تم العثور على {len(picks)} فرصة!", flush=True)
                for i, pick in enumerate(picks, 1):
                    print(f"   #{i}: {pick['symbol']} {pick['side']} - ثقة {pick['conf']:.1f}% [{pick.get('tier','A')}]",
                          flush=True)
                    send_signal(pick)

            end_time = time.time() + SLEEP_BETWEEN
            while time.time() < end_time:
                track_open_signals()
                time.sleep(5)

        except KeyboardInterrupt:
            print("\n🛑 تم إيقاف البوت يدوياً", flush=True)
            tg_send("🛑 تم إيقاف البوت يدوياً")
            log("Bot stopped manually")
            break
        except Exception as e:
            error_msg = f"خطأ في الدورة #{cycle}: {str(e)}"
            print(f"❌ {error_msg}", flush=True)
            log(f"Cycle error: {e}")
            traceback.print_exc()
            if cycle % 5 == 0:
                tg_send(f"⚠️ تحذير: {error_msg}")
            time.sleep(15)

if __name__ == "__main__":
    main()
