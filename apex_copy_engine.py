"""
APEX Copy Signal Lab — Phase 2.5
==================================
Monitors GGShot and Coin_Signals Telegram channels.
Copies valid signals as paper trades immediately at live market price.
Completely separate from APEX own signal engine.

KEY RULES:
- Paper trading only. No real orders ever.
- $100 margin x 10x leverage = $1000 exposure per trade
- Entry = live market price at signal time (ignore entry zone)
- Uses signal TP1-TP4 and Stop Loss exactly
- 25% close at each TP, SL moves to breakeven after TP1
- Separate balances, positions, stats from APEX
- APEX blocked coins DO NOT apply here
- APEX RSI/EMA/MACD filters DO NOT apply here

Railway env vars:
  TELEGRAM_TOKEN
  TELEGRAM_CHAT_ID
  TELEGRAM_API_ID
  TELEGRAM_API_HASH
  TELETHON_SESSION
  COPY_DATA_DIR   (default: /app)
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

# ─────────────────────────── CONFIG ───────────────────────────
COPY_TRADE_SIZE   = 100.0    # $100 margin per copied trade
COPY_LEVERAGE     = 10       # 10x → $1000 exposure
COPY_MAX_OPEN     = 3        # max open per channel
COPY_SLIPPAGE_PCT = 0.001    # 0.1% slippage simulation

# Per-channel starting balances
CHANNEL_BALANCES = {
    "ggshot":       500.0,
    "coin_signals": 500.0,
}

# Telegram channel usernames — exact as they appear on Telegram
COPY_CHANNELS = {
    "ggshot":       "ggshot",
    "coin_signals": "Coin_Signals",
}

# ─────────────────────────── ENV VARS ─────────────────────────
TG_TOKEN   = os.environ.get("TELEGRAM_TOKEN",    "").strip()
TG_CHAT    = os.environ.get("TELEGRAM_CHAT_ID",  "").strip()
TG_API_ID  = os.environ.get("TELEGRAM_API_ID",   "").strip()
TG_API_HASH= os.environ.get("TELEGRAM_API_HASH", "").strip()
TG_SESSION = os.environ.get("TELETHON_SESSION",  "").strip()

# ─────────────────────────── DATA PATHS ───────────────────────
DATA_DIR  = Path(os.environ.get("COPY_DATA_DIR", "/app"))
COPY_JSON = DATA_DIR / "copy_trades.json"
COPY_CSV  = DATA_DIR / "copy_trades.csv"

CSV_COLUMNS = [
    "source_channel", "symbol", "direction",
    "exec_price", "close_price", "close_reason",
    "tp_hit_count", "pnl_usdt", "opened_at", "closed_at", "duration_minutes",
]

# ─────────────────────────── STATE ────────────────────────────
state_lock = threading.RLock()

# Per-channel open positions
copy_positions = {ch: {} for ch in CHANNEL_BALANCES}

# Per-channel balances (mutable)
copy_balances = dict(CHANNEL_BALANCES)

# Per-channel closed trade history
copy_closed = {ch: [] for ch in CHANNEL_BALANCES}

# Per-channel stats
copy_stats = {
    ch: {"total": 0, "wins": 0, "tp_hit": 0, "sl_hit": 0,
         "profit": 0.0, "loss": 0.0}
    for ch in CHANNEL_BALANCES
}

# Debug log — last 20 signal events
signal_log  = []
MAX_LOG     = 20
copy_paused = False

session_http = requests.Session()


# ─────────────────────────── HELPERS ──────────────────────────
def utc_now_str():
    return datetime.now(timezone.utc).strftime("%H:%M UTC")


def fmt_p(price):
    if price is None:
        return "N/A"
    if price >= 1000:   return f"${price:.2f}"
    if price >= 100:    return f"${price:.3f}"
    if price >= 1:      return f"${price:.4f}"
    if price >= 0.01:   return f"${price:.5f}"
    return f"${price:.8f}"


def channel_label(ch):
    return {"ggshot": "GGShot", "coin_signals": "Coin_Signals"}.get(ch, ch)


def log_event(status, symbol, channel, reason=""):
    """Record signal event for /copydebug."""
    global signal_log
    entry = {
        "time":    utc_now_str(),
        "status":  status,
        "symbol":  symbol or "?",
        "channel": channel_label(channel) if channel else "?",
        "reason":  reason,
    }
    signal_log.append(entry)
    if len(signal_log) > MAX_LOG:
        signal_log = signal_log[-MAX_LOG:]
    icon = {"EXECUTED": "✅", "REJECTED": "❌", "PARSED_OK": "🔍"}.get(status, "•")
    print(f"  [{status}] {channel}/{symbol} — {reason}")


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
    """Get live price from Binance."""
    global _price_cache, _price_cache_t
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


# ─────────────────────────── PARSER ───────────────────────────
def _normalize_symbol(raw):
    """Normalize any symbol to XXXUSDT."""
    raw = raw.upper().strip().lstrip("#$")
    raw = raw.replace("/USDT", "").replace("USDT", "").strip().rstrip("/")
    return raw + "USDT" if raw else None


# Words that look like symbols but aren't
_SYM_SKIP = {
    "TP", "SL", "BUY", "SELL", "LONG", "SHORT", "ENTRY", "USDT", "USD",
    "THE", "FOR", "AND", "STOP", "TAKE", "TARGET", "PROFIT", "LOSS",
    "BINANCE", "BYBIT", "MID", "TERM", "CROSS", "LEVERAGE", "ID",
    "LAST", "SIGNALS", "STRATEGY", "ACCURACY", "SIGNAL", "DETAILS",
    "TREND", "LINE", "AFTER", "FIRST", "REST", "ZONE", "TERM",
}

# Phrases that definitely aren't trading signals
_NOISE = [
    "TAKE-PROFIT TARGET", "TP HIT", "PROFIT:", "BOOM", "CONGRAT",
    "JUST IN:", "BREAKING", "FED ", "ALL-TIME HIGH", "BACK TO BACK",
    "VIP CHANNEL", "SUBSCRIBE", "JOIN US", "CONTACT:", "FLYING",
]


def parse_signal(text):
    """
    Parse GGShot and Coin_Signals formats.
    Returns dict or None.

    Valid signal needs: symbol + direction + SL + at least TP1
    Entry zone is parsed but NOT used for execution.
    Bot always enters at live market price.
    """
    if not text or len(text) < 15:
        return None

    t = text.upper()

    # ── Noise filter ──
    for phrase in _NOISE:
        if phrase in t:
            return None

    # ── Normalise special chars ──
    t = (t.replace("\u00d7", "X").replace("\u2715", "X")
          .replace("\u2019", "'").replace("\u2018", "'")
          .replace("\u2013", "-").replace("\u2014", "-"))

    # ── Direction ──
    direction = None
    if re.search(r"#?LONG\b|#?BUY\b|\bGO\s+LONG", t):
        direction = "BUY"
    elif re.search(r"#?SHORT\b|#?SELL\b|\bGO\s+SHORT", t):
        direction = "SELL"
    if not direction:
        return None

    # ── Symbol ──
    symbol = None
    sym_patterns = [
        r"[#$]([A-Z]{2,12})[/\s]?USDT",  # #SOLUSDT #SOL/USDT
        r"\b([A-Z]{2,12})USDT\b",          # SOLUSDT
        r"\b([A-Z]{2,12})/USDT\b",         # SOL/USDT
        r"[#$]([A-Z]{2,12})\b",            # #SOL $SOL
    ]
    for pat in sym_patterns:
        for m in re.finditer(pat, t):
            raw = m.group(1).strip()
            if raw not in _SYM_SKIP and len(raw) >= 2 and not raw.isdigit():
                symbol = _normalize_symbol(raw)
                break
        if symbol:
            break
    if not symbol:
        return None

    # ── Stop Loss (required) ──
    sl = None
    for pat in [
        r"(?:STOP[-\s]?LOSS|STOPLOSS|STOP)[:\s]+([0-9]+\.[0-9]+)",
        r"\bSL[:\s]+([0-9]+\.[0-9]+)",
    ]:
        m = re.search(pat, t)
        if m:
            try:
                sl = float(m.group(1))
                break
            except Exception:
                pass
    if not sl:
        return None

    # ── Take Profits (need at least 1) ──
    tps = []
    seen_vals = set()
    for pat in [
        r"(?:TARGET|TAKE.?PROFIT|TP)\s*\d\s*[:\s]+([0-9]+\.[0-9]+)",
        r"(?:TARGET|TP)[:\s]+([0-9]+\.[0-9]+)",
    ]:
        for m in re.finditer(pat, t):
            # Skip TREND-LINE false positives
            before = t[max(0, m.start() - 12):m.start()]
            if "TREND" in before:
                continue
            try:
                val = float(m.group(1))
                if val > 0 and val not in seen_vals:
                    seen_vals.add(val)
                    tps.append(val)
            except Exception:
                pass
    if not tps:
        return None

    # ── Sort TPs correctly ──
    tps = sorted(tps) if direction == "BUY" else sorted(tps, reverse=True)

    # ── Pad to 4 TPs ──
    while len(tps) < 4:
        if len(tps) >= 2:
            diff = abs(tps[-1] - tps[-2])
        else:
            diff = abs(tps[0] * 0.01)
        nxt = tps[-1] + diff if direction == "BUY" else tps[-1] - diff
        tps.append(round(nxt, 8))

    # ── Validate SL vs TP direction logic ──
    # Use TP1 as reference since we enter at market price
    tp1 = tps[0]
    if direction == "BUY":
        if sl >= tp1:   return None  # SL above TP = invalid
    else:
        if sl <= tp1:   return None  # SL below TP = invalid

    return {
        "symbol":    symbol,
        "direction": direction,
        "tp1":       tps[0],
        "tp2":       tps[1],
        "tp3":       tps[2],
        "tp4":       tps[3],
        "sl":        sl,
    }


# ─────────────────────────── EXECUTE ──────────────────────────
def copy_execute(channel, sig, price, msg_id):
    """
    Open a paper copy trade at current live market price.
    Ignores entry zone — enters immediately.
    """
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
        if sym in copy_positions[channel]:
            log_event("REJECTED", sym, channel, "already open")
            return False
        if copy_balances[channel] < COPY_TRADE_SIZE:
            log_event("REJECTED", sym, channel, "balance too low")
            return False
        if len(copy_positions[channel]) >= COPY_MAX_OPEN:
            log_event("REJECTED", sym, channel, f"max {COPY_MAX_OPEN} open")
            return False

        copy_balances[channel] -= COPY_TRADE_SIZE
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
        bal_now = copy_balances[channel]

    label = channel_label(channel)
    side  = "LONG" if direction == "BUY" else "SHORT"
    tg_send(
        f"<b>📥 COPY TRADE OPENED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Source: <b>{label}</b>  |  <b>{sym} {side}</b>\n"
        f"Entry:  {fmt_p(exec_price)}\n"
        f"Margin: ${COPY_TRADE_SIZE:.0f}  |  {COPY_LEVERAGE}x  |  "
        f"Exposure: ${notional:.0f}\n\n"
        f"TP1: {fmt_p(sig['tp1'])}\n"
        f"TP2: {fmt_p(sig['tp2'])}\n"
        f"TP3: {fmt_p(sig['tp3'])}\n"
        f"TP4: {fmt_p(sig['tp4'])}\n"
        f"SL:  {fmt_p(sig['sl'])}\n"
        f"Liq: {fmt_p(liq_price)}\n\n"
        f"Balance [{label}]: ${bal_now:.2f}"
    )
    log_event("EXECUTED", sym, channel, f"@ {exec_price}")
    print(f"  📥 Copy opened: {channel}/{sym} {direction} @ {exec_price}")
    return True


# ─────────────────────────── MONITOR ──────────────────────────
def monitor_copy_positions():
    """Check all open copy positions for TP/SL hits every cycle."""
    for channel in list(copy_positions.keys()):
        to_remove     = []
        notifications = []

        for sym, pos in list(copy_positions[channel].items()):
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
                rem_pct    = 1.0 - (tp_hit * 0.25)

                # ── Live unrealized P&L ──
                if direction == "BUY":
                    move_pct = (price - exec_price) / exec_price * 100
                else:
                    move_pct = (exec_price - price) / exec_price * 100
                pos["unrealized_pnl"] = round(
                    margin * COPY_LEVERAGE * move_pct / 100 * rem_pct, 2)

                # ── SL / Liquidation check (SL first) ──
                sl_hit  = (direction == "BUY"  and price <= sl) or \
                          (direction == "SELL" and price >= sl)
                liq_hit = (direction == "BUY"  and price <= liq_price) or \
                          (direction == "SELL" and price >= liq_price)

                if sl_hit or liq_hit:
                    close_price = sl
                    price_move  = abs(close_price - exec_price) / exec_price * 100
                    is_profit   = (direction == "BUY"  and close_price >= exec_price) or \
                                  (direction == "SELL" and close_price <= exec_price)
                    sl_pnl      = round(margin * COPY_LEVERAGE * price_move / 100 * rem_pct, 2)
                    prior       = pos.get("currentPnl", 0)
                    total_pnl   = round(prior + (sl_pnl if is_profit else -sl_pnl), 2)
                    remaining   = margin * rem_pct

                    if is_profit:
                        copy_balances[channel]        += remaining + sl_pnl
                        copy_stats[channel]["profit"] += sl_pnl
                    else:
                        copy_balances[channel]       += max(0, remaining - sl_pnl)
                        copy_stats[channel]["loss"]  += sl_pnl

                    copy_stats[channel]["total"]  += 1
                    copy_stats[channel]["sl_hit"] += 1
                    if is_profit or pos.get("breakeven"):
                        copy_stats[channel]["wins"] += 1

                    close_reason = "SL" if sl_hit else "GAP_SL"
                    _save_copy_trade(channel, sym, pos, close_price,
                                     close_reason, total_pnl)
                    label = channel_label(channel)
                    sign  = "+" if total_pnl >= 0 else ""
                    mins  = int(elapsed // 60)
                    notifications.append(
                        f"<b>📊 COPY CLOSED — {sym}</b>\n"
                        f"Source: {label}  |  {close_reason}\n"
                        f"Time: {mins}m\n"
                        f"Result: {sign}${total_pnl:.2f} USDT\n"
                        f"Balance [{label}]: ${copy_balances[channel]:.2f}"
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
                    tp_num   = tp_hit + 1
                    pnl_pct  = abs(next_tp - exec_price) / exec_price * 100
                    tp_pnl   = round(margin * COPY_LEVERAGE * pnl_pct / 100 * 0.25, 2)

                    copy_balances[channel]        += margin * 0.25 + tp_pnl
                    copy_stats[channel]["profit"] += tp_pnl
                    copy_stats[channel]["tp_hit"] += 1
                    pos["currentPnl"]              = round(pos.get("currentPnl", 0) + tp_pnl, 2)
                    pos["tp_hit"]                  = tp_num

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
                        f"Trade so far: +${pos['currentPnl']:.2f}\n"
                        f"{'🔒 SL moved to breakeven' if tp_num == 1 else f'SL trailed to TP{tp_num-1}'}"
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


# ─────────────────────────── PERSISTENCE ──────────────────────
def _ensure_dirs():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _save_copy_trade(channel, sym, pos, close_price, close_reason, pnl_usdt):
    """Save closed trade to JSON and CSV."""
    _ensure_dirs()
    opened_ts = pos.get("opened_at", time.time())
    closed_ts = time.time()
    rec = {
        "source_channel":   channel,
        "symbol":           sym,
        "direction":        pos.get("direction", ""),
        "exec_price":       pos.get("exec_price", 0),
        "close_price":      close_price,
        "close_reason":     close_reason,
        "tp_hit_count":     pos.get("tp_hit", 0),
        "pnl_usdt":         round(pnl_usdt, 2),
        "opened_at":        datetime.fromtimestamp(opened_ts, tz=timezone.utc).isoformat(),
        "closed_at":        datetime.fromtimestamp(closed_ts, tz=timezone.utc).isoformat(),
        "duration_minutes": round((closed_ts - opened_ts) / 60, 1),
    }
    try:
        existing = json.loads(COPY_JSON.read_text()) if COPY_JSON.exists() else []
        existing.append(rec)
        COPY_JSON.write_text(json.dumps(existing, indent=2))
    except Exception as e:
        print(f"  ⚠️ JSON save failed: {e}")
    try:
        write_header = not COPY_CSV.exists()
        with open(COPY_CSV, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            if write_header:
                w.writeheader()
            w.writerow(rec)
    except Exception as e:
        print(f"  ⚠️ CSV save failed: {e}")
    copy_closed[channel].append(rec)


def load_copy_history():
    """Reload closed trades and restore balances on restart."""
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
            copy_stats[ch]["tp_hit"] += rec.get("tp_hit_count", 0)
            if pnl > 0:
                copy_stats[ch]["profit"] += pnl
                copy_stats[ch]["wins"]   += 1
            else:
                copy_stats[ch]["loss"]   += abs(pnl)
            if cr in ("SL", "GAP_SL"):
                copy_stats[ch]["sl_hit"] += 1
        # Restore balances from history
        for ch in copy_balances:
            hist_net = sum(r["pnl_usdt"] for r in copy_closed[ch])
            copy_balances[ch] = round(CHANNEL_BALANCES[ch] + hist_net, 2)
        print(f"  📂 Loaded {len(saved)} copy trades")
        tg_send(f"<b>📂 Copy history loaded</b>\n{len(saved)} trades restored.")
    except Exception as e:
        print(f"  ⚠️ Load history failed: {e}")


# ─────────────────────────── COMMANDS ─────────────────────────
def _cmd_copyreport():
    with state_lock:
        lines = []
        for ch in CHANNEL_BALANCES:
            s    = copy_stats[ch]
            wr   = round(s["wins"] / s["total"] * 100, 1) if s["total"] else 0
            net  = round(s["profit"] - s["loss"], 2)
            sign = "+" if net >= 0 else ""
            open_n = len(copy_positions[ch])
            lines.append(
                f"<b>{channel_label(ch)}</b>\n"
                f"  Balance: ${copy_balances[ch]:.2f}  |  Open: {open_n}\n"
                f"  Trades: {s['total']}  WR: {wr}%  Net: {sign}${net:.2f}\n"
                f"  TPs hit: {s['tp_hit']}  SLs: {s['sl_hit']}"
            )
        total_closed = sum(s["total"] for s in copy_stats.values())
        total_net    = sum(
            round(copy_stats[ch]["profit"] - copy_stats[ch]["loss"], 2)
            for ch in copy_stats
        )
        paused_str = "\n⚠️ Copy engine PAUSED" if copy_paused else ""

    return (
        f"<b>📊 COPY SIGNAL LAB — {utc_now_str()}</b>\n"
        f"══════════════════════════════\n"
        f"Total closed: {total_closed}  |  "
        f"Combined net: {'+'if total_net>=0 else ''}${total_net:.2f}\n"
        f"Trade size: ${COPY_TRADE_SIZE:.0f} × {COPY_LEVERAGE}x\n"
        f"{paused_str}\n\n"
        + "\n\n".join(lines)
    )


def _cmd_copydebug():
    if not signal_log:
        return "<b>📋 COPY DEBUG</b>\n\nNo signals received yet."
    icons = {
        "EXECUTED": "✅", "REJECTED": "❌",
        "PARSED_OK": "🔍", "EXPIRED": "⏰",
    }
    lines = []
    for e in reversed(signal_log[-10:]):
        icon = icons.get(e["status"], "•")
        lines.append(
            f"{icon} <b>{e['status']}</b> — {e['channel']}/{e['symbol']}\n"
            f"   {e['time']}  {e['reason']}"
        )
    return (
        f"<b>📋 COPY DEBUG — last {len(lines)} events</b>\n"
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
            elif text == "/copydebug":
                tg_send(_cmd_copydebug())
            elif text == "/copypause":
                copy_paused = True
                tg_send("<b>⏸ Copy engine paused.</b>\nSend /copyresume to restart.")
            elif text == "/copyresume":
                copy_paused = False
                tg_send("<b>▶️ Copy engine resumed.</b>")
            elif text == "/copyhelp":
                tg_send(
                    "<b>📡 Copy Lab Commands</b>\n\n"
                    "/copyreport  — Full report\n"
                    "/copydebug   — Last 10 signal events\n"
                    "/copypause   — Pause new copies\n"
                    "/copyresume  — Resume\n"
                    "/copyhelp    — This message"
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
                    "exec":          pos["exec_price"],
                    "tp_hit":        pos["tp_hit"],
                    "breakeven":     pos["breakeven"],
                    "unrealized":    pos.get("unrealized_pnl", 0),
                    "realized":      pos.get("currentPnl", 0),
                }
        payload = {
            "balances":   copy_balances,
            "stats":      copy_stats,
            "openTrades": open_pos,
            "timestamp":  utc_now_str(),
        }
    resp = jsonify(payload)
    resp.headers.add("Access-Control-Allow-Origin", "*")
    return resp


def start_flask():
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    port = int(os.environ.get("PORT", 8081))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ─────────────────────────── TELETHON ─────────────────────────
def start_telethon():
    """Monitor signal channels via Telethon user API."""
    if not TG_API_ID or not TG_API_HASH or not TG_SESSION:
        print("  ⚠️ Telethon not configured")
        tg_send(
            "<b>⚠️ Copy Lab — Telethon not configured</b>\n"
            "Set: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELETHON_SESSION"
        )
        return

    try:
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession
    except ImportError:
        print("  ⚠️ telethon not installed")
        tg_send("<b>⚠️ Copy Lab</b> — run: pip install telethon")
        return

    async def _run():
        client = TelegramClient(
            StringSession(TG_SESSION), int(TG_API_ID), TG_API_HASH)
        await client.start()
        print("  ✅ Telethon connected")

        channel_list = list(COPY_CHANNELS.values())

        @client.on(events.NewMessage(chats=channel_list))
        async def handler(event):
            text   = event.message.message or ""
            msg_id = event.message.id
            chat   = event.chat

            # Identify channel
            channel_key = None
            chat_user   = (getattr(chat, "username", "") or "").lower()
            for key, username in COPY_CHANNELS.items():
                if username.lower() in chat_user:
                    channel_key = key
                    break
            if not channel_key:
                return

            print(f"  📨 {channel_key}: {text[:60].replace(chr(10),' ')}")

            if copy_paused:
                return

            sig = parse_signal(text)
            if not sig:
                # Log rejection reason
                t = text.upper()
                if not re.search(r"#?LONG\b|#?BUY\b|#?SHORT\b|#?SELL\b", t):
                    reason = "no direction"
                elif not any(c.isdigit() for c in t):
                    reason = "no numbers"
                elif not re.search(r"STOP|STOPLOSS|SL\b", t):
                    reason = "no SL"
                elif not re.search(r"TARGET|TP\d|TAKE.?PROFIT", t):
                    reason = "no TPs"
                else:
                    reason = "invalid format"
                log_event("REJECTED", None, channel_key, reason)
                return

            sym = sig["symbol"]
            log_event("PARSED_OK", sym, channel_key,
                      f"{sig['direction']} SL={sig['sl']}")

            # Check not already open
            with state_lock:
                if sym in copy_positions[channel_key]:
                    log_event("REJECTED", sym, channel_key, "already open")
                    return
                if len(copy_positions[channel_key]) >= COPY_MAX_OPEN:
                    log_event("REJECTED", sym, channel_key,
                              f"max {COPY_MAX_OPEN} open")
                    return

            # Get live price and enter immediately
            price = get_price(sym)
            if not price:
                log_event("REJECTED", sym, channel_key, "no price from Binance")
                return

            copy_execute(channel_key, sig, price, msg_id)

        await client.run_until_disconnected()

    def _thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run())
        except Exception as e:
            print(f"  ❌ Telethon error: {e}")
            tg_send(f"<b>❌ Copy Lab crashed</b>\n{e}")

    threading.Thread(target=_thread, daemon=True).start()


# ─────────────────────────── MAIN ─────────────────────────────
def run():
    print("=" * 55)
    print("  APEX COPY SIGNAL LAB — Phase 2.5")
    print("=" * 55)
    print(f"  Channels: {list(COPY_CHANNELS.keys())}")
    print(f"  Trade:    ${COPY_TRADE_SIZE} x {COPY_LEVERAGE}x = ${COPY_TRADE_SIZE*COPY_LEVERAGE} exposure")
    print(f"  Entry:    LIVE MARKET PRICE (ignores entry zone)")
    print(f"  Telethon: {'configured' if TG_SESSION else 'NOT CONFIGURED'}")

    load_copy_history()

    threading.Thread(target=start_flask, daemon=True).start()
    print(f"  Dashboard: port {os.environ.get('PORT', 8081)}")

    start_telethon()

    tg_send(
        "<b>📡 APEX Copy Signal Lab — Online!</b>\n\n"
        f"Monitoring: <b>GGShot + Coin_Signals</b>\n"
        f"Trade size: <b>${COPY_TRADE_SIZE:.0f} x {COPY_LEVERAGE}x</b> = "
        f"<b>${COPY_TRADE_SIZE*COPY_LEVERAGE:.0f} exposure</b>\n"
        f"Entry: <b>Live market price</b> (no zone waiting)\n"
        f"Max per channel: <b>{COPY_MAX_OPEN}</b>\n\n"
        f"<b>Balances:</b>\n"
        + "\n".join(f"  {channel_label(ch)}: ${copy_balances[ch]:.2f}"
                    for ch in copy_balances) +
        f"\n\n<b>Commands:</b> /copyreport /copydebug /copypause /copyresume\n\n"
        f"<i>Paper only. No real money at risk.</i>"
    )

    offset = None
    while True:
        try:
            offset = check_copy_commands(offset)
            monitor_copy_positions()
            time.sleep(10)
        except KeyboardInterrupt:
            print("\nCopy Lab stopped.")
            break
        except Exception as e:
            print(f"  Copy Lab error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    run()