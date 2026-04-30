"""
APEX Copy Signal Lab — Phase 2.5
==================================
Completely separate from APEX own signal engine.
Paper trading only. No real orders ever placed.

Monitors 3 free Telegram signal channels and paper trades their calls.
After 30-60 days, compare real data to decide which source (if any)
deserves real money.

APEX bot is NEVER modified. This runs alongside it.

Railway service: copy-lab
Start command:   python apex_copy_engine.py

Railway env vars needed:
  TELEGRAM_TOKEN        (same bot token as APEX)
  TELEGRAM_CHAT_ID      (same chat as APEX)
  TELEGRAM_API_ID       (from my.telegram.org)
  TELEGRAM_API_HASH     (from my.telegram.org)
  TELETHON_SESSION      (generated once — see setup below)

To generate TELETHON_SESSION string:
  pip install telethon
  python3 -c "
  from telethon.sync import TelegramClient
  from telethon.sessions import StringSession
  api_id = YOUR_API_ID
  api_hash = 'YOUR_API_HASH'
  with TelegramClient(StringSession(), api_id, api_hash) as client:
      print(client.session.save())
  "
  Paste the output string as TELETHON_SESSION env var on Railway.

Separate balances:
  GGShot:       $500 paper
  Coin_Signals: $500 paper
  Raven:        $500 paper
"""

import os
import re
import csv
import json
import time
import asyncio
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify

# ─────────────────────────── COPY ENGINE CONFIG ───────────────
COPY_TRADE_SIZE      = 25.0      # $25 margin per copied trade
COPY_LEVERAGE        = 10        # 10x → $250 exposure per trade
COPY_MAX_OPEN        = 2         # max 2 open copy trades per channel
COPY_SIGNAL_TTL      = 7200      # 2 hours to wait for price to enter zone
COPY_SLIPPAGE_PCT    = 0.001

# Per-channel starting balance
CHANNEL_BALANCES = {
    "ggshot":                    500.0,
    "coin_signals":              500.0,
    "raven":                     500.0,
}

# Telegram channel usernames to monitor (exact as they appear on Telegram)
COPY_CHANNELS = {
    "ggshot":       "ggshot",
    "coin_signals": "Coin_Signals",
    "raven":        "RavenSignalsproofficiaII",
}

# ─────────────────────────── ENV VARS ─────────────────────────
TG_TOKEN      = os.environ.get("TELEGRAM_TOKEN",   "").strip()
TG_CHAT       = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TG_API_ID     = os.environ.get("TELEGRAM_API_ID",  "").strip()
TG_API_HASH   = os.environ.get("TELEGRAM_API_HASH","").strip()
TG_SESSION    = os.environ.get("TELETHON_SESSION", "").strip()

# ─────────────────────────── DATA PATHS ───────────────────────
DATA_DIR        = Path(os.environ.get("COPY_DATA_DIR", "/app"))
COPY_JSON       = DATA_DIR / "copy_trades.json"
COPY_CSV        = DATA_DIR / "copy_trades.csv"

CSV_COLUMNS = [
    "source_channel","message_id","symbol","direction",
    "entry_price","close_price","close_reason","tp_hit_count",
    "pnl_usdt","pnl_pct","opened_at","closed_at","duration_minutes",
]

# ─────────────────────────── STATE ────────────────────────────
state_lock     = threading.RLock()

# Per-channel positions: copy_positions["ggshot"]["BTCUSDT"] = {...}
copy_positions = {ch: {} for ch in CHANNEL_BALANCES}

# Per-channel balances (mutable)
copy_balances  = dict(CHANNEL_BALANCES)

# Pending signals waiting for price to enter zone
# pending_signals[channel][symbol] = {signal_data, expires_at}
pending_signals = {ch: {} for ch in CHANNEL_BALANCES}

# Per-channel closed trade history
copy_closed    = {ch: [] for ch in CHANNEL_BALANCES}

# Per-channel stats
copy_stats     = {
    ch: {
        "total": 0, "wins": 0, "tp_hit": 0, "sl_hit": 0,
        "profit": 0.0, "loss": 0.0,
    }
    for ch in CHANNEL_BALANCES
}

copy_paused    = False
signal_log     = []   # last 20 signal events for /copydebug
MAX_LOG        = 20

session_http   = requests.Session()

# ─────────────────────────── HELPERS ──────────────────────────
def utc_now_str():
    return datetime.now(timezone.utc).strftime("%H:%M UTC")


def fmt_p(price):
    if price is None:
        return "N/A"
    if price >= 1000:    return f"${price:.1f}"
    if price >= 100:     return f"${price:.2f}"
    if price >= 1:       return f"${price:.4f}"
    if price >= 0.01:    return f"${price:.5f}"
    return f"${price:.8f}"


def channel_label(ch):
    labels = {
        "ggshot":       "GGShot",
        "coin_signals": "Coin_Signals",
        "raven":        "Raven",
    }
    return labels.get(ch, ch.upper())


# ─────────────────────────── TELEGRAM ─────────────────────────
def tg_send(msg):
    if not TG_TOKEN or not TG_CHAT:
        print("TG:", msg[:80])
        return
    try:
        session_http.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": msg,
                  "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        print(f"TG error: {e}")


