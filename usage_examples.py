"""
Combines Merriam-Webster's usage examples (already fetched during Add Word
lookup - see dictionary.py's `examples` key, up to 2, drawn from MW's own
top-ranked sense) with additional examples from freedictionaryapi.com (a
free, no-key, Wiktionary-backed API) to get up to 3 usage-in-context
examples per word for the Add Word > Examples tab.

Why freedictionaryapi.com here despite dictionary.py's module docstring
explaining why Wiktionary-backed lookups were dropped for DEFINITIONS
(bad sense ordering - archaic/rare senses ahead of common ones): that
problem doesn't carry over to example SENTENCES. We're not asking
Wiktionary which sense is "the" definition (MW already answered that);
we're just borrowing extra sentences that use the word, filtered by
Wiktionary's own tags to skip the obsolete/archaic senses (see
_SKIP_TAGS) and preferring entries whose part of speech matches MW's.
"""

import requests

FREE_DICT_URL = "https://freedictionaryapi.com/api/v1/entries/en/{}"

# Senses tagged with any of these are skipped when pulling extra examples -
# Wiktionary lists obsolete/archaic/dialectal senses right alongside modern
# ones (confirmed live for "sanguine": sense #1 "blood red" is tagged
# literary, #2 an "obsolete" physiology sense - both come before the
# everyday "optimistic" sense #5). An example sentence for one of those
# reads as a different, no-longer-real word to a learner. Best-effort, not
# exhaustive - only catches Wiktionary's own tags.
_SKIP_TAGS = {"obsolete", "archaic", "dated", "rare", "literary", "dialectal"}


def _extra_examples(word: str, part_of_speech: str) -> list[str]:
    """Plain-text example sentences from freedictionaryapi.com, skipping
    tagged-obsolete/archaic/etc senses and preferring entries whose part of
    speech matches MW's (falls back to every entry if none match - the two
    APIs don't always label part of speech identically)."""
    resp = requests.get(FREE_DICT_URL.format(word.strip().lower()), timeout=8)
    resp.raise_for_status()
    data = resp.json()
    entries = data.get("entries", [])
    matching = [e for e in entries if e.get("partOfSpeech") == part_of_speech] or entries
    out = []
    for entry in matching:
        for sense in entry.get("senses", []):
            if _SKIP_TAGS & set(sense.get("tags", [])):
                continue
            for ex in sense.get("examples", []):
                ex = ex.strip()
                if ex:
                    out.append(ex)
    return out


def combined_examples(word: str, part_of_speech: str, mw_examples: list[str], limit: int = 3) -> list[str]:
    """Up to `limit` usage-in-context example sentences for `word`: MW's own
    (already fetched at lookup time) first, topped up with
    freedictionaryapi.com examples if MW didn't have enough. Best-effort -
    a freedictionaryapi.com failure (network, timeout, bad response) just
    means fewer/only-MW examples, never blocks the tab from rendering."""
    combined = list(mw_examples[:limit])
    if len(combined) >= limit:
        return combined
    seen = {e.strip().lower() for e in combined}
    try:
        extra = _extra_examples(word, part_of_speech)
    except (requests.RequestException, ValueError):
        return combined
    for ex in extra:
        if len(combined) >= limit:
            break
        key = ex.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        combined.append(ex)
    return combined
