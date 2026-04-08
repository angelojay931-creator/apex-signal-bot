import requests
import time
import hmac
import hashlib
from datetime import datetime

# ── Config ─────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = "8648873561:AAG07h-OOTh7PuH_EXtiAt0oxiBvIqbHLpI"
TELEGRAM_CHAT_ID = "5247767867"

COINS = [
    {"id": "bitcoin",   "symbol": "BTC", "pair": "BTC/USDT",  "decimals": 0},
    {"id": "ripple",    "symbol": "XRP", "pair": "XRP/USDT",  "decimals": 4},
    {"id": "sui",       "symbol": "SUI", "pair": "SUI/USDT",  "decimals": 4},
    {"id": "solana",    "symbol": "SOL", "pair": "SOL/USDT",  "decimals": 2},
    {"id": "binancecoin","symbol":"BNB", "pair": "BNB/USDT",  "decimals": 2},
    {"id": "dogecoin",  "symbol": "DOG", "pair": "DOGE/USDT", "decimals": 5},
]

CONFIDENCE_THRESHOLD = 70   # Only send signals above this %
CHECK_INTERVAL       = 300  # Check every 5 minutes
last_signal          = {}   # Track last signal per coin to avoid repeats

# ── Telegram ───────────────────────────────────────────────────────────────
def send_telegram(message):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

# ── Fetch prices from CoinGecko ────────────────────────────────────────────
def fetch_prices():
    ids = ",".join([c["id"] for c in COINS])
    url = (f"https://api.coingecko.com/api/v3/simple/price"
           f"?ids={ids}&vs_currencies=usd"
           f"&include_24hr_change=true"
           f"&include_24hr_high=true"
           f"&include_24hr_low=true"
           f"&include_24hr_vol=true")
    try:
        r = requests.get(url, timeout=10)
        return r.json()
    except Exception as e:
        print(f"Price fetch error: {e}")
        return None

# ── Signal engine ──────────────────────────────────────────────────────────
def compute_signal(price, change, high, low, vol):
    if not price:
        return None

    high  = high  or price * 1.02
    low   = low   or price * 0.98
    range_ = high - low
    position = (price - low) / range_ if range_ > 0 else 0.5

    score = 0

    # Momentum scoring
    if   change >  6: score += 5
    elif change >  4: score += 4
    elif change >  2: score += 3
    elif change >  0: score += 1
    elif change < -6: score -= 5
    elif change < -4: score -= 4
    elif change < -2: score -= 3
    else:             score -= 1

    # Position in range
    if   position < 0.20: score += 3  # Near bottom = strong buy zone
    elif position < 0.35: score += 2
    elif position > 0.85: score -= 2  # Near top = resistance
    elif position > 0.70: score -= 1

    # Volume boost (high volume = stronger signal)
    if vol and vol > 500_000_000:
        score += 1

    # Determine signal
    if score >= 4:
        signal     = "BUY"
        confidence = min(92, 65 + score * 4)
        tp         = round(price * 1.030, 6)  # 3% take profit
        sl         = round(price * 0.985, 6)  # 1.5% stop loss
    elif score >= 2:
        signal     = "BUY"
        confidence = min(78, 58 + score * 4)
        tp         = round(price * 1.020, 6)
        sl         = round(price * 0.988, 6)
    elif score <= -4:
        signal     = "SELL"
        confidence = min(90, 65 + abs(score) * 4)
        tp         = round(price * 0.970, 6)
        sl         = round(price * 1.015, 6)
    elif score <= -2:
        signal     = "SELL"
        confidence = min(75, 58 + abs(score) * 4)
        tp         = round(price * 0.980, 6)
        sl         = round(price * 1.012, 6)
    else:
        return None  # HOLD — don't send

    return {
        "signal":     signal,
        "confidence": confidence,
        "score":      score,
        "entry":      price,
        "tp":         tp,
        "sl":         sl,
    }

