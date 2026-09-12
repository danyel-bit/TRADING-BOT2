"""
Constellation morning review.

Runs once each weekday morning, right around market open, BEFORE that day's
trading logic runs. This is the "learning from yesterday" step:

1. Pulls every trade the bot actually closed (won or lost) from Alpaca's own
   record — not a guess, the real fill history.
2. Pairs each buy with its matching sell to compute a real win/loss and % return.
3. Judges performance overall AND per-ticker (a slow blue-chip and a volatile
   ticker don't behave the same way, so they get separate treatment once
   there's enough data on each).
4. Nudges parameters within safe, hard-coded bounds — never a big jump, and
   every change is logged with the reasoning behind it.
5. Writes the result to strategy_state.json, which trading_bot.py reads on its
   next run. Also posts a plain-language summary to Discord.

This is NOT a neural network and doesn't pretend to be one. It's a bounded,
inspectable feedback loop.
"""

import os
import json
import datetime
from zoneinfo import ZoneInfo
import requests

ALPACA_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET = os.environ["ALPACA_SECRET_KEY"]
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL")

TRADING_BASE = "https://paper-api.alpaca.markets"
HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}

HERE = os.path.dirname(__file__)
STATE_PATH = os.path.join(HERE, "strategy_state.json")
LOG_DIR = os.path.join(HERE, "logs")
TODAY = datetime.datetime.now(ZoneInfo("America/New_York")).date().isoformat()
REVIEW_LOG_PATH = os.path.join(LOG_DIR, f"review-{TODAY}.md")

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

# Hard bounds — nudges can never push a parameter outside these, no matter
# what the numbers say. Applies identically to global and per-symbol tuning.
BOUNDS = {
    "buy_momentum_pct": (1.5, 8.0),
    "stop_loss_pct": (-9.0, -3.0),
    "take_profit_pct": (8.0, 25.0),
}
MIN_TRADES_TO_TUNE = 3          # minimum trades before the global strategy gets nudged
MIN_TRADES_PER_SYMBOL = 3       # minimum trades on ONE ticker before it gets its own tuning


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


def get_fills(days_back=14):
    after = (datetime.datetime.utcnow() - datetime.timedelta(days=days_back)).isoformat() + "Z"
    r = requests.get(
        f"{TRADING_BASE}/v2/account/activities/FILL",
        headers=HEADERS,
        params={"after": after, "direction": "asc"},
    )
    r.raise_for_status()
    return r.json()


def pair_round_trips(fills):
    """Turn a stream of buy/sell fills into completed round-trip trades,
    grouped by symbol. The bot only ever holds one position per symbol at a
    time, so pairing each buy with the next sell of the same symbol is exact."""
    open_buys = {}
    trades = []
    for f in fills:
        sym = f["symbol"]
        side = f["side"]
        qty = float(f["qty"])
        price = float(f["price"])
        if side == "buy":
            open_buys[sym] = {"qty": qty, "price": price}
        elif side == "sell" and sym in open_buys:
            entry = open_buys.pop(sym)
            pct = (price - entry["price"]) / entry["price"] * 100
            trades.append({"symbol": sym, "entry": entry["price"], "exit": price, "pct": pct})
    return trades


def stats_for(trades):
    if not trades:
        return None
    wins = [t for t in trades if t["pct"] > 0]
    losses = [t for t in trades if t["pct"] <= 0]
    return {
        "n": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades),
        "avg_win": sum(t["pct"] for t in wins) / len(wins) if wins else 0,
        "avg_loss": sum(t["pct"] for t in losses) / len(losses) if losses else 0,
    }


