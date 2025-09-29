# -*- coding: utf-8 -*-
"""
trading_bot 0.3.py — Enhanced Strategy (USDT-M Perps) + Telegram
استراتيجية محسنة للجودة والأمان

التحسينات:
- استراتيجية مبسطة وأكثر دقة
- فلترة أقوى للإشارات عالية الجودة
- شروط أمان محسنة
- تقليل الإشارات الخاطئة

[تحديث 2025-09-29]
- مُرخّي ديناميكي للصرامة (Adaptive Relaxer) في حال عدم ظهور إشارات
- مستويات قبول: A (صارم), B (مرن), C (مرن جدًا للـ HEAVY)
- بديل Premium fallback مُحسّن
"""

import os, sys, time, traceback, logging, requests
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple

import numpy as np
import pandas as pd
import ccxt

# ====================== TELEGRAM CONFIG (مضمن) ======================
TELEGRAM_TOKEN = "8130568386:AAGmpxKQw1XhqNjtj2OBzJ_-e3_vn0FE5Bs"
CHAT_ID        = 8429537293
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
        "MIN_DOLLAR_VOLUME": 500_000,   # كان 500k
        "MAX_SPREAD_PCT": 0.25/100,     # 0.25%
        "MIN_CONFIDENCE": 68.0,         # ↓ كان 70.0 (تخفيف بسيط)
        "MAX_SIGNALS": 4,
        "COOLDOWN_MIN": 45,
        "RR_MIN": 1.28,                 # ↓ كان 1.35
        "ATR_PERIOD": 14,
        "ATR_TP": (1.0, 2.0, 3.5),
        "ATR_SL": 0.95                  # ↑ SL أبعد قليلًا لتقليل ضرب الوقف بالذبذبة
    },
    "balanced": {
        "TIMEFRAME": "5m",
        "BARS": 450,
        "SLEEP": 60,
        "SCAN_TOP": 180,
        "MIN_DOLLAR_VOLUME": 350_000,
        "MAX_SPREAD_PCT": 0.30/100,
        "MIN_CONFIDENCE": 64.0,         # ↓ كان 65.0
        "MAX_SIGNALS": 5,
        "COOLDOWN_MIN": 35,
        "RR_MIN": 1.22,                 # ↓ كان 1.25
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

# أزواج موثوقة
HEAVY = ["BTC/USDT:USDT","ETH/USDT:USDT","BNB/USDT:USDT","SOL/USDT:USDT",
         "XRP/USDT:USDT","DOGE/USDT:USDT","ADA/USDT:USDT","TON/USDT:USDT",
         "AVAX/USDT:USDT","MATIC/USDT:USDT","DOT/USDT:USDT","LINK/USDT:USDT"]

PING_EACH_SCAN = False
FORCE_TEST_SIGNAL = False

# ====================== EXCHANGE (USDT-M) ====================
ex = ccxt.binanceusdm({
    "enableRateLimit": True,
    "options": {"defaultType": "future"},
    "timeout": 25_000
})

# ====================== STATE ================================
cooldown_until: Dict[str, float] = {}
open_signals: Dict[str, dict] = {}
no_pick_streak = 0  # عداد دورات بدون توصية (للمُرخّي الديناميكي)

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
    for _ in range(3):
        try:
            return ex.fetch_tickers()
        except Exception:
            time.sleep(1.5)
    return {}

def build_universe_top_by_volume(scan_top:int) -> List[str]:
    markets = ex.load_markets()
    symbols = [
        m for m, d in markets.items()
        if d.get("active")
        and d.get("linear")
        and d.get("quote") == "USDT"
        and (d.get("swap") or d.get("type") in ("swap","future"))
    ]
    tickers = fetch_tickers_safely()
    def qvol(sym):
        t = tickers.get(sym, {})
        v = t.get("quoteVolume")
        if v is None and isinstance(t.get("info"), dict):
            v = t["info"].get("quoteVolume") or t["info"].get("quoteVolume24h")
        try: return float(v) if v is not None else 0.0
        except: return 0.0
    symbols.sort(key=lambda s: qvol(s), reverse=True)
    logging.info(f"build_universe: total={len(markets)} filtered={len(symbols)}")
    return symbols[:max(scan_top, 150)]

# ====================== DATA FETCH ===========================
def fetch_ohlcv(symbol, limit=400):
    try:
        data = ex.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=limit)
        if not data or len(data) < 150:
            return None
        df = pd.DataFrame(data, columns=["ts","open","high","low","close","volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        for c in ["open","high","low","close","volume"]:
            df[c] = df[c].ffill().bfill().astype(float)
        return df
    except Exception:
        return None

def dvol_5m_or_fallback(symbol, df:Optional[pd.DataFrame]) -> float:
    try:
        dv = float((df["close"].iloc[-30:]*df["volume"].iloc[-30:]).sum()) if df is not None else 0.0
    except Exception:
        dv = 0.0
    if dv > 0: return dv
    try:
        t = ex.fetch_ticker(symbol) or {}
        qv = t.get("quoteVolume")
        if qv is None and isinstance(t.get("info"), dict):
            qv = t["info"].get("quoteVolume") or t["info"].get("quoteVolume24h")
        qv = float(qv) if qv is not None else 0.0
        return (qv/288.0)*30.0
    except Exception:
        return 0.0

def fetch_market_meta(symbol) -> Tuple[float,float,Optional[float]]:
    spr = 999.0; last=None
    try:
        ob = ex.fetch_order_book(symbol, limit=10) or {}
        bids = ob.get("bids") or []; asks = ob.get("asks") or []
        bid = bids[0][0] if bids else None; ask = asks[0][0] if asks else None
        if bid and ask:
            mid = (bid+ask)/2.0
            if mid>0: spr = (ask-bid)/mid
    except Exception:
        pass
    try:
        t = ex.fetch_ticker(symbol) or {}
        last = t.get("last", None)
    except Exception:
        pass
    df = fetch_ohlcv(symbol, limit=80)
    dv = dvol_5m_or_fallback(symbol, df)
    return dv, spr, last

# ====================== ENHANCED STRATEGY CONFIG ======================
STRAT = {
    # اتجاه أقوى وأوضح
    "EMA_TREND_MIN_DIST": 0.005,     # 0.5% مسافة دنيا بين EMAs
    "EMA_MOMENTUM_PERIODS": 5,       # فترات لقياس زخم EMA
    
    # RSI محسن
    "RSI_BULL_ZONE": (45, 66),       # توسعة طفيفة للأعلى
    "RSI_BEAR_ZONE": (34, 55),       # توسعة طفيفة للأسفل
    "RSI_EXTREME_AVOID": (20, 80),   # تجنب المناطق المتطرفة
    
    # MACD أكثر دقة
    "MACD_MIN_SEPARATION": 0.0007,   # ↓ فصل أدنى (كان 0.001)
    "MACD_MOMENTUM_CONFIRM": True,   # تأكيد زخم MACD
    
    # دخول محسن
    "BREAKOUT_BUFFER": 0.0015,       # ↓ 0.15% هامش للاختراق
    "PULLBACK_MAX_DEPTH": 0.018,     # ↑ عمق أقصى 1.8%
    "PULLBACK_RECOVERY_MIN": 0.002,  # ↓ 0.2% دنيا للتعافي
    
    # تأكيدات إضافية
    "VOLUME_SPIKE_MIN": 1.6,         # ↓ كان 1.8
    "VOLATILITY_FILTER": True,       # فلترة التقلبات
    "PRICE_ACTION_CONFIRM": True,    # تأكيد حركة السعر
    
    # نظام تسكير محسن
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

# ====================== ENHANCED STRATEGY ======================
def enhanced_strategy_signals(df: pd.DataFrame):
    cl = df["close"]; hi = df["high"]; lo = df["low"]; vol = df["volume"]
    last = float(cl.iloc[-1])
    
    ema20 = ema(cl, 20); ema50 = ema(cl, 50); ema200 = ema(cl, 200)
    macd_v = macd_line(cl); macds = macd_signal(macd_v)
    rsi14 = rsi(cl, 14)
    atr14 = atr(hi, lo, cl, ATR_PERIOD)
    
    trend_strength = check_trend_strength(ema20, ema50, ema200)
    if not trend_strength["valid"]:
        return None, {"confidence": 40.0, "atr": float(atr14.iloc[-1]), 
                     "reasons": ["weak_trend"]}
    
    momentum_check = check_momentum(macd_v, macds, rsi14)
    if not momentum_check["valid"]:
        return None, {"confidence": 45.0, "atr": float(atr14.iloc[-1]), 
                     "reasons": ["weak_momentum"]}
    
    entry_signal = check_entry_points(df, ema20, last, trend_strength["direction"])
    if not entry_signal["valid"]:
        return None, {"confidence": 50.0, "atr": float(atr14.iloc[-1]), 
                     "reasons": ["no_entry_signal"]}
    
    confirmations = check_confirmations(vol, atr14, cl)
    
    confidence = calculate_enhanced_confidence(
        trend_strength, momentum_check, entry_signal, confirmations
    )
    
    direction = trend_strength["direction"]
    if direction != entry_signal["side"]:
        return None, {"confidence": confidence, "atr": float(atr14.iloc[-1]), 
                     "reasons": ["direction_mismatch"]}
    
    return direction, {
        "confidence": confidence,
        "atr": float(atr14.iloc[-1]),
        "reasons": trend_strength["reasons"] + momentum_check["reasons"] + 
                  entry_signal["reasons"] + confirmations["reasons"]
    }

def check_trend_strength(ema20, ema50, ema200):
    e20, e50, e200 = ema20.iloc[-1], ema50.iloc[-1], ema200.iloc[-1]
    if e20 > e50 > e200:
        dist1 = abs(e20 - e50) / e50
        dist2 = abs(e50 - e200) / e200
        if dist1 >= STRAT["EMA_TREND_MIN_DIST"] and dist2 >= STRAT["EMA_TREND_MIN_DIST"]:
            ema20_slope = (ema20.iloc[-1] - ema20.iloc[-STRAT["EMA_MOMENTUM_PERIODS"]]) / max(1e-12, ema20.iloc[-STRAT["EMA_MOMENTUM_PERIODS"]])
            if ema20_slope > 0.0018:  # ↓ كان 0.002
                return {"valid": True,"direction": "LONG",
                        "strength": min(95.0, 70.0 + (dist1 + dist2) * 1000),
                        "reasons": ["strong_uptrend", f"ema_momentum_{ema20_slope:.4f}"]}
    elif e20 < e50 < e200:
        dist1 = abs(e50 - e20) / e50
        dist2 = abs(e200 - e50) / e200
        if dist1 >= STRAT["EMA_TREND_MIN_DIST"] and dist2 >= STRAT["EMA_TREND_MIN_DIST"]:
            ema20_slope = (ema20.iloc[-STRAT["EMA_MOMENTUM_PERIODS"]] - ema20.iloc[-1]) / max(1e-12, ema20.iloc[-1])
            if ema20_slope > 0.0018:
                return {"valid": True,"direction": "SHORT",
                        "strength": min(95.0, 70.0 + (dist1 + dist2) * 1000),
                        "reasons": ["strong_downtrend", f"ema_momentum_{ema20_slope:.4f}"]}
    return {"valid": False, "reasons": ["weak_trend"]}

def check_momentum(macd_v, macds, rsi14):
    macd_current = macd_v.iloc[-1]
    signal_current = macds.iloc[-1]
    rsi_current = rsi14.iloc[-1]
    reasons = []
    if macd_current > signal_current and macd_current > 0:
        macd_sep = abs(macd_current - signal_current)
        if macd_sep >= STRAT["MACD_MIN_SEPARATION"]:
            if STRAT["MACD_MOMENTUM_CONFIRM"]:
                macd_momentum = macd_current - macd_v.iloc[-3]
                if macd_momentum > 0:
                    reasons.append("macd_bull_momentum")
                else:
                    return {"valid": False, "reasons": ["macd_no_momentum"]}
            if STRAT["RSI_BULL_ZONE"][0] <= rsi_current <= STRAT["RSI_BULL_ZONE"][1]:
                if rsi_current < STRAT["RSI_EXTREME_AVOID"][1]:
                    reasons.extend(["macd_bullish", "rsi_optimal_long"])
                    return {"valid": True,"side": "LONG",
                            "strength": min(90.0, 60.0 + macd_sep * 10000 + (65 - abs(rsi_current - 55)) * 2),
                            "reasons": reasons}
    elif macd_current < signal_current and macd_current < 0:
        macd_sep = abs(macd_current - signal_current)
        if macd_sep >= STRAT["MACD_MIN_SEPARATION"]:
            if STRAT["MACD_MOMENTUM_CONFIRM"]:
                macd_momentum = macd_v.iloc[-3] - macd_current
                if macd_momentum > 0:
                    reasons.append("macd_bear_momentum")
                else:
                    return {"valid": False, "reasons": ["macd_no_momentum"]}
            if STRAT["RSI_BEAR_ZONE"][0] <= rsi_current <= STRAT["RSI_BEAR_ZONE"][1]:
                if rsi_current > STRAT["RSI_EXTREME_AVOID"][0]:
                    reasons.extend(["macd_bearish", "rsi_optimal_short"])
                    return {"valid": True,"side": "SHORT",
                            "strength": min(90.0, 60.0 + macd_sep * 10000 + (abs(rsi_current - 45) - 65) * -2),
                            "reasons": reasons}
    return {"valid": False, "reasons": ["momentum_insufficient"]}

def check_entry_points(df, ema20, last, direction):
    cl = df["close"]; hi = df["high"]; lo = df["low"]
    reasons = []
    if direction == "LONG":
        recent_high = hi.rolling(50).max().iloc[-2]
        breakout_level = recent_high * (1 + STRAT["BREAKOUT_BUFFER"])
        if last >= breakout_level:
            volume_ratio = df["volume"].iloc[-1] / (df["volume"].rolling(20).mean().iloc[-1] + 1e-12)
            if volume_ratio >= 1.4:  # ↓ كان 1.5
                reasons.append("clean_breakout_long")
                return {"valid": True,"side": "LONG","type": "breakout",
                        "strength": min(85.0, 65.0 + volume_ratio * 10),
                        "reasons": reasons}
        lowest_recent = lo.rolling(10).min().iloc[-3:-1].min()
        ema20_level = ema20.iloc[-1]
        if cl.iloc[-2] <= ema20_level * (1 + STRAT["PULLBACK_MAX_DEPTH"]):
            recovery = (last - lowest_recent) / max(1e-12, lowest_recent)
            if recovery >= STRAT["PULLBACK_RECOVERY_MIN"]:
                if last >= ema20_level * (1 + 0.0008):  # ↓ 0.08% فوق EMA20
                    reasons.append("perfect_pullback_long")
                    return {"valid": True,"side": "LONG","type": "pullback",
                            "strength": min(80.0, 60.0 + recovery * 1000),
                            "reasons": reasons}
    elif direction == "SHORT":
        recent_low = lo.rolling(50).min().iloc[-2]
        breakdown_level = recent_low * (1 - STRAT["BREAKOUT_BUFFER"])
        if last <= breakdown_level:
            volume_ratio = df["volume"].iloc[-1] / (df["volume"].rolling(20).mean().iloc[-1] + 1e-12)
            if volume_ratio >= 1.4:
                reasons.append("clean_breakout_short")
                return {"valid": True,"side": "SHORT","type": "breakout",
                        "strength": min(85.0, 65.0 + volume_ratio * 10),
                        "reasons": reasons}
        highest_recent = hi.rolling(10).max().iloc[-3:-1].max()
        ema20_level = ema20.iloc[-1]
        if cl.iloc[-2] >= ema20_level * (1 - STRAT["PULLBACK_MAX_DEPTH"]):
            recovery = (highest_recent - last) / max(1e-12, highest_recent)
            if recovery >= STRAT["PULLBACK_RECOVERY_MIN"]:
                if last <= ema20_level * (1 - 0.0008):
                    reasons.append("perfect_pullback_short")
                    return {"valid": True,"side": "SHORT","type": "pullback",
                            "strength": min(80.0, 60.0 + recovery * 1000),
                            "reasons": reasons}
    return {"valid": False, "reasons": ["no_clean_entry"]}

def check_confirmations(vol, atr, cl):
    reasons = []
    vol_ratio = vol.iloc[-1] / (vol.rolling(20).mean().iloc[-1] + 1e-12)
    if vol_ratio >= STRAT["VOLUME_SPIKE_MIN"]:
        reasons.append(f"volume_spike_{vol_ratio:.1f}x")
    if STRAT["VOLATILITY_FILTER"]:
        current_atr = atr.iloc[-1]
        avg_atr = atr.rolling(20).mean().iloc[-1]
        atr_ratio = current_atr / max(1e-12, avg_atr)
        if 0.75 <= atr_ratio <= 2.2:   # وسّعنا النطاق قليلًا
            reasons.append("volatility_good")
        elif atr_ratio > 2.2:
            reasons.append("volatility_high")
        else:
            reasons.append("volatility_low")
    if STRAT["PRICE_ACTION_CONFIRM"]:
        price_momentum = (cl.iloc[-1] - cl.iloc[-5]) / max(1e-12, cl.iloc[-5])
        if abs(price_momentum) > 0.0018:  # ↓ كان 0.002
            reasons.append(f"price_momentum_{abs(price_momentum):.3f}")
    return {"reasons": reasons}

def calculate_enhanced_confidence(trend, momentum, entry, confirmations):
    confidence = STRAT["CONFIDENCE_BASE"]
    mult = STRAT["CONFIDENCE_MULTIPLIERS"]
    if "strong_uptrend" in trend["reasons"] or "strong_downtrend" in trend["reasons"]:
        confidence += mult["strong_trend"]
    if any("macd_" in r and "momentum" in r for r in momentum["reasons"]):
        confidence += mult["macd_momentum"]
    if any("rsi_optimal" in r for r in momentum["reasons"]):
        confidence += mult["rsi_optimal"]
    if any("clean_breakout" in r for r in entry["reasons"]):
        confidence += mult["clean_breakout"]
    if any("perfect_pullback" in r for r in entry["reasons"]):
        confidence += mult["perfect_pullback"]
    if any("volume_spike" in r for r in confirmations["reasons"]):
        confidence += mult["volume_spike"]
    if "volatility_good" in confirmations["reasons"]:
        confidence += mult["volatility_good"]
    return min(95.0, max(50.0, confidence))

# ====================== TARGETS / RISK =======================
def build_targets(entry, atr_val, side):
    if atr_val <= 0:
        atr_val = max(1e-6, entry*0.006)
    m1, m2, m3 = ATR_MULT_TP
    slm = ATR_MULT_SL
    if side == "LONG":
        t1 = entry + atr_val*m1; t2 = entry + atr_val*m2; t3 = entry + atr_val*m3; sl = entry - atr_val*slm
    else:
        t1 = entry - atr_val*m1; t2 = entry - atr_val*m2; t3 = entry - atr_val*m3; sl = entry + atr_val*slm
    return [fmt(entry,t1), fmt(entry,t2), fmt(entry,t3)], fmt(entry,sl)

def rr_ok(entry, t1, sl, side, rr_min=1.28):
    if side == "LONG":
        risk = entry - sl; reward = t1 - entry
    else:
        risk = sl - entry; reward = entry - t1
    if risk <= 0: return False
    return (reward / risk) >= rr_min

# ====================== SEND / TRACK =========================
def _badge_for_tier(tier:str, conf:float):
    if tier=="A": return "💎"
    if tier=="B": return "⭐"
    return "✅"  # C

def send_signal(sig):
    side_emoji = "🚀" if sig["side"] == "LONG" else "🔻"
    badge = _badge_for_tier(sig.get("tier","A"), sig["conf"])
    tier_text = {"A":"Strict","B":"Relaxed","C":"HeavyFlex"}.get(sig.get("tier","A"),"Strict")
    text = (
        f"<b>{badge} توصية ({tier_text}) • {MODE}</b>\n"
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
        return ex.fetch_ticker(symbol).get("last", None)
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
                tg_send(f"🛑 <b>وقف خسارة مفعل</b>\nالسعر: <code>{last}</code>\nالعملة: <code>{sym.replace('/USDT:USDT', '/USDT')}</code>", reply_to=msg_id)
                st["hit"] = -1; st["closed"] = True; continue
            if st["hit"] < 1 and last >= tps[0]:
                st["hit"] = 1; tg_send(f"✅ <b>الهدف الأول محقق!</b>\nالسعر: <code>{last}</code>\n<code>{sym.replace('/USDT:USDT', '/USDT')}</code>", reply_to=msg_id)
            if st["hit"] < 2 and last >= tps[1]:
                st["hit"] = 2; tg_send(f"🎯 <b>الهدف الثاني محقق!</b>\nالسعر: <code>{last}</code>\n<code>{sym.replace('/USDT:USDT', '/USDT')}</code>", reply_to=msg_id)
            if st["hit"] < 3 and last >= tps[2]:
                st["hit"] = 3; st["closed"] = True
                tg_send(f"🏆 <b>جميع الأهداف محققة!</b>\nالسعر: <code>{last}</code>\n<code>{sym.replace('/USDT:USDT', '/USDT')}</code>\n\n💎 <b>تم إغلاق الصفقة</b>", reply_to=msg_id)
        else:
            if last >= sl and st["hit"] >= 0:
                tg_send(f"🛑 <b>وقف خسارة مفعل</b>\nالسعر: <code>{last}</code>\n<code>{sym.replace('/USDT:USDT', '/USDT')}</code>", reply_to=msg_id)
                st["hit"] = -1; st["closed"] = True; continue
            if st["hit"] < 1 and last <= tps[0]:
                st["hit"] = 1; tg_send(f"✅ <b>الهدف الأول محقق!</b>\nالسعر: <code>{last}</code>\n<code>{sym.replace('/USDT:USDT', '/USDT')}</code>", reply_to=msg_id)
            if st["hit"] < 2 and last <= tps[1]:
                st["hit"] = 2; tg_send(f"🎯 <b>الهدف الثاني محقق!</b>\nالسعر: <code>{last}</code>\n<code>{sym.replace('/USDT:USDT', '/USDT')}</code>", reply_to=msg_id)
            if st["hit"] < 3 and last <= tps[2]:
                st["hit"] = 3; st["closed"] = True
                tg_send(f"🏆 <b>جميع الأهداف محققة!</b>\nالسعر: <code>{last}</code>\n<code>{sym.replace('/USDT:USDT', '/USDT')}</code>\n\n💎 <b>تم إغلاق الصفقة</b>", reply_to=msg_id)

# ====================== RELAXATION LOGIC ======================
def _relaxed_thresholds(level:int):
    """
    level 0: صارم (A) — use global MIN_CONFIDENCE, RR_MIN, MIN_DOLLAR_VOLUME
    level 1: مرن  (B) — تخفيف خفيف
    level 2: مرن جدًا (C) — تخفيف أكبر + يُفضّل HEAVY
    """
    if level <= 0:
        return MIN_CONFIDENCE, RR_MIN, MIN_DOLLAR_VOLUME, MAX_SPREAD_PCT
    if level == 1:
        return max(60.0, MIN_CONFIDENCE - 6.0), max(1.12, RR_MIN * 0.9), MIN_DOLLAR_VOLUME * 0.8, MAX_SPREAD_PCT * 1.15
    # level >=2
    return max(58.0, MIN_CONFIDENCE - 10.0), max(1.05, RR_MIN * 0.85), MIN_DOLLAR_VOLUME * 0.65, MAX_SPREAD_PCT * 1.25

def _tier_of_level(level:int) -> str:
    return "A" if level<=0 else ("B" if level==1 else "C")

# ====================== SCAN & FALLBACK ======================
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
                    dvol = min_vol * 1.3  # تقدير للعملات الثقيلة في المستوى C

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
                # مرونة خاصة للعملات الثقيلة
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
        print(f"\n📊 [DIAG] level={relax_level} من أصل {len(symbols[:SCAN_TOP])} عملة:")
        print(f"   dvol:{stats['dvol']} spr:{stats['spr']} last:{stats['last']} ohlcv:{stats['ohlcv']} strategy:{stats['strategy']} rr:{stats['rr']} conf:{stats['conf']} cool:{stats['cool']}")
        print(f"   معايير: conf≥{min_conf:.1f}% RR≥{rr_min:.2f} vol≥${min_vol:,.0f} spread≤{max_spread*100:.3f}%")

    picks.sort(key=lambda x: (x["tier"], x["conf"], x["dvol"]), reverse=False)  # A قبل B قبل C
    picks.sort(key=lambda x: (x["conf"], x["dvol"]), reverse=True)
    return picks[:MAX_SIGNALS]

def premium_fallback(symbols: List[str], relax_level:int=2) -> Optional[dict]:
    min_conf, rr_min, min_vol, max_spread = _relaxed_thresholds(relax_level)
    best = None; best_score = -1
    for sym in symbols[:60]:
        try:
            if cooldown_until.get(sym, 0) > time.time(): continue
            dvol, spread, last_px = fetch_market_meta(sym)
            if dvol < min_vol: continue
            if spread > max_spread*1.1: continue
            if not last_px: continue
            df = fetch_ohlcv(sym, limit=BARS)
            if df is None: continue
            side, meta = enhanced_strategy_signals(df)
            if side is None: continue
            if meta["confidence"] < (min_conf - 2): continue
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

# ====================== MAIN LOOP ===========================
def main():
    global no_pick_streak
    banner = f"{now_utc_str()} - 🚀 Enhanced Trading Bot ({MODE} mode) starting..."
    print(banner, flush=True); log(banner)

    try:
        symbols = build_universe_top_by_volume(SCAN_TOP)
    except Exception as e:
        log(f"load_markets error: {e}"); symbols = []

    if not symbols:
        print("⚠️ لا يوجد أزواج للتداول.", flush=True)
        tg_send("⚠️ خطأ في تحميل قائمة العملات من Binance")
        time.sleep(15); return

    print(f"{now_utc_str()} - ✅ تم تحميل {len(symbols)} زوج للفحص", flush=True)
    log(f"Loaded {len(symbols)} trading pairs")
    tg_send(f"🤖 <b>Enhanced Bot Started</b>\nMode: <b>{MODE}</b> (High Quality)\nTF: <b>{TIMEFRAME}</b>\nPairs: <b>{len(symbols)}</b>\nConf≥<b>{MIN_CONFIDENCE}%</b> RR≥<b>1:{RR_MIN}</b>\n{now_utc_str()}")

    cycle = 0
    while True:
        try:
            cycle += 1
            if PING_EACH_SCAN or cycle % 10 == 1:
                tg_send(f"🔍 Scanning cycle #{cycle} • {now_utc_str()}")
            print(f"\n{now_utc_str()} - 🔎 دورة الفحص #{cycle} - أعلى {SCAN_TOP} عملة...", flush=True)
            log(f"Scan cycle #{cycle}")

            # 1) مستوى A (صارم)
            picks = scan_symbols(symbols, relax_level=0)

            # 2) إذا ما فيه: جرّب مستوى B (مرن)
            if not picks:
                picks = scan_symbols(symbols, relax_level=1)

            # 3) إذا ما فيه: مستوى C (مرن جدًا + HEAVY مرونة أكبر)
            if not picks:
                picks = scan_symbols(symbols, relax_level=2)

            if not picks:
                no_pick_streak += 1
                print(f"⏳ لا توجد فرص ضمن المعايير بعد 3 مستويات. streak={no_pick_streak}", flush=True)
                # بديل مميز
                fallback = premium_fallback(symbols, relax_level=2)
                if fallback:
                    print(f"💡 بديل: {fallback['symbol']} - ثقة {fallback['conf']:.1f}%", flush=True)
                    send_signal(fallback); no_pick_streak = 0
                else:
                    print(f"⌛ انتظار {SLEEP_BETWEEN} ثانية", flush=True)
            else:
                no_pick_streak = 0
                print(f"🎯 تم العثور على {len(picks)} فرصة!", flush=True)
                for i, pick in enumerate(picks, 1):
                    print(f"   #{i}: {pick['symbol']} {pick['side']} - ثقة {pick['conf']:.1f}% [{pick.get('tier','A')}]", flush=True)
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
