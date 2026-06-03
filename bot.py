"""
╔══════════════════════════════════════════════════════════╗
║         Bitunix Multi-Asset AI Trading Bot v5            ║
║         Powered by Claude AI                             ║
║                                                          ║
║  Verbesserungen basierend auf Backtest-Ergebnissen:     ║
║  - BTC entfernt (zu niedrige Win-Rate)                  ║
║  - Nur Top-Muster: Shooting Star, Evening Star, TWS     ║
║  - Confidence: 80% (erhöht von 75%)                     ║
║  - Min Volumen: 0.5x (erhöht von 0.3x)                 ║
║  - SL: 1.0% / TP: 3.0% (1:3 Ratio)                    ║
║  - Max 2 Trades pro Symbol pro Tag                      ║
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

# BTC entfernt – zu niedrige Win-Rate (21.7%)
SYMBOLS       = ["ETHUSDT", "HBARUSDT"]
INTERVAL      = "15m"
LIMIT         = 60
CYCLE_MIN     = 15
MIN_CONF      = 80      # Erhöht von 75% auf 80%
MIN_VOL       = 0.5     # Erhöht von 0.3x auf 0.5x
SL_PCT        = 0.010   # 1.0% Stop Loss
TP_PCT        = 0.030   # 3.0% Take Profit (1:3 Ratio)
MAX_TRADES_DAY = 2      # Max 2 Trades pro Symbol pro Tag
AUTO_TRADE    = False

# Nur bewährte Top-Muster aus Backtest
TOP_PATTERNS = {
    "ETHUSDT":  ["SHOOTING_STAR", "EVENING_STAR", "BULLISH_ENGULFING", "MORNING_STAR"],
    "HBARUSDT": ["SHOOTING_STAR", "THREE_WHITE_SOLDIERS", "MORNING_STAR", "EVENING_STAR"],
}

BINANCE_BASE  = "https://fapi.binance.com"
LOG_FILE      = "signals.csv"
REPORT_FILE   = "performance.json"
OPEN_FILE     = "open_signals.json"
RESULTS_FILE  = "results.json"
DAILY_FILE    = "daily_trades.json"

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
        "SKIP":  yellow("SKIP "),
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

def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        high  = candles[i]["high"]
        low   = candles[i]["low"]
        close = candles[i-1]["close"]
        trs.append(max(high - low, abs(high - close), abs(low - close)))
    return round(sum(trs[-period:]) / period, 6)

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
    range_p1 = p1["high"] - p1["low"]

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
    if bull_c and bull_p1 and c["close"] > p1["close"] and p1["close"] > p2["close"] and body_c > range_c * 0.6:
        patterns.append("THREE_WHITE_SOLDIERS")

    # Three Black Crows
    if bear_c and bear_p1 and c["close"] < p1["close"] and p1["close"] < p2["close"] and body_c > range_c * 0.6:
        patterns.append("THREE_BLACK_CROWS")

    return patterns

# ── Support & Resistance ──────────────────────────────────────────────────────
def find_support_resistance(candles, lookback=20):
    highs  = [c["high"]  for c in candles[-lookback:]]
    lows   = [c["low"]   for c in candles[-lookback:]]
    closes = [c["close"] for c in candles[-lookback:]]
    price  = closes[-1]

    resistance  = round(max(highs), 6)
    support     = round(min(lows), 6)
    mid         = round((resistance + support) / 2, 6)
    dist_to_res = round((resistance - price) / price * 100, 2)
    dist_to_sup = round((price - support) / price * 100, 2)
    position    = "OBEN" if price > mid else "UNTEN"

    return {"resistance": resistance, "support": support, "mid": mid,
            "dist_res": dist_to_res, "dist_sup": dist_to_sup, "position": position}

# ── Trend-Struktur ────────────────────────────────────────────────────────────
def analyze_trend_structure(candles, lookback=10):
    recent = candles[-lookback:]
    highs  = [c["high"] for c in recent]
    lows   = [c["low"]  for c in recent]

    hh = all(highs[i] >= highs[i-1] for i in range(1, len(highs)))
    hl = all(lows[i]  >= lows[i-1]  for i in range(1, len(lows)))
    lh = all(highs[i] <= highs[i-1] for i in range(1, len(highs)))
    ll = all(lows[i]  <= lows[i-1]  for i in range(1, len(lows)))

    if hh and hl:   return "UPTREND (HH+HL)"
    elif lh and ll: return "DOWNTREND (LH+LL)"
    else:           return "SEITWÄRTS"

# ── Breakout ──────────────────────────────────────────────────────────────────
def detect_breakout(candles, sr):
    if len(candles) < 5:
        return None
    prev_closes = [c["close"] for c in candles[-5:-1]]
    current     = candles[-1]["close"]
    current_vol = candles[-1]["volume"]
    avg_vol     = sum(c["volume"] for c in candles[-20:]) / 20
    vol_ok      = current_vol > avg_vol * 1.5

    if all(c < sr["resistance"] for c in prev_closes) and current > sr["resistance"]:
        return "BULLISCHER BREAKOUT" + (" (Volumen OK)" if vol_ok else " (schwaches Volumen)")
    if all(c > sr["support"] for c in prev_closes) and current < sr["support"]:
        return "BÄRISCHER BREAKOUT" + (" (Volumen OK)" if vol_ok else " (schwaches Volumen)")
    return None

# ── Tages-Trade-Limit ─────────────────────────────────────────────────────────
def load_daily_trades():
    if Path(DAILY_FILE).exists():
        with open(DAILY_FILE, "r") as f:
            return json.load(f)
    return {}

def save_daily_trades(data):
    with open(DAILY_FILE, "w") as f:
        json.dump(data, f, indent=2)

def check_daily_limit(symbol):
    today  = datetime.now().strftime("%Y-%m-%d")
    daily  = load_daily_trades()
    key    = f"{symbol}_{today}"
    count  = daily.get(key, 0)
    return count < MAX_TRADES_DAY, count

def increment_daily_trades(symbol):
    today = datetime.now().strftime("%Y-%m-%d")
    daily = load_daily_trades()
    key   = f"{symbol}_{today}"
    daily[key] = daily.get(key, 0) + 1
    save_daily_trades(daily)

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

        sl = sig["stopLoss"]; tp = sig["takeProfit"]
        entry = sig["entry"]; direction = sig["signal"]
        result = None; pnl = 0.0

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
                f"Entry: ${entry:,.4f} → Close: ${current_price:,.4f}\n"
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
    atr     = calc_atr(candles)
    last    = candles[-1]
    prev    = candles[-2]
    change  = ((last["close"] - prev["close"]) / prev["close"] * 100)

    avg_vol_20 = sum(volumes[-20:]) / 20
    avg_vol_5  = sum(volumes[-5:]) / 5
    last_vol   = volumes[-1]
    vol_ratio  = round(last_vol / avg_vol_20, 2) if avg_vol_20 > 0 else 1.0
    vol_spike  = vol_ratio >= 2.0
    vol_trend  = "INCREASING" if avg_vol_5 > avg_vol_20 else "DECREASING"

    counter_move = ""
    if vol_spike:
        if rsi and rsi > 65:
            counter_move = f"SPIKE ({vol_ratio}x) bei RSI {rsi} – Reversal DOWN möglich"
        elif rsi and rsi < 35:
            counter_move = f"SPIKE ({vol_ratio}x) bei RSI {rsi} – Reversal UP möglich"
        else:
            counter_move = f"SPIKE ({vol_ratio}x) – Reversal möglich"

    all_patterns = detect_patterns(candles)
    top_patterns = TOP_PATTERNS.get(symbol, [])
    confirmed    = [p for p in all_patterns if p in top_patterns]
    sr           = find_support_resistance(candles)
    trend_str    = analyze_trend_structure(candles)
    breakout     = detect_breakout(candles, sr)

    confirmed_str = "\n".join("  • " + p + " ✓ BEWÄHRT" for p in confirmed) if confirmed else "  • Kein bewährtes Muster"
    other_str     = "\n".join("  • " + p for p in all_patterns if p not in top_patterns) if [p for p in all_patterns if p not in top_patterns] else ""
    bb_str        = f"{bb['upper']}/{bb['mid']}/{bb['lower']}" if bb else "N/A"
    atr_str       = f"{atr:.6f}" if atr else "N/A"

    candle_str = "\n".join(
        f"{datetime.fromtimestamp(c['time']/1000).strftime('%H:%M')} "
        f"O:{c['open']} H:{c['high']} L:{c['low']} C:{c['close']} V:{int(c['volume'])}"
        for c in candles[-12:]
    )

    prompt = f"""You are a professional crypto futures trader analyzing {symbol} on {INTERVAL} timeframe.
{perf_context}{results_context}

