"""
APEX Bybit Leverage Bot — PAPER TRADING MODE
==============================================
100% simulated. No real orders placed.
Uses live prices from Binance/CoinGecko.
Tracks real P&L as if you were trading 3x leverage on Bybit.

When ready to go live:
  1. Set PAPER_TRADING = False
  2. Add BYBIT_API_KEY + BYBIT_SECRET to Railway env vars
  3. Redeploy

Railway env vars needed (paper mode):
  TELEGRAM_TOKEN
  TELEGRAM_CHAT_ID
"""

import os
import json
import time
import hmac
import hashlib
import threading
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify

# ─────────────────────────── FLASK API ────────────────────────
app = Flask(__name__)

@app.route("/")
def health():
    return "APEX Paper Bot running!", 200

@app.route("/data")
def get_data():
    """Dashboard reads from this endpoint."""
    net_pnl  = round(stats["profit_usdt"] - stats["loss_usdt"], 2)
    win_rate = round(stats["tp_hit"] / stats["total"] * 100, 1) if stats["total"] > 0 else 0

    open_pos = {}
    for sym, pos in positions.items():
        open_pos[sym] = {
            "sym":       sym,
            "direction": pos["direction"],
            "entry":     pos["entry"],
            "tp1":       pos["tp1"],
            "tp2":       pos["tp2"],
            "tp3":       pos["tp3"],
            "tp4":       pos["tp4"],
            "sl":        pos["sl"],
            "tpHit":     pos["tp_hit"],
            "breakeven": pos.get("breakeven", False),
            "margin":    pos.get("margin", float(TRADE_SIZE)),
            "currentPnl": pos.get("currentPnl", 0),
            "openTime":  pos.get("opened_at", 0),
        }

    response = jsonify({
        "balance":       paper_balance,
        "startBalance":  PAPER_BALANCE,
        "netPnl":        net_pnl,
        "winRate":       win_rate,
        "totalTrades":   stats["total"],
        "tpHits":        stats["tp_hit"],
        "slHits":        stats["sl_hit"],
        "profitUsdt":    stats["profit_usdt"],
        "lossUsdt":      stats["loss_usdt"],
        "openPositions": open_pos,
        "closedTrades":  stats.get("trades_list", [])[-50:],
        "pnlHistory":    stats.get("pnl_history", [PAPER_BALANCE]),
        "leverage":      LEVERAGE,
        "tradeSize":     float(TRADE_SIZE),
        "timestamp":     utc_now_str(),
    })
    response.headers.add("Access-Control-Allow-Origin", "*")
    return response

def start_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


PAPER_TRADING = True           # ← Flip to False for real money
PAPER_BALANCE = 200.0          # Simulated starting USDT balance

BYBIT_KEY    = os.environ.get("BYBIT_API_KEY", "").strip()
BYBIT_SECRET = os.environ.get("BYBIT_SECRET", "").strip()
TG_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT      = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

TRADE_SIZE         = Decimal("25")   # USDT margin per trade
LEVERAGE           = 3               # 3x leverage
MIN_CONF           = 85
SCAN_EVERY_SECONDS = 30
HTTP_TIMEOUT       = 15
MAX_OPEN_TRADES    = 5

BLOCKED_COINS = {"ENJ"}

# ─────────────────────────── STATE ────────────────────────────
last_signal = {}
positions   = {}   # active simulated positions
watching    = {}   # signals being tracked
pre_warned  = {}

paper_balance = PAPER_BALANCE  # current simulated balance

stats = {
    "total":        0,
    "tp_hit":       0,
    "sl_hit":       0,
    "profit_usdt":  0.0,
    "loss_usdt":    0.0,
    "trades_list":  [],
    "pnl_history":  [],
}

