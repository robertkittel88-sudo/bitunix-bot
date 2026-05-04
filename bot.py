"""
╔══════════════════════════════════════════════════════════╗
║         Bitunix Multi-Asset AI Trading Bot               ║
║         Powered by Claude AI                             ║
║                                                          ║
║  Symbole:  BTC/USDT · ETH/USDT · HBAR/USDT             ║
║  Start:    py bot.py                                     ║
╚══════════════════════════════════════════════════════════╝

Benötigt:
  - Anthropic API Key (https://console.anthropic.com)
  - Optional: Bitunix API Key (nur für echtes Trading)
"""

import json
import math
import os
import time
import csv
from datetime import datetime
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv()

# ── Konfiguration ─────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
BITUNIX_API_KEY    = os.getenv("BITUNIX_API_KEY", "")
BITUNIX_SECRET_KEY = os.getenv("BITUNIX_SECRET_KEY", "")

SYMBOLS    = ["BTCUSDT", "ETHUSDT", "HBARUSDT"]
INTERVAL   = "15m"          # Kerzen-Zeitrahmen
LIMIT      = 60             # Anzahl Kerzen
CYCLE_MIN  = 15             # Minuten zwischen Analysen
MIN_CONF   = 65             # Minimale Confidence für Signal-Logging
AUTO_TRADE = False          # True = echte Orders platzieren (Vorsicht!)

BINANCE_BASE = "https://fapi.binance.com"
LOG_FILE     = "signals.csv"
REPORT_FILE  = "performance.json"

