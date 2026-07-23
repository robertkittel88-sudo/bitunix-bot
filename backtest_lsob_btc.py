"""
╔══════════════════════════════════════════════════════════╗
║     Bitunix AI Bot – LSOB Backtesting                   ║
║     Liquidity Sweep + Order Block Strategie             ║
║                                                         ║
║  Konzepte:                                              ║
║  - Order Block Erkennung (OB)                           ║
║  - Liquidity Sweep (LS)                                 ║
║  - Fair Value Gap (FVG)                                 ║
║  - Break of Structure (BOS)                             ║
║  - Change of Character (CHoCH)                         ║
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

SYMBOLS    = ["BTCUSDT"]
INTERVAL   = "1h"
YEARS_BACK = 3
SL_PCT     = 0.010   # 1% SL
TP_PCT     = 0.030   # 3% TP (1:3)
MIN_VOL    = 0.5     # Mindest-Volumen

BINANCE_BASE = "https://fapi.binance.com"
REPORT_FILE  = "backtest_lsob_btc_results.json"
CSV_FILE     = "backtest_lsob_btc_trades.csv"

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

# ── Swing High / Low Erkennung ────────────────────────────────────────────────
def find_swing_highs_lows(candles, lookback=5):
    """
    Findet Swing Highs und Swing Lows.
    Ein Swing High ist ein Candle dessen High höher ist als die
    lookback Candles davor und danach.
    """
    swing_highs = []
    swing_lows  = []

    for i in range(lookback, len(candles) - lookback):
        high = candles[i]["high"]
        low  = candles[i]["low"]

        is_swing_high = all(
            candles[i - j]["high"] < high and candles[i + j]["high"] < high
            for j in range(1, lookback + 1)
        )
        is_swing_low = all(
            candles[i - j]["low"] > low and candles[i + j]["low"] > low
            for j in range(1, lookback + 1)
        )

        if is_swing_high:
            swing_highs.append({"idx": i, "price": high, "time": candles[i]["time"]})
        if is_swing_low:
            swing_lows.append({"idx": i, "price": low, "time": candles[i]["time"]})

    return swing_highs, swing_lows

# ── Break of Structure (BOS) ──────────────────────────────────────────────────
def detect_bos(candles, swing_highs, swing_lows, current_idx):
    """
    BOS: Preis bricht über letztes Swing High (bullish BOS)
    oder unter letztes Swing Low (bearish BOS).
    """
    if not swing_highs or not swing_lows:
        return None

    current_close = candles[current_idx]["close"]

    # Letztes Swing High und Low vor aktuellem Index
    prev_highs = [sh for sh in swing_highs if sh["idx"] < current_idx]
    prev_lows  = [sl for sl in swing_lows  if sl["idx"] < current_idx]

    if not prev_highs or not prev_lows:
        return None

    last_swing_high = prev_highs[-1]["price"]
    last_swing_low  = prev_lows[-1]["price"]

    if current_close > last_swing_high:
        return {"type": "BULLISH_BOS", "level": last_swing_high}
    elif current_close < last_swing_low:
        return {"type": "BEARISH_BOS", "level": last_swing_low}

    return None

# ── Change of Character (CHoCH) ───────────────────────────────────────────────
def detect_choch(candles, swing_highs, swing_lows, current_idx, lookback=10):
    """
    CHoCH: Wechsel der Marktstruktur.
    In einem Downtrend: erstes höheres High = CHoCH bullish
    In einem Uptrend: erstes tieferes Low = CHoCH bearish
    """
    if current_idx < lookback * 2:
        return None

    recent_highs = [sh for sh in swing_highs if current_idx - lookback <= sh["idx"] < current_idx]
    recent_lows  = [sl for sl in swing_lows  if current_idx - lookback <= sl["idx"] < current_idx]

    if len(recent_highs) < 2 or len(recent_lows) < 2:
        return None

    # Downtrend: Lower Highs + Lower Lows
    lower_highs = all(recent_highs[i]["price"] > recent_highs[i+1]["price"] for i in range(len(recent_highs)-1))
    lower_lows  = all(recent_lows[i]["price"]  > recent_lows[i+1]["price"]  for i in range(len(recent_lows)-1))

    # Uptrend: Higher Highs + Higher Lows
    higher_highs = all(recent_highs[i]["price"] < recent_highs[i+1]["price"] for i in range(len(recent_highs)-1))
    higher_lows  = all(recent_lows[i]["price"]  < recent_lows[i+1]["price"]  for i in range(len(recent_lows)-1))

    current_close = candles[current_idx]["close"]

    # CHoCH bullish: war im Downtrend, bricht jetzt über letztes High
    if lower_highs and lower_lows:
        if current_close > recent_highs[-1]["price"]:
            return {"type": "BULLISH_CHOCH", "level": recent_highs[-1]["price"]}

    # CHoCH bearish: war im Uptrend, bricht jetzt unter letztes Low
    if higher_highs and higher_lows:
        if current_close < recent_lows[-1]["price"]:
            return {"type": "BEARISH_CHOCH", "level": recent_lows[-1]["price"]}

    return None

# ── Order Block Erkennung ─────────────────────────────────────────────────────
def find_order_blocks(candles, current_idx, lookback=20):
    """
    Bullischer OB: Letzte bearische Kerze vor einer starken Aufwärtsbewegung
    Bärischer OB: Letzte bullische Kerze vor einer starken Abwärtsbewegung

    Starke Bewegung = mindestens 3 Kerzen in eine Richtung mit
    durchschnittlich 0.3% Bewegung pro Kerze
    """
    obs = []
    start = max(0, current_idx - lookback)

    for i in range(start, current_idx - 3):
        # Prüfe ob nach Kerze i eine starke Bewegung kommt
        next_candles = candles[i+1:i+4]
        if len(next_candles) < 3:
            continue

        # Starke Aufwärtsbewegung nach einer bearischen Kerze
        if candles[i]["close"] < candles[i]["open"]:  # Bearische Kerze
            moves = [(c["close"] - c["open"]) / c["open"] * 100 for c in next_candles]
            if all(m > 0 for m in moves) and sum(moves) > 0.9:
                obs.append({
                    "type":   "BULLISH_OB",
                    "idx":    i,
                    "high":   candles[i]["high"],
                    "low":    candles[i]["low"],
                    "open":   candles[i]["open"],
                    "close":  candles[i]["close"],
                    "time":   candles[i]["time"],
                    "strength": sum(moves),
                })

        # Starke Abwärtsbewegung nach einer bullischen Kerze
        if candles[i]["close"] > candles[i]["open"]:  # Bullische Kerze
            moves = [(c["close"] - c["open"]) / c["open"] * 100 for c in next_candles]
            if all(m < 0 for m in moves) and sum(moves) < -0.9:
                obs.append({
                    "type":   "BEARISH_OB",
                    "idx":    i,
                    "high":   candles[i]["high"],
                    "low":    candles[i]["low"],
                    "open":   candles[i]["open"],
                    "close":  candles[i]["close"],
                    "time":   candles[i]["time"],
                    "strength": abs(sum(moves)),
                })

    return obs

# ── Fair Value Gap (FVG) ──────────────────────────────────────────────────────
def find_fvg(candles, current_idx, lookback=10):
    """
    FVG: Lücke zwischen Kerze[i-2].low und Kerze[i].high (bullisch)
    oder zwischen Kerze[i-2].high und Kerze[i].low (bärisch)
    """
    fvgs = []
    start = max(2, current_idx - lookback)

    for i in range(start, current_idx):
        c0 = candles[i-2]
        c1 = candles[i-1]
        c2 = candles[i]

        # Bullisches FVG: Lücke nach oben
        if c2["low"] > c0["high"]:
            gap_size = (c2["low"] - c0["high"]) / c0["high"] * 100
            if gap_size > 0.1:
                fvgs.append({
                    "type":     "BULLISH_FVG",
                    "upper":    c2["low"],
                    "lower":    c0["high"],
                    "gap_pct":  round(gap_size, 3),
                    "idx":      i,
                })

        # Bärisches FVG: Lücke nach unten
        if c2["high"] < c0["low"]:
            gap_size = (c0["low"] - c2["high"]) / c0["low"] * 100
            if gap_size > 0.1:
                fvgs.append({
                    "type":     "BEARISH_FVG",
                    "upper":    c0["low"],
                    "lower":    c2["high"],
                    "gap_pct":  round(gap_size, 3),
                    "idx":      i,
                })

    return fvgs

# ── Liquidity Sweep Erkennung ─────────────────────────────────────────────────
def detect_liquidity_sweep(candles, swing_highs, swing_lows, current_idx):
    """
    Liquidity Sweep: Preis überschreitet kurz ein Swing High/Low
    und kehrt dann schnell um (Wick über/unter dem Level).

    Bullischer Sweep: Preis taucht unter Swing Low → dreht um
    Bärischer Sweep: Preis bricht über Swing High → dreht um
    """
    if current_idx < 2:
        return None

    current  = candles[current_idx]
    prev     = candles[current_idx - 1]

    prev_highs = [sh for sh in swing_highs if sh["idx"] < current_idx - 1]
    prev_lows  = [sl for sl in swing_lows  if sl["idx"] < current_idx - 1]

    if not prev_highs or not prev_lows:
        return None

    last_swing_high = prev_highs[-1]["price"]
    last_swing_low  = prev_lows[-1]["price"]

    # Bullischer Sweep: Wick unter Swing Low + Close darüber
    if (prev["low"] < last_swing_low and
        prev["close"] > last_swing_low and
        current["close"] > last_swing_low):
        sweep_depth = (last_swing_low - prev["low"]) / last_swing_low * 100
        return {
            "type":        "BULLISH_SWEEP",
            "swept_level": last_swing_low,
            "sweep_depth": round(sweep_depth, 3),
            "idx":         current_idx - 1,
        }

    # Bärischer Sweep: Wick über Swing High + Close darunter
    if (prev["high"] > last_swing_high and
        prev["close"] < last_swing_high and
        current["close"] < last_swing_high):
        sweep_depth = (prev["high"] - last_swing_high) / last_swing_high * 100
        return {
            "type":        "BEARISH_SWEEP",
            "swept_level": last_swing_high,
            "sweep_depth": round(sweep_depth, 3),
            "idx":         current_idx - 1,
        }

    return None

# ── Preis in Order Block prüfen ───────────────────────────────────────────────
def price_in_ob(price, obs):
    """Prüft ob der aktuelle Preis in einem Order Block liegt"""
    for ob in obs:
        low  = min(ob["open"], ob["close"])
        high = max(ob["open"], ob["close"])
        if low <= price <= high:
            return ob
    return None

# ── LSOB Signal generieren ────────────────────────────────────────────────────
def generate_lsob_signal(candles, current_idx, swing_highs, swing_lows):
    """
    LSOB Signal-Logik:

    BUY wenn:
    1. Bullischer Liquidity Sweep (Stop Hunt nach unten)
    2. Preis reagiert auf Bullischen Order Block
    3. Bullischer BOS oder CHoCH bestätigt Richtungswechsel
    4. Optional: FVG als Einstiegszone

    SELL wenn:
    1. Bärischer Liquidity Sweep (Stop Hunt nach oben)
    2. Preis reagiert auf Bärischen Order Block
    3. Bärischer BOS oder CHoCH bestätigt Richtungswechsel
    4. Optional: FVG als Einstiegszone
    """
    if current_idx < 60:
        return None

    current_price = candles[current_idx]["close"]
    volumes       = [c["volume"] for c in candles]

    # Volumen-Filter
    avg_vol   = sum(volumes[current_idx-20:current_idx]) / 20
    last_vol  = volumes[current_idx]
    vol_ratio = round(last_vol / avg_vol, 2) if avg_vol > 0 else 0
    if vol_ratio < MIN_VOL:
        return None

    # Alle SMC-Komponenten analysieren
    sweep    = detect_liquidity_sweep(candles, swing_highs, swing_lows, current_idx)
    bos      = detect_bos(candles, swing_highs, swing_lows, current_idx)
    choch    = detect_choch(candles, swing_highs, swing_lows, current_idx)
    obs      = find_order_blocks(candles, current_idx)
    fvgs     = find_fvg(candles, current_idx)
    price_ob = price_in_ob(current_price, obs)

    # Punkte-System für Signal-Stärke
    bull_points = 0
    bear_points = 0
    reasons     = []

    # Liquidity Sweep (stärkstes Signal)
    if sweep:
        if sweep["type"] == "BULLISH_SWEEP":
            bull_points += 4
            reasons.append("BULLISH_SWEEP")
        elif sweep["type"] == "BEARISH_SWEEP":
            bear_points += 4
            reasons.append("BEARISH_SWEEP")

    # BOS
    if bos:
        if bos["type"] == "BULLISH_BOS":
            bull_points += 3
            reasons.append("BULLISH_BOS")
        elif bos["type"] == "BEARISH_BOS":
            bear_points += 3
            reasons.append("BEARISH_BOS")

    # CHoCH (starkes Umkehrsignal)
    if choch:
        if choch["type"] == "BULLISH_CHOCH":
            bull_points += 3
            reasons.append("BULLISH_CHOCH")
        elif choch["type"] == "BEARISH_CHOCH":
            bear_points += 3
            reasons.append("BEARISH_CHOCH")

    # Order Block
    if price_ob:
        if price_ob["type"] == "BULLISH_OB":
            bull_points += 3
            reasons.append("IN_BULLISH_OB")
        elif price_ob["type"] == "BEARISH_OB":
            bear_points += 3
            reasons.append("IN_BEARISH_OB")

    # FVG
    recent_fvgs = [f for f in fvgs if current_idx - f["idx"] <= 5]
    for fvg in recent_fvgs:
        if fvg["type"] == "BULLISH_FVG":
            bull_points += 2
            reasons.append("BULLISH_FVG")
        elif fvg["type"] == "BEARISH_FVG":
            bear_points += 2
            reasons.append("BEARISH_FVG")

    # Mindestens 2 SMC-Komponenten müssen übereinstimmen
    total = bull_points + bear_points
    if total < 6:
        return None

    signal     = "HOLD"
    confidence = 0

    if bull_points > bear_points and bull_points >= 6:
        signal     = "BUY"
        confidence = min(int((bull_points / total) * 100), 99)
    elif bear_points > bull_points and bear_points >= 6:
        signal     = "SELL"
        confidence = min(int((bear_points / total) * 100), 99)

    if signal == "HOLD" or confidence < 65:
        return None

    # SL/TP basierend auf Struktur
    if signal == "BUY":
        # SL unter letztem Swing Low
        prev_lows_list = [sl for sl in swing_lows if sl["idx"] < current_idx]
        if prev_lows_list:
            sl = round(prev_lows_list[-1]["price"] * 0.999, 6)
        else:
            sl = round(current_price * (1 - SL_PCT), 6)
        tp = round(current_price * (1 + TP_PCT), 6)
    else:
        # SL über letztem Swing High
        prev_highs_list = [sh for sh in swing_highs if sh["idx"] < current_idx]
        if prev_highs_list:
            sl = round(prev_highs_list[-1]["price"] * 1.001, 6)
        else:
            sl = round(current_price * (1 + SL_PCT), 6)
        tp = round(current_price * (1 - TP_PCT), 6)

    return {
        "signal":     signal,
        "confidence": confidence,
        "price":      current_price,
        "sl":         sl,
        "tp":         tp,
        "vol_ratio":  vol_ratio,
        "bull_pts":   bull_points,
        "bear_pts":   bear_points,
        "reasons":    reasons,
        "sweep":      sweep,
        "bos":        bos,
        "choch":      choch,
        "ob":         price_ob,
        "fvgs":       recent_fvgs,
    }

# ── Historische Daten laden ───────────────────────────────────────────────────
def fetch_historical(symbol, start_time, end_time):
    all_candles = []
    current     = start_time

    while current < end_time:
        url    = f"{BINANCE_BASE}/fapi/v1/klines"
        params = {
            "symbol":    symbol,
            "interval":  INTERVAL,
            "startTime": current,
            "endTime":   end_time,
            "limit":     1000
        }
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            candles = [
                {"time": int(c[0]), "open": float(c[1]), "high": float(c[2]),
                 "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])}
                for c in data
            ]
            all_candles.extend(candles)
            current = candles[-1]["time"] + 1
            time.sleep(0.1)
        except Exception as e:
            print(f"  Fehler: {e}")
            time.sleep(2)
            break

    return all_candles

# ── Backtest simulieren ───────────────────────────────────────────────────────
def run_backtest_lsob(symbol, candles):
    trades     = []
    open_trade = None
    window     = 100

    print(f"\n  Analysiere {len(candles)} Kerzen für {symbol}...")

    # Swing Highs/Lows einmalig für alle Kerzen berechnen
    swing_highs, swing_lows = find_swing_highs_lows(candles, lookback=5)

    for i in range(window, len(candles)):
        current = candles[i]
        price   = current["close"]

        # Offenen Trade prüfen
        if open_trade:
            result = None
            pnl    = 0.0

            if open_trade["signal"] == "BUY":
                if price >= open_trade["tp"]:
                    result = "WIN";  pnl = round((open_trade["tp"] - open_trade["entry"]) / open_trade["entry"] * 100, 3)
                elif price <= open_trade["sl"]:
                    result = "LOSS"; pnl = round((open_trade["sl"] - open_trade["entry"]) / open_trade["entry"] * 100, 3)
            elif open_trade["signal"] == "SELL":
                if price <= open_trade["tp"]:
                    result = "WIN";  pnl = round((open_trade["entry"] - open_trade["tp"]) / open_trade["entry"] * 100, 3)
                elif price >= open_trade["sl"]:
                    result = "LOSS"; pnl = round((open_trade["entry"] - open_trade["sl"]) / open_trade["entry"] * 100, 3)

            age_candles = i - open_trade["open_idx"]
            if age_candles > 48:
                result = "EXPIRED"
                pnl    = round((price - open_trade["entry"]) / open_trade["entry"] * 100, 3)
                if open_trade["signal"] == "SELL":
                    pnl = -pnl

            if result:
                open_trade["result"]      = result
                open_trade["close_price"] = price
                open_trade["close_time"]  = datetime.fromtimestamp(current["time"] / 1000).strftime("%Y-%m-%d %H:%M")
                open_trade["pnl"]         = pnl
                trades.append(open_trade)
                open_trade = None
            continue

        # LSOB Signal generieren
        sig = generate_lsob_signal(candles, i, swing_highs, swing_lows)
        if sig:
            open_trade = {
                "symbol":    symbol,
                "signal":    sig["signal"],
                "entry":     sig["price"],
                "sl":        sig["sl"],
                "tp":        sig["tp"],
                "confidence":sig["confidence"],
                "vol_ratio": sig["vol_ratio"],
                "reasons":   sig["reasons"],
                "open_time": datetime.fromtimestamp(current["time"] / 1000).strftime("%Y-%m-%d %H:%M"),
                "open_idx":  i,
                "result":    None,
                "pnl":       0.0,
            }

    return trades

# ── Ergebnisse auswerten ──────────────────────────────────────────────────────
def analyze_results(all_trades):
    stats = {}

    for symbol in SYMBOLS:  # BTC Analyse
        sym_trades = [t for t in all_trades if t["symbol"] == symbol]
        wins       = [t for t in sym_trades if t["result"] == "WIN"]
        losses     = [t for t in sym_trades if t["result"] == "LOSS"]
        expired    = [t for t in sym_trades if t["result"] == "EXPIRED"]
        total      = len(sym_trades)

        if total == 0:
            stats[symbol] = {"total": 0}
            continue

        win_rate  = round(len(wins) / total * 100, 1)
        total_pnl = round(sum(t["pnl"] for t in sym_trades), 2)
        avg_pnl   = round(total_pnl / total, 3)

        # Komponenten-Statistik
        component_stats = {}
        for t in sym_trades:
            for r in t.get("reasons", []):
                if r not in component_stats:
                    component_stats[r] = {"wins": 0, "losses": 0, "total": 0}
                component_stats[r]["total"] += 1
                if t["result"] == "WIN":
                    component_stats[r]["wins"] += 1
                elif t["result"] == "LOSS":
                    component_stats[r]["losses"] += 1

        for r in component_stats:
            t = component_stats[r]["total"]
            w = component_stats[r]["wins"]
            component_stats[r]["win_rate"] = round(w / t * 100, 1) if t > 0 else 0

        buy_trades  = [t for t in sym_trades if t["signal"] == "BUY"]
        sell_trades = [t for t in sym_trades if t["signal"] == "SELL"]
        buy_wins    = [t for t in buy_trades  if t["result"] == "WIN"]
        sell_wins   = [t for t in sell_trades if t["result"] == "WIN"]

        stats[symbol] = {
            "total":        total,
            "wins":         len(wins),
            "losses":       len(losses),
            "expired":      len(expired),
            "win_rate":     win_rate,
            "total_pnl":    total_pnl,
            "avg_pnl":      avg_pnl,
            "buy_total":    len(buy_trades),
            "buy_wins":     len(buy_wins),
            "buy_win_rate": round(len(buy_wins)  / len(buy_trades)  * 100, 1) if buy_trades  else 0,
            "sell_total":   len(sell_trades),
            "sell_wins":    len(sell_wins),
            "sell_win_rate":round(len(sell_wins) / len(sell_trades) * 100, 1) if sell_trades else 0,
            "components":   component_stats,
        }

    return stats

# ── Ergebnisse ausgeben ───────────────────────────────────────────────────────
def print_results(stats):
    print()
    print(bold(green("╔══════════════════════════════════════════════════════════╗")))
    print(bold(green("║           LSOB BACKTEST ERGEBNISSE                      ║")))
    print(bold(green("╚══════════════════════════════════════════════════════════╝")))

    for symbol, s in stats.items():
        if s.get("total", 0) == 0:
            print(f"\n  {bold(symbol)}: Keine Trades gefunden")
            continue

        wr_color  = green if s["win_rate"] >= 50 else red
        pnl_color = green if s["total_pnl"] >= 0 else red

        print(f"\n  {bold(cyan(symbol))}")
        print(f"  {'─'*50}")
        print(f"  Gesamt Trades:    {s['total']}")
        print(f"  Win-Rate:         {wr_color(str(s['win_rate']) + '%')}")
        print(f"  Wins/Losses:      {green(str(s['wins']))} / {red(str(s['losses']))} / {gray(str(s['expired']) + ' abgelaufen')}")
        print(f"  Gesamt PnL:       {pnl_color(str(s['total_pnl']) + '%')}")
        print(f"  Ø PnL/Trade:      {pnl_color(str(s['avg_pnl']) + '%')}")
        print(f"  BUY:  {s['buy_total']:>3} Trades  │  {green(str(s['buy_win_rate'])+'%') if s['buy_win_rate'] >= 50 else red(str(s['buy_win_rate'])+'%')}")
        print(f"  SELL: {s['sell_total']:>3} Trades  │  {green(str(s['sell_win_rate'])+'%') if s['sell_win_rate'] >= 50 else red(str(s['sell_win_rate'])+'%')}")

        if s["components"]:
            print(f"\n  SMC-Komponenten Performance:")
            sorted_c = sorted(s["components"].items(), key=lambda x: x[1]["win_rate"], reverse=True)
            for comp, cdata in sorted_c:
                if cdata["total"] >= 3:
                    wr  = cdata["win_rate"]
                    bar = "█" * int(wr / 10)
                    print(f"    {comp:<20} {cdata['total']:>4} Trades  │  {green(str(wr)+'%') if wr >= 50 else red(str(wr)+'%')} {bar}")

# ── Vergleich ─────────────────────────────────────────────────────────────────
def print_comparison(stats):
    prev = {
        "BTCUSDT": {"win_rate": 21.7, "label": "Klassisch v1"},
    }

    print()
    print(bold(cyan("╔══════════════════════════════════════════════════════════╗")))
    print(bold(cyan("║      VERGLEICH: Klassisch vs LSOB                       ║")))
    print(bold(cyan("╚══════════════════════════════════════════════════════════╝")))
    print()
    print(f"  {'Symbol':<12} {'Klassisch':>12} {'LSOB':>10} {'Verbesserung':>14}")
    print(f"  {'─'*52}")

    for symbol in SYMBOLS:
        s  = stats.get(symbol, {})
        p  = prev.get(symbol, {})
        if not s.get("total"):
            continue
        old_wr = p.get("win_rate", 0)
        new_wr = s.get("win_rate", 0)
        diff   = round(new_wr - old_wr, 1)
        diff_str = green(f"+{diff}%") if diff > 0 else red(f"{diff}%")
        print(f"  {symbol:<12} {old_wr:>11}% {new_wr:>9}% {diff_str:>14}")

# ── Claude Zusammenfassung ────────────────────────────────────────────────────
def get_claude_summary(stats):
    if not ANTHROPIC_API_KEY:
        print(red("\nANTHROPIC_API_KEY fehlt in .env – Claude Zusammenfassung übersprungen"))
        return

    print(f"\n  Sende Ergebnisse an Claude...")

    summary = "LSOB Backtest (Liquidity Sweep + Order Block, 1h, 3 Jahre):\n"
    for sym, s in stats.items():
        if s.get("total", 0) == 0:
            continue
        summary += f"\n{sym}: {s['total']} Trades, Win-Rate {s['win_rate']}%, PnL {s['total_pnl']}%"
        summary += f"\n  BUY: {s['buy_win_rate']}% | SELL: {s['sell_win_rate']}%"
        if s["components"]:
            best = sorted(s["components"].items(), key=lambda x: x[1]["win_rate"], reverse=True)[:3]
            best_parts = []
            for c in best:
                if c[1]["total"] >= 3:
                    best_parts.append(c[0] + " (" + str(c[1]["win_rate"]) + "%)")
            if best_parts:
                summary += "\n  Beste Komponenten: " + ", ".join(best_parts)

    summary += """

