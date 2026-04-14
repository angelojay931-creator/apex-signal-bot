import os
import json
import time
import hmac
import uuid
import hashlib
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone

import requests

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
PX_KEY = os.environ.get("P_API_KEY", "").strip()
PX_SEC = os.environ.get("P_SECRET", "").strip()

TRADE_SIZE = Decimal("25")
MIN_CONF = 65
SCAN_EVERY_SECONDS = 30
PRICE_TIMEOUT = 15
HTTP_TIMEOUT = 15

last_signal = {}
pending = {}
positions = {}
watching = {}
symbol_rules_cache = {}

stats = {
    "total": 0,
    "tp_hit": 0,
    "sl_hit": 0,
    "profit_usdt": 0.0,
    "loss_usdt": 0.0,
}

COINS = [
    {"id": "bitcoin", "symbol": "BTC", "pair": "BTC/USDT", "pionex": "BTC_USDT", "dec": 0, "qdec": 5},
    {"id": "ethereum", "symbol": "ETH", "pair": "ETH/USDT", "pionex": "ETH_USDT", "dec": 1, "qdec": 4},
    {"id": "ripple", "symbol": "XRP", "pair": "XRP/USDT", "pionex": "XRP_USDT", "dec": 4, "qdec": 2},
    {"id": "binancecoin", "symbol": "BNB", "pair": "BNB/USDT", "pionex": "BNB_USDT", "dec": 2, "qdec": 3},
    {"id": "solana", "symbol": "SOL", "pair": "SOL/USDT", "pionex": "SOL_USDT", "dec": 2, "qdec": 2},
    {"id": "dogecoin", "symbol": "DOGE", "pair": "DOGE/USDT", "pionex": "DOGE_USDT", "dec": 5, "qdec": 1},
    {"id": "cardano", "symbol": "ADA", "pair": "ADA/USDT", "pionex": "ADA_USDT", "dec": 4, "qdec": 1},
    {"id": "tron", "symbol": "TRX", "pair": "TRX/USDT", "pionex": "TRX_USDT", "dec": 4, "qdec": 0},
    {"id": "avalanche-2", "symbol": "AVAX", "pair": "AVAX/USDT", "pionex": "AVAX_USDT", "dec": 2, "qdec": 2},
    {"id": "sui", "symbol": "SUI", "pair": "SUI/USDT", "pionex": "SUI_USDT", "dec": 4, "qdec": 2},
    {"id": "chainlink", "symbol": "LINK", "pair": "LINK/USDT", "pionex": "LINK_USDT", "dec": 3, "qdec": 2},
    {"id": "stellar", "symbol": "XLM", "pair": "XLM/USDT", "pionex": "XLM_USDT", "dec": 5, "qdec": 1},
    {"id": "litecoin", "symbol": "LTC", "pair": "LTC/USDT", "pionex": "LTC_USDT", "dec": 2, "qdec": 3},
    {"id": "polkadot", "symbol": "DOT", "pair": "DOT/USDT", "pionex": "DOT_USDT", "dec": 3, "qdec": 2},
    {"id": "uniswap", "symbol": "UNI", "pair": "UNI/USDT", "pionex": "UNI_USDT", "dec": 3, "qdec": 2},
    {"id": "near", "symbol": "NEAR", "pair": "NEAR/USDT", "pionex": "NEAR_USDT", "dec": 3, "qdec": 2},
    {"id": "aptos", "symbol": "APT", "pair": "APT/USDT", "pionex": "APT_USDT", "dec": 3, "qdec": 2},
    {"id": "internet-computer", "symbol": "ICP", "pair": "ICP/USDT", "pionex": "ICP_USDT", "dec": 2, "qdec": 2},
    {"id": "ethereum-classic", "symbol": "ETC", "pair": "ETC/USDT", "pionex": "ETC_USDT", "dec": 2, "qdec": 2},
    {"id": "filecoin", "symbol": "FIL", "pair": "FIL/USDT", "pionex": "FIL_USDT", "dec": 3, "qdec": 2},
    {"id": "injective-protocol", "symbol": "INJ", "pair": "INJ/USDT", "pionex": "INJ_USDT", "dec": 2, "qdec": 2},
    {"id": "optimism", "symbol": "OP", "pair": "OP/USDT", "pionex": "OP_USDT", "dec": 3, "qdec": 2},
    {"id": "arbitrum", "symbol": "ARB", "pair": "ARB/USDT", "pionex": "ARB_USDT", "dec": 4, "qdec": 2},
    {"id": "pepe", "symbol": "PEPE", "pair": "PEPE/USDT", "pionex": "PEPE_USDT", "dec": 8, "qdec": 0},
    {"id": "shiba-inu", "symbol": "SHIB", "pair": "SHIB/USDT", "pionex": "SHIB_USDT", "dec": 8, "qdec": 0},
]

