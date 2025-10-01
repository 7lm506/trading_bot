# -*- coding: utf-8 -*-
"""
Trading Bot v7 Plus (EMA20/50 Cross + Volume Spike) — Multi-Exchange + Robust Rate-Limit Handling
- دخول: اتجاه EMA20/50 + تقاطع حديث ≤ 5 شموع أو تلاصق EMA (≤ 0.25%) + Volume Spike ≥ 1.5x
- TP = 1.2× / 2.2× / 3.5× ATR   |   SL = 1.15× ATR  (أوسع من v7 لتقليل SL المبكر)
- بعد TP1: SL→BE   |   بعد TP2: تتبع EMA20 مع وسادة 0.3×ATR
- تصفية: سيولة ≥ 1,000,000$ | RR ≥ 1.25 | نافذة دخول دقيقة 1–2 من كل شمعة 5م
- تقارير “لماذا لا توجد توصيات” + “قريبة من التأهيل”
- تخزين Excel (openpyxl) أو CSV تلقائيًا
- تبادلات متعددة + Fallback + Backoff لـ OKX 50011

ENV (اختياري):
  EXCHANGE=binanceusdm|bybit|okx|bitget|bingx|mexc|gateio|kucoin|kucoinfutures|binance   (افتراضي binanceusdm)
  FALLBACKS=bybit,okx,bitget,bingx,mexc,gateio,kucoin,kucoinfutures,binance
  DERIVATIVES=1  (1 للمشتقات، 0 للسبوت)
  QUOTE=USDT
  TIMEFRAME=5m
  BARS=600
  SCAN_TOP=200
  MAX_SCAN_PER_LOOP=40
  MIN_DOLLAR_VOLUME=1000000
  MIN_VOLUME_SPIKE=1.5
  MIN_CONFIDENCE=78.0
  MIN_RR=1.25
  ATR_TP1=1.2  ATR_TP2=2.2  ATR_TP3=3.5  ATR_SL=1.15
  COOLDOWN_HOURS=3
  SLEEP_BETWEEN=25
  OKX_PAUSE_MS=300         (تأخير إضافي بعد كل نداء على OKX)
  TG_TOKEN=...  TG_CHAT_ID=...
  OKX_API_KEY/OKX_SECRET/OKX_PASSWORD (لرفع الحدود العامة على بعض النطاقات، اختياري)
"""

import os, time, logging, requests, math
import pandas as pd, numpy as np
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# =================== Telegram ===================
TOKEN   = os.getenv("TG_TOKEN", "").strip()
CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
API_URL = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else None

def tg(msg: str):
    try:
        if not API_URL or not CHAT_ID:
            print("[TG disabled]", msg.replace("\n", " | ")[:500])
            return
        r = requests.post(f"{API_URL}/sendMessage",
                          json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
                          timeout=12)
        print("TG status:", r.status_code)
    except Exception as e:
        logging.error(f"TG err: {e}")

def now_utc():
    return datetime.now(timezone.utc)

# =================== إعدادات ===================
EXCHANGE_ID   = os.getenv("EXCHANGE", "binanceusdm").strip().lower()
FALLBACKS_ENV = os.getenv("FALLBACKS", "bybit,okx,bitget,bingx,mexc,gateio,kucoin,kucoinfutures,binance")
FALLBACKS     = [x.strip().lower() for x in FALLBACKS_ENV.split(",") if x.strip()]
DERIVATIVES   = os.getenv("DERIVATIVES", "1").strip() == "1"
QUOTE         = os.getenv("QUOTE", "USDT").strip()

TIMEFRAME = os.getenv("TIMEFRAME", "5m")
BARS      = int(os.getenv("BARS", "600"))

SCAN_TOP           = int(os.getenv("SCAN_TOP", "200"))
MAX_SCAN_PER_LOOP  = int(os.getenv("MAX_SCAN_PER_LOOP", str(40)))
MIN_DOLLAR_VOLUME  = float(os.getenv("MIN_DOLLAR_VOLUME", "1000000"))  # 1M$
MIN_VOLUME_SPIKE   = float(os.getenv("MIN_VOLUME_SPIKE", "1.5"))
MIN_CONFIDENCE     = float(os.getenv("MIN_CONFIDENCE", "78.0"))
MIN_RR_RATIO       = float(os.getenv("MIN_RR", "1.25"))

