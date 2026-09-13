"""
Constellation paper-trading bot.

PAPER MONEY ONLY. Alpaca paper keys physically cannot place live orders.

What makes a buy happen, in order:

  PRICE SIGNALS (the "numbers" layer)
    - Momentum: up at least X% today
    - Trend: price above its own 20-day average
    - Volume: today's volume above its recent average

  CONTEXT SIGNALS (the "understanding" layer — see market_context.py)
    - Market regime: is the whole market healthy, mixed, or falling
    - News: is there an actual catalyst behind the move, or nothing
    - Earnings: never open a position right before a company reports
    - Fundamentals: is the company obviously deteriorating

Position size is then scaled by a confidence score built from those context
signals — a setup where everything agrees gets a bigger position than a
borderline one. That's meaningfully different from betting the same amount
on every signal regardless of quality.

Every run also logs what 3 SHADOW STRATEGIES would have done without trading
them. Over months that produces real comparative evidence about whether the
live rules are actually the best ones, far faster than redesigning and
re-testing one at a time.
"""

import os
import json
import datetime
from zoneinfo import ZoneInfo
import requests

import market_context

# ---------- CONFIG ----------
ALPACA_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET = os.environ["ALPACA_SECRET_KEY"]
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL")

TRADING_BASE = "https://paper-api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"
HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}

HERE = os.path.dirname(__file__)
with open(os.path.join(HERE, "watchlist.json")) as f:
    WATCHLIST = json.load(f)["tickers"]

# ---------- STRATEGY STATE ----------
STATE_PATH = os.path.join(HERE, "strategy_state.json")
DEFAULT_GLOBAL = {
    "buy_momentum_pct": 3.0,
    "stop_loss_pct": -5.0,
    "take_profit_pct": 15.0,
    "position_size_usd": 500,
    "ma_period": 20,
    "volume_multiplier": 1.2,
}
DEFAULT_STATE = {
    "global": DEFAULT_GLOBAL,
    "per_symbol": {},
    "updated": None,
    "last_review_summary": "No review has run yet — using defaults.",
    "last_backtest_summary": "No backtest has run yet.",
}

def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                raw = json.load(f)
            state = dict(DEFAULT_STATE)
            state.update(raw)
            state["global"] = {**DEFAULT_GLOBAL, **raw.get("global", {})}
            state["per_symbol"] = raw.get("per_symbol", {})
            return state
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_STATE))

STATE = load_state()

def params_for(symbol):
    p = dict(STATE["global"])
    overrides = dict(STATE["per_symbol"].get(symbol, {}))
    overrides.pop("source", None)   # bookkeeping field, not a strategy parameter
    p.update(overrides)
    return p

BASE_POSITION_USD = STATE["global"]["position_size_usd"]

LOG_DIR = os.path.join(HERE, "logs")
TODAY = datetime.datetime.now(ZoneInfo("America/New_York")).date().isoformat()
LOG_PATH = os.path.join(LOG_DIR, f"{TODAY}.md")
SHADOW_PATH = os.path.join(HERE, "shadow_results.json")

# Alternative rule sets, logged but never traded. Each answers a specific
# "what if" that would otherwise take months of live trading to test.
SHADOW_STRATEGIES = {
    "aggressive": {"buy_momentum_pct": 1.5, "stop_loss_pct": -8.0, "take_profit_pct": 20.0,
                   "note": "looser entry, wider stops — does catching more setups beat the extra losers?"},
    "conservative": {"buy_momentum_pct": 5.0, "stop_loss_pct": -3.0, "take_profit_pct": 10.0,
                     "note": "stricter entry, tight stops — does being picky and quick beat the live rules?"},
    "no_context": {"buy_momentum_pct": 3.0, "stop_loss_pct": -5.0, "take_profit_pct": 15.0,
                   "ignore_context": True,
                   "note": "the old price-only strategy — proves whether the context layer actually helps"},
}