session = requests.Session()


def now_ms():
    return int(time.time() * 1000)


def utc_now_str():
    return datetime.now(timezone.utc).strftime("%H:%M UTC")


def dec(value):
    return Decimal(str(value))


def fmt_decimal_clean(d):
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s else "0"


def decimals_from_step(step_value):
    try:
        d = dec(step_value).normalize()
        exponent = d.as_tuple().exponent
        return abs(exponent) if exponent < 0 else 0
    except Exception:
        return 0


def floor_to_step(value, step):
    v = dec(value)
    step_dec = dec(step)
    if step_dec <= 0:
        return fmt_decimal_clean(v)
    units = (v / step_dec).quantize(Decimal("1"), rounding=ROUND_DOWN)
    result = units * step_dec
    return fmt_decimal_clean(result)


def floor_to_decimals(value, decimals):
    if decimals < 0:
        decimals = 0
    quant = Decimal("1") if decimals == 0 else Decimal("1." + ("0" * decimals))
    d = dec(value).quantize(quant, rounding=ROUND_DOWN)
    return format(d, "f")


def fmt_price(price, dec_places):
    if price is None:
        return "$0"
    if dec_places == 0:
        return "$" + str(int(dec(price).quantize(Decimal("1"), rounding=ROUND_DOWN)))
    return "$" + format(dec(price).quantize(Decimal("1." + ("0" * dec_places))), "f")


