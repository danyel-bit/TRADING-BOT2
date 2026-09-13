"""
Market context — the "understanding, not just numbers" layer.

The original strategy only ever looked at one thing: did this ticker's price and
volume move enough today. That's pattern-matching with no idea WHY anything moved.

This module adds the context a human trader would actually check first:

  1. MARKET REGIME  — is the whole market healthy right now, or is everything
     falling? A stock up 4% on a day the entire market ripped is very different
     from one up 4% while everything else bleeds. This is the single highest-value
     filter a momentum system can have.

  2. NEWS SENTIMENT — is this move backed by a real catalyst (earnings beat, new
     contract) or is it drifting on nothing? Moves with no news behind them tend
     to give the gains back.

  3. EARNINGS PROXIMITY — never open a new position 1-2 days before a company
     reports. That's a coin-flip gamble on a binary event, not momentum. Every
     serious system avoids this.

  4. FUNDAMENTALS SANITY — a company with collapsing margins jumping 5% is a much
     worse momentum bet than a healthy one doing the same. Used as a soft filter,
     not a hard veto.

Everything here degrades gracefully: if a data source is unavailable or the free
tier rate-limits, the check returns "unknown" and the bot proceeds on price
signals alone rather than crashing or silently blocking all trades.
"""

import os
import datetime
from zoneinfo import ZoneInfo
import requests

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")
FINNHUB_BASE = "https://finnhub.io/api/v1"

ALPACA_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY")
DATA_BASE = "https://data.alpaca.markets"
ALPACA_HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY or "",
    "APCA-API-SECRET-KEY": ALPACA_SECRET or "",
}

TODAY = datetime.datetime.now(ZoneInfo("America/New_York")).date()