# ─────────────────────────── COINS ────────────────────────────
COINS = [
    {"id": "bitcoin",                   "symbol": "BTC",    "bybit": "BTCUSDT"},
    {"id": "ethereum",                  "symbol": "ETH",    "bybit": "ETHUSDT"},
    {"id": "ripple",                    "symbol": "XRP",    "bybit": "XRPUSDT"},
    {"id": "binancecoin",               "symbol": "BNB",    "bybit": "BNBUSDT"},
    {"id": "solana",                    "symbol": "SOL",    "bybit": "SOLUSDT"},
    {"id": "dogecoin",                  "symbol": "DOGE",   "bybit": "DOGEUSDT"},
    {"id": "cardano",                   "symbol": "ADA",    "bybit": "ADAUSDT"},
    {"id": "tron",                      "symbol": "TRX",    "bybit": "TRXUSDT"},
    {"id": "avalanche-2",               "symbol": "AVAX",   "bybit": "AVAXUSDT"},
    {"id": "sui",                       "symbol": "SUI",    "bybit": "SUIUSDT"},
    {"id": "chainlink",                 "symbol": "LINK",   "bybit": "LINKUSDT"},
    {"id": "stellar",                   "symbol": "XLM",    "bybit": "XLMUSDT"},
    {"id": "litecoin",                  "symbol": "LTC",    "bybit": "LTCUSDT"},
    {"id": "polkadot",                  "symbol": "DOT",    "bybit": "DOTUSDT"},
    {"id": "uniswap",                   "symbol": "UNI",    "bybit": "UNIUSDT"},
    {"id": "near",                      "symbol": "NEAR",   "bybit": "NEARUSDT"},
    {"id": "aptos",                     "symbol": "APT",    "bybit": "APTUSDT"},
    {"id": "internet-computer",         "symbol": "ICP",    "bybit": "ICPUSDT"},
    {"id": "ethereum-classic",          "symbol": "ETC",    "bybit": "ETCUSDT"},
    {"id": "filecoin",                  "symbol": "FIL",    "bybit": "FILUSDT"},
    {"id": "injective-protocol",        "symbol": "INJ",    "bybit": "INJUSDT"},
    {"id": "optimism",                  "symbol": "OP",     "bybit": "OPUSDT"},
    {"id": "arbitrum",                  "symbol": "ARB",    "bybit": "ARBUSDT"},
    {"id": "pepe",                      "symbol": "PEPE",   "bybit": "PEPEUSDT"},
    {"id": "shiba-inu",                 "symbol": "SHIB",   "bybit": "SHIBUSDT"},
    {"id": "kaspa",                     "symbol": "KAS",    "bybit": "KASUSDT"},
    {"id": "bonk",                      "symbol": "BONK",   "bybit": "BONKUSDT"},
    {"id": "celestia",                  "symbol": "TIA",    "bybit": "TIAUSDT"},
    {"id": "sei-network",               "symbol": "SEI",    "bybit": "SEIUSDT"},
    {"id": "starknet",                  "symbol": "STRK",   "bybit": "STRKUSDT"},
    {"id": "the-graph",                 "symbol": "GRT",    "bybit": "GRTUSDT"},
    {"id": "render-token",              "symbol": "RENDER", "bybit": "RENDERUSDT"},
    {"id": "immutable-x",               "symbol": "IMX",    "bybit": "IMXUSDT"},
    {"id": "thorchain",                 "symbol": "RUNE",   "bybit": "RUNEUSDT"},
    {"id": "mantle",                    "symbol": "MNT",    "bybit": "MNTUSDT"},
    {"id": "ondo-finance",              "symbol": "ONDO",   "bybit": "ONDOUSDT"},
    {"id": "raydium",                   "symbol": "RAY",    "bybit": "RAYUSDT"},
    {"id": "aave",                      "symbol": "AAVE",   "bybit": "AAVEUSDT"},
    {"id": "curve-dao-token",           "symbol": "CRV",    "bybit": "CRVUSDT"},
    {"id": "lido-dao",                  "symbol": "LDO",    "bybit": "LDOUSDT"},
    {"id": "enjincoin",                 "symbol": "ENJ",    "bybit": "ENJUSDT"},
    {"id": "ankr",                      "symbol": "ANKR",   "bybit": "ANKRUSDT"},
    {"id": "fetch-ai",                  "symbol": "FET",    "bybit": "FETUSDT"},
    {"id": "matic-network",             "symbol": "POL",    "bybit": "POLUSDT"},
    {"id": "bittensor",                 "symbol": "TAO",    "bybit": "TAOUSDT"},
    {"id": "ronin",                     "symbol": "RON",    "bybit": "RONUSDT"},
    {"id": "terra-luna-2",              "symbol": "LUNA",   "bybit": "LUNAUSDT"},
    {"id": "kava",                      "symbol": "KAVA",   "bybit": "KAVAUSDT"},
    {"id": "iota",                      "symbol": "IOTA",   "bybit": "IOTAUSDT"},
    {"id": "neo",                       "symbol": "NEO",    "bybit": "NEOUSDT"},
    {"id": "dash",                      "symbol": "DASH",   "bybit": "DASHUSDT"},
    {"id": "zcash",                     "symbol": "ZEC",    "bybit": "ZECUSDT"},
    {"id": "sushi",                     "symbol": "SUSHI",  "bybit": "SUSHIUSDT"},
    {"id": "eos",                       "symbol": "EOS",    "bybit": "EOSUSDT"},
    {"id": "ontology",                  "symbol": "ONT",    "bybit": "ONTUSDT"},
    {"id": "waves",                     "symbol": "WAVES",  "bybit": "WAVESUSDT"},
    {"id": "gmx",                       "symbol": "GMX",    "bybit": "GMXUSDT"},
    {"id": "dydx-chain",                "symbol": "DYDX",   "bybit": "DYDXUSDT"},
    {"id": "pendle",                    "symbol": "PENDLE", "bybit": "PENDLEUSDT"},
    {"id": "worldcoin-wld",             "symbol": "WLD",    "bybit": "WLDUSDT"},
    {"id": "jupiter-exchange-solana",   "symbol": "JUP",    "bybit": "JUPUSDT"},
    {"id": "dogwifcoin",                "symbol": "WIF",    "bybit": "WIFUSDT"},
]