def elapsed_str(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return str(seconds) + "s"
    if seconds < 3600:
        return str(seconds // 60) + "m " + str(seconds % 60) + "s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return str(h) + "h " + str(m) + "m"


def build_query_string(params):
    items = sorted((k, v) for k, v in params.items() if v is not None)
    return "&".join(f"{k}={v}" for k, v in items)


def pionex_sign(method, path, query_params, body=None):
    query_string = build_query_string(query_params)
    path_url = f"{path}?{query_string}" if query_string else path
    body_str = ""

    if method.upper() in {"POST", "DELETE"} and body is not None:
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    payload = f"{method.upper()}{path_url}{body_str}"
    signature = hmac.new(
        PX_SEC.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return signature, body_str


def tg_send(msg, markup=None):
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
            print("Telegram HTTP error:", r.status_code, r.text)
        elif not payload.get("ok", False):
            print("Telegram API error:", payload)

        return payload
    except Exception as e:
        print("TG send error:", str(e))
        return None


def tg_answer(cb_id, text):
    if not TG_TOKEN:
        return

    url = f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery"
    try:
        session.post(
            url,
            data={"callback_query_id": cb_id, "text": text},
            timeout=HTTP_TIMEOUT,
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


def px_public_get(path, params=None):
    url = f"https://api.pionex.com{path}"
    try:
        r = session.get(url, params=params or {}, timeout=HTTP_TIMEOUT)
        try:
            return r.json()
        except Exception:
            return {"result": False, "error": f"Non-JSON response: {r.text}"}
    except Exception as e:
        return {"result": False, "error": str(e)}


def px_get_book_ticker(symbol):
    res = px_public_get("/api/v1/market/bookTickers", {"symbol": symbol})
    if not res or not res.get("result"):
        return None

    data = res.get("data") or {}
    tickers = data.get("tickers") or []
    if not tickers:
        return None

    return tickers[0]


def px_private_request(method, path, query_params=None, body=None):
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


def px_order_limit_buy(symbol, price_str, qty_str):
    body = {
        "symbol": symbol,
        "side": "BUY",
        "type": "LIMIT",
        "price": price_str,
        "size": qty_str,
        "clientOrderId": f"apex-{uuid.uuid4().hex[:20]}",
        "IOC": False,
    }
    print("Submitting order:", body)
    res = px_private_request("POST", "/api/v1/trade/order", body=body)
    print("Pionex response:", res)
    return res


def px_get_symbol_rules(symbol):
    if symbol in symbol_rules_cache:
        return symbol_rules_cache[symbol]

    res = px_public_get("/api/v1/common/symbols", {"symbols": symbol})
    if not res or not res.get("result"):
        return None

    data = res.get("data") or {}
    symbols = data.get("symbols") or []
    if not symbols:
        return None

    info = symbols[0]
    symbol_rules_cache[symbol] = info
    return info


def calc_valid_limit_buy(symbol, price, fallback_pdec, fallback_qdec):
    price_dec = dec(price)
    if price_dec <= 0:
        return None, "Invalid price"

    rules = px_get_symbol_rules(symbol) or {}
    base_step = rules.get("baseStep")
    quote_step = rules.get("quoteStep")
    min_notional = rules.get("minNotional")
    min_size_limit = rules.get("minSizeLimit")

    if quote_step:
        price_str = floor_to_step(price_dec, quote_step)
        pdec = decimals_from_step(quote_step)
    else:
        price_str = floor_to_decimals(price_dec, fallback_pdec)
        pdec = fallback_pdec

    price_dec = dec(price_str)
    if price_dec <= 0:
        return None, "Rounded price became zero"

    raw_qty = TRADE_SIZE / price_dec

    if base_step:
        qty_str = floor_to_step(raw_qty, base_step)
        qdec = decimals_from_step(base_step)
    else:
        qty_str = floor_to_decimals(raw_qty, fallback_qdec)
        qdec = fallback_qdec

    qty = dec(qty_str)
    if qty <= 0:
        return None, "Calculated quantity is zero"

    notional = qty * price_dec

    if min_size_limit:
        try:
            if qty < dec(min_size_limit):
                return None, f"Size below minSizeLimit ({min_size_limit})"
        except Exception:
            pass

    if min_notional:
        try:
            if notional < dec(min_notional):
                return None, f"Amount below minNotional ({min_notional})"
        except Exception:
            pass

    return {
        "qty": qty,
        "qty_str": qty_str,
        "price_str": price_str,
        "notional": notional,
        "pdec": pdec,
        "qdec": qdec,
    }, None


def get_prices():
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
        if d and "bitcoin" in d and d["bitcoin"].get("usd"):
            print("CoinGecko OK BTC:", d["bitcoin"]["usd"])
            return d
    except Exception as e:
        print("CoinGecko error:", str(e))

    try:
        result = {}
        pairs = {
            "bitcoin": "BTCUSDT",
            "ethereum": "ETHUSDT",
            "ripple": "XRPUSDT",
            "binancecoin": "BNBUSDT",
            "solana": "SOLUSDT",
            "dogecoin": "DOGEUSDT",
            "cardano": "ADAUSDT",
            "tron": "TRXUSDT",
            "avalanche-2": "AVAXUSDT",
            "sui": "SUIUSDT",
            "chainlink": "LINKUSDT",
            "stellar": "XLMUSDT",
            "litecoin": "LTCUSDT",
            "polkadot": "DOTUSDT",
            "uniswap": "UNIUSDT",
            "near": "NEARUSDT",
            "aptos": "APTUSDT",
            "internet-computer": "ICPUSDT",
            "ethereum-classic": "ETCUSDT",
            "filecoin": "FILUSDT",
            "injective-protocol": "INJUSDT",
            "optimism": "OPUSDT",
            "arbitrum": "ARBUSDT",
            "pepe": "PEPEUSDT",
            "shiba-inu": "SHIBUSDT",
        }

        for gid, sym in pairs.items():
            r = session.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={sym}", timeout=10)
            d = r.json()
            if d and "lastPrice" in d:
                result[gid] = {
                    "usd": float(d["lastPrice"]),
                    "usd_24h_change": float(d["priceChangePercent"]),
                    "usd_24h_high": float(d["highPrice"]),
                    "usd_24h_low": float(d["lowPrice"]),
                    "usd_24h_vol": float(d["quoteVolume"]),
                }
            time.sleep(0.08)

        if result:
            print("Binance fallback OK")
            return result
    except Exception as e:
        print("Binance fallback error:", str(e))

    return None


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
        return {"signal": "BUY", "conf": min(92, 65 + score * 4), "tp": round(price * 1.030, 8), "sl": round(price * 0.985, 8)}
    if score >= 2:
        return {"signal": "BUY", "conf": min(78, 58 + score * 4), "tp": round(price * 1.020, 8), "sl": round(price * 0.988, 8)}
    if score <= -4:
        return {"signal": "SELL", "conf": min(90, 65 + abs(score) * 4), "tp": round(price * 0.970, 8), "sl": round(price * 1.015, 8)}
    if score <= -2:
        return {"signal": "SELL", "conf": min(75, 58 + abs(score) * 4), "tp": round(price * 0.980, 8), "sl": round(price * 1.012, 8)}

    return None


def make_msg(coin, sig, price, change):
    action = sig["signal"]
    dec_places = coin["dec"]
    sign = "+" if change >= 0 else ""
    conf = sig["conf"]
    bars = "#" * int(conf / 10) + "-" * (10 - int(conf / 10))

    return (
        "<b>APEX SIGNAL</b>\n"
        "==================\n"
        f"<b>{action} - {coin['pair']}</b>\n\n"
        f"Entry:       {fmt_price(price, dec_places)}\n"
        f"Take Profit: {fmt_price(sig['tp'], dec_places)}\n"
        f"Stop Loss:   {fmt_price(sig['sl'], dec_places)}\n"
        f"Trade Size:  ${TRADE_SIZE} USDT\n\n"
        f"24h Change:  {sign}{round(change, 2)}%\n"
        f"Confidence:  {conf}%\n"
        f"[{bars}]\n\n"
        f"Time: {utc_now_str()}\n"
        "==================\n"
        "Tap below to trade on Pionex"
    )


def monitor_watching(prices):
    to_remove = []
    now = time.time()

    for sym, w in list(watching.items()):
        coin_data = next((c for c in COINS if c["symbol"] == sym), None)
        if not coin_data:
            to_remove.append(sym)
            continue

        d = prices.get(coin_data["id"], {})
        price = d.get("usd")
        if not price:
            continue

        entry = w["entry"]
        tp = w["tp"]
        sl = w["sl"]
        sig_type = w["signal"]
        fired_at = w["fired_at"]
        elapsed = now - fired_at

        if elapsed > 86400:
            pct = round((price - entry) / entry * 100, 2)
            tg_send(
                f"<b>SIGNAL EXPIRED</b>\n\n"
                f"{sym} {sig_type} expired after {elapsed_str(elapsed)}\n"
                f"Entry: ${entry}\n"
                f"Current: ${round(price, 6)}\n"
                f"Change: {'+' if pct >= 0 else ''}{pct}%\n"
                f"TP: ${tp} | SL: ${sl}"
            )
            to_remove.append(sym)
            continue

        if sig_type == "BUY":
            if price >= tp:
                profit = round((tp - entry) / entry * 100, 2)
                est_usdt = round(float(TRADE_SIZE) * profit / 100, 2)
                stats["total"] += 1
                stats["tp_hit"] += 1
                stats["profit_usdt"] += est_usdt
                tg_send(
                    f"<b>SIGNAL RESULT ✅ TP HIT</b>\n\n"
                    f"{sym} BUY (not confirmed)\n"
                    f"Entry: ${entry}\n"
                    f"TP Hit: ${tp}\n"
                    f"Time: {elapsed_str(elapsed)}\n\n"
                    f"Would have made: +{profit}% (+${est_usdt} USDT)\n\n"
                    f"Win rate: {stats['tp_hit']}/{stats['total']} signals"
                )
                to_remove.append(sym)
            elif price <= sl:
                loss = round((entry - sl) / entry * 100, 2)
                est_usdt = round(float(TRADE_SIZE) * loss / 100, 2)
                stats["total"] += 1
                stats["sl_hit"] += 1
                stats["loss_usdt"] += est_usdt
                tg_send(
                    f"<b>SIGNAL RESULT ❌ SL HIT</b>\n\n"
                    f"{sym} BUY (not confirmed)\n"
                    f"Entry: ${entry}\n"
                    f"SL Hit: ${sl}\n"
                    f"Time: {elapsed_str(elapsed)}\n\n"
                    f"Would have lost: -{loss}% (-${est_usdt} USDT)\n\n"
                    f"Win rate: {stats['tp_hit']}/{stats['total']} signals"
                )
                to_remove.append(sym)

        elif sig_type == "SELL":
            if price <= tp:
                profit = round((entry - tp) / entry * 100, 2)
                est_usdt = round(float(TRADE_SIZE) * profit / 100, 2)
                stats["total"] += 1
                stats["tp_hit"] += 1
                stats["profit_usdt"] += est_usdt
                tg_send(
                    f"<b>SIGNAL RESULT ✅ TP HIT</b>\n\n"
                    f"{sym} SELL (not confirmed)\n"
                    f"Entry: ${entry}\n"
                    f"TP Hit: ${tp}\n"
                    f"Time: {elapsed_str(elapsed)}\n\n"
                    f"Would have made: +{profit}% (+${est_usdt} USDT)\n\n"
                    f"Win rate: {stats['tp_hit']}/{stats['total']} signals"
                )
                to_remove.append(sym)
            elif price >= sl:
                loss = round((sl - entry) / entry * 100, 2)
                est_usdt = round(float(TRADE_SIZE) * loss / 100, 2)
                stats["total"] += 1
                stats["sl_hit"] += 1
                stats["loss_usdt"] += est_usdt
                tg_send(
                    f"<b>SIGNAL RESULT ❌ SL HIT</b>\n\n"
                    f"{sym} SELL (not confirmed)\n"
                    f"Entry: ${entry}\n"
                    f"SL Hit: ${sl}\n"
                    f"Time: {elapsed_str(elapsed)}\n\n"
                    f"Would have lost: -{loss}% (-${est_usdt} USDT)\n\n"
                    f"Win rate: {stats['tp_hit']}/{stats['total']} signals"
                )
                to_remove.append(sym)

    for sym in to_remove:
        watching.pop(sym, None)


def handle(cb):
    data = cb.get("data", "")
    cb_id = cb.get("id", "")
    parts = data.split("_", 1)
    action = parts[0] if parts else ""
    sym = parts[1] if len(parts) > 1 else ""

    print("BTN:", action, sym)

    if action == "CONFIRM" and sym in pending:
        trade = pending.pop(sym)
        watching.pop(sym, None)

        if trade["signal"] != "BUY":
            tg_answer(cb_id, "Only BUY auto-order is enabled")
            tg_send(f"<b>{sym}</b> signal was SELL.\n\nThis bot currently auto-places BUY spot orders only.")
            return

        tg_answer(cb_id, "Placing order...")
        book = px_get_book_ticker(trade["pionex"])
        if book and book.get("askPrice"):
            order_price = book["askPrice"]
        else:
            order_price = trade["price"]

        calc, err = calc_valid_limit_buy(
            symbol=trade["pionex"],
            price=order_price,
            fallback_pdec=trade["pdec"],
            fallback_qdec=trade["qdec"],
        )

        if err:
            tg_send(f"<b>Order failed</b>\n\n{err}")
            return

        res = px_order_limit_buy(
            symbol=trade["pionex"],
            price_str=calc["price_str"],
            qty_str=calc["qty_str"],
        )

        if res and res.get("result"):
            data_obj = res.get("data", {}) or {}
            order_id = data_obj.get("orderId")
            positions[sym] = {
                "entry": float(calc["price_str"]),
                "tp": trade["tp"],
                "sl": trade["sl"],
                "qty": float(calc["qty"]),
                "order_id": order_id,
                "pionex": trade["pionex"],
            }
            tg_send(
                "<b>ORDER PLACED!</b>\n\n"
                f"{sym}/USDT BUY @ {calc['price_str']}\n"
                f"Qty: {calc['qty_str']}\n"
                f"Notional: ${round(float(calc['notional']), 4)}\n"
                f"TP: ${trade['tp']}\n"
                f"SL: ${trade['sl']}\n"
                f"Order ID: {order_id}\n\n"
                "Monitoring..."
            )
        else:
            err_msg = None
            if isinstance(res, dict):
                err_msg = res.get("message") or res.get("error") or str(res.get("code"))
            if not err_msg:
                err_msg = str(res)
            tg_send(f"<b>Order failed</b>\n\n{err_msg}\n\nPlace manually on Pionex if needed.")

    elif action == "SKIP" and sym in pending:
        trade = pending.pop(sym, None)
        tg_answer(cb_id, "Skipped - watching for result")
        if trade:
            watching[sym] = {
                "signal": trade["signal"],
                "entry": trade["price"],
                "tp": trade["tp"],
                "sl": trade["sl"],
                "fired_at": time.time(),
            }
        tg_send(f"Skipped {sym} - watching signal result in background...")

    else:
        if sym in pending:
            trade = pending.pop(sym)
            watching[sym] = {
                "signal": trade["signal"],
                "entry": trade["price"],
                "tp": trade["tp"],
                "sl": trade["sl"],
                "fired_at": time.time(),
            }
        tg_answer(cb_id, "Signal expired - watching result")


def check_btns(offset):
    updates = tg_updates(offset)
    if updates and updates.get("ok"):
        for u in updates.get("result", []):
            offset = u["update_id"] + 1
            cb = u.get("callback_query")
            if cb:
                handle(cb)
    return offset


def preload_symbol_rules():
    print("Preloading symbol rules from Pionex...")
    for coin in COINS:
        try:
            px_get_symbol_rules(coin["pionex"])
            time.sleep(0.05)
        except Exception as e:
            print("Rule preload error:", coin["pionex"], str(e))
    print("Symbol rules loaded!")


def run():
    print("APEX Bot started")
    print("Telegram token set:", bool(TG_TOKEN))
    print("Pionex key length:", len(PX_KEY))
    print("Pionex secret length:", len(PX_SEC))
    print("Monitoring", len(COINS), "coins")

    preload_symbol_rules()

    tg_send(
        "<b>APEX Bot Online!</b>\n\n"
        f"Monitoring {len(COINS)} coins\n"
        f"Min confidence: {MIN_CONF}%\n"
        f"Trade size: ${TRADE_SIZE} USDT\n\n"
        "Skipped signals are tracked automatically!\n"
        "You will see TP/SL results even without trading."
    )

    offset = None
    last_scan_at = 0

    while True:
        try:
            offset = check_btns(offset)

            prices = get_prices()

            if prices:
                if watching:
                    monitor_watching(prices)

                if positions:
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
                                f"<b>TAKE PROFIT HIT</b>\n\n{sym} @ ${pos['tp']}\n"
                                f"Est. PnL: +${pnl} USDT\n\nClose manually on Pionex."
                            )
                            del positions[sym]
                        elif price <= pos["sl"]:
                            pnl = round((pos["sl"] - pos["entry"]) * pos["qty"], 4)
                            tg_send(
                                f"<b>STOP LOSS HIT</b>\n\n{sym} @ ${pos['sl']}\n"
                                f"Est. PnL: ${pnl} USDT\n\nClose manually on Pionex."
                            )
                            del positions[sym]

            if time.time() - last_scan_at >= SCAN_EVERY_SECONDS:
                last_scan_at = time.time()
                print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Scanning {len(COINS)} coins...")

                scan_prices = get_prices()
                if scan_prices:
                    for coin in COINS:
                        sym = coin["symbol"]
                        if sym in positions or sym in pending or sym in watching:
                            continue

                        d = scan_prices.get(coin["id"], {})
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
                        if prev and prev["signal"] == sig["signal"] and abs(prev.get("entry", 0) - price) / price < 0.005:
                            continue

                        sig["entry"] = price
                        last_signal[sym] = sig
                        pending[sym] = {
                            "signal": sig["signal"],
                            "price": price,
                            "tp": sig["tp"],
                            "sl": sig["sl"],
                            "pionex": coin["pionex"],
                            "pdec": coin["dec"],
                            "qdec": coin["qdec"],
                        }

                        markup = {
                            "inline_keyboard": [[
                                {"text": "CONFIRM TRADE", "callback_data": f"CONFIRM_{sym}"},
                                {"text": "SKIP", "callback_data": f"SKIP_{sym}"},
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
