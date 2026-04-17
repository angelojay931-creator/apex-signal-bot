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
import time
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, jsonify

# ─────────────────────────── CONFIG ───────────────────────────
PAPER_TRADING  = True
PAPER_BALANCE  = 1000.0        # Simulated balance: $1000 USDT

BYBIT_KEY    = os.environ.get("BYBIT_API_KEY", "").strip()
BYBIT_SECRET = os.environ.get("BYBIT_SECRET", "").strip()
TG_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT      = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

TRADE_SIZE         = 100.0     # Fixed $100 USDT margin per trade
USE_DYNAMIC_SIZING = False     # Fixed sizing — $100 always
RISK_PCT           = 0.10      # Unused when dynamic sizing is off
MIN_TRADE_SIZE     = 100.0
MAX_TRADE_SIZE     = 100.0

LEVERAGE           = 3         # 3x → $300 exposure per trade
MIN_CONF           = 85
SCAN_EVERY_SECONDS = 30
HTTP_TIMEOUT       = 15
MAX_OPEN_TRADES    = 8         # 8 × $100 = $800 max deployed ($200 kept in reserve)

SLIPPAGE_PCT       = 0.001
FUNDING_RATE       = 0.0001
PRE_WARN_TTL       = 7200

BLOCKED_COINS = {"ENJ"}

# ─────────────────────────── STATE ────────────────────────────
# RLock: reentrant — make_tp_msg/make_sl_msg re-acquire inside monitor_positions
state_lock    = threading.RLock()

positions     = {}
pre_warned    = {}   # {sym: timestamp}
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

