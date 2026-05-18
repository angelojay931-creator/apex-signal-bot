"""
APEX Bybit Leverage Bot - PAPER TRADING MODE
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
import time
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, jsonify

# ─────────────────────────── CONFIG ───────────────────────────
PAPER_TRADING  = True
PAPER_BALANCE  = 2000.0        # Simulated balance: $2,000 USDT

BYBIT_KEY    = os.environ.get("BYBIT_API_KEY", "").strip()
BYBIT_SECRET = os.environ.get("BYBIT_SECRET", "").strip()
TG_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT      = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

TRADE_SIZE         = 100.0     # $100 margin
USE_DYNAMIC_SIZING = False     # Fixed $100 always
RISK_PCT           = 0.05      # Unused
MIN_TRADE_SIZE     = 100.0
MAX_TRADE_SIZE     = 100.0

LEVERAGE           = 5         # 5x → $500 exposure per trade
MIN_CONF           = 70        # new minimum confidence based on indicator agreement
SCAN_EVERY_SECONDS = 30
HTTP_TIMEOUT       = 15
MAX_OPEN_TRADES    = 8
MAX_SAME_DIRECTION  = 4

SLIPPAGE_PCT       = 0.001
FUNDING_RATE       = 0.0001
PRE_WARN_TTL       = 7200
GAP_SLIPPAGE_PCT   = 0.005
LIQ_BUFFER_PCT     = 0.02
MIN_24H_VOLUME     = 300_000_000

# Phase 2 additions
SL_COOLDOWN_SECONDS = 7200    # 2h cooldown after any SL hit per coin

BLOCKED_COINS = {
    "WAVES", "HNT", "EOS", "ALPACA",
    "WRX", "REEF", "LOKA", "AUCTION", "NULS", "ALPHA",
    "CLV", "SXP", "FIS", "MDT", "DODO", "BLZ",
    "APT", "CHZ", "MANA", "SUI", "WOO", "SAND",
    "ENJ",
    "TRU", "CORE", "WLD", "CRV", "FLOW",
}

# ─────────────── PROFESSIONAL RISK MANAGEMENT ─────────────────
MAX_DAILY_LOSS_PCT  = 0.06
MAX_CONSEC_LOSSES   = 3
MAX_TRADE_HOURS     = 48
BTC_CRASH_PCT       = -5.0
MIN_FREE_CASH_PCT   = 0.30

# ─────────────────────────── STATE ────────────────────────────
state_lock    = threading.RLock()

positions     = {}
pre_warned    = {}
paper_balance = PAPER_BALANCE

stats = {
    "total":       0,
    "trades_won":  0,
    "tp_hit":      0,
    "sl_hit":      0,
    "profit_usdt": 0.0,
    "loss_usdt":   0.0,
    "trades_list": [],
    "pnl_history": [PAPER_BALANCE],
}

last_signal = {}
sl_cooldown = {}

risk_state = {
    "session_start_balance": PAPER_BALANCE,
    "consec_losses":         0,
    "trading_paused":        False,
    "pause_reason":          "",
    "btc_last_price":        None,
    "btc_last_check":        0.0,
    "pause_until":           0.0,
    "daily_reset_at":        0.0,
}

# ─────────────────────────── COINS (130) ────────────────────────────
COINS = [
    # Original 30 proven coins
    {"id": "dydx-chain",              "symbol": "DYDX",   "bybit": "DYDXUSDT"},
    {"id": "starknet",                "symbol": "STRK",   "bybit": "STRKUSDT"},
    {"id": "pendle",                  "symbol": "PENDLE", "bybit": "PENDLEUSDT"},
    {"id": "immutable-x",             "symbol": "IMX",    "bybit": "IMXUSDT"},
    {"id": "near",                    "symbol": "NEAR",   "bybit": "NEARUSDT"},
    {"id": "pyth-network",            "symbol": "PYTH",   "bybit": "PYTHUSDT"},
    {"id": "thorchain",               "symbol": "RUNE",   "bybit": "RUNEUSDT"},
    {"id": "dogwifcoin",              "symbol": "WIF",    "bybit": "WIFUSDT"},
    {"id": "kava",                    "symbol": "KAVA",   "bybit": "KAVAUSDT"},
    {"id": "eos",                     "symbol": "EOS",    "bybit": "EOSUSDT"},
    {"id": "blur",                    "symbol": "BLUR",   "bybit": "BLURUSDT"},
    {"id": "zcash",                   "symbol": "ZEC",    "bybit": "ZECUSDT"},
    {"id": "dogecoin",                "symbol": "DOGE",   "bybit": "DOGEUSDT"},
    {"id": "optimism",                "symbol": "OP",     "bybit": "OPUSDT"},
    {"id": "injective-protocol",      "symbol": "INJ",    "bybit": "INJUSDT"},
    {"id": "solana",                  "symbol": "SOL",    "bybit": "SOLUSDT"},
    {"id": "ontology",                "symbol": "ONT",    "bybit": "ONTUSDT"},
    {"id": "conflux-token",           "symbol": "CFX",    "bybit": "CFXUSDT"},
    {"id": "io-net",                  "symbol": "IO",     "bybit": "IOUSDT"},
    {"id": "polkadot",                "symbol": "DOT",    "bybit": "DOTUSDT"},
    {"id": "arbitrum",                "symbol": "ARB",    "bybit": "ARBUSDT"},
    {"id": "gmx",                     "symbol": "GMX",    "bybit": "GMXUSDT"},
    {"id": "pepe",                    "symbol": "PEPE",   "bybit": "PEPEUSDT"},
    {"id": "dash",                    "symbol": "DASH",   "bybit": "DASHUSDT"},
    {"id": "ondo-finance",            "symbol": "ONDO",   "bybit": "ONDOUSDT"},
    {"id": "lido-dao",                "symbol": "LDO",    "bybit": "LDOUSDT"},
    {"id": "aave",                    "symbol": "AAVE",   "bybit": "AAVEUSDT"},
    {"id": "ripple",                  "symbol": "XRP",    "bybit": "XRPUSDT"},
    {"id": "filecoin",                "symbol": "FIL",    "bybit": "FILUSDT"},
    {"id": "sushi",                   "symbol": "SUSHI",  "bybit": "SUSHIUSDT"},

    # Additional 100 coins (high volume, Binance Futures)
    {"id": "bitcoin",                 "symbol": "BTC",    "bybit": "BTCUSDT"},
    {"id": "ethereum",                "symbol": "ETH",    "bybit": "ETHUSDT"},
    {"id": "binancecoin",             "symbol": "BNB",    "bybit": "BNBUSDT"},
    {"id": "cardano",                 "symbol": "ADA",    "bybit": "ADAUSDT"},
    {"id": "tron",                    "symbol": "TRX",    "bybit": "TRXUSDT"},
    {"id": "avalanche-2",             "symbol": "AVAX",   "bybit": "AVAXUSDT"},
    {"id": "sui",                     "symbol": "SUI",    "bybit": "SUIUSDT"},
    {"id": "chainlink",               "symbol": "LINK",   "bybit": "LINKUSDT"},
    {"id": "stellar",                 "symbol": "XLM",    "bybit": "XLMUSDT"},
    {"id": "litecoin",                "symbol": "LTC",    "bybit": "LTCUSDT"},
    {"id": "uniswap",                 "symbol": "UNI",    "bybit": "UNIUSDT"},
    {"id": "aptos",                   "symbol": "APT",    "bybit": "APTUSDT"},
    {"id": "internet-computer",       "symbol": "ICP",    "bybit": "ICPUSDT"},
    {"id": "ethereum-classic",        "symbol": "ETC",    "bybit": "ETCUSDT"},
    {"id": "bittensor",               "symbol": "TAO",    "bybit": "TAOUSDT"},
    {"id": "hyperliquid",             "symbol": "HYPE",   "bybit": "HYPEUSDT"},
    {"id": "monero",                  "symbol": "XMR",    "bybit": "XMRUSDT"},
    {"id": "shiba-inu",               "symbol": "SHIB",   "bybit": "SHIBUSDT"},
    {"id": "bonk",                    "symbol": "BONK",   "bybit": "BONKUSDT"},
    {"id": "celestia",                "symbol": "TIA",    "bybit": "TIAUSDT"},
    {"id": "sei-network",             "symbol": "SEI",    "bybit": "SEIUSDT"},
    {"id": "the-graph",               "symbol": "GRT",    "bybit": "GRTUSDT"},
    {"id": "mantle",                  "symbol": "MNT",    "bybit": "MNTUSDT"},
    {"id": "raydium",                 "symbol": "RAY",    "bybit": "RAYUSDT"},
    {"id": "curve-dao-token",         "symbol": "CRV",    "bybit": "CRVUSDT"},
    {"id": "fetch-ai",                "symbol": "FET",    "bybit": "FETUSDT"},
    {"id": "matic-network",           "symbol": "POL",    "bybit": "POLUSDT"},
    {"id": "ronin",                   "symbol": "RON",    "bybit": "RONUSDT"},
    {"id": "terra-luna-2",            "symbol": "LUNA",   "bybit": "LUNAUSDT"},
    {"id": "iota",                    "symbol": "IOTA",   "bybit": "IOTAUSDT"},
    {"id": "neo",                     "symbol": "NEO",    "bybit": "NEOUSDT"},
    {"id": "gala",                    "symbol": "GALA",   "bybit": "GALAUSDT"},
    {"id": "chiliz",                  "symbol": "CHZ",    "bybit": "CHZUSDT"},
    {"id": "band-protocol",           "symbol": "BAND",   "bybit": "BANDUSDT"},
    {"id": "nervos-network",          "symbol": "CKB",    "bybit": "CKBUSDT"},
    {"id": "zilliqa",                 "symbol": "ZIL",    "bybit": "ZILUSDT"},
    {"id": "vechain",                 "symbol": "VET",    "bybit": "VETUSDT"},
    {"id": "floki",                   "symbol": "FLOKI",  "bybit": "FLOKIUSDT"},
    {"id": "woo-network",             "symbol": "WOO",    "bybit": "WOOUSDT"},
    {"id": "ocean-protocol",          "symbol": "OCEAN",  "bybit": "OCEANUSDT"},
    {"id": "singularitynet",          "symbol": "AGIX",   "bybit": "AGIXUSDT"},
    {"id": "api3",                    "symbol": "API3",   "bybit": "API3USDT"},
    {"id": "arkham",                  "symbol": "ARKM",   "bybit": "ARKMUSDT"},
    {"id": "akash-network",           "symbol": "AKT",    "bybit": "AKTUSDT"},
    {"id": "axie-infinity",           "symbol": "AXS",    "bybit": "AXSUSDT"},
    {"id": "decentraland",            "symbol": "MANA",   "bybit": "MANAUSDT"},
    {"id": "flow",                    "symbol": "FLOW",   "bybit": "FLOWUSDT"},
    {"id": "oasis-network",           "symbol": "ROSE",   "bybit": "ROSEUSDT"},
    {"id": "kusama",                  "symbol": "KSM",    "bybit": "KSMUSDT"},
    {"id": "compound-governance-token","symbol": "COMP",   "bybit": "COMPUSDT"},
    {"id": "yearn-finance",           "symbol": "YFI",    "bybit": "YFIUSDT"},
    {"id": "wormhole",                "symbol": "W",      "bybit": "WUSDT"},
    {"id": "notcoin",                 "symbol": "NOT",    "bybit": "NOTUSDT"},
    {"id": "zksync",                  "symbol": "ZK",     "bybit": "ZKUSDT"},
    {"id": "tensor",                  "symbol": "TNSR",   "bybit": "TNSRUSDT"},
    {"id": "portal",                  "symbol": "PORTAL", "bybit": "PORTALUSDT"},
    {"id": "bitcoin-cash",            "symbol": "BCH",    "bybit": "BCHUSDT"},
    {"id": "maker",                   "symbol": "MKR",    "bybit": "MKRUSDT"},
    {"id": "algorand",                "symbol": "ALGO",   "bybit": "ALGOUSDT"},
    {"id": "theta-token",             "symbol": "THETA",  "bybit": "THETAUSDT"},
    {"id": "elrond-erd-2",            "symbol": "EGLD",   "bybit": "EGLDUSDT"},
    {"id": "loopring",                "symbol": "LRC",    "bybit": "LRCUSDT"},
    {"id": "basic-attention-token",   "symbol": "BAT",    "bybit": "BATUSDT"},
    {"id": "iotex",                   "symbol": "IOTX",   "bybit": "IOTXUSDT"},
    {"id": "ren",                     "symbol": "REN",    "bybit": "RENUSDT"},
    {"id": "storj",                   "symbol": "STORJ",  "bybit": "STORJUSDT"},
    {"id": "celo",                    "symbol": "CELO",   "bybit": "CELOUSDT"},
    {"id": "harmony",                 "symbol": "ONE",    "bybit": "ONEUSDT"},
    {"id": "qtum",                    "symbol": "QTUM",   "bybit": "QTUMUSDT"},
    {"id": "icon",                    "symbol": "ICX",    "bybit": "ICXUSDT"},
    {"id": "ontology-gas",            "symbol": "ONG",    "bybit": "ONGUSDT"},
    {"id": "zeta-chain",              "symbol": "ZETA",   "bybit": "ZETAUSDT"},
    {"id": "ssv-network",             "symbol": "SSV",    "bybit": "SSVUSDT"},
    {"id": "civic",                   "symbol": "CVC",    "bybit": "CVCUSDT"},
    {"id": "dusk-network",            "symbol": "DUSK",   "bybit": "DUSKUSDT"},
    {"id": "nkn",                     "symbol": "NKN",    "bybit": "NKNUSDT"},
    {"id": "audius",                  "symbol": "AUDIO",  "bybit": "AUDIOUSDT"},
    {"id": "alchemy-pay",             "symbol": "ACH",    "bybit": "ACHUSDT"},
    {"id": "venus",                   "symbol": "XVS",    "bybit": "XVSUSDT"},
    {"id": "biswap",                  "symbol": "BSW",    "bybit": "BSWUSDT"},
    {"id": "truefi",                  "symbol": "TRU",    "bybit": "TRUUSDT"},
    {"id": "orion-protocol",          "symbol": "ORN",    "bybit": "ORNUSDT"},
    {"id": "litentry",                "symbol": "LIT",    "bybit": "LITUSDT"},
    {"id": "phala-network",           "symbol": "PHA",    "bybit": "PHAUSDT"},
    {"id": "superverse",              "symbol": "SUPER",  "bybit": "SUPERUSDT"},
    {"id": "swipe",                   "symbol": "SXP",    "bybit": "SXPUSDT"},
    {"id": "stafi",                   "symbol": "FIS",    "bybit": "FISUSDT"},
    {"id": "bounce-token",            "symbol": "AUCTION","bybit": "AUCTIONUSDT"},
    {"id": "nuls",                    "symbol": "NULS",   "bybit": "NULSUSDT"},
    {"id": "alphalink",               "symbol": "ALPHA",  "bybit": "ALPHAUSDT"},
    {"id": "dodo",                    "symbol": "DODO",   "bybit": "DODOUSDT"},
    {"id": "automata",                "symbol": "ATA",    "bybit": "ATAUSDT"},
    {"id": "gas",                     "symbol": "GAS",    "bybit": "GASUSDT"},
    {"id": "loom-network-new",        "symbol": "LOOM",   "bybit": "LOOMUSDT"},
    {"id": "bluzelle",                "symbol": "BLZ",    "bybit": "BLZUSDT"},
    {"id": "joe",                     "symbol": "JOE",    "bybit": "JOEUSDT"},
    {"id": "unifi-protocol-dao",      "symbol": "UNFI",   "bybit": "UNFIUSDT"},
    {"id": "synapse-2",               "symbol": "SYN",    "bybit": "SYNUSDT"},
    {"id": "boba-network",            "symbol": "BOBA",   "bybit": "BOBAUSDT"},
    {"id": "measurable-data-token",   "symbol": "MDT",    "bybit": "MDTUSDT"},
    {"id": "wazirx",                  "symbol": "WRX",    "bybit": "WRXUSDT"},
    {"id": "barnbridge",              "symbol": "BOND",   "bybit": "BONDUSDT"},
    {"id": "telos",                   "symbol": "TLOS",   "bybit": "TLOSUSDT"},
    {"id": "oraichain-token",         "symbol": "ORAI",   "bybit": "ORAIUSDT"},
    {"id": "magic",                   "symbol": "MAGIC",  "bybit": "MAGICUSDT"},
]

# Remove exact duplicates keeping first occurrence
_seen = set()
_deduped = []
for c in COINS:
    if c["symbol"] not in _seen:
        _seen.add(c["symbol"])
        _deduped.append(c)
COINS = _deduped

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
    if price is None: return "N/A"
    if decimals is None:
        if price >= 1000:    decimals = 1
        elif price >= 100:   decimals = 2
        elif price >= 1:     decimals = 4
        elif price >= 0.01:  decimals = 5
        else:                decimals = 8
    return f"${price:.{decimals}f}"

def calc_trade_size():
    if USE_DYNAMIC_SIZING:
        with state_lock:
            bal = paper_balance
        sized = round(bal * RISK_PCT, 2)
        return max(MIN_TRADE_SIZE, min(MAX_TRADE_SIZE, sized))
    return TRADE_SIZE


# ─────────────── PROFESSIONAL RISK MANAGEMENT ─────────────────
def check_circuit_breakers(scan_prices=None):
    global risk_state
    with state_lock:
        free_cash       = paper_balance
        deployed_margin = sum(pos.get("margin", 0) * (1.0 - pos.get("tp_hit", 0) * 0.25)
                              for pos in positions.values())
        total_capital   = free_cash + deployed_margin

    now       = time.time()
    start_bal = risk_state["session_start_balance"]

    if risk_state["trading_paused"] and risk_state["pause_until"] > 0:
        if now >= risk_state["pause_until"]:
            risk_state["trading_paused"] = False
            risk_state["pause_reason"]   = ""
            risk_state["consec_losses"]  = 0
            risk_state["pause_until"]    = 0.0
            tg_send(f"<b>✅ Auto-Resume - Trading Restarted</b>\n\nPause period expired.\nFree cash: ${free_cash:.2f} USDT\nTotal capital: ${total_capital:.2f} USDT")

    midnight_today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    if risk_state["daily_reset_at"] < midnight_today:
        risk_state["daily_reset_at"]        = midnight_today
        risk_state["session_start_balance"] = total_capital
        start_bal                           = total_capital
        if risk_state["trading_paused"] and "daily loss" in risk_state["pause_reason"].lower():
            risk_state["trading_paused"] = False
            risk_state["pause_reason"]   = ""
            risk_state["consec_losses"]  = 0
            risk_state["pause_until"]    = 0.0
            tg_send(f"<b>🌅 New Day - Daily Loss Limit Reset</b>\n\nSession balance reset to ${total_capital:.2f} USDT\nTrading resumed for new UTC day.")

    drawdown_pct = (total_capital - start_bal) / start_bal * 100
    if drawdown_pct <= -(MAX_DAILY_LOSS_PCT * 100):
        if not risk_state["trading_paused"]:
            reason = f"Daily loss limit hit ({drawdown_pct:.1f}% drawdown)"
            risk_state["trading_paused"] = True
            risk_state["pause_reason"]   = reason
            risk_state["pause_until"]    = 0.0
            tg_send(f"<b>🛑 CIRCUIT BREAKER - Trading Paused</b>\n\nReason: {reason}\nFree cash: ${free_cash:.2f} USDT\nTotal capital: ${total_capital:.2f} USDT\nStarted: ${start_bal:.2f} USDT\n\n<i>Will auto-resume at midnight UTC or type /resume.</i>")
        return True

    if risk_state["consec_losses"] >= MAX_CONSEC_LOSSES:
        if not risk_state["trading_paused"]:
            reason = f"{MAX_CONSEC_LOSSES} consecutive SL hits"
            risk_state["trading_paused"] = True
            risk_state["pause_reason"]   = reason
            risk_state["pause_until"]    = now + 3600
            resume_time = datetime.fromtimestamp(now + 3600, tz=timezone.utc).strftime("%H:%M UTC")
            tg_send(f"<b>⚠️ CONSECUTIVE LOSS LIMIT - Pausing 1 hour</b>\n\nReason: {reason}\nFree cash: ${free_cash:.2f} USDT\nTotal capital: ${total_capital:.2f} USDT\n\n<i>Auto-resuming at {resume_time}.</i>")
        return True

    if scan_prices and time.time() - risk_state["btc_last_check"] >= 3600:
        btc_now  = (scan_prices.get("bitcoin") or {}).get("usd")
        btc_prev = risk_state["btc_last_price"]
        risk_state["btc_last_check"] = time.time()
        if btc_now:
            risk_state["btc_last_price"] = btc_now
            if btc_prev and btc_now > 0:
                btc_change = (btc_now - btc_prev) / btc_prev * 100
                if btc_change <= BTC_CRASH_PCT:
                    if not risk_state["trading_paused"]:
                        reason = f"BTC crashed {btc_change:.1f}% in 1h"
                        risk_state["trading_paused"] = True
                        risk_state["pause_reason"]   = reason
                        risk_state["pause_until"]    = now + 7200
                        resume_time = datetime.fromtimestamp(now + 7200, tz=timezone.utc).strftime("%H:%M UTC")
                        tg_send(f"<b>🚨 BTC CRASH DETECTED - Pausing signals</b>\n\nBTC: ${btc_prev:.0f} → ${btc_now:.0f} ({btc_change:.1f}%)\nFree cash: ${free_cash:.2f} USDT\nTotal capital: ${total_capital:.2f} USDT\n\n<i>Auto-resuming at {resume_time}.</i>")
                    return True

    if risk_state["trading_paused"] and risk_state["pause_until"] == 0.0:
        risk_state["trading_paused"] = False
        risk_state["pause_reason"]   = ""
        risk_state["consec_losses"]  = 0
        tg_send(f"<b>✅ Circuit Breaker Reset - Trading Resumed</b>\n\nTotal capital: ${total_capital:.2f} USDT")

    return False


def check_stale_positions():
    stale = []
    max_seconds = MAX_TRADE_HOURS * 3600
    with state_lock:
        for sym, pos in positions.items():
            age = time.time() - pos.get("opened_at", time.time())
            if age > max_seconds:
                stale.append(sym)
    return stale


# ──────────────────── CANDLE FETCHING ──────────────────────────
def get_candles(symbol_usdt, interval="1h", limit=80):
    """Fetch full OHLCV candles from Binance."""
    try:
        r = session.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol_usdt, "interval": interval, "limit": limit},
            timeout=10,
        )
        data = r.json()
        if not isinstance(data, list):
            return None
        return [
            {
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": float(c[5]),
            }
            for c in data
        ]
    except Exception as e:
        print(f"  Candle error {symbol_usdt}: {e}")
        return None


# ──────────────────── TECHNICAL INDICATORS ──────────────────────
def calc_ema(closes, period):
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 8)

def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return None, None
    trs = []
    for i in range(1, len(candles)):
        high = candles[i].get("high", candles[i]["close"] * 1.005)
        low  = candles[i].get("low",  candles[i]["close"] * 0.995)
        prev_c = candles[i-1]["close"]
        tr = max(high - low, abs(high - prev_c), abs(low - prev_c))
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    current_price = candles[-1]["close"]
    atr_pct = round(atr / current_price * 100, 4) if current_price > 0 else None
    return round(atr, 8), atr_pct

def calc_supertrend(candles, period=10, multiplier=3.0):
    if len(candles) < period + 1:
        return None, None
    high = [c["high"] for c in candles]
    low  = [c["low"]  for c in candles]
    close = [c["close"] for c in candles]

    atr_val, _ = calc_atr(candles, period)
    if atr_val is None:
        return None, None

    upper_band = [(high[i] + low[i]) / 2 + multiplier * atr_val for i in range(len(candles))]
    lower_band = [(high[i] + low[i]) / 2 - multiplier * atr_val for i in range(len(candles))]

    supertrend = [0.0] * len(candles)
    direction = [1] * len(candles)

    for i in range(1, len(candles)):
        if close[i] > supertrend[i-1]:
            supertrend[i] = max(lower_band[i], supertrend[i-1])
            direction[i] = 1
        else:
            supertrend[i] = min(upper_band[i], supertrend[i-1])
            direction[i] = -1

    latest = supertrend[-1]
    is_bull = close[-1] > latest
    return round(latest, 8), is_bull

def calc_ut_bot(candles, key_value=1.0, atr_period=10):
    if len(candles) < atr_period + 1:
        return False, False

    ha_open  = [0.0] * len(candles)
    ha_close = [0.0] * len(candles)
    ha_high  = [0.0] * len(candles)
    ha_low   = [0.0] * len(candles)

    ha_open[0] = (candles[0]["open"] + candles[0]["close"]) / 2
    ha_close[0] = (candles[0]["open"] + candles[0]["high"] + candles[0]["low"] + candles[0]["close"]) / 4
    ha_high[0] = candles[0]["high"]
    ha_low[0]  = candles[0]["low"]

    for i in range(1, len(candles)):
        ha_open[i] = (ha_open[i-1] + ha_close[i-1]) / 2
        ha_close[i] = (candles[i]["open"] + candles[i]["high"] + candles[i]["low"] + candles[i]["close"]) / 4
        ha_high[i] = max(candles[i]["high"], ha_open[i], ha_close[i])
        ha_low[i]  = min(candles[i]["low"], ha_open[i], ha_close[i])

    atr_val, _ = calc_atr(candles, atr_period)
    if atr_val is None:
        return False, False

    nATR = key_value * atr_val
    xATRTrailingStop = [0.0] * len(candles)
    xATRTrailingStop[0] = ha_close[0] - nATR if ha_close[0] > ha_open[0] else ha_close[0] + nATR

    for i in range(1, len(candles)):
        prev_stop = xATRTrailingStop[i-1]
        if ha_close[i-1] > prev_stop:
            xATRTrailingStop[i] = max(ha_close[i] - nATR, prev_stop)
        else:
            xATRTrailingStop[i] = min(ha_close[i] + nATR, prev_stop)

    buy_signal  = False
    sell_signal = False
    if ha_close[-2] <= xATRTrailingStop[-2] and ha_close[-1] > xATRTrailingStop[-1]:
        buy_signal = True
    elif ha_close[-2] >= xATRTrailingStop[-2] and ha_close[-1] < xATRTrailingStop[-1]:
        sell_signal = True

    return buy_signal, sell_signal


def get_ta(symbol):
    candles = get_candles(symbol + "USDT", "1h", 80)
    if not candles or len(candles) < 30:
        return None
    closes = [c["close"] for c in candles]

    ema200 = calc_ema(closes, 200) if len(closes) >= 200 else None
    ema50  = calc_ema(closes, 50)  if len(closes) >= 50 else None
    ema20  = calc_ema(closes, 20)

    supertrend_val, supertrend_bull = calc_supertrend(candles, period=10, multiplier=3.0)
    ut_buy, ut_sell = calc_ut_bot(candles, key_value=1.0, atr_period=10)

    atr_val, atr_pct = calc_atr(candles, 14)

    return {
        "ema200": ema200,
        "ema50":  ema50,
        "ema20":  ema20,
        "supertrend": supertrend_val,
        "supertrend_bull": supertrend_bull,
        "ut_buy":  ut_buy,
        "ut_sell": ut_sell,
        "atr":     atr_val,
        "atr_pct": atr_pct,
    }


def fetch_ta_parallel(symbols):
    results = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        future_map = {ex.submit(get_ta, sym): sym for sym in symbols}
        for future in as_completed(future_map):
            sym = future_map[future]
            try:
                results[sym] = future.result()
            except Exception:
                results[sym] = None
    return results


# ──────────────────────── PRICES ──────────────────────────────
def get_prices():
    try:
        ids = ",".join(c["id"] for c in COINS)
        r = session.get(f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true&include_24hr_high=true&include_24hr_low=true&include_24hr_vol=true", timeout=15)
        d = r.json()
        if d and d.get("bitcoin", {}).get("usd"):
            return d
    except Exception as e:
        print("CoinGecko error:", e)

    try:
        result = {}
        for coin in COINS:
            try:
                r = session.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={coin['symbol']}USDT", timeout=10)
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
            time.sleep(0.02)
        return result if result else None
    except Exception as e:
        print("Binance fallback error:", e)
    return None


# ──────────────────────── SIGNAL ENGINE ───────────────────────
def build_signal(price, change, high, low, vol, ta):
    if not price or not ta:
        return None

    ut_buy      = ta.get("ut_buy")
    ut_sell     = ta.get("ut_sell")
    super_bull  = ta.get("supertrend_bull")
    ema20       = ta.get("ema20")
    ema50       = ta.get("ema50")
    ema200      = ta.get("ema200")

    if None in (ut_buy, ut_sell, super_bull, ema20, ema50, ema200):
        return None

    uptrend_ema = (ema20 > ema50 > ema200)
    dntrend_ema = (ema20 < ema50 < ema200)

    if ut_buy and super_bull and uptrend_ema:
        direction = "BUY"
        agreements = 3
    elif ut_sell and not super_bull and dntrend_ema:
        direction = "SELL"
        agreements = 3
    elif ut_buy and super_bull:
        direction = "BUY"
        agreements = 2
    elif ut_sell and not super_bull:
        direction = "SELL"
        agreements = 2
    else:
        return None

    conf = 70 + agreements * 10

    atr_val = ta.get("atr")
    atr_pct = ta.get("atr_pct")

    if atr_val is not None and atr_val > 0:
        if direction == "BUY":
            tp1 = round(price + 1.5 * atr_val, 8)
            tp2 = round(price + 2.5 * atr_val, 8)
            tp3 = round(price + 4.0 * atr_val, 8)
            tp4 = round(price + 6.0 * atr_val, 8)
            sl  = round(price - 2.0 * atr_val, 8)
        else:
            tp1 = round(price - 1.5 * atr_val, 8)
            tp2 = round(price - 2.5 * atr_val, 8)
            tp3 = round(price - 4.0 * atr_val, 8)
            tp4 = round(price - 6.0 * atr_val, 8)
            sl  = round(price + 2.0 * atr_val, 8)
    else:
        base = 0.025
        if direction == "BUY":
            tp1 = round(price * (1 + base * 0.40), 8)
            tp2 = round(price * (1 + base * 0.70), 8)
            tp3 = round(price * (1 + base * 1.00), 8)
            tp4 = round(price * (1 + base * 1.50), 8)
            if atr_pct and atr_pct > 0:
                sl_pct = min(max(atr_pct * 2, 0.4), 1.5)
            else:
                sl_pct = 1.0
            sl = round(price * (1 - sl_pct / 100), 8)
        else:
            tp1 = round(price * (1 - base * 0.40), 8)
            tp2 = round(price * (1 - base * 0.70), 8)
            tp3 = round(price * (1 - base * 1.00), 8)
            tp4 = round(price * (1 - base * 1.50), 8)
            if atr_pct and atr_pct > 0:
                sl_pct = min(max(atr_pct * 2, 0.4), 1.5)
            else:
                sl_pct = 1.0
            sl = round(price * (1 + sl_pct / 100), 8)

    tp_pcts = [round(abs(tp1 - price) / price * 100, 2),
               round(abs(tp2 - price) / price * 100, 2),
               round(abs(tp3 - price) / price * 100, 2),
               round(abs(tp4 - price) / price * 100, 2)]
    sl_pct_actual = round(abs(sl - price) / price * 100, 2)

    return {
        "signal":   direction,
        "conf":     conf,
        "ema20":    ema20,
        "ema50":    ema50,
        "ema200":   ema200,
        "supertrend_bull": super_bull,
        "ut_buy":   ut_buy,
        "ut_sell":  ut_sell,
        "tp1":      tp1,
        "tp2":      tp2,
        "tp3":      tp3,
        "tp4":      tp4,
        "sl":       sl,
        "sl_pct":   sl_pct_actual,
        "tp_pcts":  tp_pcts,
        "tp1_prob": 80,
        "tp2_prob": 65,
    }


# ─────────────────────────── FLASK API ────────────────────────
app = Flask(__name__)

@app.route("/")
def health():
    return "APEX Paper Bot running!", 200

@app.route("/data")
def get_data():
    with state_lock:
        total      = stats["total"]
        trades_won = stats["trades_won"]
        win_rate   = round(trades_won / total * 100, 1) if total > 0 else 0
        net_pnl    = round(stats["profit_usdt"] - stats["loss_usdt"], 2)

        open_pos = {}
        for sym, pos in positions.items():
            realized   = pos.get("currentPnl", 0)
            unrealized = pos.get("unrealized_pnl", 0)
            rem_pct    = 1.0 - (pos.get("tp_hit", 0) * 0.25)
            open_pos[sym] = {
                "sym":           sym,
                "direction":     pos.get("direction", ""),
                "entry":         pos.get("entry", 0),
                "execPrice":     pos.get("exec_price", 0),
                "tp1":           pos.get("tp1", 0),
                "tp2":           pos.get("tp2", 0),
                "tp3":           pos.get("tp3", 0),
                "tp4":           pos.get("tp4", 0),
                "sl":            pos.get("sl", 0),
                "liqPrice":      pos.get("liq_price", 0),
                "tpHit":         pos.get("tp_hit", 0),
                "breakeven":     pos.get("breakeven", False),
                "margin":        pos.get("margin", float(TRADE_SIZE)),
                "realizedPnl":   round(realized, 2),
                "unrealizedPnl": round(unrealized, 2),
                "totalPnl":      round(realized + unrealized, 2),
                "remainingPct":  round(rem_pct * 100),
                "openTime":      pos.get("opened_at", 0),
                "sigId":         pos.get("sig_id", ""),
            }

        pnl_hist = stats["pnl_history"][-500:]

        payload = {
            "balance":       paper_balance,
            "startBalance":  PAPER_BALANCE,
            "netPnl":        net_pnl,
            "winRate":       win_rate,
            "totalTrades":   total,
            "tradesWon":     trades_won,
            "tpHits":        stats["tp_hit"],
            "slHits":        stats["sl_hit"],
            "profitUsdt":    stats["profit_usdt"],
            "lossUsdt":      stats["loss_usdt"],
            "openPositions": open_pos,
            "closedTrades":  stats["trades_list"][-50:],
            "pnlHistory":    pnl_hist,
            "leverage":      LEVERAGE,
            "tradeSize":     TRADE_SIZE,
            "timestamp":     utc_now_str(),
        }
    response = jsonify(payload)
    response.headers.add("Access-Control-Allow-Origin", "*")
    return response


def start_flask():
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ─────────────────────────── TELEGRAM ─────────────────────────
def tg_send(msg):
    if not TG_TOKEN or not TG_CHAT:
        print("TG:", msg[:80])
        return None
    data = {"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = session.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, timeout=HTTP_TIMEOUT)
        payload = r.json()
        if not payload.get("ok"):
            print("TG error:", payload)
        return payload
    except Exception as e:
        print("TG send error:", e)
        return None

def tg_updates(offset=None):
    params = {"timeout": 1, "allowed_updates": '["message"]'}
    if offset is not None:
        params["offset"] = offset
    try:
        r = session.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", params=params, timeout=5)
        return r.json()
    except Exception:
        return None

def make_report():
    with state_lock:
        total      = stats["total"]
        trades_won = stats["trades_won"]
        win_rate   = round(trades_won / total * 100, 1) if total > 0 else 0
        net_pnl    = round(stats["profit_usdt"] - stats["loss_usdt"], 2)
        net_sign   = "+" if net_pnl >= 0 else "-"
        free_cash  = paper_balance
        deployed   = sum(pos.get("margin", 0) * (1.0 - pos.get("tp_hit", 0) * 0.25) for pos in positions.values())
        total_cap  = round(free_cash + deployed, 2)
        open_count = len(positions)

        pos_lines = []
        for sym, pos in positions.items():
            direction  = pos.get("direction", "")
            tp_hit     = pos.get("tp_hit", 0)
            realized   = round(pos.get("currentPnl", 0), 2)
            unrealized = round(pos.get("unrealized_pnl", 0), 2)
            arrow      = "🟢" if direction == "BUY" else "🔴"
            be_flag    = "🔒" if pos.get("breakeven") else ""
            progress   = tp_progress_bar(tp_hit, direction)
            pos_lines.append(f"{arrow} {sym} {be_flag}  {progress}\n   R:+${realized:.2f}  U:${unrealized:+.2f}")
        paused_str = f"\n⚠️ Paused: {risk_state['pause_reason']}" if risk_state.get("trading_paused") else ""

    pos_block = "\n".join(pos_lines) if pos_lines else "None"
    start_bal = risk_state["session_start_balance"]
    drawdown  = round((total_cap - start_bal) / start_bal * 100, 1)
    dd_icon   = "📈" if drawdown >= 0 else "📉"

    return (f"<b>📊 APEX REPORT - {utc_now_str()}</b>\n"
            f"══════════════════════════════\n"
            f"💰 Free cash:     ${free_cash:.2f} USDT\n"
            f"📦 Deployed:      ${deployed:.2f} USDT ({open_count} trades)\n"
            f"💎 Total capital: ${total_cap:.2f} USDT\n"
            f"{dd_icon} vs start:       {drawdown:+.1f}%\n\n"
            f"📈 Session P&L:   {net_sign}${abs(net_pnl):.2f} USDT\n"
            f"🏆 Win rate:      {trades_won}/{total} = {win_rate}%\n"
            f"✅ TP hits:       {stats['tp_hit']}\n"
            f"❌ SL hits:       {stats['sl_hit']}\n"
            f"{paused_str}\n\n"
            f"<b>Open positions ({open_count}):</b>\n"
            f"{pos_block}")


def check_btns(offset):
    updates = tg_updates(offset)
    if updates and updates.get("ok"):
        for u in updates.get("result", []):
            offset = u["update_id"] + 1
            msg  = (u.get("message") or u.get("channel_post") or {})
            text = msg.get("text", "").strip().lower()
            if text in ("/report", "/status", "/r"):
                tg_send(make_report())
            elif text == "/pause":
                risk_state["trading_paused"] = True
                risk_state["pause_reason"]   = "Manual pause via /pause"
                tg_send("<b>⏸ Trading manually paused.</b>\nSend /resume to restart.")
            elif text == "/resume":
                risk_state["trading_paused"] = False
                risk_state["pause_reason"]   = ""
                risk_state["consec_losses"]  = 0
                tg_send("<b>▶️ Trading resumed.</b>")
            elif text == "/help":
                tg_send("<b>⚡ APEX Commands</b>\n\n/report - Full session report\n/status - Same as /report\n/r      - Quick shortcut\n/pause  - Pause new signals\n/resume - Resume trading\n/help   - This message")
    return offset


# ─────────────────────────── MESSAGES ─────────────────────────
def make_pre_warn(coin, direction, price):
    arrow = "📈" if direction == "BUY" else "📉"
    return f"<b>⚠️ GET READY - {coin['symbol']}/USDT</b>\n\n{arrow} Potential <b>{direction}</b> forming\nPrice: {fmt_p(price)}\n\n<i>Full signal incoming shortly...</i>"

def make_signal_id(sym):
    now = datetime.now(timezone.utc)
    return f"{sym}-{now.strftime('%m%d')}-{now.strftime('%H%M')}"

def tp_progress_bar(tp_hit, direction):
    icons = []
    for i in range(1, 5):
        if i <= tp_hit:
            icons.append(f"TP{i}✅")
        else:
            icons.append(f"TP{i}⬜")
    return "  ".join(icons)

def make_signal_msg(coin, sig, price, change):
    action   = sig["signal"]
    sign     = "+" if change >= 0 else ""
    conf     = sig["conf"]
    bars     = "#" * int(conf / 10) + "-" * (10 - int(conf / 10))
    tp_pcts  = sig.get("tp_pcts", [0,0,0,0])
    sl_pct   = sig.get("sl_pct", 1.0)
    arrow    = "🟢" if action == "BUY" else "🔴"
    side_word= "LONG" if action == "BUY" else "SHORT"
    sig_id   = sig.get("sig_id", make_signal_id(coin["symbol"]))

    ema20  = sig.get("ema20", None)
    ema50  = sig.get("ema50", None)
    ema200 = sig.get("ema200", None)
    super_bull = sig.get("supertrend_bull", None)
    ut_buy  = sig.get("ut_buy", False)
    ut_sell = sig.get("ut_sell", False)

    ema_str = (f"↑ Uptrend" if (ema20 and ema50 and ema200 and ema20 > ema50 > ema200) else
               f"↓ Downtrend" if (ema20 and ema50 and ema200 and ema20 < ema50 < ema200) else "Mixed")
    super_str = "🟢 Bull" if super_bull else ("🔴 Bear" if super_bull is not None else "N/A")
    ut_str    = "BUY signal ✅" if ut_buy else ("SELL signal ✅" if ut_sell else "No signal")

    lev_ret = [round(p * LEVERAGE, 1) for p in tp_pcts]
    trade_size = calc_trade_size()
    notional   = trade_size * LEVERAGE

    return (f"<b>⚡ APEX SIGNAL - #{sig_id}</b>\n"
            f"══════════════════════════════\n"
            f"{arrow} <b>{side_word} - {coin['symbol']}/USDT</b>\n\n"
            f"⚙️ {LEVERAGE}x | ${trade_size:.0f} margin → ${notional:.0f} exposure\n\n"
            f"Entry:     {fmt_p(price)}\n"
            f"SL:        {fmt_p(sig['sl'])}  (-{sl_pct:.2f}% ATR)\n\n"
            f"TP1: {fmt_p(sig['tp1'])}  {sig.get('tp1_prob', 80)}% prob  (+{tp_pcts[0]}% | {lev_ret[0]}% levered)\n"
            f"TP2: {fmt_p(sig['tp2'])}  {sig.get('tp2_prob', 65)}% prob  (+{tp_pcts[1]}% | {lev_ret[1]}% levered)\n"
            f"TP3: {fmt_p(sig['tp3'])}               (+{tp_pcts[2]}% | {lev_ret[2]}% levered)\n"
            f"TP4: {fmt_p(sig['tp4'])}               (+{tp_pcts[3]}% | {lev_ret[3]}% levered)\n\n"
            f"📊 Indicators:\n"
            f"EMA 20:  {fmt_p(ema20) if ema20 else 'N/A'}\n"
            f"EMA 50:  {fmt_p(ema50) if ema50 else 'N/A'}\n"
            f"EMA 200: {fmt_p(ema200) if ema200 else 'N/A'}\n"
            f"EMA trend: {ema_str}\n"
            f"Supertrend: {super_str}\n"
            f"UT Bot:    {ut_str}\n"
            f"24h:       {sign}{round(change, 2)}%\n\n"
            f"Confidence: {conf}%  [{bars}]\n\n"
            f"🤖 <i>Paper trade auto-entered</i>\n"
            f"══════════════════════════════\n"
            f"Time: {utc_now_str()}")

def make_tp_msg(sym, direction, tp_num, entry, exec_price, tp_price, elapsed, pnl_usdt, new_sl=None, sig_id=None, tp_hit_total=0, trade_pnl_so_far=0):
    arrow     = "🟢" if direction == "BUY" else "🔴"
    side_word = "LONG" if direction == "BUY" else "SHORT"
    sl_note   = f"\n💡 SL → {fmt_p(new_sl)} (breakeven)" if tp_num == 1 and new_sl else (f"\n💡 SL trailed to TP{tp_num - 1}" if tp_num > 1 and new_sl else "")
    id_line   = f"#{sig_id}  |  " if sig_id else ""

    with state_lock:
        total      = stats["total"]
        trades_won = stats["trades_won"]
        win_rate   = round(trades_won / total * 100, 1) if total > 0 else 0
        net_pnl    = round(stats["profit_usdt"] - stats["loss_usdt"], 2)
        net_sign   = "+" if net_pnl >= 0 else "-"
        balance    = paper_balance

    entry_note  = f" (exec {fmt_p(exec_price)})" if abs(exec_price - entry) / entry > 0.0005 else ""
    progress    = tp_progress_bar(tp_hit_total, direction)
    total_so_far = round(trade_pnl_so_far, 2)

    return (f"<b>✅ TP{tp_num} HIT - {sym} {side_word}</b> {arrow}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 {id_line}Entry: {fmt_p(entry)}{entry_note}\n"
            f"{progress}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"TP{tp_num} hit: {fmt_p(tp_price)}\n"
            f"Time in trade: {elapsed_str(elapsed)}\n"
            f"This close:    +${pnl_usdt:.2f} USDT{sl_note}\n"
            f"Trade P&L so far: +${total_so_far:.2f} USDT\n\n"
            f"📊 Session stats:\n"
            f"Win rate: {trades_won}/{total} = {win_rate}%\n"
            f"Net P&L: {net_sign}${abs(net_pnl):.2f} USDT\n"
            f"Balance: ${balance:.2f} USDT")

def make_sl_msg(sym, direction, entry, exec_price, sl_price, elapsed, pnl_usdt, breakeven=False, sig_id=None, tp_hit_total=0, trade_pnl_so_far=0):
    side_word  = "LONG" if direction == "BUY" else "SHORT"
    be_str     = " (breakeven - no loss!)" if breakeven else ""
    id_line    = f"#{sig_id}  |  " if sig_id else ""

    with state_lock:
        total      = stats["total"]
        trades_won = stats["trades_won"]
        win_rate   = round(trades_won / total * 100, 1) if total > 0 else 0
        net_pnl    = round(stats["profit_usdt"] - stats["loss_usdt"], 2)
        net_sign   = "+" if net_pnl >= 0 else "-"
        balance    = paper_balance

    entry_note   = f" (exec {fmt_p(exec_price)})" if abs(exec_price - entry) / entry > 0.0005 else ""
    progress     = tp_progress_bar(tp_hit_total, direction)
    total_pnl    = round(trade_pnl_so_far + (pnl_usdt if breakeven else -pnl_usdt), 2)
    total_sign   = "+" if total_pnl >= 0 else ""
    icon         = "✅" if total_pnl >= 0 else "❌"

    return (f"<b>{icon} SL HIT{be_str} - {sym} {side_word}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 {id_line}Entry: {fmt_p(entry)}{entry_note}\n"
            f"{progress}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"SL hit: {fmt_p(sl_price)}\n"
            f"Time in trade: {elapsed_str(elapsed)}\n"
            f"This close: {'breakeven' if breakeven else f'-${pnl_usdt:.2f} USDT'}\n"
            f"<b>Total trade: {total_sign}${abs(total_pnl):.2f} USDT</b>\n\n"
            f"📊 Session stats:\n"
            f"Win rate: {trades_won}/{total} = {win_rate}%\n"
            f"Net P&L: {net_sign}${abs(net_pnl):.2f} USDT\n"
            f"Balance: ${balance:.2f} USDT")


# ──────────────────────── PAPER EXECUTE ───────────────────────
def is_sl_hit(direction, price, sl):
    if direction == "BUY":
        return price <= sl
    return price >= sl

def is_liq_hit(direction, price, liq_price):
    if direction == "BUY":
        return price <= liq_price
    return price >= liq_price

def is_sl_safe(direction, sl, liq_price):
    if liq_price <= 0 or sl <= 0:
        return False
    if direction == "BUY":
        return sl >= liq_price * (1 + LIQ_BUFFER_PCT)
    else:
        return sl <= liq_price * (1 - LIQ_BUFFER_PCT)

def paper_execute(coin, sig, price):
    global paper_balance
    sym        = coin["symbol"]
    direction  = sig["signal"]
    trade_size = calc_trade_size()
    notional   = trade_size * LEVERAGE

    if direction == "BUY":
        exec_price = round(price * (1 + SLIPPAGE_PCT), 8)
    else:
        exec_price = round(price * (1 - SLIPPAGE_PCT), 8)

    qty = round(notional / exec_price, 6)

    if direction == "BUY":
        liq_price = round(exec_price * (1 - 0.9 / LEVERAGE), 8)
    else:
        liq_price = round(exec_price * (1 + 0.9 / LEVERAGE), 8)

    if not is_sl_safe(direction, sig["sl"], liq_price):
        print(f"  ⛔ SL unsafe - SL={sig['sl']:.6f} Liq={liq_price:.6f} - rejected")
        return False

    with state_lock:
        if len(positions) >= MAX_OPEN_TRADES:
            print(f"  ⛔ Max trades reached inside execute: {len(positions)}/{MAX_OPEN_TRADES}")
            return False
        same_dir = sum(1 for p in positions.values() if p.get("direction") == direction)
        if same_dir >= MAX_SAME_DIRECTION:
            print(f"  ⛔ Direction cap: already {same_dir} {direction}s open (max {MAX_SAME_DIRECTION})")
            return False
        total_cap = paper_balance + sum(p.get("margin", 0) * (1 - p.get("tp_hit", 0) * 0.25) for p in positions.values())
        free_pct = (paper_balance - trade_size) / total_cap if total_cap > 0 else 0
        if free_pct < MIN_FREE_CASH_PCT:
            print(f"  ⛔ Free cash too low: {free_pct*100:.1f}% < {MIN_FREE_CASH_PCT*100:.0f}% required")
            return False
        if paper_balance < trade_size:
            bal_snap = paper_balance
        else:
            bal_snap = None
            sig_id   = sig.get("sig_id", make_signal_id(sym))
            paper_balance -= trade_size
            stats["pnl_history"].append(round(paper_balance, 2))
            positions[sym] = {
                "direction":               direction,
                "entry":                   price,
                "exec_price":              exec_price,
                "qty":                     qty,
                "margin":                  trade_size,
                "sl":                      sig["sl"],
                "liq_price":               liq_price,
                "tp1":                     sig["tp1"],
                "tp2":                     sig["tp2"],
                "tp3":                     sig["tp3"],
                "tp4":                     sig["tp4"],
                "tp_pcts":                 sig["tp_pcts"],
                "tp_hit":                  0,
                "first_tp_counted":        False,
                "breakeven":               False,
                "opened_at":               time.time(),
                "funding_periods_charged": 0,
                "currentPnl":              0.0,
                "unrealized_pnl":          0.0,
                "sig_id":                  sig_id,
                "close_reason":            None,
            }
            bal_after  = paper_balance
            open_count = len(positions)

    if bal_snap is not None:
        tg_send(f"<b>⚠️ Paper balance too low - {sym}</b>\n\nBalance: ${bal_snap:.2f} USDT\nRequired margin: ${trade_size:.0f} USDT\n\nSkipping trade.")
        return False

    side_word = "LONG" if direction == "BUY" else "SHORT"
    lev_ret   = [round(p * LEVERAGE, 1) for p in sig["tp_pcts"]]
    slip_note = f"\n⚡ Exec: {fmt_p(exec_price)} (slippage applied)" if abs(exec_price - price) / price > 0.0001 else ""

    tg_send(f"<b>📝 PAPER TRADE ENTERED - #{sig_id}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{'🟢' if direction == 'BUY' else '🔴'} <b>{side_word} {sym}/USDT</b>\n\n"
            f"Signal price: {fmt_p(price)}{slip_note}\n"
            f"Margin:    ${trade_size:.0f} USDT\n"
            f"Exposure:  ${notional:.0f} USDT ({LEVERAGE}x)\n\n"
            f"TP1: {fmt_p(sig['tp1'])}  ({lev_ret[0]}% levered)\n"
            f"TP2: {fmt_p(sig['tp2'])}  ({lev_ret[1]}% levered)\n"
            f"TP3: {fmt_p(sig['tp3'])}  ({lev_ret[2]}% levered)\n"
            f"TP4: {fmt_p(sig['tp4'])}  ({lev_ret[3]}% levered)\n"
            f"SL:  {fmt_p(sig['sl'])}\n"
            f"Liq: {fmt_p(liq_price)}\n\n"
            f"💰 Balance: ${bal_after:.2f} USDT  |  Open: {open_count}\n"
            f"🤖 Monitoring: auto TP/SL + trailing stop")
    print(f"  📝 Paper trade: {direction} {sym} @ {exec_price} (signal {price}) qty={qty}")
    return True


# ──────────────────────── POSITION MONITOR ────────────────────
def monitor_positions(prices):
    global paper_balance
    to_remove     = []
    notifications = []

    for sym, pos in list(positions.items()):
        coin_data = next((c for c in COINS if c["symbol"] == sym), None)
        if not coin_data:
            to_remove.append(sym)
            continue

        d     = prices.get(coin_data["id"]) or {}
        price = d.get("usd")
        if not price:
            continue

        with state_lock:
            direction  = pos.get("direction", "BUY")
            entry      = pos.get("entry", 0)
            exec_price = pos.get("exec_price", 0)
            sl         = pos.get("sl", 0)
            liq_price  = pos.get("liq_price", 0)
            tp_hit     = pos.get("tp_hit", 0)
            elapsed    = time.time() - pos.get("opened_at", time.time())
            tp_levels  = [pos.get("tp1",0), pos.get("tp2",0), pos.get("tp3",0), pos.get("tp4",0)]
            trade_size = pos.get("margin", float(TRADE_SIZE))

            remaining_pct = 1.0 - (tp_hit * 0.25)
            if exec_price > 0:
                if direction == "BUY":
                    live_move_pct = (price - exec_price) / exec_price * 100
                else:
                    live_move_pct = (exec_price - price) / exec_price * 100
                pos["unrealized_pnl"] = round(trade_size * LEVERAGE * live_move_pct / 100 * remaining_pct, 2)

            funding_due = int(elapsed / 28800)
            new_periods = funding_due - pos.get("funding_periods_charged", 0)
            if new_periods > 0:
                rem_pct      = 1.0 - (tp_hit * 0.25)
                funding_cost = trade_size * LEVERAGE * FUNDING_RATE * new_periods * rem_pct
                paper_balance -= funding_cost
                pos["funding_periods_charged"] = funding_due
                stats["pnl_history"].append(round(paper_balance, 2))
                print(f"  💸 Funding: {sym} -${funding_cost:.4f} ({new_periods} period(s))")

            if is_sl_hit(direction, price, sl):
                rem_pct          = 1.0 - (tp_hit * 0.25)
                price_move       = abs(sl - exec_price) / exec_price * 100
                is_profit        = (direction == "BUY"  and sl >= exec_price) or (direction == "SELL" and sl <= exec_price)
                pnl_usdt         = round(trade_size * LEVERAGE * price_move / 100 * rem_pct, 2)
                remaining_margin = trade_size * rem_pct

                if is_profit:
                    stats["profit_usdt"] += pnl_usdt
                    paper_balance        += remaining_margin + pnl_usdt
                else:
                    stats["loss_usdt"] += pnl_usdt
                    paper_balance      += max(0, remaining_margin - pnl_usdt)

                stats["total"]  += 1
                stats["sl_hit"] += 1
                if is_profit or pos.get("first_tp_counted"):
                    stats["trades_won"]         += 1
                    risk_state["consec_losses"]  = 0
                else:
                    risk_state["consec_losses"] += 1
                    sl_cooldown[sym] = time.time()
                    print(f"  ⏳ {sym} SL cooldown started — blocked 2h")
                stats["pnl_history"].append(round(paper_balance, 2))
                stats["trades_list"].append({"sym": sym, "direction": direction, "result": "SL", "close_reason": "SL", "pnl": pnl_usdt if is_profit else -pnl_usdt, "time": utc_now_str()})
                notifications.append(make_sl_msg(sym, direction, entry, exec_price, sl, elapsed, pnl_usdt, pos.get("breakeven"), sig_id=pos.get("sig_id"), tp_hit_total=tp_hit, trade_pnl_so_far=pos.get("currentPnl", 0)))
                to_remove.append(sym)
                continue

            if is_liq_hit(direction, price, liq_price):
                rem_pct          = 1.0 - (tp_hit * 0.25)
                remaining_margin = trade_size * rem_pct
                if direction == "BUY":
                    gap_close_price = sl * (1 - GAP_SLIPPAGE_PCT)
                else:
                    gap_close_price = sl * (1 + GAP_SLIPPAGE_PCT)
                price_move  = abs(gap_close_price - exec_price) / exec_price * 100
                pnl_usdt    = round(trade_size * LEVERAGE * price_move / 100 * rem_pct, 2)
                stats["loss_usdt"] += pnl_usdt
                paper_balance      += max(0, remaining_margin - pnl_usdt)
                stats["total"]     += 1
                stats["sl_hit"]    += 1
                risk_state["consec_losses"] += 1
                sl_cooldown[sym] = time.time()
                print(f"  ⏳ {sym} GAP_SL cooldown started — blocked 2h")
                stats["pnl_history"].append(round(paper_balance, 2))
                stats["trades_list"].append({"sym": sym, "direction": direction, "result": "GAP_SL", "close_reason": "GAP_SL", "pnl": -pnl_usdt, "time": utc_now_str()})
                notifications.append(f"<b>⚠️ GAP SL - {sym}</b> (price gapped past SL to liq)\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n📌 #{pos.get('sig_id','?')}  |  Entry: {fmt_p(entry)}\n{tp_progress_bar(tp_hit, direction)}\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nSL: {fmt_p(sl)}  →  Gap fill: {fmt_p(gap_close_price)}\nLoss: -${pnl_usdt:.2f} USDT (capped at SL, NOT full liq)\n\nBalance: ${paper_balance:.2f} USDT")
                print(f"  ⚠️ GAP SL: {sym} - skipped SL {sl:.5f}, hit liq {liq_price:.5f}")
                to_remove.append(sym)
                continue

            if tp_hit >= 4:
                to_remove.append(sym)
                continue

            next_tp    = tp_levels[tp_hit]
            tp_reached = (direction == "BUY"  and price >= next_tp) or (direction == "SELL" and price <= next_tp)

            if tp_reached:
                tp_num         = tp_hit + 1
                pnl_pct        = abs(next_tp - exec_price) / exec_price * 100
                pnl_usdt       = round(trade_size * LEVERAGE * pnl_pct / 100 * 0.25, 2)
                quarter_margin = trade_size * 0.25

                stats["tp_hit"]      += 1
                stats["profit_usdt"] += pnl_usdt
                paper_balance        += quarter_margin + pnl_usdt
                stats["pnl_history"].append(round(paper_balance, 2))
                pos["currentPnl"]     = pos.get("currentPnl", 0) + pnl_usdt

                if not pos["first_tp_counted"]:
                    pos["first_tp_counted"] = True

                new_sl = None
                if tp_num == 1 and not pos.get("breakeven"):
                    new_sl           = exec_price
                    pos["sl"]        = new_sl
                    pos["breakeven"] = True
                elif tp_num == 2:
                    new_sl    = tp_levels[0]
                    pos["sl"] = new_sl
                elif tp_num == 3:
                    new_sl    = tp_levels[1]
                    pos["sl"] = new_sl

                pos["tp_hit"] = tp_num

                if tp_num == 4:
                    stats["total"]              += 1
                    stats["trades_won"]          += 1
                    risk_state["consec_losses"]   = 0
                    stats["trades_list"].append({"sym": sym, "direction": direction, "result": "ALL_TP", "pnl": round(pos["currentPnl"], 2), "time": utc_now_str()})
                    total      = stats["total"]
                    trades_won = stats["trades_won"]
                    win_rate   = round(trades_won / total * 100, 1) if total > 0 else 0
                    net_pnl    = round(stats["profit_usdt"] - stats["loss_usdt"], 2)
                    trade_pnl  = round(pos["currentPnl"], 2)
                    side_word  = "LONG" if direction == "BUY" else "SHORT"
                    notifications.append(f"<b>🎯 ALL 4 TPs HIT - {sym}!</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n📌 #{pos.get('sig_id', '?')}  |  {side_word}\nEntry: {fmt_p(entry)} → All 4 targets hit!\n{tp_progress_bar(4, direction)}\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n💰 This trade: <b>+${trade_pnl:.2f} USDT</b>\n⏱ Time: {elapsed_str(elapsed)}\n\n📊 Session stats:\nWin rate: {trades_won}/{total} = {win_rate}%\nNet P&L: +${net_pnl:.2f} USDT\nBalance: ${paper_balance:.2f} USDT")
                    to_remove.append(sym)
                else:
                    notifications.append(make_tp_msg(sym, direction, tp_num, entry, exec_price, next_tp, elapsed, pnl_usdt, new_sl, sig_id=pos.get("sig_id"), tp_hit_total=tp_num, trade_pnl_so_far=pos.get("currentPnl", 0)))
                    print(f"  TP{tp_num} hit: {sym} @ ${price}")

    with state_lock:
        for sym in to_remove:
            positions.pop(sym, None)

    for msg in notifications:
        tg_send(msg)


# ─────────────────────────── MAIN ─────────────────────────────
def close_stale_position(sym, pos, scan_prices):
    global paper_balance
    coin_data = next((c for c in COINS if c["symbol"] == sym), None)
    if not coin_data:
        return
    d = scan_prices.get(coin_data["id"]) or {}
    live_price = d.get("usd")
    if not live_price:
        live_price = pos.get("exec_price", 0)

    with state_lock:
        if sym not in positions:
            return
        pos = positions[sym]
        direction  = pos["direction"]
        exec_price = pos["exec_price"]
        tp_hit     = pos["tp_hit"]
        rem_pct    = 1.0 - (tp_hit * 0.25)
        trade_size = pos["margin"]
        remaining_margin = trade_size * rem_pct

        if exec_price > 0:
            if direction == "BUY":
                move_pct = (live_price - exec_price) / exec_price * 100
            else:
                move_pct = (exec_price - live_price) / exec_price * 100
            unrealized_pnl = round(trade_size * LEVERAGE * move_pct / 100 * rem_pct, 2)
        else:
            unrealized_pnl = 0.0

        prior_realized = pos.get("currentPnl", 0)
        total_trade_pnl = round(prior_realized + unrealized_pnl, 2)

        if unrealized_pnl >= 0:
            stats["profit_usdt"] += unrealized_pnl
        else:
            stats["loss_usdt"] += abs(unrealized_pnl)

        paper_balance += remaining_margin + unrealized_pnl
        stats["total"] += 1
        stats["trades_list"].append({"sym": sym, "direction": direction, "result": "STALE_CLOSE", "pnl": total_trade_pnl, "time": utc_now_str()})
        positions.pop(sym, None)

        age_h = (time.time() - pos["opened_at"]) / 3600
        sign  = "+" if total_trade_pnl >= 0 else ""
        tg_send(f"<b>⏰ STALE TRADE CLOSED - {sym}</b>\nPosition open: {age_h:.1f}h (max {MAX_TRADE_HOURS}h)\nFinal price: {fmt_p(live_price)}\nTrade P&L: {sign}${total_trade_pnl:.2f}\nBalance: ${paper_balance:.2f}")


def run():
    global paper_balance, pre_warned, last_signal

    risk_state["session_start_balance"] = paper_balance
    risk_state["btc_last_check"]        = time.time()

    print("=" * 55)
    print("  APEX Bybit Bot v5 - PAPER TRADING MODE")
    print("=" * 55)
    print(f"Starting balance: ${PAPER_BALANCE} USDT (simulated)")
    print(f"Trade size: ${TRADE_SIZE} × {LEVERAGE}x leverage")
    print(f"Scanning {len(COINS)} coins")
    print(f"Telegram: {'OK' if TG_TOKEN else 'MISSING'}")

    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()
    print(f"Dashboard API running on port {os.environ.get('PORT', 8080)}")

    tg_send(f"<b>⚡ APEX — PHASE 2 (Supertrend + UT Bot + EMA) | 130 coins</b>\n\n"
            f"Exchange: <b>Bybit Futures (SIMULATED)</b>\n"
            f"Trade size: <b>${TRADE_SIZE:.0f}</b> × {LEVERAGE}x = <b>${TRADE_SIZE*LEVERAGE:.0f} exposure</b>\n"
            f"Max trades: <b>{MAX_OPEN_TRADES}</b> | Max same dir: <b>{MAX_SAME_DIRECTION}</b>\n"
            f"Whitelist coins: <b>{len(COINS)}</b>\n"
            f"Min confidence: <b>{MIN_CONF}%</b>\n\n"
            f"<b>📊 Signal Engine — Supertrend + UT Bot + EMA (20/50/200)</b>\n"
            f"• Supertrend (10,3) — trend direction\n"
            f"• UT Bot (1x ATR) — precise entry signals\n"
            f"• EMA alignment — macro trend filter\n\n"
            f"<b>🛡️ Risk:</b> {MAX_DAILY_LOSS_PCT*100:.0f}% daily loss | {MAX_CONSEC_LOSSES} consec SL | BTC crash guard\n\n"
            f"<b>📊 Commands:</b> /report /r /pause /resume /help\n\n"
            f"<i>Phase 2 — data-backed improvements</i>")

    offset       = None
    last_scan_at = 0
    last_price_t = 0
    prices       = None

    while True:
        try:
            offset = check_btns(offset)

            if time.time() - last_price_t >= 10:
                prices = get_prices()
                last_price_t = time.time()

            if prices:
                with state_lock:
                    open_syms = list(positions.keys())
                if open_syms:
                    monitor_positions(prices)

            if time.time() - last_scan_at >= SCAN_EVERY_SECONDS:
                last_scan_at = time.time()

                with state_lock:
                    open_count = len(positions)
                    balance    = paper_balance

                print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Scanning {len(COINS)} coins... (open: {open_count}, balance: ${balance:.2f})")

                now = time.time()
                pre_warned  = {k: v for k, v in pre_warned.items() if now - v < PRE_WARN_TTL}
                coin_syms   = {c["symbol"] for c in COINS}
                last_signal = {k: v for k, v in last_signal.items() if k in coin_syms}
                with state_lock:
                    if len(stats["pnl_history"]) > 600:
                        stats["pnl_history"] = stats["pnl_history"][-500:]

                scan_prices = get_prices()
                if not scan_prices:
                    time.sleep(2)
                    continue

                prices = scan_prices

                stale_syms = check_stale_positions()
                for sym in stale_syms:
                    pos_snapshot = positions.get(sym)
                    if pos_snapshot:
                        close_stale_position(sym, pos_snapshot, scan_prices)

                paused = check_circuit_breakers(scan_prices)
                if paused:
                    print(f"  🛑 Trading paused: {risk_state['pause_reason']}")
                    time.sleep(2)
                    continue

                now_ts = time.time()
                for sym_cd in list(sl_cooldown.keys()):
                    if now_ts - sl_cooldown[sym_cd] >= SL_COOLDOWN_SECONDS:
                        sl_cooldown.pop(sym_cd, None)
                        print(f"  ✅ {sym_cd} cooldown expired — available again")

                ta_candidates = []
                for coin in COINS:
                    sym = coin["symbol"]
                    if sym in BLOCKED_COINS:
                        continue
                    if sym in sl_cooldown:
                        continue
                    with state_lock:
                        if sym in positions:
                            continue
                    d      = scan_prices.get(coin["id"]) or {}
                    change = d.get("usd_24h_change", 0) or 0
                    if abs(change) >= 3:
                        ta_candidates.append(sym)

                ta_map = fetch_ta_parallel(ta_candidates) if ta_candidates else {}

                for coin in COINS:
                    sym = coin["symbol"]
                    if sym in BLOCKED_COINS:
                        continue
                    if sym in sl_cooldown:
                        remaining = int((SL_COOLDOWN_SECONDS - (time.time() - sl_cooldown[sym])) / 60)
                        print(f"  ⏳ {sym} cooldown ({remaining}min left)")
                        continue
                    with state_lock:
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

                    if not vol or vol < MIN_24H_VOLUME:
                        continue

                    print(f"  {sym}: ${price} {round(change, 2)}%", end="")

                    ta = ta_map.get(sym)
                    if ta:
                        super_str = "🟢" if ta.get("supertrend_bull") else "🔴"
                        ut_str    = "UT_BUY" if ta.get("ut_buy") else ("UT_SELL" if ta.get("ut_sell") else "UT_NONE")
                        ema_str   = (f"EMA20>{ta.get('ema20','?')}" if ta.get("ema20") else "N/A")
                        print(f" | Supertrend={super_str} {ut_str} {ema_str}", end="")

                    sig = build_signal(price, change, high, low, vol, ta)
                    print()

                    if not sig or sig["conf"] < MIN_CONF:
                        if (sig and sig["conf"] >= MIN_CONF - 8 and sym not in pre_warned and sym not in positions):
                            pre_warned[sym] = time.time()
                            tg_send(make_pre_warn(coin, sig["signal"], price))
                            print(f"  ⚠️ Pre-warn: {sym}")
                        continue

                    prev = last_signal.get(sym)
                    if prev and prev["signal"] == sig["signal"] and abs(prev.get("entry", 0) - price) / price < 0.005:
                        continue

                    with state_lock:
                        if len(positions) >= MAX_OPEN_TRADES:
                            print(f"  Max trades reached, skipping {sym}")
                            continue
                        pre_warned.pop(sym, None)

                    sig["entry"]     = price
                    sig["sig_id"]    = make_signal_id(sym)
                    last_signal[sym] = sig

                    opened = paper_execute(coin, sig, price)
                    if opened:
                        tg_send(make_signal_msg(coin, sig, price, change))
                        print(f"  🚀 Paper signal: {sym} {sig['signal']} {sig['conf']}%")
                    else:
                        print(f"  ⛔ Signal rejected: {sym} {sig['signal']} {sig['conf']}%")

            time.sleep(2)

        except KeyboardInterrupt:
            print("\nBot stopped.")
            with state_lock:
                net   = round(stats["profit_usdt"] - stats["loss_usdt"], 2)
                won   = stats["trades_won"]
                total = stats["total"]
            print(f"Final: {won}/{total} trades won | Net P&L: ${net}")
            break
        except Exception as e:
            print(f"Main loop error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    run()