def tg_updates(offset=None):
    params = {"timeout": 1, "allowed_updates": '["message"]'}
    if offset:
        params["offset"] = offset
    try:
        r = session_http.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params=params, timeout=5,
        )
        return r.json()
    except Exception:
        return None


# ─────────────────────────── PRICES ───────────────────────────
_price_cache   = {}
_price_cache_t = 0.0

def get_price(symbol_usdt):
    """Get live price from Binance for a symbol like BTCUSDT."""
    global _price_cache, _price_cache_t
    # Refresh cache every 10 seconds
    if time.time() - _price_cache_t > 10:
        _price_cache   = {}
        _price_cache_t = time.time()
    if symbol_usdt in _price_cache:
        return _price_cache[symbol_usdt]
    try:
        r = session_http.get(
            f"https://api.binance.com/api/v3/ticker/price?symbol={symbol_usdt}",
            timeout=5,
        )
        price = float(r.json()["price"])
        _price_cache[symbol_usdt] = price
        return price
    except Exception:
        return None


# ─────────────────────── DEBUG LOGGER ────────────────────────
def log_signal_event(status, symbol, channel, reason="", extra=""):
    """
    Record signal event for /copydebug command.
    Status: PARSED_OK | EXECUTED | QUEUED | EXECUTED_FROM_PENDING
            EXPIRED_PENDING | REJECTED
    """
    global signal_log
    entry = {
        "time":    utc_now_str(),
        "status":  status,
        "symbol":  symbol,
        "channel": channel_label(channel) if channel else "",
        "reason":  reason,
        "extra":   extra,
    }
    signal_log.append(entry)
    if len(signal_log) > MAX_LOG:
        signal_log = signal_log[-MAX_LOG:]
    # Always print to Railway logs
    print(f"  [{status}] {channel}/{symbol} — {reason} {extra}".strip())


# ─────────────────────── SIGNAL PARSER ────────────────────────
def _normalize_symbol(raw):
    """
    Normalize any symbol format to XXXUSDT.
    Handles: SOLUSDT, SOL/USDT, #SOL/USDT, $SOL, SOL USDT
    """
    raw = raw.upper().strip()
    # Remove # $ prefixes
    raw = raw.lstrip('#$')
    # Remove /USDT or USDT suffix then re-add
    raw = raw.replace('/USDT', '').replace('USDT', '').strip()
    # Remove trailing slash
    raw = raw.rstrip('/')
    if not raw:
        return None
    return raw + 'USDT'


