"""
Constellation paper-trading bot.

What this is: a small, fully transparent rules-based strategy that runs once a day
via GitHub Actions, checks a watchlist, and places PAPER (fake money) trades on
Alpaca's paper trading API based on simple, readable rules below. It never touches
real money — it can't, paper API keys are physically incapable of placing live orders.

The "learning" here is honest: this isn't a neural net that mysteriously improves.
It's a strategy with a few readable signals, tuned two ways:
  1. backtest.py finds good starting parameters from real historical price data
  2. review_and_tune.py nudges those parameters over time based on real live results
Both write to strategy_state.json, which this script reads before every run.

Signals used before buying (all three must agree):
  - Momentum: price is up at least X% today
  - Trend filter: price is above its own 20-day average (not fighting a downtrend)
  - Volume confirmation: today's volume is meaningfully above its recent average
    (a move on real volume is more trustworthy than one on a quiet, thin day)
"""

import os
import json
import datetime
from zoneinfo import ZoneInfo
import requests

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

# ---------- STRATEGY STATE (global defaults + optional per-symbol overrides) ----------
STATE_PATH = os.path.join(HERE, "strategy_state.json")
DEFAULT_GLOBAL = {
    "buy_momentum_pct": 3.0,
    "stop_loss_pct": -5.0,
    "take_profit_pct": 15.0,
    "position_size_usd": 500,
    "ma_period": 20,             # days in the trend-filter moving average
    "volume_multiplier": 1.2,    # today's volume must be at least this many times the recent average
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
    return json.loads(json.dumps(DEFAULT_STATE))  # deep copy

STATE = load_state()

def params_for(symbol):
    """Merge global defaults with any per-symbol override for this ticker."""
    p = dict(STATE["global"])
    p.update(STATE["per_symbol"].get(symbol, {}))
    return p

POSITION_SIZE_USD = STATE["global"]["position_size_usd"]

LOG_DIR = os.path.join(HERE, "logs")
TODAY = datetime.datetime.now(ZoneInfo("America/New_York")).date().isoformat()  # market date, not server UTC date
LOG_PATH = os.path.join(LOG_DIR, f"{TODAY}.md")


# ---------- ALPACA HELPERS ----------
def get_account():
    r = requests.get(f"{TRADING_BASE}/v2/account", headers=HEADERS)
    r.raise_for_status()
    return r.json()


def get_positions():
    r = requests.get(f"{TRADING_BASE}/v2/positions", headers=HEADERS)
    r.raise_for_status()
    return {p["symbol"]: p for p in r.json()}


def get_snapshot(symbol):
    """Returns (current_price, pct_change_today, today_volume) using the free IEX feed."""
    r = requests.get(
        f"{DATA_BASE}/v2/stocks/{symbol}/snapshot",
        headers=HEADERS,
        params={"feed": "iex"},
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
    pct_change = (price - prev_close) / prev_close * 100
    return price, pct_change, volume


def get_recent_bars(symbol, days):
    """Daily bars for the trend/volume filters — deliberately small (a few weeks),
    separate from backtest.py's much larger historical pull."""
    r = requests.get(
        f"{DATA_BASE}/v2/stocks/{symbol}/bars",
        headers=HEADERS,
        params={"timeframe": "1Day", "limit": days, "feed": "iex"},
    )
    r.raise_for_status()
    return r.json().get("bars", [])


def get_signal_context(symbol, ma_period):
    """Computes the moving average and average volume used by the trend and
    volume filters. Returns None values if there isn't enough history yet."""
    bars = get_recent_bars(symbol, ma_period + 5)
    if len(bars) < ma_period:
        return None, None
    closes = [b["c"] for b in bars[-ma_period:]]
    volumes = [b["v"] for b in bars[-ma_period:]]
    sma = sum(closes) / len(closes)
    avg_volume = sum(volumes) / len(volumes)
    return sma, avg_volume


def place_order(symbol, qty, side):
    order = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": "day",
    }
    r = requests.post(f"{TRADING_BASE}/v2/orders", headers=HEADERS, json=order)
    r.raise_for_status()
    return r.json()


# ---------- STRATEGY ----------
def decide_and_trade():
    positions = get_positions()
    lines = []
    alerts = []

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

        held = positions.get(symbol)

        if held:
            entry = float(held["avg_entry_price"])
            qty = held["qty"]
            pl_pct = (price - entry) / entry * 100

            if pl_pct <= p["stop_loss_pct"]:
                place_order(symbol, qty, "sell")
                lines.append(f"- 🔴 **SOLD {symbol}** — down {pl_pct:.1f}% since entry (stop loss at {p['stop_loss_pct']}%)")
                alerts.append(f"📉 **{symbol}** hit stop loss, down {pl_pct:.1f}% since entry — sold.")
            elif pl_pct >= p["take_profit_pct"]:
                place_order(symbol, qty, "sell")
                lines.append(f"- 🟢 **SOLD {symbol}** — up {pl_pct:.1f}% since entry (take-profit at {p['take_profit_pct']}%)")
                alerts.append(f"🚀 **{symbol}** hit take-profit, up {pl_pct:.1f}% since entry — sold.")
            else:
                lines.append(f"- ⚪ **HOLD {symbol}** — {pl_pct:+.1f}% since entry, no action")
            continue

        # Not holding it — check all three signals before buying.
        if pct_change is None or pct_change < p["buy_momentum_pct"]:
            chg = f"{pct_change:+.1f}%" if pct_change is not None else "n/a"
            lines.append(f"- ⚪ Watching **{symbol}** — {chg} today, below the +{p['buy_momentum_pct']}% momentum bar")
            continue

        try:
            sma, avg_volume = get_signal_context(symbol, p["ma_period"])
        except Exception as e:
            lines.append(f"- ⚠️ **{symbol}** had a momentum signal but trend/volume data failed ({e}) — skipped to be safe")
            continue

        if sma is None:
            lines.append(f"- ⚪ **{symbol}** — momentum signal but not enough price history yet for the trend filter, skipped")
            continue

        above_trend = price > sma
        vol_ratio = (volume / avg_volume) if (volume and avg_volume) else 0
        volume_ok = vol_ratio >= p["volume_multiplier"]

        if above_trend and volume_ok:
            qty = max(1, int(POSITION_SIZE_USD / price))
            try:
                place_order(symbol, qty, "buy")
                lines.append(
                    f"- 🟢 **BOUGHT {symbol}** — up {pct_change:.1f}% today, above its {p['ma_period']}-day average, "
                    f"volume {vol_ratio:.1f}x normal, {qty} sh @ ~${price:.2f}"
                )
                alerts.append(f"🚀 Bought **{symbol}**, up {pct_change:.1f}% today with volume confirmation — new position opened.")
            except Exception as e:
                lines.append(f"- ⚠️ Wanted to buy {symbol} but order failed: {e}")
        else:
            reasons = []
            if not above_trend:
                reasons.append(f"below its {p['ma_period']}-day average")
            if not volume_ok:
                reasons.append(f"volume only {vol_ratio:.1f}x normal, needs {p['volume_multiplier']}x")
            lines.append(f"- ⚪ **{symbol}** — up {pct_change:.1f}% today but {', '.join(reasons)}, holding off")

    return lines, alerts


def build_report(lines, alerts, account, positions):
    equity = float(account["equity"])
    cash = float(account["cash"])
    pl_today = float(account.get("equity", 0)) - float(account.get("last_equity", account["equity"]))
    g = STATE["global"]

    parts = [
        f"# Constellation daily report — {TODAY}",
        "",
        f"**Account equity:** ${equity:,.2f}  |  **Cash:** ${cash:,.2f}  |  **Today's change:** {pl_today:+,.2f}",
        f"**Base strategy:** buy on +{g['buy_momentum_pct']}% momentum (above {g['ma_period']}-day avg, "
        f"{g['volume_multiplier']}x volume) · stop at {g['stop_loss_pct']}% · take profit at +{g['take_profit_pct']}%",
    ]
    if STATE["per_symbol"]:
        overrides = ", ".join(STATE["per_symbol"].keys())
        parts.append(f"**Per-ticker tuning active for:** {overrides}")
    parts += ["", "## Positions" if positions else "## Positions\n_No open positions._"]

    for sym, p in positions.items():
        parts.append(f"- {sym}: {p['qty']} sh, avg entry ${float(p['avg_entry_price']):.2f}, unrealized P/L {float(p['unrealized_plpc'])*100:+.1f}%")

    parts += ["", "## Today's decisions", *lines]

    if alerts:
        parts += ["", "## ⚠️ Alerts", *[f"- {a}" for a in alerts]]

    return "\n".join(parts)


def send_discord(report_md):
    if not DISCORD_WEBHOOK:
        print("No DISCORD_WEBHOOK_URL set, skipping Discord send.")
        return
    content = report_md[:1900]
    r = requests.post(DISCORD_WEBHOOK, json={"content": content})
    if not r.ok:
        print(f"Discord send failed: {r.status_code} {r.text}")


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    if os.path.exists(LOG_PATH):
        print(f"Already ran today ({TODAY}), skipping to avoid double-trading.")
        return

    account = get_account()
    lines, alerts = decide_and_trade()
    positions = get_positions()
    report = build_report(lines, alerts, account, positions)

    with open(LOG_PATH, "w") as f:
        f.write(report)

    print(report)
    send_discord(report)


if __name__ == "__main__":
    main()