BACKTEST INSIGHT: Best patterns for {symbol}: {', '.join(top_patterns)}
These patterns historically outperform. Prioritize them in your analysis.

LAST 12 CANDLES:
{candle_str}

INDICATORS:
• RSI(14):  {rsi if rsi is not None else 'N/A'}
• EMA(20):  {ema20 if ema20 else 'N/A'}
• EMA(50):  {ema50 if ema50 else 'N/A'}
• MACD:     {macd if macd else 'N/A'}
• BB:       {bb_str}
• ATR(14):  {atr_str}
• Δ:        {change:.3f}%
• Price:    {last['close']}

VOLUME:
• Ratio:  {vol_ratio}x (min: {MIN_VOL}x)
• Trend:  {vol_trend}
• Spike:  {'YES – ' + counter_move if vol_spike else 'NO'}

CONFIRMED TOP PATTERNS (from backtest):
{confirmed_str}
{('OTHER PATTERNS (lower priority):\n' + other_str) if other_str else ''}

TREND: {trend_str}
S/R: Resistance {sr['resistance']} ({sr['dist_res']}% away) | Support {sr['support']} ({sr['dist_sup']}% away)
Breakout: {breakout if breakout else 'None'}

STRICT RULES:
1. Confirmed top patterns get +2 priority weight
2. NO BUY if EMA20 < EMA50 AND RSI < 45
3. NO SELL if EMA20 > EMA50 AND RSI > 55
4. Volume must be >= {MIN_VOL}x average
5. Confidence >= {MIN_CONF}% only with strong confluence
6. SL = {SL_PCT*100:.1f}% from entry, TP = {TP_PCT*100:.1f}% from entry (1:3 ratio)
7. Be very selective – quality over quantity

