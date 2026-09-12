# Constellation Paper Trading Bot

A small, fully transparent, rules-based bot that trades **fake money only** on
Alpaca's paper trading API, once a day, on a schedule — even if your laptop is
off — and sends a daily report to Discord.

**This can never touch real money.** Paper API keys are a separate, sandboxed
account type that Alpaca does not allow to place live orders, full stop.

## What it actually does

Every weekday shortly after market close, it:
1. Checks each ticker in `watchlist.json`
2. Applies simple rules (see "The strategy" below)
3. Buys / sells / holds accordingly, in the paper account only
4. Writes exactly what it did and why into `logs/YYYY-MM-DD.md`
5. Sends a summary to your Discord

## One-time setup

### 1. Get free Alpaca paper trading keys
1. Sign up at [alpaca.markets](https://alpaca.markets) (free, no card needed)
2. Go to your dashboard, make sure you're on **Paper Trading** (not Live)
3. Generate an API Key ID and Secret Key — save both, the secret is only shown once

### 2. Create a GitHub repo
1. If you don't have a GitHub account, make one free at [github.com](https://github.com)
2. Create a new repository (Settings can be Private — Actions still works fine for free)
3. Upload all the files in this folder to that repo (drag-and-drop works on the GitHub web UI, including the `.github/workflows/` folder — make sure that folder path is preserved)

### 3. Add your secrets
In your repo: **Settings → Secrets and variables → Actions → New repository secret**. Add three:
- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`
- `DISCORD_WEBHOOK_URL` — reuse the same one from your Constellation app, or make a new one the same way (Discord → Server Settings → Integrations → Webhooks)

### 4. Edit your watchlist
Open `watchlist.json` in the repo and edit the ticker list to whatever you want the bot watching.

### 5. Test it immediately
Go to the **Actions** tab in your repo → "Daily Paper Trading Bot" → **Run workflow** button. Don't wait for the schedule — this runs it right now so you can confirm everything works. Check Discord for the report afterward.

## The strategy (edit these in `trading_bot.py`)

- **Buy signal:** a watchlist ticker is up 3%+ that day and you don't already hold it → buys $500 worth
- **Stop loss:** a held position down 5%+ since you bought it → sells
- **Take profit:** a held position up 15%+ since you bought it → sells
- Otherwise: holds and logs what it's watching

This is a deliberately simple starting point, not a proven strategy — the whole
point of paper trading it first is to see how these rules actually perform
before ever considering real money.

## Where "learning" actually lives

Every day's reasoning gets committed to `logs/`. That's your real audit trail —
read back through it periodically to see what the bot did and why, and to spot
patterns worth turning into rule changes.

There's now a second, automatic layer on top of that: **the morning review.**

### The morning review (real self-tuning)

Each weekday morning, before the day's trading logic runs, a separate job
(`review_and_tune.py`) does the following:

1. Pulls the bot's actual closed trades from Alpaca's own records — real
   wins and losses, not estimates
2. Computes win rate, average win, and average loss over the past week
3. Nudges `strategy_state.json` — the buy threshold, stop-loss, and
   take-profit — based on simple, transparent rules (e.g. "win rate below
   40% → require a stronger signal before buying")
4. Every change (or decision not to change anything) gets written out in
   plain language, committed to `logs/review-YYYY-MM-DD.md`, and posted to
   Discord
5. `trading_bot.py` reads `strategy_state.json` on its next run, so that
   day's trades use the freshly tuned parameters

**What this is, honestly:** a bounded, fully inspectable feedback loop, not a
neural network. It can only nudge parameters within hard-coded safe ranges
(see `BOUNDS` in `review_and_tune.py`), and it won't touch anything until
there have been at least 3 closed trades to judge from. That's deliberate —
the goal is a system you can audit line by line, not a black box that decides
things for reasons nobody can see.

This same setup for Alpaca and Discord secrets already covers the morning
review — no extra accounts or keys needed. It runs automatically once you've
uploaded `.github/workflows/morning-review.yml` and `review_and_tune.py`
alongside everything else.
