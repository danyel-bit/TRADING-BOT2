"""
Constellation morning review.

Runs once each weekday morning, right around market open, BEFORE that day's
trading logic runs. This is the actual "learning from yesterday" step:

1. Pulls every trade the bot actually closed (won or lost) from Alpaca's own
   record — not a guess, the real fill history.
2. Pairs each buy with its matching sell to compute a real win/loss and % return.
3. Judges performance against simple, transparent thresholds.
4. Nudges the strategy's parameters within safe, hard-coded bounds — never a
   big jump, and every change is logged with the reasoning behind it.
5. Writes the result to strategy_state.json, which trading_bot.py reads on its
   next run. Also posts a plain-language summary to Discord and appends a
   dated review log to logs/.

This is NOT a neural network and doesn't pretend to be one. It's a bounded,
inspectable feedback loop — the kind of "learning" that's honest to build for
something real money could eventually touch.
"""

import os
import json
import datetime from zoneinfo
import ZoneInfo import requests

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

DEFAULT_STATE = {
    "buy_momentum_pct": 3.0,
    "stop_loss_pct": -5.0,
    "take_profit_pct": 15.0,
    "position_size_usd": 500,
    "updated": None,
    "last_review_summary": "No review has run yet — using defaults.",
}

# Hard bounds — the review can nudge parameters but never push them outside
# these ranges, no matter what the numbers say. Keeps the bot from tuning
# itself into something reckless off a small or unlucky sample.
BOUNDS = {
    "buy_momentum_pct": (1.5, 8.0),
    "stop_loss_pct": (-9.0, -3.0),
    "take_profit_pct": (8.0, 25.0),
}
MIN_TRADES_TO_TUNE = 3   # don't change anything on a tiny, noisy sample


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return {**DEFAULT_STATE, **json.load(f)}
        except Exception:
            pass
    return dict(DEFAULT_STATE)


def get_fills(days_back=7):
    """Pull real fill history from Alpaca so the review is based on what
    actually happened, not an estimate."""
    after = (datetime.datetime.utcnow() - datetime.timedelta(days=days_back)).isoformat() + "Z"
    r = requests.get(
        f"{TRADING_BASE}/v2/account/activities/FILL",
        headers=HEADERS,
        params={"after": after, "direction": "asc"},
    )
    r.raise_for_status()
    return r.json()


def pair_round_trips(fills):
    """Turn a stream of buy/sell fills into completed round-trip trades.
    The bot only ever holds one position per symbol at a time, so pairing
    each buy with the next sell of the same symbol is a safe, exact match."""
    open_buys = {}
    trades = []
    for f in fills:
        sym = f["symbol"]
        side = f["side"]
        qty = float(f["qty"])
        price = float(f["price"])
        if side == "buy":
            open_buys[sym] = {"qty": qty, "price": price, "time": f["transaction_time"]}
        elif side == "sell" and sym in open_buys:
            entry = open_buys.pop(sym)
            pct = (price - entry["price"]) / entry["price"] * 100
            trades.append({
                "symbol": sym, "entry": entry["price"], "exit": price,
                "pct": pct, "closed": f["transaction_time"],
            })
    return trades


def judge_and_tune(state, trades):
    """The actual review: look at real results, decide what (if anything)
    to change, and write down exactly why."""
    notes = []
    if len(trades) < MIN_TRADES_TO_TUNE:
        notes.append(
            f"Only {len(trades)} closed trade(s) in the lookback window — "
            f"not enough data to tune responsibly. Leaving parameters unchanged."
        )
        return state, notes, {"trades": len(trades), "wins": 0, "win_rate": None}

    wins = [t for t in trades if t["pct"] > 0]
    losses = [t for t in trades if t["pct"] <= 0]
    win_rate = len(wins) / len(trades)
    avg_win = sum(t["pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pct"] for t in losses) / len(losses) if losses else 0

    notes.append(
        f"Reviewed {len(trades)} closed trades: {len(wins)} win(s), {len(losses)} loss(es) "
        f"— win rate {win_rate*100:.0f}%. Avg win {avg_win:+.1f}%, avg loss {avg_loss:+.1f}%."
    )

    new_state = dict(state)

    # Win rate too low → be pickier about what counts as a signal
    if win_rate < 0.40:
        old = state["buy_momentum_pct"]
        new = min(BOUNDS["buy_momentum_pct"][1], round(old + 0.5, 2))
        if new != old:
            new_state["buy_momentum_pct"] = new
            notes.append(f"Win rate is low — raising the momentum threshold from {old}% to {new}% to be more selective.")
    # Win rate strong with a decent sample → can afford to loosen slightly and catch more setups
    elif win_rate > 0.65 and len(trades) >= 5:
        old = state["buy_momentum_pct"]
        new = max(BOUNDS["buy_momentum_pct"][0], round(old - 0.3, 2))
        if new != old:
            new_state["buy_momentum_pct"] = new
            notes.append(f"Win rate is strong — loosening the momentum threshold from {old}% to {new}% to catch more setups.")

    # Losses running deeper than the stop suggests slippage/gaps — tighten it
    if losses and avg_loss < state["stop_loss_pct"] - 1.5:
        old = state["stop_loss_pct"]
        new = min(BOUNDS["stop_loss_pct"][1], round(old + 1.0, 2))
        if new != old:
            new_state["stop_loss_pct"] = new
            notes.append(f"Losses are running deeper than the stop-loss level — tightening it from {old}% to {new}%.")

    # Take-profit almost never reached by winners → lower the bar
    if wins and avg_win < state["take_profit_pct"] * 0.5:
        old = state["take_profit_pct"]
        new = max(BOUNDS["take_profit_pct"][0], round(old - 2.0, 2))
        if new != old:
            new_state["take_profit_pct"] = new
            notes.append(f"Winners rarely reach the take-profit target — lowering it from {old}% to {new}% to lock in gains sooner.")

    if len(notes) == 1:  # only the summary line, no changes were made
        notes.append("Performance doesn't clearly call for a change — keeping current parameters.")

    return new_state, notes, {"trades": len(trades), "wins": len(wins), "win_rate": win_rate}


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
    fills = get_fills(days_back=7)
    trades = pair_round_trips(fills)
    new_state, notes, stats = judge_and_tune(state, trades)

    new_state["updated"] = TODAY
    new_state["last_review_summary"] = " ".join(notes)
    with open(STATE_PATH, "w") as f:
        json.dump(new_state, f, indent=2)

    report = "\n".join([
        f"# Morning review — {TODAY}",
        "",
        *[f"- {n}" for n in notes],
        "",
        f"**Parameters going into today:** buy on +{new_state['buy_momentum_pct']}% momentum, "
        f"stop at {new_state['stop_loss_pct']}%, take profit at +{new_state['take_profit_pct']}%",
    ])
    with open(REVIEW_LOG_PATH, "w") as f:
        f.write(report)

    print(report)
    discord_msg = (
        f"🧠 **Morning Review — {TODAY}**\n\n"
        + "\n".join(notes)
        + f"\n\n**Today's parameters:** buy +{new_state['buy_momentum_pct']}% momentum, "
        + f"stop {new_state['stop_loss_pct']}%, take-profit +{new_state['take_profit_pct']}%"
    )
    send_discord(discord_msg)


if __name__ == "__main__":
    main()
