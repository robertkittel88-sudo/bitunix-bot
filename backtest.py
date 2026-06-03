"""
╔══════════════════════════════════════════════════════════╗
║         Bitunix AI Bot – Backtesting Script              ║
║                                                          ║
║  Analysiert historische Daten ohne Claude API            ║
║  Kosten: ~$0.01 (nur finale Zusammenfassung)            ║
║                                                          ║
║  Start: py backtest.py                                   ║
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

# ── Konfiguration ─────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

SYMBOLS    = ["BTCUSDT", "ETHUSDT", "HBARUSDT"]
INTERVAL   = "15m"
YEARS_BACK = 3          # Wie viele Jahre zurück analysieren
MIN_CONF   = 75         # Gleiche Schwelle wie Live-Bot
MIN_VOL    = 0.3        # Gleicher Volumen-Filter
SL_PCT     = 0.015      # Stop Loss 1.5%
TP_PCT     = 0.025      # Take Profit 2.5%

BINANCE_BASE = "https://fapi.binance.com"
REPORT_FILE  = "backtest_results.json"
CSV_FILE     = "backtest_trades.csv"

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
def blue(t):   return f"{C.BLUE}{t}{C.RESET}"
def cyan(t):   return f"{C.CYAN}{t}{C.RESET}"
def gray(t):   return f"{C.GRAY}{t}{C.RESET}"
def bold(t):   return f"{C.BOLD}{t}{C.RESET}"

# ── Indikatoren ───────────────────────────────────────────────────────────────
def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = 0, 0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0: gains += d
        else: losses -= d
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
    return round(100 - 100 / (1 + avg_g / avg_l)) if avg_l != 0 else 100

def calc_ema(closes, period):
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 6)

def calc_macd(closes):
    e12, e26 = calc_ema(closes, 12), calc_ema(closes, 26)
    return round(e12 - e26, 6) if e12 and e26 else None

def calc_bollinger(closes, period=20):
    if len(closes) < period:
        return None
    sl   = closes[-period:]
    mean = sum(sl) / period
    std  = math.sqrt(sum((x - mean) ** 2 for x in sl) / period)
    return {"upper": round(mean + 2*std, 6), "mid": round(mean, 6), "lower": round(mean - 2*std, 6)}

# ── Kerzen-Muster ─────────────────────────────────────────────────────────────
def detect_patterns(candles):
    patterns = []
    if len(candles) < 3:
        return patterns

    c  = candles[-1]
    p1 = candles[-2]
    p2 = candles[-3]

    body_c  = abs(c["close"]  - c["open"])
    body_p1 = abs(p1["close"] - p1["open"])
    body_p2 = abs(p2["close"] - p2["open"])
    range_c = c["high"] - c["low"]

    bull_c  = c["close"]  > c["open"]
    bear_c  = c["close"]  < c["open"]
    bull_p1 = p1["close"] > p1["open"]
    bear_p1 = p1["close"] < p1["open"]

    # Doji
    if range_c > 0 and body_c / range_c < 0.1:
        patterns.append("DOJI")

    # Hammer
    lower_shadow = c["open"] - c["low"] if bull_c else c["close"] - c["low"]
    upper_shadow = c["high"] - c["close"] if bull_c else c["high"] - c["open"]
    if lower_shadow > 2 * body_c and upper_shadow < body_c * 0.5 and bear_p1 and range_c > 0:
        patterns.append("HAMMER")

    # Shooting Star
    upper_shadow2 = c["high"] - c["close"] if bull_c else c["high"] - c["open"]
    lower_shadow2 = c["open"] - c["low"] if bull_c else c["close"] - c["low"]
    if upper_shadow2 > 2 * body_c and lower_shadow2 < body_c * 0.5 and bull_p1 and range_c > 0:
        patterns.append("SHOOTING_STAR")

    # Bullish Engulfing
    if bull_c and bear_p1 and c["open"] < p1["close"] and c["close"] > p1["open"] and body_c > body_p1:
        patterns.append("BULLISH_ENGULFING")

    # Bearish Engulfing
    if bear_c and bull_p1 and c["open"] > p1["close"] and c["close"] < p1["open"] and body_c > body_p1:
        patterns.append("BEARISH_ENGULFING")

    # Morning Star
    if bear_p1 and body_c < body_p1 * 0.3 and bull_c and body_p2 > body_p1 * 0.5:
        patterns.append("MORNING_STAR")

    # Evening Star
    if bull_p1 and body_c < body_p1 * 0.3 and bear_c and body_p2 > body_p1 * 0.5:
        patterns.append("EVENING_STAR")

    # Three White Soldiers
    range_p1 = p1["high"] - p1["low"]
    if bull_c and bull_p1 and c["close"] > p1["close"] and p1["close"] > p2["close"] and body_c > range_c * 0.6:
        patterns.append("THREE_WHITE_SOLDIERS")

    # Three Black Crows
    if bear_c and bear_p1 and c["close"] < p1["close"] and p1["close"] < p2["close"] and body_c > range_c * 0.6:
        patterns.append("THREE_BLACK_CROWS")

    return patterns

# ── Signal-Logik (gleich wie Live-Bot, ohne Claude) ───────────────────────────
def generate_signal(candles):
    """
    Generiert Signal basierend auf Indikatoren und Mustern.
    Gleiche Logik wie Live-Bot aber ohne Claude API.
    """
    if len(candles) < 50:
        return None

    closes  = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]

    rsi   = calc_rsi(closes)
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
    macd  = calc_macd(closes)
    bb    = calc_bollinger(closes)

    last     = candles[-1]
    price    = last["close"]
    patterns = detect_patterns(candles)

    # Volumen-Filter
    avg_vol   = sum(volumes[-20:]) / 20
    last_vol  = volumes[-1]
    vol_ratio = round(last_vol / avg_vol, 2) if avg_vol > 0 else 0
    if vol_ratio < MIN_VOL:
        return None  # Kein Signal bei niedrigem Volumen

    # Punkte-System für Signal-Stärke
    bull_points = 0
    bear_points = 0

    # RSI
    if rsi:
        if rsi < 30:   bull_points += 3  # Stark überverkauft
        elif rsi < 40: bull_points += 2
        elif rsi < 45: bull_points += 1
        elif rsi > 70: bear_points += 3  # Stark überkauft
        elif rsi > 60: bear_points += 2
        elif rsi > 55: bear_points += 1

    # EMA
    if ema20 and ema50:
        if ema20 > ema50:  bull_points += 2
        else:              bear_points += 2

    # Preis vs EMA20
    if ema20:
        if price > ema20:  bull_points += 1
        else:              bear_points += 1

    # MACD
    if macd:
        if macd > 0:   bull_points += 1
        else:          bear_points += 1

    # Bollinger Bands
    if bb:
        if price < bb["lower"]:  bull_points += 2
        elif price > bb["upper"]: bear_points += 2

    # Kerzen-Muster
    bullish_patterns = ["HAMMER", "BULLISH_ENGULFING", "MORNING_STAR", "THREE_WHITE_SOLDIERS"]
    bearish_patterns = ["SHOOTING_STAR", "BEARISH_ENGULFING", "EVENING_STAR", "THREE_BLACK_CROWS"]

    for p in patterns:
        if p in bullish_patterns:  bull_points += 3
        if p in bearish_patterns:  bear_points += 3
        if p == "DOJI":            pass  # Neutral

    # Trend-Filter (gleich wie Live-Bot)
    total_points = bull_points + bear_points
    if total_points == 0:
        return None

    confidence = 0
    signal     = "HOLD"

    if bull_points > bear_points:
        # BUY nur wenn kein starker Downtrend
        if not (ema20 and ema50 and ema20 < ema50 and rsi and rsi < 45):
            confidence = min(int((bull_points / (bull_points + bear_points)) * 100), 99)
            if confidence >= MIN_CONF:
                signal = "BUY"

    elif bear_points > bull_points:
        # SELL nur wenn kein starker Uptrend
        if not (ema20 and ema50 and ema20 > ema50 and rsi and rsi > 55):
            confidence = min(int((bear_points / (bull_points + bear_points)) * 100), 99)
            if confidence >= MIN_CONF:
                signal = "SELL"

    if signal == "HOLD":
        return None

    # SL/TP berechnen
    if signal == "BUY":
        sl = round(price * (1 - SL_PCT), 6)
        tp = round(price * (1 + TP_PCT), 6)
    else:
        sl = round(price * (1 + SL_PCT), 6)
        tp = round(price * (1 - TP_PCT), 6)

    return {
        "signal":     signal,
        "confidence": confidence,
        "price":      price,
        "sl":         sl,
        "tp":         tp,
        "rsi":        rsi,
        "ema20":      ema20,
        "ema50":      ema50,
        "macd":       macd,
        "vol_ratio":  vol_ratio,
        "patterns":   patterns,
        "bull_pts":   bull_points,
        "bear_pts":   bear_points,
    }

# ── Historische Daten laden ───────────────────────────────────────────────────
def fetch_historical(symbol, start_time, end_time):
    """Lädt alle historischen Kerzen zwischen start_time und end_time"""
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
            time.sleep(0.1)  # Rate limit respektieren
        except Exception as e:
            print(f"  Fehler beim Laden: {e}")
            time.sleep(2)
            break

    return all_candles

# ── Backtest simulieren ───────────────────────────────────────────────────────
def run_backtest_symbol(symbol, candles):
    """Simuliert den Bot auf historischen Daten"""
    trades     = []
    open_trade = None
    window     = 60  # Mindestens 60 Kerzen für Indikatoren

    print(f"\n  Analysiere {len(candles)} Kerzen für {symbol}...")

    for i in range(window, len(candles)):
        window_candles = candles[i - window:i + 1]
        current        = candles[i]
        price          = current["close"]

        # Offenen Trade prüfen
        if open_trade:
            result = None
            pnl    = 0.0

            if open_trade["signal"] == "BUY":
                if price >= open_trade["tp"]:
                    result = "WIN"
                    pnl    = round((open_trade["tp"] - open_trade["entry"]) / open_trade["entry"] * 100, 3)
                elif price <= open_trade["sl"]:
                    result = "LOSS"
                    pnl    = round((open_trade["sl"] - open_trade["entry"]) / open_trade["entry"] * 100, 3)
            elif open_trade["signal"] == "SELL":
                if price <= open_trade["tp"]:
                    result = "WIN"
                    pnl    = round((open_trade["entry"] - open_trade["tp"]) / open_trade["entry"] * 100, 3)
                elif price >= open_trade["sl"]:
                    result = "LOSS"
                    pnl    = round((open_trade["entry"] - open_trade["sl"]) / open_trade["entry"] * 100, 3)

            # Max. 24h offen lassen
            age_candles = i - open_trade["open_idx"]
            if age_candles > 96:  # 96 × 15min = 24h
                result = "EXPIRED"
                pnl    = round((price - open_trade["entry"]) / open_trade["entry"] * 100, 3)
                if open_trade["signal"] == "SELL":
                    pnl = -pnl

            if result:
                open_trade["result"]     = result
                open_trade["close_price"] = price
                open_trade["close_time"] = datetime.fromtimestamp(current["time"] / 1000).strftime("%Y-%m-%d %H:%M")
                open_trade["pnl"]        = pnl
                trades.append(open_trade)
                open_trade = None
            continue

        # Neues Signal generieren
        sig = generate_signal(window_candles)
        if sig and sig["signal"] != "HOLD":
            open_trade = {
                "symbol":     symbol,
                "signal":     sig["signal"],
                "entry":      sig["price"],
                "sl":         sig["sl"],
                "tp":         sig["tp"],
                "confidence": sig["confidence"],
                "rsi":        sig["rsi"],
                "vol_ratio":  sig["vol_ratio"],
                "patterns":   sig["patterns"],
                "open_time":  datetime.fromtimestamp(current["time"] / 1000).strftime("%Y-%m-%d %H:%M"),
                "open_idx":   i,
                "result":     None,
                "pnl":        0.0,
            }

    return trades

# ── Ergebnisse auswerten ──────────────────────────────────────────────────────
def analyze_results(all_trades):
    """Wertet alle Trades aus und berechnet Statistiken"""
    stats = {}

    for symbol in SYMBOLS:
        sym_trades = [t for t in all_trades if t["symbol"] == symbol]
        wins       = [t for t in sym_trades if t["result"] == "WIN"]
        losses     = [t for t in sym_trades if t["result"] == "LOSS"]
        expired    = [t for t in sym_trades if t["result"] == "EXPIRED"]
        total      = len(sym_trades)

        if total == 0:
            stats[symbol] = {"total": 0}
            continue

        win_rate  = round(len(wins) / total * 100, 1) if total > 0 else 0
        total_pnl = round(sum(t["pnl"] for t in sym_trades), 2)
        avg_pnl   = round(total_pnl / total, 3) if total > 0 else 0

        # Beste Muster
        pattern_stats = {}
        for t in sym_trades:
            for p in t.get("patterns", []):
                if p not in pattern_stats:
                    pattern_stats[p] = {"wins": 0, "losses": 0, "total": 0}
                pattern_stats[p]["total"] += 1
                if t["result"] == "WIN":
                    pattern_stats[p]["wins"] += 1
                elif t["result"] == "LOSS":
                    pattern_stats[p]["losses"] += 1

        for p in pattern_stats:
            t = pattern_stats[p]["total"]
            w = pattern_stats[p]["wins"]
            pattern_stats[p]["win_rate"] = round(w / t * 100, 1) if t > 0 else 0

        # BUY vs SELL
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
            "buy_win_rate": round(len(buy_wins) / len(buy_trades) * 100, 1) if buy_trades else 0,
            "sell_total":   len(sell_trades),
            "sell_wins":    len(sell_wins),
            "sell_win_rate":round(len(sell_wins) / len(sell_trades) * 100, 1) if sell_trades else 0,
            "patterns":     pattern_stats,
        }

    return stats

# ── Ergebnisse ausgeben ───────────────────────────────────────────────────────
def print_results(stats):
    print()
    print(bold(green("╔══════════════════════════════════════════════════════════╗")))
    print(bold(green("║              BACKTEST ERGEBNISSE                        ║")))
    print(bold(green("╚══════════════════════════════════════════════════════════╝")))

    for symbol, s in stats.items():
        if s.get("total", 0) == 0:
            print(f"\n  {bold(symbol)}: Keine Trades")
            continue

        wr_color  = green if s["win_rate"] >= 50 else red
        pnl_color = green if s["total_pnl"] >= 0 else red

        print(f"\n  {bold(cyan(symbol))}")
        print(f"  {'─'*45}")
        print(f"  Gesamt Trades:  {s['total']}")
        print(f"  Win-Rate:       {wr_color(str(s['win_rate']) + '%')}")
        print(f"  Wins/Losses:    {green(str(s['wins']))} / {red(str(s['losses']))} / {gray(str(s['expired']) + ' abgelaufen')}")
        print(f"  Gesamt PnL:     {pnl_color(str(s['total_pnl']) + '%')}")
        print(f"  Ø PnL/Trade:    {pnl_color(str(s['avg_pnl']) + '%')}")
        print(f"  BUY:  {s['buy_total']} Trades  │  Win-Rate: {green(str(s['buy_win_rate']) + '%') if s['buy_win_rate'] >= 50 else red(str(s['buy_win_rate']) + '%')}")
        print(f"  SELL: {s['sell_total']} Trades  │  Win-Rate: {green(str(s['sell_win_rate']) + '%') if s['sell_win_rate'] >= 50 else red(str(s['sell_win_rate']) + '%')}")

        if s["patterns"]:
            print(f"\n  Muster-Performance:")
            sorted_patterns = sorted(s["patterns"].items(), key=lambda x: x[1]["win_rate"], reverse=True)
            for pattern, pdata in sorted_patterns:
                if pdata["total"] >= 3:
                    wr = pdata["win_rate"]
                    print(f"    {pattern:<25} {pdata['total']:>3} Trades  │  {green(str(wr)+'%') if wr >= 50 else red(str(wr)+'%')}")

# ── Claude Zusammenfassung ────────────────────────────────────────────────────
def get_claude_summary(stats, all_trades):
    if not ANTHROPIC_API_KEY:
        print(red("\nANTHROPIC_API_KEY fehlt – Claude Zusammenfassung übersprungen"))
        return

    print(f"\n  {blue('Sende Ergebnisse an Claude für Empfehlungen...')}")

    # Kompakte Zusammenfassung für Claude
    summary = ""
    for sym, s in stats.items():
        if s.get("total", 0) == 0:
            continue
        summary += f"\n{sym}: {s['total']} Trades, Win-Rate {s['win_rate']}%, PnL {s['total_pnl']}%"
        summary += f"\n  BUY: {s['buy_win_rate']}% Win-Rate, SELL: {s['sell_win_rate']}% Win-Rate"
        if s["patterns"]:
            best = sorted(s["patterns"].items(), key=lambda x: x[1]["win_rate"], reverse=True)[:3]
            best_str = ", ".join([f"{p[0]} ({p[1]['win_rate']}%)" for p in best if p[1]['total'] >= 3])
            summary += f"\n  Beste Muster: {best_str}"

    prompt = f"""Du bist ein professioneller Crypto-Trading-Analyst.

