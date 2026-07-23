"""
╔══════════════════════════════════════════════════════════╗
║     Bitunix AI Bot – LSOB Backtesting v3                ║
║                                                         ║
║  Verbesserungen gegenüber v1:                           ║
║  1. Session-Filter (nur 14:00-22:00 UTC)               ║
║  2. ADX > 25 Filter (nur in Trendphasen)               ║
║  3. FVG max 3 Kerzen alt                               ║
║  4. Kerzen-Close Bestätigung                           ║
║  5. Multi-OB Stacking                                  ║
╚══════════════════════════════════════════════════════════╝
"""

import json
import math
import os
import time
import csv
from datetime import datetime, timedelta
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

SYMBOLS    = ["ETHUSDT", "HBARUSDT"]
INTERVAL   = "1h"
YEARS_BACK = 3
MIN_VOL    = 0.5

# Asset-spezifische SL/TP (aus LSOB v1 – beste Ergebnisse)
SL_TP = {
    "ETHUSDT":  {"sl": 0.010, "tp": 0.030},
    "HBARUSDT": {"sl": 0.010, "tp": 0.030},
}

# Session-Filter: NY Session 14:00-22:00 UTC
SESSION_START = 14
SESSION_END   = 22

# ADX Periode
ADX_PERIOD = 14
ADX_MIN    = 25  # Nur handeln wenn Trend stark genug

BINANCE_BASE = "https://fapi.binance.com"
REPORT_FILE  = "backtest_lsob_v3_results.json"
CSV_FILE     = "backtest_lsob_v3_trades.csv"

