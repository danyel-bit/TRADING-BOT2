# Constellation Paper Trading Bot

A rules-based bot that trades **fake money only** on Alpaca's paper trading API,
gets reviewed and tuned every morning based on its own real results, and had its
starting parameters chosen by testing against 2 years of real historical prices.
It runs entirely on GitHub's servers — it doesn't need your computer on.

**This can never touch real money.** Paper API keys are a separate, sandboxed
account type that Alpaca does not allow to place live orders, full stop.

## The four moving pieces

1. **`trading_bot.py`** — runs after market close each weekday. Checks the
   watchlist, buys/sells/holds based on the current strategy, reports to Discord.
2. **`review_and_tune.py`** — runs each weekday morning, before that day's
   trading. Looks at real closed trades from the past 2 weeks (overall AND
   per-ticker) and nudges parameters within safe bounds.
3. **`backtest.py`** — runs once a month (or on demand). Tests dozens of
   parameter combinations against ~2 years of real historical prices per ticker,
   and sets each ticker's starting parameters to whatever would have performed
   best historically.
4. **`strategy_state.json`** — where all of the above read and write. Has a
   `global` set of defaults and a `per_symbol` section for tickers that have
   earned their own tuned settings.

## The strategy itself

A ticker only gets bought when **all three** of these agree:
- **Momentum** — up at least X% today (the core signal)
- **Trend filter** — price is above its own 20-day average (not fighting a downtrend)
- **Volume confirmation** — today's volume is meaningfully above its recent
  average (a move on real volume means more than one on a quiet day)

Held positions exit on a stop-loss or a take-profit, both tuned over time.

## One-time setup

### 1. Get free Alpaca paper trading keys
Sign up at [alpaca.markets](https://alpaca.markets), switch to **Paper Trading**,
generate an API Key ID and Secret Key.

### 2. Create a GitHub repo and upload everything
Create a free GitHub repo, then upload every file in this folder — including
the `.github/workflows/` folder with all three workflow files inside it.

### 3. Add your three secrets
Repo **Settings → Secrets and variables → Actions → New repository secret**:
- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`
- `DISCORD_WEBHOOK_URL`

### 4. Run the backtest first
Before letting the daily bot run, go to **Actions → Backtest → Run workflow**.
This gives every ticker an informed starting point instead of the arbitrary
defaults, based on what actually would have worked over the past ~2 years.

### 5. Test the other two the same way
**Actions → Morning Review → Run workflow**, then
**Actions → Daily Paper Trading Bot → Run workflow**. Check Discord after each.

## Editing the watchlist

Open `watchlist.json` and edit the ticker list. More tickers means more chances
to trade, which means the morning review has real data to learn from sooner —
that's a deliberate trade-off, not just "more options."

## Where "learning" actually lives

Three layers, each honest about what it actually is:

- **Backtesting** is the fast, offline layer — real historical data, tested
  in seconds instead of lived through in real time. Not a guarantee of future
  performance, but a far better starting point than a guess.
- **The morning review** is the live, ongoing layer — actual results from
  actual (paper) trades, nudging parameters within hard bounds, with every
  change explained in plain language.
- **The daily logs** in `logs/` are the human layer — read back through them
  periodically. That's where you (or Claude) spot the patterns worth turning
  into a bigger rule change than the automatic tuning would make on its own.

None of this is a neural network and none of it pretends to be one. Every
number it lands on, and every reason it landed there, is written down
somewhere you can read it.
