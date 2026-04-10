import os
import json
import time
import hmac
import uuid
import hashlib
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone

import requests

# =========================
# CONFIG
# =========================
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
PX_KEY = os.environ.get("P_API_KEY", "").strip()
PX_SEC = os.environ.get("P_SECRET", "").strip()

TRADE_SIZE = Decimal("10")   # USDT per trade
MIN_CONF = 65
SCAN_EVERY_SECONDS = 30
PRICE_TIMEOUT = 15
HTTP_TIMEOUT = 15

last_signal = {}
pending = {}
positions = {}

COINS = [
    {"id": "ripple",      "symbol": "XRP",  "pair": "XRP/USDT",  "pionex": "XRP_USDT",  "dec": 4, "qdec": 2},
    {"id": "sui",         "symbol": "SUI",  "pair": "SUI/USDT",  "pionex": "SUI_USDT",  "dec": 4, "qdec": 2},
    {"id": "bitcoin",     "symbol": "BTC",  "pair": "BTC/USDT",  "pionex": "BTC_USDT",  "dec": 0, "qdec": 5},
    {"id": "solana",      "symbol": "SOL",  "pair": "SOL/USDT",  "pionex": "SOL_USDT",  "dec": 2, "qdec": 3},
    {"id": "binancecoin", "symbol": "BNB",  "pair": "BNB/USDT",  "pionex": "BNB_USDT",  "dec": 2, "qdec": 3},
    {"id": "dogecoin",    "symbol": "DOGE", "pair": "DOGE/USDT", "pionex": "DOGE_USDT", "dec": 5, "qdec": 1},
]

session = requests.Session()


# =========================
# HELPERS
# =========================
def now_ms() -> int:
    return int(time.time() * 1000)


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M UTC")


def floor_to_decimals(value, decimals: int) -> str:
    """
    Return a string rounded DOWN to a fixed number of decimals.
    Safer for exchange quantity formatting than Python round().
    """
    q = Decimal("1") if decimals == 0 else Decimal("1." + ("0" * decimals))
    d = Decimal(str(value)).quantize(q, rounding=ROUND_DOWN)
    return format(d, "f")


def fmt_price(price, dec: int) -> str:
    if price is None:
        return "$0"
    if dec == 0:
        return "$" + str(int(Decimal(str(price)).quantize(Decimal("1"), rounding=ROUND_DOWN)))
    return "$" + format(Decimal(str(price)).quantize(Decimal("1." + ("0" * dec))), "f")


def build_query_string(params: dict) -> str:
    """
    Pionex requires query params sorted alphabetically for signature construction.
    """
    items = sorted((k, v) for k, v in params.items() if v is not None)
    return "&".join(f"{k}={v}" for k, v in items)


