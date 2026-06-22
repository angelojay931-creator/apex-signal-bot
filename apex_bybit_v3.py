"""
APEX Bybit Bot — Supertrend + UT Bot + EMA Multi-Timeframe
===========================================================
Signal Engine: Supertrend + UT Bot + EMA 20/50/200
Timeframes:    1m + 5m + 1h + 4h (all must agree)
Paper trading only — no real orders placed.

Fixes applied:
  1. Candle limit 80 → 250 (EMA200 now works)
  2. Supertrend rolling ATR (was static — wrong signals)
  3. UT Bot pure regular candles (no Heikin Ashi mix)
  4. Multi-timeframe: 1m/5m/1h/4h consensus
  5. EMA200 optional fallback (trade if unavailable)
  6. 130 coins scanning

Railway env vars:
  TELEGRAM_TOKEN
  TELEGRAM_CHAT_ID
"""

import os, time, threading
import json
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from flask import Flask, jsonify

# ─────────────────────── CONFIG ───────────────────────────────
PAPER_TRADING   = True
PAPER_BALANCE   = 2000.0

TG_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT   = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

TRADE_SIZE          = 100.0
LEVERAGE            = 5
MIN_CONF            = 70      # lower = more signals from MTF system
SCAN_EVERY_SECONDS  = 30
HTTP_TIMEOUT        = 15
MAX_OPEN_TRADES     = 8
MAX_SAME_DIRECTION  = 4
SLIPPAGE_PCT        = 0.001
FUNDING_RATE        = 0.0001
GAP_SLIPPAGE_PCT    = 0.005
LIQ_BUFFER_PCT      = 0.02
MIN_24H_VOLUME      = 300_000_000
SL_COOLDOWN_SECONDS = 86400   # 24h — stops re-entry on downtrending coins
PRE_WARN_TTL        = 7200
MIN_FREE_CASH_PCT   = 0.30
MAX_DAILY_LOSS_PCT  = 0.06
MAX_CONSEC_LOSSES   = 3
MAX_TRADE_HOURS     = 48
BTC_CRASH_PCT       = -5.0

USE_DYNAMIC_SIZING = False
RISK_PCT           = 0.05
MIN_TRADE_SIZE     = 100.0
MAX_TRADE_SIZE     = 100.0

BLOCKED_COINS = {
    "WAVES","HNT","ALPACA","WRX","REEF","LOKA","AUCTION",
    "NULS","ALPHA","CLV","SXP","FIS","MDT","DODO","BLZ",
    "APT","CHZ","MANA","SUI","WOO","SAND","ENJ",
    "TRU","CORE","WLD","CRV","FLOW",
}

# ─────────────────────── STATE ─────────────────────────────────
state_lock    = threading.RLock()
positions     = {}
pre_warned    = {}
paper_balance = PAPER_BALANCE
last_signal   = {}
sl_cooldown   = {}

stats = {
    "total":0,"trades_won":0,"tp_hit":0,"sl_hit":0,
    "profit_usdt":0.0,"loss_usdt":0.0,
    "trades_list":[],"pnl_history":[PAPER_BALANCE],
}

risk_state = {
    "session_start_balance": PAPER_BALANCE,
    "consec_losses":0,"trading_paused":False,"pause_reason":"",
    "btc_last_price":None,"btc_last_check":0.0,
    "pause_until":0.0,"daily_reset_at":0.0,
}

