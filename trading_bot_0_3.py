# -*- coding: utf-8 -*-
"""
Trading_bot_v7_plus.py  —  "القليل الموثوق"
هجين من v7 مع تحسينات إدارة المخاطر وتقليل SL المبكّر

الملامح:
- دخول: تقاطع EMA20/50 حديث + Volume Spike ≥ 1.5× + ADX≥18 + شمعة تأكيد
- منع المطاردة: منع الدخول لو امتداد السعر عن EMA20 > 1.2×ATR
- SL ذكي: max(1.25×ATR, مسافة خلف EMA50 بـ 0.35×ATR)
- TP: 1.0, 2.0, 3.2 × ATR  (RR≥1.18 إجباري)
- بعد TP1: SL→Break-Even  |  بعد TP2: تتبع EMA20
- كول داون 3 ساعات للعملة | سيولة ≥ 1M$ | سبريد ≤ 0.20%
- تقرير تشخيصي في كل دورة + تقرير خسائر كل ساعتين
"""
import os, time, logging, requests, pandas as pd, numpy as np
from datetime import datetime, timezone, timedelta
from openpyxl import Workbook, load_workbook
import ccxt

# ========= Telegram =========
TG_TOKEN   = os.getenv("TG_TOKEN",   "8130568386:AAGmpxKQw1XhqNjtj2OBzJ_-e3_vn0FE5Bs")
TG_CHAT_ID = int(os.getenv("TG_CHAT_ID", "8429537293"))
TG_API     = f"https://api.telegram.org/bot{TG_TOKEN}"

