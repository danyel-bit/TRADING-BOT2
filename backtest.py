"""
Constellation backtest engine.

The problem this solves: waiting for real live trades to learn from is painfully
slow — you might get one closed trade a week. This script instead runs the exact
same strategy against 1-2 years of REAL historical price data for every ticker on
the watchlist, testing many parameter combinations in seconds, and picks whichever
combination would actually have performed best historically.

This is not a guarantee of future performance — no backtest is. Markets change,
and a strategy that worked great in the past can stop working. But it's a far
better starting point than arbitrary guesses, and it's the same real data anyone
doing this seriously would look at.

Run this manually whenever you want (via the Actions tab), or let it run on its
own monthly schedule to periodically re-check whether the tuned parameters still
make sense against a rolling history window.
"""

import os
import json
import datetime
import requests

ALPACA_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET = os.environ["ALPACA_SECRET_KEY"]
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL")

DATA_BASE = "https://data.alpaca.markets"
HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}

HERE = os.path.dirname(__file__)
STATE_PATH = os.path.join(HERE, "strategy_state.json")
LOG_DIR = os.path.join(HERE, "logs")
TODAY = datetime.date.today().isoformat()

with open(os.path.join(HERE, "watchlist.json")) as f:
    WATCHLIST = json.load(f)["tickers"]

DEFAULT_GLOBAL = {
    "buy_momentum_pct": 3.0,
    "stop_loss_pct": -5.0,
    "take_profit_pct": 15.0,
    "position_size_usd": 500,
    "ma_period": 20,
    "volume_multiplier": 1.2,
}

HISTORY_DAYS = 730          # about 2 years of calendar days (weekends included, harmless)
MIN_SIMULATED_TRADES = 4    # a parameter combo needs at least this many trades to be trusted

# The grid of parameter combinations to test per ticker. Deliberately small —
# a bigger grid takes longer and risks fitting too tightly to the past.
MOMENTUM_GRID = [2.0, 3.0, 4.0, 5.0]
STOP_GRID = [-4.0, -6.0, -8.0]
PROFIT_GRID = [10.0, 15.0, 20.0]


def get_daily_bars(symbol, days):
    start = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    r = requests.get(
        f"{DATA_BASE}/v2/stocks/{symbol}/bars",
        headers=HEADERS,
        params={"timeframe": "1Day", "start": start, "limit": 10000, "feed": "iex"},
    )
    r.raise_for_status()
    return r.json().get("bars", [])


def simulate(bars, momentum_pct, stop_pct, profit_pct, ma_period=20, volume_mult=1.2):
    """Walks through the bar history day by day, applying the exact same three
    signals as the live bot (momentum + trend filter + volume confirmation),
    with the same stop-loss/take-profit exit rules. Returns the list of
    completed round-trip trades."""
    trades = []
    position = None  # {"entry": price, "day": i}

    for i in range(ma_period, len(bars)):
        today = bars[i]
        yesterday = bars[i - 1]
        price = today["c"]
        pct_change = (today["c"] - yesterday["c"]) / yesterday["c"] * 100
        window = bars[i - ma_period:i]
        sma = sum(b["c"] for b in window) / ma_period
        avg_vol = sum(b["v"] for b in window) / ma_period

        if position:
            pl_pct = (price - position["entry"]) / position["entry"] * 100
            if pl_pct <= stop_pct or pl_pct >= profit_pct:
                trades.append({"entry": position["entry"], "exit": price, "pct": pl_pct})
                position = None
            continue

        vol_ratio = (today["v"] / avg_vol) if avg_vol else 0
        if pct_change >= momentum_pct and price > sma and vol_ratio >= volume_mult:
            position = {"entry": price}

    return trades


def score(trades):
    """A simple, transparent scoring function: total compounded return across
    all trades, penalized if too few trades happened to trust the result."""
    if len(trades) < MIN_SIMULATED_TRADES:
        return None
    total_return = 1.0
    for t in trades:
        total_return *= (1 + t["pct"] / 100)
    win_rate = len([t for t in trades if t["pct"] > 0]) / len(trades)
    return {
        "total_return_pct": (total_return - 1) * 100,
        "n_trades": len(trades),
        "win_rate": win_rate,
    }


def backtest_symbol(symbol):
    bars = get_daily_bars(symbol, HISTORY_DAYS)
    if len(bars) < 60:
        return None, f"{symbol}: not enough historical data ({len(bars)} bars) — skipped."

    best = None
    for m in MOMENTUM_GRID:
        for s in STOP_GRID:
            for tp in PROFIT_GRID:
                trades = simulate(bars, m, s, tp)
                result = score(trades)
                if result is None:
                    continue
                if best is None or result["total_return_pct"] > best["result"]["total_return_pct"]:
                    best = {"params": {"buy_momentum_pct": m, "stop_loss_pct": s, "take_profit_pct": tp}, "result": result}

    if best is None:
        return None, f"{symbol}: no parameter combination produced enough trades to trust — kept existing settings."

    p, r = best["params"], best["result"]
    note = (
        f"{symbol}: best found was momentum +{p['buy_momentum_pct']}%, stop {p['stop_loss_pct']}%, "
        f"take-profit +{p['take_profit_pct']}% — {r['n_trades']} simulated trades over ~2 years, "
        f"{r['win_rate']*100:.0f}% win rate, {r['total_return_pct']:+.1f}% total simulated return."
    )
    return best["params"], note


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                raw = json.load(f)
            return raw
        except Exception:
            pass
    return {"global": dict(DEFAULT_GLOBAL), "per_symbol": {}}


def send_discord(text):
    if not DISCORD_WEBHOOK:
        print("No DISCORD_WEBHOOK_URL set, skipping Discord send.")
        return
    r = requests.post(DISCORD_WEBHOOK, json={"content": text[:1900]})
    if not r.ok:
        print(f"Discord send failed: {r.status_code} {r.text}")


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    state = load_state()
    state.setdefault("global", dict(DEFAULT_GLOBAL))
    state.setdefault("per_symbol", {})

    notes = []
    for symbol in WATCHLIST:
        try:
            params, note = backtest_symbol(symbol)
        except Exception as e:
            note = f"{symbol}: backtest failed ({e})."
            params = None
        notes.append(note)

        if not params:
            continue

        existing = state["per_symbol"].get(symbol)
        if existing and existing.get("source") == "review":
            notes.append(f"{symbol}: skipping backtest update — live review has already tuned this ticker from real trades, which takes priority.")
            continue

        params["source"] = "backtest"
        state["per_symbol"][symbol] = params

    state["last_backtest_summary"] = " ".join(notes)
    state["updated"] = TODAY
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

    report = "\n".join([f"# Backtest results — {TODAY}", "", *[f"- {n}" for n in notes]])
    with open(os.path.join(LOG_DIR, f"backtest-{TODAY}.md"), "w") as f:
        f.write(report)

    print(report)
    send_discord(f"🔬 **Backtest complete — {TODAY}**\n\n" + "\n".join(notes) +
                 "\n\nThese per-ticker parameters are now active. Live results will "
                 "still be reviewed and further tuned every morning.")


if __name__ == "__main__":
    main()