ATR_TP1 = float(os.getenv("ATR_TP1", "1.2"))
ATR_TP2 = float(os.getenv("ATR_TP2", "2.2"))
ATR_TP3 = float(os.getenv("ATR_TP3", "3.5"))
ATR_SL  = float(os.getenv("ATR_SL",  "1.15"))

COOLDOWN_HOURS = float(os.getenv("COOLDOWN_HOURS", "3"))
SLEEP_BETWEEN  = int(os.getenv("SLEEP_BETWEEN", "25"))

OKX_PAUSE_MS   = int(os.getenv("OKX_PAUSE_MS", "300"))  # تأخير إضافي لـ OKX

DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__)); os.makedirs(DATA_DIR, exist_ok=True)

# =================== تخزين (XLSX/CSV) ===================
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
            ws.append(["#", "Pair", "RejectReason", "Confidence", "DollarVol",
                       "ATR", "VolumeSpike", "Time"])
            wb.save(REJ_F)
    else:
        if not os.path.exists(SIG_F):
            pd.DataFrame(columns=["#", "Pair", "Side", "Entry", "TP1", "TP2", "TP3", "SL",
                                  "Result", "EntryTime", "ExitTime", "DurationMin",
                                  "ProfitPct", "EntryReason", "ExitReason"]).to_csv(SIG_F, index=False)
        if not os.path.exists(REJ_F):
            pd.DataFrame(columns=["#", "Pair", "RejectReason", "Confidence", "DollarVol",
                                  "ATR", "VolumeSpike", "Time"]).to_csv(REJ_F, index=False)
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

# =================== Exchanges + Fallback ===================
import ccxt

def make_exchange(exchange_id: str):
    x = exchange_id.lower().strip()
    kw = {"enableRateLimit": True, "timeout": 25000, "options": {}}

    if x == "okx":
        # دعم مفاتيح OKX (اختياري) — أحيانًا يرفع حدود Public
        k, s, p = os.getenv("OKX_API_KEY"), os.getenv("OKX_SECRET"), os.getenv("OKX_PASSWORD")
        if k and s and p:
            kw.update({"apiKey": k, "secret": s, "password": p})
        kw["options"]["defaultType"] = "swap" if DERIVATIVES else "spot"
        return ccxt.okx(kw)

    if x == "binanceusdm":
        kw["options"]["defaultType"] = "future"
        return ccxt.binanceusdm(kw)
    if x == "binance":
        kw["options"]["defaultType"] = "spot"
        return ccxt.binance(kw)
    if x == "bybit":
        kw["options"]["defaultType"] = "swap" if DERIVATIVES else "spot"
        return ccxt.bybit(kw)
    if x == "bitget":
        kw["options"]["defaultType"] = "swap" if DERIVATIVES else "spot"
        return ccxt.bitget(kw)
    if x == "bingx":
        kw["options"]["defaultType"] = "swap" if DERIVATIVES else "spot"
        return ccxt.bingx(kw)
    if x == "mexc":
        kw["options"]["defaultType"] = "swap" if DERIVATIVES else "spot"
        return ccxt.mexc(kw)
    if x in ["gateio", "gate"]:
        kw["options"]["defaultType"] = "swap" if DERIVATIVES else "spot"
        return ccxt.gateio(kw)
    if x == "kucoin":
        kw["options"]["defaultType"] = "spot"
        return ccxt.kucoin(kw)
    if x in ["kucoinfutures", "kucoinfuturesusdtm"]:
        kw["options"]["defaultType"] = "swap"
        return ccxt.kucoinfutures(kw)

    kw["options"]["defaultType"] = "spot"
    return ccxt.binance(kw)

def is_region_block(err: Exception) -> bool:
    s = str(err).lower()
    return ("451" in s) or ("restricted location" in s) or ("403 forbidden" in s) or ("cloudfront" in s)

