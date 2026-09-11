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