# ---------------------------------------------------------------- regime
def _get_bars(symbol, limit):
    r = requests.get(
        f"{DATA_BASE}/v2/stocks/{symbol}/bars",
        headers=ALPACA_HEADERS,
        params={"timeframe": "1Day", "limit": limit, "feed": "iex"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("bars", [])


def detect_regime():
    """Classifies the overall market using SPY's own trend.

    Returns a dict:
      regime: "bullish" | "neutral" | "bearish" | "unknown"
      detail: human-readable explanation
      size_multiplier: how much to scale position sizes in this regime
      allow_new_buys: whether new positions should be opened at all
    """
    try:
        bars = _get_bars("SPY", 60)
    except Exception as e:
        return {"regime": "unknown", "detail": f"couldn't load SPY data ({e}) — proceeding without a regime filter",
                "size_multiplier": 1.0, "allow_new_buys": True}

    if len(bars) < 50:
        return {"regime": "unknown", "detail": "not enough SPY history for a regime read",
                "size_multiplier": 1.0, "allow_new_buys": True}

    closes = [b["c"] for b in bars]
    price = closes[-1]
    ma50 = sum(closes[-50:]) / 50
    ma20 = sum(closes[-20:]) / 20

    # how far above/below its own 50-day average the market is
    pct_vs_ma50 = (price - ma50) / ma50 * 100

    if price > ma50 and ma20 > ma50:
        return {
            "regime": "bullish",
            "detail": f"SPY is {pct_vs_ma50:+.1f}% vs its 50-day average with a rising 20-day — broad market is healthy",
            "size_multiplier": 1.0,
            "allow_new_buys": True,
        }
    if price < ma50 and ma20 < ma50:
        return {
            "regime": "bearish",
            "detail": f"SPY is {pct_vs_ma50:+.1f}% vs its 50-day average with a falling 20-day — broad market is weak, momentum longs fail more often here",
            "size_multiplier": 0.0,
            "allow_new_buys": False,
        }
    return {
        "regime": "neutral",
        "detail": f"SPY is {pct_vs_ma50:+.1f}% vs its 50-day average, trend is mixed — trading smaller",
        "size_multiplier": 0.5,
        "allow_new_buys": True,
    }


# ---------------------------------------------------------------- news
# Deliberately simple keyword scoring. This is NOT real NLP and shouldn't be
# mistaken for it — it's a crude catalyst detector that answers "is there
# obviously good/bad news attached to this move, or nothing at all."
POSITIVE_WORDS = [
    "beat", "beats", "surge", "surges", "record", "upgrade", "upgraded", "raises",
    "raised", "strong", "growth", "wins", "awarded", "approval", "approved",
    "partnership", "expands", "outperform", "buy rating", "breakthrough", "profit",
    "tops", "exceeds", "rally", "soars", "jumps",
]
NEGATIVE_WORDS = [
    "miss", "misses", "plunge", "plunges", "downgrade", "downgraded", "cuts", "cut",
    "weak", "decline", "lawsuit", "investigation", "probe", "recall", "warns",
    "warning", "loss", "losses", "underperform", "sell rating", "resign", "layoffs",
    "falls", "slumps", "drops", "sinks", "halts", "delays",
]


def news_sentiment(symbol, days_back=3):
    """Scores recent headlines for a ticker.

    Returns a dict:
      score: -1.0 to +1.0 (0 if no news or unavailable)
      label: "positive" | "negative" | "mixed" | "no news" | "unavailable"
      headline_count / detail
    """
    if not FINNHUB_KEY:
        return {"score": 0.0, "label": "unavailable", "headline_count": 0,
                "detail": "no Finnhub key set — news check skipped"}

    frm = (TODAY - datetime.timedelta(days=days_back)).isoformat()
    to = TODAY.isoformat()
    try:
        r = requests.get(
            f"{FINNHUB_BASE}/company-news",
            params={"symbol": symbol, "from": frm, "to": to, "token": FINNHUB_KEY},
            timeout=20,
        )
        if not r.ok:
            return {"score": 0.0, "label": "unavailable", "headline_count": 0,
                    "detail": f"news request failed ({r.status_code})"}
        articles = r.json()
    except Exception as e:
        return {"score": 0.0, "label": "unavailable", "headline_count": 0,
                "detail": f"news request error ({e})"}

    if not isinstance(articles, list) or not articles:
        return {"score": 0.0, "label": "no news", "headline_count": 0,
                "detail": "no recent headlines — move isn't backed by an obvious catalyst"}

    pos = neg = 0
    for a in articles[:25]:
        text = f"{a.get('headline','')} {a.get('summary','')}".lower()
        pos += sum(1 for w in POSITIVE_WORDS if w in text)
        neg += sum(1 for w in NEGATIVE_WORDS if w in text)

    total = pos + neg
    if total == 0:
        return {"score": 0.0, "label": "no news", "headline_count": len(articles),
                "detail": f"{len(articles)} headlines but no clear positive/negative language"}

    score = (pos - neg) / total
    if score > 0.25:
        label = "positive"
    elif score < -0.25:
        label = "negative"
    else:
        label = "mixed"

    return {
        "score": round(score, 2),
        "label": label,
        "headline_count": len(articles),
        "detail": f"{len(articles)} headlines, {pos} positive / {neg} negative signals → {label}",
    }


# ---------------------------------------------------------------- earnings
def earnings_soon(symbol, days_ahead=2):
    """True if the company reports within the next `days_ahead` days.

    Opening a momentum position right before earnings is a coin flip on a binary
    event, not a momentum trade — so the bot skips those setups entirely.
    """
    if not FINNHUB_KEY:
        return {"soon": False, "detail": "no Finnhub key — earnings check skipped"}

    frm = TODAY.isoformat()
    to = (TODAY + datetime.timedelta(days=days_ahead)).isoformat()
    try:
        r = requests.get(
            f"{FINNHUB_BASE}/calendar/earnings",
            params={"from": frm, "to": to, "symbol": symbol, "token": FINNHUB_KEY},
            timeout=20,
        )
        if not r.ok:
            return {"soon": False, "detail": f"earnings request failed ({r.status_code})"}
        cal = r.json().get("earningsCalendar", [])
    except Exception as e:
        return {"soon": False, "detail": f"earnings request error ({e})"}

    if cal:
        when = cal[0].get("date", "soon")
        return {"soon": True, "detail": f"reports earnings {when} — skipping, that's a coin flip not a momentum setup"}
    return {"soon": False, "detail": "no earnings in the next few days"}


# ---------------------------------------------------------------- fundamentals
def fundamentals_check(symbol):
    """A soft sanity filter on company health.

    Deliberately lenient — this exists to catch obviously deteriorating companies,
    not to pick stocks. Anything it can't determine passes.
    """
    if not FINNHUB_KEY:
        return {"ok": True, "label": "unknown", "detail": "no Finnhub key — fundamentals check skipped"}

    try:
        r = requests.get(
            f"{FINNHUB_BASE}/stock/metric",
            params={"symbol": symbol, "metric": "all", "token": FINNHUB_KEY},
            timeout=20,
        )
        if not r.ok:
            return {"ok": True, "label": "unknown", "detail": f"fundamentals request failed ({r.status_code})"}
        metrics = (r.json() or {}).get("metric", {})
    except Exception as e:
        return {"ok": True, "label": "unknown", "detail": f"fundamentals request error ({e})"}

    if not metrics:
        return {"ok": True, "label": "unknown", "detail": "no fundamentals data available"}

    concerns = []
    strengths = []

    rev_growth = metrics.get("revenueGrowthTTMYoy")
    if isinstance(rev_growth, (int, float)):
        if rev_growth < -15:
            concerns.append(f"revenue down {rev_growth:.0f}% YoY")
        elif rev_growth > 10:
            strengths.append(f"revenue up {rev_growth:.0f}% YoY")

    margin = metrics.get("netProfitMarginTTM")
    if isinstance(margin, (int, float)):
        if margin < -20:
            concerns.append(f"deeply unprofitable ({margin:.0f}% net margin)")
        elif margin > 10:
            strengths.append(f"healthy {margin:.0f}% net margin")

    debt_eq = metrics.get("totalDebt/totalEquityQuarterly")
    if isinstance(debt_eq, (int, float)) and debt_eq > 3:
        concerns.append(f"heavy debt load (debt/equity {debt_eq:.1f})")

    # ETFs and index funds legitimately have none of these metrics
    if not concerns and not strengths:
        return {"ok": True, "label": "unknown", "detail": "no meaningful fundamentals (normal for ETFs)"}

    if len(concerns) >= 2:
        return {"ok": False, "label": "weak", "detail": "; ".join(concerns)}
    if concerns:
        return {"ok": True, "label": "mixed", "detail": "; ".join(concerns)}
    return {"ok": True, "label": "strong", "detail": "; ".join(strengths) if strengths else "no red flags"}


# ---------------------------------------------------------------- combined
def evaluate(symbol, regime):
    """Runs every context check for one ticker and produces a single verdict
    plus a confidence score used for position sizing.

    Returns:
      allow: bool — should the bot take this trade at all
      confidence: 0.0-1.5 multiplier on position size
      reasons: list of human-readable strings explaining the decision
    """
    reasons = []
    confidence = 1.0
    allow = True

    # earnings is a hard veto — never open into a binary event
    earn = earnings_soon(symbol)
    if earn["soon"]:
        return {"allow": False, "confidence": 0.0, "reasons": [earn["detail"]],
                "news": None, "fundamentals": None}

    news = news_sentiment(symbol)
    if news["label"] == "negative":
        allow = False
        reasons.append(f"news is negative ({news['detail']}) — not buying strength into bad news")
    elif news["label"] == "positive":
        confidence += 0.3
        reasons.append(f"news supports the move ({news['detail']})")
    elif news["label"] == "no news":
        confidence -= 0.2
        reasons.append("no clear catalyst behind the move — sizing down")
    else:
        reasons.append(f"news {news['label']}")

    fund = fundamentals_check(symbol)
    if not fund["ok"]:
        allow = False
        reasons.append(f"fundamentals look weak ({fund['detail']})")
    elif fund["label"] == "strong":
        confidence += 0.2
        reasons.append(f"fundamentals solid ({fund['detail']})")
    elif fund["label"] == "mixed":
        confidence -= 0.1
        reasons.append(f"fundamentals mixed ({fund['detail']})")

    # regime scales everything
    confidence *= regime["size_multiplier"]
    if not regime["allow_new_buys"]:
        allow = False
        reasons.append(regime["detail"])

    confidence = max(0.0, min(1.5, confidence))
    return {"allow": allow, "confidence": round(confidence, 2), "reasons": reasons,
            "news": news, "fundamentals": fund}