def parse_signal(text):
    """
    Robust multi-format signal parser.
    Returns parsed dict or None if not a valid signal.
    Logs rejection reason for every failed parse.

    A valid signal MUST have ALL of:
      - Symbol
      - Direction (LONG/SHORT)
      - Entry zone or single entry price
      - Stop Loss
      - At least 1 Take Profit
    """
    if not text or len(text) < 20:
        return None

    t = text.upper()

    # ── Quick pre-filter: skip obvious non-signals ──
    noise_phrases = [
        'TAKE-PROFIT TARGET', 'TP HIT', 'PROFIT:', 'BOOM', 'CONGRAT',
        'JUST IN:', 'BREAKING', 'FED ', 'RATE ', 'ALL-TIME HIGH',
        'BACK TO BACK', 'FLYING', 'SIGNALS THAT SPEAK', 'VIP CHANNEL',
        'SUBSCRIBE', 'JOIN US', 'CONTACT:', 'TELEGRAM.ME',
    ]
    for phrase in noise_phrases:
        if phrase in t:
            return None

    # ── Direction ──
    direction = None
    if re.search(r'\bLONG\b|\bBUY\b', t):
        direction = 'BUY'
    elif re.search(r'\bSHORT\b|\bSELL\b', t):
        direction = 'SELL'
    if not direction:
        return None

    # ── Symbol — try multiple patterns ──
    symbol = None
    sym_patterns = [
        r'[#$]([A-Z]{2,10})[/\s]?USDT',   # #SOL/USDT or $SOL USDT
        r'\b([A-Z]{2,10})USDT\b',           # SOLUSDT
        r'\b([A-Z]{2,10})/USDT\b',          # SOL/USDT
        r'[#$]([A-Z]{2,10})\b',             # #SOL or $SOL
        r'\bPAIR[:\s]+([A-Z]{2,10})',       # PAIR: SOL
    ]
    skip = {'TP', 'SL', 'BUY', 'SELL', 'LONG', 'SHORT', 'ENTRY',
            'USDT', 'USD', 'THE', 'FOR', 'AND', 'STOP', 'TAKE',
            'TARGET', 'PROFIT', 'LOSS', 'BINANCE', 'BYBIT'}
    for pat in sym_patterns:
        m = re.search(pat, t)
        if m:
            raw = m.group(1).strip()
            if raw not in skip and len(raw) >= 2:
                symbol = _normalize_symbol(raw)
                break
    if not symbol:
        return None  # REJECTED: no symbol

    # ── Entry zone ──
    entry_low = entry_high = None
    zone_patterns = [
        # "Entry: 85.31 - 87.86" or "Entry Zone: 85.31-87.86"
        r'ENTRY\s*(?:ZONE)?[:\s]*([0-9.]+)\s*[-–TO]+\s*([0-9.]+)',
        # "Buy Zone 2.10 - 2.08"
        r'(?:BUY|ENTRY)\s*ZONE[:\s]*([0-9.]+)\s*[-–TO]+\s*([0-9.]+)',
        # "85.31 to 87.86"
        r'([0-9.]+)\s+TO\s+([0-9.]+)',
        # Generic range "85.31 - 87.86" (must be near entry keyword)
        r'(?:ENTRY|ZONE|PRICE)[^\n]*?([0-9.]+)\s*[-–]\s*([0-9.]+)',
    ]
    for pat in zone_patterns:
        m = re.search(pat, t)
        if m:
            try:
                a, b = float(m.group(1)), float(m.group(2))
                if a > 0 and b > 0:
                    entry_low  = min(a, b)
                    entry_high = max(a, b)
                    break
            except Exception:
                pass

    # Single entry price fallback
    if not entry_low:
        m = re.search(r'ENTRY[:\s]+([0-9.]+)', t)
        if m:
            try:
                v = float(m.group(1))
                if v > 0:
                    # Create a small zone ±0.5% around single price
                    entry_low  = round(v * 0.995, 8)
                    entry_high = round(v * 1.005, 8)
            except Exception:
                pass

    if not entry_low:
        return None  # REJECTED: no entry

    # ── Stop Loss (required) ──
    sl = None
    sl_patterns = [
        r'(?:STOPLOSS|STOP\s*LOSS|STOP)[:\s]+([0-9.]+)',
        r'\bSL[:\s]+([0-9.]+)',
    ]
    for pat in sl_patterns:
        m = re.search(pat, t)
        if m:
            try:
                sl = float(m.group(1))
                break
            except Exception:
                pass
    if not sl:
        return None  # REJECTED: no SL

    # ── Take Profits (at least 1 required) ──
    tps = []
    tp_patterns = [
        r'(?:TARGET|TAKE.?PROFIT|TP)\s*(\d)\s*[:\s]+([0-9.]+)',
        r'(?:TARGET|TP)[:\s]+([0-9.]+)',
        r'T\s*(\d)\s*[:\s]+([0-9.]+)',
    ]
    seen = set()
    for pat in tp_patterns:
        for m in re.finditer(pat, t):
            try:
                val = float(m.group(2) if m.lastindex >= 2 else m.group(1))
                if val > 0 and val not in seen:
                    seen.add(val)
                    tps.append(val)
            except Exception:
                pass

    if not tps:
        return None  # REJECTED: no TPs

    # Sort TPs in correct direction
    if direction == 'BUY':
        tps = sorted(tps)       # ascending for longs
    else:
        tps = sorted(tps, reverse=True)  # descending for shorts

    # Pad to 4 TPs
    while len(tps) < 4:
        if len(tps) >= 2:
            diff = abs(tps[-1] - tps[-2])
            if direction == 'BUY':
                tps.append(round(tps[-1] + diff, 8))
            else:
                tps.append(round(tps[-1] - diff, 8))
        else:
            gap = abs(tps[0] - (entry_low + entry_high) / 2) * 0.5
            if direction == 'BUY':
                tps.append(round(tps[-1] + gap, 8))
            else:
                tps.append(round(tps[-1] - gap, 8))

    # ── Validate direction logic ──
    entry_mid = (entry_low + entry_high) / 2
    if direction == 'BUY':
        if sl >= entry_mid:     return None  # SL above entry — invalid
        if tps[0] <= entry_mid: return None  # TP below entry — invalid
    else:
        if sl <= entry_mid:     return None  # SL below entry — invalid
        if tps[0] >= entry_mid: return None  # TP above entry — invalid

    return {
        "symbol":      symbol,
        "direction":   direction,
        "entry_low":   entry_low,
        "entry_high":  entry_high,
        "entry_mid":   entry_mid,
        "tp1":         tps[0],
        "tp2":         tps[1],
        "tp3":         tps[2],
        "tp4":         tps[3],
        "sl":          sl,
    }


