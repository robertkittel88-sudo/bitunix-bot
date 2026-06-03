"""
╔══════════════════════════════════════════════════════════╗
║         Bitunix Multi-Asset AI Trading Bot v3            ║
║         Powered by Claude AI                             ║
║                                                          ║
║  Symbole:  BTC/USDT · ETH/USDT · HBAR/USDT             ║
║  Neu:      Kerzen-Muster · Support/Resistance · Breakout ║
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
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

SYMBOLS    = ["BTCUSDT", "ETHUSDT", "HBARUSDT"]
INTERVAL   = "15m"
LIMIT      = 60
CYCLE_MIN  = 15
MIN_CONF   = 65
AUTO_TRADE = False

BINANCE_BASE  = "https://fapi.binance.com"
LOG_FILE      = "signals.csv"
REPORT_FILE   = "performance.json"
OPEN_FILE     = "open_signals.json"
RESULTS_FILE  = "results.json"

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
def ts():      return gray(f"[{datetime.now().strftime('%H:%M:%S')}]")

def log(msg, level="INFO"):
    prefix = {
        "INFO":  blue("INFO "),
        "OK":    green("OK   "),
        "WARN":  yellow("WARN "),
        "ERROR": red("ERROR"),
        "BUY":   green("BUY  "),
        "SELL":  red("SELL "),
        "HOLD":  yellow("HOLD "),
        "WIN":   green("WIN  "),
        "LOSS":  red("LOSS "),
    }.get(level, "     ")
    print(f"{ts()} {prefix} {msg}")

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": int(TELEGRAM_CHAT_ID), "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        log(f"Telegram Fehler: {e}", "WARN")

# ── Technische Indikatoren ────────────────────────────────────────────────────
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
    sl = closes[-period:]
    mean = sum(sl) / period
    std = math.sqrt(sum((x - mean) ** 2 for x in sl) / period)
    return {"upper": round(mean + 2*std, 6), "mid": round(mean, 6), "lower": round(mean - 2*std, 6)}

# ── Kerzen-Muster ─────────────────────────────────────────────────────────────
def detect_candle_patterns(candles):
    patterns = []
    if len(candles) < 3:
        return patterns

    c  = candles[-1]   # aktuelle Kerze
    p1 = candles[-2]   # vorherige
    p2 = candles[-3]   # vorvorherige

    body_c  = abs(c["close"]  - c["open"])
    body_p1 = abs(p1["close"] - p1["open"])
    body_p2 = abs(p2["close"] - p2["open"])
    range_c = c["high"] - c["low"]
    range_p1 = p1["high"] - p1["low"]

    bull_c  = c["close"]  > c["open"]
    bear_c  = c["close"]  < c["open"]
    bull_p1 = p1["close"] > p1["open"]
    bear_p1 = p1["close"] < p1["open"]

    # ── Doji ──────────────────────────────────────────────────────────────────
    if range_c > 0 and body_c / range_c < 0.1:
        patterns.append("DOJI – Unentschlossenheit, mögliche Trendwende")

    # ── Hammer (bullisch) ─────────────────────────────────────────────────────
    lower_shadow = c["open"] - c["low"] if bull_c else c["close"] - c["low"]
    upper_shadow = c["high"] - c["close"] if bull_c else c["high"] - c["open"]
    if (lower_shadow > 2 * body_c and upper_shadow < body_c * 0.5
            and bear_p1 and range_c > 0):
        patterns.append("HAMMER – Bullisches Umkehrmuster, Kaufdruck von unten")

    # ── Shooting Star (bärisch) ───────────────────────────────────────────────
    upper_shadow2 = c["high"] - c["close"] if bull_c else c["high"] - c["open"]
    lower_shadow2 = c["open"] - c["low"] if bull_c else c["close"] - c["low"]
    if (upper_shadow2 > 2 * body_c and lower_shadow2 < body_c * 0.5
            and bull_p1 and range_c > 0):
        patterns.append("SHOOTING STAR – Bärisches Umkehrmuster, Verkaufsdruck von oben")

    # ── Bullish Engulfing ─────────────────────────────────────────────────────
    if (bull_c and bear_p1
            and c["open"] < p1["close"]
            and c["close"] > p1["open"]
            and body_c > body_p1):
        patterns.append("BULLISH ENGULFING – Starkes bullisches Umkehrsignal")

    # ── Bearish Engulfing ─────────────────────────────────────────────────────
    if (bear_c and bull_p1
            and c["open"] > p1["close"]
            and c["close"] < p1["open"]
            and body_c > body_p1):
        patterns.append("BEARISH ENGULFING – Starkes bärisches Umkehrsignal")

    # ── Morning Star (bullisch, 3 Kerzen) ─────────────────────────────────────
    if (bear_p1 and body_p1 > 0
            and body_c < body_p1 * 0.3
            and bull_c
            and c["close"] > (p2["open"] + p2["close"]) / 2
            and body_p2 > body_p1 * 0.5):
        patterns.append("MORNING STAR – Bullisches 3-Kerzen-Umkehrmuster")

    # ── Evening Star (bärisch, 3 Kerzen) ──────────────────────────────────────
    if (bull_p1 and body_p1 > 0
            and body_c < body_p1 * 0.3
            and bear_c
            and c["close"] < (p2["open"] + p2["close"]) / 2
            and body_p2 > body_p1 * 0.5):
        patterns.append("EVENING STAR – Bärisches 3-Kerzen-Umkehrmuster")

    # ── Three White Soldiers (bullisch) ───────────────────────────────────────
    if (bull_c and bull_p1
            and c["close"] > p1["close"]
            and p1["close"] > p2["close"]
            and body_c > range_c * 0.6
            and body_p1 > range_p1 * 0.6):
        patterns.append("THREE WHITE SOLDIERS – Starke bullische Fortsetzung")

    # ── Three Black Crows (bärisch) ────────────────────────────────────────────
    if (bear_c and bear_p1
            and c["close"] < p1["close"]
            and p1["close"] < p2["close"]
            and body_c > range_c * 0.6
            and body_p1 > range_p1 * 0.6):
        patterns.append("THREE BLACK CROWS – Starke bärische Fortsetzung")

    return patterns

# ── Support & Resistance ──────────────────────────────────────────────────────
def find_support_resistance(candles, lookback=20):
    highs  = [c["high"]  for c in candles[-lookback:]]
    lows   = [c["low"]   for c in candles[-lookback:]]
    closes = [c["close"] for c in candles[-lookback:]]
    price  = closes[-1]

    resistance = round(max(highs), 6)
    support    = round(min(lows), 6)
    mid        = round((resistance + support) / 2, 6)

    dist_to_res = round((resistance - price) / price * 100, 2)
    dist_to_sup = round((price - support) / price * 100, 2)

    position = "OBEN" if price > mid else "UNTEN"

    return {
        "resistance":   resistance,
        "support":      support,
        "mid":          mid,
        "dist_res":     dist_to_res,
        "dist_sup":     dist_to_sup,
        "position":     position,
    }

# ── Trend-Struktur (Higher Highs / Lower Lows) ────────────────────────────────
def analyze_trend_structure(candles, lookback=10):
    recent = candles[-lookback:]
    highs  = [c["high"]  for c in recent]
    lows   = [c["low"]   for c in recent]

    hh = all(highs[i] >= highs[i-1] for i in range(1, len(highs)))
    hl = all(lows[i]  >= lows[i-1]  for i in range(1, len(lows)))
    lh = all(highs[i] <= highs[i-1] for i in range(1, len(highs)))
    ll = all(lows[i]  <= lows[i-1]  for i in range(1, len(lows)))

    if hh and hl:
        return "UPTREND (Higher Highs + Higher Lows)"
    elif lh and ll:
        return "DOWNTREND (Lower Highs + Lower Lows)"
    elif hh and not hl:
        return "SCHWACHER UPTREND (Higher Highs, aber keine Higher Lows)"
    elif ll and not lh:
        return "SCHWACHER DOWNTREND (Lower Lows, aber keine Lower Highs)"
    else:
        return "SEITWÄRTS (keine klare Struktur)"

# ── Breakout Erkennung ────────────────────────────────────────────────────────
def detect_breakout(candles, sr):
    if len(candles) < 5:
        return None
    prev_closes = [c["close"] for c in candles[-5:-1]]
    current     = candles[-1]["close"]
    current_vol = candles[-1]["volume"]
    avg_vol     = sum(c["volume"] for c in candles[-20:]) / 20

    vol_confirmed = current_vol > avg_vol * 1.5

    # Breakout nach oben
    if all(c < sr["resistance"] for c in prev_closes) and current > sr["resistance"]:
        if vol_confirmed:
            return "BULLISCHER BREAKOUT – Preis bricht Widerstand mit hohem Volumen"
        else:
            return "SCHWACHER BULLISCHER BREAKOUT – Kein Volumen, möglicherweise False Breakout"

    # Breakout nach unten
    if all(c > sr["support"] for c in prev_closes) and current < sr["support"]:
        if vol_confirmed:
            return "BÄRISCHER BREAKOUT – Preis bricht Support mit hohem Volumen"
        else:
            return "SCHWACHER BÄRISCHER BREAKOUT – Kein Volumen, möglicherweise False Breakout"

    return None

# ── Kerzen laden ──────────────────────────────────────────────────────────────
def fetch_candles(symbol):
    url    = f"{BINANCE_BASE}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": INTERVAL, "limit": LIMIT}
    resp   = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list) or len(data) == 0:
        raise ValueError(f"Keine Kerzendaten für {symbol}")
    return [
        {"time": int(c[0]), "open": float(c[1]), "high": float(c[2]),
         "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])}
        for c in data
    ]

# ── SL/TP Tracking ────────────────────────────────────────────────────────────
def load_open_signals():
    if Path(OPEN_FILE).exists():
        with open(OPEN_FILE, "r") as f:
            return json.load(f)
    return []

def save_open_signals(signals):
    with open(OPEN_FILE, "w") as f:
        json.dump(signals, f, indent=2)

def load_results():
    if Path(RESULTS_FILE).exists():
        with open(RESULTS_FILE, "r") as f:
            return json.load(f)
    return {"wins": 0, "losses": 0, "total_pnl": 0.0, "by_symbol": {}}

def save_results(results):
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2)

def check_open_signals(symbol, current_price):
    open_signals = load_open_signals()
    results      = load_results()
    updated      = []

    for sig in open_signals:
        if sig["symbol"] != symbol:
            updated.append(sig)
            continue

        sl        = sig["stopLoss"]
        tp        = sig["takeProfit"]
        entry     = sig["entry"]
        direction = sig["signal"]
        result    = None
        pnl       = 0.0

        if direction == "BUY":
            if current_price >= tp:
                result = "WIN";  pnl = round((tp - entry) / entry * 100, 3)
            elif current_price <= sl:
                result = "LOSS"; pnl = round((sl - entry) / entry * 100, 3)
        elif direction == "SELL":
            if current_price <= tp:
                result = "WIN";  pnl = round((entry - tp) / entry * 100, 3)
            elif current_price >= sl:
                result = "LOSS"; pnl = round((entry - sl) / entry * 100, 3)

        if result:
            log(f"[{symbol}] {direction} → {result} | PnL: {'+' if pnl > 0 else ''}{pnl}%",
                "WIN" if result == "WIN" else "LOSS")
            if symbol not in results["by_symbol"]:
                results["by_symbol"][symbol] = {"wins": 0, "losses": 0, "pnl": 0.0}
            if result == "WIN":
                results["wins"] += 1
                results["by_symbol"][symbol]["wins"] += 1
            else:
                results["losses"] += 1
                results["by_symbol"][symbol]["losses"] += 1
            results["total_pnl"] = round(results["total_pnl"] + pnl, 3)
            results["by_symbol"][symbol]["pnl"] = round(
                results["by_symbol"][symbol].get("pnl", 0) + pnl, 3)
            save_results(results)
            emoji = "✅" if result == "WIN" else "❌"
            send_telegram(
                f"{emoji} <b>{result}: {symbol} {direction}</b>\n"
                f"Entry: ${entry:,.4f}\n"
                f"Close: ${current_price:,.4f}\n"
                f"PnL: {'+' if pnl > 0 else ''}{pnl}%"
            )
        else:
            age_h = (time.time() - sig["openTime"]) / 3600
            if age_h < 24:
                updated.append(sig)

    save_open_signals(updated)
    return results

def add_open_signal(signal_data):
    if signal_data.get("signal") in ["BUY", "SELL"] and signal_data.get("confidence", 0) >= MIN_CONF:
        open_signals = load_open_signals()
        open_signals.append({
            "symbol":     signal_data["symbol"],
            "signal":     signal_data["signal"],
            "entry":      signal_data["entry"],
            "stopLoss":   signal_data["stopLoss"],
            "takeProfit": signal_data["takeProfit"],
            "confidence": signal_data["confidence"],
            "openTime":   time.time(),
            "openDate":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        save_open_signals(open_signals)

# ── Claude Analyse ────────────────────────────────────────────────────────────
def analyze_with_claude(client, symbol, candles, perf_context="", results_context=""):
    closes  = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]
    rsi     = calc_rsi(closes)
    ema20   = calc_ema(closes, 20)
    ema50   = calc_ema(closes, 50)
    macd    = calc_macd(closes)
    bb      = calc_bollinger(closes)
    last    = candles[-1]
    prev    = candles[-2]
    change  = ((last["close"] - prev["close"]) / prev["close"] * 100)

    # Volumen
    avg_vol_20 = sum(volumes[-20:]) / 20
    avg_vol_5  = sum(volumes[-5:]) / 5
    last_vol   = volumes[-1]
    vol_ratio  = round(last_vol / avg_vol_20, 2) if avg_vol_20 > 0 else 1.0
    vol_spike  = vol_ratio >= 2.0
    vol_trend  = "INCREASING" if avg_vol_5 > avg_vol_20 else "DECREASING"

    counter_move = ""
    if vol_spike:
        if rsi and rsi > 65:
            counter_move = f"SPIKE ({vol_ratio}x) bei RSI {rsi} – Erschöpfung, Reversal DOWN wahrscheinlich"
        elif rsi and rsi < 35:
            counter_move = f"SPIKE ({vol_ratio}x) bei RSI {rsi} – Erschöpfung, Reversal UP wahrscheinlich"
        else:
            counter_move = f"SPIKE ({vol_ratio}x) – Reversal möglich"

    # Kerzen-Muster
    patterns  = detect_candle_patterns(candles)
    sr        = find_support_resistance(candles)
    trend_str = analyze_trend_structure(candles)
    breakout  = detect_breakout(candles, sr)

    patterns_str = "\n".join(f"  • {p}" for p in patterns) if patterns else "  • Kein bekanntes Muster erkannt"
    breakout_str = breakout if breakout else "Kein Breakout"

    candle_str = "\n".join(
        f"{datetime.fromtimestamp(c['time']/1000).strftime('%H:%M')} "
        f"O:{c['open']} H:{c['high']} L:{c['low']} C:{c['close']} V:{int(c['volume'])}"
        for c in candles[-12:]
    )
    bb_str = f"{bb['upper']}/{bb['mid']}/{bb['lower']}" if bb else "N/A"

    prompt = f"""You are a professional crypto futures trader analyzing {symbol} on {INTERVAL} timeframe.
{perf_context}{results_context}