session = requests.Session()


# ─────────────────────────── HELPERS ──────────────────────────
def utc_now_str():
    return datetime.now(timezone.utc).strftime("%H:%M UTC")


def elapsed_str(seconds):
    seconds = int(seconds)
    if seconds < 60:   return f"{seconds}s"
    if seconds < 3600: return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def fmt_p(price, decimals=None):
    if price is None:
        return "N/A"
    if decimals is None:
        if price >= 1000: decimals = 1
        elif price >= 100: decimals = 2
        elif price >= 1:   decimals = 4
        elif price >= 0.01: decimals = 5
        else:               decimals = 8
    return f"${price:.{decimals}f}"


# ──────────────────────── RSI / EMA ───────────────────────────
def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(diff if diff > 0 else 0.0)
        losses.append(abs(diff) if diff < 0 else 0.0)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)


def calc_ema(closes, period):
    if len(closes) < period:
        return None
    k   = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 8)


def calc_volume_ratio(volumes):
    if len(volumes) < 2:
        return 1.0
    avg = sum(volumes[:-1]) / len(volumes[:-1])
    return round(volumes[-1] / avg, 2) if avg > 0 else 1.0


def get_candles(symbol_usdt, interval="1h", limit=60):
    try:
        r = session.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol_usdt, "interval": interval, "limit": limit},
            timeout=10,
        )
        data = r.json()
        if not isinstance(data, list):
            return None
        return [{"close": float(c[4]), "volume": float(c[5])} for c in data]
    except Exception as e:
        print(f"  Candle error {symbol_usdt}: {e}")
        return None


def get_ta(symbol):
    candles = get_candles(symbol + "USDT", "1h", 60)
    if not candles or len(candles) < 20:
        return None
    closes  = [c["close"]  for c in candles]
    volumes = [c["volume"] for c in candles]
    return {
        "rsi":       calc_rsi(closes, 14),
        "ema20":     calc_ema(closes, 20),
        "ema50":     calc_ema(closes, 50) if len(closes) >= 50 else None,
        "vol_ratio": calc_volume_ratio(volumes),
    }


# ──────────────────────── PRICES ──────────────────────────────
def get_prices():
    try:
        ids = ",".join(c["id"] for c in COINS)
        r   = session.get(
            "https://api.coingecko.com/api/v3/simple/price"
            f"?ids={ids}&vs_currencies=usd"
            "&include_24hr_change=true&include_24hr_high=true"
            "&include_24hr_low=true&include_24hr_vol=true",
            timeout=15,
        )
        d = r.json()
        if d and d.get("bitcoin", {}).get("usd"):
            return d
    except Exception as e:
        print("CoinGecko error:", e)

    try:
        result = {}
        for coin in COINS:
            try:
                r = session.get(
                    f"https://api.binance.com/api/v3/ticker/24hr?symbol={coin['symbol']}USDT",
                    timeout=10,
                )
                d = r.json()
                if "lastPrice" in d:
                    result[coin["id"]] = {
                        "usd":            float(d["lastPrice"]),
                        "usd_24h_change": float(d["priceChangePercent"]),
                        "usd_24h_high":   float(d["highPrice"]),
                        "usd_24h_low":    float(d["lowPrice"]),
                        "usd_24h_vol":    float(d["quoteVolume"]),
                    }
            except Exception:
                pass
            time.sleep(0.05)
        return result if result else None
    except Exception as e:
        print("Binance fallback error:", e)
    return None