# ─────────────────── COPY EXECUTE ─────────────────────────────
def copy_execute(channel, sig, price, msg_id):
    """Open a paper copy trade for the given channel."""
    sym       = sig["symbol"]
    direction = sig["direction"]
    notional  = COPY_TRADE_SIZE * COPY_LEVERAGE

    if direction == "BUY":
        exec_price = round(price * (1 + COPY_SLIPPAGE_PCT), 8)
        liq_price  = round(exec_price * (1 - 0.9 / COPY_LEVERAGE), 8)
    else:
        exec_price = round(price * (1 - COPY_SLIPPAGE_PCT), 8)
        liq_price  = round(exec_price * (1 + 0.9 / COPY_LEVERAGE), 8)

    with state_lock:
        if copy_balances[channel] < COPY_TRADE_SIZE:
            print(f"  ⛔ Copy balance too low for {channel}")
            return False
        if sym in copy_positions[channel]:
            print(f"  ⛔ {sym} already open in {channel}")
            return False
        n_open = len(copy_positions[channel])
        if n_open >= COPY_MAX_OPEN:
            print(f"  ⛔ Max copy trades reached for {channel}")
            return False

        copy_balances[channel]    -= COPY_TRADE_SIZE
        copy_positions[channel][sym] = {
            "source_channel": channel,
            "message_id":     msg_id,
            "direction":      direction,
            "entry_price":    price,
            "exec_price":     exec_price,
            "notional":       notional,
            "margin":         COPY_TRADE_SIZE,
            "leverage":       COPY_LEVERAGE,
            "tp1":            sig["tp1"],
            "tp2":            sig["tp2"],
            "tp3":            sig["tp3"],
            "tp4":            sig["tp4"],
            "sl":             sig["sl"],
            "liq_price":      liq_price,
            "tp_hit":         0,
            "breakeven":      False,
            "currentPnl":     0.0,
            "unrealized_pnl": 0.0,
            "opened_at":      time.time(),
        }

    label = channel_label(channel)
    side  = "LONG" if direction == "BUY" else "SHORT"
    tg_send(
        f"<b>📥 COPY TRADE OPENED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Source: <b>{label}</b>\n"
        f"Coin: <b>{sym}</b>  {side}\n"
        f"Entry: {fmt_p(exec_price)}\n"
        f"${COPY_TRADE_SIZE:.0f} margin | {COPY_LEVERAGE}x\n\n"
        f"TP1: {fmt_p(sig['tp1'])}  TP2: {fmt_p(sig['tp2'])}\n"
        f"TP3: {fmt_p(sig['tp3'])}  TP4: {fmt_p(sig['tp4'])}\n"
        f"SL:  {fmt_p(sig['sl'])}\n\n"
        f"Balance [{label}]: ${copy_balances[channel]:.2f}"
    )
    print(f"  📥 Copy trade: {channel}/{sym} {direction} @ {exec_price}")
    return True


# ─────────────────── COPY MONITOR ─────────────────────────────
def monitor_copy_positions():
    """Check all open copy positions for TP/SL hits."""
    for channel, positions in copy_positions.items():
        to_remove  = []
        notifications = []

        for sym, pos in list(positions.items()):
            price = get_price(sym)
            if not price:
                continue

            with state_lock:
                direction  = pos["direction"]
                exec_price = pos["exec_price"]
                sl         = pos["sl"]
                liq_price  = pos["liq_price"]
                tp_hit     = pos["tp_hit"]
                tp_levels  = [pos["tp1"], pos["tp2"], pos["tp3"], pos["tp4"]]
                margin     = pos["margin"]
                elapsed    = time.time() - pos["opened_at"]

                # Live unrealized PnL
                rem_pct = 1.0 - (tp_hit * 0.25)
                if direction == "BUY":
                    move_pct = (price - exec_price) / exec_price * 100
                else:
                    move_pct = (exec_price - price) / exec_price * 100
                pos["unrealized_pnl"] = round(
                    margin * COPY_LEVERAGE * move_pct / 100 * rem_pct, 2)

                # ── Liquidation ──
                liq_hit = (direction == "BUY"  and price <= liq_price) or \
                          (direction == "SELL" and price >= liq_price)
                sl_hit  = (direction == "BUY"  and price <= sl) or \
                          (direction == "SELL" and price >= sl)

                # SL check first (same as APEX Phase 1 principle)
                if sl_hit or liq_hit:
                    close_price  = sl  # close at SL even if liq triggered
                    rem_pct_n    = 1.0 - (tp_hit * 0.25)
                    price_move   = abs(close_price - exec_price) / exec_price * 100
                    is_profit    = (direction == "BUY"  and close_price >= exec_price) or \
                                   (direction == "SELL" and close_price <= exec_price)
                    sl_pnl       = round(margin * COPY_LEVERAGE * price_move / 100 * rem_pct_n, 2)
                    prior        = pos.get("currentPnl", 0)
                    total_pnl    = round(prior + (sl_pnl if is_profit else -sl_pnl), 2)
                    remaining    = margin * rem_pct_n

                    if is_profit:
                        copy_balances[channel]       += remaining + sl_pnl
                        copy_stats[channel]["profit"] += sl_pnl
                    else:
                        copy_balances[channel]       += max(0, remaining - sl_pnl)
                        copy_stats[channel]["loss"]   += sl_pnl

                    copy_stats[channel]["total"]   += 1
                    copy_stats[channel]["sl_hit"]  += 1
                    if is_profit or pos.get("breakeven"):
                        copy_stats[channel]["wins"] += 1
                    close_reason = "SL" if sl_hit else "GAP_SL"
                    _save_copy_trade(channel, sym, pos, close_price,
                                     close_reason, total_pnl)
                    label = channel_label(channel)
                    sign  = "+" if total_pnl >= 0 else ""
                    notifications.append(
                        f"<b>📊 COPY CLOSED — {sym}</b>\n"
                        f"Source: {label} | {close_reason}\n"
                        f"Result: {sign}${total_pnl:.2f} USDT\n"
                        f"Balance [{label}]: ${copy_balances[channel]:.2f}"
                    )
                    to_remove.append(sym)
                    continue

                # ── TP check ──
                if tp_hit >= 4:
                    to_remove.append(sym)
                    continue

                next_tp     = tp_levels[tp_hit]
                tp_reached  = (direction == "BUY"  and price >= next_tp) or \
                              (direction == "SELL" and price <= next_tp)

                if tp_reached:
                    tp_num     = tp_hit + 1
                    pnl_pct    = abs(next_tp - exec_price) / exec_price * 100
                    tp_pnl     = round(margin * COPY_LEVERAGE * pnl_pct / 100 * 0.25, 2)
                    q_margin   = margin * 0.25

                    copy_balances[channel]           += q_margin + tp_pnl
                    copy_stats[channel]["profit"]    += tp_pnl
                    copy_stats[channel]["tp_hit"]    += 1
                    pos["currentPnl"]                 = round(pos.get("currentPnl", 0) + tp_pnl, 2)
                    pos["tp_hit"]                     = tp_num

                    # Trail SL
                    if tp_num == 1 and not pos["breakeven"]:
                        pos["sl"]        = exec_price
                        pos["breakeven"] = True
                    elif tp_num == 2:
                        pos["sl"] = tp_levels[0]
                    elif tp_num == 3:
                        pos["sl"] = tp_levels[1]

                    label = channel_label(channel)
                    notifications.append(
                        f"<b>✅ COPY TP{tp_num} HIT — {sym}</b>\n"
                        f"Source: {label}\n"
                        f"+${tp_pnl:.2f} USDT  |  "
                        f"Trade so far: +${pos['currentPnl']:.2f}"
                    )

                    if tp_num == 4:
                        copy_stats[channel]["total"] += 1
                        copy_stats[channel]["wins"]  += 1
                        total_pnl = pos["currentPnl"]
                        _save_copy_trade(channel, sym, pos, next_tp,
                                         "ALL_TP", total_pnl)
                        notifications.append(
                            f"<b>🎯 COPY ALL TPs HIT — {sym}!</b>\n"
                            f"Source: {label}\n"
                            f"Total: +${total_pnl:.2f} USDT\n"
                            f"Balance [{label}]: ${copy_balances[channel]:.2f}"
                        )
                        to_remove.append(sym)

        with state_lock:
            for sym in to_remove:
                copy_positions[channel].pop(sym, None)

        for msg in notifications:
            tg_send(msg)