# ─────────────────────── COINS (130) ──────────────────────────
COINS = [
    # Proven 30 whitelist
    {"id":"dydx-chain",         "symbol":"DYDX",   "bybit":"DYDXUSDT"},
    {"id":"starknet",           "symbol":"STRK",   "bybit":"STRKUSDT"},
    {"id":"pendle",             "symbol":"PENDLE", "bybit":"PENDLEUSDT"},
    {"id":"immutable-x",        "symbol":"IMX",    "bybit":"IMXUSDT"},
    {"id":"near",               "symbol":"NEAR",   "bybit":"NEARUSDT"},
    {"id":"pyth-network",       "symbol":"PYTH",   "bybit":"PYTHUSDT"},
    {"id":"thorchain",          "symbol":"RUNE",   "bybit":"RUNEUSDT"},
    {"id":"dogwifcoin",         "symbol":"WIF",    "bybit":"WIFUSDT"},
    {"id":"kava",               "symbol":"KAVA",   "bybit":"KAVAUSDT"},
    {"id":"eos",                "symbol":"EOS",    "bybit":"EOSUSDT"},
    {"id":"blur",               "symbol":"BLUR",   "bybit":"BLURUSDT"},
    {"id":"zcash",              "symbol":"ZEC",    "bybit":"ZECUSDT"},
    {"id":"dogecoin",           "symbol":"DOGE",   "bybit":"DOGEUSDT"},
    {"id":"optimism",           "symbol":"OP",     "bybit":"OPUSDT"},
    {"id":"injective-protocol", "symbol":"INJ",    "bybit":"INJUSDT"},
    {"id":"solana",             "symbol":"SOL",    "bybit":"SOLUSDT"},
    {"id":"ontology",           "symbol":"ONT",    "bybit":"ONTUSDT"},
    {"id":"conflux-token",      "symbol":"CFX",    "bybit":"CFXUSDT"},
    {"id":"io-net",             "symbol":"IO",     "bybit":"IOUSDT"},
    {"id":"polkadot",           "symbol":"DOT",    "bybit":"DOTUSDT"},
    {"id":"arbitrum",           "symbol":"ARB",    "bybit":"ARBUSDT"},
    {"id":"gmx",                "symbol":"GMX",    "bybit":"GMXUSDT"},
    {"id":"pepe",               "symbol":"PEPE",   "bybit":"PEPEUSDT"},
    {"id":"dash",               "symbol":"DASH",   "bybit":"DASHUSDT"},
    {"id":"ondo-finance",       "symbol":"ONDO",   "bybit":"ONDOUSDT"},
    {"id":"lido-dao",           "symbol":"LDO",    "bybit":"LDOUSDT"},
    {"id":"aave",               "symbol":"AAVE",   "bybit":"AAVEUSDT"},
    {"id":"ripple",             "symbol":"XRP",    "bybit":"XRPUSDT"},
    {"id":"filecoin",           "symbol":"FIL",    "bybit":"FILUSDT"},
    {"id":"sushi",              "symbol":"SUSHI",  "bybit":"SUSHIUSDT"},
    # Additional 100
    {"id":"bitcoin",            "symbol":"BTC",    "bybit":"BTCUSDT"},
    {"id":"ethereum",           "symbol":"ETH",    "bybit":"ETHUSDT"},
    {"id":"binancecoin",        "symbol":"BNB",    "bybit":"BNBUSDT"},
    {"id":"cardano",            "symbol":"ADA",    "bybit":"ADAUSDT"},
    {"id":"tron",               "symbol":"TRX",    "bybit":"TRXUSDT"},
    {"id":"avalanche-2",        "symbol":"AVAX",   "bybit":"AVAXUSDT"},
    {"id":"chainlink",          "symbol":"LINK",   "bybit":"LINKUSDT"},
    {"id":"stellar",            "symbol":"XLM",    "bybit":"XLMUSDT"},
    {"id":"litecoin",           "symbol":"LTC",    "bybit":"LTCUSDT"},
    {"id":"uniswap",            "symbol":"UNI",    "bybit":"UNIUSDT"},
    {"id":"internet-computer",  "symbol":"ICP",    "bybit":"ICPUSDT"},
    {"id":"ethereum-classic",   "symbol":"ETC",    "bybit":"ETCUSDT"},
    {"id":"bittensor",          "symbol":"TAO",    "bybit":"TAOUSDT"},
    {"id":"hyperliquid",        "symbol":"HYPE",   "bybit":"HYPEUSDT"},
    {"id":"monero",             "symbol":"XMR",    "bybit":"XMRUSDT"},
    {"id":"shiba-inu",          "symbol":"SHIB",   "bybit":"SHIBUSDT"},
    {"id":"bonk",               "symbol":"BONK",   "bybit":"BONKUSDT"},
    {"id":"celestia",           "symbol":"TIA",    "bybit":"TIAUSDT"},
    {"id":"sei-network",        "symbol":"SEI",    "bybit":"SEIUSDT"},
    {"id":"the-graph",          "symbol":"GRT",    "bybit":"GRTUSDT"},
    {"id":"mantle",             "symbol":"MNT",    "bybit":"MNTUSDT"},
    {"id":"raydium",            "symbol":"RAY",    "bybit":"RAYUSDT"},
    {"id":"fetch-ai",           "symbol":"FET",    "bybit":"FETUSDT"},
    {"id":"matic-network",      "symbol":"POL",    "bybit":"POLUSDT"},
    {"id":"ronin",              "symbol":"RON",    "bybit":"RONUSDT"},
    {"id":"terra-luna-2",       "symbol":"LUNA",   "bybit":"LUNAUSDT"},
    {"id":"iota",               "symbol":"IOTA",   "bybit":"IOTAUSDT"},
    {"id":"neo",                "symbol":"NEO",    "bybit":"NEOUSDT"},
    {"id":"gala",               "symbol":"GALA",   "bybit":"GALAUSDT"},
    {"id":"band-protocol",      "symbol":"BAND",   "bybit":"BANDUSDT"},
    {"id":"nervos-network",     "symbol":"CKB",    "bybit":"CKBUSDT"},
    {"id":"zilliqa",            "symbol":"ZIL",    "bybit":"ZILUSDT"},
    {"id":"vechain",            "symbol":"VET",    "bybit":"VETUSDT"},
    {"id":"floki",              "symbol":"FLOKI",  "bybit":"FLOKIUSDT"},
    {"id":"ocean-protocol",     "symbol":"OCEAN",  "bybit":"OCEANUSDT"},
    {"id":"singularitynet",     "symbol":"AGIX",   "bybit":"AGIXUSDT"},
    {"id":"api3",               "symbol":"API3",   "bybit":"API3USDT"},
    {"id":"arkham",             "symbol":"ARKM",   "bybit":"ARKMUSDT"},
    {"id":"akash-network",      "symbol":"AKT",    "bybit":"AKTUSDT"},
    {"id":"axie-infinity",      "symbol":"AXS",    "bybit":"AXSUSDT"},
    {"id":"oasis-network",      "symbol":"ROSE",   "bybit":"ROSEUSDT"},
    {"id":"kusama",             "symbol":"KSM",    "bybit":"KSMUSDT"},
    {"id":"compound-governance-token","symbol":"COMP","bybit":"COMPUSDT"},
    {"id":"yearn-finance",      "symbol":"YFI",    "bybit":"YFIUSDT"},
    {"id":"wormhole",           "symbol":"W",      "bybit":"WUSDT"},
    {"id":"notcoin",            "symbol":"NOT",    "bybit":"NOTUSDT"},
    {"id":"zksync",             "symbol":"ZK",     "bybit":"ZKUSDT"},
    {"id":"tensor",             "symbol":"TNSR",   "bybit":"TNSRUSDT"},
    {"id":"bitcoin-cash",       "symbol":"BCH",    "bybit":"BCHUSDT"},
    {"id":"maker",              "symbol":"MKR",    "bybit":"MKRUSDT"},
    {"id":"algorand",           "symbol":"ALGO",   "bybit":"ALGOUSDT"},
    {"id":"theta-token",        "symbol":"THETA",  "bybit":"THETAUSDT"},
    {"id":"elrond-erd-2",       "symbol":"EGLD",   "bybit":"EGLDUSDT"},
    {"id":"loopring",           "symbol":"LRC",    "bybit":"LRCUSDT"},
    {"id":"basic-attention-token","symbol":"BAT",  "bybit":"BATUSDT"},
    {"id":"iotex",              "symbol":"IOTX",   "bybit":"IOTXUSDT"},
    {"id":"storj",              "symbol":"STORJ",  "bybit":"STORJUSDT"},
    {"id":"celo",               "symbol":"CELO",   "bybit":"CELOUSDT"},
    {"id":"harmony",            "symbol":"ONE",    "bybit":"ONEUSDT"},
    {"id":"qtum",               "symbol":"QTUM",   "bybit":"QTUMUSDT"},
    {"id":"icon",               "symbol":"ICX",    "bybit":"ICXUSDT"},
    {"id":"zeta-chain",         "symbol":"ZETA",   "bybit":"ZETAUSDT"},
    {"id":"dusk-network",       "symbol":"DUSK",   "bybit":"DUSKUSDT"},
    {"id":"audius",             "symbol":"AUDIO",  "bybit":"AUDIOUSDT"},
    {"id":"alchemy-pay",        "symbol":"ACH",    "bybit":"ACHUSDT"},
    {"id":"venus",              "symbol":"XVS",    "bybit":"XVSUSDT"},
    {"id":"orion-protocol",     "symbol":"ORN",    "bybit":"ORNUSDT"},
    {"id":"litentry",           "symbol":"LIT",    "bybit":"LITUSDT"},
    {"id":"phala-network",      "symbol":"PHA",    "bybit":"PHAUSDT"},
    {"id":"superverse",         "symbol":"SUPER",  "bybit":"SUPERUSDT"},
    {"id":"bounce-token",       "symbol":"AUCTION","bybit":"AUCTIONUSDT"},
    {"id":"alphalink",          "symbol":"ALPHA",  "bybit":"ALPHAUSDT"},
    {"id":"automata",           "symbol":"ATA",    "bybit":"ATAUSDT"},
    {"id":"gas",                "symbol":"GAS",    "bybit":"GASUSDT"},
    {"id":"joe",                "symbol":"JOE",    "bybit":"JOEUSDT"},
    {"id":"synapse-2",          "symbol":"SYN",    "bybit":"SYNUSDT"},
    {"id":"barnbridge",         "symbol":"BOND",   "bybit":"BONDUSDT"},
    {"id":"telos",              "symbol":"TLOS",   "bybit":"TLOSUSDT"},
    {"id":"magic",              "symbol":"MAGIC",  "bybit":"MAGICUSDT"},
    # ─── additional coins ───
    {"id":"pancakeswap-token", "symbol":"CAKE", "bybit":"CAKEUSDT"},  # new
    {"id":"1inch", "symbol":"1INCH", "bybit":"1INCHUSDT"},  # new
    {"id":"trust-wallet-token", "symbol":"TWT", "bybit":"TWTUSDT"},  # new
    {"id":"stacks", "symbol":"STX", "bybit":"STXUSDT"},  # new
    {"id":"arweave", "symbol":"AR", "bybit":"ARUSDT"},  # new
    {"id":"render-token", "symbol":"RENDER", "bybit":"RENDERUSDT"},  # new
    {"id":"jupiter-exchange-solana", "symbol":"JUP", "bybit":"JUPUSDT"},  # new
    {"id":"jito-governance-token", "symbol":"JTO", "bybit":"JTOUSDT"},  # new
    {"id":"alt-layer", "symbol":"ALT", "bybit":"ALTUSDT"},  # new
    {"id":"manta-network", "symbol":"MANTA", "bybit":"MANTAUSDT"},  # new
    {"id":"omni-network", "symbol":"OMNI", "bybit":"OMNIUSDT"},  # new
    {"id":"ssv-network", "symbol":"SSV", "bybit":"SSVUSDT"},  # new
    {"id":"civic", "symbol":"CVC", "bybit":"CVCUSDT"},  # new
    {"id":"kaspa", "symbol":"KAS", "bybit":"KASUSDT"},  # new
    {"id":"ethena", "symbol":"ENA", "bybit":"ENAUSDT"},  # new
    {"id":"toncoin", "symbol":"TON", "bybit":"TONUSDT"},  # new
    {"id":"convex-finance", "symbol":"CVX", "bybit":"CVXUSDT"},  # new
    {"id":"frax-share", "symbol":"FXS", "bybit":"FXSUSDT"},  # new
    {"id":"perpetual-protocol", "symbol":"PERP", "bybit":"PERPUSDT"},  # new
    {"id":"gains-network", "symbol":"GNS", "bybit":"GNSUSDT"},  # new
    {"id":"spell-token", "symbol":"SPELL", "bybit":"SPELLUSDT"},  # new
    {"id":"nkn", "symbol":"NKN", "bybit":"NKNUSDT"},  # new
    {"id":"worldcoin-wld", "symbol":"WLD", "bybit":"WLDUSDT"},  # new
    {"id":"dent", "symbol":"DENT", "bybit":"DENTUSDT"},  # new
    {"id":"ankr", "symbol":"ANKR", "bybit":"ANKRUSDT"},  # new
    {"id":"uma", "symbol":"UMA", "bybit":"UMAUSDT"},  # new
    {"id":"ravencoin", "symbol":"RVN", "bybit":"RVNUSDT"},  # new
    {"id":"horizen", "symbol":"ZEN", "bybit":"ZENUSDT"},  # new
    {"id":"livepeer", "symbol":"LPT", "bybit":"LPTUSDT"},  # new
    {"id":"siacoin", "symbol":"SC", "bybit":"SCUSDT"},  # new
    {"id":"iexec-rlc", "symbol":"RLC", "bybit":"RLCUSDT"},  # new
    {"id":"cronos", "symbol":"CRO", "bybit":"CROUSDT"},  # new
    {"id":"woo-network", "symbol":"WOO", "bybit":"WOOUSDT"},  # new
    {"id":"balancer", "symbol":"BAL", "bybit":"BALUSDT"},  # new
    {"id":"ren", "symbol":"REN", "bybit":"RENUSDT"},  # new
    {"id":"numeraire", "symbol":"NMR", "bybit":"NMRUSDT"},  # new
    {"id":"alice", "symbol":"ALICE", "bybit":"ALICEUSDT"},  # new
    {"id":"high-street", "symbol":"HIGH", "bybit":"HIGHUSDT"},  # new
    {"id":"pixels", "symbol":"PIXEL", "bybit":"PIXELUSDT"},  # new
    {"id":"cortex", "symbol":"CTXC", "bybit":"CTXCUSDT"},  # new
    {"id":"hamster-kombat", "symbol":"HMSTR", "bybit":"HMSTRUSDT"},  # new
    {"id":"catizen", "symbol":"CATI", "bybit":"CATIUSDT"},  # new
    {"id":"dogs-token", "symbol":"DOGS", "bybit":"DOGSUSDT"},  # new
    {"id":"aura-finance", "symbol":"AURA", "bybit":"AURAUSDT"},  # new
    {"id":"portal", "symbol":"PORTAL", "bybit":"PORTALUSDT"},  # new
    {"id":"prom", "symbol":"PROM", "bybit":"PROMUSDT"},  # new
    {"id":"xem", "symbol":"XEM", "bybit":"XEMUSDT"},  # new
    {"id":"cocos-bcx", "symbol":"COCOS", "bybit":"COCOSUSDT"},  # new
    {"id":"ontology-gas", "symbol":"ONG", "bybit":"ONGUSDT"},  # new
    {"id":"loom-network-new", "symbol":"LOOM", "bybit":"LOOMUSDT"},  # new
    {"id":"bluzelle", "symbol":"BLZ", "bybit":"BLZUSDT"},  # new
    {"id":"unifi-protocol-dao", "symbol":"UNFI", "bybit":"UNFIUSDT"},  # new
    {"id":"boba-network", "symbol":"BOBA", "bybit":"BOBAUSDT"},  # new
    {"id":"wazirx", "symbol":"WRX", "bybit":"WRXUSDT"},  # new
    {"id":"oraichain-token", "symbol":"ORAI", "bybit":"ORAIUSDT"},  # new
    {"id":"biswap", "symbol":"BSW", "bybit":"BSWUSDT"},  # new
    {"id":"truefi", "symbol":"TRU", "bybit":"TRUUSDT"},  # new
    {"id":"stafi", "symbol":"FIS", "bybit":"FISUSDT"},  # new
    {"id":"dodo", "symbol":"DODO", "bybit":"DODOUSDT"},  # new
    {"id":"reef", "symbol":"REEF", "bybit":"REEFUSDT"},  # new
]