# ──────────────────────── SIGNAL ENGINE ───────────────────────
def calc_levels(price, direction, rsi, vol_ratio):
    base = 0.025
    if vol_ratio and vol_ratio > 3:    base = 0.042
    elif vol_ratio and vol_ratio > 2:  base = 0.034
    elif rsi and (rsi < 25 or rsi > 75): base = 0.036

    if direction == "BUY":
        tp1 = round(price * (1 + base * 0.40), 8)
        tp2 = round(price * (1 + base * 0.70), 8)
        tp3 = round(price * (1 + base * 1.00), 8)
        tp4 = round(price * (1 + base * 1.50), 8)
        sl  = round(price * (1 - base * 0.60), 8)
    else:
        tp1 = round(price * (1 - base * 0.40), 8)
        tp2 = round(price * (1 - base * 0.70), 8)
        tp3 = round(price * (1 - base * 1.00), 8)
        tp4 = round(price * (1 - base * 1.50), 8)
        sl  = round(price * (1 + base * 0.60), 8)

    tp_pcts = [
        round(abs(tp1 - price) / price * 100, 2),
        round(abs(tp2 - price) / price * 100, 2),
        round(abs(tp3 - price) / price * 100, 2),
        round(abs(tp4 - price) / price * 100, 2),
    ]
    return {"tp1": tp1, "tp2": tp2, "tp3": tp3, "tp4": tp4, "sl": sl, "tp_pcts": tp_pcts}


def build_signal(price, change, high, low, vol, ta):
    if not price:
        return None
    high = high or price * 1.02
    low  = low  or price * 0.98
    rng  = high - low
    pos  = (price - low) / rng if rng > 0 else 0.5
    score = 0

    if change > 8:    score += 6
    elif change > 6:  score += 5
    elif change > 4:  score += 4
    elif change > 2:  score += 2
    elif change < -8:  score -= 6
    elif change < -6:  score -= 5
    elif change < -4:  score -= 4
    elif change < -2:  score -= 2
    else: return None

    if pos < 0.15:   score += 4
    elif pos < 0.25: score += 3
    elif pos > 0.90: score -= 3
    elif pos > 0.80: score -= 2

    if vol and vol > 2_000_000_000:   score += 3
    elif vol and vol > 1_000_000_000: score += 2
    elif vol and vol > 500_000_000:   score += 1
    else:                              score -= 2

    rsi       = (ta or {}).get("rsi")
    ema20     = (ta or {}).get("ema20")
    ema50     = (ta or {}).get("ema50")
    vol_ratio = (ta or {}).get("vol_ratio", 1.0)

    if score > 0 and rsi and rsi > 75:
        print(f"    RSI veto BUY ({rsi:.1f})")
        return None
    if score < 0 and rsi and rsi < 25:
        print(f"    RSI veto SELL ({rsi:.1f})")
        return None

    if score > 0 and rsi:
        if rsi < 40:   score += 2
        elif rsi < 50: score += 1
    if score < 0 and rsi:
        if rsi > 60:   score -= 2
        elif rsi > 50: score -= 1

    if ema20 and ema50:
        if score > 0 and ema20 > ema50: score += 1
        if score < 0 and ema20 < ema50: score -= 1

    if vol_ratio and vol_ratio > 3:
        score = score + 2 if score > 0 else score - 2
    elif vol_ratio and vol_ratio > 2:
        score = score + 1 if score > 0 else score - 1

    if score >= 7:
        conf = min(95, 70 + score * 3); direction = "BUY"
    elif score >= 5:
        conf = min(88, 65 + score * 3); direction = "BUY"
    elif score <= -7:
        conf = min(95, 70 + abs(score) * 3); direction = "SELL"
    elif score <= -5:
        conf = min(88, 65 + abs(score) * 3); direction = "SELL"
    else:
        return None

    levels = calc_levels(price, direction, rsi, vol_ratio)
    return {
        "signal":    direction,
        "conf":      conf,
        "rsi":       rsi,
        "ema20":     ema20,
        "ema50":     ema50,
        "vol_ratio": vol_ratio,
        "tp1":       levels["tp1"],
        "tp2":       levels["tp2"],
        "tp3":       levels["tp3"],
        "tp4":       levels["tp4"],
        "sl":        levels["sl"],
        "tp_pcts":   levels["tp_pcts"],
    }


# ─────────────────────────── TELEGRAM ─────────────────────────
def tg_send(msg, markup=None):
    if not TG_TOKEN or not TG_CHAT:
        print("TG:", msg[:80])
        return None
    data = {
        "chat_id":                  TG_CHAT,
        "text":                     msg,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    if markup:
        data["reply_markup"] = json.dumps(markup, separators=(",", ":"))
    try:
        r = session.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=data, timeout=HTTP_TIMEOUT,
        )
        payload = r.json()
        if not payload.get("ok"):
            print("TG error:", payload)
        return payload
    except Exception as e:
        print("TG send error:", e)
        return None


def tg_updates(offset=None):
    params = {"timeout": 1, "allowed_updates": "[]"}
    if offset is not None:
        params["offset"] = offset
    try:
        r = session.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params=params, timeout=5,
        )
        return r.json()
    except Exception:
        return None


def check_btns(offset):
    updates = tg_updates(offset)
    if updates and updates.get("ok"):
        for u in updates.get("result", []):
            offset = u["update_id"] + 1
    return offset