LAST 12 CANDLES:
{candle_str}

TECHNICAL INDICATORS:
• RSI(14):            {rsi if rsi is not None else 'N/A'}
• EMA(20):            {ema20 if ema20 else 'N/A'}
• EMA(50):            {ema50 if ema50 else 'N/A'}
• MACD(12,26):        {macd if macd else 'N/A'}
• BB Upper/Mid/Lower: {bb_str}
• Last candle Δ:      {change:.3f}%
• Current price:      {last['close']}

VOLUME ANALYSIS:
• Volume ratio:       {vol_ratio}x avg
• Volume trend:       {vol_trend}
• Volume spike:       {'YES – ' + counter_move if vol_spike else 'NO'}

CANDLE PATTERNS DETECTED:
{patterns_str}

TREND STRUCTURE:
• {trend_str}

SUPPORT & RESISTANCE:
• Resistance:  {sr['resistance']} ({sr['dist_res']}% away)
• Support:     {sr['support']} ({sr['dist_sup']}% away)
• Price pos.:  {sr['position']} der Mitte
• Breakout:    {breakout_str}

TRADING RULES:
- Candle patterns ADD weight to signals – Engulfing/Hammer/Stars are strong signals
- Breakout WITH volume = strong entry signal
- Breakout WITHOUT volume = wait for confirmation
- Price near resistance + bearish pattern = SELL bias
- Price near support + bullish pattern = BUY bias
- DOJI near extremes = possible reversal, wait for confirmation
- Trend structure must align with signal direction
- Only signal BUY/SELL when 3+ factors align (indicators + pattern + volume)
- Be BALANCED: give SELL when bearish, BUY when bullish

