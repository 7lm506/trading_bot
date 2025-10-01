# -*- coding: utf-8 -*-
"""
Trading_bot_v7_plus (multi-exchange, region-safe)
- استراتيجية v7 المُحسّنة: EMA20/50 recent cross + Volume Spike
- TP = 1.2× / 2.2× / 3.5× ATR | SL = 1.15× ATR (افتراضي لتقليل SL المبكر)
- بعد TP1: SL→BE | بعد TP2: تتبّع EMA20 مع وسادة 0.3×ATR
- تقرير «لماذا لا توجد توصيات» + Near-Qualify
- تخزين XLSX (openpyxl) أو CSV تلقائياً
- يدعم Binance(spot/futures)، Bybit، OKX، Bitget مع Fallback تلقائي عند الحجب (451)

بيئة التشغيل (اختياري):
EXCHANGE=binanceusdm|bybit|okx|bitget|binance   (افتراضي binanceusdm)
FALLBACKS=bybit,okx,bitget,binance
DERIVATIVES=1  (1 للمشتقات، 0 للسبوت)
TIMEFRAME=5m
BARS=600
SCAN_TOP=250
MIN_DOLLAR_VOLUME=1000000
MAX_SPREAD_PCT=0.20
MAX_SIGNALS=3
COOLDOWN_HOURS=3
MIN_VOLUME_SPIKE=1.5
MIN_CONFIDENCE=78.0
MIN_RR=1.25
SLEEP_BETWEEN=25
DATA_DIR=/data
TG_TOKEN=...
TG_CHAT_ID=...
"""

import os, time, logging, requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from typing import Dict, List, Optional

# ============ Telegram ============
TOKEN   = os.getenv("TG_TOKEN", "").strip()
CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
API_URL = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else None

def tg(msg: str):
    try:
        if not API_URL or not CHAT_ID:
            print("[TG disabled]", msg.replace("\n", " | ")[:300])
            return
        r = requests.post(
            f"{API_URL}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=12
        )
        print("TG status:", r.status_code)
    except Exception as e:
        logging.error(f"TG err: {e}")

def now_utc():
    return datetime.now(timezone.utc)

# ============ إعدادات عامة ============
EXCHANGE_ID   = os.getenv("EXCHANGE", "binanceusdm").strip().lower()
FALLBACKS_ENV = os.getenv("FALLBACKS", "bybit,okx,bitget,binance")
FALLBACKS     = [x.strip().lower() for x in FALLBACKS_ENV.split(",") if x.strip()]

DERIVATIVES   = os.getenv("DERIVATIVES", "1").strip() == "1"  # 1=margins/perps, 0=spot
QUOTE         = os.getenv("QUOTE", "USDT").strip()

TIMEFRAME = os.getenv("TIMEFRAME", "5m")
BARS      = int(os.getenv("BARS", "600"))
SCAN_TOP  = int(os.getenv("SCAN_TOP", "250"))

MIN_DOLLAR_VOLUME = float(os.getenv("MIN_DOLLAR_VOLUME", "1000000"))  # 1M$
MAX_SPREAD_PCT    = float(os.getenv("MAX_SPREAD_PCT", "0.20")) / 100  # 0.20%
MAX_SIGNALS       = int(os.getenv("MAX_SIGNALS", "3"))
COOLDOWN_HOURS    = float(os.getenv("COOLDOWN_HOURS", "3"))

ATR_TP1 = float(os.getenv("ATR_TP1", "1.2"))
ATR_TP2 = float(os.getenv("ATR_TP2", "2.2"))
ATR_TP3 = float(os.getenv("ATR_TP3", "3.5"))
ATR_SL  = float(os.getenv("ATR_SL",  "1.15"))

MIN_VOLUME_SPIKE  = float(os.getenv("MIN_VOLUME_SPIKE", "1.5"))
MIN_CONFIDENCE    = float(os.getenv("MIN_CONFIDENCE", "78.0"))
MIN_RR_RATIO      = float(os.getenv("MIN_RR", "1.25"))