Respond ONLY with valid JSON:
{{
  "signal": "BUY" or "SELL" or "HOLD",
  "confidence": integer 0-100,
  "reasoning": "max 200 chars",
  "entry": {last['close']},
  "stopLoss": number,
  "takeProfit": number,
  "risk": "LOW" or "MEDIUM" or "HIGH",
  "trend": "BULLISH" or "BEARISH" or "NEUTRAL",
  "rsi": {rsi if rsi is not None else 50},
  "volumeRatio": {vol_ratio},
  "confirmedPatterns": {json.dumps(confirmed)},
  "allPatterns": {json.dumps(all_patterns)}
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
            "volumeRatio", "confirmedPatterns", "reasoning"
        ])
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "timestamp":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol":           signal_data.get("symbol"),
            "signal":           signal_data.get("signal"),
            "confidence":       signal_data.get("confidence"),
            "entry":            signal_data.get("entry"),
            "stopLoss":         signal_data.get("stopLoss"),
            "takeProfit":       signal_data.get("takeProfit"),
            "risk":             signal_data.get("risk"),
            "trend":            signal_data.get("trend"),
            "rsi":              signal_data.get("rsi"),
            "volumeRatio":      signal_data.get("volumeRatio"),
            "confirmedPatterns":str(signal_data.get("confirmedPatterns", [])),
            "reasoning":        signal_data.get("reasoning"),
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
    return (f"\nHISTORY ({data['total']} signals): "
            f"BUY:{data.get('buy',0)} SELL:{data.get('sell',0)} HOLD:{data.get('hold',0)}")

def get_results_context(symbol):
    results  = load_results()
    sym_data = results.get("by_symbol", {}).get(symbol, {})
    if not sym_data:
        return ""
    wins = sym_data.get("wins", 0); losses = sym_data.get("losses", 0)
    pnl  = sym_data.get("pnl", 0); total  = wins + losses
    wr   = round(wins / total * 100) if total > 0 else 0
    return f"\nRESULTS: {wins}W/{losses}L | WinRate:{wr}% | PnL:{pnl:+.2f}%"