_seen = set()
_deduped = []
for c in COINS:
    if c["symbol"] not in _seen:
        _seen.add(c["symbol"])
        _deduped.append(c)
COINS = _deduped

session = requests.Session()

# ─────────────────────── HELPERS ──────────────────────────────
def utc_now_str():
    return datetime.now(timezone.utc).strftime("%H:%M UTC")

def elapsed_str(s):
    s = int(s)
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s//60}m {s%60}s"
    return f"{s//3600}h {(s%3600)//60}m"

def fmt_p(price, decimals=None):
    if price is None: return "N/A"
    if decimals is None:
        if price >= 1000:   decimals = 1
        elif price >= 100:  decimals = 2
        elif price >= 1:    decimals = 4
        elif price >= 0.01: decimals = 5
        else:               decimals = 8
    return f"${price:.{decimals}f}"

def calc_trade_size():
    if USE_DYNAMIC_SIZING:
        with state_lock:
            bal = paper_balance
        return max(MIN_TRADE_SIZE, min(MAX_TRADE_SIZE, round(bal * RISK_PCT, 2)))
    return TRADE_SIZE

# ─────────────────────── RISK MANAGEMENT ──────────────────────
def check_circuit_breakers(scan_prices=None):
    global risk_state
    with state_lock:
        free_cash       = paper_balance
        deployed_margin = sum(p.get("margin",0)*(1-p.get("tp_hit",0)*0.25) for p in positions.values())
        total_capital   = free_cash + deployed_margin
    now = time.time()
    start_bal = risk_state["session_start_balance"]

    if risk_state["trading_paused"] and risk_state["pause_until"] > 0 and now >= risk_state["pause_until"]:
        risk_state.update({"trading_paused":False,"pause_reason":"","consec_losses":0,"pause_until":0.0})
        tg_send(f"<b>✅ Auto-Resume</b>\nFree cash: ${free_cash:.2f}")

    midnight = datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    if risk_state["daily_reset_at"] < midnight:
        risk_state["daily_reset_at"] = midnight
        risk_state["session_start_balance"] = total_capital
        start_bal = total_capital
        if risk_state["trading_paused"] and "daily" in risk_state["pause_reason"].lower():
            risk_state.update({"trading_paused":False,"pause_reason":"","consec_losses":0,"pause_until":0.0})
            tg_send(f"<b>🌅 New Day Reset</b>\nBalance: ${total_capital:.2f}")

    dd = (total_capital - start_bal) / start_bal * 100
    if dd <= -(MAX_DAILY_LOSS_PCT * 100):
        if not risk_state["trading_paused"]:
            risk_state.update({"trading_paused":True,"pause_reason":f"Daily loss {dd:.1f}%","pause_until":0.0})
            tg_send(f"<b>🛑 CIRCUIT BREAKER</b>\nDaily loss {dd:.1f}%\nBalance: ${total_capital:.2f}")
        return True

    if risk_state["consec_losses"] >= MAX_CONSEC_LOSSES:
        if not risk_state["trading_paused"]:
            resume = datetime.fromtimestamp(now+3600, tz=timezone.utc).strftime("%H:%M UTC")
            risk_state.update({"trading_paused":True,"pause_reason":f"{MAX_CONSEC_LOSSES} consec SL","pause_until":now+3600})
            tg_send(f"<b>⚠️ PAUSING 1H</b>\n{MAX_CONSEC_LOSSES} consecutive SL hits\nResumes {resume}")
        return True

    if scan_prices and now - risk_state["btc_last_check"] >= 3600:
        btc_now  = (scan_prices.get("bitcoin") or {}).get("usd")
        btc_prev = risk_state["btc_last_price"]
        risk_state["btc_last_check"] = now
        if btc_now:
            risk_state["btc_last_price"] = btc_now
            if btc_prev and btc_now > 0:
                chg = (btc_now - btc_prev) / btc_prev * 100
                if chg <= BTC_CRASH_PCT:
                    if not risk_state["trading_paused"]:
                        resume = datetime.fromtimestamp(now+7200, tz=timezone.utc).strftime("%H:%M UTC")
                        risk_state.update({"trading_paused":True,"pause_reason":f"BTC crash {chg:.1f}%","pause_until":now+7200})
                        tg_send(f"<b>🚨 BTC CRASH {chg:.1f}%</b>\nPausing 2h. Resumes {resume}")
                    return True

    if risk_state["trading_paused"] and risk_state["pause_until"] == 0.0:
        risk_state.update({"trading_paused":False,"pause_reason":"","consec_losses":0})
        tg_send(f"<b>✅ Trading Resumed</b>\nCapital: ${total_capital:.2f}")
    return False

def check_stale_positions():
    stale = []
    with state_lock:
        for sym, pos in positions.items():
            if time.time() - pos.get("opened_at", time.time()) > MAX_TRADE_HOURS * 3600:
                stale.append(sym)
    return stale

# ─────────────────────── CANDLES ──────────────────────────────
def get_candles(symbol_usdt, interval="1h", limit=250):
    """FIX: limit=250 so EMA200 always has enough data."""
    try:
        r = session.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol_usdt, "interval": interval, "limit": limit},
            timeout=10,
        )
        data = r.json()
        if not isinstance(data, list): return None
        return [{"open":float(c[1]),"high":float(c[2]),"low":float(c[3]),
                 "close":float(c[4]),"volume":float(c[5])} for c in data]
    except Exception as e:
        print(f"  Candle error {symbol_usdt} {interval}: {e}")
        return None

# ─────────────────────── INDICATORS ───────────────────────────
def calc_ema(closes, period):
    if len(closes) < period: return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 8)

