"""
╔══════════════════════════════════════════════════════════╗
║         Bitunix AI Bot v6 – LSOB + 4h Trend Filter     ║
║         Powered by Claude AI                            ║
║                                                         ║
║  Strategie: Smart Money Concepts (SMC)                  ║
║  - Liquidity Sweep + Order Block                        ║
║  - Break of Structure / CHoCH                          ║
║  - Fair Value Gap                                       ║
║  - 4h Trend-Filter (nur mit übergeordnetem Trend)      ║
║  - Asset-spezifische SL/TP                             ║
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
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")

# Nur ETH und HBAR – BTC entfernt
SYMBOLS   = ["ETHUSDT", "HBARUSDT"]
INTERVAL  = "1h"    # Entry-Timeframe
TF_TREND  = "4h"    # Trend-Timeframe
LIMIT     = 100
CYCLE_MIN = 60      # Jede Stunde analysieren (1h Kerzen)
MIN_VOL   = 0.5
MIN_CONF  = 70
MAX_TRADES_DAY = 2
AUTO_TRADE = False

# Asset-spezifische SL/TP basierend auf Backtest
SL_TP = {
    "ETHUSDT":  {"sl": 0.010, "tp": 0.030},  # 1% SL / 3% TP
    "HBARUSDT": {"sl": 0.020, "tp": 0.050},  # 2% SL / 5% TP
}

BINANCE_BASE = "https://fapi.binance.com"
LOG_FILE     = "signals_v6.csv"
REPORT_FILE  = "performance_v6.json"
OPEN_FILE    = "open_signals_v6.json"
RESULTS_FILE = "results_v6.json"
DAILY_FILE   = "daily_trades_v6.json"

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

# ── Kerzen laden ──────────────────────────────────────────────────────────────
def fetch_candles(symbol, interval, limit=100):
    url    = f"{BINANCE_BASE}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp   = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list) or len(data) == 0:
        raise ValueError(f"Keine Daten für {symbol} {interval}")
    return [
        {"time": int(c[0]), "open": float(c[1]), "high": float(c[2]),
         "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])}
        for c in data
    ]

# ── 4h Trend Filter ───────────────────────────────────────────────────────────
def get_4h_trend(symbol):
    """
    Bestimmt den übergeordneten Trend auf dem 4h Chart.
    Bullisch: EMA20 > EMA50 UND Preis über EMA20
    Bärisch:  EMA20 < EMA50 UND Preis unter EMA20
    """
    try:
        candles = fetch_candles(symbol, TF_TREND, limit=60)
        closes  = [c["close"] for c in candles]
        price   = closes[-1]

        k20 = 2 / (20 + 1)
        k50 = 2 / (50 + 1)
        ema20 = sum(closes[:20]) / 20
        ema50 = sum(closes[:50]) / 50
        for c in closes[20:]:
            ema20 = c * k20 + ema20 * (1 - k20)
        for c in closes[50:]:
            ema50 = c * k50 + ema50 * (1 - k50)

        if ema20 > ema50 and price > ema20:
            return "BULLISH", round(ema20, 6), round(ema50, 6)
        elif ema20 < ema50 and price < ema20:
            return "BEARISH", round(ema20, 6), round(ema50, 6)
        else:
            return "NEUTRAL", round(ema20, 6), round(ema50, 6)
    except Exception as e:
        log(f"4h Trend Fehler: {e}", "WARN")
        return "NEUTRAL", 0, 0

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

# ── Break of Structure ────────────────────────────────────────────────────────
def detect_bos(candles, swing_highs, swing_lows):
    if not swing_highs or not swing_lows:
        return None
    current_close   = candles[-1]["close"]
    last_swing_high = swing_highs[-1]["price"]
    last_swing_low  = swing_lows[-1]["price"]
    if current_close > last_swing_high:
        return {"type": "BULLISH_BOS", "level": last_swing_high}
    elif current_close < last_swing_low:
        return {"type": "BEARISH_BOS", "level": last_swing_low}
    return None

# ── CHoCH ─────────────────────────────────────────────────────────────────────
def detect_choch(candles, swing_highs, swing_lows, lookback=10):
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None
    recent_highs = swing_highs[-3:]
    recent_lows  = swing_lows[-3:]
    lower_highs  = all(recent_highs[i]["price"] > recent_highs[i+1]["price"] for i in range(len(recent_highs)-1))
    lower_lows   = all(recent_lows[i]["price"]  > recent_lows[i+1]["price"]  for i in range(len(recent_lows)-1))
    higher_highs = all(recent_highs[i]["price"] < recent_highs[i+1]["price"] for i in range(len(recent_highs)-1))
    higher_lows  = all(recent_lows[i]["price"]  < recent_lows[i+1]["price"]  for i in range(len(recent_lows)-1))
    price = candles[-1]["close"]
    if lower_highs and lower_lows and price > recent_highs[-1]["price"]:
        return {"type": "BULLISH_CHOCH", "level": recent_highs[-1]["price"]}
    if higher_highs and higher_lows and price < recent_lows[-1]["price"]:
        return {"type": "BEARISH_CHOCH", "level": recent_lows[-1]["price"]}
    return None

# ── Order Blocks ──────────────────────────────────────────────────────────────
def find_order_blocks(candles, lookback=20):
    obs   = []
    start = max(0, len(candles) - lookback)
    for i in range(start, len(candles) - 3):
        next_c = candles[i+1:i+4]
        if len(next_c) < 3:
            continue
        if candles[i]["close"] < candles[i]["open"]:
            moves = [(c["close"] - c["open"]) / c["open"] * 100 for c in next_c]
            if all(m > 0 for m in moves) and sum(moves) > 0.9:
                obs.append({"type": "BULLISH_OB", "high": candles[i]["high"],
                             "low": candles[i]["low"], "idx": i})
        if candles[i]["close"] > candles[i]["open"]:
            moves = [(c["close"] - c["open"]) / c["open"] * 100 for c in next_c]
            if all(m < 0 for m in moves) and sum(moves) < -0.9:
                obs.append({"type": "BEARISH_OB", "high": candles[i]["high"],
                             "low": candles[i]["low"], "idx": i})
    return obs

# ── Fair Value Gap ────────────────────────────────────────────────────────────
def find_fvg(candles, lookback=10):
    fvgs  = []
    start = max(2, len(candles) - lookback)
    for i in range(start, len(candles)):
        c0, c1, c2 = candles[i-2], candles[i-1], candles[i]
        if c2["low"] > c0["high"]:
            fvgs.append({"type": "BULLISH_FVG", "upper": c2["low"], "lower": c0["high"], "idx": i})
        if c2["high"] < c0["low"]:
            fvgs.append({"type": "BEARISH_FVG", "upper": c0["low"], "lower": c2["high"], "idx": i})
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
        return {"type": "BULLISH_SWEEP", "level": last_sl,
                "depth": round((last_sl - prev["low"]) / last_sl * 100, 3)}
    if prev["high"] > last_sh and prev["close"] < last_sh and current["close"] < last_sh:
        return {"type": "BEARISH_SWEEP", "level": last_sh,
                "depth": round((prev["high"] - last_sh) / last_sh * 100, 3)}
    return None

# ── Preis in OB ───────────────────────────────────────────────────────────────
def price_in_ob(price, obs):
    for ob in obs:
        lo = min(ob.get("low", 0), ob.get("high", 0))
        hi = max(ob.get("low", 0), ob.get("high", 0))
        if lo <= price <= hi:
            return ob
    return None

# ── LSOB Signal ───────────────────────────────────────────────────────────────
def analyze_lsob(symbol, candles_1h, trend_4h):
    current_price = candles_1h[-1]["close"]
    volumes       = [c["volume"] for c in candles_1h]

    # Volumen-Filter
    avg_vol   = sum(volumes[-20:]) / 20
    last_vol  = volumes[-1]
    vol_ratio = round(last_vol / avg_vol, 2) if avg_vol > 0 else 0
    if vol_ratio < MIN_VOL:
        return None, f"Volumen zu niedrig ({vol_ratio}x)"

    swing_highs, swing_lows = find_swing_highs_lows(candles_1h)
    bos      = detect_bos(candles_1h, swing_highs, swing_lows)
    choch    = detect_choch(candles_1h, swing_highs, swing_lows)
    obs      = find_order_blocks(candles_1h)
    fvgs     = find_fvg(candles_1h)
    sweep    = detect_liquidity_sweep(candles_1h, swing_highs, swing_lows)
    price_ob = price_in_ob(current_price, obs)

    bull_pts = 0
    bear_pts = 0
    reasons  = []

    # Sweep (stärkstes Signal)
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

    # Order Block
    if price_ob:
        if price_ob["type"] == "BULLISH_OB": bull_pts += 3; reasons.append("IN_BULLISH_OB")
        elif price_ob["type"] == "BEARISH_OB": bear_pts += 3; reasons.append("IN_BEARISH_OB")

    # FVG (letzte 5 Kerzen)
    recent_fvgs = [f for f in fvgs if len(candles_1h) - f["idx"] <= 5]
    for fvg in recent_fvgs:
        if fvg["type"] == "BULLISH_FVG": bull_pts += 2; reasons.append("BULLISH_FVG")
        elif fvg["type"] == "BEARISH_FVG": bear_pts += 2; reasons.append("BEARISH_FVG")

    total = bull_pts + bear_pts
    if total < 6:
        return None, f"Zu wenig Confluence ({total} Punkte)"

    signal     = "HOLD"
    confidence = 0

    if bull_pts > bear_pts and bull_pts >= 6:
        # 4h Trend-Filter: BUY nur in bullischem oder neutralem Trend
        if trend_4h == "BEARISH":
            return None, "BUY blockiert – 4h Trend BEARISH"
        signal     = "BUY"
        confidence = min(int((bull_pts / total) * 100), 99)

    elif bear_pts > bull_pts and bear_pts >= 6:
        # 4h Trend-Filter: SELL nur in bärischem oder neutralem Trend
        if trend_4h == "BULLISH":
            return None, "SELL blockiert – 4h Trend BULLISH"
        signal     = "SELL"
        confidence = min(int((bear_pts / total) * 100), 99)

    if signal == "HOLD" or confidence < MIN_CONF:
        return None, f"Confidence zu niedrig ({confidence}%)"

    # SL/TP berechnen
    sl_pct = SL_TP.get(symbol, {"sl": 0.015, "tp": 0.035})["sl"]
    tp_pct = SL_TP.get(symbol, {"sl": 0.015, "tp": 0.035})["tp"]

    if signal == "BUY":
        sl = round(current_price * (1 - sl_pct), 6)
        tp = round(current_price * (1 + tp_pct), 6)
    else:
        sl = round(current_price * (1 + sl_pct), 6)
        tp = round(current_price * (1 - tp_pct), 6)

    return {
        "signal":     signal,
        "confidence": confidence,
        "price":      current_price,
        "sl":         sl,
        "tp":         tp,
        "sl_pct":     sl_pct,
        "tp_pct":     tp_pct,
        "vol_ratio":  vol_ratio,
        "bull_pts":   bull_pts,
        "bear_pts":   bear_pts,
        "reasons":    reasons,
        "trend_4h":   trend_4h,
        "sweep":      sweep,
        "bos":        bos,
        "choch":      choch,
        "ob":         price_ob,
        "fvgs":       recent_fvgs,
    }, None

# ── Claude Bestätigung ────────────────────────────────────────────────────────
def confirm_with_claude(client, symbol, sig, candles_1h, trend_4h):
    """Claude bestätigt das LSOB Signal mit zusätzlichem Kontext"""
    last = candles_1h[-1]
    candle_str = "\n".join(
        f"{datetime.fromtimestamp(c['time']/1000).strftime('%m-%d %H:%M')} "
        f"O:{c['open']} H:{c['high']} L:{c['low']} C:{c['close']} V:{int(c['volume'])}"
        for c in candles_1h[-8:]
    )

    reasons_str = ", ".join(sig["reasons"])
    prompt = f"""You are an expert SMC/LSOB trader analyzing {symbol}.

