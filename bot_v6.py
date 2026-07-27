"""
╔══════════════════════════════════════════════════════════╗
║     Bitunix AI Bot v6 – LSOB + Selbstlernend           ║
║                                                         ║
║  Strategie: Smart Money Concepts (SMC/LSOB)            ║
║  Lernen:    Adaptive Komponenten-Gewichtung             ║
║             Automatische Confidence-Anpassung           ║
║             Wöchentlicher Selbst-Report per Telegram    ║
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

SYMBOLS   = ["ETHUSDT", "HBARUSDT"]
INTERVAL  = "1h"
TF_TREND  = "4h"
LIMIT     = 100
CYCLE_MIN = 60
MIN_VOL   = 0.5
AUTO_TRADE = False
MAX_TRADES_DAY = 2

# LSOB v1 Parameter (beste Backtest-Ergebnisse)
SL_TP = {
    "ETHUSDT":  {"sl": 0.010, "tp": 0.030},
    "HBARUSDT": {"sl": 0.010, "tp": 0.030},
}

# Adaptive Confidence – startet bei 70%, wird automatisch angepasst
BASE_CONF = 70
CONF_FILE = "adaptive_conf.json"

# Komponenten-Gewichte – werden wöchentlich angepasst
WEIGHTS_FILE  = "component_weights.json"
LEARN_FILE    = "learning_data.json"

BINANCE_BASE = "https://fapi.binance.com"
LOG_FILE     = "signals_v6.csv"
REPORT_FILE  = "performance_v6.json"
OPEN_FILE    = "open_signals_v6.json"
RESULTS_FILE = "results_v6.json"
DAILY_FILE   = "daily_trades_v6.json"

# Standard-Gewichte basierend auf Backtest-Ergebnissen
DEFAULT_WEIGHTS = {
    "ETHUSDT": {
        "BULLISH_SWEEP":   1.0,
        "BEARISH_SWEEP":   1.0,
        "BULLISH_BOS":     3.0,
        "BEARISH_BOS":     3.5,  # Backtest: 36.5% – stärker gewichtet
        "BULLISH_CHOCH":   3.0,
        "BEARISH_CHOCH":   3.0,
        "IN_BULLISH_OB":   3.0,
        "IN_BEARISH_OB":   3.0,
        "BULLISH_FVG":     2.0,
        "BEARISH_FVG":     2.5,  # Backtest: 35.8% – stärker gewichtet
    },
    "HBARUSDT": {
        "BULLISH_SWEEP":   1.0,
        "BEARISH_SWEEP":   1.0,
        "BULLISH_BOS":     3.5,  # Backtest: 58.4% – stark gewichtet
        "BEARISH_BOS":     3.0,
        "BULLISH_CHOCH":   3.0,
        "BEARISH_CHOCH":   3.0,
        "IN_BULLISH_OB":   3.0,
        "IN_BEARISH_OB":   3.0,
        "BULLISH_FVG":     3.0,  # Backtest: 55.4% – stark gewichtet
        "BEARISH_FVG":     2.5,
    },
}

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
        "LEARN": cyan("LEARN"),
    }.get(level, "     ")
    print(f"{ts()} {prefix} {msg}")

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": int(TELEGRAM_CHAT_ID), "text": msg,
                                  "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        log(f"Telegram Fehler: {e}", "WARN")

# ── Adaptive Confidence ───────────────────────────────────────────────────────
def load_conf():
    if Path(CONF_FILE).exists():
        with open(CONF_FILE) as f:
            return json.load(f)
    return {"ETHUSDT": BASE_CONF, "HBARUSDT": BASE_CONF}

def save_conf(conf):
    with open(CONF_FILE, "w") as f:
        json.dump(conf, f, indent=2)

def adapt_confidence(symbol, win_rate, current_conf):
    """
    Passt Confidence-Schwelle automatisch an:
    - Win-Rate > 55%: Senke Schwelle (mehr Trades erlauben)
    - Win-Rate < 40%: Erhöhe Schwelle (strenger werden)
    - Win-Rate 40-55%: Keine Änderung
    """
    if win_rate is None:
        return current_conf

    new_conf = current_conf
    if win_rate > 55:
        new_conf = max(60, current_conf - 2)  # Senken, min 60%
    elif win_rate < 40:
        new_conf = min(85, current_conf + 2)  # Erhöhen, max 85%

    if new_conf != current_conf:
        direction = "↓" if new_conf < current_conf else "↑"
        log(f"[{symbol}] Confidence angepasst: {current_conf}% → {new_conf}% {direction} (Win-Rate: {win_rate}%)", "LEARN")

    return new_conf

# ── Komponenten-Gewichte ──────────────────────────────────────────────────────
def load_weights():
    if Path(WEIGHTS_FILE).exists():
        with open(WEIGHTS_FILE) as f:
            return json.load(f)
    return DEFAULT_WEIGHTS.copy()

def save_weights(weights):
    with open(WEIGHTS_FILE, "w") as f:
        json.dump(weights, f, indent=2)

def load_learning_data():
    if Path(LEARN_FILE).exists():
        with open(LEARN_FILE) as f:
            return json.load(f)
    return {}

def save_learning_data(data):
    with open(LEARN_FILE, "w") as f:
        json.dump(data, f, indent=2)

def update_component_stats(symbol, components, result):
    """Trackt Win/Loss pro Komponente für späteres Lernen"""
    data = load_learning_data()
    if symbol not in data:
        data[symbol] = {}
    for comp in components:
        if comp not in data[symbol]:
            data[symbol][comp] = {"wins": 0, "losses": 0, "total": 0}
        data[symbol][comp]["total"] += 1
        if result == "WIN":
            data[symbol][comp]["wins"] += 1
        elif result == "LOSS":
            data[symbol][comp]["losses"] += 1
    save_learning_data(data)

def adapt_weights(symbol, weights):
    """
    Passt Komponenten-Gewichte basierend auf Live-Performance an.
    Komponenten mit Win-Rate > 50% bekommen mehr Gewicht.
    Komponenten mit Win-Rate < 30% bekommen weniger Gewicht.
    Mindestens 10 Trades nötig für Anpassung.
    """
    data = load_learning_data()
    sym_data = data.get(symbol, {})
    if not sym_data:
        return weights, []

    changes = []
    new_weights = weights.copy()
    if symbol not in new_weights:
        new_weights[symbol] = DEFAULT_WEIGHTS.get(symbol, {}).copy()

    for comp, stats in sym_data.items():
        total = stats["total"]
        if total < 10:
            continue  # Zu wenig Daten

        wins    = stats["wins"]
        wr      = round(wins / total * 100, 1)
        old_w   = new_weights[symbol].get(comp, 2.0)
        new_w   = old_w

        if wr > 50:
            new_w = min(5.0, old_w + 0.3)  # Erhöhe Gewicht
        elif wr < 30:
            new_w = max(0.5, old_w - 0.3)  # Senke Gewicht

        new_w = round(new_w, 1)
        if new_w != old_w:
            new_weights[symbol][comp] = new_w
            direction = "↑" if new_w > old_w else "↓"
            changes.append(f"{comp}: {old_w}→{new_w} {direction} (WR:{wr}%)")
            log(f"[{symbol}] Gewicht angepasst: {comp} {old_w}→{new_w} (WR: {wr}%)", "LEARN")

    return new_weights, changes

# ── Wöchentlicher Lernbericht ─────────────────────────────────────────────────
def send_weekly_learning_report(weights, conf):
    if not TELEGRAM_TOKEN:
        return

    data    = load_learning_data()
    results = load_results()

    msg = "🧠 <b>WÖCHENTLICHER LERNBERICHT</b>\n━━━━━━━━━━━━━━━━\n"

    wins  = results.get("wins", 0)
    losses= results.get("losses", 0)
    total = wins + losses
    wr    = round(wins/total*100) if total > 0 else 0
    pnl   = results.get("total_pnl", 0)
    msg += f"Gesamt: {wins}W/{losses}L | WR: {wr}% | PnL: {'+' if pnl>=0 else ''}{pnl:.2f}%\n\n"

    for sym in SYMBOLS:
        msg += f"<b>{sym}</b>\n"
        msg += f"  Confidence-Schwelle: {conf.get(sym, BASE_CONF)}%\n"

        sym_data = data.get(sym, {})
        if sym_data:
            best = sorted(sym_data.items(),
                         key=lambda x: x[1]["wins"]/x[1]["total"] if x[1]["total"]>=5 else 0,
                         reverse=True)[:3]
            msg += "  Beste Komponenten:\n"
            for comp, stats in best:
                if stats["total"] >= 5:
                    comp_wr = round(stats["wins"]/stats["total"]*100, 1)
                    w = weights.get(sym, {}).get(comp, 2.0)
                    msg += f"  • {comp}: {comp_wr}% WR (Gewicht: {w})\n"
        msg += "\n"

    send_telegram(msg)
    log("Wöchentlicher Lernbericht gesendet", "LEARN")

# ── Kerzen laden ──────────────────────────────────────────────────────────────
def fetch_candles(symbol, interval, limit=100):
    url    = f"{BINANCE_BASE}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp   = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list) or len(data) == 0:
        raise ValueError(f"Keine Daten für {symbol}")
    return [{"time": int(c[0]), "open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])} for c in data]

# ── 4h Trend ──────────────────────────────────────────────────────────────────
def get_4h_trend(symbol):
    try:
        candles = fetch_candles(symbol, TF_TREND, limit=60)
        closes  = [c["close"] for c in candles]
        price   = closes[-1]
        k20 = 2/(20+1); k50 = 2/(50+1)
        ema20 = sum(closes[:20])/20; ema50 = sum(closes[:50])/50
        for c in closes[20:]: ema20 = c*k20 + ema20*(1-k20)
        for c in closes[50:]: ema50 = c*k50 + ema50*(1-k50)
        if ema20 > ema50 and price > ema20:   return "BULLISH"
        elif ema20 < ema50 and price < ema20: return "BEARISH"
        else:                                  return "NEUTRAL"
    except Exception as e:
        log(f"4h Trend Fehler: {e}", "WARN"); return "NEUTRAL"

# ── Swing Highs/Lows ──────────────────────────────────────────────────────────
def find_swing_highs_lows(candles, lookback=5):
    swing_highs = []; swing_lows = []
    for i in range(lookback, len(candles)-lookback):
        high = candles[i]["high"]; low = candles[i]["low"]
        if all(candles[i-j]["high"] < high and candles[i+j]["high"] < high for j in range(1, lookback+1)):
            swing_highs.append({"idx": i, "price": high})
        if all(candles[i-j]["low"] > low and candles[i+j]["low"] > low for j in range(1, lookback+1)):
            swing_lows.append({"idx": i, "price": low})
    return swing_highs, swing_lows

# ── SMC Komponenten ───────────────────────────────────────────────────────────
def detect_bos(candles, swing_highs, swing_lows):
    if not swing_highs or not swing_lows: return None
    close = candles[-1]["close"]
    if close > swing_highs[-1]["price"]: return {"type": "BULLISH_BOS"}
    elif close < swing_lows[-1]["price"]: return {"type": "BEARISH_BOS"}
    return None

def detect_choch(candles, swing_highs, swing_lows):
    if len(swing_highs) < 2 or len(swing_lows) < 2: return None
    rh = swing_highs[-3:]; rl = swing_lows[-3:]
    lower_highs = all(rh[i]["price"] > rh[i+1]["price"] for i in range(len(rh)-1))
    lower_lows  = all(rl[i]["price"] > rl[i+1]["price"] for i in range(len(rl)-1))
    higher_highs= all(rh[i]["price"] < rh[i+1]["price"] for i in range(len(rh)-1))
    higher_lows = all(rl[i]["price"] < rl[i+1]["price"] for i in range(len(rl)-1))
    price = candles[-1]["close"]
    if lower_highs and lower_lows and price > rh[-1]["price"]: return {"type": "BULLISH_CHOCH"}
    if higher_highs and higher_lows and price < rl[-1]["price"]: return {"type": "BEARISH_CHOCH"}
    return None

def find_order_blocks(candles, lookback=20):
    obs = []; start = max(0, len(candles)-lookback)
    for i in range(start, len(candles)-3):
        next_c = candles[i+1:i+4]
        if len(next_c) < 3: continue
        if candles[i]["close"] < candles[i]["open"]:
            moves = [(c["close"]-c["open"])/c["open"]*100 for c in next_c]
            if all(m > 0 for m in moves) and sum(moves) > 0.9:
                obs.append({"type": "BULLISH_OB", "high": candles[i]["high"], "low": candles[i]["low"]})
        if candles[i]["close"] > candles[i]["open"]:
            moves = [(c["close"]-c["open"])/c["open"]*100 for c in next_c]
            if all(m < 0 for m in moves) and sum(moves) < -0.9:
                obs.append({"type": "BEARISH_OB", "high": candles[i]["high"], "low": candles[i]["low"]})
    return obs

def find_fvg(candles, lookback=5):
    fvgs = []; start = max(2, len(candles)-lookback)
    for i in range(start, len(candles)):
        c0, c2 = candles[i-2], candles[i]
        if c2["low"] > c0["high"]: fvgs.append({"type": "BULLISH_FVG"})
        if c2["high"] < c0["low"]: fvgs.append({"type": "BEARISH_FVG"})
    return fvgs

def detect_sweep(candles, swing_highs, swing_lows):
    if len(candles) < 2 or not swing_highs or not swing_lows: return None
    current = candles[-1]; prev = candles[-2]
    last_sh = swing_highs[-1]["price"]; last_sl = swing_lows[-1]["price"]
    if prev["low"] < last_sl and prev["close"] > last_sl and current["close"] > last_sl:
        return {"type": "BULLISH_SWEEP"}
    if prev["high"] > last_sh and prev["close"] < last_sh and current["close"] < last_sh:
        return {"type": "BEARISH_SWEEP"}
    return None

def price_in_ob(price, obs):
    for ob in obs:
        if min(ob["low"], ob["high"]) <= price <= max(ob["low"], ob["high"]):
            return ob
    return None

# ── LSOB Analyse mit adaptiven Gewichten ─────────────────────────────────────
def analyze_lsob(symbol, candles_1h, trend_4h, weights, min_conf):
    price   = candles_1h[-1]["close"]
    volumes = [c["volume"] for c in candles_1h]

    avg_vol   = sum(volumes[-20:]) / 20
    vol_ratio = round(volumes[-1] / avg_vol, 2) if avg_vol > 0 else 0
    if vol_ratio < MIN_VOL:
        return None, f"Volumen {vol_ratio}x"

    swing_highs, swing_lows = find_swing_highs_lows(candles_1h)
    bos      = detect_bos(candles_1h, swing_highs, swing_lows)
    choch    = detect_choch(candles_1h, swing_highs, swing_lows)
    obs      = find_order_blocks(candles_1h)
    fvgs     = find_fvg(candles_1h)
    sweep    = detect_sweep(candles_1h, swing_highs, swing_lows)
    price_ob = price_in_ob(price, obs)

    sym_weights = weights.get(symbol, DEFAULT_WEIGHTS.get(symbol, {}))

    bull_pts = 0.0; bear_pts = 0.0; reasons = []

    if sweep:
        w = sym_weights.get(sweep["type"], 1.0)
        if sweep["type"] == "BULLISH_SWEEP": bull_pts += w*4; reasons.append("BULLISH_SWEEP")
        elif sweep["type"] == "BEARISH_SWEEP": bear_pts += w*4; reasons.append("BEARISH_SWEEP")

    if bos:
        w = sym_weights.get(bos["type"], 3.0)
        if bos["type"] == "BULLISH_BOS": bull_pts += w; reasons.append("BULLISH_BOS")
        elif bos["type"] == "BEARISH_BOS": bear_pts += w; reasons.append("BEARISH_BOS")

    if choch:
        w = sym_weights.get(choch["type"], 3.0)
        if choch["type"] == "BULLISH_CHOCH": bull_pts += w; reasons.append("BULLISH_CHOCH")
        elif choch["type"] == "BEARISH_CHOCH": bear_pts += w; reasons.append("BEARISH_CHOCH")

    if price_ob:
        w = sym_weights.get(price_ob["type"].replace("OB", "OB"), 3.0)
        if price_ob["type"] == "BULLISH_OB": bull_pts += w; reasons.append("IN_BULLISH_OB")
        elif price_ob["type"] == "BEARISH_OB": bear_pts += w; reasons.append("IN_BEARISH_OB")

    for fvg in fvgs:
        w = sym_weights.get(fvg["type"], 2.0)
        if fvg["type"] == "BULLISH_FVG": bull_pts += w; reasons.append("BULLISH_FVG")
        elif fvg["type"] == "BEARISH_FVG": bear_pts += w; reasons.append("BEARISH_FVG")

    total = bull_pts + bear_pts
    if total < 6: return None, f"Confluence {total:.1f}/6"

    signal = "HOLD"; confidence = 0
    if bull_pts > bear_pts and bull_pts >= 6:
        if trend_4h == "BEARISH": return None, "BUY blockiert – 4h BEARISH"
        signal = "BUY"; confidence = min(int((bull_pts/total)*100), 99)
    elif bear_pts > bull_pts and bear_pts >= 6:
        if trend_4h == "BULLISH": return None, "SELL blockiert – 4h BULLISH"
        signal = "SELL"; confidence = min(int((bear_pts/total)*100), 99)

    if signal == "HOLD" or confidence < min_conf:
        return None, f"Confidence {confidence}% < {min_conf}%"

    sl_pct = SL_TP.get(symbol, {"sl": 0.010, "tp": 0.030})["sl"]
    tp_pct = SL_TP.get(symbol, {"sl": 0.010, "tp": 0.030})["tp"]

    sl = round(price*(1-sl_pct), 6) if signal=="BUY" else round(price*(1+sl_pct), 6)
    tp = round(price*(1+tp_pct), 6) if signal=="BUY" else round(price*(1-tp_pct), 6)

    return {
        "signal": signal, "confidence": confidence, "price": price,
        "sl": sl, "tp": tp, "sl_pct": sl_pct, "tp_pct": tp_pct,
        "vol_ratio": vol_ratio, "bull_pts": round(bull_pts,1),
        "bear_pts": round(bear_pts,1), "reasons": reasons, "trend_4h": trend_4h,
    }, None

# ── Claude Bestätigung ────────────────────────────────────────────────────────
def confirm_with_claude(client, symbol, sig, candles_1h):
    last       = candles_1h[-1]
    candle_str = "\n".join(
        f"{datetime.fromtimestamp(c['time']/1000).strftime('%m-%d %H:%M')} "
        f"O:{c['open']} H:{c['high']} L:{c['low']} C:{c['close']} V:{int(c['volume'])}"
        for c in candles_1h[-6:]
    )
    prompt = f"""SMC/LSOB trader analyzing {symbol}.