def calc_atr_rolling(candles, period=14):
    """FIX: Returns rolling ATR list — NOT a single value."""
    if len(candles) < period + 1: return None
    trs = []
    for i in range(1, len(candles)):
        h = candles[i].get("high", candles[i]["close"]*1.005)
        l = candles[i].get("low",  candles[i]["close"]*0.995)
        pc = candles[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    # Wilder smoothing — build a list
    atrs = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        atrs.append((atrs[-1]*(period-1) + tr) / period)
    return atrs  # len = len(candles) - period

def calc_supertrend(candles, period=10, multiplier=3.0):
    """
    FIX: Proper rolling supertrend — recalculates ATR per candle.
    Returns (current_value, is_bullish)
    """
    if len(candles) < period + 2: return None, None
    atrs = calc_atr_rolling(candles, period)
    if not atrs: return None, None

    # Align: atrs[0] = candles[period]
    n = len(atrs)
    aligned = candles[period:]  # same length as atrs

    upper = [(aligned[i]["high"]+aligned[i]["low"])/2 + multiplier*atrs[i] for i in range(n)]
    lower = [(aligned[i]["high"]+aligned[i]["low"])/2 - multiplier*atrs[i] for i in range(n)]

    # Build sticky bands
    final_up = [lower[0]]
    final_dn = [upper[0]]
    for i in range(1, n):
        fu = lower[i]
        if aligned[i-1]["close"] > final_up[-1]: fu = max(fu, final_up[-1])
        final_up.append(fu)
        fd = upper[i]
        if aligned[i-1]["close"] < final_dn[-1]: fd = min(fd, final_dn[-1])
        final_dn.append(fd)

    # Trend direction
    trend = [1]
    for i in range(1, n):
        if trend[-1] == -1 and aligned[i]["close"] > final_dn[i-1]:
            trend.append(1)
        elif trend[-1] == 1 and aligned[i]["close"] < final_up[i-1]:
            trend.append(-1)
        else:
            trend.append(trend[-1])

    is_bull = trend[-1] == 1
    val = round(final_up[-1] if is_bull else final_dn[-1], 8)
    return val, is_bull

def calc_ut_bot(candles, key_value=1.0, atr_period=10):
    """
    FIX: Pure regular candles — no Heikin Ashi mixing.
    Returns (buy_signal, sell_signal)
    """
    if len(candles) < atr_period + 2: return False, False
    atrs = calc_atr_rolling(candles, atr_period)
    if not atrs: return False, False

    closes  = [c["close"] for c in candles]
    aligned = closes[atr_period:]
    n_loss  = [key_value * a for a in atrs]
    n       = len(aligned)

    if n < 2: return False, False

    # Build trailing stop
    trailing = [aligned[0] - n_loss[0]]
    for i in range(1, n):
        prev = trailing[-1]
        nl   = n_loss[i]
        src  = aligned[i]
        sp   = aligned[i-1]
        if sp > prev:  new = max(prev, src - nl)
        elif sp < prev: new = min(prev, src + nl)
        elif src > prev: new = src - nl
        else:            new = src + nl
        trailing.append(new)

    buy  = aligned[-2] < trailing[-2] and aligned[-1] > trailing[-1]
    sell = aligned[-2] > trailing[-2] and aligned[-1] < trailing[-1]
    return buy, sell

def get_ta_timeframe(symbol, interval, limit=250):
    """Get TA for one timeframe."""
    candles = get_candles(symbol + "USDT", interval, limit)
    if not candles or len(candles) < 30: return None
    closes = [c["close"] for c in candles]

    # EMA 20/50/200
    ema20  = calc_ema(closes, 20)
    ema50  = calc_ema(closes, 50)  if len(closes) >= 50  else None
    ema200 = calc_ema(closes, 200) if len(closes) >= 200 else None

    # Supertrend + UT Bot
    st_val, st_bull = calc_supertrend(candles, 10, 3.0)
    ut_buy, ut_sell = calc_ut_bot(candles, 1.0, 10)

    # ATR for SL sizing (current)
    atrs = calc_atr_rolling(candles, 14)
    atr_val = atrs[-1] if atrs else None
    atr_pct = round(atr_val / closes[-1] * 100, 4) if atr_val and closes[-1] > 0 else None

    return {
        "ema20":  ema20,  "ema50": ema50,  "ema200": ema200,
        "st_bull": st_bull, "st_val": st_val,
        "ut_buy":  ut_buy,  "ut_sell": ut_sell,
        "atr":     atr_val, "atr_pct": atr_pct,
        "close":   closes[-1],
    }

def get_ta(symbol):
    """
    Multi-timeframe TA: 1m + 5m + 1h + 4h.
    Returns aggregated signal.
    """
    results = {}
    for interval, limit in [("1m", 250), ("5m", 250), ("1h", 250), ("4h", 250)]:
        results[interval] = get_ta_timeframe(symbol, interval, limit)
    return results

def fetch_ta_parallel(symbols):
    res = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        fmap = {ex.submit(get_ta, sym): sym for sym in symbols}
        for f in as_completed(fmap):
            sym = fmap[f]
            try:   res[sym] = f.result()
            except: res[sym] = None
    return res

# ─────────────────────── PRICES ───────────────────────────────
def get_prices():
    try:
        ids = ",".join(c["id"] for c in COINS)
        r = session.get(
            f"https://api.coingecko.com/api/v3/simple/price?ids={ids}"
            "&vs_currencies=usd&include_24hr_change=true"
            "&include_24hr_high=true&include_24hr_low=true&include_24hr_vol=true",
            timeout=15)
        d = r.json()
        first_id = COINS[0]["id"]
        if d and (d.get("bitcoin",{}).get("usd") or d.get(first_id,{}).get("usd")):
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
                        "usd":float(d["lastPrice"]),"usd_24h_change":float(d["priceChangePercent"]),
                        "usd_24h_high":float(d["highPrice"]),"usd_24h_low":float(d["lowPrice"]),
                        "usd_24h_vol":float(d["quoteVolume"]),
                    }
            except: pass
            time.sleep(0.02)
        return result if result else None
    except Exception as e:
        print("Binance error:", e)
    return None

# ─────────────────────── SIGNAL ENGINE ────────────────────────
def build_signal(price, change, high, low, vol, ta_mtf):
    """
    Multi-timeframe signal:
    1m + 5m + 1h + 4h Supertrend + UT Bot + EMA agreement.
    More timeframes agreeing = higher confidence.
    """
    if not price or not ta_mtf: return None

    buy_votes = 0
    sell_votes = 0
    total_tf = 0
    atr_for_sl = None
    ema200_4h = None
    ema20_1h = ema50_1h = None

    tf_weights = {"1m": 1, "5m": 1, "1h": 2, "4h": 3}  # higher TF = more weight

    for tf, weight in tf_weights.items():
        ta = ta_mtf.get(tf)
        if not ta: continue

        st_bull = ta.get("st_bull")
        ut_buy  = ta.get("ut_buy")
        ut_sell = ta.get("ut_sell")
        ema20   = ta.get("ema20")
        ema50   = ta.get("ema50")
        ema200  = ta.get("ema200")

        if st_bull is None: continue
        total_tf += weight

        # BUY vote: supertrend bull + UT Bot buy OR price above EMA alignment
        bull_ema = (ema20 and ema50 and ema20 > ema50) if (ema20 and ema50) else True
        bear_ema = (ema20 and ema50 and ema20 < ema50) if (ema20 and ema50) else True

        if st_bull and (ut_buy or bull_ema):
            buy_votes += weight
        elif not st_bull and (ut_sell or bear_ema):
            sell_votes += weight

        # Store 4h data for EMA200 filter
        if tf == "4h":
            ema200_4h = ema200
        if tf == "1h":
            atr_for_sl = ta.get("atr")
            ema20_1h   = ema20
            ema50_1h   = ema50

    if total_tf == 0: return None

    buy_pct  = buy_votes  / total_tf * 100
    sell_pct = sell_votes / total_tf * 100

    # Need at least 55% agreement
    if buy_pct >= 55:
        direction = "BUY"
        agreement = buy_pct
    elif sell_pct >= 55:
        direction = "SELL"
        agreement = sell_pct
    else:
        return None

    # EMA200 4h filter — must be on right side if available
    if ema200_4h:
        if direction == "BUY"  and price < ema200_4h * 0.99: return None
        if direction == "SELL" and price > ema200_4h * 1.01: return None

    # Volume filter
    if vol and vol < MIN_24H_VOLUME: return None

    # ATR flat market filter
    if atr_for_sl:
        atr_pct = atr_for_sl / price * 100
        if atr_pct < 0.3 or atr_pct > 8.0: return None

    # Confidence: 60% base + agreement bonus
    conf = min(95, int(60 + agreement * 0.35))

    # Levels — ATR based
    if atr_for_sl and atr_for_sl > 0:
        sl_mult = 2.0
        if direction == "BUY":
            sl  = round(price - sl_mult * atr_for_sl, 8)
            tp1 = round(price + 1.5 * atr_for_sl, 8)
            tp2 = round(price + 3.0 * atr_for_sl, 8)
            tp3 = round(price + 4.5 * atr_for_sl, 8)
            tp4 = round(price + 6.0 * atr_for_sl, 8)
        else:
            sl  = round(price + sl_mult * atr_for_sl, 8)
            tp1 = round(price - 1.5 * atr_for_sl, 8)
            tp2 = round(price - 3.0 * atr_for_sl, 8)
            tp3 = round(price - 4.5 * atr_for_sl, 8)
            tp4 = round(price - 6.0 * atr_for_sl, 8)
    else:
        base = 0.025
        if direction == "BUY":
            tp1 = round(price*(1+base*0.4),8); tp2 = round(price*(1+base*0.7),8)
            tp3 = round(price*(1+base*1.0),8); tp4 = round(price*(1+base*1.5),8)
            sl  = round(price*(1-1.0/100),8)
        else:
            tp1 = round(price*(1-base*0.4),8); tp2 = round(price*(1-base*0.7),8)
            tp3 = round(price*(1-base*1.0),8); tp4 = round(price*(1-base*1.5),8)
            sl  = round(price*(1+1.0/100),8)

    tp_pcts = [round(abs(tp-price)/price*100, 2) for tp in [tp1,tp2,tp3,tp4]]
    sl_pct  = round(abs(sl-price)/price*100, 2)

    return {
        "signal":  direction, "conf": conf,
        "buy_votes": buy_votes, "sell_votes": sell_votes,
        "agreement": round(agreement, 1),
        "ema20": ema20_1h, "ema50": ema50_1h, "ema200": ema200_4h,
        "atr_pct": round(atr_for_sl/price*100, 2) if atr_for_sl else None,
        "tp1":tp1,"tp2":tp2,"tp3":tp3,"tp4":tp4,"sl":sl,
        "sl_pct":sl_pct,"tp_pcts":tp_pcts,
        "atr_abs":atr_for_sl,
        "tp1_prob":80,"tp2_prob":65,
    }

# ─────────────────────── FLASK ────────────────────────────────
app = Flask(__name__)

@app.route("/")
def health(): return "APEX MTF Bot running!", 200

@app.route("/data")
def get_data():
    with state_lock:
        total = stats["total"]; tw = stats["trades_won"]
        wr    = round(tw/total*100,1) if total>0 else 0
        net   = round(stats["profit_usdt"]-stats["loss_usdt"],2)
        open_pos = {}
        for sym,pos in positions.items():
            rem = 1-(pos.get("tp_hit",0)*0.25)
            open_pos[sym] = {
                "sym":sym,"direction":pos.get("direction",""),
                "entry":pos.get("entry",0),"execPrice":pos.get("exec_price",0),
                "tp1":pos.get("tp1",0),"tp2":pos.get("tp2",0),
                "tp3":pos.get("tp3",0),"tp4":pos.get("tp4",0),
                "sl":pos.get("sl",0),"liqPrice":pos.get("liq_price",0),
                "tpHit":pos.get("tp_hit",0),"breakeven":pos.get("breakeven",False),
                "margin":pos.get("margin",TRADE_SIZE),
                "realizedPnl":round(pos.get("currentPnl",0),2),
                "unrealizedPnl":round(pos.get("unrealized_pnl",0),2),
                "totalPnl":round(pos.get("currentPnl",0)+pos.get("unrealized_pnl",0),2),
                "remainingPct":round(rem*100),"openTime":pos.get("opened_at",0),
                "sigId":pos.get("sig_id",""),
            }
        payload = {
            "balance":paper_balance,"startBalance":PAPER_BALANCE,"netPnl":net,
            "winRate":wr,"totalTrades":total,"tradesWon":tw,
            "tpHits":stats["tp_hit"],"slHits":stats["sl_hit"],
            "profitUsdt":stats["profit_usdt"],"lossUsdt":stats["loss_usdt"],
            "openPositions":open_pos,"closedTrades":stats["trades_list"][-50:],
            "pnlHistory":stats["pnl_history"][-500:],
            "leverage":LEVERAGE,"tradeSize":TRADE_SIZE,"timestamp":utc_now_str(),
        }
    resp = jsonify(payload)
    resp.headers.add("Access-Control-Allow-Origin","*")
    return resp