4H TREND: {trend_4h}
1H SIGNAL: {sig['signal']} (Confidence: {sig['confidence']}%)

SMC COMPONENTS DETECTED:
{reasons_str}

Points: {sig['bull_pts']} Bull / {sig['bear_pts']} Bear
Volume: {sig['vol_ratio']}x average

LAST 8 CANDLES (1h):
{candle_str}

PROPOSED TRADE:
- Entry: ${sig['price']:,.4f}
- SL: ${sig['sl']:,.4f} (-{sig['sl_pct']*100:.1f}%)
- TP: ${sig['tp']:,.4f} (+{sig['tp_pct']*100:.1f}%)
- Ratio: 1:{int(sig['tp_pct']/sig['sl_pct'])}

As an SMC expert, confirm or reject this signal.
Consider: Is the 4h trend aligned? Are the SMC components strong enough?
Is the entry timing good or should we wait?

Respond ONLY with valid JSON:
{{
  "confirmed": true or false,
  "confidence": integer 0-100,
  "reasoning": "max 150 chars",
  "risk": "LOW" or "MEDIUM" or "HIGH",
  "entry_quality": "GOOD" or "AVERAGE" or "POOR"
}}"""

    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}]
    )
    text  = message.content[0].text.strip()
    start = text.find("{"); end = text.rfind("}") + 1
    return json.loads(text[start:end])

# ── SL/TP Tracking ────────────────────────────────────────────────────────────
def load_open_signals():
    if Path(OPEN_FILE).exists():
        with open(OPEN_FILE) as f: return json.load(f)
    return []

def save_open_signals(s):
    with open(OPEN_FILE, "w") as f: json.dump(s, f, indent=2)

def load_results():
    if Path(RESULTS_FILE).exists():
        with open(RESULTS_FILE) as f: return json.load(f)
    return {"wins": 0, "losses": 0, "total_pnl": 0.0, "by_symbol": {}}

def save_results(r):
    with open(RESULTS_FILE, "w") as f: json.dump(r, f, indent=2)

def check_open_signals(symbol, price):
    open_sigs = load_open_signals()
    results   = load_results()
    updated   = []
    for sig in open_sigs:
        if sig["symbol"] != symbol:
            updated.append(sig); continue
        sl = sig["stopLoss"]; tp = sig["takeProfit"]
        entry = sig["entry"]; direction = sig["signal"]
        result = None; pnl = 0.0
        if direction == "BUY":
            if price >= tp:   result = "WIN";  pnl = round((tp-entry)/entry*100, 3)
            elif price <= sl: result = "LOSS"; pnl = round((sl-entry)/entry*100, 3)
        elif direction == "SELL":
            if price <= tp:   result = "WIN";  pnl = round((entry-tp)/entry*100, 3)
            elif price >= sl: result = "LOSS"; pnl = round((entry-sl)/entry*100, 3)
        if result:
            log(f"[{symbol}] {direction} → {result} | PnL: {'+' if pnl>0 else ''}{pnl}%",
                "WIN" if result=="WIN" else "LOSS")
            if symbol not in results["by_symbol"]:
                results["by_symbol"][symbol] = {"wins":0,"losses":0,"pnl":0.0}
            if result == "WIN": results["wins"]+=1; results["by_symbol"][symbol]["wins"]+=1
            else: results["losses"]+=1; results["by_symbol"][symbol]["losses"]+=1
            results["total_pnl"] = round(results["total_pnl"]+pnl, 3)
            results["by_symbol"][symbol]["pnl"] = round(
                results["by_symbol"][symbol].get("pnl",0)+pnl, 3)
            save_results(results)
            emoji = "✅" if result=="WIN" else "❌"
            send_telegram(f"{emoji} <b>{result}: {symbol} {direction}</b>\n"
                         f"Entry: ${entry:,.4f} → ${price:,.4f}\n"
                         f"PnL: {'+' if pnl>0 else ''}{pnl}%")
        else:
            age_h = (time.time()-sig["openTime"])/3600
            if age_h < 48: updated.append(sig)
    save_open_signals(updated)
    return results

def add_open_signal(sig_data):
    if sig_data.get("signal") in ["BUY","SELL"] and sig_data.get("confidence",0) >= MIN_CONF:
        sigs = load_open_signals()
        sigs.append({
            "symbol":     sig_data["symbol"],
            "signal":     sig_data["signal"],
            "entry":      sig_data["entry"],
            "stopLoss":   sig_data["sl"],
            "takeProfit": sig_data["tp"],
            "confidence": sig_data["confidence"],
            "openTime":   time.time(),
            "openDate":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        save_open_signals(sigs)

# ── Performance ───────────────────────────────────────────────────────────────
def load_performance():
    if Path(REPORT_FILE).exists():
        with open(REPORT_FILE) as f: return json.load(f)
    return {}

def update_performance(symbol, signal, perf):
    if symbol not in perf:
        perf[symbol] = {"total":0,"buy":0,"sell":0,"hold":0,"skipped":0}
    perf[symbol]["total"] += 1
    perf[symbol][signal.lower()] = perf[symbol].get(signal.lower(),0)+1
    with open(REPORT_FILE,"w") as f: json.dump(perf,f,indent=2)
    return perf

# ── Tages-Limit ───────────────────────────────────────────────────────────────
def load_daily():
    if Path(DAILY_FILE).exists():
        with open(DAILY_FILE) as f: return json.load(f)
    return {}

def check_daily_limit(symbol):
    today = datetime.now().strftime("%Y-%m-%d")
    daily = load_daily()
    count = daily.get(f"{symbol}_{today}", 0)
    return count < MAX_TRADES_DAY, count

def increment_daily(symbol):
    today = datetime.now().strftime("%Y-%m-%d")
    daily = load_daily()
    key   = f"{symbol}_{today}"
    daily[key] = daily.get(key,0)+1
    with open(DAILY_FILE,"w") as f: json.dump(daily,f)

# ── Signal speichern ──────────────────────────────────────────────────────────
def save_signal(data):
    exists = Path(LOG_FILE).exists()
    with open(LOG_FILE,"a",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f,fieldnames=[
            "timestamp","symbol","signal","confidence","entry","sl","tp",
            "trend_4h","reasons","risk","vol_ratio","reasoning"])
        if not exists: w.writeheader()
        w.writerow({
            "timestamp":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol":     data.get("symbol"),
            "signal":     data.get("signal"),
            "confidence": data.get("confidence"),
            "entry":      data.get("entry"),
            "sl":         data.get("sl"),
            "tp":         data.get("tp"),
            "trend_4h":   data.get("trend_4h"),
            "reasons":    ",".join(data.get("reasons",[])),
            "risk":       data.get("risk",""),
            "vol_ratio":  data.get("vol_ratio"),
            "reasoning":  data.get("reasoning",""),
        })

# ── Tägliche Zusammenfassung ──────────────────────────────────────────────────
def send_daily_summary(perf):
    if not TELEGRAM_TOKEN: return
    results = load_results()
    wins=results.get("wins",0); losses=results.get("losses",0)
    total=wins+losses; wr=round(wins/total*100) if total>0 else 0
    pnl=results.get("total_pnl",0)
    msg = "📊 <b>TÄGLICHE ZUSAMMENFASSUNG v6</b>\n━━━━━━━━━━━━━━━━\n"
    msg += f"Gesamt: {wins}W/{losses}L | Win-Rate: {wr}%\n"
    msg += f"PnL: {'+' if pnl>=0 else ''}{pnl:.2f}%\n\n"
    for sym in SYMBOLS:
        d  = perf.get(sym,{})
        rd = results.get("by_symbol",{}).get(sym,{})
        w=rd.get("wins",0); l=rd.get("losses",0); p=rd.get("pnl",0)
        msg += f"<b>{sym}</b>: {d.get('buy',0)}B/{d.get('sell',0)}S"
        if w+l>0: msg += f" | {w}W/{l}L | {'+' if p>=0 else ''}{p:.2f}%"
        msg += "\n"
    send_telegram(msg)

# ── Signal ausgeben ───────────────────────────────────────────────────────────
def print_signal(symbol, sig, confirmed=None, skip_reason=""):
    signal    = sig.get("signal","?")
    conf      = sig.get("confidence",0)
    trend_4h  = sig.get("trend_4h","?")
    reasons   = sig.get("reasons",[])
    vol_ratio = sig.get("vol_ratio",0)
    price     = sig.get("price",0)

    sig_str = green(f"▲ {signal}") if signal=="BUY" else red(f"▼ {signal}") if signal=="SELL" else yellow(f"● {signal}")

    print()
    print(f"  ┌─ {bold(symbol)} {'─'*30}")
    print(f"  │  Signal:    {sig_str}  Confidence: {conf}%")
    print(f"  │  4h Trend:  {green(trend_4h) if trend_4h=='BULLISH' else red(trend_4h) if trend_4h=='BEARISH' else yellow(trend_4h)}")
    print(f"  │  Volumen:   {vol_ratio}x")
    for r in reasons:
        print(f"  │  SMC:       {cyan(r)}")
    if skip_reason:
        print(f"  │  {yellow('SKIP: ' + skip_reason)}")
    print(f"  │  Preis:     ${price:,.4f}")
    if signal != "HOLD" and not skip_reason:
        sl_pct = sig.get("sl_pct",0.01)*100
        tp_pct = sig.get("tp_pct",0.03)*100
        ratio  = int(sig.get("tp_pct",0.03)/sig.get("sl_pct",0.01))
        print(f"  │  Entry:     ${sig.get('entry',0):,.4f}")
        sl_val = sig.get("sl", 0)
        tp_val = sig.get("tp", 0)
        print(f"  │  SL:        {red(f'${sl_val:,.4f}')} (-{sl_pct:.1f}%)")
        print(f"  │  TP:        {green(f'${tp_val:,.4f}')} (+{tp_pct:.1f}%)")
        print(f"  │  Ratio:     1:{ratio}")
        if confirmed:
            quality = confirmed.get("entry_quality","?")
            risk    = confirmed.get("risk","?")
            print(f"  │  Claude:    {green('✓ BESTÄTIGT')} | Qualität: {quality} | Risiko: {risk}")
            print(f"  │  Begründung:{gray(confirmed.get('reasoning',''))}")
        elif confirmed is not None:
            print(f"  │  Claude:    {red('✗ ABGELEHNT')} – {gray(confirmed.get('reasoning',''))}")
    print(f"  └{'─'*38}")

# ── Haupt-Bot-Loop ────────────────────────────────────────────────────────────
def run_bot():
    print()
    print(bold(green("╔══════════════════════════════════════════════════════════╗")))
    print(bold(green("║         BITUNIX AI BOT v6 – LSOB + 4h TREND FILTER     ║")))
    print(bold(green("╠══════════════════════════════════════════════════════════╣")))
    print(bold(green(f"║  Symbole:    {' · '.join(SYMBOLS):<43}║")))
    print(bold(green(f"║  Entry:      {INTERVAL} │ Trend: {TF_TREND} │ Zyklus: {CYCLE_MIN}min           ║")))
    print(bold(green(f"║  ETH SL/TP:  1% / 3% │ HBAR SL/TP: 2% / 5%          ║")))
    print(bold(green(f"║  Min Conf:   {MIN_CONF}% │ Min Vol: {MIN_VOL}x │ Max {MAX_TRADES_DAY}/Tag         ║")))
    print(bold(green("╚══════════════════════════════════════════════════════════╝")))
    print()

    if not ANTHROPIC_API_KEY:
        print(red("FEHLER: ANTHROPIC_API_KEY fehlt"))
        return

    client     = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    perf       = load_performance()
    cycle      = 0
    last_daily = datetime.now().date()

    log("Claude API verbunden ✓", "OK")
    log("LSOB Strategie: Liquidity Sweep + OB + BOS/CHoCH + FVG", "INFO")
    log(f"4h Trend-Filter: aktiv", "OK")
    log(f"Asset-spezifische SL/TP: ETH 1%/3% | HBAR 2%/5%", "INFO")

    if TELEGRAM_TOKEN:
        send_telegram(
            "🚀 <b>Bitunix Bot v6 gestartet</b>\n"
            "Strategie: LSOB + 4h Trend-Filter\n"
            "ETH: 1%SL/3%TP | HBAR: 2%SL/5%TP\n"
            f"Symbole: {' · '.join(SYMBOLS)}"
        )

    while True:
        cycle += 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(bold(f"\n{'═'*58}"))
        print(bold(f"  ZYKLUS #{cycle}  │  {now}"))
        print(bold(f"{'═'*58}"))

        results = load_results()

        for symbol in SYMBOLS:
            log(f"[{symbol}] Starte Analyse...", "INFO")

            try:
                # 4h Trend bestimmen
                trend_4h, ema20_4h, ema50_4h = get_4h_trend(symbol)
                log(f"[{symbol}] 4h Trend: {trend_4h} (EMA20:{ema20_4h} EMA50:{ema50_4h})", "INFO")

                # 1h Kerzen laden
                candles_1h = fetch_candles(symbol, INTERVAL, LIMIT)
                price      = candles_1h[-1]["close"]
                log(f"[{symbol}] Preis: ${price:,.4f}", "OK")

                # SL/TP Tracking
                results = check_open_signals(symbol, price)

            except Exception as e:
                log(f"[{symbol}] Datenfehler: {e}", "ERROR")
                time.sleep(3); continue

            # Tages-Limit
            limit_ok, trade_count = check_daily_limit(symbol)
            if not limit_ok:
                log(f"[{symbol}] SKIP: Tages-Limit ({trade_count}/{MAX_TRADES_DAY})", "SKIP")
                time.sleep(2); continue

            # LSOB Analyse
            sig, skip_reason = analyze_lsob(symbol, candles_1h, trend_4h)

            if sig is None:
                log(f"[{symbol}] HOLD: {skip_reason}", "HOLD")
                time.sleep(2); continue

            sig["symbol"]   = symbol
            sig["trend_4h"] = trend_4h

            # Claude Bestätigung
            log(f"[{symbol}] Claude bestätigt Signal...", "INFO")
            try:
                confirmed = confirm_with_claude(client, symbol, sig, candles_1h, trend_4h)
                sig["reasoning"] = confirmed.get("reasoning", "")
                sig["risk"]      = confirmed.get("risk", "MEDIUM")

                if not confirmed.get("confirmed", False):
                    log(f"[{symbol}] Claude lehnt ab: {confirmed.get('reasoning','')}", "SKIP")
                    print_signal(symbol, sig, confirmed=confirmed)
                    time.sleep(2); continue

                # Signal bestätigt!
                print_signal(symbol, sig, confirmed=confirmed)
                save_signal(sig)
                perf = update_performance(symbol, sig["signal"], perf)
                add_open_signal(sig)

                if sig["confidence"] >= MIN_CONF:
                    increment_daily(symbol)
                    emoji = "🟢" if sig["signal"] == "BUY" else "🔴"
                    msg   = (
                        f"{emoji} <b>{sig['signal']}: {symbol}</b>\n"
                        f"Preis: ${price:,.4f}\n"
                        f"4h Trend: {trend_4h}\n"
                        f"SMC: {', '.join(sig['reasons'])}\n"
                        f"Confidence: {sig['confidence']}% | Vol: {sig['vol_ratio']}x\n"
                        f"SL: ${sig['sl']:,.4f} | TP: ${sig['tp']:,.4f}\n"
                        f"Qualität: {confirmed.get('entry_quality','?')} | Risiko: {sig['risk']}\n"
                        f"📝 {sig.get('reasoning','')}"
                    )
                    send_telegram(msg)
                    log(f"[{symbol}] {sig['signal']} Signal bestätigt! Confidence: {sig['confidence']}%",
                        sig["signal"])

            except Exception as e:
                log(f"[{symbol}] Claude Fehler: {e}", "ERROR")

            time.sleep(3)

        # Statistik
        print()
        print(bold("  STATISTIK:"))
        results = load_results()
        wins=results.get("wins",0); losses=results.get("losses",0)
        total=wins+losses
        if total > 0:
            wr  = round(wins/total*100)
            pnl = results.get("total_pnl",0)
            print(f"  Gesamt: {green(str(wins)+'W')} / {red(str(losses)+'L')} | "
                  f"Win-Rate: {green(str(wr)+'%') if wr>=50 else red(str(wr)+'%')} | "
                  f"PnL: {green('+'+str(pnl)+'%') if pnl>=0 else red(str(pnl)+'%')}")
        for sym in SYMBOLS:
            d = perf.get(sym,{})
            if d.get("total",0)>0:
                _, tc = check_daily_limit(sym)
                print(f"  {sym:<12} BUY:{green(str(d.get('buy',0)))} "
                      f"SELL:{red(str(d.get('sell',0)))} "
                      f"Heute:{cyan(str(tc))}/{MAX_TRADES_DAY}")

        # Tägliche Zusammenfassung
        today = datetime.now().date()
        if today > last_daily and datetime.now().hour >= 8:
            send_daily_summary(perf)
            last_daily = today

        log(f"Nächste Analyse in {CYCLE_MIN} Minuten.", "INFO")
        try:
            for remaining in range(CYCLE_MIN*60, 0, -30):
                mins=remaining//60; secs=remaining%60
                print(f"\r  {gray(f'Nächste Analyse in: {mins:02d}:{secs:02d}')}  ", end="", flush=True)
                time.sleep(30)
        except KeyboardInterrupt:
            print(); log("Bot gestoppt.", "WARN")
            send_telegram("⛔ <b>Bot v6 gestoppt</b>")
            break
        print(f"\r{' '*50}\r", end="")

if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        print(); log("Bot beendet.", "WARN")
