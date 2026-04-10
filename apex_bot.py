import os
import requests
import time
import hmac
import hashlib
from datetime import datetime
import json

TG_TOKEN = os.environ.get(“TELEGRAM_TOKEN”, “”)
TG_CHAT = os.environ.get(“TELEGRAM_CHAT_ID”, “”)
PX_KEY = os.environ.get(“P_API_KEY”, “”)
PX_SEC = os.environ.get(“P_SECRET”, “”)
TRADE_SIZE = 10
MIN_CONF = 65
last_signal = {}
pending = {}
positions = {}

COINS = [
{“id”:“ripple”,“symbol”:“XRP”,“pair”:“XRP/USDT”,“pionex”:“XRPUSDT”,“dec”:4,“qdec”:2},
{“id”:“sui”,“symbol”:“SUI”,“pair”:“SUI/USDT”,“pionex”:“SUIUSDT”,“dec”:4,“qdec”:2},
{“id”:“bitcoin”,“symbol”:“BTC”,“pair”:“BTC/USDT”,“pionex”:“BTCUSDT”,“dec”:0,“qdec”:5},
{“id”:“solana”,“symbol”:“SOL”,“pair”:“SOL/USDT”,“pionex”:“SOLUSDT”,“dec”:2,“qdec”:3},
{“id”:“binancecoin”,“symbol”:“BNB”,“pair”:“BNB/USDT”,“pionex”:“BNBUSDT”,“dec”:2,“qdec”:3},
{“id”:“dogecoin”,“symbol”:“DOGE”,“pair”:“DOGE/USDT”,“pionex”:“DOGEUSDT”,“dec”:5,“qdec”:1},
]

def tg_send(msg, markup=None):
url = “https://api.telegram.org/bot” + TG_TOKEN + “/sendMessage”
data = {“chat_id”: TG_CHAT, “text”: msg, “parse_mode”: “HTML”}
if markup:
data[“reply_markup”] = json.dumps(markup)
try:
r = requests.post(url, data=data, timeout=10)
return r.json()
except Exception as e:
print(“TG error: “ + str(e))
return None

def tg_answer(cb_id, text):
url = “https://api.telegram.org/bot” + TG_TOKEN + “/answerCallbackQuery”
try:
requests.post(url, data={“callback_query_id”: cb_id, “text”: text}, timeout=10)
except:
pass

def tg_updates(offset=None):
url = “https://api.telegram.org/bot” + TG_TOKEN + “/getUpdates”
params = {“timeout”: 1, “allowed_updates”: [“callback_query”]}
if offset:
params[“offset”] = offset
try:
r = requests.get(url, params=params, timeout=5)
return r.json()
except:
return None

