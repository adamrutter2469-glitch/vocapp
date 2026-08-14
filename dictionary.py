"""
Phase 2: dictionary auto-lookup. Type a word, hit "Look up", and the
definition/part of speech/example/synonyms fields fill in automatically -
per the project plan: "You shouldn't have to manually enter all of that."

Uses the free dictionaryapi.dev API (no key required). Fields still land
in editable inputs before saving - auto-lookup fills the form, it doesn't
bypass it, since the free API is occasionally thin or wrong for less
common words.
"""

import requests

API_URL = "https://api.dictionaryapi.dev/api/v2/entries/en/{}"


class LookupNotFound(Exception):
    pass


def lookup_word(word: str) -> dict:
    """Returns {definition, part_of_speech, example, synonyms, phonetic}.
    Raises LookupNotFound if the API has nothing for this word, or
    requests.RequestException on a network/timeout failure - callers
    should catch both and fall back to manual entry."""
    resp = requests.get(API_URL.format(word.strip().lower()), timeout=8)
    if resp.status_code == 404:
        raise LookupNotFound(f"No dictionary entry found for '{word}'.")
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise LookupNotFound(f"No dictionary entry found for '{word}'.")

    entry = data[0]
    phonetic = entry.get("phonetic", "") or next(
        (p.get("text", "") for p in entry.get("phonetics", []) if p.get("text")), ""
    )

    meanings = entry.get("meanings", [])
    if not meanings:
        raise LookupNotFound(f"No definitions found for '{word}'.")

    # Primary definition/part of speech/example: first meaning, first
    # definition - the API doesn't rank senses by commonness, but this
    # is consistently the most-relevant one in practice.
    first_meaning = meanings[0]
    first_def = first_meaning.get("definitions", [{}])[0]

    # Synonyms: dedup across every meaning/definition, not just the first -
    # a word's other senses often carry useful synonyms too.
    synonyms: list[str] = []
    for m in meanings:
        synonyms.extend(m.get("synonyms", []))
        for d in m.get("definitions", []):
            synonyms.extend(d.get("synonyms", []))
    seen = set()
    deduped_synonyms = []
    for s in synonyms:
        if s.lower() not in seen:
            seen.add(s.lower())
            deduped_synonyms.append(s)

    return {
        "definition": first_def.get("definition", ""),
        "part_of_speech": first_meaning.get("partOfSpeech", ""),
        "example": first_def.get("example", ""),
        "synonyms": deduped_synonyms[:8],  # cap - long tails are usually noise
        "phonetic": phonetic,
    }