def tune_params(current, stats, label):
    """The shared tuning logic — used for both the global params and any
    individual symbol's params. Returns (new_params, notes)."""
    notes = []
    new_params = dict(current)

    if stats["win_rate"] < 0.40:
        old = current["buy_momentum_pct"]
        new = min(BOUNDS["buy_momentum_pct"][1], round(old + 0.5, 2))
        if new != old:
            new_params["buy_momentum_pct"] = new
            notes.append(f"{label}: win rate low ({stats['win_rate']*100:.0f}%) — raising momentum bar from {old}% to {new}%.")
    elif stats["win_rate"] > 0.65 and stats["n"] >= 5:
        old = current["buy_momentum_pct"]
        new = max(BOUNDS["buy_momentum_pct"][0], round(old - 0.3, 2))
        if new != old:
            new_params["buy_momentum_pct"] = new
            notes.append(f"{label}: win rate strong ({stats['win_rate']*100:.0f}%) — loosening momentum bar from {old}% to {new}%.")

    if stats["losses"] and stats["avg_loss"] < current["stop_loss_pct"] - 1.5:
        old = current["stop_loss_pct"]
        new = min(BOUNDS["stop_loss_pct"][1], round(old + 1.0, 2))
        if new != old:
            new_params["stop_loss_pct"] = new
            notes.append(f"{label}: losses running deeper than the stop ({stats['avg_loss']:+.1f}%) — tightening from {old}% to {new}%.")

    if stats["wins"] and stats["avg_win"] < current["take_profit_pct"] * 0.5:
        old = current["take_profit_pct"]
        new = max(BOUNDS["take_profit_pct"][0], round(old - 2.0, 2))
        if new != old:
            new_params["take_profit_pct"] = new
            notes.append(f"{label}: winners rarely reach take-profit (avg {stats['avg_win']:+.1f}%) — lowering from {old}% to {new}%.")

    return new_params, notes


def send_discord(text):
    if not DISCORD_WEBHOOK:
        print("No DISCORD_WEBHOOK_URL set, skipping Discord send.")
        return
    r = requests.post(DISCORD_WEBHOOK, json={"content": text[:1900]})
    if not r.ok:
        print(f"Discord send failed: {r.status_code} {r.text}")


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    if os.path.exists(REVIEW_LOG_PATH):
        print(f"Already reviewed today ({TODAY}), skipping.")
        return

    state = load_state()
    fills = get_fills(days_back=14)
    trades = pair_round_trips(fills)

    all_notes = []

    # --- overall / global tuning ---
    overall_stats = stats_for(trades)
    if overall_stats is None or overall_stats["n"] < MIN_TRADES_TO_TUNE:
        n = overall_stats["n"] if overall_stats else 0
        all_notes.append(f"Only {n} closed trade(s) overall — not enough to tune the base strategy yet.")
    else:
        all_notes.append(
            f"Overall: {overall_stats['n']} closed trades, {overall_stats['wins']} win(s), "
            f"{overall_stats['losses']} loss(es), win rate {overall_stats['win_rate']*100:.0f}%, "
            f"avg win {overall_stats['avg_win']:+.1f}%, avg loss {overall_stats['avg_loss']:+.1f}%."
        )
        new_global, notes = tune_params(state["global"], overall_stats, "Base strategy")
        state["global"] = new_global
        all_notes += notes if notes else ["Base strategy: performance doesn't clearly call for a change."]

    # --- per-symbol tuning ---
    by_symbol = {}
    for t in trades:
        by_symbol.setdefault(t["symbol"], []).append(t)

    for symbol, sym_trades in by_symbol.items():
        sym_stats = stats_for(sym_trades)
        if sym_stats["n"] < MIN_TRADES_PER_SYMBOL:
            continue
        current = dict(state["global"])
        current.update(state["per_symbol"].get(symbol, {}))
        new_params, notes = tune_params(current, sym_stats, symbol)
        if notes:
            # only store the fields that actually differ from global, keeps the file readable
            overrides = {k: v for k, v in new_params.items() if state["global"].get(k) != v}
            overrides["source"] = "review"
            state["per_symbol"][symbol] = overrides
            all_notes += notes
        all_notes.append(
            f"{symbol}: {sym_stats['n']} trades, win rate {sym_stats['win_rate']*100:.0f}%."
        )

    state["updated"] = TODAY
    state["last_review_summary"] = " ".join(all_notes)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

    g = state["global"]
    report = "\n".join([
        f"# Morning review — {TODAY}",
        "",
        *[f"- {n}" for n in all_notes],
        "",
        f"**Base parameters going into today:** buy +{g['buy_momentum_pct']}% momentum, "
        f"stop {g['stop_loss_pct']}%, take profit +{g['take_profit_pct']}%",
    ])
    with open(REVIEW_LOG_PATH, "w") as f:
        f.write(report)

    print(report)
    discord_msg = (
        f"🧠 **Morning Review — {TODAY}**\n\n"
        + "\n".join(all_notes)
        + f"\n\n**Base parameters:** buy +{g['buy_momentum_pct']}% momentum, "
        + f"stop {g['stop_loss_pct']}%, take-profit +{g['take_profit_pct']}%"
    )
    send_discord(discord_msg)


if __name__ == "__main__":
    main()