def tg(text: str):
    try:
        requests.post(f"{TG_API}/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=12)
    except Exception as e:
        logging.error(f"TG send err: {e}")

# ========= Exchange =========
ex = ccxt.binanceusdm({
    "enableRateLimit": True,
    "options": {"defaultType": "future"},
    "timeout": 25000
})

# ========= Config =========
TIMEFRAME = "5m"
BARS = 500
SCAN_TOP = 200
SLEEP_BETWEEN = 30                # فاصل بين دورات المسح
MIN_DOLLAR_VOLUME = 1_000_000     # جودة أعلى
MAX_SPREAD_PCT    = 0.20/100
COOLDOWN_HOURS    = 3
MIN_RR            = 1.18
ADX_MIN           = 18
VOL_SPIKE_MIN     = 1.5
EXTENSION_MAX_ATR = 1.2           # منع مطاردة السعر
MIN_CONF          = 70.0          # لأغراض التبويب/التقارير

ATR_MULT_TP = (1.0, 2.0, 3.2)
SL_ATR_BASE = 1.25
SL_BEHIND_EMA50_ATR = 0.35        # هامش إضافي خلف EMA50

# ========= Files =========
BASE_DIR = os.path.dirname(__file__)
SIG_XLSX = os.path.join(BASE_DIR, "signals_v7_plus.xlsx")
REJ_XLSX = os.path.join(BASE_DIR, "reject_v7_plus.xlsx")

def init_excels():
    if not os.path.exists(SIG_XLSX):
        wb = Workbook(); ws = wb.active; ws.title = "Signals"
        ws.append(["#", "Pair", "Side", "Entry", "TP1", "TP2", "TP3", "SL",
                   "Result", "EntryTime", "ExitTime", "DurationMin", "ProfitPct",
                   "EntryReason", "ExitReason"])
        wb.save(SIG_XLSX)
    if not os.path.exists(REJ_XLSX):
        wb = Workbook(); ws = wb.active; ws.title = "Rejects"
        ws.append(["#", "Pair", "Reason", "ADX", "VolSpike", "ExtATR", "Spread%", "DVol", "Time"])
        wb.save(REJ_XLSX)
init_excels()

def xl_append_signal(sig: dict, res: str, exit_t: str, dur_min: float, pl_pct: float, exit_rsn: str):
    try:
        wb = load_workbook(SIG_XLSX); ws = wb.active
        ws.append([ws.max_row,
                   sig["symbol"].replace("/USDT:USDT", "/USDT"),
                   sig["side"], sig["entry"], *sig["tps"], sig["sl"],
                   res, sig["start_time"], exit_t, round(dur_min,1), round(pl_pct,2),
                   " • ".join(sig.get("reasons", [])[:4]), exit_rsn])
        wb.save(SIG_XLSX)
    except Exception as e:
        logging.error(f"XL signal err: {e}")

def xl_append_reject(sym: str, reason: str, adx: float, vsp: float, ext: float, spr: float, dvol: float):
    try:
        wb = load_workbook(REJ_XLSX); ws = wb.active
        ws.append([ws.max_row, sym.replace("/USDT:USDT","/USDT"), reason,
                   round(adx,1), round(vsp,2), round(ext,2), round(spr*100,3),
                   round(dvol,0), datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")])
        wb.save(REJ_XLSX)
    except Exception as e:
        logging.error(f"XL reject err: {e}")

# ========= TA =========
def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def rsi(s, n=14):
    d = s.diff(); up, dn = np.maximum(d,0), np.maximum(-d,0)
    rs = up.ewm(span=n, adjust=False).mean() / (dn.ewm(span=n, adjust=False).mean() + 1e-12)
    return 100 - 100 / (1 + rs)
def tr(h, l, c):
    pc = c.shift(1)
    return pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
def atr(h, l, c, n=14): return tr(h,l,c).ewm(span=n, adjust=False).mean()
def adx(h, l, c, n=14):
    up = h.diff(); dn = -l.diff()
    up[up < 0] = 0; dn[dn < 0] = 0
    trv = tr(h,l,c); atrv = trv.ewm(span=n, adjust=False).mean()
    pdi = 100 * (up.ewm(span=n, adjust=False).mean() / (atrv + 1e-12))
    mdi = 100 * (dn.ewm(span=n, adjust=False).mean() / (atrv + 1e-12))
    dx  = 100 * (abs(pdi - mdi) / (pdi + mdi + 1e-12))
    return dx.ewm(span=n, adjust=False).mean(), pdi, mdi

# ========= Market =========
def ohlcv(sym, lim=BARS):
    try:
        data = ex.fetch_ohlcv(sym, TIMEFRAME, limit=lim)
        if not data or len(data) < 200: return None
        df = pd.DataFrame(data, columns=["ts","o","h","l","c","v"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        for col in ["o","h","l","c","v"]:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        logging.error(f"ohlcv {sym}: {e}")
        return None

def last_price(sym):
    try:
        return float(ex.fetch_ticker(sym)["last"])
    except Exception:
        return None

def spread_pct(sym):
    try:
        ob = ex.fetch_order_book(sym, limit=5)
        bid, ask = ob["bids"][0][0], ob["asks"][0][0]
        return (ask - bid) / ask if ask else 1.0
    except Exception:
        return 1.0

def dollar_vol(df):
    try:
        return float((df["c"].iloc[-30:] * df["v"].iloc[-30:]).sum())
    except Exception:
        return 0.0

# ========= Strategy =========
def strat(sym, df):
    c,h,l,v = df["c"], df["h"], df["l"], df["v"]
    last = float(c.iloc[-1])
    e20, e50 = ema(c,20), ema(c,50)
    atr14 = float(atr(h,l,c).iloc[-1])
    adx14, pdi, mdi = adx(h,l,c)
    adx_cur = float(adx14.iloc[-1])
    vsp = float(v.iloc[-1] / (v.rolling(20).median().iloc[-1] + 1e-12))

    # الاتجاه
    if e20.iloc[-1] > e50.iloc[-1]:
        side = "LONG"
    elif e20.iloc[-1] < e50.iloc[-1]:
        side = "SHORT"
    else:
        xl_append_reject(sym, "no_trend", adx_cur, vsp, 0.0, 0.0, 0.0)
        return None, {"reasons":["no_trend"], "atr":atr14}

    # تقاطع حديث خلال آخر 6 شمعات
    def crossed_recent(f, s, side):
        rng = range(1, 7)
        for i in rng:
            prev_ok = f.iloc[-i-1] <= s.iloc[-i-1] if side=="LONG" else f.iloc[-i-1] >= s.iloc[-i-1]
            now_ok  = f.iloc[-i]   >  s.iloc[-i]   if side=="LONG" else f.iloc[-i]   <  s.iloc[-i]
            if prev_ok and now_ok:
                return True, i
        return False, None

    cross_ok, cross_age = crossed_recent(e20, e50, side)
    if not cross_ok:
        xl_append_reject(sym, "no_recent_cross", adx_cur, vsp, 0.0, 0.0, 0.0)
        return None, {"reasons":["no_recent_cross"], "atr":atr14}

    # حجم
    if vsp < VOL_SPIKE_MIN:
        xl_append_reject(sym, f"low_vol_{vsp:.1f}x", adx_cur, vsp, 0.0, 0.0, dollar_vol(df))
        return None, {"reasons":["low_volume"], "atr":atr14}

    # ADX
    if adx_cur < ADX_MIN:
        xl_append_reject(sym, f"weak_adx_{int(adx_cur)}", adx_cur, vsp, 0.0, 0.0, dollar_vol(df))
        return None, {"reasons":["weak_adx"], "atr":atr14}

    # منع المطاردة: امتداد عن EMA20 بأكثر من 1.2×ATR
    ext_atr = abs(last - e20.iloc[-1]) / (atr14 + 1e-12)
    if ext_atr > EXTENSION_MAX_ATR:
        xl_append_reject(sym, "extended_from_ema20", adx_cur, vsp, ext_atr, 0.0, dollar_vol(df))
        return None, {"reasons":["extended"], "atr":atr14}

    # شمعة تأكيد باتجاه الترند
    if side=="LONG" and not (c.iloc[-1] > c.iloc[-2]): 
        xl_append_reject(sym, "no_confirm_long", adx_cur, vsp, ext_atr, 0.0, dollar_vol(df)); 
        return None, {"reasons":["no_conf_long"], "atr":atr14}
    if side=="SHORT" and not (c.iloc[-1] < c.iloc[-2]):
        xl_append_reject(sym, "no_confirm_short", adx_cur, vsp, ext_atr, 0.0, dollar_vol(df));
        return None, {"reasons":["no_conf_short"], "atr":atr14}

    # RSI ضد التشبع
    r = float(rsi(c).iloc[-1])
    if (side=="LONG" and r>=70) or (side=="SHORT" and r<=30):
        xl_append_reject(sym, f"rsi_extreme_{int(r)}", adx_cur, vsp, ext_atr, 0.0, dollar_vol(df))
        return None, {"reasons":["rsi_extreme"], "atr":atr14}

    reasons = [f"ema20_50_cross_{cross_age}c", f"vol_spike_{vsp:.1f}x", f"adx_{int(adx_cur)}", "confirm_candle", "rsi_ok"]
    return side, {"atr":atr14, "adx":adx_cur, "vsp":vsp, "reasons":reasons}

# ========= Targets & SL =========
def build_sl(entry, side, atr_val, ema50_last):
    sl_atr = SL_ATR_BASE * atr_val
    if side=="LONG":
        sl_by_ema = entry - max(sl_atr, (entry - (ema50_last - SL_BEHIND_EMA50_ATR*atr_val)))
        # sl_by_ema يحسب كفارق، نريد سعر SL الحقيقي:
        sl = min(entry - SL_ATR_BASE*atr_val, ema50_last - SL_BEHIND_EMA50_ATR*atr_val)
    else:
        sl = max(entry + SL_ATR_BASE*atr_val, ema50_last + SL_BEHIND_EMA50_ATR*atr_val)
    return round(sl, 6)

def targets(entry, atr_val, side):
    m1, m2, m3 = ATR_MULT_TP
    if side=="LONG":
        t1 = entry + m1*atr_val; t2 = entry + m2*atr_val; t3 = entry + m3*atr_val
    else:
        t1 = entry - m1*atr_val; t2 = entry - m2*atr_val; t3 = entry - m3*atr_val
    return [round(t1,6), round(t2,6), round(t3,6)]

# ========= Entry window (اختياري خفيف) =========
def entry_window_ok():
    # افتح النافذة من الدقيقة 0:30 إلى 2:30 لكل شمعة 5م لمنع الدخول في أول/آخر ثواني
    now = datetime.now(timezone.utc)
    sec = (now.minute % 5)*60 + now.second
    return 30 <= sec <= 150

# ========= Scan / Send / Track =========
cooldown = {}          # symbol -> unix time
open_trades = {}       # symbol -> state dict
reject_counter = {}    # سبب الرفض في الدورة

def scan():
    global reject_counter
    reject_counter = {}
    try:
        mkts = ex.load_markets()
        syms = [s for s,d in mkts.items() if d.get("linear") and d.get("quote")=="USDT" and d.get("active")]
        ticks = ex.fetch_tickers()
        syms.sort(key=lambda s: float((ticks.get(s) or {}).get("quoteVolume") or 0), reverse=True)
        syms = syms[:SCAN_TOP]
    except Exception as e:
        logging.error(f"load markets: {e}")
        return []

    picks = []
    for sym in syms:
        try:
            if cooldown.get(sym, 0) > time.time(): 
                reject_counter["cooldown"] = reject_counter.get("cooldown",0)+1
                continue

            df = ohlcv(sym, BARS)
            if df is None:
                reject_counter["ohlcv_fail"] = reject_counter.get("ohlcv_fail",0)+1
                continue

            dvol = dollar_vol(df)
            if dvol < MIN_DOLLAR_VOLUME:
                reject_counter["low_dvol"] = reject_counter.get("low_dvol",0)+1
                continue

            spr = spread_pct(sym)
            if spr > MAX_SPREAD_PCT:
                reject_counter["high_spread"] = reject_counter.get("high_spread",0)+1
                continue

            lp = last_price(sym)
            if not lp:
                reject_counter["no_price"] = reject_counter.get("no_price",0)+1
                continue

            side, meta = strat(sym, df)
            if side is None:
                reason = meta["reasons"][0] if meta.get("reasons") else "unknown"
                reject_counter[reason] = reject_counter.get(reason,0)+1
                continue

            # أهداف و SL
            e50 = float(ema(df["c"],50).iloc[-1])
            sl = build_sl(lp, side, meta["atr"], e50)
            tps = targets(lp, meta["atr"], side)

            # R/R check على TP1
            rr = abs((tps[0]-lp)/(sl-lp)) if (sl-lp)!=0 else 0
            if rr < MIN_RR:
                reject_counter["poor_rr"] = reject_counter.get("poor_rr",0)+1
                continue

            picks.append({
                "symbol": sym, "side": side, "entry": float(lp),
                "tps": tps, "sl": float(sl),
                "atr": meta["atr"], "reasons": meta["reasons"],
                "start_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "state": "OPEN", "tp_stage": 0
            })
        except Exception as e:
            logging.error(f"scan {sym}: {e}")
    return picks

def send(sig):
    emoji = "🚀" if sig["side"]=="LONG" else "🔻"
    txt = (f"<b>⚡ توصية v7+</b>\n"
           f"{emoji} <b>{sig['side']}</b> <code>{sig['symbol'].replace('/USDT:USDT','/USDT')}</code>\n\n"
           f"💰 Entry: <code>{sig['entry']}</code>\n"
           f"🎯 TP1: <code>{sig['tps'][0]}</code> | TP2: <code>{sig['tps'][1]}</code> | TP3: <code>{sig['tps'][2]}</code>\n"
           f"🛑 SL: <code>{sig['sl']}</code>\n\n"
           f"🔎 Signals: <i>{' • '.join(sig['reasons'][:4])}</i>\n"
           f"🛡️ Risk: SL→BE بعد TP1 • تتبع EMA20 بعد TP2")
    tg(txt)
    open_trades[sig["symbol"]] = sig
    cooldown[sig["symbol"]] = time.time() + COOLDOWN_HOURS*3600

def track():
    for sym, st in list(open_trades.items()):
        if st.get("state") != "OPEN": 
            continue
        lp = last_price(sym); 
        if not lp: 
            continue

        side = st["side"]; tps = st["tps"]; sl = st["sl"]; entry = st["entry"]
        e20 = float(ema(ohlcv(sym, 120)["c"],20).iloc[-1]) if True else entry

        # مراحل إدارة الصفقة
        if side=="LONG":
            # ضرب SL؟
            if lp <= sl:
                res_exit(sym, st, "SL", lp, "Stop Loss")
                continue
            # TP stages
            if st["tp_stage"] < 1 and lp >= tps[0]:
                st["tp_stage"] = 1
                st["sl"] = entry  # BE
                tg(f"✅ TP1 • SL→BE\nالسعر: <code>{lp}</code>\n{sym.replace('/USDT:USDT','/USDT')}")
            elif st["tp_stage"] < 2 and lp >= tps[1]:
                st["tp_stage"] = 2
                st["sl"] = max(st["sl"], e20)  # تتبع EMA20
                tg(f"🎯 TP2 • تتبع SL عند EMA20≈<code>{st['sl']:.6f}</code>\n{sym.replace('/USDT:USDT','/USDT')}")
            elif lp >= tps[2]:
                res_exit(sym, st, "TP3", lp, "Target 3 🎯")
        else:
            if lp >= sl:
                res_exit(sym, st, "SL", lp, "Stop Loss")
                continue
            if st["tp_stage"] < 1 and lp <= tps[0]:
                st["tp_stage"] = 1
                st["sl"] = entry  # BE
                tg(f"✅ TP1 • SL→BE\nالسعر: <code>{lp}</code>\n{sym.replace('/USDT:USDT','/USDT')}")
            elif st["tp_stage"] < 2 and lp <= tps[1]:
                st["tp_stage"] = 2
                st["sl"] = min(st["sl"], e20)  # تتبع EMA20
                tg(f"🎯 TP2 • تتبع SL عند EMA20≈<code>{st['sl']:.6f}</code>\n{sym.replace('/USDT:USDT','/USDT')}")
            elif lp <= tps[2]:
                res_exit(sym, st, "TP3", lp, "Target 3 🎯")

def res_exit(sym, st, result, price, exit_reason):
    exit_t = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    dur = (datetime.strptime(exit_t, "%Y-%m-%d %H:%M")
           - datetime.strptime(st["start_time"], "%Y-%m-%d %H:%M")).total_seconds()/60
    if st["side"]=="LONG":
        pl_pct = (price - st["entry"]) / st["entry"] * 100
    else:
        pl_pct = (st["entry"] - price) / st["entry"] * 100
    xl_append_signal(st, result, exit_t, dur, pl_pct, exit_reason)
    emoji = {"SL":"🛑", "TP1":"✅", "TP2":"🎉", "TP3":"🏆"}.get(result, "✅")
    tg(f"{emoji} <b>{result}</b> {sym.replace('/USDT:USDT','/USDT')} @ <code>{price}</code>\n"
       f"P/L: <b>{pl_pct:+.2f}%</b> • مدة: {dur:.0f}m\n"
       f"خروج: {exit_reason}")
    st["state"] = "CLOSED"

# ========= Diagnostics =========
last_diag = 0
def send_cycle_diag():
    global last_diag
    reasons_sorted = sorted(reject_counter.items(), key=lambda x: x[1], reverse=True)
    lines = [f"📋 <b>لماذا لا توجد توصيات (الدورة)</b>",
             f"عتبات: ADX≥{ADX_MIN} • RR≥{MIN_RR} • vol≥{VOL_SPIKE_MIN}× • امتداد≤{EXTENSION_MAX_ATR}×ATR"]
    if reasons_sorted:
        lines.append("أكثر أسباب الرفض:")
        for k,v in reasons_sorted[:6]:
            lines.append(f"• {k}: {v}")
    tg("\n".join(lines))
    last_diag = time.time()

def diag_report_2h():
    try:
        wb = load_workbook(SIG_XLSX); ws = wb.active
        if ws.max_row <= 1:
            return "لا توجد بيانات بعد."
        total = ws.max_row - 1
        wins = 0; total_pl = 0.0
        sl_rows = []; tp_rows = []
        for r in range(2, ws.max_row+1):
            res = ws.cell(r,9).value or ""
            pl  = float(ws.cell(r,13).value or 0)
            total_pl += pl
            if res.startswith("TP"): wins += 1
            if res == "SL": sl_rows.append(r)
            if res == "TP3": tp_rows.append(r)
        wr = wins/total if total else 0.0
        worst_pair = "—"
        top_loss_reason = "—"
        if sl_rows:
            # الأكثر خسارة تكرارًا
            pairs = {}
            reasons = {}
            for r in sl_rows:
                p = ws.cell(r,2).value; pairs[p] = pairs.get(p,0)+1
                rsn = ws.cell(r,14).value or ""
                reasons[rsn] = reasons.get(rsn,0)+1
            worst_pair = max(pairs, key=pairs.get)
            top_loss_reason = max(reasons, key=reasons.get)
        avg_tp_dur = "-"
        if tp_rows:
            durs = [float(ws.cell(r,12).value or 0) for r in tp_rows]
            if durs: avg_tp_dur = f"{sum(durs)/len(durs):.0f}m"
        msg = (f"<b>📊 تقرير كل ساعتين</b>\n"
               f"إجمالي الإشارات: {total}\n"
               f"نسبة الفوز: {wr:.0%} • إجمالي P/L: {total_pl:+.1f}%\n"
               f"أسوأ زوج: <code>{worst_pair}</code>\n"
               f"أكثر سبب دخول خسر: <i>{top_loss_reason}</i>\n"
               f"متوسط مدة TP3: {avg_tp_dur}")
        tg(msg)
    except Exception as e:
        logging.error(f"2h report err: {e}")

# ========= Main =========
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    logging.info("🚀 Trading Bot v7+ (موثوق وأقل SL مبكّر)")
    tg("🤖 <b>Bot Started v7+</b>\n"
       f"⏱️ TF: {TIMEFRAME} • 🔝 Top: {SCAN_TOP}\n"
       f"🛑 SL: {SL_ATR_BASE}×ATR خلف EMA50({SL_BEHIND_EMA50_ATR}×ATR)\n"
       f"📈 شروط: EMA20/50 + Vol≥{VOL_SPIKE_MIN}× + ADX≥{ADX_MIN} • RR≥{MIN_RR}")

    last_two_hour = time.time()
    while True:
        try:
            picks = scan()
            # نافذة الدخول
            if picks and not entry_window_ok():
                tg("⏳ لسنا في نافذة دخول (0:30–2:30 من الشمعة). تجاهلت الإشارات لهذه الدورة.")
                picks = []

            for s in picks[:2]:  # أقصى إشارتين/دورة
                send(s)
                time.sleep(2)

            # تتبّع الصفقات
            for _ in range(max(1, SLEEP_BETWEEN//5)):
                track()
                time.sleep(5)

            # تشخيص سريع كل دورة
            send_cycle_diag()

            # تقرير كل ساعتين
            if time.time() - last_two_hour > 2*3600:
                diag_report_2h()
                last_two_hour = time.time()

        except KeyboardInterrupt:
            tg("🛑 Bot stopped")
            break
        except Exception as e:
            logging.error(f"main loop: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