Vergleich zu klassischer Strategie:
- ETH klassisch: 29.0% Win-Rate
- HBAR klassisch: 36.5% Win-Rate"""

    prompt = f"""Du bist ein professioneller SMC/LSOB Trading-Analyst.

Ich habe einen Trading-Bot mit Smart Money Concepts getestet:
{summary}

Analysiere die Ergebnisse und beantworte:
1. Ist LSOB besser als klassische Indikatoren?
2. Welche SMC-Komponenten funktionieren am besten?
3. Ist die Strategie jetzt bereit für Auto-Trading?
4. Was sind die nächsten konkreten Verbesserungen?
5. Mit welchen Parametern würdest du live starten?

Sei direkt, kritisch und ehrlich."""

    client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    print()
    print(bold(green("═" * 58)))
    print(bold(green("  CLAUDE EMPFEHLUNG")))
    print(bold(green("═" * 58)))
    print(message.content[0].text)

# ── CSV speichern ─────────────────────────────────────────────────────────────
def save_trades_csv(all_trades):
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "symbol", "signal", "open_time", "close_time",
            "entry", "sl", "tp", "close_price",
            "result", "pnl", "vol_ratio", "confidence", "reasons"
        ])
        writer.writeheader()
        for t in all_trades:
            writer.writerow({
                "symbol":     t["symbol"],
                "signal":     t["signal"],
                "open_time":  t["open_time"],
                "close_time": t.get("close_time", ""),
                "entry":      t["entry"],
                "sl":         t["sl"],
                "tp":         t["tp"],
                "close_price":t.get("close_price", ""),
                "result":     t["result"],
                "pnl":        t["pnl"],
                "vol_ratio":  t.get("vol_ratio", ""),
                "confidence": t["confidence"],
                "reasons":    ",".join(t.get("reasons", [])),
            })
    print(f"\n  Trades gespeichert: {cyan(CSV_FILE)}")

# ── Haupt-Funktion ────────────────────────────────────────────────────────────
def main():
    print()
    print(bold(green("╔══════════════════════════════════════════════════════════╗")))
    print(bold(green("║     BITUNIX BOT – LSOB BACKTESTING                     ║")))
    print(bold(green("╠══════════════════════════════════════════════════════════╣")))
    print(bold(green(f"║  Symbole:   {' · '.join(SYMBOLS):<44}║")))
    print(bold(green(f"║  Timeframe: {INTERVAL} │ Zeitraum: {YEARS_BACK} Jahre               ║")))
    print(bold(green(f"║  Strategie: Liquidity Sweep + Order Block + BOS/CHoCH  ║")))
    print(bold(green(f"║  SL/TP:     {SL_PCT*100:.1f}% / {TP_PCT*100:.1f}% (1:3 Ratio)                   ║")))
    print(bold(green("╚══════════════════════════════════════════════════════════╝")))
    print()

    end_time   = int(datetime.now().timestamp() * 1000)
    start_time = int((datetime.now() - timedelta(days=365 * YEARS_BACK)).timestamp() * 1000)
    start_str  = datetime.fromtimestamp(start_time / 1000).strftime("%Y-%m-%d")
    end_str    = datetime.fromtimestamp(end_time / 1000).strftime("%Y-%m-%d")
    print(f"  Zeitraum: {cyan(start_str)} bis {cyan(end_str)}")
    print()

    all_trades = []

    for symbol in SYMBOLS:
        print(bold(f"\n  {'═'*50}"))
        print(bold(f"  {symbol}"))
        print(f"  {'═'*50}")

        candles = fetch_historical(symbol, start_time, end_time)
        print(f"  {green(str(len(candles)))} Kerzen geladen ({INTERVAL})")

        if len(candles) < 200:
            print(red("  Zu wenige Daten"))
            continue

        trades = run_backtest_lsob(symbol, candles)
        all_trades.extend(trades)

        wins  = len([t for t in trades if t["result"] == "WIN"])
        total = len(trades)
        wr    = round(wins / total * 100, 1) if total > 0 else 0
        print(f"  Trades: {total} │ Win-Rate: {green(str(wr)+'%') if wr >= 50 else red(str(wr)+'%')}")

    if not all_trades:
        print(red("\nKeine Trades gefunden."))
        return

    stats = analyze_results(all_trades)
    print_results(stats)
    print_comparison(stats)
    save_trades_csv(all_trades)

    with open(REPORT_FILE, "w") as f:
        json.dump(stats, f, indent=2)
    print(f"  Bericht: {cyan(REPORT_FILE)}")

    get_claude_summary(stats)

    print()
    print(bold(green("  LSOB Backtesting abgeschlossen!")))
    print()

if __name__ == "__main__":
    main()