# ── Format message ─────────────────────────────────────────────────────────
def format_message(coin, sig, price, change):
    emoji  = "🟢" if sig["signal"] == "BUY" else "🔴"
    action = "BUY  — LONG"  if sig["signal"] == "BUY" else "SELL — SHORT"
    dec    = coin["decimals"]
    chsign = "+" if change >= 0 else ""
    time_  = datetime.utcnow().strftime("%H:%M UTC")

    # Format prices nicely
    def fmt(v):
        if dec == 0: return f"${v:,.0f}"
        return f"${v:.{dec}f}"

    bars  = "█" * int(sig["confidence"] / 10) + "░" * (10 - int(sig["confidence"] / 10))

    msg = (
        f"⚡ <b>APEX SIGNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{emoji} <b>{sig['signal']} — {coin['pair']}</b>\n\n"
        f"💰 Entry:        {fmt(sig['entry'])}\n"
        f"🎯 Take Profit:  {fmt(sig['tp'])}\n"
        f"🛑 Stop Loss:    {fmt(sig['sl'])}\n\n"
        f"📊 24h Change:   {chsign}{change:.2f}%\n"
        f"🔥 Confidence:   {sig['confidence']}%\n"
        f"    {bars}\n\n"
        f"📱 <i>Place manually on Pionex</i>\n"
        f"⏰ {time_}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Not financial advice. DYOR."
    )
    return msg

# ── Main loop ──────────────────────────────────────────────────────────────
def run():
    print("🚀 APEX Trading Signal Bot started")
    print(f"📊 Monitoring: {', '.join([c['symbol'] for c in COINS])}")
    print(f"🎯 Min confidence: {CONFIDENCE_THRESHOLD}%")
    print(f"⏱  Check interval: {CHECK_INTERVAL}s\n")

    send_telegram(
        "🚀 <b>APEX Signal Bot Online</b>\n\n"
        f"Monitoring: BTC · XRP · SUI · SOL · BNB · DOGE\n"
        f"Min confidence: {CONFIDENCE_THRESHOLD}%\n"
        "Only strong signals will be sent. 💪"
    )

    while True:
        try:
            print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] Scanning markets...")
            prices = fetch_prices()

            if not prices:
                print("  ⚠ Price fetch failed, retrying...")
                time.sleep(60)
                continue

            signals_sent = 0

            for coin in COINS:
                d = prices.get(coin["id"], {})
                if not d:
                    continue

                price  = d.get("usd")
                change = d.get("usd_24h_change", 0) or 0
                high   = d.get("usd_24h_high")
                low    = d.get("usd_24h_low")
                vol    = d.get("usd_24h_vol")

                if not price:
                    continue

                sig = compute_signal(price, change, high, low, vol)

                if not sig:
                    print(f"  {coin['symbol']}: HOLD (no strong signal)")
                    continue

                if sig["confidence"] < CONFIDENCE_THRESHOLD:
                    print(f"  {coin['symbol']}: {sig['signal']} but confidence too low ({sig['confidence']}%)")
                    continue

                # Avoid sending same signal twice in a row
                prev = last_signal.get(coin["symbol"])
                if prev and prev["signal"] == sig["signal"] and abs(prev["entry"] - price) / price < 0.005:
                    print(f"  {coin['symbol']}: Same signal as last time, skipping")
                    continue

                # Send it!
                msg = format_message(coin, sig, price, change)
                send_telegram(msg)
                last_signal[coin["symbol"]] = sig
                signals_sent += 1
                print(f"  ✅ {coin['symbol']}: {sig['signal']} signal sent! Confidence: {sig['confidence']}%")
                time.sleep(1)  # Small delay between messages

            if signals_sent == 0:
                print(f"  No strong signals this cycle.")

        except Exception as e:
            print(f"  Error: {e}")

        print(f"  Sleeping {CHECK_INTERVAL}s...\n")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    run()