Set SL 1-2% away, TP 2-3% away based on S/R levels.

Respond ONLY with valid JSON, no markdown:
{{
  "signal": "BUY" or "SELL" or "HOLD",
  "confidence": integer 0-100,
  "reasoning": "max 180 chars including pattern info",
  "entry": {last['close']},
  "stopLoss": number,
  "takeProfit": number,
  "risk": "LOW" or "MEDIUM" or "HIGH",
  "trend": "BULLISH" or "BEARISH" or "NEUTRAL",
  "rsi": {rsi if rsi is not None else 50},
  "volumeRatio": {vol_ratio},
  "volumeSpike": {"true" if vol_spike else "false"},
  "patterns": {json.dumps(patterns)},
  "breakout": "{breakout_str}"
}}"""

    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=700,
        messages=[{"role": "user", "content": prompt}]
    )
    text  = message.content[0].text.strip()
    start = text.find("{")
    end   = text.rfind("}") + 1
    return json.loads(text[start:end])

# ── Signal speichern ──────────────────────────────────────────────────────────
def save_signal(signal_data):
    file_exists = Path(LOG_FILE).exists()
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "timestamp", "symbol", "signal", "confidence", "entry",
            "stopLoss", "takeProfit", "risk", "trend", "rsi",
            "volumeRatio", "patterns", "breakout", "reasoning"
        ])
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol":      signal_data.get("symbol"),
            "signal":      signal_data.get("signal"),
            "confidence":  signal_data.get("confidence"),
            "entry":       signal_data.get("entry"),
            "stopLoss":    signal_data.get("stopLoss"),
            "takeProfit":  signal_data.get("takeProfit"),
            "risk":        signal_data.get("risk"),
            "trend":       signal_data.get("trend"),
            "rsi":         signal_data.get("rsi"),
            "volumeRatio": signal_data.get("volumeRatio"),
            "patterns":    str(signal_data.get("patterns", [])),
            "breakout":    signal_data.get("breakout", ""),
            "reasoning":   signal_data.get("reasoning"),
        })

# ── Performance ───────────────────────────────────────────────────────────────
def load_performance():
    if Path(REPORT_FILE).exists():
        with open(REPORT_FILE, "r") as f:
            return json.load(f)
    return {}

def get_perf_context(symbol, perf):
    data = perf.get(symbol, {})
    if not data or data.get("total", 0) == 0:
        return ""
    return (f"\nSIGNAL HISTORY ({data['total']} total): "
            f"BUY: {data.get('buy',0)} | SELL: {data.get('sell',0)} | HOLD: {data.get('hold',0)}")

def get_results_context(symbol):
    results  = load_results()
    sym_data = results.get("by_symbol", {}).get(symbol, {})
    if not sym_data:
        return ""
    wins   = sym_data.get("wins", 0)
    losses = sym_data.get("losses", 0)
    pnl    = sym_data.get("pnl", 0)
    total  = wins + losses
    wr     = round(wins / total * 100) if total > 0 else 0
    return f"\nRESULTS ({symbol}): {wins}W/{losses}L | Win-Rate: {wr}% | PnL: {pnl:+.2f}%"

def update_performance(symbol, signal, perf):
    if symbol not in perf:
        perf[symbol] = {"total": 0, "buy": 0, "sell": 0, "hold": 0}
    perf[symbol]["total"] += 1
    perf[symbol][signal.lower()] = perf[symbol].get(signal.lower(), 0) + 1
    with open(REPORT_FILE, "w") as f:
        json.dump(perf, f, indent=2)
    return perf

# ── Tägliche Zusammenfassung ──────────────────────────────────────────────────
def send_daily_summary(perf):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    results = load_results()
    wins    = results.get("wins", 0)
    losses  = results.get("losses", 0)
    total   = wins + losses
    wr      = round(wins / total * 100) if total > 0 else 0
    pnl     = results.get("total_pnl", 0)

    msg  = "📊 <b>TÄGLICHE ZUSAMMENFASSUNG</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"Gesamt: {wins}W / {losses}L | Win-Rate: {wr}%\n"
    msg += f"Gesamt PnL: {'+' if pnl >= 0 else ''}{pnl:.2f}%\n\n"
    for sym in SYMBOLS:
        d  = perf.get(sym, {})
        rd = results.get("by_symbol", {}).get(sym, {})
        w  = rd.get("wins", 0)
        l  = rd.get("losses", 0)
        p  = rd.get("pnl", 0)
        msg += f"<b>{sym}</b>: {d.get('buy',0)}B/{d.get('sell',0)}S/{d.get('hold',0)}H"
        if w + l > 0:
            msg += f" | {w}W/{l}L | {'+' if p >= 0 else ''}{p:.2f}%"
        msg += "\n"
    send_telegram(msg)
    log("Tägliche Zusammenfassung per Telegram gesendet", "OK")

# ── Signal anzeigen ───────────────────────────────────────────────────────────
def print_signal(symbol, result, price):
    sig       = result["signal"]
    conf      = result["confidence"]
    trend     = result["trend"]
    rsi_val   = result.get("rsi", "?")
    vol_ratio = result.get("volumeRatio", 1.0)
    vol_spike = result.get("volumeSpike", False)
    patterns  = result.get("patterns", [])
    breakout  = result.get("breakout", "")

    sig_str   = green(f"▲ {sig}") if sig == "BUY" else red(f"▼ {sig}") if sig == "SELL" else yellow(f"● {sig}")
    conf_str  = green(f"{conf}%") if conf >= MIN_CONF else yellow(f"{conf}%")

    print()
    print(f"  ┌─ {bold(symbol)} {'─'*30}")
    print(f"  │  Signal:     {sig_str}  Confidence: {conf_str}")
    print(f"  │  Trend:      {trend}  │  RSI: {rsi_val}")
    print(f"  │  Volumen:    {vol_ratio}x {'⚠ SPIKE' if vol_spike else ''}")
    if patterns:
        for p in patterns:
            print(f"  │  Muster:     {yellow(p)}")
    if breakout and breakout != "Kein Breakout":
        print(f"  │  Breakout:   {cyan(breakout)}")
    print(f"  │  Preis:      ${price:,.4f}")
    if sig != "HOLD":
        sl = result.get("stopLoss", 0)
        tp = result.get("takeProfit", 0)
        print(f"  │  Entry:      ${result.get('entry', price):,.4f}")
        print(f"  │  Stop Loss:  {red(f'${sl:,.4f}')}")
        print(f"  │  Take Profit:{green(f'${tp:,.4f}')}")
        print(f"  │  Risiko:     {result.get('risk', '?')}")
    print(f"  │  Begründung: {gray(result.get('reasoning', ''))}")
    print(f"  └{'─'*38}")

# ── Zusammenfassung ───────────────────────────────────────────────────────────
def print_summary(results_dict, results):
    print()
    print(bold(f"  {'─'*50}"))
    print(bold("  ZYKLUS ZUSAMMENFASSUNG"))
    print(f"  {'─'*50}")
    for sym, res in results_dict.items():
        if res is None:
            print(f"  {sym:<12} {red('FEHLER')}")
            continue
        sig     = res["signal"]
        sig_str = green("BUY ") if sig == "BUY" else red("SELL") if sig == "SELL" else yellow("HOLD")
        conf    = res["confidence"]
        pats    = res.get("patterns", [])
        pat_str = f" │ {yellow(pats[0][:30]+'...' if len(pats[0]) > 30 else pats[0])}" if pats else ""
        print(f"  {sym:<12} {sig_str}  {conf}%  {gray(res.get('trend','?'))}{pat_str}")
    print(f"  {'─'*50}")

    wins   = results.get("wins", 0)
    losses = results.get("losses", 0)
    total  = wins + losses
    if total > 0:
        wr  = round(wins / total * 100)
        pnl = results.get("total_pnl", 0)
        print(f"\n  TRACKING: {green(f'{wins}W')} / {red(f'{losses}L')} | "
              f"Win-Rate: {green(f'{wr}%') if wr >= 50 else red(f'{wr}%')} | "
              f"PnL: {green(f'+{pnl:.2f}%') if pnl >= 0 else red(f'{pnl:.2f}%')}")
    print()

# ── Haupt-Bot-Loop ────────────────────────────────────────────────────────────
def run_bot():
    print()
    print(bold(green("╔══════════════════════════════════════════════════════════╗")))
    print(bold(green("║         BITUNIX MULTI-ASSET AI BOT v3                   ║")))
    print(bold(green("║         Powered by Claude AI                             ║")))
    print(bold(green("╠══════════════════════════════════════════════════════════╣")))
    print(bold(green(f"║  Symbole:  {' · '.join(SYMBOLS):<44}║")))
    print(bold(green(f"║  Muster:   Hammer · Engulfing · Doji · Stars · Breakout ║")))
    print(bold(green(f"║  Telegram: {'AN' if TELEGRAM_TOKEN else 'AUS'}{'':>48}║")))
    print(bold(green("╚══════════════════════════════════════════════════════════╝")))
    print()

    if not ANTHROPIC_API_KEY:
        print(red("FEHLER: ANTHROPIC_API_KEY fehlt in .env"))
        return

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    perf   = load_performance()
    cycle  = 0
    last_daily = datetime.now().date()

    log("Claude API verbunden ✓", "OK")
    log(f"Telegram: {'aktiv' if TELEGRAM_TOKEN else 'nicht konfiguriert'}", "INFO")
    log("Kerzen-Muster Erkennung: aktiv", "OK")
    log("Starte erste Analyse...", "INFO")

    if TELEGRAM_TOKEN:
        send_telegram(
            "🚀 <b>Bitunix AI Bot v3 gestartet</b>\n"
            "Analysiere: " + " · ".join(SYMBOLS) + "\n"
            "Neu: Kerzen-Muster, Support/Resistance, Breakout"
        )

    while True:
        cycle += 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(bold(f"\n{'═'*58}"))
        print(bold(f"  ZYKLUS #{cycle}  │  {now}"))
        print(bold(f"{'═'*58}"))

        results_dict = {}
        results      = load_results()

        for symbol in SYMBOLS:
            log(f"[{symbol}] Lade Kerzen...", "INFO")
            try:
                candles = fetch_candles(symbol)
                price   = candles[-1]["close"]
                log(f"[{symbol}] Preis: ${price:,.4f}  │  {len(candles)} Kerzen", "OK")
                results = check_open_signals(symbol, price)
            except Exception as e:
                log(f"[{symbol}] Kerzen-Fehler: {e}", "ERROR")
                results_dict[symbol] = None
                time.sleep(3)
                continue

            log(f"[{symbol}] Claude analysiert...", "INFO")
            try:
                perf_ctx    = get_perf_context(symbol, perf)
                results_ctx = get_results_context(symbol)
                result      = analyze_with_claude(client, symbol, candles, perf_ctx, results_ctx)
                result["symbol"] = symbol

                print_signal(symbol, result, price)
                save_signal(result)
                perf = update_performance(symbol, result["signal"], perf)
                results_dict[symbol] = result
                add_open_signal(result)

                if result["signal"] != "HOLD" and result["confidence"] >= MIN_CONF:
                    patterns = result.get("patterns", [])
                    pat_str  = "\n".join(f"  • {p}" for p in patterns) if patterns else ""
                    breakout = result.get("breakout", "")
                    emoji    = "🟢" if result["signal"] == "BUY" else "🔴"
                    sl       = result.get("stopLoss", 0)
                    tp       = result.get("takeProfit", 0)
                    msg      = (
                        f"{emoji} <b>{result['signal']}: {symbol}</b>\n"
                        f"Preis: ${price:,.4f}\n"
                        f"Confidence: {result['confidence']}%\n"
                        f"Trend: {result['trend']} | RSI: {result.get('rsi','?')}\n"
                        f"SL: ${sl:,.4f} | TP: ${tp:,.4f}\n"
                    )
                    if pat_str:
                        msg += f"Muster:\n{pat_str}\n"
                    if breakout and breakout != "Kein Breakout":
                        msg += f"Breakout: {breakout}\n"
                    msg += f"📝 {result.get('reasoning','')}"
                    send_telegram(msg)

            except Exception as e:
                log(f"[{symbol}] Analyse-Fehler: {e}", "ERROR")
                results_dict[symbol] = None

            time.sleep(3)

        print_summary(results_dict, results)

        print(bold("  GESAMTE STATISTIK:"))
        for sym in SYMBOLS:
            d = perf.get(sym, {})
            if d.get("total", 0) > 0:
                print(f"  {sym:<12} Gesamt: {d['total']:>4}  "
                      f"BUY: {green(str(d.get('buy',0)))}  "
                      f"SELL: {red(str(d.get('sell',0)))}  "
                      f"HOLD: {yellow(str(d.get('hold',0)))}")

        today = datetime.now().date()
        if today > last_daily and datetime.now().hour >= 8:
            send_daily_summary(perf)
            last_daily = today

        log(f"Nächste Analyse in {CYCLE_MIN} Minuten. STRG+C zum Beenden.", "INFO")
        try:
            for remaining in range(CYCLE_MIN * 60, 0, -30):
                mins = remaining // 60
                secs = remaining % 60
                print(f"\r  {gray(f'Nächste Analyse in: {mins:02d}:{secs:02d}')}  ", end="", flush=True)
                time.sleep(30)
        except KeyboardInterrupt:
            print()
            log("Bot gestoppt.", "WARN")
            send_telegram("⛔ <b>Bot gestoppt</b>")
            break

        print(f"\r{' '*50}\r", end="")

if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        print()
        log("Bot beendet.", "WARN")
