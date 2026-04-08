import os
import time
import requests
import hmac
import hashlib
from datetime import datetime

# ── Config ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8648873561:AAG07h-OOTh7PuH_EXtiAt0oxiBvIqbHLpI")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "5247767867")
SCAN_INTERVAL      = 300  # scan every 5 minutes

COINS = [
    {"id": "xrp", "gecko_id": "ripple",  "symbol": "XRP/USDT",  "decimals": 4},
    {"id": "sui", "gecko_id": "sui",     "symbol": "SUI/USDT",  "decimals": 4},
]

# Track last signal sent to avoid spam
last_signal = {"xrp": None, "sui": None}

# ── Telegram ────────────────────────────────────────────────────────────────
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        return res.json()
    except Exception as e:
        print(f"Telegram error: {e}")
        return None

# ── Fetch prices from CoinGecko ─────────────────────────────────────────────
def fetch_prices():
    ids  = ",".join([c["gecko_id"] for c in COINS])
    url  = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true&include_24hr_high=true&include_24hr_low=true&include_24hr_vol=true"
    try:
        res  = requests.get(url, timeout=10)
        data = res.json()
        prices = {}
        for coin in COINS:
            d = data.get(coin["gecko_id"], {})
            if d:
                prices[coin["id"]] = {
                    "price":  d.get("usd", 0),
                    "change": d.get("usd_24h_change", 0),
                    "high":   d.get("usd_24h_high", 0),
                    "low":    d.get("usd_24h_low", 0),
                    "vol":    d.get("usd_24h_vol", 0),
                }
        return prices
    except Exception as e:
        print(f"Price fetch error: {e}")
        return {}

# ── Signal engine ───────────────────────────────────────────────────────────
def compute_signal(data):
    price  = data["price"]
    change = data["change"]
    high   = data["high"] or price * 1.02
    low    = data["low"]  or price * 0.98

    range_ = high - low
    pos    = (price - low) / range_ if range_ > 0 else 0.5

    score = 0
    if   change >  4: score += 4
    elif change >  2: score += 3
    elif change >  0: score += 1
    elif change < -4: score -= 4
    elif change < -2: score -= 3
    else:             score -= 1

    if   pos < 0.25: score += 2   # near daily low  = potential bounce
    elif pos > 0.80: score -= 1   # near daily high = possible resistance

    # Levels
    if score >= 2:
        signal     = "BUY"
        entry      = price
        take_profit = round(price * 1.025, 6)
        stop_loss   = round(price * 0.985, 6)
        confidence  = min(85, 55 + score * 5)
    elif score <= -2:
        signal     = "SELL"
        entry      = price
        take_profit = round(price * 0.975, 6)
        stop_loss   = round(price * 1.015, 6)
        confidence  = min(80, 55 + abs(score) * 5)
    else:
        signal     = "HOLD"
        entry      = price
        take_profit = round(price * 1.02, 6)
        stop_loss   = round(price * 0.98, 6)
        confidence  = 50

    return {
        "signal":      signal,
        "entry":       entry,
        "take_profit": take_profit,
        "stop_loss":   stop_loss,
        "confidence":  confidence,
        "score":       score,
        "change":      change,
    }

# ── Format Telegram message ─────────────────────────────────────────────────
def format_message(coin, price_data, sig):
    now   = datetime.utcnow().strftime("%d %b %Y %H:%M UTC")
    dec   = coin["decimals"]
    sym   = coin["symbol"]
    chg   = price_data["change"]
    chg_s = f"+{chg:.2f}%" if chg >= 0 else f"{chg:.2f}%"

    if sig["signal"] == "BUY":
        emoji  = "🟢"
        action = "BUY — Place a LONG trade"
        pionex = "➡️ On Pionex: Buy Market or Limit order"
    elif sig["signal"] == "SELL":
        emoji  = "🔴"
        action = "SELL — Consider closing or avoiding"
        pionex = "➡️ On Pionex: Sell or wait for reversal"
    else:
        emoji  = "🟡"
        action = "HOLD — No trade yet, monitor closely"
        pionex = "➡️ On Pionex: Hold current position"

    bar_filled = int(sig["confidence"] / 10)
    bar        = "█" * bar_filled + "░" * (10 - bar_filled)

    msg = f"""⚡ <b>APEX TRADING SIGNAL</b>

{emoji} <b>{sig['signal']} — {sym}</b>
📅 {now}

💵 <b>Current Price:</b>  ${sig['entry']:.{dec}f}
📈 <b>24h Change:</b>     {chg_s}

━━━━━━━━━━━━━━━
💰 <b>Entry:</b>        ${sig['entry']:.{dec}f}
🎯 <b>Take Profit:</b>  ${sig['take_profit']:.{dec}f}
🛑 <b>Stop Loss:</b>    ${sig['stop_loss']:.{dec}f}
━━━━━━━━━━━━━━━

📊 <b>Confidence:</b> {sig['confidence']}%
{bar}

{pionex}

⚠️ <i>Not financial advice. Always manage your risk.</i>"""
    return msg

# ── Main loop ───────────────────────────────────────────────────────────────
def main():
    print("🚀 APEX Signal Bot starting...")
    send_telegram("🚀 <b>APEX Signal Bot is now ONLINE</b>\n\nMonitoring XRP/USDT and SUI/USDT\nYou'll receive signals every time there's a strong BUY or SELL opportunity.\n\n⚡ Scanning every 5 minutes...")

    while True:
        print(f"\n[{datetime.utcnow().strftime('%H:%M:%S')}] Scanning markets...")
        prices = fetch_prices()

        if not prices:
            print("No price data — retrying in 60s")
            time.sleep(60)
            continue

        for coin in COINS:
            cid  = coin["id"]
            data = prices.get(cid)
            if not data or not data["price"]:
                print(f"  {cid.upper()}: no data")
                continue

            sig = compute_signal(data)
            print(f"  {cid.upper()}: ${data['price']:.4f} | {sig['signal']} (score:{sig['score']}, conf:{sig['confidence']}%)")

            # Only send BUY or SELL signals — skip HOLD to avoid spam
            # Also avoid sending same signal twice in a row
            if sig["signal"] in ("BUY", "SELL") and last_signal[cid] != sig["signal"]:
                msg = format_message(coin, data, sig)
                result = send_telegram(msg)
                if result and result.get("ok"):
                    print(f"  ✅ Signal sent to Telegram: {sig['signal']} {cid.upper()}")
                    last_signal[cid] = sig["signal"]
                else:
                    print(f"  ⚠ Telegram send failed: {result}")
            else:
                print(f"  ⏳ {cid.upper()} HOLD or repeat signal — not sending")

        print(f"  Next scan in {SCAN_INTERVAL//60} minutes...")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