# ─────────────────────────── MESSAGES ─────────────────────────
def make_pre_warn(coin, direction, price):
    arrow = "📈" if direction == "BUY" else "📉"
    return (
        f"<b>⚠️ GET READY — {coin['symbol']}/USDT</b>\n\n"
        f"{arrow} Potential <b>{direction}</b> forming\n"
        f"Price: {fmt_p(price)}\n\n"
        "<i>Full signal incoming shortly...</i>"
    )


def make_signal_msg(coin, sig, price, change):
    action    = sig["signal"]
    sign      = "+" if change >= 0 else ""
    conf      = sig["conf"]
    bars      = "#" * int(conf / 10) + "-" * (10 - int(conf / 10))
    rsi       = sig.get("rsi")
    ema20     = sig.get("ema20")
    ema50     = sig.get("ema50")
    vol_ratio = sig.get("vol_ratio", 1.0)
    tp_pcts   = sig.get("tp_pcts", [0, 0, 0, 0])
    arrow     = "🟢" if action == "BUY" else "🔴"
    side_word = "LONG" if action == "BUY" else "SHORT"

    rsi_str = f"{rsi:.1f}" if rsi else "N/A"
    ema_str = ("↑ Uptrend" if ema20 > ema50 else "↓ Downtrend") if (ema20 and ema50) else "N/A"
    vol_str = f"{vol_ratio:.1f}x avg" if vol_ratio else "N/A"
    lev_ret = [round(p * LEVERAGE, 1) for p in tp_pcts]

    notional = float(TRADE_SIZE) * LEVERAGE

    return (
        f"<b>📝 PAPER TRADE — APEX SIGNAL</b>\n"
        f"══════════════════════════════\n"
        f"{arrow} <b>{side_word} — {coin['symbol']}/USDT</b>\n\n"
        f"⚙️ {LEVERAGE}x Leverage | ${float(TRADE_SIZE)} margin → ${notional:.0f} exposure\n\n"
        f"Entry:     {fmt_p(price)}\n"
        f"Target 1:  {fmt_p(sig['tp1'])}  (+{tp_pcts[0]}% | {lev_ret[0]}% levered)\n"
        f"Target 2:  {fmt_p(sig['tp2'])}  (+{tp_pcts[1]}% | {lev_ret[1]}% levered)\n"
        f"Target 3:  {fmt_p(sig['tp3'])}  (+{tp_pcts[2]}% | {lev_ret[2]}% levered)\n"
        f"Target 4:  {fmt_p(sig['tp4'])}  (+{tp_pcts[3]}% | {lev_ret[3]}% levered)\n"
        f"Stop Loss: {fmt_p(sig['sl'])}\n\n"
        f"📊 Indicators:\n"
        f"RSI(14):   {rsi_str}\n"
        f"EMA trend: {ema_str}\n"
        f"Volume:    {vol_str}\n"
        f"24h:       {sign}{round(change, 2)}%\n\n"
        f"Confidence: {conf}%  [{bars}]\n\n"
        f"🤖 <i>Paper trade auto-entered instantly</i>\n"
        f"══════════════════════════════\n"
        f"Time: {utc_now_str()}"
    )


def make_tp_msg(sym, direction, tp_num, entry, tp_price, elapsed, pnl_usdt, new_sl=None):
    arrow     = "🟢" if direction == "BUY" else "🔴"
    side_word = "LONG" if direction == "BUY" else "SHORT"
    sl_note   = f"\n💡 SL → {fmt_p(new_sl)} (breakeven)" if tp_num == 1 and new_sl else \
                f"\n💡 SL trailed to TP{tp_num - 1}" if tp_num > 1 and new_sl else ""
    win_rate  = round(stats["tp_hit"] / stats["total"] * 100, 1) if stats["total"] > 0 else 0
    net_pnl   = round(stats["profit_usdt"] - stats["loss_usdt"], 2)
    net_sign  = "+" if net_pnl >= 0 else ""

    return (
        f"<b>✅ TP{tp_num} HIT — {sym} {side_word}</b> {arrow}\n\n"
        f"[PAPER TRADE]\n"
        f"Entry: {fmt_p(entry)} → TP{tp_num}: {fmt_p(tp_price)}\n"
        f"Time: {elapsed_str(elapsed)}\n"
        f"Est. +${pnl_usdt:.2f} USDT (25% of position){sl_note}\n\n"
        f"📊 Session stats:\n"
        f"Win rate: {stats['tp_hit']}/{stats['total']} = {win_rate}%\n"
        f"Net P&L: {net_sign}${abs(net_pnl):.2f} USDT\n"
        f"Balance: ${paper_balance:.2f} USDT"
    )


