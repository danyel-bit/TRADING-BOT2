"""
Constellation paper-trading bot.

What this is: a small, fully transparent rules-based strategy that runs once a day
via GitHub Actions, checks a watchlist, and places PAPER (fake money) trades on
Alpaca's paper trading API based on simple, readable rules below. It never touches
real money — it can't, paper API keys are physically incapable of placing live orders.

The "learning" here is honest: this isn't a neural net that mysteriously improves.
It's a fixed strategy that logs exactly why it did what it did, every day, into
logs/. Read those logs over time to see what worked and refine the rules yourself
(or ask Claude to help you refine them) — that's the actual learning loop.
"""

import os
import json
import datetime
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

with open(os.path.join(os.path.dirname(__file__), "watchlist.json")) as f:
    WATCHLIST = json.load(f)["tickers"]

# Strategy knobs — deliberately simple and readable. Change these to experiment.
BUY_MOMENTUM_PCT = 3.0     # buy if a ticker is up at least this much today and we don't already hold it
STOP_LOSS_PCT = -5.0       # sell a held position if it's down this much since we bought it
TAKE_PROFIT_PCT = 15.0     # sell a held position if it's up this much since we bought it
POSITION_SIZE_USD = 500    # dollars to put into each new position

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
TODAY = datetime.date.today().isoformat()
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
    """Returns (current_price, pct_change_today) using free IEX data feed."""
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
    if not price or not prev_close:
        return None, None
    pct_change = (price - prev_close) / prev_close * 100
    return price, pct_change


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
        try:
            price, pct_change = get_snapshot(symbol)
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

            if pl_pct <= STOP_LOSS_PCT:
                place_order(symbol, qty, "sell")
                lines.append(f"- 🔴 **SOLD {symbol}** — down {pl_pct:.1f}% since entry (stop loss triggered)")
                alerts.append(f"📉 **{symbol}** hit stop loss, down {pl_pct:.1f}% since entry — sold.")
            elif pl_pct >= TAKE_PROFIT_PCT:
                place_order(symbol, qty, "sell")
                lines.append(f"- 🟢 **SOLD {symbol}** — up {pl_pct:.1f}% since entry (took profit)")
                alerts.append(f"🚀 **{symbol}** hit take-profit, up {pl_pct:.1f}% since entry — sold.")
            else:
                lines.append(f"- ⚪ **HOLD {symbol}** — {pl_pct:+.1f}% since entry, no action")
        else:
            if pct_change is not None and pct_change >= BUY_MOMENTUM_PCT:
                qty = max(1, int(POSITION_SIZE_USD / price))
                try:
                    place_order(symbol, qty, "buy")
                    lines.append(f"- 🟢 **BOUGHT {symbol}** — up {pct_change:.1f}% today (momentum signal), {qty} sh @ ~${price:.2f}")
                    alerts.append(f"🚀 Bought **{symbol}**, up {pct_change:.1f}% today — new momentum position opened.")
                except Exception as e:
                    lines.append(f"- ⚠️ Wanted to buy {symbol} but order failed: {e}")
            else:
                chg = f"{pct_change:+.1f}%" if pct_change is not None else "n/a"
                lines.append(f"- ⚪ Watching **{symbol}** — {chg} today, no signal")

    return lines, alerts


def build_report(lines, alerts, account, positions):
    equity = float(account["equity"])
    cash = float(account["cash"])
    pl_today = float(account.get("equity", 0)) - float(account.get("last_equity", account["equity"]))

    parts = [
        f"# Constellation daily report — {TODAY}",
        "",
        f"**Account equity:** ${equity:,.2f}  |  **Cash:** ${cash:,.2f}  |  **Today's change:** {pl_today:+,.2f}",
        "",
        "## Positions" if positions else "## Positions\n_No open positions._",
    ]
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
