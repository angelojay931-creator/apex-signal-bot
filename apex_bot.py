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
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    if reply_markup:
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

def fetch_prices():
    # Try CoinGecko first
    try:
        ids = ",".join([c["id"] for c in COINS])
        url = (f"https://api.coingecko.com/api/v3/simple/price"
               f"?ids={ids}&vs_currencies=usd"
               f"&include_24hr_change=true&include_24hr_high=true"
               f"&include_24hr_low=true&include_24hr_vol=true")
        r = requests.get(url, timeout=15)
        data = r.json()
        # Check we actually got prices
        if data and "ripple" in data and data["ripple"].get("usd"):
            print(f"  Prices OK - XRP: ${data['ripple']['usd']}")
            return data
    except Exception as e:
        print(f"  CoinGecko error: {e}")

    # Fallback: try Binance
    try:
        result = {}
        mapping = {
            "ripple":"XRPUSDT","bitcoin":"BTCUSDT","sui":"SUIUSDT",
            "solana":"SOLUSDT","binancecoin":"BNBUSDT","dogecoin":"DOGEUSDT"
        }
        for gecko_id, symbol in mapping.items():
            r = requests.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}", timeout=10)
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
            print(f"  Binance prices OK - XRP: ${result.get('ripple',{}).get('usd','?')}")
            return result
    except Exception as e:
        print(f"  Binance error: {e}")

    print("  All price sources failed!")
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

def format_signal(coin, sig, price, change):
    emoji = "🟢" if sig["signal"] == "BUY" else "🔴"
    dec   = coin["decimals"]
    sign  = "+" if change >= 0 else ""
    bars  = "█" * int(sig["confidence"]/10) + "░" * (10-int(sig["confidence"]/10))
    def fmt(v): return f"${v:,.0f}" if dec == 0 else f"${v:.{dec}f}"
    return (
        f"⚡ <b>APEX SIGNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{emoji} <b>{sig['signal']} — {coin['pair']}</b>\n\n"
        f"💰 Entry:​​​​​​​​​​​​​​​​