# ── Farben ────────────────────────────────────────────────────────────────────
class C:
    GREEN  = "\033[92m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    GRAY   = "\033[90m"
    BOLD   = "\033[1m"
    RESET  = "\033[0m"

def green(t):  return f"{C.GREEN}{t}{C.RESET}"
def red(t):    return f"{C.RED}{t}{C.RESET}"
def yellow(t): return f"{C.YELLOW}{t}{C.RESET}"
def cyan(t):   return f"{C.CYAN}{t}{C.RESET}"
def gray(t):   return f"{C.GRAY}{t}{C.RESET}"
def bold(t):   return f"{C.BOLD}{t}{C.RESET}"

# ── ADX Berechnung ────────────────────────────────────────────────────────────
def calc_adx(candles, period=14):
    """
    ADX (Average Directional Index) misst Trendstärke.
    ADX > 25 = starker Trend (gut zum Handeln)
    ADX < 25 = Seitwärtsmarkt (schlechte Signale)
    """
    if len(candles) < period * 2:
        return None

    plus_dm  = []
    minus_dm = []
    trs      = []

    for i in range(1, len(candles)):
        high  = candles[i]["high"]
        low   = candles[i]["low"]
        prev_high = candles[i-1]["high"]
        prev_low  = candles[i-1]["low"]
        prev_close= candles[i-1]["close"]

        up_move   = high - prev_high
        down_move = prev_low - low

        plus_dm.append(up_move   if up_move > down_move and up_move > 0   else 0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)

        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    # Smoothed averages
    def smooth(data, p):
        s = sum(data[:p])
        result = [s]
        for x in data[p:]:
            s = s - s/p + x
            result.append(s)
        return result

    smooth_tr    = smooth(trs, period)
    smooth_plus  = smooth(plus_dm, period)
    smooth_minus = smooth(minus_dm, period)

    if not smooth_tr or smooth_tr[-1] == 0:
        return None

    plus_di  = 100 * smooth_plus[-1]  / smooth_tr[-1]
    minus_di = 100 * smooth_minus[-1] / smooth_tr[-1]

    if plus_di + minus_di == 0:
        return None

    dx  = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
    return round(dx, 1)

# ── Session-Filter ────────────────────────────────────────────────────────────
def is_in_session(timestamp_ms):
    """Prüft ob die Kerze in der NY Session liegt (14:00-22:00 UTC)"""
    dt   = datetime.utcfromtimestamp(timestamp_ms / 1000)
    hour = dt.hour
    return SESSION_START <= hour < SESSION_END

# ── Swing High / Low ──────────────────────────────────────────────────────────
def find_swing_highs_lows(candles, lookback=5):
    swing_highs = []
    swing_lows  = []
    for i in range(lookback, len(candles) - lookback):
        high = candles[i]["high"]
        low  = candles[i]["low"]
        if all(candles[i-j]["high"] < high and candles[i+j]["high"] < high for j in range(1, lookback+1)):
            swing_highs.append({"idx": i, "price": high})
        if all(candles[i-j]["low"] > low  and candles[i+j]["low"]  > low  for j in range(1, lookback+1)):
            swing_lows.append({"idx": i, "price": low})
    return swing_highs, swing_lows

# ── BOS ───────────────────────────────────────────────────────────────────────
def detect_bos(candles, swing_highs, swing_lows):
    if not swing_highs or not swing_lows:
        return None
    close = candles[-1]["close"]
    if close > swing_highs[-1]["price"]:
        return {"type": "BULLISH_BOS", "level": swing_highs[-1]["price"]}
    elif close < swing_lows[-1]["price"]:
        return {"type": "BEARISH_BOS", "level": swing_lows[-1]["price"]}
    return None

# ── CHoCH ─────────────────────────────────────────────────────────────────────
def detect_choch(candles, swing_highs, swing_lows):
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None
    rh = swing_highs[-3:] if len(swing_highs) >= 3 else swing_highs
    rl = swing_lows[-3:]  if len(swing_lows)  >= 3 else swing_lows
    lower_highs = all(rh[i]["price"] > rh[i+1]["price"] for i in range(len(rh)-1))
    lower_lows  = all(rl[i]["price"] > rl[i+1]["price"] for i in range(len(rl)-1))
    higher_highs= all(rh[i]["price"] < rh[i+1]["price"] for i in range(len(rh)-1))
    higher_lows = all(rl[i]["price"] < rl[i+1]["price"] for i in range(len(rl)-1))
    price = candles[-1]["close"]
    if lower_highs and lower_lows and price > rh[-1]["price"]:
        return {"type": "BULLISH_CHOCH", "level": rh[-1]["price"]}
    if higher_highs and higher_lows and price < rl[-1]["price"]:
        return {"type": "BEARISH_CHOCH", "level": rl[-1]["price"]}
    return None

# ── Order Blocks mit Stacking ─────────────────────────────────────────────────
def find_order_blocks(candles, lookback=20):
    """
    NEU: Multi-OB Stacking – wenn 2 OBs übereinander liegen = stärkeres Signal
    """
    obs   = []
    start = max(0, len(candles) - lookback)

    for i in range(start, len(candles) - 3):
        next_c = candles[i+1:i+4]
        if len(next_c) < 3:
            continue

        if candles[i]["close"] < candles[i]["open"]:
            moves = [(c["close"]-c["open"])/c["open"]*100 for c in next_c]
            if all(m > 0 for m in moves) and sum(moves) > 0.9:
                obs.append({"type": "BULLISH_OB", "high": candles[i]["high"],
                            "low": candles[i]["low"], "idx": i, "strength": sum(moves)})

        if candles[i]["close"] > candles[i]["open"]:
            moves = [(c["close"]-c["open"])/c["open"]*100 for c in next_c]
            if all(m < 0 for m in moves) and sum(moves) < -0.9:
                obs.append({"type": "BEARISH_OB", "high": candles[i]["high"],
                            "low": candles[i]["low"], "idx": i, "strength": abs(sum(moves))})

    # OB Stacking prüfen – überlagernde OBs = doppelte Stärke
    for i, ob1 in enumerate(obs):
        for ob2 in obs[i+1:]:
            if ob1["type"] == ob2["type"]:
                lo1 = min(ob1["high"], ob1["low"])
                hi1 = max(ob1["high"], ob1["low"])
                lo2 = min(ob2["high"], ob2["low"])
                hi2 = max(ob2["high"], ob2["low"])
                # Überlappung?
                if lo1 <= hi2 and lo2 <= hi1:
                    ob1["stacked"] = True
                    ob2["stacked"] = True

    return obs

# ── FVG (max 3 Kerzen alt) ────────────────────────────────────────────────────
def find_fvg(candles, max_age=3):
    """NEU: Nur FVGs die maximal 3 Kerzen alt sind"""
    fvgs  = []
    start = max(2, len(candles) - max_age - 2)
    for i in range(start, len(candles)):
        c0, c1, c2 = candles[i-2], candles[i-1], candles[i]
        if c2["low"] > c0["high"]:
            fvgs.append({"type": "BULLISH_FVG", "upper": c2["low"],
                        "lower": c0["high"], "idx": i, "age": len(candles)-i})
        if c2["high"] < c0["low"]:
            fvgs.append({"type": "BEARISH_FVG", "upper": c0["low"],
                        "lower": c2["high"], "idx": i, "age": len(candles)-i})
    return fvgs

# ── Liquidity Sweep ───────────────────────────────────────────────────────────
def detect_liquidity_sweep(candles, swing_highs, swing_lows):
    if len(candles) < 2 or not swing_highs or not swing_lows:
        return None
    current = candles[-1]
    prev    = candles[-2]
    last_sh = swing_highs[-1]["price"]
    last_sl = swing_lows[-1]["price"]
    if prev["low"] < last_sl and prev["close"] > last_sl and current["close"] > last_sl:
        return {"type": "BULLISH_SWEEP", "level": last_sl}
    if prev["high"] > last_sh and prev["close"] < last_sh and current["close"] < last_sh:
        return {"type": "BEARISH_SWEEP", "level": last_sh}
    return None

# ── Preis in OB ───────────────────────────────────────────────────────────────
def price_in_ob(price, obs):
    stacked_ob = None
    for ob in obs:
        lo = min(ob["low"], ob["high"])
        hi = max(ob["low"], ob["high"])
        if lo <= price <= hi:
            if ob.get("stacked"):
                stacked_ob = ob  # Bevorzuge gestackte OBs
            elif stacked_ob is None:
                stacked_ob = ob
    return stacked_ob

# ── LSOB v3 Signal ───────────────────────────────────────────────────────────
def generate_signal_v3(candles, current_idx, swing_highs, swing_lows, symbol):
    if current_idx < 60:
        return None, "Zu wenig Daten"

    window  = candles[current_idx-60:current_idx+1]
    current = candles[current_idx]
    price   = current["close"]

    # 1. Session-Filter
    if not is_in_session(current["time"]):
        return None, "Außerhalb NY Session"

    # 2. Volumen-Filter
    volumes   = [c["volume"] for c in window]
    avg_vol   = sum(volumes[-20:]) / 20
    last_vol  = volumes[-1]
    vol_ratio = round(last_vol / avg_vol, 2) if avg_vol > 0 else 0
    if vol_ratio < MIN_VOL:
        return None, f"Volumen {vol_ratio}x"

    # 3. ADX-Filter
    adx = calc_adx(window)
    if adx is None or adx < ADX_MIN:
        return None, f"ADX {adx} < {ADX_MIN}"

    # 4. SMC Komponenten
    sw_highs = [sh for sh in swing_highs if sh["idx"] < current_idx]
    sw_lows  = [sl for sl in swing_lows  if sl["idx"] < current_idx]

    bos      = detect_bos(window, sw_highs[-5:] if sw_highs else [], sw_lows[-5:] if sw_lows else [])
    choch    = detect_choch(window, sw_highs[-5:] if sw_highs else [], sw_lows[-5:] if sw_lows else [])
    obs      = find_order_blocks(window)
    fvgs     = find_fvg(window, max_age=3)
    sweep    = detect_liquidity_sweep(window, sw_highs[-5:] if sw_highs else [], sw_lows[-5:] if sw_lows else [])
    price_ob = price_in_ob(price, obs)

    bull_pts = 0
    bear_pts = 0
    reasons  = []

    # Sweep
    if sweep:
        if sweep["type"] == "BULLISH_SWEEP": bull_pts += 4; reasons.append("BULLISH_SWEEP")
        elif sweep["type"] == "BEARISH_SWEEP": bear_pts += 4; reasons.append("BEARISH_SWEEP")

    # BOS
    if bos:
        if bos["type"] == "BULLISH_BOS": bull_pts += 3; reasons.append("BULLISH_BOS")
        elif bos["type"] == "BEARISH_BOS": bear_pts += 3; reasons.append("BEARISH_BOS")

    # CHoCH
    if choch:
        if choch["type"] == "BULLISH_CHOCH": bull_pts += 3; reasons.append("BULLISH_CHOCH")
        elif choch["type"] == "BEARISH_CHOCH": bear_pts += 3; reasons.append("BEARISH_CHOCH")

    # OB (mit Stacking-Bonus)
    if price_ob:
        pts = 4 if price_ob.get("stacked") else 3
        if price_ob["type"] == "BULLISH_OB":
            bull_pts += pts
            reasons.append("IN_BULLISH_OB_STACKED" if price_ob.get("stacked") else "IN_BULLISH_OB")
        elif price_ob["type"] == "BEARISH_OB":
            bear_pts += pts
            reasons.append("IN_BEARISH_OB_STACKED" if price_ob.get("stacked") else "IN_BEARISH_OB")

    # FVG (nur frische, max 3 Kerzen)
    for fvg in fvgs:
        if fvg["type"] == "BULLISH_FVG": bull_pts += 2; reasons.append("BULLISH_FVG")
        elif fvg["type"] == "BEARISH_FVG": bear_pts += 2; reasons.append("BEARISH_FVG")

    # ADX Bonus bei sehr starkem Trend
    if adx and adx > 35:
        bull_pts += 1; bear_pts += 1  # Beide profitieren von starkem Trend

    total = bull_pts + bear_pts
    if total < 6:
        return None, f"Confluence {total}/6"

    signal     = "HOLD"
    confidence = 0

    if bull_pts > bear_pts and bull_pts >= 6:
        signal     = "BUY"
        confidence = min(int((bull_pts / total) * 100), 99)
    elif bear_pts > bull_pts and bear_pts >= 6:
        signal     = "SELL"
        confidence = min(int((bear_pts / total) * 100), 99)

    if signal == "HOLD" or confidence < 65:
        return None, f"Confidence {confidence}%"

    # 5. Kerzen-Close Bestätigung
    # BUY: aktuelle Kerze muss bullisch schließen
    # SELL: aktuelle Kerze muss bärisch schließen
    if signal == "BUY" and current["close"] < current["open"]:
        return None, "BUY: Kerze schließt bärisch"
    if signal == "SELL" and current["close"] > current["open"]:
        return None, "SELL: Kerze schließt bullisch"

    sl_pct = SL_TP.get(symbol, {"sl": 0.010, "tp": 0.030})["sl"]
    tp_pct = SL_TP.get(symbol, {"sl": 0.010, "tp": 0.030})["tp"]

    if signal == "BUY":
        sl = round(price * (1 - sl_pct), 6)
        tp = round(price * (1 + tp_pct), 6)
    else:
        sl = round(price * (1 + sl_pct), 6)
        tp = round(price * (1 - tp_pct), 6)

    return {
        "signal":     signal,
        "confidence": confidence,
        "price":      price,
        "sl":         sl,
        "tp":         tp,
        "vol_ratio":  vol_ratio,
        "adx":        adx,
        "reasons":    reasons,
        "bull_pts":   bull_pts,
        "bear_pts":   bear_pts,
        "session":    True,
    }, None

# ── Historische Daten laden ───────────────────────────────────────────────────
def fetch_historical(symbol, start_time, end_time):
    all_candles = []
    current     = start_time
    while current < end_time:
        url    = f"{BINANCE_BASE}/fapi/v1/klines"
        params = {"symbol": symbol, "interval": INTERVAL,
                  "startTime": current, "endTime": end_time, "limit": 1000}
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if not data: break
            candles = [{"time": int(c[0]), "open": float(c[1]), "high": float(c[2]),
                        "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])} for c in data]
            all_candles.extend(candles)
            current = candles[-1]["time"] + 1
            time.sleep(0.1)
        except Exception as e:
            print(f"  Fehler: {e}"); time.sleep(2); break
    return all_candles

# ── Backtest ──────────────────────────────────────────────────────────────────
def run_backtest(symbol, candles):
    trades     = []
    open_trade = None

    print(f"\n  Analysiere {len(candles)} Kerzen für {symbol}...")

    swing_highs, swing_lows = find_swing_highs_lows(candles, lookback=5)

    skip_reasons = {}
    total_checked = 0

    for i in range(100, len(candles)):
        current = candles[i]
        price   = current["close"]
        total_checked += 1

        # Offenen Trade prüfen
        if open_trade:
            result = None; pnl = 0.0
            if open_trade["signal"] == "BUY":
                if price >= open_trade["tp"]:   result="WIN";  pnl=round((open_trade["tp"]-open_trade["entry"])/open_trade["entry"]*100,3)
                elif price <= open_trade["sl"]: result="LOSS"; pnl=round((open_trade["sl"]-open_trade["entry"])/open_trade["entry"]*100,3)
            elif open_trade["signal"] == "SELL":
                if price <= open_trade["tp"]:   result="WIN";  pnl=round((open_trade["entry"]-open_trade["tp"])/open_trade["entry"]*100,3)
                elif price >= open_trade["sl"]: result="LOSS"; pnl=round((open_trade["entry"]-open_trade["sl"])/open_trade["entry"]*100,3)
            if i - open_trade["open_idx"] > 48: result="EXPIRED"; pnl=round((price-open_trade["entry"])/open_trade["entry"]*100,3); pnl = -pnl if open_trade["signal"]=="SELL" else pnl
            if result:
                open_trade["result"]=result; open_trade["close_price"]=price
                open_trade["close_time"]=datetime.fromtimestamp(current["time"]/1000).strftime("%Y-%m-%d %H:%M")
                open_trade["pnl"]=pnl; trades.append(open_trade); open_trade=None
            continue

        sig, reason = generate_signal_v3(candles, i, swing_highs, swing_lows, symbol)
        if sig is None:
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            continue

        open_trade = {
            "symbol": symbol, "signal": sig["signal"],
            "entry": sig["price"], "sl": sig["sl"], "tp": sig["tp"],
            "confidence": sig["confidence"], "adx": sig["adx"],
            "vol_ratio": sig["vol_ratio"], "reasons": sig["reasons"],
            "open_time": datetime.fromtimestamp(current["time"]/1000).strftime("%Y-%m-%d %H:%M"),
            "open_idx": i, "result": None, "pnl": 0.0,
        }

    # Skip-Statistik ausgeben
    print(f"\n  Skip-Gründe (Top 5):")
    for reason, count in sorted(skip_reasons.items(), key=lambda x: x[1], reverse=True)[:5]:
        pct = round(count/total_checked*100, 1)
        print(f"    {reason:<35} {count:>5}x ({pct}%)")

    return trades

# ── Ergebnisse auswerten ──────────────────────────────────────────────────────
def analyze_results(all_trades):
    stats = {}
    for symbol in SYMBOLS:
        sym_trades = [t for t in all_trades if t["symbol"]==symbol]
        wins    = [t for t in sym_trades if t["result"]=="WIN"]
        losses  = [t for t in sym_trades if t["result"]=="LOSS"]
        expired = [t for t in sym_trades if t["result"]=="EXPIRED"]
        total   = len(sym_trades)
        if total == 0: stats[symbol]={"total":0}; continue

        win_rate  = round(len(wins)/total*100, 1)
        total_pnl = round(sum(t["pnl"] for t in sym_trades), 2)
        avg_pnl   = round(total_pnl/total, 3)

        # Komponenten
        comp_stats = {}
        for t in sym_trades:
            for r in t.get("reasons",[]):
                if r not in comp_stats: comp_stats[r]={"wins":0,"losses":0,"total":0}
                comp_stats[r]["total"]+=1
                if t["result"]=="WIN": comp_stats[r]["wins"]+=1
                elif t["result"]=="LOSS": comp_stats[r]["losses"]+=1
        for r in comp_stats:
            t=comp_stats[r]["total"]; w=comp_stats[r]["wins"]
            comp_stats[r]["win_rate"]=round(w/t*100,1) if t>0 else 0

        # ADX Analyse
        adx_wins   = [t["adx"] for t in wins   if t.get("adx")]
        adx_losses = [t["adx"] for t in losses  if t.get("adx")]
        avg_adx_w  = round(sum(adx_wins)/len(adx_wins),1)     if adx_wins   else None
        avg_adx_l  = round(sum(adx_losses)/len(adx_losses),1) if adx_losses else None

        buy_trades  = [t for t in sym_trades if t["signal"]=="BUY"]
        sell_trades = [t for t in sym_trades if t["signal"]=="SELL"]
        buy_wins    = [t for t in buy_trades  if t["result"]=="WIN"]
        sell_wins   = [t for t in sell_trades if t["result"]=="WIN"]

        stats[symbol] = {
            "total": total, "wins": len(wins), "losses": len(losses),
            "expired": len(expired), "win_rate": win_rate,
            "total_pnl": total_pnl, "avg_pnl": avg_pnl,
            "buy_total": len(buy_trades), "buy_wins": len(buy_wins),
            "buy_win_rate": round(len(buy_wins)/len(buy_trades)*100,1) if buy_trades else 0,
            "sell_total": len(sell_trades), "sell_wins": len(sell_wins),
            "sell_win_rate": round(len(sell_wins)/len(sell_trades)*100,1) if sell_trades else 0,
            "avg_adx_wins": avg_adx_w, "avg_adx_losses": avg_adx_l,
            "components": comp_stats,
        }
    return stats

# ── Ergebnisse ausgeben ───────────────────────────────────────────────────────
def print_results(stats):
    print()
    print(bold(green("╔══════════════════════════════════════════════════════════╗")))
    print(bold(green("║       LSOB v3 BACKTEST ERGEBNISSE                      ║")))
    print(bold(green("║  Session + ADX + FVG3 + Close-Bestätigung + OB-Stack   ║")))
    print(bold(green("╚══════════════════════════════════════════════════════════╝")))

    for symbol, s in stats.items():
        if s.get("total",0)==0: print(f"\n  {symbol}: Keine Trades"); continue
        wr_c  = green if s["win_rate"]>=50 else red
        pnl_c = green if s["total_pnl"]>=0 else red
        print(f"\n  {bold(cyan(symbol))}")
        print(f"  {'─'*50}")
        print(f"  Gesamt Trades:    {s['total']}")
        print(f"  Win-Rate:         {wr_c(str(s['win_rate'])+'%')}")
        print(f"  Wins/Losses:      {green(str(s['wins']))} / {red(str(s['losses']))} / {gray(str(s['expired'])+' abgelaufen')}")
        print(f"  Gesamt PnL:       {pnl_c(str(s['total_pnl'])+'%')}")
        print(f"  Ø PnL/Trade:      {pnl_c(str(s['avg_pnl'])+'%')}")
        print(f"  BUY:  {s['buy_total']:>3} │ {green(str(s['buy_win_rate'])+'%') if s['buy_win_rate']>=50 else red(str(s['buy_win_rate'])+'%')}")
        print(f"  SELL: {s['sell_total']:>3} │ {green(str(s['sell_win_rate'])+'%') if s['sell_win_rate']>=50 else red(str(s['sell_win_rate'])+'%')}")
        if s.get("avg_adx_wins"): print(f"  Ø ADX Wins:       {s['avg_adx_wins']}")
        if s.get("avg_adx_losses"): print(f"  Ø ADX Losses:     {s['avg_adx_losses']}")
        if s["components"]:
            print(f"\n  Komponenten:")
            for comp, d in sorted(s["components"].items(), key=lambda x: x[1]["win_rate"], reverse=True):
                if d["total"]>=3:
                    wr=d["win_rate"]; bar="█"*int(wr/10)
                    print(f"    {comp:<28} {d['total']:>3}x │ {green(str(wr)+'%') if wr>=50 else red(str(wr)+'%')} {bar}")

def print_comparison(stats):
    v1 = {"ETHUSDT": 43.6, "HBARUSDT": 50.4}
    print()
    print(bold(cyan("╔══════════════════════════════════════════════════════════╗")))
    print(bold(cyan("║      VERGLEICH: LSOB v1 vs v3                          ║")))
    print(bold(cyan("╚══════════════════════════════════════════════════════════╝")))
    print()
    print(f"  {'Symbol':<12} {'LSOB v1':>10} {'LSOB v3':>10} {'Verbesserung':>14}")
    print(f"  {'─'*50}")
    for symbol in SYMBOLS:
        s   = stats.get(symbol,{})
        old = v1.get(symbol,0)
        new = s.get("win_rate",0)
        if not s.get("total"): continue
        diff = round(new-old,1)
        diff_str = green(f"+{diff}%") if diff>0 else red(f"{diff}%")
        print(f"  {symbol:<12} {old:>9}% {new:>9}% {diff_str:>14}")

def get_claude_summary(stats):
    if not ANTHROPIC_API_KEY:
        print(red("\nANTHROPIC_API_KEY fehlt")); return
    print(f"\n  Sende an Claude...")
    summary = "LSOB v3 (Session + ADX + FVG3 + Close-Bestätigung + OB-Stacking):\n"
    for sym, s in stats.items():
        if s.get("total",0)==0: continue
        summary += f"\n{sym}: {s['total']} Trades, WR {s['win_rate']}%, PnL {s['total_pnl']}%"
        summary += f"\n  BUY: {s['buy_win_rate']}% | SELL: {s['sell_win_rate']}%"
        if s["components"]:
            best = sorted(s["components"].items(), key=lambda x: x[1]["win_rate"], reverse=True)[:3]
            best_parts = [c[0]+" ("+str(c[1]["win_rate"])+"% WR)" for c in best if c[1]["total"]>=3]
            if best_parts: summary += "\n  Beste: "+", ".join(best_parts)
    summary += "\n\nVergleich LSOB v1: ETH 43.6%, HBAR 50.4%"
    prompt = f"""Du bist ein professioneller SMC Trading-Analyst.

Ergebnisse nach Hinzufügen von Session-Filter, ADX, frischen FVGs und Kerzen-Close-Bestätigung:
{summary}

1. Hat sich die Win-Rate verbessert gegenüber v1?
2. Welche Verbesserung hat am meisten geholfen?
3. Ist die Strategie jetzt bereit für Auto-Trading mit echtem Geld?
4. Mit welcher Trade-Größe und welchem Kapital empfiehlst du zu starten?
5. Was ist der wichtigste nächste Schritt?

Sei direkt und konkret."""
    client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(model="claude-sonnet-4-5", max_tokens=800,
                                     messages=[{"role":"user","content":prompt}])
    print()
    print(bold(green("═"*58)))
    print(bold(green("  CLAUDE EMPFEHLUNG")))
    print(bold(green("═"*58)))
    print(message.content[0].text)

def save_trades_csv(all_trades):
    with open(CSV_FILE,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=["symbol","signal","open_time","close_time",
            "entry","sl","tp","close_price","result","pnl","adx","vol_ratio","reasons"])
        w.writeheader()
        for t in all_trades:
            w.writerow({"symbol":t["symbol"],"signal":t["signal"],"open_time":t["open_time"],
                "close_time":t.get("close_time",""),"entry":t["entry"],"sl":t["sl"],"tp":t["tp"],
                "close_price":t.get("close_price",""),"result":t["result"],"pnl":t["pnl"],
                "adx":t.get("adx",""),"vol_ratio":t.get("vol_ratio",""),
                "reasons":",".join(t.get("reasons",[]))})
    print(f"\n  Trades: {cyan(CSV_FILE)}")

def main():
    print()
    print(bold(green("╔══════════════════════════════════════════════════════════╗")))
    print(bold(green("║     BITUNIX BOT – LSOB BACKTESTING v3                  ║")))
    print(bold(green("╠══════════════════════════════════════════════════════════╣")))
    print(bold(green(f"║  Symbole:  {' · '.join(SYMBOLS):<45}║")))
    print(bold(green(f"║  NEU: Session 14-22 UTC │ ADX>{ADX_MIN} │ FVG≤3 Kerzen      ║")))
    print(bold(green(f"║  NEU: Kerzen-Close Bestätigung │ OB-Stacking           ║")))
    print(bold(green("╚══════════════════════════════════════════════════════════╝")))
    print()

    end_time   = int(datetime.now().timestamp()*1000)
    start_time = int((datetime.now()-timedelta(days=365*YEARS_BACK)).timestamp()*1000)
    print(f"  Zeitraum: {cyan(datetime.fromtimestamp(start_time/1000).strftime('%Y-%m-%d'))} bis {cyan(datetime.fromtimestamp(end_time/1000).strftime('%Y-%m-%d'))}")
    print()

    all_trades = []
    for symbol in SYMBOLS:
        print(bold(f"\n  {'═'*50}"))
        print(bold(f"  {symbol}"))
        print(f"  {'═'*50}")
        candles = fetch_historical(symbol, start_time, end_time)
        print(f"  {green(str(len(candles)))} Kerzen geladen")
        if len(candles)<200: print(red("  Zu wenig Daten")); continue
        trades = run_backtest(symbol, candles)
        all_trades.extend(trades)
        wins=len([t for t in trades if t["result"]=="WIN"]); total=len(trades)
        wr=round(wins/total*100,1) if total>0 else 0
        print(f"  Trades: {total} │ Win-Rate: {green(str(wr)+'%') if wr>=50 else red(str(wr)+'%')}")

    if not all_trades: print(red("\nKeine Trades")); return

    stats = analyze_results(all_trades)
    print_results(stats)
    print_comparison(stats)
    save_trades_csv(all_trades)
    with open(REPORT_FILE,"w") as f: json.dump(stats,f,indent=2)
    print(f"  Bericht: {cyan(REPORT_FILE)}")
    get_claude_summary(stats)
    print()
    print(bold(green("  LSOB v3 Backtesting abgeschlossen!")))
    print()

if __name__=="__main__":
    main()