# ─────────────────────────── COINS ────────────────────────────
# Top 200 coins by market cap — stablecoins and wrapped tokens excluded
COINS = [
    # ── TOP 10 ──
    {"id": "bitcoin",                   "symbol": "BTC",     "bybit": "BTCUSDT"},
    {"id": "ethereum",                  "symbol": "ETH",     "bybit": "ETHUSDT"},
    {"id": "ripple",                    "symbol": "XRP",     "bybit": "XRPUSDT"},
    {"id": "binancecoin",               "symbol": "BNB",     "bybit": "BNBUSDT"},
    {"id": "solana",                    "symbol": "SOL",     "bybit": "SOLUSDT"},
    {"id": "dogecoin",                  "symbol": "DOGE",    "bybit": "DOGEUSDT"},
    {"id": "cardano",                   "symbol": "ADA",     "bybit": "ADAUSDT"},
    {"id": "tron",                      "symbol": "TRX",     "bybit": "TRXUSDT"},
    {"id": "avalanche-2",               "symbol": "AVAX",    "bybit": "AVAXUSDT"},
    {"id": "sui",                       "symbol": "SUI",     "bybit": "SUIUSDT"},
    # ── 11-30 ──
    {"id": "chainlink",                 "symbol": "LINK",    "bybit": "LINKUSDT"},
    {"id": "stellar",                   "symbol": "XLM",     "bybit": "XLMUSDT"},
    {"id": "litecoin",                  "symbol": "LTC",     "bybit": "LTCUSDT"},
    {"id": "polkadot",                  "symbol": "DOT",     "bybit": "DOTUSDT"},
    {"id": "uniswap",                   "symbol": "UNI",     "bybit": "UNIUSDT"},
    {"id": "near",                      "symbol": "NEAR",    "bybit": "NEARUSDT"},
    {"id": "aptos",                     "symbol": "APT",     "bybit": "APTUSDT"},
    {"id": "internet-computer",         "symbol": "ICP",     "bybit": "ICPUSDT"},
    {"id": "ethereum-classic",          "symbol": "ETC",     "bybit": "ETCUSDT"},
    {"id": "bittensor",                 "symbol": "TAO",     "bybit": "TAOUSDT"},
    {"id": "hyperliquid",               "symbol": "HYPE",    "bybit": "HYPEUSDT"},
    {"id": "pepe",                      "symbol": "PEPE",    "bybit": "PEPEUSDT"},
    {"id": "aave",                      "symbol": "AAVE",    "bybit": "AAVEUSDT"},
    {"id": "monero",                    "symbol": "XMR",     "bybit": "XMRUSDT"},
    {"id": "filecoin",                  "symbol": "FIL",     "bybit": "FILUSDT"},
    {"id": "injective-protocol",        "symbol": "INJ",     "bybit": "INJUSDT"},
    {"id": "arbitrum",                  "symbol": "ARB",     "bybit": "ARBUSDT"},
    {"id": "optimism",                  "symbol": "OP",      "bybit": "OPUSDT"},
    {"id": "kaspa",                     "symbol": "KAS",     "bybit": "KASUSDT"},
    {"id": "render-token",              "symbol": "RENDER",  "bybit": "RENDERUSDT"},
    # ── 31-60 ──
    {"id": "shiba-inu",                 "symbol": "SHIB",    "bybit": "SHIBUSDT"},
    {"id": "bonk",                      "symbol": "BONK",    "bybit": "BONKUSDT"},
    {"id": "celestia",                  "symbol": "TIA",     "bybit": "TIAUSDT"},
    {"id": "sei-network",               "symbol": "SEI",     "bybit": "SEIUSDT"},
    {"id": "starknet",                  "symbol": "STRK",    "bybit": "STRKUSDT"},
    {"id": "the-graph",                 "symbol": "GRT",     "bybit": "GRTUSDT"},
    {"id": "immutable-x",               "symbol": "IMX",     "bybit": "IMXUSDT"},
    {"id": "thorchain",                 "symbol": "RUNE",    "bybit": "RUNEUSDT"},
    {"id": "mantle",                    "symbol": "MNT",     "bybit": "MNTUSDT"},
    {"id": "ondo-finance",              "symbol": "ONDO",    "bybit": "ONDOUSDT"},
    {"id": "raydium",                   "symbol": "RAY",     "bybit": "RAYUSDT"},
    {"id": "curve-dao-token",           "symbol": "CRV",     "bybit": "CRVUSDT"},
    {"id": "lido-dao",                  "symbol": "LDO",     "bybit": "LDOUSDT"},
    {"id": "fetch-ai",                  "symbol": "FET",     "bybit": "FETUSDT"},
    {"id": "matic-network",             "symbol": "POL",     "bybit": "POLUSDT"},
    {"id": "ronin",                     "symbol": "RON",     "bybit": "RONUSDT"},
    {"id": "terra-luna-2",              "symbol": "LUNA",    "bybit": "LUNAUSDT"},
    {"id": "kava",                      "symbol": "KAVA",    "bybit": "KAVAUSDT"},
    {"id": "iota",                      "symbol": "IOTA",    "bybit": "IOTAUSDT"},
    {"id": "neo",                       "symbol": "NEO",     "bybit": "NEOUSDT"},
    {"id": "dash",                      "symbol": "DASH",    "bybit": "DASHUSDT"},
    {"id": "zcash",                     "symbol": "ZEC",     "bybit": "ZECUSDT"},
    {"id": "sushi",                     "symbol": "SUSHI",   "bybit": "SUSHIUSDT"},
    {"id": "eos",                       "symbol": "EOS",     "bybit": "EOSUSDT"},
    {"id": "ontology",                  "symbol": "ONT",     "bybit": "ONTUSDT"},
    {"id": "waves",                     "symbol": "WAVES",   "bybit": "WAVESUSDT"},
    {"id": "gmx",                       "symbol": "GMX",     "bybit": "GMXUSDT"},
    {"id": "dydx-chain",                "symbol": "DYDX",    "bybit": "DYDXUSDT"},
    {"id": "pendle",                    "symbol": "PENDLE",  "bybit": "PENDLEUSDT"},
    {"id": "worldcoin-wld",             "symbol": "WLD",     "bybit": "WLDUSDT"},
    {"id": "jupiter-exchange-solana",   "symbol": "JUP",     "bybit": "JUPUSDT"},
    {"id": "dogwifcoin",                "symbol": "WIF",     "bybit": "WIFUSDT"},
    # ── 61-100 ──
    {"id": "ankr",                      "symbol": "ANKR",    "bybit": "ANKRUSDT"},
    {"id": "enjincoin",                 "symbol": "ENJ",     "bybit": "ENJUSDT"},
    {"id": "conflux-token",             "symbol": "CFX",     "bybit": "CFXUSDT"},
    {"id": "trust-wallet-token",        "symbol": "TWT",     "bybit": "TWTUSDT"},
    {"id": "1inch",                     "symbol": "1INCH",   "bybit": "1INCHUSDT"},
    {"id": "gala",                      "symbol": "GALA",    "bybit": "GALAUSDT"},
    {"id": "chiliz",                    "symbol": "CHZ",     "bybit": "CHZUSDT"},
    {"id": "band-protocol",             "symbol": "BAND",    "bybit": "BANDUSDT"},
    {"id": "nervos-network",            "symbol": "CKB",     "bybit": "CKBUSDT"},
    {"id": "zilliqa",                   "symbol": "ZIL",     "bybit": "ZILUSDT"},
    {"id": "vechain",                   "symbol": "VET",     "bybit": "VETUSDT"},
    {"id": "helium",                    "symbol": "HNT",     "bybit": "HNTUSDT"},
    {"id": "floki",                     "symbol": "FLOKI",   "bybit": "FLOKIUSDT"},
    {"id": "woo-network",               "symbol": "WOO",     "bybit": "WOOUSDT"},
    {"id": "ocean-protocol",            "symbol": "OCEAN",   "bybit": "OCEANUSDT"},
    {"id": "singularitynet",            "symbol": "AGIX",    "bybit": "AGIXUSDT"},
    {"id": "api3",                      "symbol": "API3",    "bybit": "API3USDT"},
    {"id": "blur",                      "symbol": "BLUR",    "bybit": "BLURUSDT"},
    {"id": "arkham",                    "symbol": "ARKM",    "bybit": "ARKMUSDT"},
    {"id": "akash-network",             "symbol": "AKT",     "bybit": "AKTUSDT"},
    {"id": "axie-infinity",             "symbol": "AXS",     "bybit": "AXSUSDT"},
    {"id": "sandbox",                   "symbol": "SAND",    "bybit": "SANDUSDT"},
    {"id": "decentraland",              "symbol": "MANA",    "bybit": "MANAUSDT"},
    {"id": "flow",                      "symbol": "FLOW",    "bybit": "FLOWUSDT"},
    {"id": "oasis-network",             "symbol": "ROSE",    "bybit": "ROSEUSDT"},
    {"id": "kusama",                    "symbol": "KSM",     "bybit": "KSMUSDT"},
    {"id": "pyth-network",              "symbol": "PYTH",    "bybit": "PYTHUSDT"},
    {"id": "compound-governance-token", "symbol": "COMP",    "bybit": "COMPUSDT"},
    {"id": "yearn-finance",             "symbol": "YFI",     "bybit": "YFIUSDT"},
    {"id": "wormhole",                  "symbol": "W",       "bybit": "WUSDT"},
    {"id": "io-net",                    "symbol": "IO",      "bybit": "IOUSDT"},
    {"id": "notcoin",                   "symbol": "NOT",     "bybit": "NOTUSDT"},
    {"id": "zksync",                    "symbol": "ZK",      "bybit": "ZKUSDT"},
    {"id": "raydium",                   "symbol": "RAY",     "bybit": "RAYUSDT"},
    {"id": "tensor",                    "symbol": "TNSR",    "bybit": "TNSRUSDT"},
    {"id": "portal",                    "symbol": "PORTAL",  "bybit": "PORTALUSDT"},
    {"id": "dogwifcoin",                "symbol": "WIF",     "bybit": "WIFUSDT"},
    {"id": "coredaoorg",                "symbol": "CORE",    "bybit": "COREUSDT"},
    {"id": "bitcoin-cash",              "symbol": "BCH",     "bybit": "BCHUSDT"},
    {"id": "maker",                     "symbol": "MKR",     "bybit": "MKRUSDT"},
    # ── 101-150 ──
    {"id": "algorand",                  "symbol": "ALGO",    "bybit": "ALGOUSDT"},
    {"id": "iota",                      "symbol": "IOTA",    "bybit": "IOTAUSDT"},
    {"id": "theta-token",               "symbol": "THETA",   "bybit": "THETAUSDT"},
    {"id": "elrond-erd-2",              "symbol": "EGLD",    "bybit": "EGLDUSDT"},
    {"id": "loopring",                  "symbol": "LRC",     "bybit": "LRCUSDT"},
    {"id": "basic-attention-token",     "symbol": "BAT",     "bybit": "BATUSDT"},
    {"id": "iotex",                     "symbol": "IOTX",    "bybit": "IOTXUSDT"},
    {"id": "ren",                       "symbol": "REN",     "bybit": "RENUSDT"},
    {"id": "storj",                     "symbol": "STORJ",   "bybit": "STORJUSDT"},
    {"id": "celo",                      "symbol": "CELO",    "bybit": "CELOSDT"},
    {"id": "harmony",                   "symbol": "ONE",     "bybit": "ONEUSDT"},
    {"id": "qtum",                      "symbol": "QTUM",    "bybit": "QTUMUSDT"},
    {"id": "icon",                      "symbol": "ICX",     "bybit": "ICXUSDT"},
    {"id": "ontology-gas",              "symbol": "ONG",     "bybit": "ONGUSDT"},
    {"id": "zeta-chain",                "symbol": "ZETA",    "bybit": "ZETAUSDT"},
    {"id": "ssv-network",               "symbol": "SSV",     "bybit": "SSVUSDT"},
    {"id": "civic",                     "symbol": "CVC",     "bybit": "CVCUSDT"},
    {"id": "dusk-network",              "symbol": "DUSK",    "bybit": "DUSKUSDT"},
    {"id": "nkn",                       "symbol": "NKN",     "bybit": "NKNUSDT"},
    {"id": "spell-token",               "symbol": "SPELL",   "bybit": "SPELLUSDT"},
    {"id": "audius",                    "symbol": "AUDIO",   "bybit": "AUDIOUSDT"},
    {"id": "alchemy-pay",               "symbol": "ACH",     "bybit": "ACHUSDT"},
    {"id": "nervos-network",            "symbol": "CKB",     "bybit": "CKBUSDT"},
    {"id": "verge",                     "symbol": "XVG",     "bybit": "XVGUSDT"},
    {"id": "ripple",                    "symbol": "XRP",     "bybit": "XRPUSDT"},
    {"id": "venus",                     "symbol": "XVS",     "bybit": "XVSUSDT"},
    {"id": "dego-finance",              "symbol": "DEGO",    "bybit": "DEGOUSDT"},
    {"id": "alpaca-finance",            "symbol": "ALPACA",  "bybit": "ALPACAUSDT"},
    {"id": "cream-finance",             "symbol": "CREAM",   "bybit": "CREAMUSDT"},
    {"id": "biswap",                    "symbol": "BSW",     "bybit": "BSWUSDT"},
    {"id": "truefi",                    "symbol": "TRU",     "bybit": "TRUUSDT"},
    {"id": "lazio-fan-token",           "symbol": "LAZIO",   "bybit": "LAZIOUSDT"},
    {"id": "mobox",                     "symbol": "MBOX",    "bybit": "MBOXUSDT"},
    {"id": "prom",                      "symbol": "PROM",    "bybit": "PROMUSDT"},
    {"id": "voxies",                    "symbol": "VOXEL",   "bybit": "VOXELUSDT"},
    {"id": "highstreet",                "symbol": "HIGH",    "bybit": "HIGHUSDT"},
    {"id": "beta-finance",              "symbol": "BETA",    "bybit": "BETAUSDT"},
    {"id": "football-fan-token",        "symbol": "PORTO",   "bybit": "PORTOUSDT"},
    {"id": "chess",                     "symbol": "CHESS",   "bybit": "CHESSUSDT"},
    {"id": "klay-token",                "symbol": "KLAY",    "bybit": "KLAYUSDT"},
    # ── 151-200 ──
    {"id": "spell-token",               "symbol": "SPELL",   "bybit": "SPELLUSDT"},
    {"id": "cocos-bcx",                 "symbol": "COCOS",   "bybit": "COCOSUSDT"},
    {"id": "derace",                    "symbol": "DERACE",  "bybit": "DERACEUSDT"},
    {"id": "orion-protocol",            "symbol": "ORN",     "bybit": "ORNUSDT"},
    {"id": "litentry",                  "symbol": "LIT",     "bybit": "LITUSDT"},
    {"id": "phala-network",             "symbol": "PHA",     "bybit": "PHAUSDT"},
    {"id": "clv-p",                     "symbol": "CLV",     "bybit": "CLVUSDT"},
    {"id": "bifrost-native-coin",       "symbol": "BNC",     "bybit": "BNCUSDT"},
    {"id": "xdefi-wallet",              "symbol": "XDEFI",   "bybit": "XDEFIUSDT"},
    {"id": "reef",                      "symbol": "REEF",    "bybit": "REEFUSDT"},
    {"id": "superverse",                "symbol": "SUPER",   "bybit": "SUPERUSDT"},
    {"id": "swipe",                     "symbol": "SXP",     "bybit": "SXPUSDT"},
    {"id": "ageur",                     "symbol": "AGEUR",   "bybit": "AGEURUSDT"},
    {"id": "stafi",                     "symbol": "FIS",     "bybit": "FISUSDT"},
    {"id": "loka",                      "symbol": "LOKA",    "bybit": "LOKAUSDT"},
    {"id": "bounce-token",              "symbol": "AUCTION", "bybit": "AUCTIONUSDT"},
    {"id": "prosper",                   "symbol": "PROS",    "bybit": "PROSUSDT"},
    {"id": "dfx-finance",               "symbol": "DFX",     "bybit": "DFXUSDT"},
    {"id": "nuls",                      "symbol": "NULS",    "bybit": "NULSUSDT"},
    {"id": "step-finance",              "symbol": "STEP",    "bybit": "STEPUSDT"},
    {"id": "alphalink",                 "symbol": "ALPHA",   "bybit": "ALPHAUSDT"},
    {"id": "tornado-cash",              "symbol": "TORN",    "bybit": "TORNUSDT"},
    {"id": "dodo",                      "symbol": "DODO",    "bybit": "DODOUSDT"},
    {"id": "burger-swap",               "symbol": "BURGER",  "bybit": "BURGERUSDT"},
    {"id": "automata",                  "symbol": "ATA",     "bybit": "ATAUSDT"},
    {"id": "gas",                       "symbol": "GAS",     "bybit": "GASUSDT"},
    {"id": "loom-network-new",          "symbol": "LOOM",    "bybit": "LOOMUSDT"},
    {"id": "bluzelle",                  "symbol": "BLZ",     "bybit": "BLZUSDT"},
    {"id": "polymath-network",          "symbol": "POLY",    "bybit": "POLYUSDT"},
    {"id": "venus-btc",                 "symbol": "VBTC",    "bybit": "VBTCUSDT"},
    {"id": "joe",                       "symbol": "JOE",     "bybit": "JOEUSDT"},
    {"id": "hegic",                     "symbol": "HEGIC",   "bybit": "HEGICUSDT"},
    {"id": "mdex",                      "symbol": "MDX",     "bybit": "MDXUSDT"},
    {"id": "ribbon-finance",            "symbol": "RBN",     "bybit": "RBNUSDT"},
    {"id": "tornado-cash",              "symbol": "TORN",    "bybit": "TORNUSDT"},
    {"id": "unifi-protocol-dao",        "symbol": "UNFI",    "bybit": "UNFIUSDT"},
    {"id": "terra-luna-2",              "symbol": "LUNA",    "bybit": "LUNAUSDT"},
    {"id": "synapse-2",                 "symbol": "SYN",     "bybit": "SYNUSDT"},
    {"id": "bezoge-earth",              "symbol": "BEZOGE",  "bybit": "BEZOGEUSDT"},
    {"id": "boba-network",              "symbol": "BOBA",    "bybit": "BOBAUSDT"},
    {"id": "measurable-data-token",     "symbol": "MDT",     "bybit": "MDTUSDT"},
    {"id": "vite",                      "symbol": "VITE",    "bybit": "VITEUSDT"},
    {"id": "wazirx",                    "symbol": "WRX",     "bybit": "WRXUSDT"},
    {"id": "dfi-money",                 "symbol": "YFII",    "bybit": "YFIIUSDT"},
    {"id": "barnbridge",                "symbol": "BOND",    "bybit": "BONDUSDT"},
    {"id": "xyo-network",               "symbol": "XYO",     "bybit": "XYOUSDT"},
    {"id": "telos",                     "symbol": "TLOS",    "bybit": "TLOSUSDT"},
    {"id": "oraichain-token",           "symbol": "ORAI",    "bybit": "ORAIUSDT"},
    {"id": "hifi-finance",              "symbol": "HIFI",    "bybit": "HIFIUSDT"},
    {"id": "magic",                     "symbol": "MAGIC",   "bybit": "MAGICUSDT"},
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
    if price is None:
        return "N/A"
    if decimals is None:
        if price >= 1000:    decimals = 1
        elif price >= 100:   decimals = 2
        elif price >= 1:     decimals = 4
        elif price >= 0.01:  decimals = 5
        else:                decimals = 8
    return f"${price:.{decimals}f}"


def calc_trade_size():
    """Return margin for this trade — dynamic or fixed."""
    if USE_DYNAMIC_SIZING:
        with state_lock:
            bal = paper_balance
        sized = round(bal * RISK_PCT, 2)
        return max(MIN_TRADE_SIZE, min(MAX_TRADE_SIZE, sized))
    return TRADE_SIZE


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
    if vol_ratio and vol_ratio > 3:       base = 0.042
    elif vol_ratio and vol_ratio > 2:     base = 0.034
    elif rsi and (rsi < 25 or rsi > 75):  base = 0.036

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

    if change > 8:     score += 6
    elif change > 6:   score += 5
    elif change > 4:   score += 4
    elif change > 2:   score += 2
    elif change < -8:  score -= 6
    elif change < -6:  score -= 5
    elif change < -4:  score -= 4
    elif change < -2:  score -= 2
    else:
        return None

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
        conf = min(95, 70 + score * 3);      direction = "BUY"
    elif score >= 5:
        conf = min(88, 75 + score * 2);      direction = "BUY"
    elif score <= -7:
        conf = min(95, 70 + abs(score) * 3); direction = "SELL"
    elif score <= -5:
        conf = min(88, 75 + abs(score) * 2); direction = "SELL"
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
            open_pos[sym] = {
                "sym":        sym,
                "direction":  pos["direction"],
                "entry":      pos["entry"],
                "execPrice":  pos["exec_price"],
                "tp1":        pos["tp1"],
                "tp2":        pos["tp2"],
                "tp3":        pos["tp3"],
                "tp4":        pos["tp4"],
                "sl":         pos["sl"],
                "liqPrice":   pos["liq_price"],
                "tpHit":      pos["tp_hit"],
                "breakeven":  pos.get("breakeven", False),
                "margin":     pos["margin"],
                "currentPnl": pos.get("currentPnl", 0),
                "openTime":   pos.get("opened_at", 0),
            }

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
            "pnlHistory":    stats["pnl_history"],
            "leverage":      LEVERAGE,
            "tradeSize":     TRADE_SIZE,
            "timestamp":     utc_now_str(),
        }

    response = jsonify(payload)
    response.headers.add("Access-Control-Allow-Origin", "*")
    return response