# ─────────────────── PENDING SIGNALS ──────────────────────────
def check_pending_signals():
    """Check if any pending signals have entered their entry zone."""
    now = time.time()
    for channel, pending in pending_signals.items():
        expired = []
        for sym, ps in list(pending.items()):
            if now > ps["expires_at"]:
                expired.append(sym)
                log_signal_event("EXPIRED_PENDING", sym, channel,
                                 "2h window passed without price entering zone")
                continue
            price = get_price(sym)
            if not price:
                continue
            sig     = ps["sig"]
            in_zone = sig["entry_low"] <= price <= sig["entry_high"]
            if in_zone:
                # Check still valid
                with state_lock:
                    if sym in copy_positions[channel]:
                        expired.append(sym)
                        continue
                    if len(copy_positions[channel]) >= COPY_MAX_OPEN:
                        expired.append(sym)
                        continue
                opened = copy_execute(channel, sig, price, ps["msg_id"])
                if opened:
                    log_signal_event("EXECUTED_FROM_PENDING", sym, channel,
                                     "price entered zone", f"price={price}")
                expired.append(sym)

        for sym in expired:
            pending.pop(sym, None)


# ─────────────────── PERSISTENCE ──────────────────────────────
def _ensure_dirs():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _save_copy_trade(channel, sym, pos, close_price, close_reason, pnl_usdt):
    """Append closed copy trade to JSON and CSV."""
    _ensure_dirs()
    opened_ts  = pos.get("opened_at", time.time())
    closed_ts  = time.time()
    dur        = round((closed_ts - opened_ts) / 60, 1)
    pnl_pct    = round(pnl_usdt / pos.get("margin", COPY_TRADE_SIZE) * 100, 2)

    rec = {
        "source_channel":  channel,
        "message_id":      pos.get("message_id", ""),
        "symbol":          sym,
        "direction":       pos.get("direction", ""),
        "entry_price":     pos.get("entry_price", 0),
        "close_price":     close_price,
        "close_reason":    close_reason,
        "tp_hit_count":    pos.get("tp_hit", 0),
        "pnl_usdt":        round(pnl_usdt, 2),
        "pnl_pct":         pnl_pct,
        "opened_at":       datetime.fromtimestamp(opened_ts, tz=timezone.utc).isoformat(),
        "closed_at":       datetime.fromtimestamp(closed_ts, tz=timezone.utc).isoformat(),
        "duration_minutes":dur,
    }

    # JSON
    try:
        existing = json.loads(COPY_JSON.read_text()) if COPY_JSON.exists() else []
        existing.append(rec)
        COPY_JSON.write_text(json.dumps(existing, indent=2))
    except Exception as e:
        print(f"  ⚠️ Copy JSON save failed: {e}")

    # CSV
    try:
        write_header = not COPY_CSV.exists()
        with open(COPY_CSV, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            if write_header:
                w.writeheader()
            w.writerow(rec)
    except Exception as e:
        print(f"  ⚠️ Copy CSV save failed: {e}")

    # In-memory
    copy_closed[channel].append(rec)


def load_copy_history():
    """Reload closed copy trades on restart."""
    _ensure_dirs()
    if not COPY_JSON.exists():
        print("  📂 No copy_trades.json — starting fresh")
        return
    try:
        saved = json.loads(COPY_JSON.read_text())
        for rec in saved:
            ch  = rec.get("source_channel")
            pnl = rec.get("pnl_usdt", 0)
            cr  = rec.get("close_reason", "")
            if ch not in copy_stats:
                continue
            copy_closed[ch].append(rec)
            copy_stats[ch]["total"] += 1
            if pnl > 0:
                copy_stats[ch]["profit"] += pnl
                copy_stats[ch]["wins"]   += 1
            else:
                copy_stats[ch]["loss"]   += abs(pnl)
            copy_stats[ch]["tp_hit"] += rec.get("tp_hit_count", 0)
            if cr in ("SL", "GAP_SL"):
                copy_stats[ch]["sl_hit"] += 1
        # Restore balances from history
        for ch in copy_balances:
            hist_net = sum(r["pnl_usdt"] for r in copy_closed[ch])
            copy_balances[ch] = round(CHANNEL_BALANCES[ch] + hist_net, 2)
        n = len(saved)
        print(f"  📂 Loaded {n} copy trades from history")
        tg_send(f"<b>📂 Copy history loaded</b>\n{n} trades restored.")
    except Exception as e:
        print(f"  ⚠️ Could not load copy history: {e}")


# ─────────────────── TELEGRAM COMMANDS ────────────────────────
def _cmd_copyreport():
    with state_lock:
        total_open = sum(len(p) for p in copy_positions.values())
        lines = []
        for ch in CHANNEL_BALANCES:
            s   = copy_stats[ch]
            wr  = round(s["wins"]/s["total"]*100, 1) if s["total"] else 0
            net = round(s["profit"] - s["loss"], 2)
            sign= "+" if net >= 0 else ""
            n_open = len(copy_positions[ch])
            lines.append(
                f"<b>{channel_label(ch)}</b>\n"
                f"  Balance: ${copy_balances[ch]:.2f}  |  Open: {n_open}\n"
                f"  Trades: {s['total']}  WR: {wr}%  Net: {sign}${net:.2f}"
            )
        total_closed = sum(s["total"] for s in copy_stats.values())
        all_profit   = sum(s["profit"] for s in copy_stats.values())
        all_loss     = sum(s["loss"]   for s in copy_stats.values())
        net_all      = round(all_profit - all_loss, 2)

        # Best/worst channel by net
        nets = {ch: round(copy_stats[ch]["profit"]-copy_stats[ch]["loss"],2)
                for ch in copy_stats}
        best  = max(nets, key=nets.get)
        worst = min(nets, key=nets.get)

    paused_str = "\n⚠️ Copy engine PAUSED" if copy_paused else ""
    return (
        f"<b>📊 COPY SIGNAL LAB — {utc_now_str()}</b>\n"
        f"══════════════════════════════\n"
        f"Total closed: {total_closed}  |  Open: {total_open}\n"
        f"Combined net: {'+'if net_all>=0 else ''}${net_all:.2f}\n"
        f"Best channel:  {channel_label(best)} (${nets[best]:+.2f})\n"
        f"Worst channel: {channel_label(worst)} (${nets[worst]:+.2f})\n"
        f"{paused_str}\n\n"
        + "\n\n".join(lines)
    )


def _cmd_copychannels():
    lines = []
    for ch, username in COPY_CHANNELS.items():
        n_open   = len(copy_positions[ch])
        n_closed = copy_stats[ch]["total"]
        lines.append(
            f"<b>{channel_label(ch)}</b> @{username}\n"
            f"  Open: {n_open} | Closed: {n_closed} | "
            f"Balance: ${copy_balances[ch]:.2f}"
        )
    return (
        f"<b>📡 TRACKED CHANNELS</b>\n\n"
        + "\n\n".join(lines)
    )


def _cmd_copydebug():
    """Show last 10 signal events."""
    if not signal_log:
        return "<b>📋 COPY DEBUG</b>\n\nNo signals received yet."
    recent = signal_log[-10:]
    icons  = {
        "PARSED_OK":             "🔍",
        "EXECUTED":              "✅",
        "QUEUED":                "⏳",
        "EXECUTED_FROM_PENDING": "🚀",
        "EXPIRED_PENDING":       "⏰",
        "REJECTED":              "❌",
    }
    lines = []
    for e in reversed(recent):
        icon = icons.get(e["status"], "•")
        lines.append(
            f"{icon} <b>{e['status']}</b> — {e['channel']}/{e['symbol']}\n"
            f"   {e['time']}  {e['reason']}"
        )
    return (
        f"<b>📋 COPY DEBUG — last {len(recent)} signals</b>\n"
        f"══════════════════════════════\n\n"
        + "\n\n".join(lines)
    )


def _cmd_pendingcopy():
    """Show all pending signals with time remaining."""
    lines = []
    now   = time.time()
    for channel, pending in pending_signals.items():
        for sym, ps in pending.items():
            sig         = ps["sig"]
            remaining   = max(0, ps["expires_at"] - now)
            mins        = int(remaining // 60)
            price       = get_price(sym)
            price_str   = fmt_p(price) if price else "N/A"
            side        = "LONG" if sig["direction"] == "BUY" else "SHORT"
            lines.append(
                f"<b>{sym}</b> {side} [{channel_label(channel)}]\n"
                f"  Zone: {fmt_p(sig['entry_low'])} – {fmt_p(sig['entry_high'])}\n"
                f"  Now:  {price_str}  |  Expires: {mins}m"
            )
    if not lines:
        return "<b>⏳ PENDING SIGNALS</b>\n\nNone waiting."
    return (
        f"<b>⏳ PENDING SIGNALS ({len(lines)})</b>\n"
        f"══════════════════════════════\n\n"
        + "\n\n".join(lines)
    )


def check_copy_commands(offset):
    global copy_paused
    updates = tg_updates(offset)
    if updates and updates.get("ok"):
        for u in updates.get("result", []):
            offset = u["update_id"] + 1
            msg  = u.get("message") or u.get("channel_post") or {}
            text = msg.get("text", "").strip().lower()
            if text == "/copyreport":
                tg_send(_cmd_copyreport())
            elif text == "/copychannels":
                tg_send(_cmd_copychannels())
            elif text == "/copydebug":
                tg_send(_cmd_copydebug())
            elif text == "/pendingcopy":
                tg_send(_cmd_pendingcopy())
            elif text == "/copypause":
                copy_paused = True
                tg_send("<b>⏸ Copy engine paused.</b>")
            elif text == "/copyresume":
                copy_paused = False
                tg_send("<b>▶️ Copy engine resumed.</b>")
            elif text == "/copyhelp":
                tg_send(
                    "<b>📡 Copy Lab Commands</b>\n\n"
                    "/copyreport    — Full lab report\n"
                    "/copychannels  — Show tracked channels\n"
                    "/copydebug     — Last 10 signal events\n"
                    "/pendingcopy   — Pending signals + time left\n"
                    "/copypause     — Pause new copies\n"
                    "/copyresume    — Resume\n"
                    "/copyhelp      — This message"
                )
    return offset


# ─────────────────────────── FLASK ────────────────────────────
app = Flask(__name__)

@app.route("/")
def health():
    return "APEX Copy Lab running!", 200

@app.route("/data")
def get_data():
    with state_lock:
        open_pos = {}
        for ch, positions in copy_positions.items():
            for sym, pos in positions.items():
                open_pos[f"{ch}/{sym}"] = {
                    "channel":       ch,
                    "symbol":        sym,
                    "direction":     pos["direction"],
                    "entry":         pos["entry_price"],
                    "tp_hit":        pos["tp_hit"],
                    "breakeven":     pos["breakeven"],
                    "unrealized":    pos.get("unrealized_pnl", 0),
                    "realized":      pos.get("currentPnl", 0),
                }
        payload = {
            "balances":    copy_balances,
            "stats":       copy_stats,
            "openTrades":  open_pos,
            "timestamp":   utc_now_str(),
        }
    resp = jsonify(payload)
    resp.headers.add("Access-Control-Allow-Origin", "*")
    return resp


def start_flask():
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    port = int(os.environ.get("PORT", 8081))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ─────────────────── TELETHON LISTENER ────────────────────────
def start_telethon():
    """
    Start Telethon async listener in a background thread.
    Monitors signal channels and triggers copy_execute.
    Requires TELEGRAM_API_ID, TELEGRAM_API_HASH, TELETHON_SESSION.
    """
    if not TG_API_ID or not TG_API_HASH or not TG_SESSION:
        print("  ⚠️ Telethon not configured — copy engine in MANUAL mode")
        print("  Set TELEGRAM_API_ID, TELEGRAM_API_HASH, TELETHON_SESSION")
        tg_send(
            "<b>⚠️ Copy Lab — Telethon not configured</b>\n\n"
            "Set env vars to enable channel monitoring:\n"
            "TELEGRAM_API_ID\nTELEGRAM_API_HASH\nTELETHON_SESSION"
        )
        return

    try:
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession
    except ImportError:
        print("  ⚠️ telethon not installed — run: pip install telethon")
        tg_send("<b>⚠️ Copy Lab</b> — telethon not installed.\nRun: pip install telethon")
        return

    async def _run():
        client = TelegramClient(
            StringSession(TG_SESSION),
            int(TG_API_ID),
            TG_API_HASH,
        )
        await client.start()
        print(f"  ✅ Telethon connected")

        channel_list = list(COPY_CHANNELS.values())

        @client.on(events.NewMessage(chats=channel_list))
        async def handler(event):
            text = event.message.message or ""
            msg_id = event.message.id
            chat   = event.chat

            # Identify which channel key this came from
            channel_key = None
            for key, username in COPY_CHANNELS.items():
                if username.lower() in (getattr(chat, 'username', '') or '').lower():
                    channel_key = key
                    break
            if not channel_key:
                return

            print(f"  📨 New msg from {channel_key}: {text[:60]}")
            sig = parse_signal(text)

            if not sig:
                # Log rejection with reason
                t = text.upper()
                if not any(w in t for w in ['LONG','SHORT','BUY','SELL']):
                    reason = "no direction"
                elif not any(w in t for w in ['USDT','BTC','ETH','SOL','BNB']):
                    reason = "no symbol"
                elif not any(w in t for w in ['SL','STOP']):
                    reason = "missing SL"
                elif not any(w in t for w in ['TP','TARGET','TAKE']):
                    reason = "no targets"
                elif not any(w in t for w in ['ENTRY','ZONE','BUY ZONE']):
                    reason = "no entry zone"
                else:
                    reason = "invalid format"
                log_signal_event("REJECTED", "?", channel_key, reason,
                                 text[:40].replace('\n',' '))
                return

            sym = sig["symbol"]
            log_signal_event("PARSED_OK", sym, channel_key,
                             f"{sig['direction']} entry={sig['entry_low']}-{sig['entry_high']} SL={sig['sl']}")

            if copy_paused:
                log_signal_event("REJECTED", sym, channel_key, "copy engine paused")
                return

            # Safety filters
            with state_lock:
                if sym in copy_positions[channel_key]:
                    log_signal_event("REJECTED", sym, channel_key, "already open")
                    return
                if len(copy_positions[channel_key]) >= COPY_MAX_OPEN:
                    log_signal_event("REJECTED", sym, channel_key,
                                     f"max {COPY_MAX_OPEN} trades open")
                    return

            price = get_price(sym)
            if not price:
                log_signal_event("REJECTED", sym, channel_key, "no price available")
                return

            # Check if price is inside entry zone
            in_zone = sig["entry_low"] <= price <= sig["entry_high"]

            if in_zone:
                opened = copy_execute(channel_key, sig, price, msg_id)
                if opened:
                    log_signal_event("EXECUTED", sym, channel_key,
                                     "inside entry zone", f"price={price}")
            else:
                # Queue as pending — wait up to 2 hours
                with state_lock:
                    pending_signals[channel_key][sym] = {
                        "sig":        sig,
                        "msg_id":     msg_id,
                        "expires_at": time.time() + COPY_SIGNAL_TTL,
                        "created_at": time.time(),
                    }
                label = channel_label(channel_key)
                side  = "LONG" if sig["direction"] == "BUY" else "SHORT"
                above_below = "above" if price > sig["entry_high"] else "below"
                log_signal_event("QUEUED", sym, channel_key,
                                 f"price {price} {above_below} zone {sig['entry_low']}-{sig['entry_high']}")
                tg_send(
                    f"<b>⏳ COPY SIGNAL QUEUED — {sym}</b>\n"
                    f"Source: {label}  |  {side}\n"
                    f"Zone: {fmt_p(sig['entry_low'])} – {fmt_p(sig['entry_high'])}\n"
                    f"Current price: {fmt_p(price)} ({above_below} zone)\n"
                    f"<i>Waiting up to 2h for price to enter zone.</i>"
                )
                print(f"  ⏳ Queued {channel_key}/{sym} — waiting for zone entry")

        await client.run_until_disconnected()

    def _thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run())
        except Exception as e:
            print(f"  ❌ Telethon error: {e}")
            tg_send(f"<b>❌ Copy Lab Telethon crashed</b>\n{e}")

    t = threading.Thread(target=_thread, daemon=True)
    t.start()


# ─────────────────────────── MAIN ─────────────────────────────
def run():
    print("=" * 55)
    print("  APEX COPY SIGNAL LAB — Phase 2.5")
    print("=" * 55)
    print(f"Channels: {list(COPY_CHANNELS.keys())}")
    print(f"Trade size: ${COPY_TRADE_SIZE} × {COPY_LEVERAGE}x")
    print(f"Max per channel: {COPY_MAX_OPEN}")
    print(f"Telethon: {'configured' if TG_SESSION else 'NOT CONFIGURED'}")

    load_copy_history()

    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()
    print(f"Dashboard: port {os.environ.get('PORT', 8081)}")

    start_telethon()

    tg_send(
        "<b>📡 APEX Copy Signal Lab — Online!</b>\n\n"
        f"Monitoring: <b>{', '.join(channel_label(c) for c in COPY_CHANNELS)}</b>\n"
        f"Trade size: <b>${COPY_TRADE_SIZE:.0f} × {COPY_LEVERAGE}x</b>\n"
        f"Max trades/channel: <b>{COPY_MAX_OPEN}</b>\n\n"
        f"<b>Balances:</b>\n"
        + "\n".join(
            f"  {channel_label(ch)}: ${copy_balances[ch]:.2f}"
            for ch in copy_balances
        ) +
        f"\n\n<b>Commands:</b> /copyreport /copypause /copyresume /copychannels\n\n"
        f"<i>Paper only. No real money at risk.</i>"
    )

    offset = None
    while True:
        try:
            offset = check_copy_commands(offset)
            monitor_copy_positions()
            check_pending_signals()
            time.sleep(10)
        except KeyboardInterrupt:
            print("\nCopy Lab stopped.")
            break
        except Exception as e:
            print(f"Copy Lab error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    run()