def init_exchange() -> Tuple[ccxt.Exchange, str]:
    candidates = [EXCHANGE_ID] + [x for x in FALLBACKS if x != EXCHANGE_ID]
    last_err = None
    tried = []
    for exid in candidates:
        try:
            ex = make_exchange(exid)
            ex.load_markets()
            tg(f"🔌 <b>Connected</b> → <code>{exid}</code> • Mode: {'Derivatives' if DERIVATIVES else 'Spot'}")
            return ex, exid
        except Exception as e:
            tried.append(exid)
            last_err = e
            logging.error(f"init_exchange {exid} err: {e}")
            continue
    if last_err:
        if is_region_block(last_err):
            tg("🛑 <b>All exchanges blocked from this server region.</b>\n"
               "جرّب <code>EXCHANGE=okx</code> أو <code>bitget</code> أو انقل الخدمة لمنطقة أخرى.")
        else:
            tg(f"🛑 <b>Failed to init any exchange.</b>\nآخر خطأ: <code>{str(last_err)[:250]}</code>")
    raise RuntimeError(f"All exchanges failed. Tried: {tried}. Last error: {last_err}")

ex, EX_ID = init_exchange()

# =================== المؤشرات ===================
def ema(s, n): return s.ewm(span=n, adjust=False).mean()
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

# =================== Utils / Rate limit ===================
def sleep_after_call():
    # تأخير بسيط بعد النداء — OKX أكثر تشدداً
    if EX_ID == "okx":
        time.sleep(max(OKX_PAUSE_MS, 250) / 1000.0)
    else:
        # احترام معدل ccxt العام
        time.sleep(max(getattr(ex, "rateLimit", 120), 120) / 1000.0)

def fetch_ohlcv_retry(symbol: str, limit: int = BARS, tries: int = 4):
    delay = 0.6
    for i in range(tries):
        try:
            d = ex.fetch_ohlcv(symbol, TIMEFRAME, limit=limit)
            sleep_after_call()
            return d
        except Exception as e:
            s = str(e)
            if "50011" in s or "Too Many Requests" in s or "rate limit" in s.lower():
                time.sleep(delay)
                delay *= 1.6
                continue
            logging.error(f"ohlcv err {symbol}: {e}")
            return None
    logging.error(f"ohlcv err {symbol}: too many retries")
    return None

# =================== السوق: ترتيب الرموز بدُفعات ===================
def filter_symbols_by_mode() -> List[str]:
    syms = []
    for sym, m in ex.markets.items():
        if not m.get("active", True):
            continue
        if DERIVATIVES:
            # نفضّل سواب خطي USDT
            if m.get("type") in ("swap","future") and str(m.get("linear")) == "True" and m.get("quote") == QUOTE:
                syms.append(sym)
        else:
            if m.get("type") == "spot" and m.get("quote") == QUOTE:
                syms.append(sym)
    return syms

def chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

_tickers_cache = {"ts": 0.0, "data": {}}
def get_ranked_symbols(all_syms: List[str], refresh_sec: int = 60) -> Tuple[List[str], Dict[str, dict]]:
    # استخدم كاش لمدة قصيرة لتقليل استدعاءات API
    now = time.time()
    if now - _tickers_cache["ts"] < refresh_sec and _tickers_cache["data"]:
        tmap = _tickers_cache["data"]
    else:
        tmap = {}
        # اجلب tickers بدُفعات لتفادي 50011
        for batch in chunked(all_syms, 50 if EX_ID == "okx" else 100):
            try:
                t = ex.fetch_tickers(batch)
                tmap.update(t)
            except Exception as e:
                logging.error(f"fetch_tickers batch err: {e}")
            sleep_after_call()
        _tickers_cache["ts"] = now
        _tickers_cache["data"] = tmap

    def vol_score(sym: str) -> float:
        t = tmap.get(sym, {})
        # حاول استخدام quoteVolume أو last*baseVolume أو info حق OKX
        qv = t.get("quoteVolume") or 0
        if qv:
            return float(qv)
        last = t.get("last") or t.get("close") or 0
        bv = t.get("baseVolume") or 0
        if last and bv:
            try:
                return float(last) * float(bv)
            except:
                pass
        info = t.get("info") or {}
        # OKX: volCcy24h (قيمة) أو volUsd24h
        for k in ("volUsd24h", "volCcy24h", "volCcy24hUsd"):
            if k in info:
                try:
                    return float(info[k])
                except:
                    continue
        return 0.0

    ranked = sorted([s for s in all_syms], key=lambda s: vol_score(s), reverse=True)
    top = ranked[:SCAN_TOP]
    return top, tmap

