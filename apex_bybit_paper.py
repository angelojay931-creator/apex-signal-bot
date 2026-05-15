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

TRADE_SIZE         = 100.0     # $100 margin - Phase 1 safe sizing
USE_DYNAMIC_SIZING = False     # Fixed $100 always
RISK_PCT           = 0.05      # Unused - dynamic sizing off
MIN_TRADE_SIZE     = 100.0
MAX_TRADE_SIZE     = 100.0

LEVERAGE           = 5         # 5x → $500 exposure per trade ($100 × 5)
MIN_CONF           = 85        # Phase 2: back to 85 — proven best on 7-indicator version
SCAN_EVERY_SECONDS = 30
HTTP_TIMEOUT       = 15
MAX_OPEN_TRADES    = 8         # Phase 2: 8 trades for faster data collection
MAX_SAME_DIRECTION  = 4        # 4 per side → allows 8 total (4 longs + 4 shorts)

SLIPPAGE_PCT       = 0.001
FUNDING_RATE       = 0.0001
PRE_WARN_TTL       = 7200
GAP_SLIPPAGE_PCT   = 0.005    # Gap fill slippage when price skips SL
LIQ_BUFFER_PCT     = 0.02     # SL must be at least 2% before liq price
MIN_24H_VOLUME     = 300_000_000  # Skip coins with <$300M 24h volume

# ── Phase 2 additions ──
SL_COOLDOWN_SECONDS = 7200    # 2h cooldown after any SL hit per coin

BLOCKED_COINS = {
    # Liquidated us - extreme gap risk
    "WAVES", "HNT", "EOS", "ALPACA",
    # Extreme volatility / low liquidity
    "WRX", "REEF", "LOKA", "AUCTION", "NULS", "ALPHA",
    "CLV", "SXP", "FIS", "MDT", "DODO", "BLZ",
    # Low quality - from data analysis
    "APT", "CHZ", "MANA", "SUI", "WOO", "SAND",
    # Legacy block
    "ENJ",
    # Phase 2 additions — proven losers from 593-trade analysis
    "TRU", "CORE", "WLD", "CRV", "FLOW",
}

# ─────────────── PROFESSIONAL RISK MANAGEMENT ─────────────────
# Circuit breakers - protect capital when things go wrong
MAX_DAILY_LOSS_PCT  = 0.06   # 6% daily loss limit — tighter than 8%, allows 6 trades
MAX_CONSEC_LOSSES   = 3      # Pause after 3 consecutive SL hits
MAX_TRADE_HOURS     = 48     # Force-close positions open > 48 hours
BTC_CRASH_PCT       = -5.0   # Pause new signals if BTC drops >5% in 1h
MIN_FREE_CASH_PCT   = 0.30   # 30% free cash floor — allows 8 × $100 on $2,000

# ─────────────────────────── STATE ────────────────────────────
# RLock: reentrant - make_tp_msg/make_sl_msg re-acquire inside monitor_positions
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
sl_cooldown = {}   # {sym: timestamp} — 2h block after SL hit

# ───────────────── RISK MANAGEMENT STATE ──────────────────────
risk_state = {
    "session_start_balance": PAPER_BALANCE,  # reset on bot start
    "consec_losses":         0,              # consecutive SL hits
    "trading_paused":        False,          # circuit breaker flag
    "pause_reason":          "",             # why trading is paused
    "btc_last_price":        None,           # for BTC crash detection
    "btc_last_check":        0.0,            # timestamp of last BTC check
    "pause_until":           0.0,            # auto-resume timestamp (0 = manual only)
    "daily_reset_at":        0.0,            # timestamp of last daily loss reset
}