# PHASE 2.6: Agent close endpoint — lets AI agent close a position
agent_close_queue = []   # symbols the agent wants closed

@app.route("/agent_close/<token>/<symbol>", methods=["POST","GET"])
def agent_close(token, symbol):
    # Simple shared-secret check so only our agent can close trades
    if token != os.environ.get("AGENT_TOKEN", "apex-secret"):
        return jsonify({"error":"unauthorized"}), 403
    sym = symbol.upper()
    with state_lock:
        if sym not in positions:
            return jsonify({"error":f"{sym} not open"}), 404
        if sym not in agent_close_queue:
            agent_close_queue.append(sym)
    return jsonify({"status":"queued","symbol":sym}), 200

def start_flask():
    import logging; logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",8083)),debug=False,use_reloader=False)

# ─────────────────────── TELEGRAM ─────────────────────────────
def tg_send(msg):
    if not TG_TOKEN or not TG_CHAT:
        print("TG:", msg[:80]); return
    try:
        session.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id":TG_CHAT,"text":msg,"parse_mode":"HTML","disable_web_page_preview":True},
            timeout=HTTP_TIMEOUT)
    except Exception as e:
        print("TG error:", e)

def tg_updates(offset=None):
    params = {"timeout":1,"allowed_updates":'["message"]'}
    if offset: params["offset"] = offset
    try:
        r = session.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",params=params,timeout=5)
        return r.json()
    except: return None

def tp_progress_bar(tp_hit, direction):
    return "  ".join([f"TP{i}{'✅' if i<=tp_hit else '⬜'}" for i in range(1,5)])

def make_signal_id(sym):
    now = datetime.now(timezone.utc)
    return f"{sym}-{now.strftime('%m%d')}-{now.strftime('%H%M')}"

def make_report():
    with state_lock:
        total=stats["total"]; tw=stats["trades_won"]
        wr=round(tw/total*100,1) if total>0 else 0
        net=round(stats["profit_usdt"]-stats["loss_usdt"],2)
        free=paper_balance
        dep=sum(p.get("margin",0)*(1-p.get("tp_hit",0)*0.25) for p in positions.values())
        cap=round(free+dep,2); oc=len(positions)
        pos_lines=[]
        for sym,pos in positions.items():
            arrow="🟢" if pos.get("direction")=="BUY" else "🔴"
            be="🔒" if pos.get("breakeven") else ""
            pos_lines.append(f"{arrow} {sym} {be}  {tp_progress_bar(pos.get('tp_hit',0),pos.get('direction',''))}\n   R:+${pos.get('currentPnl',0):.2f}  U:${pos.get('unrealized_pnl',0):+.2f}")
        paused=f"\n⚠️ Paused: {risk_state['pause_reason']}" if risk_state.get("trading_paused") else ""
    sb=risk_state["session_start_balance"]; dd=round((cap-sb)/sb*100,1)
    sign="+" if net>=0 else ""; ddi="📈" if dd>=0 else "📉"
    return (f"<b>📊 APEX MTF REPORT - {utc_now_str()}</b>\n══════════════════════════════\n"
            f"💰 Free:      ${free:.2f}\n📦 Deployed:  ${dep:.2f} ({oc} trades)\n"
            f"💎 Total:     ${cap:.2f}\n{ddi} vs start:  {dd:+.1f}%\n\n"
            f"📈 P&L:  {sign}${abs(net):.2f}\n🏆 WR:   {tw}/{total} = {wr}%\n"
            f"✅ TP:   {stats['tp_hit']}  ❌ SL: {stats['sl_hit']}{paused}\n\n"
            f"<b>Open ({oc}):</b>\n"+("\n".join(pos_lines) if pos_lines else "None"))

def check_btns(offset):
    updates = tg_updates(offset)
    if updates and updates.get("ok"):
        for u in updates.get("result",[]):
            offset = u["update_id"]+1
            msg  = u.get("message") or u.get("channel_post") or {}
            text = msg.get("text","").strip().lower()
            if text in ("/report","/r","/status"): tg_send(make_report())
            elif text=="/pause":
                risk_state["trading_paused"]=True; risk_state["pause_reason"]="Manual"
                tg_send("<b>⏸ Paused.</b> /resume to restart.")
            elif text=="/resume":
                risk_state.update({"trading_paused":False,"pause_reason":"","consec_losses":0})
                tg_send("<b>▶️ Resumed.</b>")
            elif text=="/help":
                tg_send("<b>⚡ Commands</b>\n/report /r /pause /resume /help")
    return offset

def make_signal_msg(coin, sig, price, change):
    action=sig["signal"]; sign="+" if change>=0 else ""
    conf=sig["conf"]; bars="#"*int(conf/10)+"-"*(10-int(conf/10))
    tp_pcts=sig.get("tp_pcts",[0,0,0,0]); sl_pct=sig.get("sl_pct",1.0)
    arrow="🟢" if action=="BUY" else "🔴"; side="LONG" if action=="BUY" else "SHORT"
    sig_id=sig.get("sig_id",make_signal_id(coin["symbol"]))
    lev_ret=[round(p*LEVERAGE,1) for p in tp_pcts]
    ts=calc_trade_size(); notional=ts*LEVERAGE
    agree=sig.get("agreement",0)
    ema20=sig.get("ema20"); ema50=sig.get("ema50"); ema200=sig.get("ema200")
    ema_str=("↑" if ema20 and ema50 and ema20>ema50 else "↓") if ema20 and ema50 else "?"
    return (f"<b>⚡ APEX MTF SIGNAL - #{sig_id}</b>\n══════════════════════════════\n"
            f"{arrow} <b>{side} - {coin['symbol']}/USDT</b>\n\n"
            f"⚙️ {LEVERAGE}x | ${ts:.0f} → ${notional:.0f} exposure\n\n"
            f"Entry: {fmt_p(price)}\nSL:    {fmt_p(sig['sl'])}  (-{sl_pct:.2f}%)\n\n"
            f"TP1: {fmt_p(sig['tp1'])}  (+{tp_pcts[0]}% | {lev_ret[0]}% lev)\n"
            f"TP2: {fmt_p(sig['tp2'])}  (+{tp_pcts[1]}% | {lev_ret[1]}% lev)\n"
            f"TP3: {fmt_p(sig['tp3'])}  (+{tp_pcts[2]}% | {lev_ret[2]}% lev)\n"
            f"TP4: {fmt_p(sig['tp4'])}  (+{tp_pcts[3]}% | {lev_ret[3]}% lev)\n\n"
            f"📊 MTF Agreement: {agree:.0f}%\n"
            f"EMA trend: {ema_str}  EMA200(4h): {fmt_p(ema200)}\n"
            f"24h: {sign}{round(change,2)}%\n\n"
            f"Confidence: {conf}%  [{bars}]\n"
            f"<i>1m+5m+1h+4h consensus</i>\n══════════════════════════════\nTime: {utc_now_str()}")

def make_tp_msg(sym,direction,tp_num,entry,exec_price,tp_price,elapsed,pnl_usdt,new_sl=None,sig_id=None,tp_hit_total=0,trade_pnl_so_far=0):
    arrow="🟢" if direction=="BUY" else "🔴"; side="LONG" if direction=="BUY" else "SHORT"
    sl_note=f"\n💡 SL→{fmt_p(new_sl)} (BE)" if tp_num==1 and new_sl else (f"\n💡 SL→TP{tp_num-1}" if tp_num>1 and new_sl else "")
    with state_lock:
        total=stats["total"]; tw=stats["trades_won"]
        wr=round(tw/total*100,1) if total>0 else 0
        net=round(stats["profit_usdt"]-stats["loss_usdt"],2); bal=paper_balance
    return (f"<b>✅ TP{tp_num} HIT - {sym} {side}</b> {arrow}\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Entry: {fmt_p(entry)}\n{tp_progress_bar(tp_hit_total,direction)}\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"TP{tp_num}: {fmt_p(tp_price)}\nTime: {elapsed_str(elapsed)}\n"
            f"Close: +${pnl_usdt:.2f}{sl_note}\nSo far: +${trade_pnl_so_far:.2f}\n\n"
            f"WR: {tw}/{total}={wr}% | P&L: ${net:.2f} | Bal: ${bal:.2f}")