def update_performance(symbol, signal, perf):
    if symbol not in perf:
        perf[symbol] = {"total": 0, "buy": 0, "sell": 0, "hold": 0, "filtered": 0}
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
    wins = results.get("wins", 0); losses = results.get("losses", 0)
    total = wins + losses
    wr  = round(wins / total * 100) if total > 0 else 0
    pnl = results.get("total_pnl", 0)

    msg  = "📊 <b>TÄGLICHE ZUSAMMENFASSUNG v5</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"Gesamt: {wins}W / {losses}L | Win-Rate: {wr}%\n"
    msg += f"Gesamt PnL: {'+' if pnl >= 0 else ''}{pnl:.2f}%\n\n"
    for sym in SYMBOLS:
        d  = perf.get(sym, {})
        rd = results.get("by_symbol", {}).get(sym, {})
        w  = rd.get("wins", 0); l = rd.get("losses", 0); p = rd.get("pnl", 0)
        msg += f"<b>{sym}</b>: {d.get('buy',0)}B/{d.get('sell',0)}S"
        if w + l > 0:
            msg += f" | {w}W/{l}L | {'+' if p >= 0 else ''}{p:.2f}%"
        msg += "\n"
    send_telegram(msg)
    log("Tägliche Zusammenfassung gesendet", "OK")

# ── Signal anzeigen ───────────────────────────────────────────────────────────
def print_signal(symbol, result, price, skip_reason=""):
    sig       = result["signal"]
    conf      = result["confidence"]
    trend     = result["trend"]
    rsi_val   = result.get("rsi", "?")
    vol_ratio = result.get("volumeRatio", 1.0)
    confirmed = result.get("confirmedPatterns", [])
    all_pats  = result.get("allPatterns", [])

    sig_str  = green(f"▲ {sig}") if sig == "BUY" else red(f"▼ {sig}") if sig == "SELL" else yellow(f"● {sig}")
    conf_str = green(f"{conf}%") if conf >= MIN_CONF else yellow(f"{conf}%")

    print()
    print(f"  ┌─ {bold(symbol)} {'─'*30}")
    print(f"  │  Signal:     {sig_str}  Confidence: {conf_str}")
    if skip_reason:
        print(f"  │  {yellow('SKIP: ' + skip_reason)}")
    print(f"  │  Trend:      {trend}  │  RSI: {rsi_val}")
    print(f"  │  Volumen:    {vol_ratio}x (Min: {MIN_VOL}x)")
    for p in confirmed:
        print(f"  │  Muster:     {green(p + ' ✓ TOP')}")
    for p in all_pats:
        if p not in confirmed:
            print(f"  │  Muster:     {gray(p)}")
    print(f"  │  Preis:      ${price:,.4f}")
    if sig != "HOLD" and not skip_reason:
        sl = result.get("stopLoss", 0); tp = result.get("takeProfit", 0)
        print(f"  │  Entry:      ${result.get('entry', price):,.4f}")
        print(f"  │  Stop Loss:  {red(f'${sl:,.4f}')} (-{SL_PCT*100:.1f}%)")
        print(f"  │  Take Profit:{green(f'${tp:,.4f}')} (+{TP_PCT*100:.1f}%)")
        print(f"  │  Ratio:      1:{int(TP_PCT/SL_PCT)}")
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
            print(f"  {sym:<12} {red('FEHLER')}"); continue
        sig     = res["signal"]
        sig_str = green("BUY ") if sig == "BUY" else red("SELL") if sig == "SELL" else yellow("HOLD")
        conf    = res["confidence"]
        top     = res.get("confirmedPatterns", [])
        top_str = green(" ✓" + top[0]) if top else ""
        print(f"  {sym:<12} {sig_str}  {conf}%  {gray(res.get('trend','?'))}{top_str}")
    print(f"  {'─'*50}")

    wins = results.get("wins", 0); losses = results.get("losses", 0)
    total = wins + losses
    if total > 0:
        wr  = round(wins / total * 100)
        pnl = results.get("total_pnl", 0)
        wr_str  = green(f"{wr}%") if wr >= 50 else red(f"{wr}%")
        pnl_str = green(f"+{pnl:.2f}%") if pnl >= 0 else red(f"{pnl:.2f}%")
        print(f"\n  TRACKING: {green(str(wins)+'W')} / {red(str(losses)+'L')} | Win-Rate: {wr_str} | PnL: {pnl_str}")
    print()