def make_sl_msg(sym, direction, entry, sl_price, elapsed, pnl_usdt, breakeven=False):
    side_word = "LONG" if direction == "BUY" else "SHORT"
    be_str    = " (at breakeven — no loss!)" if breakeven else ""
    sign      = "+" if breakeven else "-"
    win_rate  = round(stats["tp_hit"] / stats["total"] * 100, 1) if stats["total"] > 0 else 0
    net_pnl   = round(stats["profit_usdt"] - stats["loss_usdt"], 2)
    net_sign  = "+" if net_pnl >= 0 else ""

    return (
        f"<b>{'✅' if breakeven else '❌'} SL HIT{be_str} — {sym} {side_word}</b>\n\n"
        f"[PAPER TRADE]\n"
        f"Entry: {fmt_p(entry)} → SL: {fmt_p(sl_price)}\n"
        f"Time: {elapsed_str(elapsed)}\n"
        f"Est. {sign}${pnl_usdt:.2f} USDT\n\n"
        f"📊 Session stats:\n"
        f"Win rate: {stats['tp_hit']}/{stats['total']} = {win_rate}%\n"
        f"Net P&L: {net_sign}${abs(net_pnl):.2f} USDT\n"
        f"Balance: ${paper_balance:.2f} USDT"
    )


# ──────────────────────── PAPER EXECUTE ───────────────────────
def paper_execute(coin, sig, price):
    """Simulate opening a leveraged position instantly."""
    global paper_balance
    sym       = coin["symbol"]
    direction = sig["signal"]
    notional  = float(TRADE_SIZE) * LEVERAGE
    qty       = round(notional / price, 6)

    if paper_balance < float(TRADE_SIZE):
        tg_send(
            f"<b>⚠️ Paper balance too low — {sym}</b>\n\n"
            f"Balance: ${paper_balance:.2f} USDT\n"
            f"Required margin: ${float(TRADE_SIZE)} USDT\n\n"
            f"Skipping trade."
        )
        return False

    paper_balance -= float(TRADE_SIZE)  # Lock margin
    stats["pnl_history"].append(round(paper_balance, 2))

    positions[sym] = {
        "direction":  direction,
        "entry":      price,
        "qty":        qty,
        "margin":     float(TRADE_SIZE),
        "sl":         sig["sl"],
        "tp1":        sig["tp1"],
        "tp2":        sig["tp2"],
        "tp3":        sig["tp3"],
        "tp4":        sig["tp4"],
        "tp_pcts":    sig["tp_pcts"],
        "tp_hit":     0,
        "breakeven":  False,
        "opened_at":  time.time(),
    }

    side_word = "LONG" if direction == "BUY" else "SHORT"
    lev_ret   = [round(p * LEVERAGE, 1) for p in sig["tp_pcts"]]

    tg_send(
        f"<b>📝 PAPER TRADE ENTERED — {sym}</b>\n\n"
        f"{'🟢' if direction == 'BUY' else '🔴'} <b>{side_word} {sym}/USDT</b>\n\n"
        f"Entry:     {fmt_p(price)}\n"
        f"Margin:    ${float(TRADE_SIZE)} USDT\n"
        f"Exposure:  ${notional:.0f} USDT ({LEVERAGE}x)\n"
        f"Qty:       {qty}\n\n"
        f"TP1: {fmt_p(sig['tp1'])}  ({lev_ret[0]}% levered)\n"
        f"TP2: {fmt_p(sig['tp2'])}  ({lev_ret[1]}% levered)\n"
        f"TP3: {fmt_p(sig['tp3'])}  ({lev_ret[2]}% levered)\n"
        f"TP4: {fmt_p(sig['tp4'])}  ({lev_ret[3]}% levered)\n"
        f"SL:  {fmt_p(sig['sl'])}\n\n"
        f"💰 Paper balance: ${paper_balance:.2f} USDT\n"
        f"Open trades: {len(positions)}\n\n"
        f"🤖 Bot monitors: auto TP/SL + trailing stop"
    )
    print(f"  📝 Paper trade: {direction} {sym} @ {price} qty={qty}")
    return True