# ─────────────────────────── COINS ────────────────────────────
# Top 200 coins by market cap - stablecoins and wrapped tokens excluded
COINS = [
    # ════════════════════════════════════════════════
    # PHASE 2 WHITELIST — 30 proven coins only
    # Selected from 593-trade Phase 1 analysis
    # Every coin here achieved 55%+ WR in real data
    # ════════════════════════════════════════════════

    # TOP TIER — 80%+ WR proven
    {"id": "dydx-chain",              "symbol": "DYDX",   "bybit": "DYDXUSDT"},   # 82% WR 40 trades ⭐
    {"id": "starknet",                "symbol": "STRK",   "bybit": "STRKUSDT"},   # 88% WR 24 trades
    {"id": "pendle",                  "symbol": "PENDLE", "bybit": "PENDLEUSDT"}, # 87% WR  9 trades
    {"id": "immutable-x",             "symbol": "IMX",    "bybit": "IMXUSDT"},    # 100% WR 7 trades
    {"id": "near",                    "symbol": "NEAR",   "bybit": "NEARUSDT"},   # 100% WR 7 trades
    {"id": "pyth-network",            "symbol": "PYTH",   "bybit": "PYTHUSDT"},   # 100% WR 6 trades
    {"id": "thorchain",               "symbol": "RUNE",   "bybit": "RUNEUSDT"},   # 91% WR  5 trades
    {"id": "dogwifcoin",              "symbol": "WIF",    "bybit": "WIFUSDT"},    # 89% WR  5 trades
    {"id": "kava",                    "symbol": "KAVA",   "bybit": "KAVAUSDT"},   # 86% WR  5 trades
    {"id": "eos",                     "symbol": "EOS",    "bybit": "EOSUSDT"},    # 93% WR 10 trades

    # SOLID TIER — 65-80% WR proven
    {"id": "blur",                    "symbol": "BLUR",   "bybit": "BLURUSDT"},   # 75% WR 28 trades
    {"id": "zcash",                   "symbol": "ZEC",    "bybit": "ZECUSDT"},    # 70% WR 29 trades
    {"id": "dogecoin",                "symbol": "DOGE",   "bybit": "DOGEUSDT"},   # 75% WR 22 trades
    {"id": "optimism",                "symbol": "OP",     "bybit": "OPUSDT"},     # 80% WR  8 trades
    {"id": "injective-protocol",      "symbol": "INJ",    "bybit": "INJUSDT"},    # 70% WR  9 trades
    {"id": "solana",                  "symbol": "SOL",    "bybit": "SOLUSDT"},    # 67% WR 15 trades
    {"id": "ontology",                "symbol": "ONT",    "bybit": "ONTUSDT"},    # 78% WR  8 trades
    {"id": "conflux-token",           "symbol": "CFX",    "bybit": "CFXUSDT"},    # 77% WR  8 trades
    {"id": "io-net",                  "symbol": "IO",     "bybit": "IOUSDT"},     # 70% WR  6 trades
    {"id": "polkadot",                "symbol": "DOT",    "bybit": "DOTUSDT"},    # 75% WR  6 trades

    # ACCEPTABLE TIER — 55-65% WR proven
    {"id": "arbitrum",                "symbol": "ARB",    "bybit": "ARBUSDT"},    # 63% WR  7 trades
    {"id": "gmx",                     "symbol": "GMX",    "bybit": "GMXUSDT"},    # 63% WR  5 trades
    {"id": "pepe",                    "symbol": "PEPE",   "bybit": "PEPEUSDT"},   # 62% WR  9 trades
    {"id": "dash",                    "symbol": "DASH",   "bybit": "DASHUSDT"},   # 62% WR 10 trades
    {"id": "ondo-finance",            "symbol": "ONDO",   "bybit": "ONDOUSDT"},   # 60% WR  7 trades
    {"id": "lido-dao",                "symbol": "LDO",    "bybit": "LDOUSDT"},    # 58% WR  9 trades
    {"id": "aave",                    "symbol": "AAVE",   "bybit": "AAVEUSDT"},   # 57% WR  8 trades
    {"id": "ripple",                  "symbol": "XRP",    "bybit": "XRPUSDT"},    # 57% WR  7 trades
    {"id": "filecoin",                "symbol": "FIL",    "bybit": "FILUSDT"},    # 57% WR  7 trades
    {"id": "sushi",                   "symbol": "SUSHI",  "bybit": "SUSHIUSDT"},  # 56% WR  8 trades
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
    """Return margin for this trade - dynamic or fixed."""
    if USE_DYNAMIC_SIZING:
        with state_lock:
            bal = paper_balance
        sized = round(bal * RISK_PCT, 2)
        return max(MIN_TRADE_SIZE, min(MAX_TRADE_SIZE, sized))
    return TRADE_SIZE


# ─────────────── PROFESSIONAL RISK MANAGEMENT ─────────────────
def check_circuit_breakers(scan_prices=None):
    """
    Professional circuit breakers - pause trading when:
    1. Daily drawdown > MAX_DAILY_LOSS_PCT (8%) - resets at midnight UTC
    2. Consecutive SL losses >= MAX_CONSEC_LOSSES (3) - auto-resumes after 1h
    3. BTC drops > BTC_CRASH_PCT (-5%) in last hour - auto-resumes after 2h
    Returns True if trading should be paused.
    """
    global risk_state

    with state_lock:
        free_cash       = paper_balance
        deployed_margin = sum(pos.get("margin", 0) * (1.0 - pos.get("tp_hit", 0) * 0.25)
                              for pos in positions.values())
        total_capital   = free_cash + deployed_margin

    now       = time.time()
    start_bal = risk_state["session_start_balance"]

    # ── Auto-resume check - runs before all triggers ──
    # If a timed pause has expired, clear it regardless of which trigger set it
    if risk_state["trading_paused"] and risk_state["pause_until"] > 0:
        if now >= risk_state["pause_until"]:
            risk_state["trading_paused"] = False
            risk_state["pause_reason"]   = ""
            risk_state["consec_losses"]  = 0
            risk_state["pause_until"]    = 0.0
            tg_send(
                f"<b>✅ Auto-Resume - Trading Restarted</b>\n\n"
                f"Pause period expired.\n"
                f"Free cash: ${free_cash:.2f} USDT\n"
                f"Total capital: ${total_capital:.2f} USDT"
            )

    # ── Daily reset at midnight UTC ──
    # Reset session_start_balance each new UTC day so daily loss tracks correctly
    midnight_today = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    if risk_state["daily_reset_at"] < midnight_today:
        risk_state["daily_reset_at"]        = midnight_today
        risk_state["session_start_balance"] = total_capital
        start_bal                           = total_capital
        # If paused for daily loss, clear it on new day
        if risk_state["trading_paused"] and "daily loss" in risk_state["pause_reason"].lower():
            risk_state["trading_paused"] = False
            risk_state["pause_reason"]   = ""
            risk_state["consec_losses"]  = 0
            risk_state["pause_until"]    = 0.0
            tg_send(
                f"<b>🌅 New Day - Daily Loss Limit Reset</b>\n\n"
                f"Session balance reset to ${total_capital:.2f} USDT\n"
                f"Trading resumed for new UTC day."
            )

    # ── 1. Daily loss limit ──
    drawdown_pct = (total_capital - start_bal) / start_bal * 100
    if drawdown_pct <= -(MAX_DAILY_LOSS_PCT * 100):
        if not risk_state["trading_paused"]:
            reason = f"Daily loss limit hit ({drawdown_pct:.1f}% drawdown)"
            risk_state["trading_paused"] = True
            risk_state["pause_reason"]   = reason
            risk_state["pause_until"]    = 0.0   # manual resume or midnight reset
            tg_send(
                f"<b>🛑 CIRCUIT BREAKER - Trading Paused</b>\n\n"
                f"Reason: {reason}\n"
                f"Free cash: ${free_cash:.2f} USDT\n"
                f"Total capital: ${total_capital:.2f} USDT\n"
                f"Started: ${start_bal:.2f} USDT\n\n"
                f"<i>Will auto-resume at midnight UTC or type /resume.</i>"
            )
        return True

    # ── 2. Consecutive losses - auto-resume after 1 hour ──
    if risk_state["consec_losses"] >= MAX_CONSEC_LOSSES:
        if not risk_state["trading_paused"]:
            reason = f"{MAX_CONSEC_LOSSES} consecutive SL hits"
            risk_state["trading_paused"] = True
            risk_state["pause_reason"]   = reason
            risk_state["pause_until"]    = now + 3600   # 1 hour
            resume_time = datetime.fromtimestamp(now + 3600, tz=timezone.utc).strftime("%H:%M UTC")
            tg_send(
                f"<b>⚠️ CONSECUTIVE LOSS LIMIT - Pausing 1 hour</b>\n\n"
                f"Reason: {reason}\n"
                f"Free cash: ${free_cash:.2f} USDT\n"
                f"Total capital: ${total_capital:.2f} USDT\n\n"
                f"<i>Auto-resuming at {resume_time}.</i>"
            )
        return True

    # ── 3. BTC crash guard - auto-resume after 2 hours ──
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
                        risk_state["pause_until"]    = now + 7200   # 2 hours
                        resume_time = datetime.fromtimestamp(now + 7200, tz=timezone.utc).strftime("%H:%M UTC")
                        tg_send(
                            f"<b>🚨 BTC CRASH DETECTED - Pausing signals</b>\n\n"
                            f"BTC: ${btc_prev:.0f} → ${btc_now:.0f} ({btc_change:.1f}%)\n"
                            f"Free cash: ${free_cash:.2f} USDT\n"
                            f"Total capital: ${total_capital:.2f} USDT\n\n"
                            f"<i>Auto-resuming at {resume_time}.</i>"
                        )
                    return True

    # ── Resume if all conditions clear ──
    if risk_state["trading_paused"] and risk_state["pause_until"] == 0.0:
        risk_state["trading_paused"] = False
        risk_state["pause_reason"]   = ""
        risk_state["consec_losses"]  = 0
        tg_send(
            f"<b>✅ Circuit Breaker Reset - Trading Resumed</b>\n\n"
            f"Total capital: ${total_capital:.2f} USDT"
        )

    return False


def check_stale_positions():
    """
    Force-close positions that have been open > MAX_TRADE_HOURS.
    Professional traders never let a trade run indefinitely.
    Returns list of symbols to close.
    """
    stale = []
    max_seconds = MAX_TRADE_HOURS * 3600
    with state_lock:
        for sym, pos in positions.items():
            age = time.time() - pos.get("opened_at", time.time())
            if age > max_seconds:
                stale.append(sym)
    return stale


# ──────────────────── RSI / EMA / MACD / ATR / BB / OBV ──────────────────────
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


def calc_macd(closes, fast=12, slow=26, signal=9):
    """
    MACD = EMA(12) - EMA(26), Signal = EMA(9) of MACD line.
    FIX: O(n) incremental EMA - no longer recalculates full EMA per candle.
    Returns: (macd_val, signal_val, histogram, is_bullish)
    """
    if len(closes) < slow + signal:
        return None, None, None, None

    # Seed EMAs on first 'slow' candles, then roll forward
    k_fast = 2 / (fast + 1)
    k_slow = 2 / (slow + 1)
    k_sig  = 2 / (signal + 1)

    ema_f = sum(closes[:fast]) / fast
    ema_s = sum(closes[:slow]) / slow

    # Walk forward from slow-1 to build the MACD line
    macd_line = []
    for price in closes[slow:]:
        ema_f = price * k_fast + ema_f * (1 - k_fast)
        ema_s = price * k_slow + ema_s * (1 - k_slow)
        macd_line.append(ema_f - ema_s)

    if len(macd_line) < signal:
        return None, None, None, None

    # Seed signal EMA on first 'signal' MACD values
    sig_ema = sum(macd_line[:signal]) / signal
    for m in macd_line[signal:]:
        sig_ema = m * k_sig + sig_ema * (1 - k_sig)

    macd_val   = round(macd_line[-1], 8)
    signal_val = round(sig_ema, 8)
    histogram  = round(macd_val - signal_val, 8)
    is_bullish = macd_val > signal_val
    return macd_val, signal_val, histogram, is_bullish


def calc_atr(candles, period=14):
    """
    ATR = Average True Range over period candles
    True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    Uses close as proxy since we only have OHLCV from Binance
    Returns ATR value and ATR as % of current price
    """
    if len(candles) < period + 1:
        return None, None
    trs = []
    for i in range(1, len(candles)):
        high  = candles[i].get("high",  candles[i]["close"] * 1.005)
        low   = candles[i].get("low",   candles[i]["close"] * 0.995)
        prev_c = candles[i-1]["close"]
        tr = max(
            high - low,
            abs(high - prev_c),
            abs(low  - prev_c),
        )
        trs.append(tr)
    # Wilder smoothing
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    current_price = candles[-1]["close"]
    atr_pct = round(atr / current_price * 100, 4) if current_price > 0 else None
    return round(atr, 8), atr_pct


def calc_bollinger(closes, period=20, std_dev=2.0):
    """
    Bollinger Bands: middle=SMA(20), upper/lower = ±2 std devs
    Returns: (upper, middle, lower, bandwidth, is_expanding)
    bandwidth = (upper-lower)/middle - wider = more volatile/expanding
    """
    if len(closes) < period:
        return None, None, None, None, None
    window  = closes[-period:]
    middle  = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std     = variance ** 0.5
    upper   = round(middle + std_dev * std, 8)
    lower   = round(middle - std_dev * std, 8)
    middle  = round(middle, 8)
    bandwidth = round((upper - lower) / middle * 100, 4) if middle > 0 else 0
    # Compare to previous bandwidth to detect expansion
    if len(closes) >= period + 5:
        prev_window = closes[-(period+5):-5]
        prev_mid    = sum(prev_window) / period
        prev_var    = sum((x - prev_mid) ** 2 for x in prev_window) / period
        prev_std    = prev_var ** 0.5
        prev_bw     = (prev_std * 2 * std_dev) / prev_mid * 100 if prev_mid > 0 else 0
        is_expanding = bandwidth > prev_bw
    else:
        is_expanding = True
    return upper, middle, lower, bandwidth, is_expanding


def calc_obv(closes, volumes):
    """
    OBV: cumulative volume - add on up days, subtract on down days
    Returns: (current_obv, obv_trend) where trend is 'rising', 'falling', 'flat'
    """
    if len(closes) < 2 or len(volumes) < 2:
        return None, None
    obv = 0.0
    obv_history = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]:
            obv += volumes[i]
        elif closes[i] < closes[i-1]:
            obv -= volumes[i]
        obv_history.append(obv)
    # Trend: compare last 5 OBV values to previous 5
    if len(obv_history) >= 10:
        recent = sum(obv_history[-5:]) / 5
        prev   = sum(obv_history[-10:-5]) / 5
        if recent > prev * 1.01:
            trend = "rising"
        elif recent < prev * 0.99:
            trend = "falling"
        else:
            trend = "flat"
    else:
        trend = "rising" if obv > 0 else "flat"
    return round(obv, 2), trend