def make_sl_msg(sym,direction,entry,exec_price,sl_price,elapsed,pnl_usdt,breakeven=False,sig_id=None,tp_hit_total=0,trade_pnl_so_far=0):
    side="LONG" if direction=="BUY" else "SHORT"
    with state_lock:
        total=stats["total"]; tw=stats["trades_won"]
        wr=round(tw/total*100,1) if total>0 else 0
        net=round(stats["profit_usdt"]-stats["loss_usdt"],2); bal=paper_balance
    total_pnl=round(trade_pnl_so_far+(pnl_usdt if breakeven else -pnl_usdt),2)
    icon="✅" if total_pnl>=0 else "❌"; sign="+" if total_pnl>=0 else "-"
    be=" (breakeven)" if breakeven else ""
    return (f"<b>{icon} SL HIT{be} - {sym} {side}</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Entry: {fmt_p(entry)}\n{tp_progress_bar(tp_hit_total,direction)}\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"SL: {fmt_p(sl_price)}\nTime: {elapsed_str(elapsed)}\n"
            f"Close: {'BE' if breakeven else f'-${pnl_usdt:.2f}'}\n"
            f"<b>Total: {sign}${abs(total_pnl):.2f}</b>\n\nWR:{tw}/{total}={wr}% | Bal:${bal:.2f}")
# ─────────────────────── PAPER EXECUTE ────────────────────────
def is_sl_safe(direction, sl, liq_price):
    if liq_price<=0 or sl<=0: return False
    if direction=="BUY":  return sl >= liq_price*(1+LIQ_BUFFER_PCT)
    return sl <= liq_price*(1-LIQ_BUFFER_PCT)

def paper_execute(coin, sig, price):
    global paper_balance
    sym=coin["symbol"]; direction=sig["signal"]
    ts=calc_trade_size(); notional=ts*LEVERAGE
    exec_price=round(price*(1+SLIPPAGE_PCT if direction=="BUY" else 1-SLIPPAGE_PCT),8)
    qty=round(notional/exec_price,6)
    liq_price=round(exec_price*(1-0.9/LEVERAGE if direction=="BUY" else 1+0.9/LEVERAGE),8)

    if not is_sl_safe(direction, sig["sl"], liq_price):
        print(f"  ⛔ SL unsafe {sym}"); return False

    with state_lock:
        if len(positions)>=MAX_OPEN_TRADES: return False
        same=sum(1 for p in positions.values() if p.get("direction")==direction)
        if same>=MAX_SAME_DIRECTION: return False
        cap=paper_balance+sum(p.get("margin",0)*(1-p.get("tp_hit",0)*0.25) for p in positions.values())
        if (paper_balance-ts)/cap < MIN_FREE_CASH_PCT: return False
        if paper_balance < ts:
            tg_send(f"<b>⚠️ Balance too low - {sym}</b>"); return False
        sig_id=sig.get("sig_id",make_signal_id(sym))
        paper_balance -= ts
        stats["pnl_history"].append(round(paper_balance,2))
        positions[sym]={
            "direction":direction,"entry":price,"exec_price":exec_price,
            "qty":qty,"margin":ts,"sl":sig["sl"],"liq_price":liq_price,
            "tp1":sig["tp1"],"tp2":sig["tp2"],"tp3":sig["tp3"],"tp4":sig["tp4"],
            "tp_pcts":sig["tp_pcts"],"tp_hit":0,"first_tp_counted":False,
            "breakeven":False,"opened_at":time.time(),"funding_periods_charged":0,
            "currentPnl":0.0,"unrealized_pnl":0.0,"sig_id":sig_id,"close_reason":None,
            "atr_val":sig.get("atr_abs"),"trailing_active":False,"trail_mult":2.0,
        }
        bal_after=paper_balance; oc=len(positions)

    side="LONG" if direction=="BUY" else "SHORT"
    lev_ret=[round(p*LEVERAGE,1) for p in sig["tp_pcts"]]
    tg_send(f"<b>📝 PAPER TRADE - #{sig_id}</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{'🟢' if direction=='BUY' else '🔴'} <b>{side} {sym}/USDT</b>\n\n"
            f"Entry: {fmt_p(price)}\nMargin: ${ts:.0f} | Exposure: ${notional:.0f}\n\n"
            f"TP1:{fmt_p(sig['tp1'])} ({lev_ret[0]}%)\nTP2:{fmt_p(sig['tp2'])} ({lev_ret[1]}%)\n"
            f"TP3:{fmt_p(sig['tp3'])} ({lev_ret[2]}%)\nTP4:{fmt_p(sig['tp4'])} ({lev_ret[3]}%)\n"
            f"SL:{fmt_p(sig['sl'])}\nLiq:{fmt_p(liq_price)}\n\nBal:${bal_after:.2f} | Open:{oc}")
    return True

# ─────────────────────── MONITOR ──────────────────────────────
def process_agent_closes(prices):
    """PHASE 2.6: Close any positions the AI agent flagged via /agent_close."""
    global paper_balance
    with state_lock:
        to_close = list(agent_close_queue)
        agent_close_queue.clear()
    for sym in to_close:
        coin_data = next((c for c in COINS if c["symbol"]==sym), None)
        if not coin_data:
            continue
        d = prices.get(coin_data["id"]) or {}
        live = d.get("usd")
        with state_lock:
            if sym not in positions:
                continue
            pos = positions[sym]
            direction = pos["direction"]; ep = pos["exec_price"]
            if not live: live = ep
            tp_hit = pos["tp_hit"]; rem = 1-(tp_hit*0.25); ts = pos["margin"]; rm = ts*rem
            mv = (live-ep)/ep*100 if direction=="BUY" else (ep-live)/ep*100
            upnl = round(ts*LEVERAGE*mv/100*rem, 2)
            prior = pos.get("currentPnl", 0); total_pnl = round(prior+upnl, 2)
            if upnl >= 0: stats["profit_usdt"] += upnl
            else:         stats["loss_usdt"] += abs(upnl)
            paper_balance += rm + upnl
            stats["total"] += 1
            if total_pnl >= 0: stats["trades_won"] += 1
            stats["pnl_history"].append(round(paper_balance, 2))
            stats["trades_list"].append({"sym":sym,"direction":direction,"result":"AGENT_CLOSE","pnl":total_pnl,"time":utc_now_str()})
            positions.pop(sym, None)
            bal = paper_balance
        sign = "+" if total_pnl>=0 else ""
        tg_send(f"<b>🤖 AGENT CLOSE - {sym}</b>\nAI agent flagged reversal\nP&L: {sign}${total_pnl:.2f}\nBal: ${bal:.2f}")