def px_order(symbol, side, price, qty, qdec):
try:
ts = str(int(time.time() * 1000))
path = “/api/v1/trade/order”
body = {“symbol”:symbol,“side”:side,“type”:“LIMIT”,“price”:str(round(price,6)),“size”:str(round(qty,qdec)),“timeInForce”:“GTC”}
sorted_body = “&”.join(str(k) + “=” + str(v) for k, v in sorted(body.items()))
to_sign = “POST” + path + “?timestamp=” + ts + sorted_body
sig = hmac.new(PX_SEC.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
headers = {“PIONEX-KEY”: PX_KEY, “PIONEX-SIGNATURE”: sig, “Content-Type”: “application/json”}
body[“timestamp”] = ts
r = requests.post(“https://api.pionex.com” + path + “?timestamp=” + ts, json=body, headers=headers, timeout=10)
print(“RESULT: “ + str(r.json()))
return r.json()
except Exception as e:
print(“ERROR: “ + str(e))
return {“error”: str(e)}

def get_prices():
try:
ids = “,”.join([c[“id”] for c in COINS])
url = “https://api.coingecko.com/api/v3/simple/price?ids=” + ids + “&vs_currencies=usd&include_24hr_change=true&include_24hr_high=true&include_24hr_low=true&include_24hr_vol=true”
r = requests.get(url, timeout=15)
d = r.json()
if d and “ripple” in d and d[“ripple”].get(“usd”):
print(“Prices OK XRP:” + str(d[“ripple”][“usd”]))
return d
except Exception as e:
print(“CoinGecko:” + str(e))
try:
result = {}
pairs = {“ripple”:“XRPUSDT”,“bitcoin”:“BTCUSDT”,“sui”:“SUIUSDT”,“solana”:“SOLUSDT”,“binancecoin”:“BNBUSDT”,“dogecoin”:“DOGEUSDT”}
for gid, sym in pairs.items():
r = requests.get(“https://api.binance.com/api/v3/ticker/24hr?symbol=” + sym, timeout=10)
d = r.json()
if d and “lastPrice” in d:
result[gid] = {“usd”:float(d[“lastPrice”]),“usd_24h_change”:float(d[“priceChangePercent”]),“usd_24h_high”:float(d[“highPrice”]),“usd_24h_low”:float(d[“lowPrice”]),“usd_24h_vol”:float(d[“quoteVolume”])}
time.sleep(0.2)
if result:
print(“Binance OK”)
return result
except Exception as e:
print(“Binance:” + str(e))
return None

def fmt(price, dec):
if dec == 0:
return “$” + str(int(round(price, 0)))
return “$” + str(round(price, dec))

def signal(price, change, high, low, vol):
if not price:
return None
high = high or price * 1.02
low = low or price * 0.98
rng = high - low
pos = (price - low) / rng if rng > 0 else 0.5
score = 0
if change > 6: score += 5
elif change > 4: score += 4
elif change > 2: score += 3
elif change > 0: score += 1
elif change < -6: score -= 5
elif change < -4: score -= 4
elif change < -2: score -= 3
else: score -= 1
if pos < 0.20: score += 3
elif pos < 0.35: score += 2
elif pos > 0.85: score -= 2
elif pos > 0.70: score -= 1
if vol and vol > 500000000:
score += 1
if score >= 4:
return {“signal”:“BUY”,“conf”:min(92,65+score*4),“tp”:round(price*1.030,6),“sl”:round(price*0.985,6)}
elif score >= 2:
return {“signal”:“BUY”,“conf”:min(78,58+score*4),“tp”:round(price*1.020,6),“sl”:round(price*0.988,6)}
elif score <= -4:
return {“signal”:“SELL”,“conf”:min(90,65+abs(score)*4),“tp”:round(price*0.970,6),“sl”:round(price*1.015,6)}
elif score <= -2:
return {“signal”:“SELL”,“conf”:min(75,58+abs(score)*4),“tp”:round(price*0.980,6),“sl”:round(price*1.012,6)}
return None

def make_msg(coin, sig, price, change):
action = sig[“signal”]
dec = coin[“dec”]
sign = “+” if change >= 0 else “”
conf = sig[“conf”]
bars = “#” * int(conf/10) + “-” * (10 - int(conf/10))
return (
“<b>APEX SIGNAL</b>\n”
“==================\n”
“<b>” + action + “ - “ + coin[“pair”] + “</b>\n\n”
“Entry:       “ + fmt(price, dec) + “\n”
“Take Profit: “ + fmt(sig[“tp”], dec) + “\n”
“Stop Loss:   “ + fmt(sig[“sl”], dec) + “\n”
“Trade Size:  $” + str(TRADE_SIZE) + “ USDT\n\n”
“24h Change:  “ + sign + str(round(change, 2)) + “%\n”
“Confidence:  “ + str(conf) + “%\n”
“[” + bars + “]\n\n”
“Time: “ + datetime.utcnow().strftime(”%H:%M UTC”) + “\n”
“==================\n”
“Tap below to trade on Pionex”
)

def handle(cb):
data = cb.get(“data”, “”)
cb_id = cb[“id”]
parts = data.split(”_”)
action = parts[0] if parts else “”
sym = parts[1] if len(parts) > 1 else “”
print(“BTN: “ + action + “ “ + sym)
if action == “CONFIRM” and sym in pending:
trade = pending.pop(sym)
tg_answer(cb_id, “Placing order…”)
qty = round(TRADE_SIZE / trade[“price”], trade[“qdec”])
res = px_order(trade[“pionex”], “BUY”, trade[“price”], qty, trade[“qdec”])
if res and not res.get(“error”) and res.get(“result”):
positions[sym] = {“entry”:trade[“price”],“tp”:trade[“tp”],“sl”:trade[“sl”],“qty”:qty,“pionex”:trade[“pionex”],“qdec”:trade[“qdec”]}
tg_send(”<b>ORDER PLACED!</b>\n\n” + sym + “/USDT BUY @ $” + str(trade[“price”]) + “\nTP: $” + str(trade[“tp”]) + “\nSL: $” + str(trade[“sl”]) + “\nSize: $” + str(TRADE_SIZE) + “ USDT\n\nMonitoring…”)
else:
err = res.get(“message”) or res.get(“error”) or str(res)
tg_send(”<b>Order failed</b>\n\n” + str(err) + “\n\nPlace manually on Pionex.”)
elif action == “SKIP” and sym in pending:
pending.pop(sym, None)
tg_answer(cb_id, “Skipped”)
tg_send(“Skipped “ + sym + “ - next signal coming…”)
else:
tg_answer(cb_id, “Signal expired - wait for next one”)

def check_btns(offset):
updates = tg_updates(offset)
if updates and updates.get(“ok”):
for u in updates.get(“result”, []):
offset = u[“update_id”] + 1
cb = u.get(“callback_query”)
if cb:
handle(cb)
return offset

def run():
print(“APEX Bot v8 started”)
print(“API Key: “ + str(len(PX_KEY)) + “ chars”)
print(“Secret: “ + str(len(PX_SEC)) + “ chars”)
tg_send(
“<b>APEX Bot v8 Online!</b>\n\n”
“Monitoring: XRP, SUI, BTC, SOL, BNB, DOGE\n”
“Min confidence: “ + str(MIN_CONF) + “%\n”
“Trade size: $” + str(TRADE_SIZE) + “ USDT\n\n”
“Signals with CONFIRM and SKIP buttons!”
)
offset = None
counter = 0
while True:
offset = check_btns(offset)
if positions:
prices = get_prices()
if prices:
for coin in COINS:
sym = coin[“symbol”]
if sym not in positions:
continue
d = prices.get(coin[“id”], {})
price = d.get(“usd”)
if not price:
continue
pos = positions[sym]
if price >= pos[“tp”]:
pnl = round((pos[“tp”] - pos[“entry”]) * pos[“qty”], 2)
tg_send(”<b>TAKE PROFIT!</b>\n\n” + sym + “ @ $” + str(pos[“tp”]) + “\nProfit: +$” + str(pnl) + “ USDT”)
px_order(pos[“pionex”], “SELL”, pos[“tp”], pos[“qty”], pos[“qdec”])
del positions[sym]
elif price <= pos[“sl”]:
loss = round((pos[“sl”] - pos[“entry”]) * pos[“qty”], 2)
tg_send(”<b>STOP LOSS</b>\n\n” + sym + “ @ $” + str(pos[“sl”]) + “\nLoss: $” + str(loss) + “ USDT”)
px_order(pos[“pionex”], “SELL”, pos[“sl”], pos[“qty”], pos[“qdec”])
del positions[sym]
counter += 1
if counter >= 30:
counter = 0
print(”[” + datetime.utcnow().strftime(”%H:%M:%S”) + “] Scanning…”)
prices = get_prices()
if prices:
for coin in COINS:
sym = coin[“symbol”]
if sym in positions or sym in pending:
continue
d = prices.get(coin[“id”], {})
price = d.get(“usd”)
change = d.get(“usd_24h_change”, 0) or 0
high = d.get(“usd_24h_high”)
low = d.get(“usd_24h_low”)
vol = d.get(“usd_24h_vol”)
if not price:
continue
print(sym + “: $” + str(price) + “ “ + str(round(change,2)) + “%”)
sig = signal(price, change, high, low, vol)
if not sig or sig[“conf”] < MIN_CONF:
continue
prev = last_signal.get(sym)
if prev and prev[“signal”] == sig[“signal”] and abs(prev.get(“entry”,0) - price) / price < 0.005:
continue
sig[“entry”] = price
last_signal[sym] = sig
pending[sym] = {“price”:price,“tp”:sig[“tp”],“sl”:sig[“sl”],“pionex”:coin[“pionex”],“qdec”:coin[“qdec”]}
markup = {“inline_keyboard”:[[{“text”:“CONFIRM TRADE”,“callback_data”:“CONFIRM_” + sym},{“text”:“SKIP”,“callback_data”:“SKIP_” + sym}]]}
tg_send(make_msg(coin, sig, price, change), markup)
print(sym + “: “ + sig[“signal”] + “ “ + str(sig[“conf”]) + “%”)
time.sleep(10)

if **name** == “**main**”:
run()
