"""
Historical word-usage data from Google Books Ngram Viewer.

Uses the same JSON endpoint the public Ngram Viewer's own web page
calls (books.google.com/ngrams/json) - not a documented, supported
Google API (there's no official public Ngrams REST API), just the
internal endpoint their own frontend happens to use. It could change or
disappear without notice, so every call here is wrapped to fail soft
(returns None) rather than break the page if that happens.

Separate module from frequency.py deliberately: frequency.py's
wordfreq lookup is a local, offline, always-available snapshot;
usage_trend() here is a live network call to an unofficial endpoint,
with a real chance of being unavailable - callers should treat the two
very differently (frequency.difficulty() as always-on, this as
best-effort/optional).
"""

import requests
import streamlit as st

NGRAMS_URL = "https://books.google.com/ngrams/json"


@st.cache_data(ttl=None, show_spinner=False)
def usage_trend(word: str, year_start: int = 1800, year_end: int = 2019) -> dict | None:
    """Returns {"years": [...], "per_million": [...]} - one point per
    year, each roughly "how many times per million words of published
    English this word appeared that year" (Google's raw figures are a
    fraction of total words; *1,000,000 here for a more readable
    number) - or None if Google has no data for this word (multi-word
    phrases, invented/typo'd words, or the endpoint being unreachable
    all land here the same way, since there's no way to tell them
    apart from the response alone).

    Cached indefinitely (ttl=None) - historical Ngram data for a given
    word doesn't change, so there's no reason to ever re-fetch it
    within a running session."""
    try:
        resp = requests.get(
            NGRAMS_URL,
            params={
                "content": word.strip().lower(),
                "year_start": year_start,
                "year_end": year_end,
                "corpus": "en-2019",
                "smoothing": 3,
            },
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if not data or not data[0].get("timeseries"):
        return None
    series = data[0]["timeseries"]
    return {
        "years": list(range(year_start, year_end + 1)),
        "per_million": [v * 1_000_000 for v in series],
    }


# A swing smaller than this over the trailing window reads as "flat"
# rather than a misleadingly confident "rising"/"falling" - Ngram data is
# noisy year to year, and without a floor a word like "nevertheless"
# (+1.4% over 2000-2019 - real, but not a trend) would get called
# "Rising" the same as a word that's genuinely doubled in use.
FLAT_THRESHOLD_PCT = 10.0


def trend_summary(trend: dict, window_years: int = 20) -> dict:
    """Given a usage_trend() result, returns the peak year/value, the
    lowest year/value, and a plain-language read on the most recent
    window_years of data: "rising", "falling", or "flat", plus the
    percent change and the two endpoint values backing that call.

    Peak/lowest scan the entire series (not just the trailing window) -
    for most words that's the same order-of-magnitude era the chart
    already shows, and scanning the whole thing is simpler than picking
    a second, different window to justify."""
    years = trend["years"]
    values = trend["per_million"]
    peak_i = max(range(len(values)), key=lambda i: values[i])
    low_i = min(range(len(values)), key=lambda i: values[i])

    end_idx = len(years) - 1
    start_idx = max(0, end_idx - (window_years - 1))
    start_value = values[start_idx]
    end_value = values[end_idx]
    pct_change = ((end_value - start_value) / start_value * 100) if start_value else 0.0

    if pct_change > FLAT_THRESHOLD_PCT:
        direction = "rising"
    elif pct_change < -FLAT_THRESHOLD_PCT:
        direction = "falling"
    else:
        direction = "flat"

    return {
        "peak_year": years[peak_i],
        "peak_value": values[peak_i],
        "low_year": years[low_i],
        "low_value": values[low_i],
        "direction": direction,
        "pct_change": pct_change,
        "window_start_year": years[start_idx],
        "window_end_year": years[end_idx],
        "window_start_value": start_value,
        "window_end_value": end_value,
    }