def pionex_sign(method: str, path: str, query_params: dict, body: dict | None = None) -> tuple[str, str]:
    """
    Signature for Pionex private endpoints:
    METHOD + PATH_URL(with sorted query incl timestamp) + body(JSON) for POST/DELETE.
    """
    query_string = build_query_string(query_params)
    path_url = f"{path}?{query_string}" if query_string else path

    body_str = ""
    if method.upper() in {"POST", "DELETE"} and body is not None:
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    payload = f"{method.upper()}{path_url}{body_str}"
    signature = hmac.new(
        PX_SEC.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    return signature, body_str


# =========================
# TELEGRAM
# =========================
def tg_send(msg: str, markup: dict | None = None):
    if not TG_TOKEN or not TG_CHAT:
        print("Telegram config missing.")
        return None

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = {
        "chat_id": TG_CHAT,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if markup:
        data["reply_markup"] = json.dumps(markup, separators=(",", ":"))

    try:
        r = session.post(url, data=data, timeout=HTTP_TIMEOUT)
        try:
            payload = r.json()
        except Exception:
            payload = {"ok": False, "status_code": r.status_code, "text": r.text}

        if not r.ok:
            print("Telegram send HTTP error:", r.status_code, r.text)
        elif not payload.get("ok", False):
            print("Telegram send API error:", payload)

        return payload
    except Exception as e:
        print("TG send error:", str(e))
        return None


def tg_answer(cb_id: str, text: str):
    if not TG_TOKEN:
        return

    url = f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery"
    try:
        session.post(
            url,
            data={"callback_query_id": cb_id, "text": text},
            timeout=HTTP_TIMEOUT
        )
    except Exception:
        pass


def tg_updates(offset=None):
    if not TG_TOKEN:
        return None

    url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates"
    params = {
        "timeout": 1,
        "allowed_updates": json.dumps(["callback_query"]),
    }
    if offset is not None:
        params["offset"] = offset

    try:
        r = session.get(url, params=params, timeout=5)
        return r.json()
    except Exception as e:
        print("TG updates error:", str(e))
        return None


# =========================
# PIONEX
# =========================
def px_private_request(method: str, path: str, query_params=None, body=None):
    if not PX_KEY or not PX_SEC:
        return {"result": False, "error": "Missing Pionex API credentials"}

    query_params = dict(query_params or {})
    query_params["timestamp"] = now_ms()

    signature, body_str = pionex_sign(method, path, query_params, body)

    headers = {
        "PIONEX-KEY": PX_KEY,
        "PIONEX-SIGNATURE": signature,
        "Content-Type": "application/json",
    }

    url = f"https://api.pionex.com{path}"
    try:
        if method.upper() == "GET":
            r = session.get(url, params=query_params, headers=headers, timeout=HTTP_TIMEOUT)
        elif method.upper() == "POST":
            r = session.post(url, params=query_params, data=body_str, headers=headers, timeout=HTTP_TIMEOUT)
        elif method.upper() == "DELETE":
            r = session.delete(url, params=query_params, data=body_str, headers=headers, timeout=HTTP_TIMEOUT)
        else:
            return {"result": False, "error": f"Unsupported method: {method}"}

        try:
            res = r.json()
        except Exception:
            res = {"result": False, "error": f"Non-JSON response: {r.text}"}

        if not r.ok:
            if isinstance(res, dict):
                res.setdefault("error", f"HTTP {r.status_code}")
            else:
                res = {"result": False, "error": f"HTTP {r.status_code}", "raw": str(res)}

        return res
    except Exception as e:
        return {"result": False, "error": str(e)}


def px_order(symbol: str, side: str, price, qty, qdec: int):
    """
    Place a LIMIT order on Pionex SPOT trade API.
    """
    body = {
        "symbol": symbol,
        "side": side.upper(),
        "type": "LIMIT",
        "price": floor_to_decimals(price, 8),
        "size": floor_to_decimals(qty, qdec),
        "clientOrderId": f"apex-{uuid.uuid4().hex[:20]}",
        "IOC": False,
    }

    print("Submitting order:", body)
    res = px_private_request("POST", "/api/v1/trade/order", body=body)
    print("Pionex response:", res)
    return res


def px_cancel(symbol: str, order_id):
    body = {
        "symbol": symbol,
        "orderId": order_id,
    }
    return px_private_request("DELETE", "/api/v1/trade/order", body=body)


# =========================
# PRICE FEED
# =========================
def get_prices():
    """
    First try CoinGecko, then fall back to Binance public ticker.
    """
    try:
        ids = ",".join([c["id"] for c in COINS])
        url = (
            "https://api.coingecko.com/api/v3/simple/price"
            f"?ids={ids}"
            "&vs_currencies=usd"
            "&include_24hr_change=true"
            "&include_24hr_high=true"
            "&include_24hr_low=true"
            "&include_24hr_vol=true"
        )
        r = session.get(url, timeout=PRICE_TIMEOUT)
        d = r.json()
        if d and "ripple" in d and d["ripple"].get("usd"):
            print("CoinGecko OK XRP:", d["ripple"]["usd"])
            return d
    except Exception as e:
        print("CoinGecko error:", str(e))

    try:
        result = {}
        pairs = {
            "ripple": "XRPUSDT",
            "bitcoin": "BTCUSDT",
            "sui": "SUIUSDT",
            "solana": "SOLUSDT",
            "binancecoin": "BNBUSDT",
            "dogecoin": "DOGEUSDT",
        }
        for gid, sym in pairs.items():
            r = session.get(
                f"https://api.binance.com/api/v3/ticker/24hr?symbol={sym}",
                timeout=10
            )
            d = r.json()
            if d and "lastPrice" in d:
                result[gid] = {
                    "usd": float(d["lastPrice"]),
                    "usd_24h_change": float(d["priceChangePercent"]),
                    "usd_24h_high": float(d["highPrice"]),
                    "usd_24h_low": float(d["lowPrice"]),
                    "usd_24h_vol": float(d["quoteVolume"]),
                }
            time.sleep(0.2)

        if result:
            print("Binance fallback OK")
            return result
    except Exception as e:
        print("Binance fallback error:", str(e))

    return None


# =========================
# SIGNAL LOGIC
# =========================
def signal(price, change, high, low, vol):
    if not price:
        return None

    high = high or price * 1.02
    low = low or price * 0.98
    rng = high - low
    pos = (price - low) / rng if rng > 0 else 0.5

    score = 0

    if change > 6:
        score += 5
    elif change > 4:
        score += 4
    elif change > 2:
        score += 3
    elif change > 0:
        score += 1
    elif change < -6:
        score -= 5
    elif change < -4:
        score -= 4
    elif change < -2:
        score -= 3
    else:
        score -= 1

    if pos < 0.20:
        score += 3
    elif pos < 0.35:
        score += 2
    elif pos > 0.85:
        score -= 2
    elif pos > 0.70:
        score -= 1

    if vol and vol > 500000000:
        score += 1

    if score >= 4:
        return {
            "signal": "BUY",
            "conf": min(92, 65 + score * 4),
            "tp": round(price * 1.030, 8),
            "sl": round(price * 0.985, 8),
        }
    elif score >= 2:
        return {
            "signal": "BUY",
            "conf": min(78, 58 + score * 4),
            "tp": round(price * 1.020, 8),
            "sl": round(price * 0.988, 8),
        }
    elif score <= -4:
        return {
            "signal": "SELL",
            "conf": min(90, 65 + abs(score) * 4),
            "tp": round(price * 0.970, 8),
            "sl": round(price * 1.015, 8),
        }
    elif score <= -2:
        return {
            "signal": "SELL",
            "conf": min(75, 58 + abs(score) * 4),
            "tp": round(price * 0.980, 8),
            "sl": round(price * 1.012, 8),
        }

    return None


def make_msg(coin, sig, price, change):
    action = sig["signal"]
    dec = coin["dec"]
    sign = "+" if change >= 0 else ""
    conf = sig["conf"]
    bars = "#" * int(conf / 10) + "-" * (10 - int(conf / 10))

    return (
        "<b>APEX SIGNAL</b>\n"
        "==================\n"
        f"<b>{action} - {coin['pair']}</b>\n\n"
        f"Entry:       {fmt_price(price, dec)}\n"
        f"Take Profit: {fmt_price(sig['tp'], dec)}\n"
        f"Stop Loss:   {fmt_price(sig['sl'], dec)}\n"
        f"Trade Size:  ${TRADE_SIZE} USDT\n\n"
        f"24h Change:  {sign}{round(change, 2)}%\n"
        f"Confidence:  {conf}%\n"
        f"[{bars}]\n\n"
        f"Time: {utc_now_str()}\n"
        "==================\n"
        "Tap below to trade on Pionex"
    )


# =========================
# TELEGRAM BUTTON HANDLING
# =========================
def handle(cb):
    data = cb.get("data", "")
    cb_id = cb.get("id", "")
    parts = data.split("_")
    action = parts[0] if parts else ""
    sym = parts[1] if len(parts) > 1 else ""

    print("BTN:", action, sym)

    if action == "CONFIRM" and sym in pending:
        trade = pending.pop(sym)

        if trade["signal"] != "BUY":
            tg_answer(cb_id, "Only BUY auto-order is enabled")
            tg_send(
                f"<b>{sym}</b> signal was SELL.\n\n"
                "This bot is currently set to place only BUY spot orders automatically."
            )
            return

        tg_answer(cb_id, "Placing order...")

        qty_decimal = TRADE_SIZE / Decimal(str(trade["price"]))
        qty_str = floor_to_decimals(qty_decimal, trade["qdec"])
        qty = Decimal(qty_str)

        if qty <= 0:
            tg_send(f"<b>Order failed</b>\n\nCalculated quantity for {sym} is too small.")
            return

        res = px_order(
            symbol=trade["pionex"],
            side="BUY",
            price=trade["price"],
            qty=qty,
            qdec=trade["qdec"],
        )

        if res and res.get("result"):
            data_obj = res.get("data", {}) or {}
            order_id = data_obj.get("orderId")

            positions[sym] = {
                "entry": trade["price"],
                "tp": trade["tp"],
                "sl": trade["sl"],
                "qty": float(qty),
                "order_qty_str": qty_str,
                "order_id": order_id,
                "pionex": trade["pionex"],
                "qdec": trade["qdec"],
            }

            tg_send(
                "<b>ORDER PLACED!</b>\n\n"
                f"{sym}/USDT BUY @ ${trade['price']}\n"
                f"Qty: {qty_str}\n"
                f"TP: ${trade['tp']}\n"
                f"SL: ${trade['sl']}\n"
                f"Size: ${TRADE_SIZE} USDT\n"
                f"Order ID: {order_id}\n\n"
                "Monitoring..."
            )
        else:
            err = None
            if isinstance(res, dict):
                err = res.get("message") or res.get("error") or res.get("code")
            if not err:
                err = str(res)

            tg_send(
                "<b>Order failed</b>\n\n"
                f"{err}\n\n"
                "Place manually on Pionex if needed."
            )

    elif action == "SKIP" and sym in pending:
        pending.pop(sym, None)
        tg_answer(cb_id, "Skipped")
        tg_send(f"Skipped {sym} - next signal coming...")

    else:
        tg_answer(cb_id, "Signal expired - wait for next one")


def check_btns(offset):
    updates = tg_updates(offset)
    if updates and updates.get("ok"):
        for u in updates.get("result", []):
            offset = u["update_id"] + 1
            cb = u.get("callback_query")
            if cb:
                handle(cb)
    return offset


# =========================
# MAIN LOOP
# =========================
def run():
    print("APEX Bot started")
    print("Telegram token set:", bool(TG_TOKEN))
    print("Telegram chat set:", bool(TG_CHAT))
    print("Pionex key length:", len(PX_KEY))
    print("Pionex secret length:", len(PX_SEC))

    tg_send(
        "<b>APEX Bot Online!</b>\n\n"
        "Monitoring: XRP, SUI, BTC, SOL, BNB, DOGE\n"
        f"Min confidence: {MIN_CONF}%\n"
        f"Trade size: ${TRADE_SIZE} USDT\n\n"
        "Signals come with CONFIRM and SKIP buttons."
    )

    offset = None
    last_scan_at = 0

    while True:
        try:
            offset = check_btns(offset)

            # Monitor positions for TP/SL notifications
            if positions:
                prices = get_prices()
                if prices:
                    for coin in COINS:
                        sym = coin["symbol"]
                        if sym not in positions:
                            continue

                        d = prices.get(coin["id"], {})
                        price = d.get("usd")
                        if not price:
                            continue

                        pos = positions[sym]

                        if price >= pos["tp"]:
                            pnl = round((pos["tp"] - pos["entry"]) * pos["qty"], 4)
                            tg_send(
                                "<b>TAKE PROFIT HIT</b>\n\n"
                                f"{sym} @ ${pos['tp']}\n"
                                f"Est. PnL: +${pnl} USDT\n\n"
                                "Note: this bot currently notifies TP/SL; "
                                "it does not auto-close the spot order."
                            )
                            del positions[sym]

                        elif price <= pos["sl"]:
                            pnl = round((pos["sl"] - pos["entry"]) * pos["qty"], 4)
                            tg_send(
                                "<b>STOP LOSS HIT</b>\n\n"
                                f"{sym} @ ${pos['sl']}\n"
                                f"Est. PnL: ${pnl} USDT\n\n"
                                "Note: this bot currently notifies TP/SL; "
                                "it does not auto-close the spot order."
                            )
                            del positions[sym]

            # Scan for new signals
            if time.time() - last_scan_at >= SCAN_EVERY_SECONDS:
                last_scan_at = time.time()
                print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Scanning...")

                prices = get_prices()
                if prices:
                    for coin in COINS:
                        sym = coin["symbol"]

                        if sym in positions or sym in pending:
                            continue

                        d = prices.get(coin["id"], {})
                        price = d.get("usd")
                        change = d.get("usd_24h_change", 0) or 0
                        high = d.get("usd_24h_high")
                        low = d.get("usd_24h_low")
                        vol = d.get("usd_24h_vol")

                        if not price:
                            continue

                        print(f"{sym}: ${price} {round(change, 2)}%")

                        sig = signal(price, change, high, low, vol)
                        if not sig or sig["conf"] < MIN_CONF:
                            continue

                        prev = last_signal.get(sym)
                        if (
                            prev
                            and prev["signal"] == sig["signal"]
                            and abs(prev.get("entry", 0) - price) / price < 0.005
                        ):
                            continue

                        sig["entry"] = price
                        last_signal[sym] = sig

                        pending[sym] = {
                            "signal": sig["signal"],
                            "price": price,
                            "tp": sig["tp"],
                            "sl": sig["sl"],
                            "pionex": coin["pionex"],
                            "qdec": coin["qdec"],
                        }

                        markup = {
                            "inline_keyboard": [[
                                {"text": "CONFIRM TRADE", "callback_data": f"CONFIRM_{sym}"},
                                {"text": "SKIP", "callback_data": f"SKIP_{sym}"}
                            ]]
                        }

                        tg_send(make_msg(coin, sig, price, change), markup)
                        print(f"{sym}: {sig['signal']} {sig['conf']}%")

            time.sleep(2)

        except KeyboardInterrupt:
            print("Bot stopped by user")
            break
        except Exception as e:
            print("Main loop error:", str(e))
            time.sleep(5)


if __name__ == "__main__":
    run()
