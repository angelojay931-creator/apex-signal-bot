import requests
import time
import hmac
import hashlib
from datetime import datetime
import json

TELEGRAM_TOKEN   = "8648873561:AAG07h-OOTh7PuH_EXtiAt0oxiBvIqbHLpI"
TELEGRAM_CHAT_ID = "5247767867"
PIONEX_API_KEY   = "9fFe42LKbokar2qu1NoSQbVsQzbFFiS7w8RsVuBzqP2hdB4EG9GQjAPLgXMChZNZc3"
PIONEX_SECRET    = "8pVokBaQY1CUeznWidI5YIhfogeshtyvqgxkwh3gffqXVgJg0cGOv4AQAxnKkr3C"
TRADE_SIZE_USDT  = 10
CONFIDENCE_THRESHOLD = 65
CHECK_INTERVAL   = 300
last_signal      = {}
pending_trades   = {}
open_positions   = {}

COINS = [
    {"id":"ripple",      "symbol":"XRP",  "pair":"XRP/USDT",  "pionex":"XRPUSDT",  "decimals":4, "qty_dec":2},
    {"id":"sui",         "symbol":"SUI",  "pair":"SUI/USDT",  "pionex":"SUIUSDT",  "decimals":4, "qty_dec":2},
    {"id":"bitcoin",     "symbol":"BTC",  "pair":"BTC/USDT",  "pionex":"BTCUSDT",  "decimals":0, "qty_dec":5},
    {"id":"solana",      "symbol":"SOL",  "pair":"SOL/USDT",  "pionex":"SOLUSDT",  "decimals":2, "qty_dec":3},
    {"id":"binancecoin", "symbol":"BNB",  "pair":"BNB/USDT",  "pionex":"BNBUSDT",  "decimals":2, "qty_dec":3},
    {"id":"dogecoin",    "symbol":"DOGE", "pair":"DOGE/USDT", "pionex":"DOGEUSDT", "decimals":5, "qty_dec":1},
]

def send_telegram(message, reply_markup=None):
    url  = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(url, data=data, timeout=10)
        return r.json()
    except Exception as e:
        print("Telegram error: " + str(e))
        return None

def answer_callback(callback_id, text):
    url  = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/answerCallbackQuery"
    data = {"callback_query_id": callback_id, "text": text}
    try:
        requests.post(url, data=data, timeout=10)
    except:
        pass

def get_updates(offset=None):
    url    = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/getUpdates"
    params = {"timeout": 5, "allowed_updates": ["callback_query"]}
    if offset:
        params["offset"] = offset
    try:
        r = requests.get(url, params=params, timeout=10)
        return r.json()
    except:
        return None

def pionex_sign(secret, params):
    sorted_params = "&".join(str(k) + "=" + str(v) for k, v in sorted(params.items()))
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
        print("Order error: " + str(e))
        return {"error": str(e)}

def fetch_prices():
    try:
        ids = ",".join([c["id"] for c in COINS])
        url = "https://api.coingecko.com/api/v3/simple/price?ids=" + ids + "&vs_currencies=usd&include_24hr_change=true&include_24hr_high=true&include_24hr_low=true&include_24hr_vol=true"
        r = requests.get(url, timeout=15)
        data = r.json()
        if data and "ripple" in data and data["ripple"].get("usd"):
            print("Prices OK - XRP: " + str(data["ripple"]["usd"]))
            return data
    except Exception as e:
        print("CoinGecko error: " + str(e))

    try:
        result = {}
        mapping = {
            "ripple":"XRPUSDT","bitcoin":"BTCUSDT","sui":"SUIUSDT",
            "solana":"SOLUSDT","binancecoin":"BNBUSDT","dogecoin":"DOGEUSDT"
        }
        for gecko_id, symbol in mapping.items():
            r = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=" + symbol, timeout=10)
            d = r.json()
            if d and "lastPrice" in d:
                result[gecko_id] = {
                    "usd": float(d["lastPrice"]),
                    "usd_24h_change": float(d["priceChangePercent"]),
                    "usd_24h_high": float(d["highPrice"]),
                    "usd_24h_low": float(d["lowPrice"]),
                    "usd_24h_vol": float(d["quoteVolume"]),
                }
            time.sleep(0.2)
        if result:
            print("Binance OK - XRP: " + str(result.get("ripple",{}).get("usd","?")))
            return result
    except Exception as e:
        print("Binance error: " + str(e))

    print("All price sources failed!")
    return None

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
    if vol and vol > 500_000_000:
        score += 1
    if score >= 4:
        return {"signal":"BUY","confidence":min(92,65+score*4),"tp":round(price*1.030,6),"sl":round(price*0.985,6)}
    elif score >= 2:
        return {"signal":"BUY","confidence":min(78,58+score*4),"tp":round(price*1.020,6),"sl":round(price*0.988,6)}
    elif score <= -4:
        return {"signal":"SELL","confidence":min(90,65+abs(score)*4),"tp":round(price*0.970,6),"sl":round(price*1.015,6)}
    elif score <= -2:
        return {"signal":"SELL","confidence":min(75,58+abs(score)*4),"tp":round(price*0.980,6),"sl":round(price*1.012,6)}
    return None