SLEEP_BETWEEN     = int(os.getenv("SLEEP_BETWEEN", "25"))

DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__)); os.makedirs(DATA_DIR, exist_ok=True)

# ============ تخزين (XLSX/CSV) ============
try:
    from openpyxl import Workbook, load_workbook
    HAS_XLSX = True
except Exception:
    HAS_XLSX = False

SIG_F = os.path.join(DATA_DIR, "signals_v7_plus.xlsx" if HAS_XLSX else "signals_v7_plus.csv")
REJ_F = os.path.join(DATA_DIR, "reject_v7_plus.xlsx"  if HAS_XLSX else "reject_v7_plus.csv")

def init_store():
    if HAS_XLSX:
        if not os.path.exists(SIG_F):
            wb = Workbook(); ws = wb.active; ws.title = "Data"
            ws.append(["#", "Pair", "Side", "Entry", "TP1", "TP2", "TP3", "SL",
                       "Result", "EntryTime", "ExitTime", "DurationMin",
                       "ProfitPct", "EntryReason", "ExitReason"])
            wb.save(SIG_F)
        if not os.path.exists(REJ_F):
            wb = Workbook(); ws = wb.active; ws.title = "Data"
            ws.append(["#", "Pair", "RejectReason", "Confidence", "SpreadPct",
                       "DollarVol", "ATR", "VolumeSpike", "Time"])
            wb.save(REJ_F)
    else:
        if not os.path.exists(SIG_F):
            pd.DataFrame(columns=["#", "Pair", "Side", "Entry", "TP1", "TP2", "TP3", "SL",
                                  "Result", "EntryTime", "ExitTime", "DurationMin",
                                  "ProfitPct", "EntryReason", "ExitReason"]).to_csv(SIG_F, index=False)
        if not os.path.exists(REJ_F):
            pd.DataFrame(columns=["#", "Pair", "RejectReason", "Confidence", "SpreadPct",
                                  "DollarVol", "ATR", "VolumeSpike", "Time"]).to_csv(REJ_F, index=False)
init_store()

def sig_append(row: List):
    try:
        if HAS_XLSX:
            wb = load_workbook(SIG_F); ws = wb.active
            row[0] = ws.max_row
            ws.append(row); wb.save(SIG_F)
        else:
            df = pd.read_csv(SIG_F)
            row[0] = len(df) + 1
            df.loc[len(df)] = row
            df.to_csv(SIG_F, index=False)
    except Exception as e:
        logging.error(f"sig_append err: {e}")

def rej_append(row: List):
    try:
        if HAS_XLSX:
            wb = load_workbook(REJ_F); ws = wb.active
            row[0] = ws.max_row
            ws.append(row); wb.save(REJ_F)
        else:
            df = pd.read_csv(REJ_F)
            row[0] = len(df) + 1
            df.loc[len(df)] = row
            df.to_csv(REJ_F, index=False)
    except Exception as e:
        logging.error(f"rej_append err: {e}")

# ============ Exchange (multi + fallback) ============
import ccxt

def make_exchange(exchange_id: str):
    exchange_id = exchange_id.lower().strip()
    if exchange_id == "binanceusdm":
        return ccxt.binanceusdm({"enableRateLimit": True, "options": {"defaultType": "future"}, "timeout": 25000})
    if exchange_id == "binance":
        return ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}, "timeout": 25000})
    if exchange_id == "bybit":
        # Bybit: perpetuals = swap
        return ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap" if DERIVATIVES else "spot"}, "timeout": 25000})
    if exchange_id == "okx":
        return ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "swap" if DERIVATIVES else "spot"}, "timeout": 25000})
    if exchange_id == "bitget":
        return ccxt.bitget({"enableRateLimit": True, "options": {"defaultType": "swap" if DERIVATIVES else "spot"}, "timeout": 25000})
    # افتراضي: binance spot
    return ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}, "timeout": 25000})