# =================== Strategy ===================
def ohlcv_df(raw) -> Optional[pd.DataFrame]:
    if not raw or len(raw) < 200:
        return None
    df = pd.DataFrame(raw, columns=["ts","o","h","l","c","v"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    for col in ["o","h","l","c","v"]:
        df[col] = df[col].astype(float)
    return df

def strat(df: pd.DataFrame) -> Tuple[Optional[str], Dict]:
    c, h, l, v = df["c"], df["h"], df["l"], df["v"]
    e20, e50 = ema(c, 20), ema(c, 50)
    atr14 = float(atr(h, l, c).iloc[-1])
    macd_v = macd(c); macd_s = macd_sig(macd_v)
    rsi14 = rsi(c)

    # اتجاه
    if e20.iloc[-1] > e50.iloc[-1]:
        side = "LONG"
    elif e20.iloc[-1] < e50.iloc[-1]:
        side = "SHORT"
    else:
        return None, {"atr": atr14, "reasons": ["no_ema_trend"]}

    # تقاطع حديث ≤ 5 شموع
    cross_recent = False
    for i in range(1, 6):
        if side == "LONG" and (e20.iloc[-i] > e50.iloc[-i]) and (e20.iloc[-i-1] <= e50.iloc[-i-1]):
            cross_recent = True; break
        if side == "SHORT" and (e20.iloc[-i] < e50.iloc[-i]) and (e20.iloc[-i-1] >= e50.iloc[-i-1]):
            cross_recent = True; break

    # أو تلاصق ضيق
    tight = abs(e20.iloc[-1] - e50.iloc[-1]) / (e50.iloc[-1] + 1e-12) * 100 <= 0.25

    # Volume Spike
    vol_spike = v.iloc[-1] / (v.rolling(20).mean().iloc[-1] + 1e-12)

    if not (cross_recent or tight):
        return None, {"atr": atr14, "reasons": ["no_recent_cross"]}
    if vol_spike < MIN_VOLUME_SPIKE:
        return None, {"atr": atr14, "reasons": [f"low_vol_{vol_spike:.1f}x"]}

    # زخم منطقي
    if side == "LONG" and (macd_v.iloc[-1] <= macd_s.iloc[-1] or rsi14.iloc[-1] >= 74):
        return None, {"atr": atr14, "reasons": ["mom_not_long"]}
    if side == "SHORT" and (macd_v.iloc[-1] >= macd_s.iloc[-1] or rsi14.iloc[-1] <= 26):
        return None, {"atr": atr14, "reasons": ["mom_not_short"]}

    # شمعة تأكيد
    if side == "LONG" and c.iloc[-1] <= c.iloc[-2]:
        return None, {"atr": atr14, "reasons": ["no_conf_long"]}
    if side == "SHORT" and c.iloc[-1] >= c.iloc[-2]:
        return None, {"atr": atr14, "reasons": ["no_conf_short"]}

    # ثقة بسيطة
    confidence = 60
    confidence += 18 if cross_recent else 10
    confidence += 12 if vol_spike >= (MIN_VOLUME_SPIKE + 0.3) else 6
    if side == "LONG":
        confidence += 10 if (macd_v.iloc[-1] > macd_s.iloc[-1] and rsi14.iloc[-1] <= 68) else 4
    else:
        confidence += 10 if (macd_v.iloc[-1] < macd_s.iloc[-1] and rsi14.iloc[-1] >= 32) else 4
    confidence = min(confidence, 100)

    reasons = []
    reasons.append("ema_trend")
    reasons.append("fresh_cross" if cross_recent else "ema_tight")
    reasons.append(f"vol_spike_{vol_spike:.1f}x")
    reasons.append("macd_ok"); reasons.append("rsi_ok"); reasons.append("conf_candle")

    return side, {"atr": atr14, "reasons": reasons, "conf": confidence, "vol_spike": vol_spike}

def targets(entry: float, atr_val: float, side: str):
    tps = []
    if side == "LONG":
        tps = [round(entry + atr_val * ATR_TP1, 6),
               round(entry + atr_val * ATR_TP2, 6),
               round(entry + atr_val * ATR_TP3, 6)]
        sl = round(entry - atr_val * ATR_SL, 6)
    else:
        tps = [round(entry - atr_val * ATR_TP1, 6),
               round(entry - atr_val * ATR_TP2, 6),
               round(entry - atr_val * ATR_TP3, 6)]
        sl = round(entry + atr_val * ATR_SL, 6)
    return tps, sl

def entry_window_5m() -> bool:
    # نافذة دخول دقيقة 1–2 من كل شمعة 5 دقائق
    n = now_utc()
    sec = (n.minute % 5) * 60 + n.second
    return 60 <= sec <= 120

# =================== Scan / Send / Track ===================
cooldown: Dict[str, float] = {}
open_trades: Dict[str, dict] = {}
reject_stats: Dict[str, int] = {}
near_misses: List[str] = []
_symbol_ring: List[str] = []

def last_price_from_tickers(sym: str, tmap: Dict[str, dict]) -> Optional[float]:
    t = tmap.get(sym, {})
    v = t.get("last") or t.get("close")
    return float(v) if v is not None else None

def dollar_vol_est(df: pd.DataFrame) -> float:
    try:
        return float((df["c"].iloc[-30:] * df["v"].iloc[-30:]).sum())
    except Exception:
        return 0.0

def scan():
    global _symbol_ring
    try:
        if not _symbol_ring:
            all_syms = filter_symbols_by_mode()
            if EX_ID == "okx":
                # قلل الحمل على OKX
                all_syms = [s for s in all_syms if ((":USDT" in s) if DERIVATIVES else ("/USDT" in s))]
            ranked, tmap = get_ranked_symbols(all_syms)
            _symbol_ring = ranked[:]  # إنشاء حلقة
            scan_candidates = ranked[:MAX_SCAN_PER_LOOP]
        else:
            ranked, tmap = get_ranked_symbols(_symbol_ring)  # نجدد التيكرز فقط
            scan_candidates = _symbol_ring[:MAX_SCAN_PER_LOOP]
            _symbol_ring = _symbol_ring[MAX_SCAN_PER_LOOP:] + _symbol_ring[:MAX_SCAN_PER_LOOP]
    except Exception as e:
        logging.error(f"scan prepare err: {e}")
        return []

    picks = []
    reject_stats.clear()
    near_misses.clear()

    for sym in scan_candidates:
        try:
            if cooldown.get(sym, 0) > time.time():
                reject_stats["cooldown"] = reject_stats.get("cooldown", 0) + 1
                continue

            raw = fetch_ohlcv_retry(sym, BARS)
            if not raw:
                reject_stats["ohlcv_fail"] = reject_stats.get("ohlcv_fail", 0) + 1
                continue
            df = ohlcv_df(raw)
            if df is None:
                reject_stats["short_data"] = reject_stats.get("short_data", 0) + 1
                continue

            dv = dollar_vol_est(df)
            if dv < MIN_DOLLAR_VOLUME:
                reject_stats["low_dollar_vol"] = reject_stats.get("low_dollar_vol", 0) + 1
                continue

            side, meta = strat(df)
            if side is None:
                reason = (meta.get("reasons") or ["unknown"])[-1]
                reject_stats[reason] = reject_stats.get(reason, 0) + 1
                # قريب من التأهيل؟
                if reason.startswith("low_vol_") or reason.startswith("no_recent_cross"):
                    near_misses.append(f"{sym}: {reason}")
                continue

            lp = last_price_from_tickers(sym, tmap)
            if lp is None:
                # آخر سعر من CCXT سريع
                try:
                    t = ex.fetch_ticker(sym); sleep_after_call()
                    lp = float(t.get("last") or t.get("close"))
                except Exception:
                    reject_stats["no_price"] = reject_stats.get("no_price", 0) + 1
                    continue

            tps, sl = targets(lp, meta["atr"], side)
            tp1_dist = abs(tps[0] - lp)
            sl_dist = abs(sl - lp)
            rr = (tp1_dist / sl_dist) if sl_dist > 0 else 0

            if meta["conf"] < MIN_CONFIDENCE:
                reject_stats["conf_low"] = reject_stats.get("conf_low", 0) + 1
                if meta["conf"] >= MIN_CONFIDENCE - 5:
                    near_misses.append(f"{sym}: conf {meta['conf']:.0f}%")
                continue
            if rr < MIN_RR_RATIO:
                reject_stats["poor_rr"] = reject_stats.get("poor_rr", 0) + 1
                if rr >= MIN_RR_RATIO - 0.07:
                    near_misses.append(f"{sym}: rr {rr:.2f}")
                continue

            # نافذة الدخول
            if not entry_window_5m():
                reject_stats["not_entry_window"] = reject_stats.get("not_entry_window", 0) + 1
                continue

            picks.append({
                "symbol": sym,
                "side": side,
                "entry": float(lp),
                "tps": tps,
                "sl": sl,
                "conf": meta["conf"],
                "atr": meta["atr"],
                "vol_spike": meta.get("vol_spike", 1.0),
                "rr": rr,
                "start_time": now_utc().strftime("%Y-%m-%d %H:%M"),
                "reasons": meta["reasons"],
                "stage": 0,  # 0: قبل TP1, 1: بعد TP1 (SL=BE), 2: تتبع EMA20
            })
            if len(picks) >= 3:
                break
        except Exception as e:
            logging.error(f"scan sym {sym}: {e}")

    return picks

def fmt_pair(sym: str) -> str:
    return sym.replace("/USDT:USDT", "/USDT")

def send(sig):
    txt = (f"<b>⚡ توصية v7+</b>\n"
           f"{'🚀 LONG' if sig['side']=='LONG' else '🔻 SHORT'} <code>{fmt_pair(sig['symbol'])}</code>\n\n"
           f"💰 Entry: <code>{sig['entry']}</code>\n"
           f"🎯 TP1: <code>{sig['tps'][0]}</code> | TP2: <code>{sig['tps'][1]}</code> | TP3: <code>{sig['tps'][2]}</code>\n"
           f"🛑 SL: <code>{sig['sl']}</code>\n\n"
           f"📊 Conf: <b>{sig['conf']:.0f}%</b> • ⚖️ R/R: <b>{sig['rr']:.2f}</b> • 📈 Vol: <b>{sig['vol_spike']:.1f}x</b>\n"
           f"🔎 Signals: <i>{' • '.join(sig['reasons'][:4])}</i>\n"
           f"🛡️ Risk: SL→BE بعد TP1 • تتبع EMA20 بعد TP2 (+0.3×ATR)")
    tg(txt)
    open_trades[sig["symbol"]] = sig
    cooldown[sig["symbol"]] = time.time() + COOLDOWN_HOURS * 3600

def trailing_sl_from_ema20(sym: str, side: str, atr_val: float) -> Optional[float]:
    raw = fetch_ohlcv_retry(sym, 60)
    if not raw:
        return None
    df = ohlcv_df(raw)
    if df is None:
        return None
    c = df["c"]
    e20 = ema(c, 20).iloc[-1]
    pad = 0.3 * atr_val
    if side == "LONG":
        return round(float(e20 - pad), 6)
    else:
        return round(float(e20 + pad), 6)

def xl_on_close(st: dict, res: str, lp: float, exit_rsn: str):
    exit_t = now_utc().strftime("%Y-%m-%d %H:%M")
    dur = (datetime.strptime(exit_t, "%Y-%m-%d %H:%M") -
           datetime.strptime(st["start_time"], "%Y-%m-%d %H:%M")).total_seconds() / 60
    if st["side"] == "LONG":
        profit_pct = (lp - st["entry"]) / st["entry"] * 100
    else:
        profit_pct = (st["entry"] - lp) / st["entry"] * 100
    sig_append([0, fmt_pair(st["symbol"]), st["side"], st["entry"],
                st["tps"][0], st["tps"][1], st["tps"][2], st["sl"],
                res, st["start_time"], exit_t, round(dur,1), round(profit_pct,2),
                " • ".join(st.get("reasons", [])[:4]), exit_rsn])

def track():
    for sym, st in list(open_trades.items()):
        if st.get("closed"): 
            continue
        try:
            t = ex.fetch_ticker(sym); sleep_after_call()
            lp = float(t.get("last") or t.get("close"))
        except Exception:
            continue

        side, tps, entry = st["side"], st["tps"], st["entry"]
        res, exit_rsn = None, ""

        # مراحل الإدارة
        if st["stage"] == 0:
            # TP1 أو SL
            if (side == "LONG" and lp >= tps[0]) or (side == "SHORT" and lp <= tps[0]):
                st["sl"] = entry  # BE
                st["stage"] = 1
                tg(f"🔧 <b>إدارة الصفقة</b> {fmt_pair(sym)} → SL إلى BE @ <code>{st['sl']}</code>")
            elif (side == "LONG" and lp <= st["sl"]) or (side == "SHORT" and lp >= st["sl"]):
                res, exit_rsn = "SL", "Stop Loss"

        elif st["stage"] == 1:
            # TP2 أو SL(BE)
            if (side == "LONG" and lp >= tps[1]) or (side == "SHORT" and lp <= tps[1]):
                st["stage"] = 2
                tg(f"🔧 <b>إدارة الصفقة</b> {fmt_pair(sym)} → بدء تتبع EMA20 (+0.3×ATR)")
            elif (side == "LONG" and lp <= st["sl"]) or (side == "SHORT" and lp >= st["sl"]):
                res, exit_rsn = "BE", "Break Even"

        else:  # stage 2: trailing
            tsl = trailing_sl_from_ema20(sym, side, st["atr"])
            if tsl is not None:
                st["sl"] = tsl
            # TP3 أو SL المتتبع
            if (side == "LONG" and lp >= tps[2]) or (side == "SHORT" and lp <= tps[2]):
                res, exit_rsn = "TP3", "Target 3 🎯"
            elif (side == "LONG" and lp <= st["sl"]) or (side == "SHORT" and lp >= st["sl"]):
                res, exit_rsn = "TSL", "Trailing SL (EMA20)"

        if res:
            xl_on_close(st, res, lp, exit_rsn)
            emoji = {"SL":"🛑", "TP3":"🏆", "BE":"➖", "TSL":"🧲"}.get(res, "✅")
            tg(f"{emoji} <b>{res}</b> — {fmt_pair(sym)} @ <code>{lp}</code>\n"
               f"⏱️ منذ: {st['start_time']} • إدارة: {exit_rsn}")
            st["closed"] = True
            # كول داون ديناميكي بسيط
            cooldown[sym] = time.time() + (COOLDOWN_HOURS * (2.0 if res == "SL" else 1.0)) * 3600

# =================== تقارير الرفض ===================
_last_report_ts = 0.0
def send_cycle_report():
    global _last_report_ts
    if time.time() - _last_report_ts < 20*60:  # كل 20 دقيقة
        return
    _last_report_ts = time.time()

    total = sum(reject_stats.values())
    if total == 0:
        return

    # أهم الأسباب
    top = sorted(reject_stats.items(), key=lambda x: x[1], reverse=True)[:6]
    lines = [f"📋 <b>لماذا لا توجد توصيات (هذه الدورة)</b>",
             f"عتبات: conf≥{MIN_CONFIDENCE:.0f} • RR≥{MIN_RR_RATIO:.2f} • vol_spike≥{MIN_VOLUME_SPIKE:.2f}",
             f"Exchange: {EX_ID.upper()} • Mode: {'Derivatives' if DERIVATIVES else 'Spot'}",
             "أكثر أسباب الرفض:"]
    for k, v in top:
        pct = (v/total*100) if total>0 else 0
        lines.append(f"• {k}: {v} ({pct:.0f}%)")

    if near_misses:
        lines.append("\nقريبة من التأهيل:")
        lines.extend([f"• {s}" for s in near_misses[:6]])

    tg("\n".join(lines))

# =================== Main ===================
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    tg("🤖 <b>Bot Started - v7 Plus (EMA20/50 + Volume)</b>\n"
       f"⏱️ TF: {TIMEFRAME} • 🔝 Top: {SCAN_TOP} • 🔄/دورة: {MAX_SCAN_PER_LOOP}\n"
       f"🎯 TP: {ATR_TP1}/{ATR_TP2}/{ATR_TP3}×ATR • 🛑 SL: {ATR_SL}×ATR\n"
       f"⚙️ Exchange: {EX_ID.upper()} • Mode: {'Derivatives' if DERIVATIVES else 'Spot'}")

    while True:
        try:
            picks = scan()
            if picks:
                for s in picks:
                    send(s)
                    time.sleep(2)
            else:
                send_cycle_report()

            # تتبّع المراكز المفتوحة
            track()

            time.sleep(SLEEP_BETWEEN)
        except KeyboardInterrupt:
            tg("🛑 <b>Bot Stopped</b>")
            break
        except Exception as e:
            logging.error(f"main err: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