# ── Farben für Konsole ────────────────────────────────────────────────────────
class C:
    GREEN  = "\033[92m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    ORANGE = "\033[33m"
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

def ts():
    return gray(f"[{datetime.now().strftime('%H:%M:%S')}]")

def log(msg, level="INFO"):
    prefix = {
        "INFO":  blue("INFO "),
        "OK":    green("OK   "),
        "WARN":  yellow("WARN "),
        "ERROR": red("ERROR"),
        "BUY":   green("BUY  "),
        "SELL":  red("SELL "),
        "HOLD":  yellow("HOLD "),
    }.get(level, "     ")
    print(f"{ts()} {prefix} {msg}")

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

# ── Kerzen laden ──────────────────────────────────────────────────────────────
def fetch_candles(symbol):
    url = f"{BINANCE_BASE}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": INTERVAL, "limit": LIMIT}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list) or len(data) == 0:
        raise ValueError(f"Keine Kerzendaten für {symbol}")
    # Binance Format: [openTime, open, high, low, close, volume, ...]
    return [
        {"time": int(c[0]), "open": float(c[1]), "high": float(c[2]),
         "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])}
        for c in data
    ]

# ── Claude Analyse ────────────────────────────────────────────────────────────
def analyze_with_claude(client, symbol, candles, perf_context=""):
    closes  = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]
    rsi    = calc_rsi(closes)
    ema20  = calc_ema(closes, 20)
    ema50  = calc_ema(closes, 50)
    macd   = calc_macd(closes)
    bb     = calc_bollinger(closes)
    last   = candles[-1]
    prev   = candles[-2]
    change = ((last["close"] - prev["close"]) / prev["close"] * 100)

    # ── Volumen-Analyse ──────────────────────────────────────────────────────
    avg_vol_20    = sum(volumes[-20:]) / 20
    avg_vol_5     = sum(volumes[-5:]) / 5
    last_vol      = volumes[-1]
    vol_ratio     = round(last_vol / avg_vol_20, 2) if avg_vol_20 > 0 else 1.0
    vol_spike     = vol_ratio >= 2.0   # Volumen 2x über Durchschnitt = Spike
    vol_trend     = "INCREASING" if avg_vol_5 > avg_vol_20 else "DECREASING"

    # Gegenbewegungswarnung: hohes Volumen + Preis an Extrempunkt
    counter_move_warning = ""
    if vol_spike:
        if rsi and rsi > 65:
            counter_move_warning = f"⚠ HIGH VOLUME SPIKE ({vol_ratio}x avg) at RSI {rsi} – possible exhaustion, reversal DOWN likely"
        elif rsi and rsi < 35:
            counter_move_warning = f"⚠ HIGH VOLUME SPIKE ({vol_ratio}x avg) at RSI {rsi} – possible exhaustion, reversal UP likely"
        else:
            counter_move_warning = f"⚠ HIGH VOLUME SPIKE ({vol_ratio}x avg) – watch for reversal"

    candle_str = "\n".join(
        f"{datetime.fromtimestamp(c['time']/1000).strftime('%H:%M')} "
        f"O:{c['open']} H:{c['high']} L:{c['low']} C:{c['close']} V:{int(c['volume'])}"
        for c in candles[-12:]
    )

    bb_str = f"{bb['upper']}/{bb['mid']}/{bb['lower']}" if bb else "N/A"

    prompt = f"""You are a professional crypto futures trader analyzing {symbol} on {INTERVAL} timeframe.
{perf_context}
LAST 12 CANDLES:
{candle_str}

INDICATORS:
• RSI(14):            {rsi if rsi is not None else 'N/A'}
• EMA(20):            {ema20 if ema20 else 'N/A'}
• EMA(50):            {ema50 if ema50 else 'N/A'}
• MACD(12,26):        {macd if macd else 'N/A'}
• BB Upper/Mid/Lower: {bb_str}
• Last candle Δ:      {change:.3f}%
• Current price:      {last['close']}

VOLUME ANALYSIS:
• Last volume:        {int(last_vol)}
• Avg volume (20):    {int(avg_vol_20)}
• Volume ratio:       {vol_ratio}x (last vs 20-period avg)
• Volume trend:       {vol_trend}
• Volume spike:       {'YES – ' + counter_move_warning if vol_spike else 'NO'}

IMPORTANT VOLUME RULES:
- A volume spike (2x+ average) at RSI extremes (>65 or <35) often signals exhaustion and counter-move
- High volume at resistance = likely reversal DOWN
- High volume at support = likely reversal UP  
- Do NOT enter in the direction of a spike at extremes – wait for confirmation
- Decreasing volume on a trend = trend weakening

Be conservative. Only signal BUY/SELL with clear multi-indicator confluence including volume confirmation.
Set SL 1-2% away from entry, TP 2-4% away.

Respond ONLY with valid JSON, no markdown, no extra text:
{{
  "signal": "BUY" or "SELL" or "HOLD",
  "confidence": integer 0-100,
  "reasoning": "brief explanation max 150 chars",
  "entry": {last['close']},
  "stopLoss": number,
  "takeProfit": number,
  "risk": "LOW" or "MEDIUM" or "HIGH",
  "trend": "BULLISH" or "BEARISH" or "NEUTRAL",
  "rsi": {rsi if rsi is not None else 50},
  "volumeRatio": {vol_ratio},
  "volumeSpike": {"true" if vol_spike else "false"}
}}"""

    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )
    text = message.content[0].text.strip()
    # JSON extrahieren
    start = text.find("{")
    end   = text.rfind("}") + 1
    return json.loads(text[start:end])

# ── Signal speichern ──────────────────────────────────────────────────────────
def save_signal(signal_data):
    file_exists = Path(LOG_FILE).exists()
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "timestamp", "symbol", "signal", "confidence", "entry",
            "stopLoss", "takeProfit", "risk", "trend", "rsi", "reasoning"
        ])
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "timestamp":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol":     signal_data.get("symbol"),
            "signal":     signal_data.get("signal"),
            "confidence": signal_data.get("confidence"),
            "entry":      signal_data.get("entry"),
            "stopLoss":   signal_data.get("stopLoss"),
            "takeProfit": signal_data.get("takeProfit"),
            "risk":       signal_data.get("risk"),
            "trend":      signal_data.get("trend"),
            "rsi":        signal_data.get("rsi"),
            "reasoning":  signal_data.get("reasoning"),
        })

