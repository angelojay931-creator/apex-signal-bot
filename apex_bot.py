import requests
import time
import hmac
import hashlib
import urllib.parse
from datetime import datetime

# ── Config ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = "8648873561:AAG07h-OOTh7PuH_EXtiAt0oxiBvIqbHLpI"
TELEGRAM_CHAT_ID = "5247767867"
PIONEX_API_KEY    = "9fFe42LKbokar2qu1NoSQbVsQzbFFiS7w8RsVuBzqP2hdB4EG9GQjAPLgXMChZNZc3"
PIONEX_SECRET     = "8pVokBaQY1CUeznWidI5YIhfogeshtyvqgxkwh3gffqXVgJg0cGOv4AQAxnKkr3C"
TRADE_SIZE_USDT   = 10
CONFIDENCE_THRESHOLD = 70
CHECK_INTERVAL    = 300
last_signal       = {}
pending_trades    = {}
open_positions    = {}

COINS = [
    {"id":"bitcoin",     "symbol":"BTC", "pair":"BTC/USDT",  "pionex":"BTCUSDT",  "decimals":0, "qty_dec":5},
    {"id":"ripple",      "symbol":"XRP", "pair":"XRP/USDT",  "pionex":"XRPUSDT",  "decimals":4, "qty_dec":2},
    {"id":"sui",         "symbol":"SUI", "pair":"SUI/USDT",  "pionex":"SUIUSDT",  "decimals":4, "qty_dec":2},
    {"id":"solana",      "symbol":"SOL", "pair":"SOL/USDT",  "pionex":"SOLUSDT",  "decimals":2, "qty_dec":3},
    {"id":"binancecoin", "symbol":"BNB", "pair":"BNB/USDT",  "pionex":"BNBUSDT",  "decimals":2, "qty_dec":3},
    {"id":"dogecoin",    "symbol":"DOGE","pair":"DOGE/USDT", "pionex":"DOGEUSDT", "decimals":5, "qty_dec":1},
]

# ── Telegram ─────────────────────────────────────────────────────────────────
def send_telegram(message, reply_markup=None):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    if reply_markup:
        import json
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(url, data=data, timeout=10)
        return r.json()
    except Exception as e:
        print(f"Telegram error: {e}")
        return None

def answer_callback(callback_id, text):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    data = {"callback_query_id": callback_id, "text": text}
    try:
        requests.post(url, data=data, timeout=10)
    except:
        pass

def get_updates(offset=None):
    url    = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"timeout": 5, "allowed_updates": ["callback_query"]}
    if offset:
        params["offset"] = offset
    try:
        r = requests.get(url, params=params, timeout=10)
        return r.json()
    except:
        return None

# ── Pionex API ────────────────────────────────────────────────────────────────
def pionex_sign(secret, params):
    sorted_params = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(secret.encode(), sorted_params.encode(), hashlib.sha256).hexdigest()