def get_candles(symbol_usdt, interval="1h", limit=80):
    """Fetch full OHLCV candles - now includes high/low for ATR."""
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


def get_ta(symbol):
    """
    Full technical analysis - all 7 indicators:
    RSI, EMA20/50, MACD, ATR, Bollinger Bands, OBV
    """
    candles = get_candles(symbol + "USDT", "1h", 80)
    if not candles or len(candles) < 30:
        return None
    closes  = [c["close"]  for c in candles]
    volumes = [c["volume"] for c in candles]

    rsi    = calc_rsi(closes, 14)
    ema20  = calc_ema(closes, 20)
    ema50  = calc_ema(closes, 50) if len(closes) >= 50 else None
    vol_r  = calc_volume_ratio(volumes)

    macd_val, signal_val, histogram, macd_bullish = calc_macd(closes)
    atr_val, atr_pct                              = calc_atr(candles, 14)
    bb_upper, bb_mid, bb_lower, bb_bw, bb_expand = calc_bollinger(closes, 20)
    obv_val, obv_trend                            = calc_obv(closes, volumes)

    # Volume profile decay — last 3 candles vs previous 10
    vol_profile_decay = False
    if len(volumes) >= 13:
        recent_avg = sum(volumes[-3:]) / 3
        prev_avg   = sum(volumes[-13:-3]) / 10
        if recent_avg < prev_avg * 0.95:
            vol_profile_decay = True

    return {
        "rsi":               rsi,
        "ema20":             ema20,
        "ema50":             ema50,
        "vol_ratio":         vol_r,
        "macd":              macd_val,
        "macd_signal":       signal_val,
        "macd_hist":         histogram,
        "macd_bullish":      macd_bullish,
        "atr":               atr_val,
        "atr_pct":           atr_pct,
        "bb_upper":          bb_upper,
        "bb_lower":          bb_lower,
        "bb_bw":             bb_bw,
        "bb_expanding":      bb_expand,
        "obv":               obv_val,
        "obv_trend":         obv_trend,
        "vol_profile_decay": vol_profile_decay,
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
            time.sleep(0.02)   # 0.02s × 151 coins = 3s max fallback
        return result if result else None
    except Exception as e:
        print("Binance fallback error:", e)
    return None


# ──────────────────────── SIGNAL ENGINE ───────────────────────
# ATR minimum threshold - below this = flat/dead market = no trade
ATR_MIN_PCT = 0.3   # 0.3% minimum ATR - skip signals in sleeping markets

# TP probability estimates based on historical hit rates
# These are calibrated estimates - will improve over time
TP_PROB = {
    "tp1": {"high": 91, "med": 85, "low": 78},
    "tp2": {"high": 82, "med": 74, "low": 65},
}


def calc_tp_probability(conf, vol_ratio, macd_bullish, bb_expanding):
    """
    Estimate TP1 and TP2 hit probability based on signal strength.
    More confirmations = higher probability.
    """
    # Base from confidence score
    if conf >= 92:    base = "high"
    elif conf >= 87:  base = "med"
    else:             base = "low"

    tp1_prob = TP_PROB["tp1"][base]
    tp2_prob = TP_PROB["tp2"][base]

    # Bonus for extra confirmations
    bonus = 0
    if vol_ratio and vol_ratio > 2:   bonus += 2
    if macd_bullish is not None:       bonus += 2
    if bb_expanding:                   bonus += 1

    tp1_prob = min(96, tp1_prob + bonus)
    tp2_prob = min(90, tp2_prob + bonus)
    return tp1_prob, tp2_prob


def calc_levels(price, direction, rsi, vol_ratio, atr_pct=None):
    """
    Calculate TP1-4 and SL.
    SL is now ATR-based (dynamic) when ATR data available.
    Fixed % fallback when ATR not available.
    """
    base = 0.025
    if vol_ratio and vol_ratio > 3:       base = 0.042
    elif vol_ratio and vol_ratio > 2:     base = 0.034
    elif rsi is not None and (rsi < 25 or rsi > 75): base = 0.036

    if direction == "BUY":
        tp1 = round(price * (1 + base * 0.40), 8)
        tp2 = round(price * (1 + base * 0.70), 8)
        tp3 = round(price * (1 + base * 1.00), 8)
        tp4 = round(price * (1 + base * 1.50), 8)
        # ATR SL: use 2× ATR if available, else fixed 1.0%
        if atr_pct and atr_pct > 0:
            sl_pct = min(max(atr_pct * 2, 0.4), 1.5)  # clamp 0.4%-1.5%
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

    tp_pcts = [
        round(abs(tp1 - price) / price * 100, 2),
        round(abs(tp2 - price) / price * 100, 2),
        round(abs(tp3 - price) / price * 100, 2),
        round(abs(tp4 - price) / price * 100, 2),
    ]
    sl_pct_actual = round(abs(sl - price) / price * 100, 2)
    return {
        "tp1": tp1, "tp2": tp2, "tp3": tp3, "tp4": tp4,
        "sl": sl, "sl_pct": sl_pct_actual, "tp_pcts": tp_pcts,
    }


def build_signal(price, change, high, low, vol, ta):
    """
    APEX v3 Signal Engine - 7 indicator filters:
    RSI + EMA + Volume + MACD + ATR + Bollinger + OBV
    ALL must confirm before signal fires.
    """
    if not price:
        return None

    high = high or price * 1.02
    low  = low  or price * 0.98
    rng  = high - low
    pos  = (price - low) / rng if rng > 0 else 0.5
    score = 0

    # ── 1. Momentum (24h change) ──
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

    # ── 2. Range position ──
    if pos < 0.15:   score += 4
    elif pos < 0.25: score += 3
    elif pos > 0.90: score -= 3
    elif pos > 0.80: score -= 2

    # ── 3. Volume (24h) ──
    if vol and vol > 2_000_000_000:   score += 3
    elif vol and vol > 1_000_000_000: score += 2
    elif vol and vol > 500_000_000:   score += 1
    else:                              score -= 2

    # Extract all TA values
    rsi          = (ta or {}).get("rsi")
    ema20        = (ta or {}).get("ema20")
    ema50        = (ta or {}).get("ema50")
    vol_ratio    = (ta or {}).get("vol_ratio", 1.0)
    macd_bullish = (ta or {}).get("macd_bullish")
    macd_hist    = (ta or {}).get("macd_hist")
    atr_pct      = (ta or {}).get("atr_pct")
    bb_expanding = (ta or {}).get("bb_expanding")
    bb_bw        = (ta or {}).get("bb_bw")
    obv_trend    = (ta or {}).get("obv_trend")

    # ── 4. ATR flat market filter ──
    # If market is sleeping (ATR too low) → skip entirely
    if atr_pct is not None and atr_pct < ATR_MIN_PCT:
        return None

    # ── 4b. ATR extreme volatility filter (Phase 1) ──
    # Block coins with ATR > 8% - too volatile, SL gaps to liquidation
    # WAVES=26%, HNT=17%, EOS=17% all liquidated us - this stops it
    ATR_MAX_PCT = 8.0
    if atr_pct is not None and atr_pct > ATR_MAX_PCT:
        return None

    # ── 5. RSI hard veto ──
    if score > 0 and rsi is not None and rsi > 75:
        return None
    if score < 0 and rsi is not None and rsi < 25:
        return None

    # ── 6. RSI confirmation ──
    if score > 0 and rsi is not None:
        if rsi < 40:   score += 2
        elif rsi < 50: score += 1
    if score < 0 and rsi is not None:
        if rsi > 60:   score -= 2
        elif rsi > 50: score -= 1

    # ── 7. EMA trend ──
    if ema20 is not None and ema50 is not None:
        if score > 0 and ema20 > ema50: score += 1
        if score < 0 and ema20 < ema50: score -= 1

    # ── 8. Volume spike bonus ──
    if vol_ratio and vol_ratio > 3:
        score = score + 2 if score > 0 else score - 2
    elif vol_ratio and vol_ratio > 2:
        score = score + 1 if score > 0 else score - 1

    # ── 9. MACD confirmation (NEW) ──
    if macd_bullish is not None:
        if score > 0 and macd_bullish:      score += 2  # bullish cross = BUY confirmed
        elif score > 0 and not macd_bullish: score -= 1  # MACD bearish = weaken BUY
        if score < 0 and not macd_bullish:  score -= 2  # bearish cross = SELL confirmed
        elif score < 0 and macd_bullish:    score += 1  # MACD bullish = weaken SELL

    # ── 10. Bollinger Bands expansion (NEW) ──
    if bb_expanding is not None:
        if bb_expanding:
            score = score + 1 if score > 0 else score - 1   # expanding = real move
        else:
            score = score - 1 if score > 0 else score + 1   # contracting = fake move

    # ── 11. OBV trend confirmation (NEW) ──
    if obv_trend:
        if score > 0 and obv_trend == "rising":   score += 1  # real buying
        elif score > 0 and obv_trend == "falling": score -= 2  # fake pump
        if score < 0 and obv_trend == "falling":  score -= 1  # real selling
        elif score < 0 and obv_trend == "rising":  score += 2  # fake dump

    # ── 12. Score → direction + confidence ──
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

    levels   = calc_levels(price, direction, rsi, vol_ratio, atr_pct)
    tp1_prob, tp2_prob = calc_tp_probability(
        conf, vol_ratio, macd_bullish, bb_expanding
    )

    return {
        "signal":       direction,
        "conf":         conf,
        "score":        score,
        # Original indicators
        "rsi":          rsi,
        "ema20":        ema20,
        "ema50":        ema50,
        "vol_ratio":    vol_ratio,
        # New indicators
        "macd_bullish": macd_bullish,
        "macd_hist":    macd_hist,
        "atr_pct":      atr_pct,
        "bb_expanding": bb_expanding,
        "bb_bw":        bb_bw,
        "obv_trend":    obv_trend,
        # Levels
        "tp1":          levels["tp1"],
        "tp2":          levels["tp2"],
        "tp3":          levels["tp3"],
        "tp4":          levels["tp4"],
        "sl":           levels["sl"],
        "sl_pct":       levels["sl_pct"],
        "tp_pcts":      levels["tp_pcts"],
        # Probability
        "tp1_prob":     tp1_prob,
        "tp2_prob":     tp2_prob,
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
            unrealized = pos.get("unrealized_pnl", 0)   # updated live by monitor loop
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

        # Cap pnl_history at 500 points to prevent unbounded memory growth
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
    params = {"timeout": 1, "allowed_updates": '["message"]'}
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


def make_report():
    """Generate a full session report on demand."""
    with state_lock:
        total      = stats["total"]
        trades_won = stats["trades_won"]
        win_rate   = round(trades_won / total * 100, 1) if total > 0 else 0
        net_pnl    = round(stats["profit_usdt"] - stats["loss_usdt"], 2)
        net_sign   = "+" if net_pnl >= 0 else "-"
        free_cash  = paper_balance
        deployed   = sum(pos.get("margin", 0) * (1.0 - pos.get("tp_hit", 0) * 0.25)
                         for pos in positions.values())
        total_cap  = round(free_cash + deployed, 2)
        open_count = len(positions)

        # Build open positions summary
        pos_lines = []
        for sym, pos in positions.items():
            direction  = pos.get("direction", "")
            tp_hit     = pos.get("tp_hit", 0)
            realized   = round(pos.get("currentPnl", 0), 2)
            unrealized = round(pos.get("unrealized_pnl", 0), 2)
            arrow      = "🟢" if direction == "BUY" else "🔴"
            be_flag    = "🔒" if pos.get("breakeven") else ""
            progress   = tp_progress_bar(tp_hit, direction)
            pos_lines.append(
                f"{arrow} {sym} {be_flag}  {progress}\n"
                f"   R:+${realized:.2f}  U:${unrealized:+.2f}"
            )

        paused_str = f"\n⚠️ Paused: {risk_state['pause_reason']}" \
                     if risk_state.get("trading_paused") else ""

    pos_block = "\n".join(pos_lines) if pos_lines else "None"
    start_bal = risk_state["session_start_balance"]
    drawdown  = round((total_cap - start_bal) / start_bal * 100, 1)
    dd_icon   = "📈" if drawdown >= 0 else "📉"

    return (
        f"<b>📊 APEX REPORT - {utc_now_str()}</b>\n"
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
        f"{pos_block}"
    )


def check_btns(offset):
    updates = tg_updates(offset)
    if updates and updates.get("ok"):
        for u in updates.get("result", []):
            offset = u["update_id"] + 1
            # Handle text commands from user
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
                tg_send(
                    "<b>⚡ APEX Commands</b>\n\n"
                    "/report - Full session report\n"
                    "/status - Same as /report\n"
                    "/r      - Quick shortcut\n"
                    "/pause  - Pause new signals\n"
                    "/resume - Resume trading\n"
                    "/help   - This message"
                )
    return offset


# ─────────────────────────── MESSAGES ─────────────────────────
def make_pre_warn(coin, direction, price):
    arrow = "📈" if direction == "BUY" else "📉"
    return (
        f"<b>⚠️ GET READY - {coin['symbol']}/USDT</b>\n\n"
        f"{arrow} Potential <b>{direction}</b> forming\n"
        f"Price: {fmt_p(price)}\n\n"
        "<i>Full signal incoming shortly...</i>"
    )


def make_signal_id(sym):
    """Generate a short unique signal ID e.g. SOL-0417-1423"""
    now = datetime.now(timezone.utc)
    return f"{sym}-{now.strftime('%m%d')}-{now.strftime('%H%M')}"


def tp_progress_bar(tp_hit, direction):
    """Visual TP progress - e.g. TP1✅ TP2✅ TP3⬜ TP4⬜"""
    icons = []
    for i in range(1, 5):
        if i <= tp_hit:
            icons.append(f"TP{i}✅")
        else:
            icons.append(f"TP{i}⬜")
    return "  ".join(icons)


def make_signal_msg(coin, sig, price, change):
    action       = sig["signal"]
    sign         = "+" if change >= 0 else ""
    conf         = sig["conf"]
    bars         = "#" * int(conf / 10) + "-" * (10 - int(conf / 10))
    rsi          = sig.get("rsi")
    ema20        = sig.get("ema20")
    ema50        = sig.get("ema50")
    vol_ratio    = sig.get("vol_ratio", 1.0)
    tp_pcts      = sig.get("tp_pcts", [0, 0, 0, 0])
    macd_bullish = sig.get("macd_bullish")
    atr_pct      = sig.get("atr_pct")
    bb_expanding = sig.get("bb_expanding")
    obv_trend    = sig.get("obv_trend")
    tp1_prob     = sig.get("tp1_prob", 85)
    tp2_prob     = sig.get("tp2_prob", 75)
    sl_pct       = sig.get("sl_pct", 1.0)
    arrow        = "🟢" if action == "BUY" else "🔴"
    side_word    = "LONG" if action == "BUY" else "SHORT"
    sig_id       = sig.get("sig_id", make_signal_id(coin["symbol"]))

    rsi_str  = f"{rsi:.1f}" if rsi is not None else "N/A"
    ema_str  = ("↑ Uptrend" if ema20 > ema50 else "↓ Downtrend") if (ema20 is not None and ema50 is not None) else "N/A"
    vol_str  = f"{vol_ratio:.1f}x avg" if vol_ratio else "N/A"
    macd_str = ("✅ Bullish" if macd_bullish else "⚠️ Bearish") if macd_bullish is not None else "N/A"
    atr_str  = f"{atr_pct:.2f}%" if atr_pct else "N/A"
    bb_str   = ("✅ Expanding" if bb_expanding else "⚠️ Contracting") if bb_expanding is not None else "N/A"
    obv_str  = ("✅ Rising" if obv_trend == "rising" else
                "⚠️ Falling" if obv_trend == "falling" else "➡️ Flat") if obv_trend else "N/A"

    lev_ret      = [round(p * LEVERAGE, 1) for p in tp_pcts]
    trade_size   = calc_trade_size()
    notional     = trade_size * LEVERAGE

    return (
        f"<b>⚡ APEX SIGNAL - #{sig_id}</b>\n"
        f"══════════════════════════════\n"
        f"{arrow} <b>{side_word} - {coin['symbol']}/USDT</b>\n\n"
        f"⚙️ {LEVERAGE}x | ${trade_size:.0f} margin → ${notional:.0f} exposure\n\n"
        f"Entry:     {fmt_p(price)}\n"
        f"SL:        {fmt_p(sig['sl'])}  (-{sl_pct:.2f}% ATR)\n\n"
        f"TP1: {fmt_p(sig['tp1'])}  {tp1_prob}% prob  (+{tp_pcts[0]}% | {lev_ret[0]}% levered)\n"
        f"TP2: {fmt_p(sig['tp2'])}  {tp2_prob}% prob  (+{tp_pcts[1]}% | {lev_ret[1]}% levered)\n"
        f"TP3: {fmt_p(sig['tp3'])}               (+{tp_pcts[2]}% | {lev_ret[2]}% levered)\n"
        f"TP4: {fmt_p(sig['tp4'])}               (+{tp_pcts[3]}% | {lev_ret[3]}% levered)\n\n"
        f"📊 Indicators:\n"
        f"RSI(14):  {rsi_str}\n"
        f"EMA:      {ema_str}\n"
        f"MACD:     {macd_str}\n"
        f"BB:       {bb_str}\n"
        f"OBV:      {obv_str}\n"
        f"ATR:      {atr_str}\n"
        f"Volume:   {vol_str}\n"
        f"24h:      {sign}{round(change, 2)}%\n\n"
        f"Confidence: {conf}%  [{bars}]\n\n"
        f"🤖 <i>Paper trade auto-entered</i>\n"
        f"══════════════════════════════\n"
        f"Time: {utc_now_str()}"
    )


def make_tp_msg(sym, direction, tp_num, entry, exec_price, tp_price, elapsed, pnl_usdt, new_sl=None, sig_id=None, tp_hit_total=0, trade_pnl_so_far=0):
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
        net_sign   = "+" if net_pnl >= 0 else "-"
        balance    = paper_balance

    entry_note  = f" (exec {fmt_p(exec_price)})" if abs(exec_price - entry) / entry > 0.0005 else ""
    progress    = tp_progress_bar(tp_hit_total, direction)
    total_so_far = round(trade_pnl_so_far, 2)

    return (
        f"<b>✅ TP{tp_num} HIT - {sym} {side_word}</b> {arrow}\n"
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
        f"Balance: ${balance:.2f} USDT"
    )


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

    return (
        f"<b>{icon} SL HIT{be_str} - {sym} {side_word}</b>\n"
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
        f"Balance: ${balance:.2f} USDT"
    )


# ──────────────────────── PAPER EXECUTE ───────────────────────
# ─────────────────── PHASE 1 HELPER FUNCTIONS ────────────────
def is_sl_hit(direction, price, sl):
    """Returns True if current price has reached the stop-loss."""
    if direction == "BUY":
        return price <= sl
    return price >= sl


def is_liq_hit(direction, price, liq_price):
    """Returns True if current price has reached the liquidation price."""
    if direction == "BUY":
        return price <= liq_price
    return price >= liq_price


def is_sl_safe(direction, sl, liq_price):
    """
    Returns True if SL is safely before the liquidation price.
    Requires LIQ_BUFFER_PCT (2%) gap between SL and liquidation.
    Prevents trades where SL is so close to liq that any gap causes liquidation.
    """
    if liq_price <= 0 or sl <= 0:
        return False
    if direction == "BUY":
        # SL must be at least LIQ_BUFFER_PCT above liq price
        return sl >= liq_price * (1 + LIQ_BUFFER_PCT)
    else:
        # SL must be at least LIQ_BUFFER_PCT below liq price
        return sl <= liq_price * (1 - LIQ_BUFFER_PCT)


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

    # ── Phase 1: Reject if SL is not safely before liquidation ──
    if not is_sl_safe(direction, sig["sl"], liq_price):
        print(f"  ⛔ SL unsafe - SL={sig['sl']:.6f} Liq={liq_price:.6f} - rejected")
        return False

    # All state mutations under lock; TG calls outside to avoid holding lock during HTTP
    with state_lock:
        # ── Hard cap - safety net in case scan loop check is bypassed ──
        if len(positions) >= MAX_OPEN_TRADES:
            print(f"  ⛔ Max trades reached inside execute: {len(positions)}/{MAX_OPEN_TRADES}")
            return False

        # ── Phase 1: Direction correlation cap ──
        same_dir = sum(1 for p in positions.values() if p.get("direction") == direction)
        if same_dir >= MAX_SAME_DIRECTION:
            print(f"  ⛔ Direction cap: already {same_dir} {direction}s open (max {MAX_SAME_DIRECTION})")
            return False

        # ── Phase 1: Minimum free cash check (40%) ──
        total_cap = paper_balance + sum(
            p.get("margin", 0) * (1 - p.get("tp_hit", 0) * 0.25)
            for p in positions.values())
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
                "close_reason":            None,  # Phase 1: track close reason
            }
            bal_after  = paper_balance
            open_count = len(positions)

    # Send TG outside lock - no HTTP while holding state_lock
    if bal_snap is not None:
        tg_send(
            f"<b>⚠️ Paper balance too low - {sym}</b>\n\n"
            f"Balance: ${bal_snap:.2f} USDT\n"
            f"Required margin: ${trade_size:.0f} USDT\n\nSkipping trade."
        )
        return False

    side_word = "LONG" if direction == "BUY" else "SHORT"
    lev_ret   = [round(p * LEVERAGE, 1) for p in sig["tp_pcts"]]
    slip_note = f"\n⚡ Exec: {fmt_p(exec_price)} (slippage applied)" \
                if abs(exec_price - price) / price > 0.0001 else ""

    tg_send(
        f"<b>📝 PAPER TRADE ENTERED - #{sig_id}</b>\n"
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
    notifications = []   # (msg_str,) - built inside lock, sent outside

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
            # Read all pos fields inside lock - prevents race with /data endpoint
            direction  = pos.get("direction", "BUY")
            entry      = pos.get("entry", 0)
            exec_price = pos.get("exec_price", 0)
            sl         = pos.get("sl", 0)
            liq_price  = pos.get("liq_price", 0)
            tp_hit     = pos.get("tp_hit", 0)
            elapsed    = time.time() - pos.get("opened_at", time.time())
            tp_levels  = [pos.get("tp1",0), pos.get("tp2",0),
                          pos.get("tp3",0), pos.get("tp4",0)]
            trade_size = pos.get("margin", float(TRADE_SIZE))

            # ── Live unrealized PnL - updated every monitor cycle ──
            remaining_pct = 1.0 - (tp_hit * 0.25)
            if exec_price > 0:
                if direction == "BUY":
                    live_move_pct = (price - exec_price) / exec_price * 100
                else:
                    live_move_pct = (exec_price - price) / exec_price * 100
                pos["unrealized_pnl"] = round(
                    trade_size * LEVERAGE * live_move_pct / 100 * remaining_pct, 2
                )

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

            # ── SL check FIRST (always before liquidation per Phase 1) ──
            if is_sl_hit(direction, price, sl):
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
                if is_profit or pos.get("first_tp_counted"):
                    stats["trades_won"]         += 1
                    risk_state["consec_losses"]  = 0
                else:
                    risk_state["consec_losses"] += 1
                    # Phase 2: start cooldown — block this coin for 2h
                    sl_cooldown[sym] = time.time()
                    print(f"  ⏳ {sym} SL cooldown started — blocked 2h")
                stats["pnl_history"].append(round(paper_balance, 2))
                stats["trades_list"].append({
                    "sym": sym, "direction": direction,
                    "result": "SL", "close_reason": "SL",
                    "pnl": pnl_usdt if is_profit else -pnl_usdt,
                    "time": utc_now_str(),
                })
                notifications.append(
                    make_sl_msg(sym, direction, entry, exec_price, sl,
                                elapsed, pnl_usdt, pos.get("breakeven"),
                                sig_id=pos.get("sig_id"), tp_hit_total=tp_hit,
                                trade_pnl_so_far=pos.get("currentPnl", 0))
                )
                to_remove.append(sym)
                continue

            # ── Liquidation check SECOND ──
            # SL was NOT hit above, so if liq is hit the price gapped
            # straight through SL → treat as GAP_SL, fill at SL + GAP_SLIPPAGE_PCT
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
                # Phase 2: start cooldown for GAP_SL as well
                sl_cooldown[sym] = time.time()
                print(f"  ⏳ {sym} GAP_SL cooldown started — blocked 2h")
                stats["pnl_history"].append(round(paper_balance, 2))
                stats["trades_list"].append({
                    "sym": sym, "direction": direction,
                    "result": "GAP_SL", "close_reason": "GAP_SL",
                    "pnl": -pnl_usdt, "time": utc_now_str(),
                })
                notifications.append(
                    f"<b>⚠️ GAP SL - {sym}</b> (price gapped past SL to liq)\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 #{pos.get('sig_id','?')}  |  Entry: {fmt_p(entry)}\n"
                    f"{tp_progress_bar(tp_hit, direction)}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"SL: {fmt_p(sl)}  →  Gap fill: {fmt_p(gap_close_price)}\n"
                    f"Loss: -${pnl_usdt:.2f} USDT (capped at SL, NOT full liq)\n\n"
                    f"Balance: ${paper_balance:.2f} USDT"
                )
                print(f"  ⚠️ GAP SL: {sym} - skipped SL {sl:.5f}, hit liq {liq_price:.5f}")
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

                # Mark first TP hit (used to set breakeven SL)
                if not pos["first_tp_counted"]:
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
                    stats["total"]              += 1
                    stats["trades_won"]          += 1   # all 4 TPs = definite win
                    risk_state["consec_losses"]   = 0   # reset streak
                    stats["trades_list"].append({
                        "sym": sym, "direction": direction, "result": "ALL_TP",
                        "pnl": round(pos["currentPnl"], 2), "time": utc_now_str(),
                    })
                    total      = stats["total"]
                    trades_won = stats["trades_won"]
                    win_rate   = round(trades_won / total * 100, 1) if total > 0 else 0
                    net_pnl    = round(stats["profit_usdt"] - stats["loss_usdt"], 2)
                    trade_pnl  = round(pos["currentPnl"], 2)
                    side_word  = "LONG" if direction == "BUY" else "SHORT"
                    notifications.append(
                        f"<b>🎯 ALL 4 TPs HIT - {sym}!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📌 #{pos.get('sig_id', '?')}  |  {side_word}\n"
                        f"Entry: {fmt_p(entry)} → All 4 targets hit!\n"
                        f"{tp_progress_bar(4, direction)}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"💰 This trade: <b>+${trade_pnl:.2f} USDT</b>\n"
                        f"⏱ Time: {elapsed_str(elapsed)}\n\n"
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
                                    sig_id=pos.get("sig_id"), tp_hit_total=tp_num,
                                    trade_pnl_so_far=pos.get("currentPnl", 0))
                    )
                    print(f"  TP{tp_num} hit: {sym} @ ${price}")

        # End of with state_lock

    # Cleanup positions
    with state_lock:
        for sym in to_remove:
            positions.pop(sym, None)

    # FIX: send all notifications OUTSIDE the lock - no HTTP while holding state_lock
    for msg in notifications:
        tg_send(msg)


# ─────────────────────────── MAIN ─────────────────────────────
def close_stale_position(sym, pos, scan_prices):
    """Close a stale position using current live price and book accurate P&L."""
    global paper_balance
    coin_data = next((c for c in COINS if c["symbol"] == sym), None)
    if not coin_data:
        return
    d = scan_prices.get(coin_data["id"]) or {}
    live_price = d.get("usd")
    if not live_price:
        live_price = pos.get("exec_price", 0)

    with state_lock:
        # Re‑check existence inside lock
        if sym not in positions:
            return
        pos = positions[sym]   # re‑read under lock
        direction  = pos["direction"]
        exec_price = pos["exec_price"]
        tp_hit     = pos["tp_hit"]
        rem_pct    = 1.0 - (tp_hit * 0.25)
        trade_size = pos["margin"]
        remaining_margin = trade_size * rem_pct

        # Calculate unrealized P&L on the remaining position only
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

        # Only add the unrealized part to stats (prior TP profits already counted)
        if unrealized_pnl >= 0:
            stats["profit_usdt"] += unrealized_pnl
        else:
            stats["loss_usdt"] += abs(unrealized_pnl)

        # Return remaining margin + unrealized profit/loss
        paper_balance += remaining_margin + unrealized_pnl

        stats["total"] += 1
        stats["trades_list"].append({
            "sym": sym,
            "direction": direction,
            "result": "STALE_CLOSE",
            "pnl": total_trade_pnl,
            "time": utc_now_str(),
        })
        positions.pop(sym, None)

        # Build message (outside lock but using the now‑cleaned state)
        age_h = (time.time() - pos["opened_at"]) / 3600
        sign  = "+" if total_trade_pnl >= 0 else ""
        tg_send(
            f"<b>⏰ STALE TRADE CLOSED - {sym}</b>\n"
            f"Position open: {age_h:.1f}h (max {MAX_TRADE_HOURS}h)\n"
            f"Final price: {fmt_p(live_price)}\n"
            f"Trade P&L: {sign}${total_trade_pnl:.2f}\n"
            f"Balance: ${paper_balance:.2f}"
        )


def run():
    # FIX: declare all mutable globals so pruning updates the real objects
    global paper_balance, pre_warned, last_signal

    # Reset session start balance for daily loss tracking
    risk_state["session_start_balance"] = paper_balance
    risk_state["btc_last_check"]        = time.time()

    print("=" * 55)
    print("  APEX Bybit Bot v5 - PAPER TRADING MODE")
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
        "<b>⚡ APEX — PHASE 2</b>\n\n"
        f"Exchange: <b>Bybit Futures (SIMULATED)</b>\n"
        f"Trade size: <b>${TRADE_SIZE:.0f}</b> × {LEVERAGE}x = <b>${TRADE_SIZE*LEVERAGE:.0f} exposure</b>\n"
        f"Max trades: <b>{MAX_OPEN_TRADES}</b> | Max same dir: <b>{MAX_SAME_DIRECTION}</b>\n"
        f"Whitelist coins: <b>{len(COINS)}</b> proven winners only\n"
        f"Min confidence: <b>{MIN_CONF}%</b>\n\n"
        f"<b>📊 Signal Engine — 7 Indicators (proven 56.7% WR):</b>\n"
        f"• RSI(14) — overbought/oversold filter\n"
        f"• EMA 20/50 — trend direction\n"
        f"• Volume ratio — spike detection\n"
        f"• MACD — trend confirmation\n"
        f"• ATR — dynamic SL + flat filter\n"
        f"• Bollinger Bands — real breakouts\n"
        f"• OBV — real vs fake volume\n\n"
        f"<b>🔧 Phase 2 Fixes:</b>\n"
        f"• 30 whitelist coins only (proven winners)\n"
        f"• SL cooldown 2h — stops re-entry bug\n"
        f"• GAP SL + ATR cap + is_sl_safe\n\n"
        f"<b>🛡️ Risk:</b> {MAX_DAILY_LOSS_PCT*100:.0f}% daily loss | {MAX_CONSEC_LOSSES} consec SL | BTC crash guard\n\n"
        f"<b>📊 Commands:</b> /report /r /pause /resume /help\n\n"
        f"<i>Phase 2 — data-backed improvements</i>"
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

                # FIX: declare all mutable globals so pruning updates the real objects
                now = time.time()
                pre_warned  = {k: v for k, v in pre_warned.items()  if now - v < PRE_WARN_TTL}
                # Keep last_signal only for coins currently being watched (in COINS list)
                coin_syms   = {c["symbol"] for c in COINS}
                last_signal = {k: v for k, v in last_signal.items() if k in coin_syms}
                # FIX: trim pnl_history in-place so RAM never grows unbounded
                with state_lock:   # thread‑safe truncation
                    if len(stats["pnl_history"]) > 600:
                        stats["pnl_history"] = stats["pnl_history"][-500:]

                # Fetch fresh prices once, then close stale positions with accurate P&L
                scan_prices = get_prices()
                if not scan_prices:
                    time.sleep(2)
                    continue

                prices = scan_prices

                # ── Check for stale positions (open > MAX_TRADE_HOURS) ──
                stale_syms = check_stale_positions()
                for sym in stale_syms:
                    pos_snapshot = positions.get(sym)
                    if pos_snapshot:
                        close_stale_position(sym, pos_snapshot, scan_prices)

                # ── Circuit breaker check ──
                paused = check_circuit_breakers(scan_prices)
                if paused:
                    print(f"  🛑 Trading paused: {risk_state['pause_reason']}")
                    time.sleep(2)
                    continue

                # ── Phase 2: Prune expired SL cooldowns ──
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
                    # Phase 2: skip coins in SL cooldown
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
                    # Phase 2: skip coins in SL cooldown
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

                    # ── Phase 1: Volume filter - skip low liquidity coins ──
                    if not vol or vol < MIN_24H_VOLUME:
                        continue

                    print(f"  {sym}: ${price} {round(change, 2)}%", end="")

                    ta = ta_map.get(sym)
                    if ta:
                        trend    = "↑" if ta.get("ema20") and ta.get("ema50") and ta["ema20"] > ta["ema50"] else "↓"
                        macd_c   = "M✅" if ta.get("macd_bullish") else "M⚠️"
                        bb_c     = "BB✅" if ta.get("bb_expanding") else "BB⚠️"
                        obv_c    = "OBV✅" if ta.get("obv_trend") == "rising" else "OBV⚠️"
                        atr_val  = ta.get("atr_pct")
                        atr_c    = f"ATR={atr_val:.2f}%" if atr_val is not None else "ATR=N/A"
                        print(f" | RSI={ta.get('rsi','?')} EMA={trend} {macd_c} {bb_c} {obv_c} {atr_c}", end="")

                    sig = build_signal(price, change, high, low, vol, ta)
                    print()

                    if not sig or sig["conf"] < MIN_CONF:
                        if (sig and sig["conf"] >= MIN_CONF - 8
                                and sym not in pre_warned
                                and sym not in positions):
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

                    # ── Phase 1: Execute FIRST - only notify if trade opens ──
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