def format_signal(coin, sig, price, change):
    arrow = "BUY" if sig["signal"] == "BUY" else "SELL"
    dec   = coin["decimals"]
    sign  = "+" if change >= 0 else ""
    conf  = sig["confidence"]
    bars  = "#" * int(conf/10) + "-" * (10-int(conf/10))
    if dec == 0:
        ep  = "${:,.0f}".format(price)
        tp  = "${:,.0f}".format(sig["tp"])
        sl  = "${:,.0f}".format(sig["sl"])
    else:
        ep  = "${:.{}f}".format(dec, price)
        tp  = "${:.{}f}".format(dec, sig["tp"])
        sl  = "${:.{}f}".format(dec, sig["sl"])
    msg = (
        "<b>APEX SIGNAL</b>\n"
        "==================\n"
        "<b>" + arrow + " - " + coin["pair"] + "</b>\n\n"
        "Entry:       " + ep + "\n"
        "Take Profit: " + tp + "\n"
        "Stop Loss:   " + sl + "\n"
        "Trade Size:  $" + str(TRADE_SIZE_USDT) + " USDT\n\n"
        "24h Change:  " + sign + "{:.2f}".format(change) + "%\n"
        "Confidence:  " + str(conf) + "%\n"
        "[" + bars + "]\n\n"
        "Time: " + datetime.utcnow().strftime("%H:%M UTC") + "\n"
        "==================\n"
        "Tap below to trade on Pionex"
    )
    return msg

def run():
    print("APEX Semi-Auto Trading Bot started")
    send_telegram(
        "<b>APEX Bot Online!</b>\n\n"
        "Monitoring: XRP, SUI, BTC, SOL, BNB, DOGE\n"
        "Min confidence: " + str(CONFIDENCE_THRESHOLD) + "%\n"
        "Trade size: $" + str(TRADE_SIZE_USDT) + " USDT\n\n"
        "Signals coming with CONFIRM and SKIP buttons!"
    )

    offset = None

    while True:
        updates = get_updates(offset)
        if updates and updates.get("ok"):
            for update in updates.get("result", []):
                offset = update["update_id"] + 1
                cb = update.get("callback_query")
                if not cb:
                    continue
                data   = cb.get("data", "")
                cb_id  = cb["id"]
                parts  = data.split("_")
                action = parts[0] if parts else ""
                symbol = parts[1] if len(parts) > 1 else ""

                if action == "CONFIRM" and symbol in pending_trades:
                    trade = pending_trades.pop(symbol)
                    answer_callback(cb_id, "Placing order on Pionex...")
                    qty    = round(TRADE_SIZE_USDT / trade["price"], trade["qty_dec"])
                    result = place_order(trade["pionex"], "BUY", trade["price"], qty, trade["qty_dec"])
                    if result and not result.get("error") and result.get("result"):
                        open_positions[symbol] = {
                            "entry":   trade["price"],
                            "tp":      trade["tp"],
                            "sl":      trade["sl"],
                            "qty":     qty,
                            "pionex":  trade["pionex"],
                            "qty_dec": trade["qty_dec"],
                        }
                        send_telegram(
                            "<b>ORDER PLACED!</b>\n\n"
                            + symbol + "/USDT BUY @ $" + str(trade["price"]) + "\n"
                            "TP: $" + str(trade["tp"]) + "\n"
                            "SL: $" + str(trade["sl"]) + "\n"
                            "Size: $" + str(TRADE_SIZE_USDT) + " USDT\n\n"
                            "Monitoring position automatically..."
                        )
                    else:
                        err = result.get("message") or result.get("error") or "Unknown"
                        send_telegram("Order failed: " + str(err) + "\n\nPlace manually on Pionex.")

                elif action == "SKIP" and symbol in pending_trades:
                    pending_trades.pop(symbol, None)
                    answer_callback(cb_id, "Skipped")
                    send_telegram("Skipped " + symbol + " - waiting for next signal.")

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
                        pnl = round((pos["tp"] - pos["entry"]) * pos["qty"], 2)
                        send_telegram(
                            "<b>TAKE PROFIT HIT!</b>\n\n"
                            + sym + "/USDT closed @ $" + str(pos["tp"]) + "\n"
                            "Profit: +$" + str(pnl) + " USDT"
                        )
                        place_order(pos["pionex"], "SELL", pos["tp"], pos["qty"], pos["qty_dec"])
                        del open_positions[sym]
                    elif price <= pos["sl"]:
                        loss = round((pos["sl"] - pos["entry"]) * pos["qty"], 2)
                        send_telegram(
                            "<b>STOP LOSS HIT</b>\n\n"
                            + sym + "/USDT closed @ $" + str(pos["sl"]) + "\n"
                            "Loss: $" + str(loss) + " USDT\n\n"
                            "Position protected."
                        )
                        place_order(pos["pionex"], "SELL", pos["sl"], pos["qty"], pos["qty_dec"])
                        del open_positions[sym]

        print("[" + datetime.utcnow().strftime("%H:%M:%S") + "] Scanning markets...")
        prices = fetch_prices()
        if not prices:
            print("No price data - retrying in 60s")
            time.sleep(60)
            continue

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
                print(sym + ": no price")
                continue

            print(sym + ": $" + str(price) + " | " + "{:+.2f}".format(change) + "%")
            sig = compute_signal(price, change, high, low, vol)
            if not sig:
                print(sym + ": HOLD")
                continue
            if sig["confidence"] < CONFIDENCE_THRESHOLD:
                print(sym + ": " + sig["signal"] + " low confidence " + str(sig["confidence"]) + "%")
                continue
            prev = last_signal.get(sym)
            if prev and prev["signal"] == sig["signal"] and price and abs(prev.get("entry",0) - price)/price < 0.005:
                print(sym + ": same signal - skipping")
                continue

            sig["entry"] = price
            last_signal[sym] = sig

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
                    {"text": "CONFIRM TRADE", "callback_data": "CONFIRM_" + sym},
                    {"text": "SKIP",          "callback_data": "SKIP_" + sym}
                ]]
            }
            send_telegram(msg, reply_markup=markup)
            print(sym + ": " + sig["signal"] + " signal sent! " + str(sig["confidence"]) + "%")

        print("Next scan in " + str(CHECK_INTERVAL) + "s\n")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    run()