def place_order(pionex_symbol, side, price, qty, qty_dec):
    try:
        timestamp = str(int(time.time() * 1000))
        params = {
            "symbol":      pionex_symbol,
            "side":        side,
            "type":        "LIMIT",
            "price":       str(round(price, 6)),
            "size":        str(round(qty, qty_dec)),
            "timeInForce": "GTC",
            "timestamp":   timestamp,
        }
        sig = pionex_sign(PIONEX_SECRET, params)
        params["signature"] = sig
        headers = {"X-PIONEX-KEY": PIONEX_API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
        r = requests.post("https://api.pionex.com/api/v1/trade/order", data=params, headers=headers, timeout=10)
        return r.json()
    except Exception as e:
        print(f"Order error: {e}")
        return {"error": str(e)}

# ── Prices ───────────────────────────────────────────────────────────────────
def fetch_prices():
    ids = ",".join([c["id"] for c in COINS])
    url = (f"https://api.coingecko.com/api/v3/simple/price"
           f"?ids={ids}&vs_currencies=usd"
           f"&include_24hr_change=true&include_24hr_high=true"
           f"&include_24hr_low=true&include_24hr_vol=true")
    try:
        r = requests.get(url, timeout=10)
        return r.json()
    except:
        return None

# ── Signal ───────────────────────────────────────────────────────────────────
def compute_signal(price, change, high, low, vol):
    if not price:
        return None
    high = high or price * 1.02
    low  = low  or price * 0.98
    rng  = high - low
    pos  = (price - low) / rng if rng > 0 else 0.5
    score = 0
    if   change >  6: score += 5
    elif change >  4: score += 4
    elif change >  2: score += 3
    elif change >  0: score += 1
    elif change < -6: score -= 5
    elif change < -4: score -= 4
    elif change < -2: score -= 3
    else:             score -= 1
    if   pos < 0.20: score += 3
    elif pos < 0.35: score += 2
    elif pos > 0.85: score -= 2
    elif pos > 0.70: score -= 1
    if vol and vol > 500_000_000: score += 1

    if score >= 4:
        return {"signal":"BUY","confidence":min(92,65+score*4),"tp":round(price*1.030,6),"sl":round(price*0.985,6)}
    elif score >= 2:
        return {"signal":"BUY","confidence":min(78,58+score*4),"tp":round(price*1.020,6),"sl":round(price*0.988,6)}
    elif score <= -4:
        return {"signal":"SELL","confidence":min(90,65+abs(score)*4),"tp":round(price*0.970,6),"sl":round(price*1.015,6)}
    elif score <= -2:
        return {"signal":"SELL","confidence":min(75,58+abs(score)*4),"tp":round(price*0.980,6),"sl":round(price*1.012,6)}
    return None

# ── Format signal message ─────────────────────────────────────────────────────
def format_signal(coin, sig, price, change):
    emoji  = "🟢" if sig["signal"] == "BUY" else "🔴"
    dec    = coin["decimals"]
    sign   = "+" if change >= 0 else ""
    bars   = "█" * int(sig["confidence"]/10) + "░" * (10-int(sig["confidence"]/10))
    def fmt(v): return f"${v:,.0f}" if dec == 0 else f"${v:.{dec}f}"
    return (
        f"⚡ <b>APEX SIGNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{emoji} <b>{sig['signal']} — {coin['pair']}</b>\n\n"
        f"💰 Entry:        {fmt(price)}\n"
        f"🎯 Take Profit:  {fmt(sig['tp'])}\n"
        f"🛑 Stop Loss:    {fmt(sig['sl'])}\n"
        f"💵 Trade Size:   ${TRADE_SIZE_USDT} USDT\n\n"
        f"📊 24h Change:   {sign}{change:.2f}%\n"
        f"🔥 Confidence:   {sig['confidence']}%\n"
        f"    {bars}\n\n"
        f"⏰ {datetime.utcnow().strftime('%H:%M UTC')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Tap below to trade on Pionex 👇"
    )

# ── Main loop ─────────────────────────────────────────────────────────────────
def run():
    print("🚀 APEX Semi-Auto Trading Bot started")
    send_telegram(
        "🚀 <b>APEX Semi-Auto Bot Online!</b>\n\n"
        "I will send you signals with\n"
        "✅ CONFIRM and ❌ SKIP buttons.\n\n"
        "Tap CONFIRM to place trade on Pionex automatically!\n"
        f"Trade size: ${TRADE_SIZE_USDT} USDT per trade 💰"
    )

    offset = None

    while True:
        # ── Check for button presses ──
        updates = get_updates(offset)
        if updates and updates.get("ok"):
            for update in updates.get("result", []):
                offset = update["update_id"] + 1
                cb = update.get("callback_query")
                if not cb:
                    continue

                data      = cb.get("data", "")
                cb_id     = cb["id"]
                parts     = data.split("_")
                action    = parts[0] if parts else ""
                symbol    = parts[1] if len(parts) > 1 else ""

                if action == "CONFIRM" and symbol in pending_trades:
                    trade = pending_trades.pop(symbol)
                    answer_callback(cb_id, "⚡ Placing order on Pionex...")

                    qty    = round(TRADE_SIZE_USDT / trade["price"], trade["qty_dec"])
                    result = place_order(trade["pionex"], "BUY", trade["price"], qty, trade["qty_dec"])

                    if result and not result.get("error") and result.get("result"):
                        open_positions[symbol] = {
                            "entry": trade["price"],
                            "tp":    trade["tp"],
                            "sl":    trade["sl"],
                            "qty":   qty,
                            "pionex":trade["pionex"],
                            "qty_dec":trade["qty_dec"],
                        }
                        send_telegram(
                            f"✅ <b>ORDER PLACED!</b>\n\n"
                            f"📈 {symbol}/USDT BUY @ ${trade['price']}\n"
                            f"🎯 TP: ${trade['tp']}\n"
                            f"🛑 SL: ${trade['sl']}\n"
                            f"💵 Size: ${TRADE_SIZE_USDT} USDT\n\n"
                            f"Monitoring position automatically... 👀"
                        )
                    else:
                        err = result.get("message") or result.get("error") or "Unknown error"
                        send_telegram(f"⚠️ Order failed: {err}\n\nPlease place manually on Pionex.")

                elif action == "SKIP" and symbol in pending_trades:
                    pending_trades.pop(symbol, None)
                    answer_callback(cb_id, "⏭ Signal skipped")
                    send_telegram(f"⏭ <b>Skipped {symbol}</b> — waiting for next signal.")

        # ── Check open positions ──
        if open_positions:
            prices = fetch_prices()
            if prices:
                for coin in COINS:
                    sym = coin["symbol"]
                    if sym not in open_positions:
                        continue
                    d     = prices.get(coin["id"], {})
                    price = d.get("usd")
                    if not price:
                        continue
                    pos = open_positions[sym]
                    if price >= pos["tp"]:
                        pnl = (pos["tp"] - pos["entry"]) * pos["qty"]
                        send_telegram(
                            f"🎉 <b>TAKE PROFIT HIT!</b>\n\n"
                            f"✅ {sym}/USDT closed @ ${pos['tp']}\n"
                            f"💰 Profit: +${pnl:.2f} USDT\n\n"
                            f"Great trade! 🚀"
                        )
                        place_order(pos["pionex"], "SELL", pos["tp"], pos["qty"], pos["qty_dec"])
                        del open_positions[sym]
                    elif price <= pos["sl"]:
                        loss = (pos["sl"] - pos["entry"]) * pos["qty"]
                        send_telegram(
                            f"🛑 <b>STOP LOSS HIT</b>\n\n"
                            f"❌ {sym}/USDT closed @ ${pos['sl']}\n"
                            f"📉 Loss: ${loss:.2f} USDT\n\n"
                            f"Position protected. Next signal coming... 💪"
                        )
                        place_order(pos["pionex"], "SELL", pos["sl"], pos["qty"], pos["qty_dec"])
                        del open_positions[sym]

        # ── Scan for new signals ──
        print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] Scanning markets...")
        prices = fetch_prices()
        if prices:
            for coin in COINS:
                sym = coin["symbol"]
                if sym in open_positions or sym in pending_trades:
                    continue
                d      = prices.get(coin["id"], {})
                price  = d.get("usd")
                change = d.get("usd_24h_change", 0) or 0
                high   = d.get("usd_24h_high")
                low    = d.get("usd_24h_low")
                vol    = d.get("usd_24h_vol")
                if not price:
                    continue
                sig = compute_signal(price, change, high, low, vol)
                if not sig or sig["confidence"] < CONFIDENCE_THRESHOLD:
                    continue
                prev = last_signal.get(sym)
                if prev and prev["signal"] == sig["signal"] and abs(prev.get("entry",0) - price)/price < 0.005:
                    continue
                sig["entry"] = price
                last_signal[sym] = sig

                if sig["signal"] == "BUY":
                    pending_trades[sym] = {
                        "price":   price,
                        "tp":      sig["tp"],
                        "sl":      sig["sl"],
                        "pionex":  coin["pionex"],
                        "qty_dec": coin["qty_dec"],
                    }
                    msg = format_signal(coin, sig, price, change)
                    markup = {
                        "inline_keyboard": [[
                            {"text": "✅ CONFIRM TRADE", "callback_data": f"CONFIRM_{sym}"},
                            {"text": "❌ SKIP",          "callback_data": f"SKIP_{sym}"}
                        ]]
                    }
                    send_telegram(msg, reply_markup=markup)
                    print(f"  ✅ {sym}: Signal sent! Confidence: {sig['confidence']}%")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    run()