Ich habe einen Trading-Bot über {YEARS_BACK} Jahre historische Daten (15min Kerzen) getestet.
Folgende Ergebnisse wurden erzielt:

{summary}

Analysiere diese Ergebnisse und gib mir:
1. Eine ehrliche Bewertung ob die Strategie profitabel ist
2. Welche Symbole am besten funktionieren
3. Welche Muster am zuverlässigsten sind
4. Konkrete Empfehlungen zur Verbesserung der Strategie
5. Ob Auto-Trade mit dieser Strategie ratsam wäre

Sei ehrlich und kritisch – keine falschen Versprechungen."""

    client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    print()
    print(bold(cyan("═" * 58)))
    print(bold(cyan("  CLAUDE EMPFEHLUNGEN")))
    print(bold(cyan("═" * 58)))
    print(message.content[0].text)

# ── CSV speichern ─────────────────────────────────────────────────────────────
def save_trades_csv(all_trades):
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "symbol", "signal", "open_time", "close_time",
            "entry", "sl", "tp", "close_price",
            "result", "pnl", "rsi", "vol_ratio",
            "confidence", "patterns"
        ])
        writer.writeheader()
        for t in all_trades:
            writer.writerow({
                "symbol":      t["symbol"],
                "signal":      t["signal"],
                "open_time":   t["open_time"],
                "close_time":  t.get("close_time", ""),
                "entry":       t["entry"],
                "sl":          t["sl"],
                "tp":          t["tp"],
                "close_price": t.get("close_price", ""),
                "result":      t["result"],
                "pnl":         t["pnl"],
                "rsi":         t.get("rsi", ""),
                "vol_ratio":   t.get("vol_ratio", ""),
                "confidence":  t["confidence"],
                "patterns":    ",".join(t.get("patterns", [])),
            })
    print(f"\n  Alle Trades gespeichert in: {cyan(CSV_FILE)}")

# ── Haupt-Funktion ────────────────────────────────────────────────────────────
def main():
    print()
    print(bold(green("╔══════════════════════════════════════════════════════════╗")))
    print(bold(green("║         BITUNIX BOT – BACKTESTING                       ║")))
    print(bold(green("╠══════════════════════════════════════════════════════════╣")))
    print(bold(green(f"║  Symbole:   {' · '.join(SYMBOLS):<44}║")))
    print(bold(green(f"║  Zeitraum:  Letzte {YEARS_BACK} Jahre ({INTERVAL} Kerzen)              ║")))
    print(bold(green(f"║  SL/TP:     {SL_PCT*100:.1f}% / {TP_PCT*100:.1f}%                                 ║")))
    print(bold(green(f"║  Min Vol:   {MIN_VOL}x  │  Min Conf: {MIN_CONF}%                    ║")))
    print(bold(green("╚══════════════════════════════════════════════════════════╝")))
    print()

    # Zeitraum berechnen
    end_time   = int(datetime.now().timestamp() * 1000)
    start_time = int((datetime.now() - timedelta(days=365 * YEARS_BACK)).timestamp() * 1000)

    start_str = datetime.fromtimestamp(start_time / 1000).strftime("%Y-%m-%d")
    end_str   = datetime.fromtimestamp(end_time / 1000).strftime("%Y-%m-%d")
    print(f"  Zeitraum: {cyan(start_str)} bis {cyan(end_str)}")
    print()

    all_trades = []

    for symbol in SYMBOLS:
        print(bold(f"\n  {'═'*50}"))
        print(bold(f"  Lade historische Daten: {symbol}"))
        print(f"  {'═'*50}")

        candles = fetch_historical(symbol, start_time, end_time)
        print(f"  {green(str(len(candles)))} Kerzen geladen")

        if len(candles) < 100:
            print(red(f"  Zu wenige Daten für {symbol} – übersprungen"))
            continue

        trades = run_backtest_symbol(symbol, candles)
        all_trades.extend(trades)

        wins   = len([t for t in trades if t["result"] == "WIN"])
        losses = len([t for t in trades if t["result"] == "LOSS"])
        total  = len(trades)
        wr     = round(wins / total * 100, 1) if total > 0 else 0

        print(f"  Trades simuliert: {total}")
        print(f"  Vorläufige Win-Rate: {green(str(wr)+'%') if wr >= 50 else red(str(wr)+'%')}")

    if not all_trades:
        print(red("\nKeine Trades gefunden. Strategie zu restriktiv oder keine Daten."))
        return

    # Ergebnisse auswerten
    stats = analyze_results(all_trades)
    print_results(stats)

    # CSV speichern
    save_trades_csv(all_trades)

    # JSON speichern
    with open(REPORT_FILE, "w") as f:
        json.dump(stats, f, indent=2)
    print(f"  Bericht gespeichert in: {cyan(REPORT_FILE)}")

    # Claude Zusammenfassung (nur einmal, günstig)
    get_claude_summary(stats, all_trades)

    print()
    print(bold(green("  Backtesting abgeschlossen!")))
    print()

if __name__ == "__main__":
    main()