# ──────────────────────── POSITION MONITOR ────────────────────
def monitor_positions(prices):
    """Check all paper positions against live prices."""
    global paper_balance
    to_remove = []

    for sym, pos in list(positions.items()):
        coin_data = next((c for c in COINS if c["symbol"] == sym), None)
        if not coin_data:
            to_remove.append(sym)
            continue

        d     = (prices.get(coin_data["id"]) or {})
        price = d.get("usd")
        if not price:
            continue

        direction = pos["direction"]
        entry     = pos["entry"]
        sl        = pos["sl"]
        tp_hit    = pos["tp_hit"]
        elapsed   = time.time() - pos["opened_at"]
        tp_levels = [pos["tp1"], pos["tp2"], pos["tp3"], pos["tp4"]]

        # ── SL check ──
        sl_hit = (direction == "BUY" and price <= sl) or \
                 (direction == "SELL" and price >= sl)

        if sl_hit:
            # Calculate loss on remaining position (after any partial closes)
            remaining_pct = 1.0 - (tp_hit * 0.25)
            price_move    = abs(sl - entry) / entry * 100
            is_profit     = (direction == "BUY" and sl >= entry) or \
                            (direction == "SELL" and sl <= entry)
            pnl_usdt      = round(float(TRADE_SIZE) * LEVERAGE * price_move / 100 * remaining_pct, 2)
            remaining_margin = float(TRADE_SIZE) * remaining_pct

            if is_profit:
                stats["profit_usdt"] += pnl_usdt
                paper_balance += remaining_margin + pnl_usdt
            else:
                stats["loss_usdt"] += pnl_usdt
                paper_balance += max(0, remaining_margin - pnl_usdt)
            stats["pnl_history"].append(round(paper_balance, 2))

            stats["total"] += 1
            stats["sl_hit"] += 1
            stats["trades_list"].append({
                "sym": sym, "direction": direction, "result": "SL",
                "pnl": pnl_usdt if is_profit else -pnl_usdt,
                "time": utc_now_str(),
            })
            tg_send(make_sl_msg(sym, direction, entry, sl, elapsed, pnl_usdt, pos.get("breakeven")))
            to_remove.append(sym)
            continue

        # ── TP check ──
        if tp_hit >= 4:
            to_remove.append(sym)
            continue

        next_tp = tp_levels[tp_hit]
        tp_reached = (direction == "BUY" and price >= next_tp) or \
                     (direction == "SELL" and price <= next_tp)

        if tp_reached:
            tp_num    = tp_hit + 1
            pnl_pct   = abs(next_tp - entry) / entry * 100
            pnl_usdt  = round(float(TRADE_SIZE) * LEVERAGE * pnl_pct / 100 * 0.25, 2)
            quarter_margin = float(TRADE_SIZE) * 0.25

            stats["tp_hit"]      += 1
            stats["profit_usdt"] += pnl_usdt
            paper_balance        += quarter_margin + pnl_usdt
            stats["pnl_history"].append(round(paper_balance, 2))
            pos["currentPnl"] = pos.get("currentPnl", 0) + pnl_usdt

            # Move/trail SL
            new_sl = None
            if tp_num == 1 and not pos.get("breakeven"):
                new_sl         = entry  # Breakeven
                pos["sl"]      = new_sl
                pos["breakeven"] = True
            elif tp_num == 2:
                new_sl    = tp_levels[0]  # Trail to TP1
                pos["sl"] = new_sl
            elif tp_num == 3:
                new_sl    = tp_levels[1]  # Trail to TP2
                pos["sl"] = new_sl

            pos["tp_hit"] = tp_num

            if tp_num == 4:
                # All TPs hit — full win
                stats["total"] += 1
                stats["trades_list"].append({
                    "sym": sym, "direction": direction, "result": "ALL_TP",
                    "pnl": round(stats["profit_usdt"], 2), "time": utc_now_str(),
                })
                win_rate = round(stats["tp_hit"] / stats["total"] * 100, 1)
                net_pnl  = round(stats["profit_usdt"] - stats["loss_usdt"], 2)
                tg_send(
                    f"<b>🎯 ALL 4 TPs HIT — {sym}!</b>\n\n"
                    f"[PAPER TRADE]\n"
                    f"Full trade complete — perfect signal!\n\n"
                    f"📊 Session stats:\n"
                    f"Win rate: {stats['tp_hit']}/{stats['total']} = {win_rate}%\n"
                    f"Net P&L: +${net_pnl:.2f} USDT\n"
                    f"Balance: ${paper_balance:.2f} USDT"
                )
                to_remove.append(sym)
            else:
                tg_send(make_tp_msg(sym, direction, tp_num, entry, next_tp, elapsed, pnl_usdt, new_sl))
                print(f"  TP{tp_num} hit: {sym} @ ${price}")

    for sym in to_remove:
        positions.pop(sym, None)