def start_flask():
    # FIX: suppress Flask/werkzeug development server warning
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ─────────────────────────── TELEGRAM ─────────────────────────
def tg_send(msg):
    if not TG_TOKEN or not TG_CHAT:
        print("TG:", msg[:80])
        return None
    data = {
        "chat_id":                  TG_CHAT,
        "text":                     msg,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
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


def make_signal_id(sym):
    """Generate a short unique signal ID e.g. SOL-0417-1423"""
    now = datetime.now(timezone.utc)
    return f"{sym}-{now.strftime('%m%d')}-{now.strftime('%H%M')}"


def tp_progress_bar(tp_hit, direction):
    """Visual TP progress — e.g. TP1✅ TP2✅ TP3⬜ TP4⬜"""
    icons = []
    for i in range(1, 5):
        if i <= tp_hit:
            icons.append(f"TP{i}✅")
        else:
            icons.append(f"TP{i}⬜")
    return "  ".join(icons)


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
    sig_id    = sig.get("sig_id", make_signal_id(coin["symbol"]))

    rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"
    ema_str = ("↑ Uptrend" if ema20 > ema50 else "↓ Downtrend") if (ema20 and ema50) else "N/A"
    vol_str = f"{vol_ratio:.1f}x avg" if vol_ratio else "N/A"
    lev_ret = [round(p * LEVERAGE, 1) for p in tp_pcts]

    trade_size = calc_trade_size()
    notional   = trade_size * LEVERAGE

    return (
        f"<b>📝 APEX SIGNAL — #{sig_id}</b>\n"
        f"══════════════════════════════\n"
        f"{arrow} <b>{side_word} — {coin['symbol']}/USDT</b>\n\n"
        f"⚙️ {LEVERAGE}x Leverage | ${trade_size:.0f} margin → ${notional:.0f} exposure\n\n"
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


def make_tp_msg(sym, direction, tp_num, entry, exec_price, tp_price, elapsed, pnl_usdt, new_sl=None, sig_id=None, tp_hit_total=0):
    arrow     = "🟢" if direction == "BUY" else "🔴"
    side_word = "LONG" if direction == "BUY" else "SHORT"
    sl_note   = f"\n💡 SL → {fmt_p(new_sl)} (breakeven)" if tp_num == 1 and new_sl else \
                f"\n💡 SL trailed to TP{tp_num - 1}" if tp_num > 1 and new_sl else ""
    id_line   = f"#{sig_id}  |  " if sig_id else ""

    with state_lock:
        total      = stats["total"]
        trades_won = stats["trades_won"]
        win_rate   = round(trades_won / total * 100, 1) if total > 0 else 0
        net_pnl    = round(stats["profit_usdt"] - stats["loss_usdt"], 2)
        net_sign   = "+" if net_pnl >= 0 else ""
        balance    = paper_balance

    entry_note = f" (exec {fmt_p(exec_price)})" if abs(exec_price - entry) / entry > 0.0005 else ""
    progress   = tp_progress_bar(tp_hit_total, direction)

    return (
        f"<b>✅ TP{tp_num} HIT — {sym} {side_word}</b> {arrow}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 {id_line}Entry: {fmt_p(entry)}{entry_note}\n"
        f"{progress}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"TP{tp_num} hit: {fmt_p(tp_price)}\n"
        f"Time in trade: {elapsed_str(elapsed)}\n"
        f"Est. +${pnl_usdt:.2f} USDT (25% closed){sl_note}\n\n"
        f"📊 Session stats:\n"
        f"Win rate: {trades_won}/{total} = {win_rate}%\n"
        f"Net P&L: {net_sign}${abs(net_pnl):.2f} USDT\n"
        f"Balance: ${balance:.2f} USDT"
    )


def make_sl_msg(sym, direction, entry, exec_price, sl_price, elapsed, pnl_usdt, breakeven=False, sig_id=None, tp_hit_total=0):
    side_word = "LONG" if direction == "BUY" else "SHORT"
    be_str    = " (breakeven — no loss!)" if breakeven else ""
    sign      = "+" if breakeven else "-"
    id_line   = f"#{sig_id}  |  " if sig_id else ""

    with state_lock:
        total      = stats["total"]
        trades_won = stats["trades_won"]
        win_rate   = round(trades_won / total * 100, 1) if total > 0 else 0
        net_pnl    = round(stats["profit_usdt"] - stats["loss_usdt"], 2)
        net_sign   = "+" if net_pnl >= 0 else ""
        balance    = paper_balance

    entry_note = f" (exec {fmt_p(exec_price)})" if abs(exec_price - entry) / entry > 0.0005 else ""
    progress   = tp_progress_bar(tp_hit_total, direction)

    return (
        f"<b>{'✅' if breakeven else '❌'} SL HIT{be_str} — {sym} {side_word}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 {id_line}Entry: {fmt_p(entry)}{entry_note}\n"
        f"{progress}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"SL hit: {fmt_p(sl_price)}\n"
        f"Time in trade: {elapsed_str(elapsed)}\n"
        f"Est. {sign}${pnl_usdt:.2f} USDT\n\n"
        f"📊 Session stats:\n"
        f"Win rate: {trades_won}/{total} = {win_rate}%\n"
        f"Net P&L: {net_sign}${abs(net_pnl):.2f} USDT\n"
        f"Balance: ${balance:.2f} USDT"
    )


# ──────────────────────── PAPER EXECUTE ───────────────────────
def paper_execute(coin, sig, price):
    """Simulate opening a leveraged position instantly."""
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

    # All state mutations under lock; TG calls outside to avoid holding lock during HTTP
    with state_lock:
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
                "sig_id":                  sig_id,
            }
            bal_after  = paper_balance
            open_count = len(positions)

    # Send TG outside lock — no HTTP while holding state_lock
    if bal_snap is not None:
        tg_send(
            f"<b>⚠️ Paper balance too low — {sym}</b>\n\n"
            f"Balance: ${bal_snap:.2f} USDT\n"
            f"Required margin: ${trade_size:.0f} USDT\n\nSkipping trade."
        )
        return False

    side_word = "LONG" if direction == "BUY" else "SHORT"
    lev_ret   = [round(p * LEVERAGE, 1) for p in sig["tp_pcts"]]
    slip_note = f"\n⚡ Exec: {fmt_p(exec_price)} (slippage applied)" \
                if abs(exec_price - price) / price > 0.0001 else ""

    tg_send(
        f"<b>📝 PAPER TRADE ENTERED — #{sig_id}</b>\n"
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
        f"🤖 Monitoring: auto TP/SL + trailing stop"
    )
    print(f"  📝 Paper trade: {direction} {sym} @ {exec_price} (signal {price}) qty={qty}")
    return True


# ──────────────────────── POSITION MONITOR ────────────────────
def monitor_positions(prices):
    """
    FIX: Collect all notifications in a list, send AFTER releasing lock.
    This prevents holding state_lock during slow HTTP calls to Telegram,
    keeping the Flask /data endpoint responsive.
    """
    global paper_balance
    to_remove     = []
    notifications = []   # (msg_str,) — built inside lock, sent outside

    for sym, pos in list(positions.items()):
        coin_data = next((c for c in COINS if c["symbol"] == sym), None)
        if not coin_data:
            to_remove.append(sym)
            continue

        d     = prices.get(coin_data["id"]) or {}
        price = d.get("usd")
        if not price:
            continue

        direction  = pos["direction"]
        entry      = pos["entry"]
        exec_price = pos["exec_price"]
        sl         = pos["sl"]
        liq_price  = pos["liq_price"]
        tp_hit     = pos["tp_hit"]
        elapsed    = time.time() - pos["opened_at"]
        tp_levels  = [pos["tp1"], pos["tp2"], pos["tp3"], pos["tp4"]]
        trade_size = pos["margin"]

        with state_lock:
            # ── Funding deduction every 8 hours ──
            funding_due = int(elapsed / 28800)
            new_periods = funding_due - pos.get("funding_periods_charged", 0)
            if new_periods > 0:
                rem_pct      = 1.0 - (tp_hit * 0.25)
                funding_cost = trade_size * LEVERAGE * FUNDING_RATE * new_periods * rem_pct
                paper_balance -= funding_cost
                pos["funding_periods_charged"] = funding_due
                stats["pnl_history"].append(round(paper_balance, 2))
                print(f"  💸 Funding: {sym} -${funding_cost:.4f} ({new_periods} period(s))")

            # ── Liquidation check ──
            liq_hit = (direction == "BUY"  and price <= liq_price) or \
                      (direction == "SELL" and price >= liq_price)

            if liq_hit:
                rem_pct          = 1.0 - (tp_hit * 0.25)
                remaining_margin = trade_size * rem_pct
                stats["loss_usdt"] += remaining_margin
                stats["total"]     += 1
                stats["sl_hit"]    += 1
                stats["pnl_history"].append(round(paper_balance, 2))
                stats["trades_list"].append({
                    "sym": sym, "direction": direction,
                    "result": "LIQUIDATED", "pnl": -remaining_margin,
                    "time": utc_now_str(),
                })
                notifications.append(
                    f"<b>💀 LIQUIDATED — {sym}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 #{pos.get('sig_id', '?')}  |  Entry: {fmt_p(entry)}\n"
                    f"{tp_progress_bar(tp_hit, direction)}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Price hit liquidation at {fmt_p(liq_price)}\n"
                    f"Margin lost: ${remaining_margin:.2f} USDT\n\n"
                    f"Balance: ${paper_balance:.2f} USDT"
                )
                to_remove.append(sym)
                continue

            # ── SL check ──
            sl_hit = (direction == "BUY"  and price <= sl) or \
                     (direction == "SELL" and price >= sl)

            if sl_hit:
                rem_pct          = 1.0 - (tp_hit * 0.25)
                price_move       = abs(sl - exec_price) / exec_price * 100
                is_profit        = (direction == "BUY"  and sl >= exec_price) or \
                                   (direction == "SELL" and sl <= exec_price)
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
                stats["pnl_history"].append(round(paper_balance, 2))
                stats["trades_list"].append({
                    "sym": sym, "direction": direction, "result": "SL",
                    "pnl": pnl_usdt if is_profit else -pnl_usdt,
                    "time": utc_now_str(),
                })
                # Build message inside lock (reads stats), send outside
                notifications.append(
                    make_sl_msg(sym, direction, entry, exec_price, sl,
                                elapsed, pnl_usdt, pos.get("breakeven"),
                                sig_id=pos.get("sig_id"), tp_hit_total=tp_hit)
                )
                to_remove.append(sym)
                continue

            # ── TP check ──
            if tp_hit >= 4:
                to_remove.append(sym)
                continue

            next_tp    = tp_levels[tp_hit]
            tp_reached = (direction == "BUY"  and price >= next_tp) or \
                         (direction == "SELL" and price <= next_tp)

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

                # FIX: increment trades_won on first TP hit (once per trade)
                if not pos["first_tp_counted"]:
                    stats["trades_won"]    += 1
                    pos["first_tp_counted"] = True

                # Trail SL
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
                    stats["total"] += 1
                    stats["trades_list"].append({
                        "sym": sym, "direction": direction, "result": "ALL_TP",
                        "pnl": round(pos["currentPnl"], 2), "time": utc_now_str(),
                    })
                    total      = stats["total"]
                    trades_won = stats["trades_won"]
                    win_rate   = round(trades_won / total * 100, 1) if total > 0 else 0
                    net_pnl    = round(stats["profit_usdt"] - stats["loss_usdt"], 2)
                    notifications.append(
                        f"<b>🎯 ALL 4 TPs HIT — {sym}!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📌 #{pos.get('sig_id', '?')}  |  Entry: {fmt_p(entry)}\n"
                        f"{tp_progress_bar(4, direction)}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"[PAPER TRADE] Perfect signal!\n\n"
                        f"📊 Session stats:\n"
                        f"Win rate: {trades_won}/{total} = {win_rate}%\n"
                        f"Net P&L: +${net_pnl:.2f} USDT\n"
                        f"Balance: ${paper_balance:.2f} USDT"
                    )
                    to_remove.append(sym)
                else:
                    notifications.append(
                        make_tp_msg(sym, direction, tp_num, entry, exec_price,
                                    next_tp, elapsed, pnl_usdt, new_sl,
                                    sig_id=pos.get("sig_id"), tp_hit_total=tp_num)
                    )
                    print(f"  TP{tp_num} hit: {sym} @ ${price}")

        # End of with state_lock

    # Cleanup positions
    with state_lock:
        for sym in to_remove:
            positions.pop(sym, None)

    # FIX: send all notifications OUTSIDE the lock — no HTTP while holding state_lock
    for msg in notifications:
        tg_send(msg)


# ─────────────────────────── MAIN ─────────────────────────────
def run():
    # FIX: declare both globals so pre_warned pruning updates the real global dict
    global paper_balance, pre_warned

    print("=" * 55)
    print("  APEX Bybit Bot — PAPER TRADING MODE")
    print("=" * 55)
    sizing_note = f"dynamic ({RISK_PCT*100:.0f}% of balance)" if USE_DYNAMIC_SIZING else "fixed"
    print(f"Starting balance: ${PAPER_BALANCE} USDT (simulated)")
    print(f"Trade size: {sizing_note} × {LEVERAGE}x leverage")
    print(f"Slippage: {SLIPPAGE_PCT*100:.2f}% | Funding: {FUNDING_RATE*100:.3f}%/8h")
    print(f"Telegram: {'OK' if TG_TOKEN else 'MISSING'}")
    print(f"Blocked: {BLOCKED_COINS}")

    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()
    print(f"Dashboard API running on port {os.environ.get('PORT', 8080)}")

    tg_send(
        "<b>📝 APEX Paper Trading Bot — Online!</b>\n\n"
        f"Exchange: <b>Bybit Futures (SIMULATED)</b>\n"
        f"Leverage: <b>{LEVERAGE}x</b>\n"
        f"Sizing: <b>{sizing_note}</b>\n"
        f"Starting balance: <b>${PAPER_BALANCE} USDT</b>\n"
        f"Coins monitored: <b>{len(COINS)}</b>\n"
        f"Min confidence: <b>{MIN_CONF}%</b>\n\n"
        f"<b>Signal Engine:</b>\n"
        f"• RSI(14) — blocks overbought/oversold\n"
        f"• EMA 20/50 trend filter\n"
        f"• Volume spike detection\n"
        f"• 4-target GG Shot style signals\n"
        f"• BUY (Long) + SELL (Short) signals\n\n"
        f"<b>Simulation:</b>\n"
        f"• Entry slippage: {SLIPPAGE_PCT*100:.2f}%\n"
        f"• Funding rate: {FUNDING_RATE*100:.3f}%/8h\n"
        f"• Liquidation price tracked\n\n"
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

            if time.time() - last_price_t >= 10:
                prices = get_prices()
                last_price_t = time.time()

            if prices:
                with state_lock:
                    open_syms = list(positions.keys())
                if open_syms:
                    monitor_positions(prices)

            # ── SCAN ──
            if time.time() - last_scan_at >= SCAN_EVERY_SECONDS:
                last_scan_at = time.time()

                with state_lock:
                    open_count = len(positions)
                    balance    = paper_balance

                print(
                    f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                    f"Scanning {len(COINS)} coins... "
                    f"(open: {open_count}, balance: ${balance:.2f})"
                )

                # FIX: prune stale pre-warns properly using global declaration above
                now = time.time()
                pre_warned = {k: v for k, v in pre_warned.items() if now - v < PRE_WARN_TTL}

                scan_prices = get_prices()
                if scan_prices:
                    prices = scan_prices

                    ta_candidates = []
                    for coin in COINS:
                        sym = coin["symbol"]
                        if sym in BLOCKED_COINS:
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

                        print(f"  {sym}: ${price} {round(change, 2)}%", end="")

                        ta = ta_map.get(sym)
                        if ta:
                            trend = "↑" if ta.get("ema20") and ta.get("ema50") and \
                                          ta["ema20"] > ta["ema50"] else "↓"
                            print(f" | RSI={ta.get('rsi','?')} EMA={trend} "
                                  f"Vol={ta.get('vol_ratio', 1.0):.1f}x", end="")

                        sig = build_signal(price, change, high, low, vol, ta)
                        print()

                        if not sig or sig["conf"] < MIN_CONF:
                            if sig and sig["conf"] >= MIN_CONF - 8 and sym not in pre_warned:
                                pre_warned[sym] = time.time()
                                tg_send(make_pre_warn(coin, sig["signal"], price))
                                print(f"  ⚠️ Pre-warn: {sym}")
                            continue

                        prev = last_signal.get(sym)
                        if prev and prev["signal"] == sig["signal"] and \
                                abs(prev.get("entry", 0) - price) / price < 0.005:
                            continue

                        with state_lock:
                            if len(positions) >= MAX_OPEN_TRADES:
                                print(f"  Max trades reached, skipping {sym}")
                                continue
                            pre_warned.pop(sym, None)

                        sig["entry"]     = price
                        sig["sig_id"]    = make_signal_id(sym)
                        last_signal[sym] = sig

                        tg_send(make_signal_msg(coin, sig, price, change))
                        paper_execute(coin, sig, price)
                        print(f"  🚀 Paper signal: {sym} {sig['signal']} {sig['conf']}%")

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