# ---------- ALPACA HELPERS ----------
def get_account():
    r = requests.get(f"{TRADING_BASE}/v2/account", headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def get_positions():
    r = requests.get(f"{TRADING_BASE}/v2/positions", headers=HEADERS, timeout=20)
    r.raise_for_status()
    return {p["symbol"]: p for p in r.json()}


def get_snapshot(symbol):
    r = requests.get(
        f"{DATA_BASE}/v2/stocks/{symbol}/snapshot",
        headers=HEADERS, params={"feed": "iex"}, timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    daily = data.get("dailyBar") or {}
    prev = data.get("prevDailyBar") or {}
    price = daily.get("c") or (data.get("latestTrade") or {}).get("p")
    prev_close = prev.get("c")
    volume = daily.get("v")
    if not price or not prev_close:
        return None, None, None
    return price, (price - prev_close) / prev_close * 100, volume


def get_signal_context(symbol, ma_period):
    r = requests.get(
        f"{DATA_BASE}/v2/stocks/{symbol}/bars",
        headers=HEADERS,
        params={"timeframe": "1Day", "limit": ma_period + 5, "feed": "iex"},
        timeout=20,
    )
    r.raise_for_status()
    bars = r.json().get("bars", [])
    if len(bars) < ma_period:
        return None, None
    closes = [b["c"] for b in bars[-ma_period:]]
    volumes = [b["v"] for b in bars[-ma_period:]]
    return sum(closes)/len(closes), sum(volumes)/len(volumes)


def place_order(symbol, qty, side):
    order = {"symbol": symbol, "qty": str(qty), "side": side,
             "type": "market", "time_in_force": "day"}
    r = requests.post(f"{TRADING_BASE}/v2/orders", headers=HEADERS, json=order, timeout=20)
    r.raise_for_status()
    return r.json()


# ---------- SHADOW TRACKING ----------
def load_shadows():
    if os.path.exists(SHADOW_PATH):
        try:
            with open(SHADOW_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {name: {"open": {}, "closed": [], "note": cfg["note"]}
            for name, cfg in SHADOW_STRATEGIES.items()}


def run_shadows(shadows, market_data, regime, context_by_symbol):
    """Simulates each alternate rule set against the same day's real data.
    No orders are placed — this only records what each would have done."""
    lines = []
    for name, cfg in SHADOW_STRATEGIES.items():
        book = shadows.setdefault(name, {"open": {}, "closed": [], "note": cfg["note"]})
        opened = closed = 0

        for symbol, d in market_data.items():
            price, pct_change, volume, sma, avg_vol = d
            if price is None:
                continue

            held = book["open"].get(symbol)
            if held:
                pl = (price - held["entry"]) / held["entry"] * 100
                if pl <= cfg["stop_loss_pct"] or pl >= cfg["take_profit_pct"]:
                    book["closed"].append({"symbol": symbol, "pct": round(pl, 2), "closed": TODAY})
                    del book["open"][symbol]
                    closed += 1
                continue

            if pct_change is None or sma is None:
                continue
            price_ok = (pct_change >= cfg["buy_momentum_pct"]
                        and price > sma
                        and (volume / avg_vol if avg_vol else 0) >= 1.2)
            if not price_ok:
                continue

            # the no_context shadow deliberately ignores the understanding layer
            if not cfg.get("ignore_context"):
                ctx = context_by_symbol.get(symbol)
                if ctx and not ctx["allow"]:
                    continue

            book["open"][symbol] = {"entry": price, "opened": TODAY}
            opened += 1

        n_closed = len(book["closed"])
        if n_closed:
            wins = len([t for t in book["closed"] if t["pct"] > 0])
            total = 1.0
            for t in book["closed"]:
                total *= (1 + t["pct"]/100)
            lines.append(f"- **{name}**: {n_closed} closed, {wins/n_closed*100:.0f}% win rate, "
                         f"{(total-1)*100:+.1f}% simulated return ({len(book['open'])} open)")
        else:
            lines.append(f"- **{name}**: {len(book['open'])} open, none closed yet")
        if opened or closed:
            lines[-1] += f" · today: +{opened} opened, {closed} closed"
    return lines


# ---------- STRATEGY ----------
def decide_and_trade():
    positions = get_positions()
    lines, alerts = [], []
    market_data = {}
    context_by_symbol = {}

    regime = market_context.detect_regime()
    lines.append(f"**Market regime: {regime['regime']}** — {regime['detail']}")
    lines.append("")

    for symbol in WATCHLIST:
        p = params_for(symbol)
        try:
            price, pct_change, volume = get_snapshot(symbol)
        except Exception as e:
            lines.append(f"- **{symbol}**: could not fetch data ({e})")
            continue
        if price is None:
            lines.append(f"- **{symbol}**: no data available today")
            continue

        try:
            sma, avg_vol = get_signal_context(symbol, p["ma_period"])
        except Exception:
            sma, avg_vol = None, None
        market_data[symbol] = (price, pct_change, volume, sma, avg_vol)

        held = positions.get(symbol)
        if held:
            entry = float(held["avg_entry_price"])
            qty = held["qty"]
            pl_pct = (price - entry) / entry * 100
            if pl_pct <= p["stop_loss_pct"]:
                place_order(symbol, qty, "sell")
                lines.append(f"- 🔴 **SOLD {symbol}** — down {pl_pct:.1f}% (stop loss at {p['stop_loss_pct']}%)")
                alerts.append(f"📉 **{symbol}** hit stop loss, down {pl_pct:.1f}% — sold.")
            elif pl_pct >= p["take_profit_pct"]:
                place_order(symbol, qty, "sell")
                lines.append(f"- 🟢 **SOLD {symbol}** — up {pl_pct:.1f}% (take-profit at {p['take_profit_pct']}%)")
                alerts.append(f"🚀 **{symbol}** hit take-profit, up {pl_pct:.1f}% — sold.")
            else:
                lines.append(f"- ⚪ **HOLD {symbol}** — {pl_pct:+.1f}% since entry")
            continue

        # --- price signals first (cheap, no API cost) ---
        if pct_change is None or pct_change < p["buy_momentum_pct"]:
            chg = f"{pct_change:+.1f}%" if pct_change is not None else "n/a"
            lines.append(f"- ⚪ **{symbol}** — {chg} today, below the +{p['buy_momentum_pct']}% bar")
            continue
        if sma is None:
            lines.append(f"- ⚪ **{symbol}** — momentum signal but not enough history for the trend filter")
            continue
        if price <= sma:
            lines.append(f"- ⚪ **{symbol}** — up {pct_change:.1f}% but below its {p['ma_period']}-day average")
            continue
        vol_ratio = (volume / avg_vol) if (volume and avg_vol) else 0
        if vol_ratio < p["volume_multiplier"]:
            lines.append(f"- ⚪ **{symbol}** — up {pct_change:.1f}% but volume only {vol_ratio:.1f}x normal")
            continue

        # --- price signals passed, now check the context layer ---
        ctx = market_context.evaluate(symbol, regime)
        context_by_symbol[symbol] = ctx

        if not ctx["allow"]:
            reason = ctx["reasons"][0] if ctx["reasons"] else "context check failed"
            lines.append(f"- 🚫 **{symbol}** — price signals passed but skipped: {reason}")
            continue

        # --- confidence-weighted sizing ---
        size_usd = BASE_POSITION_USD * ctx["confidence"]
        qty = max(1, int(size_usd / price))
        try:
            place_order(symbol, qty, "buy")
            lines.append(
                f"- 🟢 **BOUGHT {symbol}** — up {pct_change:.1f}%, {vol_ratio:.1f}x volume, "
                f"confidence {ctx['confidence']}x → {qty} sh @ ~${price:.2f}"
            )
            for reason in ctx["reasons"]:
                lines.append(f"    ↳ {reason}")
            alerts.append(f"🚀 Bought **{symbol}** at {ctx['confidence']}x confidence — {pct_change:.1f}% move with context confirmed.")
        except Exception as e:
            lines.append(f"- ⚠️ Wanted to buy {symbol} but the order failed: {e}")

    return lines, alerts, regime, market_data, context_by_symbol


def build_report(lines, alerts, account, positions, shadow_lines, regime):
    equity = float(account["equity"])
    cash = float(account["cash"])
    pl_today = equity - float(account.get("last_equity", account["equity"]))
    g = STATE["global"]

    parts = [
        f"# Constellation daily report — {TODAY}",
        "",
        f"**Equity:** ${equity:,.2f} | **Cash:** ${cash:,.2f} | **Today:** {pl_today:+,.2f}",
        f"**Base strategy:** +{g['buy_momentum_pct']}% momentum, above {g['ma_period']}-day avg, "
        f"{g['volume_multiplier']}x volume · stop {g['stop_loss_pct']}% · target +{g['take_profit_pct']}%",
        "",
        "## Positions" if positions else "## Positions\n_No open positions._",
    ]
    for sym, p in positions.items():
        parts.append(f"- {sym}: {p['qty']} sh, entry ${float(p['avg_entry_price']):.2f}, "
                     f"P/L {float(p['unrealized_plpc'])*100:+.1f}%")

    parts += ["", "## Today's decisions", *lines]

    if shadow_lines:
        parts += ["", "## Shadow strategies (logged, never traded)", *shadow_lines]

    if alerts:
        parts += ["", "## ⚠️ Alerts", *[f"- {a}" for a in alerts]]

    return "\n".join(parts)


def send_discord(report_md):
    if not DISCORD_WEBHOOK:
        print("No DISCORD_WEBHOOK_URL set, skipping Discord send.")
        return
    r = requests.post(DISCORD_WEBHOOK, json={"content": report_md[:1900]}, timeout=20)
    if not r.ok:
        print(f"Discord send failed: {r.status_code} {r.text}")


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    if os.path.exists(LOG_PATH):
        print(f"Already ran today ({TODAY}), skipping to avoid double-trading.")
        return

    account = get_account()
    lines, alerts, regime, market_data, context_by_symbol = decide_and_trade()

    shadows = load_shadows()
    shadow_lines = run_shadows(shadows, market_data, regime, context_by_symbol)
    with open(SHADOW_PATH, "w") as f:
        json.dump(shadows, f, indent=2)

    positions = get_positions()
    report = build_report(lines, alerts, account, positions, shadow_lines, regime)

    with open(LOG_PATH, "w") as f:
        f.write(report)

    print(report)
    send_discord(report)


if __name__ == "__main__":
    main()