4H TREND: {sig['trend_4h']}
SIGNAL: {sig['signal']} | Confidence: {sig['confidence']}%
COMPONENTS: {', '.join(sig['reasons'])}
Bull/Bear Points: {sig['bull_pts']}/{sig['bear_pts']}
Volume: {sig['vol_ratio']}x

LAST 6 CANDLES (1h):
{candle_str}

TRADE: Entry ${sig['price']:,.4f} | SL ${sig['sl']:,.4f} | TP ${sig['tp']:,.4f}

Confirm or reject. JSON only:
{{"confirmed": true/false, "confidence": 0-100, "reasoning": "max 120 chars", "risk": "LOW/MEDIUM/HIGH", "entry_quality": "GOOD/AVERAGE/POOR"}}"""

    message = client.messages.create(
        model="claude-sonnet-4-5", max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    text  = message.content[0].text.strip()
    start = text.find("{"); end = text.rfind("}")+1
    return json.loads(text[start:end])

# ── SL/TP Tracking ────────────────────────────────────────────────────────────
def load_open_signals():
    if Path(OPEN_FILE).exists():
        with open(OPEN_FILE) as f: return json.load(f)
    return []

def save_open_signals(s):
    with open(OPEN_FILE,"w") as f: json.dump(s,f,indent=2)

def load_results():
    if Path(RESULTS_FILE).exists():
        with open(RESULTS_FILE) as f: return json.load(f)
    return {"wins":0,"losses":0,"total_pnl":0.0,"by_symbol":{}}

def save_results(r):
    with open(RESULTS_FILE,"w") as f: json.dump(r,f,indent=2)

def check_open_signals(symbol, price, weights):
    open_sigs = load_open_signals()
    results   = load_results()
    updated   = []

    for sig in open_sigs:
        if sig["symbol"] != symbol: updated.append(sig); continue
        sl=sig["stopLoss"]; tp=sig["takeProfit"]; entry=sig["entry"]; direction=sig["signal"]
        result=None; pnl=0.0
        if direction=="BUY":
            if price>=tp:   result="WIN";  pnl=round((tp-entry)/entry*100,3)
            elif price<=sl: result="LOSS"; pnl=round((sl-entry)/entry*100,3)
        elif direction=="SELL":
            if price<=tp:   result="WIN";  pnl=round((entry-tp)/entry*100,3)
            elif price>=sl: result="LOSS"; pnl=round((entry-sl)/entry*100,3)

        if (time.time()-sig["openTime"])/3600 > 48 and not result:
            result="EXPIRED"; pnl=round((price-entry)/entry*100,3)
            if direction=="SELL": pnl=-pnl

        if result:
            log(f"[{symbol}] {direction} → {result} | PnL: {'+' if pnl>0 else ''}{pnl}%",
                "WIN" if result=="WIN" else "LOSS")
            if symbol not in results["by_symbol"]:
                results["by_symbol"][symbol]={"wins":0,"losses":0,"pnl":0.0}
            if result=="WIN": results["wins"]+=1; results["by_symbol"][symbol]["wins"]+=1
            else: results["losses"]+=1; results["by_symbol"][symbol]["losses"]+=1
            results["total_pnl"]=round(results["total_pnl"]+pnl,3)
            results["by_symbol"][symbol]["pnl"]=round(
                results["by_symbol"][symbol].get("pnl",0)+pnl,3)
            save_results(results)

            # Lerndata updaten
            components = sig.get("components", [])
            if components:
                update_component_stats(symbol, components, result)

            emoji="✅" if result=="WIN" else "❌"
            send_telegram(f"{emoji} <b>{result}: {symbol} {direction}</b>\n"
                         f"Entry: ${entry:,.4f} → ${price:,.4f}\n"
                         f"PnL: {'+' if pnl>0 else ''}{pnl}%")
        else:
            updated.append(sig)

    save_open_signals(updated)
    return results

def add_open_signal(sig_data):
    if sig_data.get("signal") in ["BUY","SELL"]:
        sigs = load_open_signals()
        sigs.append({
            "symbol":     sig_data["symbol"],
            "signal":     sig_data["signal"],
            "entry":      sig_data["entry"],
            "stopLoss":   sig_data["sl"],
            "takeProfit": sig_data["tp"],
            "confidence": sig_data["confidence"],
            "components": sig_data.get("reasons",[]),
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
        perf[symbol]={"total":0,"buy":0,"sell":0,"hold":0}
    perf[symbol]["total"]+=1
    perf[symbol][signal.lower()]=perf[symbol].get(signal.lower(),0)+1
    with open(REPORT_FILE,"w") as f: json.dump(perf,f,indent=2)
    return perf

def get_symbol_win_rate(symbol):
    results  = load_results()
    sym_data = results.get("by_symbol",{}).get(symbol,{})
    wins     = sym_data.get("wins",0)
    losses   = sym_data.get("losses",0)
    total    = wins+losses
    return round(wins/total*100,1) if total >= 10 else None

# ── Tages-Limit ───────────────────────────────────────────────────────────────
def load_daily():
    if Path(DAILY_FILE).exists():
        with open(DAILY_FILE) as f: return json.load(f)
    return {}

def check_daily_limit(symbol):
    today=datetime.now().strftime("%Y-%m-%d"); daily=load_daily()
    count=daily.get(f"{symbol}_{today}",0)
    return count < MAX_TRADES_DAY, count

def increment_daily(symbol):
    today=datetime.now().strftime("%Y-%m-%d"); daily=load_daily()
    key=f"{symbol}_{today}"; daily[key]=daily.get(key,0)+1
    with open(DAILY_FILE,"w") as f: json.dump(daily,f)

# ── Signal speichern ──────────────────────────────────────────────────────────
def save_signal(data):
    exists=Path(LOG_FILE).exists()
    with open(LOG_FILE,"a",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=["timestamp","symbol","signal","confidence",
            "entry","sl","tp","trend_4h","reasons","risk","vol_ratio","reasoning"])
        if not exists: w.writeheader()
        w.writerow({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol":    data.get("symbol"), "signal": data.get("signal"),
            "confidence":data.get("confidence"), "entry": data.get("entry"),
            "sl":        data.get("sl"), "tp": data.get("tp"),
            "trend_4h":  data.get("trend_4h"),
            "reasons":   ",".join(data.get("reasons",[])),
            "risk":      data.get("risk",""), "vol_ratio": data.get("vol_ratio"),
            "reasoning": data.get("reasoning",""),
        })

# ── Tägliche Zusammenfassung ──────────────────────────────────────────────────
def send_daily_summary(perf, conf):
    if not TELEGRAM_TOKEN: return
    results=load_results()
    wins=results.get("wins",0); losses=results.get("losses",0)
    total=wins+losses; wr=round(wins/total*100) if total>0 else 0
    pnl=results.get("total_pnl",0)
    msg="📊 <b>TÄGLICHE ZUSAMMENFASSUNG v6</b>\n━━━━━━━━━━━━━━━━\n"
    msg+=f"Gesamt: {wins}W/{losses}L | WR: {wr}% | PnL: {'+' if pnl>=0 else ''}{pnl:.2f}%\n\n"
    for sym in SYMBOLS:
        d=perf.get(sym,{}); rd=results.get("by_symbol",{}).get(sym,{})
        w=rd.get("wins",0); l=rd.get("losses",0); p=rd.get("pnl",0)
        sym_wr=round(w/(w+l)*100) if w+l>0 else 0
        msg+=f"<b>{sym}</b>: {d.get('buy',0)}B/{d.get('sell',0)}S | Conf: {conf.get(sym,BASE_CONF)}%"
        if w+l>0: msg+=f" | {w}W/{l}L ({sym_wr}%) | {'+' if p>=0 else ''}{p:.2f}%"
        msg+="\n"
    send_telegram(msg)

# ── Haupt-Bot-Loop ────────────────────────────────────────────────────────────
def run_bot():
    print()
    print(bold(green("╔══════════════════════════════════════════════════════════╗")))
    print(bold(green("║     BITUNIX AI BOT v6 – LSOB + SELBSTLERNEND           ║")))
    print(bold(green("╠══════════════════════════════════════════════════════════╣")))
    print(bold(green(f"║  Symbole:    {' · '.join(SYMBOLS):<43}║")))
    print(bold(green(f"║  Entry: {INTERVAL} │ Trend: {TF_TREND} │ SL 1% / TP 3%              ║")))
    print(bold(green(f"║  Lernen: Adaptive Confidence + Komponenten-Gewichte    ║")))
    print(bold(green(f"║  Report: Wöchentlich per Telegram                      ║")))
    print(bold(green("╚══════════════════════════════════════════════════════════╝")))
    print()

    if not ANTHROPIC_API_KEY:
        print(red("FEHLER: ANTHROPIC_API_KEY fehlt")); return

    client     = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    perf       = load_performance()
    weights    = load_weights()
    conf       = load_conf()
    cycle      = 0
    last_daily = datetime.now().date()
    last_learn = datetime.now()
    last_weekly= datetime.now()

    log("Claude API verbunden ✓", "OK")
    log("LSOB Strategie aktiv", "OK")
    log("Selbstlern-System aktiv", "LEARN")
    for sym in SYMBOLS:
        log(f"[{sym}] Confidence-Schwelle: {conf.get(sym, BASE_CONF)}%", "LEARN")

    if TELEGRAM_TOKEN:
        send_telegram(
            "🚀 <b>Bitunix Bot v6 gestartet</b>\n"
            "LSOB + Selbstlernend\n"
            "Adaptive Confidence + Gewichte\n"
            f"ETH Conf: {conf.get('ETHUSDT', BASE_CONF)}% | HBAR Conf: {conf.get('HBARUSDT', BASE_CONF)}%"
        )

    while True:
        cycle += 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(bold(f"\n{'═'*58}"))
        print(bold(f"  ZYKLUS #{cycle}  │  {now}"))
        print(bold(f"{'═'*58}"))

        results = load_results()

        for symbol in SYMBOLS:
            log(f"[{symbol}] Analyse... (Conf: {conf.get(symbol, BASE_CONF)}%)", "INFO")

            try:
                trend_4h   = get_4h_trend(symbol)
                candles_1h = fetch_candles(symbol, INTERVAL, LIMIT)
                price      = candles_1h[-1]["close"]
                log(f"[{symbol}] Preis: ${price:,.4f} | 4h: {trend_4h}", "OK")
                results = check_open_signals(symbol, price, weights)
            except Exception as e:
                log(f"[{symbol}] Fehler: {e}", "ERROR"); time.sleep(3); continue

            limit_ok, trade_count = check_daily_limit(symbol)
            if not limit_ok:
                log(f"[{symbol}] Tages-Limit ({trade_count}/{MAX_TRADES_DAY})", "SKIP")
                time.sleep(2); continue

            sig, skip_reason = analyze_lsob(symbol, candles_1h, trend_4h,
                                            weights, conf.get(symbol, BASE_CONF))

            if sig is None:
                log(f"[{symbol}] HOLD: {skip_reason}", "HOLD")
                time.sleep(2); continue

            sig["symbol"]   = symbol
            sig["trend_4h"] = trend_4h

            log(f"[{symbol}] Signal: {sig['signal']} {sig['confidence']}% | {', '.join(sig['reasons'])}", sig["signal"])

            try:
                confirmed = confirm_with_claude(client, symbol, sig, candles_1h)
                sig["reasoning"] = confirmed.get("reasoning","")
                sig["risk"]      = confirmed.get("risk","MEDIUM")

                print()
                print(f"  ┌─ {bold(symbol)} {'─'*30}")
                print(f"  │  Signal:   {green('▲ '+sig['signal']) if sig['signal']=='BUY' else red('▼ '+sig['signal'])}  Conf: {sig['confidence']}%")
                print(f"  │  4h Trend: {trend_4h}")
                print(f"  │  Vol:      {sig['vol_ratio']}x")
                for r in sig["reasons"]: print(f"  │  SMC:      {cyan(r)}")
                print(f"  │  Preis:    ${price:,.4f}")
                sl_val = sig['sl']; tp_val = sig['tp']
                print(f"  │  SL:       {red(f'${sl_val:,.4f}')} | TP: {green(f'${tp_val:,.4f}')}")
                claude_ok = confirmed.get("confirmed", False)
                print(f"  │  Claude:   {green('✓ BESTÄTIGT') if claude_ok else red('✗ ABGELEHNT')} | {gray(sig['reasoning'])}")
                print(f"  └{'─'*38}")

                if not claude_ok:
                    log(f"[{symbol}] Claude lehnt ab", "SKIP")
                    time.sleep(2); continue

                save_signal(sig)
                perf    = update_performance(symbol, sig["signal"], perf)
                add_open_signal(sig)
                increment_daily(symbol)

                emoji = "🟢" if sig["signal"]=="BUY" else "🔴"
                send_telegram(
                    f"{emoji} <b>{sig['signal']}: {symbol}</b>\n"
                    f"Preis: ${price:,.4f} | Conf: {sig['confidence']}%\n"
                    f"4h: {trend_4h} | Vol: {sig['vol_ratio']}x\n"
                    f"SMC: {', '.join(sig['reasons'])}\n"
                    f"SL: ${sl_val:,.4f} | TP: ${tp_val:,.4f}\n"
                    f"📝 {sig.get('reasoning','')}"
                )

            except Exception as e:
                log(f"[{symbol}] Claude Fehler: {e}", "ERROR")

            time.sleep(3)

        # ── Lernzyklus (alle 24h) ──────────────────────────────────────────
        if (datetime.now() - last_learn).total_seconds() > 86400:
            log("Starte täglichen Lernzyklus...", "LEARN")
            for sym in SYMBOLS:
                wr = get_symbol_win_rate(sym)
                if wr is not None:
                    old_conf = conf.get(sym, BASE_CONF)
                    new_conf = adapt_confidence(sym, wr, old_conf)
                    conf[sym] = new_conf
            save_conf(conf)

            new_weights, all_changes = {}, []
            for sym in SYMBOLS:
                nw, changes = adapt_weights(sym, weights)
                new_weights.update(nw)
                all_changes.extend([f"[{sym}] {c}" for c in changes])
            weights = new_weights
            save_weights(weights)

            if all_changes:
                log(f"Gewichte angepasst: {len(all_changes)} Änderungen", "LEARN")
                for change in all_changes:
                    log(change, "LEARN")

            last_learn = datetime.now()

        # ── Wöchentlicher Report ───────────────────────────────────────────
        if (datetime.now() - last_weekly).total_seconds() > 604800:
            send_weekly_learning_report(weights, conf)
            last_weekly = datetime.now()

        # ── Statistik ──────────────────────────────────────────────────────
        print()
        print(bold("  STATISTIK:"))
        results = load_results()
        wins=results.get("wins",0); losses=results.get("losses",0)
        total=wins+losses
        if total > 0:
            wr=round(wins/total*100); pnl=results.get("total_pnl",0)
            print(f"  Gesamt: {green(str(wins)+'W')} / {red(str(losses)+'L')} | "
                  f"WR: {green(str(wr)+'%') if wr>=50 else red(str(wr)+'%')} | "
                  f"PnL: {green('+'+str(pnl)+'%') if pnl>=0 else red(str(pnl)+'%')}")
        for sym in SYMBOLS:
            d=perf.get(sym,{})
            if d.get("total",0)>0:
                _, tc = check_daily_limit(sym)
                sym_wr = get_symbol_win_rate(sym)
                wr_str = f" | WR: {sym_wr}%" if sym_wr else ""
                print(f"  {sym:<12} BUY:{green(str(d.get('buy',0)))} "
                      f"SELL:{red(str(d.get('sell',0)))} "
                      f"Heute:{cyan(str(tc))}/{MAX_TRADES_DAY}"
                      f" | Conf:{yellow(str(conf.get(sym,BASE_CONF))+'%')}{wr_str}")

        today = datetime.now().date()
        if today > last_daily and datetime.now().hour >= 8:
            send_daily_summary(perf, conf)
            last_daily = today

        log(f"Nächste Analyse in {CYCLE_MIN} Minuten.", "INFO")
        try:
            for remaining in range(CYCLE_MIN*60, 0, -30):
                mins=remaining//60; secs=remaining%60
                print(f"\r  {gray(f'Nächste Analyse in: {mins:02d}:{secs:02d}')}  ", end="", flush=True)
                time.sleep(30)
        except KeyboardInterrupt:
            print(); log("Bot gestoppt.", "WARN")
            send_telegram("⛔ <b>Bot v6 gestoppt</b>"); break
        print(f"\r{' '*50}\r", end="")

if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        print(); log("Bot beendet.", "WARN")