# ── Haupt-Bot-Loop ────────────────────────────────────────────────────────────
def run_bot():
    print()
    print(bold(green("╔══════════════════════════════════════════════════════════╗")))
    print(bold(green("║         BITUNIX AI BOT v5 – BACKTEST-OPTIMIERT          ║")))
    print(bold(green("╠══════════════════════════════════════════════════════════╣")))
    print(bold(green(f"║  Symbole:    {' · '.join(SYMBOLS):<43}║")))
    print(bold(green(f"║  BTC:        Entfernt (21.7% Win-Rate zu niedrig)      ║")))
    print(bold(green(f"║  SL/TP:      {SL_PCT*100:.1f}% / {TP_PCT*100:.1f}% (1:3 Ratio)                    ║")))
    print(bold(green(f"║  Min Conf:   {MIN_CONF}% │ Min Vol: {MIN_VOL}x │ Max {MAX_TRADES_DAY} Trades/Tag   ║")))
    print(bold(green("╚══════════════════════════════════════════════════════════╝")))
    print()

    if not ANTHROPIC_API_KEY:
        print(red("FEHLER: ANTHROPIC_API_KEY fehlt in .env"))
        return

    client     = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    perf       = load_performance()
    cycle      = 0
    last_daily = datetime.now().date()

    log("Claude API verbunden ✓", "OK")
    log(f"Symbole: {', '.join(SYMBOLS)} (BTC entfernt)", "INFO")
    log(f"SL: {SL_PCT*100:.1f}% | TP: {TP_PCT*100:.1f}% | Ratio 1:{int(TP_PCT/SL_PCT)}", "INFO")
    log(f"Top-Muster ETH: {', '.join(TOP_PATTERNS['ETHUSDT'])}", "INFO")
    log(f"Top-Muster HBAR: {', '.join(TOP_PATTERNS['HBARUSDT'])}", "INFO")

    if TELEGRAM_TOKEN:
        send_telegram(
            "🚀 <b>Bitunix Bot v5 gestartet</b>\n"
            "Backtest-optimiert:\n"
            "• BTC entfernt\n"
            "• SL 1% / TP 3% (1:3)\n"
            "• Nur Top-Muster\n"
            f"• Min Conf: {MIN_CONF}% | Min Vol: {MIN_VOL}x"
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
                log(f"[{symbol}] Preis: ${price:,.4f}", "OK")
                results = check_open_signals(symbol, price)
            except Exception as e:
                log(f"[{symbol}] Fehler: {e}", "ERROR")
                results_dict[symbol] = None
                time.sleep(3)
                continue

            # Volumen-Filter
            volumes   = [c["volume"] for c in candles]
            avg_vol   = sum(volumes[-20:]) / 20
            last_vol  = volumes[-1]
            vol_ratio = round(last_vol / avg_vol, 2) if avg_vol > 0 else 0

            if vol_ratio < MIN_VOL:
                log(f"[{symbol}] SKIP: Volumen {vol_ratio}x < {MIN_VOL}x", "SKIP")
                results_dict[symbol] = {"signal": "HOLD", "confidence": 0, "trend": "N/A",
                                        "volumeRatio": vol_ratio, "confirmedPatterns": [],
                                        "allPatterns": [], "reasoning": f"Vol {vol_ratio}x zu niedrig"}
                time.sleep(2)
                continue

            # Tages-Limit prüfen
            limit_ok, trade_count = check_daily_limit(symbol)
            if not limit_ok:
                log(f"[{symbol}] SKIP: Tages-Limit erreicht ({trade_count}/{MAX_TRADES_DAY})", "SKIP")
                results_dict[symbol] = {"signal": "HOLD", "confidence": 0, "trend": "N/A",
                                        "volumeRatio": vol_ratio, "confirmedPatterns": [],
                                        "allPatterns": [], "reasoning": "Tages-Limit erreicht"}
                time.sleep(2)
                continue

            log(f"[{symbol}] Claude analysiert... (Trades heute: {trade_count}/{MAX_TRADES_DAY})", "INFO")
            try:
                perf_ctx    = get_perf_context(symbol, perf)
                results_ctx = get_results_context(symbol)
                result      = analyze_with_claude(client, symbol, candles, perf_ctx, results_ctx)
                result["symbol"] = symbol

                # Trend-Filter
                closes = [c["close"] for c in candles]
                ema20  = calc_ema(closes, 20)
                ema50  = calc_ema(closes, 50)
                rsi    = result.get("rsi")
                skip_reason = ""

                if result["signal"] == "BUY" and ema20 and ema50 and ema20 < ema50 and rsi and rsi < 45:
                    skip_reason = "BUY im Downtrend blockiert"
                elif result["signal"] == "SELL" and ema20 and ema50 and ema20 > ema50 and rsi and rsi > 55:
                    skip_reason = "SELL im Uptrend blockiert"

                print_signal(symbol, result, price, skip_reason)

                if not skip_reason:
                    save_signal(result)
                    perf = update_performance(symbol, result["signal"], perf)
                    results_dict[symbol] = result
                    add_open_signal(result)

                    if result["signal"] != "HOLD" and result["confidence"] >= MIN_CONF:
                        increment_daily_trades(symbol)
                        confirmed = result.get("confirmedPatterns", [])
                        emoji = "🟢" if result["signal"] == "BUY" else "🔴"
                        sl = result.get("stopLoss", 0); tp = result.get("takeProfit", 0)
                        msg = (
                            f"{emoji} <b>{result['signal']}: {symbol}</b>\n"
                            f"Preis: ${price:,.4f} | Conf: {result['confidence']}%\n"
                            f"RSI: {rsi} | Vol: {vol_ratio}x\n"
                            f"SL: ${sl:,.4f} (-{SL_PCT*100:.1f}%) | TP: ${tp:,.4f} (+{TP_PCT*100:.1f}%)\n"
                            f"Ratio: 1:{int(TP_PCT/SL_PCT)}\n"
                        )
                        if confirmed:
                            msg += "TOP-Muster: " + ", ".join(confirmed) + "\n"
                        msg += f"📝 {result.get('reasoning','')}"
                        send_telegram(msg)
                else:
                    results_dict[symbol] = {"signal": "HOLD", "confidence": result["confidence"],
                                            "trend": result.get("trend", "N/A"),
                                            "confirmedPatterns": result.get("confirmedPatterns", []),
                                            "allPatterns": result.get("allPatterns", []),
                                            "reasoning": skip_reason}

            except Exception as e:
                log(f"[{symbol}] Analyse-Fehler: {e}", "ERROR")
                results_dict[symbol] = None

            time.sleep(3)

        print_summary(results_dict, results)

        print(bold("  STATISTIK:"))
        for sym in SYMBOLS:
            d = perf.get(sym, {})
            if d.get("total", 0) > 0:
                limit_ok, tc = check_daily_limit(sym)
                print(f"  {sym:<12} "
                      f"BUY:{green(str(d.get('buy',0)))} "
                      f"SELL:{red(str(d.get('sell',0)))} "
                      f"HOLD:{yellow(str(d.get('hold',0)))} "
                      f"Heute:{cyan(str(tc))}/{MAX_TRADES_DAY}")

        today = datetime.now().date()
        if today > last_daily and datetime.now().hour >= 8:
            send_daily_summary(perf)
            last_daily = today

        log(f"Nächste Analyse in {CYCLE_MIN} Minuten.", "INFO")
        try:
            for remaining in range(CYCLE_MIN * 60, 0, -30):
                mins = remaining // 60; secs = remaining % 60
                print(f"\r  {gray(f'Nächste Analyse in: {mins:02d}:{secs:02d}')}  ", end="", flush=True)
                time.sleep(30)
        except KeyboardInterrupt:
            print(); log("Bot gestoppt.", "WARN")
            send_telegram("⛔ <b>Bot gestoppt</b>")
            break

        print(f"\r{' '*50}\r", end="")

if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        print(); log("Bot beendet.", "WARN")