# ─────────────────────────── MAIN ─────────────────────────────
def run():
    global paper_balance
    print("=" * 55)
    print("  APEX Bybit Bot — PAPER TRADING MODE")
    print("=" * 55)
    print(f"Starting balance: ${PAPER_BALANCE} USDT (simulated)")
    print(f"Trade size: ${TRADE_SIZE} × {LEVERAGE}x = ${float(TRADE_SIZE) * LEVERAGE} exposure")
    print(f"Telegram: {'OK' if TG_TOKEN else 'MISSING'}")
    print(f"Blocked: {BLOCKED_COINS}")

    # Start Flask API in background thread
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()
    print(f"Dashboard API running on port {os.environ.get('PORT', 8080)}")

    tg_send(
        "<b>📝 APEX Paper Trading Bot — Online!</b>\n\n"
        f"Exchange: <b>Bybit Futures (SIMULATED)</b>\n"
        f"Leverage: <b>{LEVERAGE}x</b>\n"
        f"Trade size: <b>${TRADE_SIZE} USDT</b> → ${float(TRADE_SIZE) * LEVERAGE:.0f} exposure\n"
        f"Starting balance: <b>${PAPER_BALANCE} USDT</b>\n"
        f"Coins monitored: <b>{len(COINS)}</b>\n"
        f"Min confidence: <b>{MIN_CONF}%</b>\n\n"
        f"<b>Signal Engine:</b>\n"
        f"• RSI(14) — blocks overbought/oversold\n"
        f"• EMA 20/50 trend filter\n"
        f"• Volume spike detection\n"
        f"• 4-target GG Shot style signals\n"
        f"• BUY (Long) + SELL (Short) signals\n\n"
        f"<b>Simulation:</b> Fires instantly, tracks real P&L\n"
        f"Blocked: {', '.join(BLOCKED_COINS) or 'None'}\n\n"
        f"<i>No real money at risk — pure data collection!</i>"
    )

    offset       = None
    last_scan_at = 0
    last_price_t = 0
    prices       = None

    while True:
        try:
            offset = check_btns(offset)

            # Refresh prices every 10s
            if time.time() - last_price_t >= 10:
                prices = get_prices()
                last_price_t = time.time()

            if prices and positions:
                monitor_positions(prices)

            # ── SCAN ──
            if time.time() - last_scan_at >= SCAN_EVERY_SECONDS:
                last_scan_at = time.time()
                print(
                    f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                    f"Scanning {len(COINS)} coins... "
                    f"(open: {len(positions)}, balance: ${paper_balance:.2f})"
                )

                scan_prices = get_prices()
                if scan_prices:
                    prices = scan_prices

                    for coin in COINS:
                        sym = coin["symbol"]

                        if sym in BLOCKED_COINS:
                            continue
                        if sym in positions:
                            continue

                        d      = scan_prices.get(coin["id"]) or {}
                        price  = d.get("usd")
                        change = d.get("usd_24h_change", 0) or 0
                        high   = d.get("usd_24h_high")
                        low    = d.get("usd_24h_low")
                        vol    = d.get("usd_24h_vol")

                        if not price or abs(change) < 2:
                            continue

                        print(f"  {sym}: ${price} {round(change, 2)}%", end="")

                        ta = None
                        if abs(change) >= 3:
                            ta = get_ta(sym)
                            if ta:
                                trend = "↑" if ta.get("ema20") and ta.get("ema50") and ta["ema20"] > ta["ema50"] else "↓"
                                print(f" | RSI={ta.get('rsi','?')} EMA={trend} Vol={ta.get('vol_ratio', 1.0):.1f}x", end="")
                            time.sleep(0.1)

                        sig = build_signal(price, change, high, low, vol, ta)
                        print()

                        if not sig or sig["conf"] < MIN_CONF:
                            if sig and sig["conf"] >= MIN_CONF - 8 and sym not in pre_warned:
                                pre_warned[sym] = time.time()
                                tg_send(make_pre_warn(coin, sig["signal"], price))
                                print(f"  ⚠️ Pre-warn: {sym}")
                            continue

                        # Deduplicate
                        prev = last_signal.get(sym)
                        if prev and prev["signal"] == sig["signal"] and abs(prev.get("entry", 0) - price) / price < 0.005:
                            continue

                        # Max trades check
                        if len(positions) >= MAX_OPEN_TRADES:
                            print(f"  Max trades reached, skipping {sym}")
                            continue

                        pre_warned.pop(sym, None)
                        sig["entry"] = price
                        last_signal[sym] = sig

                        # Send signal message
                        tg_send(make_signal_msg(coin, sig, price, change))

                        # Enter paper trade immediately
                        paper_execute(coin, sig, price)
                        print(f"  🚀 Paper signal: {sym} {sig['signal']} {sig['conf']}%")

            time.sleep(2)

        except KeyboardInterrupt:
            print("\nBot stopped.")
            net = round(stats["profit_usdt"] - stats["loss_usdt"], 2)
            print(f"Final: {stats['tp_hit']}/{stats['total']} wins | Net P&L: ${net}")
            break
        except Exception as e:
            print(f"Main loop error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    run()