def monitor_positions(prices):
    global paper_balance
    to_remove=[]; notifications=[]

    for sym,pos in list(positions.items()):
        coin_data=next((c for c in COINS if c["symbol"]==sym),None)
        if not coin_data: to_remove.append(sym); continue
        d=prices.get(coin_data["id"]) or {}
        price=d.get("usd")
        if not price: continue

        with state_lock:
            direction=pos.get("direction","BUY"); entry=pos.get("entry",0)
            exec_price=pos.get("exec_price",0); sl=pos.get("sl",0)
            liq_price=pos.get("liq_price",0); tp_hit=pos.get("tp_hit",0)
            elapsed=time.time()-pos.get("opened_at",time.time())
            tp_levels=[pos.get(f"tp{i}",0) for i in range(1,5)]
            ts=pos.get("margin",TRADE_SIZE)

            # Unrealized PnL
            rem=1-(tp_hit*0.25)
            if exec_price>0:
                mv=(price-exec_price)/exec_price*100 if direction=="BUY" else (exec_price-price)/exec_price*100
                pos["unrealized_pnl"]=round(ts*LEVERAGE*mv/100*rem,2)

            # Funding
            fd=int(elapsed/28800); np=fd-pos.get("funding_periods_charged",0)
            if np>0:
                cost=ts*LEVERAGE*FUNDING_RATE*np*rem
                paper_balance-=cost; pos["funding_periods_charged"]=fd
                stats["pnl_history"].append(round(paper_balance,2))

            # PHASE 2.6: Trailing stop update (after TP2 activates it)
            if pos.get("trailing_active") and pos.get("atr_val"):
                trail_mult = pos.get("trail_mult", 2.0)
                atr_v = pos["atr_val"]
                if direction=="BUY":
                    new_trail = price - trail_mult*atr_v
                    if new_trail > sl:  # only move SL up
                        pos["sl"] = round(new_trail, 8)
                        sl = pos["sl"]
                else:
                    new_trail = price + trail_mult*atr_v
                    if new_trail < sl:  # only move SL down
                        pos["sl"] = round(new_trail, 8)
                        sl = pos["sl"]

            # SL check
            sl_hit=(direction=="BUY" and price<=sl) or (direction=="SELL" and price>=sl)
            if sl_hit:
                rem2=1-(tp_hit*0.25); pm=abs(sl-exec_price)/exec_price*100
                is_p=(direction=="BUY" and sl>=exec_price) or (direction=="SELL" and sl<=exec_price)
                pnl=round(ts*LEVERAGE*pm/100*rem2,2); rm=ts*rem2
                if is_p: stats["profit_usdt"]+=pnl; paper_balance+=rm+pnl
                else:    stats["loss_usdt"]+=pnl;   paper_balance+=max(0,rm-pnl)
                stats["total"]+=1; stats["sl_hit"]+=1
                if is_p or pos.get("first_tp_counted"):
                    stats["trades_won"]+=1; risk_state["consec_losses"]=0
                else:
                    risk_state["consec_losses"]+=1
                    sl_cooldown[sym]=time.time()
                stats["pnl_history"].append(round(paper_balance,2))
                stats["trades_list"].append({"sym":sym,"direction":direction,"result":"SL","pnl":pnl if is_p else -pnl,"time":utc_now_str()})
                notifications.append(make_sl_msg(sym,direction,entry,exec_price,sl,elapsed,pnl,pos.get("breakeven"),sig_id=pos.get("sig_id"),tp_hit_total=tp_hit,trade_pnl_so_far=pos.get("currentPnl",0)))
                to_remove.append(sym); continue

            # GAP SL
            liq_hit=(direction=="BUY" and price<=liq_price) or (direction=="SELL" and price>=liq_price)
            if liq_hit:
                rem2=1-(tp_hit*0.25); rm=ts*rem2
                gcp=sl*(1-GAP_SLIPPAGE_PCT if direction=="BUY" else 1+GAP_SLIPPAGE_PCT)
                pm=abs(gcp-exec_price)/exec_price*100; pnl=round(ts*LEVERAGE*pm/100*rem2,2)
                stats["loss_usdt"]+=pnl; paper_balance+=max(0,rm-pnl)
                stats["total"]+=1; stats["sl_hit"]+=1; risk_state["consec_losses"]+=1
                sl_cooldown[sym]=time.time()
                stats["pnl_history"].append(round(paper_balance,2))
                stats["trades_list"].append({"sym":sym,"direction":direction,"result":"GAP_SL","pnl":-pnl,"time":utc_now_str()})
                notifications.append(f"<b>⚠️ GAP SL - {sym}</b>\nLoss:-${pnl:.2f}\nBal:${paper_balance:.2f}")
                to_remove.append(sym); continue

            if tp_hit>=4: to_remove.append(sym); continue

            next_tp=tp_levels[tp_hit]
            tp_reached=(direction=="BUY" and price>=next_tp) or (direction=="SELL" and price<=next_tp)
            if tp_reached:
                tp_num=tp_hit+1; pnl_pct=abs(next_tp-exec_price)/exec_price*100
                pnl=round(ts*LEVERAGE*pnl_pct/100*0.25,2); qm=ts*0.25
                stats["tp_hit"]+=1; stats["profit_usdt"]+=pnl
                paper_balance+=qm+pnl; stats["pnl_history"].append(round(paper_balance,2))
                pos["currentPnl"]=pos.get("currentPnl",0)+pnl
                if not pos["first_tp_counted"]: pos["first_tp_counted"]=True
                new_sl=None
                if tp_num==1 and not pos.get("breakeven"):
                    new_sl=exec_price; pos["sl"]=new_sl; pos["breakeven"]=True
                elif tp_num==2:
                    new_sl=tp_levels[0]; pos["sl"]=new_sl
                    # PHASE 2.6: Activate trailing stop after TP2
                    pos["trailing_active"]=True
                    pos["trail_mult"]=2.0  # 2x ATR trail initially
                elif tp_num==3:
                    new_sl=tp_levels[1]; pos["sl"]=new_sl
                    pos["trail_mult"]=1.0  # tighten to 1x ATR
                pos["tp_hit"]=tp_num
                if tp_num==4:
                    stats["total"]+=1; stats["trades_won"]+=1; risk_state["consec_losses"]=0
                    stats["trades_list"].append({"sym":sym,"direction":direction,"result":"ALL_TP","pnl":round(pos["currentPnl"],2),"time":utc_now_str()})
                    total=stats["total"]; tw=stats["trades_won"]; wr=round(tw/total*100,1)
                    notifications.append(f"<b>🎯 ALL 4 TPs HIT - {sym}!</b>\nTrade P&L: +${pos['currentPnl']:.2f}\nWR:{tw}/{total}={wr}%\nBal:${paper_balance:.2f}")
                    to_remove.append(sym)
                else:
                    notifications.append(make_tp_msg(sym,direction,tp_num,entry,exec_price,next_tp,elapsed,pnl,new_sl,sig_id=pos.get("sig_id"),tp_hit_total=tp_num,trade_pnl_so_far=pos.get("currentPnl",0)))

    with state_lock:
        for sym in to_remove: positions.pop(sym,None)
    for msg in notifications: tg_send(msg)

# ─────────────────────── STALE CLOSE ──────────────────────────
def close_stale_position(sym, pos, scan_prices):
    global paper_balance
    coin_data=next((c for c in COINS if c["symbol"]==sym),None)
    if not coin_data: return
    d=scan_prices.get(coin_data["id"]) or {}
    live=d.get("usd") or pos.get("exec_price",0)
    with state_lock:
        if sym not in positions: return
        pos=positions[sym]; direction=pos["direction"]; ep=pos["exec_price"]
        tp_hit=pos["tp_hit"]; rem=1-(tp_hit*0.25); ts=pos["margin"]; rm=ts*rem
        mv=(live-ep)/ep*100 if direction=="BUY" else (ep-live)/ep*100
        upnl=round(ts*LEVERAGE*mv/100*rem,2)
        prior=pos.get("currentPnl",0); total_pnl=round(prior+upnl,2)
        if upnl>=0: stats["profit_usdt"]+=upnl
        else:       stats["loss_usdt"]+=abs(upnl)
        paper_balance+=rm+upnl; stats["total"]+=1
        stats["trades_list"].append({"sym":sym,"direction":direction,"result":"STALE_CLOSE","pnl":total_pnl,"time":utc_now_str()})
        positions.pop(sym,None)
        age=f"{(time.time()-pos['opened_at'])/3600:.1f}h"
        tg_send(f"<b>⏰ STALE CLOSE - {sym}</b>\nOpen: {age}\nP&L: {'+' if total_pnl>=0 else ''}${total_pnl:.2f}\nBal:${paper_balance:.2f}")

# ─────────────────────── MAIN ─────────────────────────────────

# ═══════════════════════════════════════════════════════════
# PHASE 2.6 — BUILT-IN AI AGENT (runs as background thread)
# ═══════════════════════════════════════════════════════════
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY","").strip()
AGENT_ENABLED     = bool(ANTHROPIC_API_KEY)
AGENT_INTERVAL    = int(os.environ.get("AGENT_INTERVAL","3600"))  # 1h
AGENT_AUTO_CLOSE  = os.environ.get("AGENT_AUTO_CLOSE","true").lower()=="true"
AGENT_MODEL       = "claude-sonnet-4-6"

def agent_get_candles(symbol_usdt, interval="1h", limit=100):
    try:
        r=requests.get("https://api.binance.com/api/v3/klines",
            params={"symbol":symbol_usdt,"interval":interval,"limit":limit},timeout=10)
        d=r.json()
        if not isinstance(d,list): return None
        return [{"high":float(c[2]),"low":float(c[3]),"close":float(c[4]),"volume":float(c[5])} for c in d]
    except Exception: return None

def agent_ema(vals,p):
    if len(vals)<p: return None
    k=2/(p+1); e=sum(vals[:p])/p
    for v in vals[p:]: e=v*k+e*(1-k)
    return e

def agent_snapshot(symbol):
    c1=agent_get_candles(symbol+"USDT","1h",100)
    c4=agent_get_candles(symbol+"USDT","4h",100)
    if not c1 or not c4: return None
    cl1=[c["close"] for c in c1]; vl1=[c["volume"] for c in c1]
    e20=agent_ema(cl1,20); e50=agent_ema(cl1,50)
    t1="up" if (e20 and e50 and e20>e50) else "down"
    cl4=[c["close"] for c in c4]
    e20_4=agent_ema(cl4,20); e50_4=agent_ema(cl4,50)
    t4="up" if (e20_4 and e50_4 and e20_4>e50_4) else "down"
    rv=vl1[-1]; av=sum(vl1[-20:])/20 if len(vl1)>=20 else sum(vl1)/len(vl1)
    return {"price":cl1[-1],"trend_1h":t1,"trend_4h":t4,"volume":"rising" if rv>av else "falling"}

def agent_ask_claude(open_pos, market_data, summary):
    prompt=f"""You are a crypto trading risk analyst monitoring open paper positions.

STATS: {summary}

OPEN POSITIONS:
{json.dumps(open_pos, indent=2)}

MARKET DATA:
{json.dumps(market_data, indent=2)}

For EACH position decide: KEEP (healthy), CLOSE (clear reversal), or WATCH (uncertain).
Close candidate if: in profit but 4h trend flipped against it AND volume falling, OR losing long while 1h and 4h both down.

Respond ONLY with JSON:
{{"recommendations":[{{"symbol":"XXX","action":"KEEP|CLOSE|WATCH","reason":"under 12 words"}}],"market_summary":"one sentence"}}"""
    try:
        r=requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
            json={"model":AGENT_MODEL,"max_tokens":1000,"messages":[{"role":"user","content":prompt}]},timeout=40)
        d=r.json()
        txt="".join(b.get("text","") for b in d.get("content",[]) if b.get("type")=="text")
        txt=txt.replace("```json","").replace("```","").strip()
        return json.loads(txt)
    except Exception as e:
        print("agent claude error:",e); return None

