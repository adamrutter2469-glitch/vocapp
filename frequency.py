"""
Word difficulty via the `wordfreq` package - local and offline (no API
call, no network, no rate limit, unlike dictionary.py's MW lookups).

Uses "Zipf frequency": a log-scale commonality score, roughly 0-8, where
higher means more common (7+ is a function word like "the"; 0 means
wordfreq has no data for the word at all - typos, proper nouns, truly
obscure terms). See https://pypi.org/project/wordfreq/ for the scale's
exact definition.

Tier thresholds are calibrated against this app's actual word list, not
picked in the abstract - a sample across the existing vocab (circumspect,
mellifluous, tepid, sanguine, obviate, ...) clusters tightly at Zipf
~1.2-2.9, well below everyday words like "step"/"train" (~5), with
almost nothing landing in 3-4. Buckets are sized so that cluster - where
this app's real content actually lives - gets more resolution than the
"common word" end, which barely ever shows up here.
"""

from wordfreq import zipf_frequency

# (min Zipf score, label, Streamlit markdown color name), descending -
# first threshold the score clears wins. See module docstring for how
# these were chosen.
_TIERS = [
    (5.0, "Common", "green"),
    (4.0, "Familiar", "blue"),
    (3.0, "Intermediate", "violet"),
    (2.0, "Advanced", "orange"),
    (0.0, "Rare", "red"),
]


def difficulty(word: str) -> tuple[str, str, float]:
    """Returns (tier_label, streamlit_color_name, zipf_score) for `word`."""
    score = zipf_frequency(word.strip().lower(), "en")
    for threshold, label, color in _TIERS:
        if score >= threshold:
            return label, color, score
    return _TIERS[-1][1], _TIERS[-1][2], score  # unreachable (last threshold is 0.0) - kept defensively