def init_exchange():
    """حاول التهيئة مع fallback تلقائي عند حجب المنطقة أو الأخطاء."""
    candidates = [EXCHANGE_ID] + [x for x in FALLBACKS if x != EXCHANGE_ID]
    last_err = None
    for exid in candidates:
        try:
            ex = make_exchange(exid)
            ex.load_markets()
            tg(f"🔌 <b>Connected</b> → <code>{exid}</code> • Mode: {'Derivatives' if DERIVATIVES else 'Spot'}")
            return ex, exid
        except Exception as e:
            last_err = e
            emsg = str(e)
            logging.error(f"init_exchange {exid} err: {emsg}")
            # 451 أو حجب: جرّب التالي
            continue
    raise RuntimeError(f"All exchanges failed. Last error: {last_err}")

ex, EX_ID = init_exchange()

# ============ TA ============

def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def sma(s, n): return s.rolling(window=n).mean()
def rsi(s, n=14):
    d = s.diff(); u, d = np.maximum(d, 0), np.maximum(-d, 0)
    rs = u.ewm(span=n, adjust=False).mean() / (d.ewm(span=n, adjust=False).mean() + 1e-12)
    return 100 - 100 / (1 + rs)
def macd(s, f=12, sl=26): return ema(s, f) - ema(s, sl)
def macd_sig(m): return ema(m, 9)
def tr(h, l, c):
    pc = c.shift(1)
    return pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
def atr(h, l, c, n=14): return tr(h, l, c).ewm(span=n, adjust=False).mean()

def adx(h, l, c, n=14):
    plus_dm = h.diff()
    minus_dm = -l.diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0
    tr_val = tr(h, l, c)
    atr_val = tr_val.ewm(span=n, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(span=n, adjust=False).mean() / (atr_val + 1e-12))
    minus_di = 100 * (minus_dm.ewm(span=n, adjust=False).mean() / (atr_val + 1e-12))
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di + 1e-12)
    return dx.ewm(span=n, adjust=False).mean(), plus_di, minus_di

# ============ سوق ============