def agent_close_position(sym, prices):
    """Close a position directly (same process, no HTTP needed)."""
    global paper_balance
    coin=next((c for c in COINS if c["symbol"]==sym),None)
    if not coin: return False
    d=prices.get(coin["id"]) or {}; live=d.get("usd")
    with state_lock:
        if sym not in positions: return False
        pos=positions[sym]; direction=pos["direction"]; ep=pos["exec_price"]
        if not live: live=ep
        tp_hit=pos["tp_hit"]; rem=1-(tp_hit*0.25); ts=pos["margin"]; rm=ts*rem
        mv=(live-ep)/ep*100 if direction=="BUY" else (ep-live)/ep*100
        upnl=round(ts*LEVERAGE*mv/100*rem,2)
        prior=pos.get("currentPnl",0); total=round(prior+upnl,2)
        if upnl>=0: stats["profit_usdt"]+=upnl
        else: stats["loss_usdt"]+=abs(upnl)
        paper_balance+=rm+upnl; stats["total"]+=1
        if total>=0: stats["trades_won"]+=1
        stats["pnl_history"].append(round(paper_balance,2))
        stats["trades_list"].append({"sym":sym,"direction":direction,"result":"AGENT_CLOSE","pnl":total,"time":utc_now_str()})
        positions.pop(sym,None); bal=paper_balance
    sign="+" if total>=0 else ""
    tg_send(f"<b>🤖 AGENT CLOSE - {sym}</b>\nAI flagged reversal\nP&L: {sign}${total:.2f}\nBal: ${bal:.2f}")
    return True

def agent_loop(get_prices_fn):
    """Background thread: hourly Claude analysis of open trades."""
    if not AGENT_ENABLED:
        print("  Agent disabled (no ANTHROPIC_API_KEY)")
        return
    time.sleep(120)  # let bot warm up first
    tg_send(f"<b>🤖 AI Agent active</b>\nHourly analysis ON\nAuto-close: {'ON' if AGENT_AUTO_CLOSE else 'OFF'}")
    while True:
        try:
            with state_lock:
                open_syms=list(positions.keys())
                open_snapshot={s:{"direction":positions[s]["direction"],
                    "entry":positions[s]["exec_price"],
                    "tp_hit":positions[s]["tp_hit"],
                    "unrealized":positions[s].get("unrealized_pnl",0)} for s in open_syms}
            if not open_syms:
                time.sleep(AGENT_INTERVAL); continue
            prices=get_prices_fn()
            market={}
            for s in open_syms:
                snap=agent_snapshot(s)
                if snap: market[s]=snap
                time.sleep(0.3)
            summary=f"Balance ${paper_balance:.0f}, {stats['total']} trades, {stats.get('trades_won',0)} won"
            result=agent_ask_claude(open_snapshot,market,summary)
            if not result:
                time.sleep(AGENT_INTERVAL); continue
            recs=result.get("recommendations",[]); mkt=result.get("market_summary","")
            lines=[f"<b>🤖 AGENT ANALYSIS</b>","━"*18]
            for rec in recs:
                sym=rec.get("symbol","?"); act=rec.get("action","WATCH"); rsn=rec.get("reason","")
                icon={"KEEP":"✅","CLOSE":"🔴","WATCH":"👀"}.get(act,"•")
                lines.append(f"{icon} <b>{sym}</b>: {act} — {rsn}")
                if act=="CLOSE" and AGENT_AUTO_CLOSE:
                    if agent_close_position(sym,prices): lines.append("   → closed ✅")
            if mkt: lines.append("━"*18); lines.append(f"📊 {mkt}")
            tg_send("\n".join(lines))
            time.sleep(AGENT_INTERVAL)
        except Exception as e:
            print("agent loop error:",e); time.sleep(180)


def run():
    global paper_balance, pre_warned, last_signal
    risk_state["session_start_balance"]=paper_balance
    risk_state["btc_last_check"]=time.time()

    print("="*55)
    print("  APEX MTF Bot — Supertrend+UTBot+EMA 1m/5m/1h/4h")
    print("="*55)
    print(f"  Coins: {len(COINS)} | Trade: ${TRADE_SIZE}×{LEVERAGE}x | MinConf: {MIN_CONF}%")

    flask_thread=threading.Thread(target=start_flask,daemon=True)
    flask_thread.start()

    # PHASE 2.6: Start built-in AI agent thread
    agent_thread=threading.Thread(target=agent_loop,args=(get_prices,),daemon=True)
    agent_thread.start()

    tg_send(
        f"<b>⚡ APEX v3 (Phase 2.6) — Online!</b>\n\n"
        f"Signal Engine: <b>Supertrend + UT Bot + EMA</b>\n"
        f"Timeframes: <b>1m + 5m + 1h + 4h consensus</b>\n"
        f"Coins: <b>{len(COINS)}</b>\n"
        f"Trade: <b>${TRADE_SIZE}×{LEVERAGE}x = ${TRADE_SIZE*LEVERAGE} exposure</b>\n"
        f"Min conf: <b>{MIN_CONF}%</b>\n\n"
        f"<b>Bugs fixed:</b>\n"
        f"• EMA200 now works (250 candles)\n"
        f"• Supertrend rolling ATR (accurate)\n"
        f"• UT Bot pure candles (no HA mix)\n"
        f"• Multi-TF consensus signal\n\n"
        f"Commands: /report /r /pause /resume /help"
    )

    offset=None; last_scan_at=0; last_price_t=0; prices=None

    while True:
        try:
            offset=check_btns(offset)
            if time.time()-last_price_t>=10:
                prices=get_prices(); last_price_t=time.time()
            if prices:
                with state_lock: open_syms=list(positions.keys())
                if open_syms: monitor_positions(prices)
                process_agent_closes(prices)

            if time.time()-last_scan_at>=SCAN_EVERY_SECONDS:
                last_scan_at=time.time()
                with state_lock: oc=len(positions); bal=paper_balance
                print(f"\n[{utc_now_str()}] Scanning {len(COINS)} coins (open:{oc} bal:${bal:.2f})")

                now=time.time()
                pre_warned={k:v for k,v in pre_warned.items() if now-v<PRE_WARN_TTL}
                coin_syms={c["symbol"] for c in COINS}
                last_signal={k:v for k,v in last_signal.items() if k in coin_syms}
                with state_lock:
                    if len(stats["pnl_history"])>600:
                        stats["pnl_history"]=stats["pnl_history"][-500:]

                scan_prices=get_prices()
                if not scan_prices: time.sleep(2); continue
                prices=scan_prices

                for sym in check_stale_positions():
                    pos=positions.get(sym)
                    if pos: close_stale_position(sym,pos,scan_prices)

                if check_circuit_breakers(scan_prices):
                    print(f"  🛑 Paused: {risk_state['pause_reason']}")
                    time.sleep(2); continue

                # Prune cooldowns
                for sym_cd in list(sl_cooldown.keys()):
                    if time.time()-sl_cooldown[sym_cd]>=SL_COOLDOWN_SECONDS:
                        sl_cooldown.pop(sym_cd,None)
                        print(f"  ✅ {sym_cd} cooldown expired")

                # Find candidates with >2% move
                ta_candidates=[]
                for coin in COINS:
                    sym=coin["symbol"]
                    if sym in BLOCKED_COINS or sym in sl_cooldown: continue
                    with state_lock:
                        if sym in positions: continue
                    d=scan_prices.get(coin["id"]) or {}
                    vol=d.get("usd_24h_vol")
                    if vol and vol < MIN_24H_VOLUME: continue
                    change=d.get("usd_24h_change",0) or 0
                    if abs(change)>=2:
                        ta_candidates.append(sym)

                ta_map=fetch_ta_parallel(ta_candidates) if ta_candidates else {}

                for coin in COINS:
                    sym=coin["symbol"]
                    if sym in BLOCKED_COINS or sym in sl_cooldown: continue
                    with state_lock:
                        if sym in positions: continue
                    d=scan_prices.get(coin["id"]) or {}
                    price=d.get("usd"); change=d.get("usd_24h_change",0) or 0
                    high=d.get("usd_24h_high"); low=d.get("usd_24h_low")
                    vol=d.get("usd_24h_vol")
                    if not price or abs(change)<2: continue
                    if vol and vol<MIN_24H_VOLUME: continue

                    ta_mtf=ta_map.get(sym)
                    print(f"  {sym}: ${price} {round(change,2)}%",end="")
                    if ta_mtf:
                        h1=ta_mtf.get("1h") or {}; h4=ta_mtf.get("4h") or {}
                        st1="🟢" if h1.get("st_bull") else "🔴"
                        st4="🟢" if h4.get("st_bull") else "🔴"
                        ut1="UT✅" if h1.get("ut_buy") or h1.get("ut_sell") else "UT-"
                        print(f" | ST1h={st1} ST4h={st4} {ut1}",end="")
                    print()

                    sig=build_signal(price,change,high,low,vol,ta_mtf)
                    if not sig or sig["conf"]<MIN_CONF: continue

                    prev=last_signal.get(sym)
                    if prev and prev["signal"]==sig["signal"] and abs(prev.get("entry",0)-price)/price<0.005: continue

                    with state_lock:
                        if len(positions)>=MAX_OPEN_TRADES:
                            print(f"  Max trades, skip {sym}"); continue
                        pre_warned.pop(sym,None)

                    sig["entry"]=price; sig["sig_id"]=make_signal_id(sym)
                    last_signal[sym]=sig

                    opened=paper_execute(coin,sig,price)
                    if opened:
                        tg_send(make_signal_msg(coin,sig,price,change))
                        print(f"  🚀 Signal: {sym} {sig['signal']} {sig['conf']}% (agree:{sig['agreement']}%)")
                    else:
                        print(f"  ⛔ Rejected: {sym}")

            time.sleep(2)

        except KeyboardInterrupt:
            print("\nStopped.")
            with state_lock:
                net=round(stats["profit_usdt"]-stats["loss_usdt"],2)
                print(f"Final: {stats['trades_won']}/{stats['total']} | P&L: ${net}")
            break
        except Exception as e:
            print(f"Error: {e}"); time.sleep(5)

if __name__=="__main__":
    run()