# ── Performance laden ─────────────────────────────────────────────────────────
def load_performance():
    if Path(REPORT_FILE).exists():
        with open(REPORT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def get_perf_context(symbol, perf):
    data = perf.get(symbol, {})
    if not data or data.get("total", 0) == 0:
        return ""
    return (f"\nHISTORY ({data['total']} signals): "
            f"BUY: {data.get('buy',0)}x | SELL: {data.get('sell',0)}x | HOLD: {data.get('hold',0)}x")

def update_performance(symbol, signal, perf):
    if symbol not in perf:
        perf[symbol] = {"total": 0, "buy": 0, "sell": 0, "hold": 0}
    perf[symbol]["total"] += 1
    perf[symbol][signal.lower()] = perf[symbol].get(signal.lower(), 0) + 1
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(perf, f, indent=2)
    return perf

# ── Signal anzeigen ───────────────────────────────────────────────────────────
def print_signal(symbol, result, price):
    sig = result["signal"]
    conf = result["confidence"]
    trend = result["trend"]
    rsi_val = result.get("rsi", "?")

    sig_str = green(f"▲ {sig}") if sig == "BUY" else red(f"▼ {sig}") if sig == "SELL" else yellow(f"● {sig}")
    conf_str = green(f"{conf}%") if conf >= MIN_CONF else yellow(f"{conf}%")

    print()
    print(f"  ┌─ {bold(symbol)} {'─'*30}")
    print(f"  │  Signal:     {sig_str}  Confidence: {conf_str}")
    print(f"  │  Trend:      {trend}  │  RSI: {rsi_val}")
    vol_ratio = result.get('volumeRatio', 1.0)
    vol_spike = result.get('volumeSpike', False)
    vol_str = f"{vol_ratio}x {'⚠ SPIKE!' if vol_spike else ''}"
    print(f"  │  Volumen:    {vol_str}")
    print(f"  │  Preis:      ${price:,.4f}")
    if sig != "HOLD":
        print(f"  │  Entry:      ${result.get('entry', price):,.4f}")
        sl = result.get('stopLoss', 0)
        tp = result.get('takeProfit', 0)
        print(f"  │  Stop Loss:  {red(f'${sl:,.4f}')}")
        print(f"  │  Take Profit:{green(f'${tp:,.4f}')}")
        print(f"  │  Risiko:     {result.get('risk', '?')}")
    print(f"  │  Begründung: {gray(result.get('reasoning', ''))}")
    print(f"  └{'─'*38}")

# ── Zusammenfassung ───────────────────────────────────────────────────────────
def print_summary(results):
    print()
    print(bold(f"  {'─'*50}"))
    print(bold(f"  ZYKLUS ZUSAMMENFASSUNG"))
    print(f"  {'─'*50}")
    for sym, res in results.items():
        if res is None:
            print(f"  {sym:<12} {red('FEHLER')}")
            continue
        sig = res["signal"]
        sig_str = green("BUY ") if sig == "BUY" else red("SELL") if sig == "SELL" else yellow("HOLD")
        conf = res["confidence"]
        print(f"  {sym:<12} {sig_str}  {conf}%  {gray(res.get('trend','?'))}")
    print(f"  {'─'*50}")
    print()

# ── Haupt-Bot-Loop ────────────────────────────────────────────────────────────
def run_bot():
    print()
    print(bold(green("╔══════════════════════════════════════════════════════════╗")))
    print(bold(green("║         BITUNIX MULTI-ASSET AI BOT                      ║")))
    print(bold(green("║         Powered by Claude AI                             ║")))
    print(bold(green("╠══════════════════════════════════════════════════════════╣")))
    print(bold(green(f"║  Symbole:  {' · '.join(SYMBOLS):<44}║")))
    print(bold(green(f"║  Interval: {INTERVAL:<7}  Zyklus: alle {CYCLE_MIN} Minuten          ║")))
    print(bold(green(f"║  Auto-Trade: {'AN (VORSICHT!)' if AUTO_TRADE else 'AUS (nur Analyse)'}{'':>32}║")))
    print(bold(green("╚══════════════════════════════════════════════════════════╝")))
    print()

    # API-Key prüfen
    if not ANTHROPIC_API_KEY:
        print(red("FEHLER: ANTHROPIC_API_KEY fehlt in .env!"))
        print(f"Hole dir einen Key unter: {cyan('https://console.anthropic.com')}")
        print(f"Dann in .env eintragen:   {cyan('ANTHROPIC_API_KEY=sk-ant-...')}")
        return

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    perf   = load_performance()
    cycle  = 0

    log(f"Claude API verbunden ✓", "OK")
    log(f"Signale werden gespeichert in: {cyan(LOG_FILE)}", "INFO")
    log(f"Starte erste Analyse...", "INFO")
    print()

    while True:
        cycle += 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(bold(f"\n{'═'*58}"))
        print(bold(f"  ZYKLUS #{cycle}  │  {now}"))
        print(bold(f"{'═'*58}"))

        results = {}

        for symbol in SYMBOLS:
            log(f"[{symbol}] Lade Kerzen von Bitunix...", "INFO")
            try:
                candles = fetch_candles(symbol)
                price   = candles[-1]["close"]
                log(f"[{symbol}] Preis: ${price:,.4f}  │  {len(candles)} Kerzen geladen", "OK")
            except Exception as e:
                log(f"[{symbol}] Kerzen-Fehler: {e}", "ERROR")
                results[symbol] = None
                time.sleep(3)
                continue

            log(f"[{symbol}] Claude analysiert...", "INFO")
            try:
                perf_ctx = get_perf_context(symbol, perf)
                result   = analyze_with_claude(client, symbol, candles, perf_ctx)
                result["symbol"] = symbol

                print_signal(symbol, result, price)
                save_signal(result)
                perf = update_performance(symbol, result["signal"], perf)
                results[symbol] = result

                if result["signal"] != "HOLD":
                    log(f"[{symbol}] Signal gespeichert in {LOG_FILE}", "OK")

            except Exception as e:
                log(f"[{symbol}] Analyse-Fehler: {e}", "ERROR")
                results[symbol] = None

            # Kurze Pause zwischen Symbolen
            time.sleep(3)

        # Zusammenfassung
        print_summary(results)

        # Statistik anzeigen
        print(bold("  GESAMTE STATISTIK:"))
        for sym in SYMBOLS:
            d = perf.get(sym, {})
            if d.get("total", 0) > 0:
                print(f"  {sym:<12} Gesamt: {d['total']:>3}  "
                      f"BUY: {green(str(d.get('buy',0))):>3}  "
                      f"SELL: {red(str(d.get('sell',0))):>3}  "
                      f"HOLD: {yellow(str(d.get('hold',0))):>3}")

        # Countdown bis nächster Zyklus
        print()
        log(f"Nächste Analyse in {CYCLE_MIN} Minuten. STRG+C zum Beenden.", "INFO")
        try:
            for remaining in range(CYCLE_MIN * 60, 0, -30):
                mins = remaining // 60
                secs = remaining % 60
                print(f"\r  {gray(f'Nächste Analyse in: {mins:02d}:{secs:02d}')}  ", end="", flush=True)
                time.sleep(30)
        except KeyboardInterrupt:
            print()
            print()
            log("Bot gestoppt.", "WARN")
            print(f"\n  Alle Signals gespeichert in: {cyan(LOG_FILE)}")
            break

        print(f"\r{' '*50}\r", end="")

if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        print()
        log("Bot beendet.", "WARN")