def ohlcv(sym: str, lim=BARS) -> Optional[pd.DataFrame]:
    try:
        d = ex.fetch_ohlcv(sym, TIMEFRAME, limit=lim)
        if len(d) < 200: return None
        df = pd.DataFrame(d, columns=["ts", "o", "h", "l", "c", "v"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        for col in ["o", "h", "l", "c", "v"]:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        logging.error(f"ohlcv err {sym}: {e}")
        return None

def last_price(sym: str) -> Optional[float]:
    try:
        t = ex.fetch_ticker(sym)
        return float(t.get("last") or t.get("close"))
    except Exception:
        return None

def dollar_vol(df: pd.DataFrame) -> float:
    try:
        return float((df["c"].iloc[-30:] * df["v"].iloc[-30:]).sum())
    except Exception:
        return 0.0

def spread_pct(sym: str) -> float:
    try:
        ob = ex.fetch_order_book(sym, limit=5)
        bid = ob["bids"][0][0] if ob.get("bids") else None
        ask = ob["asks"][0][0] if ob.get("asks") else None
        if not bid or not ask: return 1.0
        return (ask - bid) / ask
    except Exception:
        return 1.0

def list_top_symbols() -> List[str]:
    """اختيار أفضل الأزواج: نشطة + QUOTE + نوع السوق + أعلى سيولة."""
    mkts = ex.markets
    syms = []
    for s, d in mkts.items():
        if not d.get("active"): continue
        if d.get("quote") != QUOTE: continue
        if DERIVATIVES:
            # اقبل swap/future/linear إن وُجد
            if not (d.get("swap") or d.get("future") or d.get("linear")):
                continue
        else:
            if not d.get("spot"):
                continue
        syms.append(s)

    # رتّب باستخدام fetch_tickers (quoteVolume أو baseVolume*last)
    try:
        ticks = ex.fetch_tickers(syms)
        def notional(t):
            if not t: return 0.0
            qv = t.get("quoteVolume")
            if qv: return float(qv)
            last = t.get("last") or t.get("close") or 0
            bv = t.get("baseVolume") or 0
            try:
                return float(last) * float(bv)
            except Exception:
                return 0.0
        syms.sort(key=lambda x: notional(ticks.get(x, {})), reverse=True)
    except Exception as e:
        logging.error(f"fetch_tickers sort err: {e}")

    return syms[:SCAN_TOP]

# ============ الاستراتيجية ============

def strat(sym: str, df: pd.DataFrame):
    c, h, l, v = df["c"], df["h"], df["l"], df["v"]
    e20, e50 = ema(c, 20), ema(c, 50)
    macd_v, macd_s = macd(c), macd_sig(macd(c))
    rsi14 = rsi(c)
    atr14 = float(atr(h, l, c).iloc[-1])

    # اتجاه
    if e20.iloc[-1] > e50.iloc[-1]:
        side = "LONG"
    elif e20.iloc[-1] < e50.iloc[-1]:
        side = "SHORT"
    else:
        return None, {"atr": atr14, "reasons": ["no_clear_trend"]}

    # تقاطع حديث أو تلاصق EMAs
    def recent_cross(_fast, _slow, _side):
        if _side == "LONG":
            return (_fast.iloc[-1] > _slow.iloc[-1]) and (_fast.iloc[-6:-1] <= _slow.iloc[-6:-1]).any()
        else:
            return (_fast.iloc[-1] < _slow.iloc[-1]) and (_fast.iloc[-6:-1] >= _slow.iloc[-6:-1]).any()

    if not recent_cross(e20, e50, side):
        ema_dist = abs(e20.iloc[-1] - e50.iloc[-1]) / (e50.iloc[-1] + 1e-12) * 100
        if ema_dist > 0.35:
            return None, {"atr": atr14, "reasons": ["no_recent_cross"]}

    # Spike
    vol_spike = v.iloc[-1] / (v.rolling(20).mean().iloc[-1] + 1e-12)
    if vol_spike < MIN_VOLUME_SPIKE:
        return None, {"atr": atr14, "reasons": [f"low_vol_{vol_spike:.1f}x"], "vol_spike": vol_spike}

    # Momentum
    if side == "LONG" and (macd_v.iloc[-1] <= macd_s.iloc[-1] or rsi14.iloc[-1] >= 72):
        return None, {"atr": atr14, "reasons": ["macd_not_bull" if macd_v.iloc[-1] <= macd_s.iloc[-1] else f"rsi_overbought_{rsi14.iloc[-1]:.0f}"]}
    if side == "SHORT" and (macd_v.iloc[-1] >= macd_s.iloc[-1] or rsi14.iloc[-1] <= 28):
        return None, {"atr": atr14, "reasons": ["macd_not_bear" if macd_v.iloc[-1] >= macd_s.iloc[-1] else f"rsi_oversold_{rsi14.iloc[-1]:.0f}"]}

    # شمعة تأكيد
    if side == "LONG" and c.iloc[-1] <= c.iloc[-2]:
        return None, {"atr": atr14, "reasons": ["no_conf_long"]}
    if side == "SHORT" and c.iloc[-1] >= c.iloc[-2]:
        return None, {"atr": atr14, "reasons": ["no_conf_short"]}

    # ADX بسيط
    adx_v, plus_di, minus_di = adx(h, l, c)
    adx_now = float(adx_v.iloc[-1])
    if adx_now < 16:
        return None, {"atr": atr14, "reasons": [f"weak_adx_{int(adx_now)}"], "vol_spike": vol_spike}

    reasons = [
        "ema20_50_trend",
        "recent_cross_or_tight",
        f"vol_spike_{vol_spike:.1f}x",
        "macd_ok",
        "rsi_ok",
        f"adx_{int(adx_now)}"
    ]
    return side, {"atr": atr14, "reasons": reasons, "vol_spike": vol_spike}

def make_targets(entry: float, atr_val: float, side: str):
    if side == "LONG":
        tps = [round(entry + atr_val * ATR_TP1, 6),
               round(entry + atr_val * ATR_TP2, 6),
               round(entry + atr_val * ATR_TP3, 6)]
        sl  = round(entry - atr_val * ATR_SL, 6)
    else:
        tps = [round(entry - atr_val * ATR_TP1, 6),
               round(entry - atr_val * ATR_TP2, 6),
               round(entry - atr_val * ATR_TP3, 6)]
        sl  = round(entry + atr_val * ATR_SL, 6)
    return tps, sl

# ============ دورة المسح ============
cooldown: Dict[str, float] = {}
open_trades: Dict[str, dict] = {}
reject_stats: Dict[str, int] = {}

def scan_cycle() -> List[dict]:
    try:
        syms = list_top_symbols()
    except Exception as e:
        logging.error(f"list_top_symbols err: {e}")
        return []

    picks, near_qualify = [], []
    reject_stats.clear()

    for sym in syms:
        try:
            if cooldown.get(sym, 0) > time.time():
                reject_stats["cooldown"] = reject_stats.get("cooldown", 0) + 1
                continue
            if sym in open_trades and not open_trades[sym].get("closed"):
                continue

            df = ohlcv(sym)
            if df is None:
                reject_stats["ohlcv_fail"] = reject_stats.get("ohlcv_fail", 0) + 1
                continue

            dv = dollar_vol(df)
            if dv < MIN_DOLLAR_VOLUME:
                reject_stats["low_dollar_vol"] = reject_stats.get("low_dollar_vol", 0) + 1
                continue

            spr = spread_pct(sym)
            if spr > MAX_SPREAD_PCT:
                reject_stats["high_spread"] = reject_stats.get("high_spread", 0) + 1
                continue

            lp = last_price(sym)
            if not lp:
                reject_stats["no_price"] = reject_stats.get("no_price", 0) + 1
                continue

            side, meta = strat(sym, df)
            if side is None:
                reason = meta["reasons"][0] if meta.get("reasons") else "unknown"
                reject_stats[reason] = reject_stats.get(reason, 0) + 1
                rej_append([0, sym.replace("/USDT:USDT", "/USDT"), reason, 0.0,
                            round(spr*100, 3), round(dv, 0),
                            round(meta.get("atr", 0), 6), round(meta.get("vol_spike", 0) or 0, 2),
                            now_utc().strftime("%Y-%m-%d %H:%M")])
                continue

            entry = float(lp)
            tps, sl = make_targets(entry, meta["atr"], side)

            tp1_dist = abs(tps[0] - entry)
            sl_dist  = abs(sl - entry)
            rr_ratio = (tp1_dist / sl_dist) if sl_dist > 0 else 0.0
            if rr_ratio < MIN_RR_RATIO:
                reject_stats["poor_rr"] = reject_stats.get("poor_rr", 0) + 1
                if rr_ratio > MIN_RR_RATIO * 0.9:
                    near_qualify.append(f"{sym}: rr={rr_ratio:.2f}")
                continue

            picks.append({
                "symbol": sym,
                "side": side,
                "entry": entry,
                "tps": tps,
                "sl": sl,
                "start_time": now_utc().strftime("%Y-%m-%d %H:%M"),
                "reasons": meta["reasons"],
                "rr": rr_ratio
            })

        except Exception as e:
            logging.error(f"scan {sym} err: {e}")

    if not picks:
        send_reject_summary(near_qualify)

    return picks[:MAX_SIGNALS]

def send_reject_summary(near_qualify: List[str]):
    if not reject_stats:
        return
    total = sum(reject_stats.values()) or 1
    th = (f"عتبات: RR≥{MIN_RR_RATIO:.2f} • vol_spike≥{MIN_VOLUME_SPIKE:.2f} • "
          f"SL={ATR_SL:.2f}×ATR • TP1={ATR_TP1:.2f}×ATR")
    lines = ["📋 <b>لماذا لا توجد توصيات (هذه الدورة)</b>", th, "أكثر أسباب الرفض:"]
    for k, v in sorted(reject_stats.items(), key=lambda x: x[1], reverse=True)[:6]:
        pct = (v/total*100)
        lines.append(f"• {k}: {v} ({pct:.0f}%)")
    if near_qualify:
        lines.append("\nقريبة من التأهيل:")
        lines.extend([f"• {x}" for x in near_qualify[:6]])
    tg("\n".join(lines))

# ============ إرسال ============
def send_signal(sig: dict):
    side_emoji = "🚀" if sig["side"] == "LONG" else "🔻"
    txt = (f"⚡ <b>v7+ • {EX_ID.upper()}</b>\n"
           f"{side_emoji} <b>{sig['side']}</b> <code>{sig['symbol'].replace('/USDT:USDT','/USDT')}</code>\n\n"
           f"💰 Entry: <code>{sig['entry']}</code>\n"
           f"🎯 TP1: <code>{sig['tps'][0]}</code> | TP2: <code>{sig['tps'][1]}</code> | TP3: <code>{sig['tps'][2]}</code>\n"
           f"🛑 SL: <code>{sig['sl']}</code>\n\n"
           f"⚖️ R/R: <b>1:{sig['rr']:.2f}</b>\n"
           f"🔎 Signals: <i>{' • '.join(sig['reasons'][:5])}</i>\n"
           f"🛡️ Risk: SL→BE بعد TP1 • تتبّع EMA20 بعد TP2")
    tg(txt)
    open_trades[sig["symbol"]] = {**sig, "tp1_hit": False, "tp2_hit": False, "closed": False}
    cooldown[sig["symbol"]] = time.time() + COOLDOWN_HOURS * 3600

# ============ تتبّع ============
def trail_sl_using_ema20(sym: str, st: dict, df: Optional[pd.DataFrame] = None) -> float:
    try:
        df = df or ohlcv(sym, 120)
        if df is None: return st["sl"]
        c, h, l = df["c"], df["h"], df["l"]
        e20 = ema(c, 20)
        atr14 = float(atr(h, l, c).iloc[-1])
        if st["side"] == "LONG":
            new_sl = max(st.get("be_price", st["sl"]), round(float(e20.iloc[-1] - 0.3*atr14), 6))
        else:
            new_sl = min(st.get("be_price", st["sl"]), round(float(e20.iloc[-1] + 0.3*atr14), 6))
        return new_sl
    except Exception:
        return st["sl"]

def track_open_trades():
    for sym, st in list(open_trades.items()):
        if st.get("closed"):
            continue
        lp = last_price(sym)
        if lp is None:
            continue

        side, tps, sl, entry = st["side"], st["tps"], st["sl"], st["entry"]
        res, exit_rsn = None, ""

        # إدارة بعد TP1/TP2
        if side == "LONG":
            if not st["tp1_hit"] and lp >= tps[0]:
                st["tp1_hit"] = True
                st["be_price"] = entry
                st["sl"] = max(st["sl"], entry)
                tg(f"✅ TP1 • SL→BE\n<code>{sym.replace('/USDT:USDT','/USDT')}</code> @ <code>{lp}</code>")
            if st["tp1_hit"] and not st["tp2_hit"] and lp >= tps[1]:
                st["tp2_hit"] = True
                st["sl"] = trail_sl_using_ema20(sym, st)
                tg(f"🎯 TP2 • SL تتبّع EMA20→ <code>{st['sl']}</code>\n<code>{sym.replace('/USDT:USDT','/USDT')}</code>")
        else:
            if not st["tp1_hit"] and lp <= tps[0]:
                st["tp1_hit"] = True
                st["be_price"] = entry
                st["sl"] = min(st["sl"], entry)
                tg(f"✅ TP1 • SL→BE\n<code>{sym.replace('/USDT:USDT','/USDT')}</code> @ <code>{lp}</code>")
            if st["tp1_hit"] and not st["tp2_hit"] and lp <= tps[1]:
                st["tp2_hit"] = True
                st["sl"] = trail_sl_using_ema20(sym, st)
                tg(f"🎯 TP2 • SL تتبّع EMA20→ <code>{st['sl']}</code>\n<code>{sym.replace('/USDT:USDT','/USDT')}</code>")

        # تحقق TP3/SL
        if side == "LONG":
            if lp <= st["sl"]:
                res, exit_rsn = "SL", "stopped"
            elif lp >= tps[2]:
                res, exit_rsn = "TP3", "trend_cont"
        else:
            if lp >= st["sl"]:
                res, exit_rsn = "SL", "stopped"
            elif lp <= tps[2]:
                res, exit_rsn = "TP3", "trend_cont"

        if res:
            exit_t = now_utc().strftime("%Y-%m-%d %H:%M")
            dur = (datetime.strptime(exit_t, "%Y-%m-%d %H:%M") -
                   datetime.strptime(st["start_time"], "%Y-%m-%d %H:%M")).total_seconds() / 60.0
            profit_pct = ((lp - entry) / entry * 100.0) if side == "LONG" else ((entry - lp) / entry * 100.0)
            sig_append([0, sym.replace("/USDT:USDT","/USDT"), side, entry,
                        tps[0], tps[1], tps[2], st["sl"], res,
                        st["start_time"], exit_t, round(dur, 1), round(profit_pct, 2),
                        " • ".join(st.get("reasons", [])[:4]), exit_rsn])

            emoji = {"SL": "🛑", "TP3": "🏆"}.get(res, "✅")
            tg(f"{emoji} <b>{res}</b> {sym.replace('/USDT:USDT','/USDT')} @ <code>{lp}</code>\n"
               f"P/L: <b>{profit_pct:+.2f}%</b> • مدة: {dur:.0f}m")

            st["closed"] = True
            cd_hours = COOLDOWN_HOURS * (2.2 if res == "SL" else 1.0)
            cooldown[sym] = time.time() + cd_hours * 3600

# ============ نافذة الدخول ============
def entry_window_ok() -> bool:
    if not TIMEFRAME.endswith("m"):
        return True
    frame = int(TIMEFRAME[:-1] or "5")
    m = now_utc().minute
    sec_in_candle = (m % frame) * 60 + now_utc().second
    return 60 <= sec_in_candle <= 120  # دقيقة 1–2 من الشمعة

# ============ Main ============
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    tg(
        f"🤖 <b>Bot Started - v7+ (EMA20/50)</b>\n"
        f"🔌 Exchange: <code>{EX_ID}</code> • Mode: {'Derivatives' if DERIVATIVES else 'Spot'}\n"
        f"⏱️ TF: {TIMEFRAME} • 🔝 Top: {SCAN_TOP}\n"
        f"🛑 SL: {ATR_SL:.2f}×ATR • 🎯 TP: {ATR_TP1:.2f}/{ATR_TP2:.2f}/{ATR_TP3:.2f}×ATR • RR≥{MIN_RR_RATIO:.2f}\n"
        f"💧 MinVol: ${MIN_DOLLAR_VOLUME:,.0f} • 📉 MaxSpread: {MAX_SPREAD_PCT*100:.3f}%\n"
        f"💾 Store: {'XLSX' if HAS_XLSX else 'CSV'} → {DATA_DIR}"
    )

    while True:
        try:
            if entry_window_ok():
                for s in scan_cycle():
                    send_signal(s)
            # تتبع الصفقات خلال فترات النوم
            for _ in range(max(1, SLEEP_BETWEEN // 5)):
                track_open_trades()
                time.sleep(5)
        except KeyboardInterrupt:
            tg("🛑 Bot Stopped")
            break
        except Exception as e:
            logging.error(f"main err: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
