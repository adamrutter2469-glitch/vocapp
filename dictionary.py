"""
Dictionary auto-lookup - fills in definition/part of speech/example/
synonyms/pronunciation for the Add Word form so the user doesn't have to
type all of that by hand.

Uses Merriam-Webster's Collegiate Dictionary + Collegiate Thesaurus APIs
(both free, non-commercial tier, dictionaryapi.com). Replaced the earlier
free api.dictionaryapi.dev (Wiktionary-backed) implementation: that API
lists senses in Wiktionary's historical/etymological order, not by how
common a sense actually is - confirmed live, "mellifluous" returned
"Flowing like honey" (a rare literal sense) as definition #1 ahead of the
common "having a smooth rich flow (of sound)" sense, and "sanguine"
returned the archaic *noun* sense ("blood colour; red") as the first
match ahead of the everyday adjective sense ("optimistic, confident").
MW's Collegiate Dictionary is editorially curated and lists the most
current/common sense first - confirmed live for both of the words above
before switching.

Add Word is lookup-only (no manual definition entry) - see app.py's Add
Word tab.
"""

import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()

DICT_URL = "https://www.dictionaryapi.com/api/v3/references/collegiate/json/{}"
THESAURUS_URL = "https://www.dictionaryapi.com/api/v3/references/thesaurus/json/{}"
AUDIO_BASE_URL = "https://media.merriam-webster.com/audio/prons/en/us/mp3"


class LookupNotFound(Exception):
    pass


def _get_keys() -> tuple[str, str]:
    dict_key = os.environ.get("MW_DICTIONARY_KEY")
    thesaurus_key = os.environ.get("MW_THESAURUS_KEY")
    if not dict_key:
        raise RuntimeError(
            "MW_DICTIONARY_KEY not set - add it to .env (see "
            "dictionaryapi.com/register for a free Collegiate Dictionary key)."
        )
    return dict_key, thesaurus_key


def _matching_entries(data: list, word: str) -> list[dict]:
    """MW returns a flat list mixing real entries (dicts) with, when there's
    no exact match, plain suggestion strings. Keep only dict entries whose
    headword is this exact word (drops phrase entries like 'sanguine
    temperament', and drops homograph entries for *other* words entirely -
    keeps 'sanguine:1', 'sanguine:2', etc.). List order is MW's own
    editorial ranking, most-common sense first - not re-sorted here."""
    w = word.strip().lower()
    return [
        e for e in data
        if isinstance(e, dict) and e.get("meta", {}).get("id", "").split(":")[0].lower() == w
    ]


def _find_runon(data: list, word: str) -> tuple[dict, dict] | None:
    """Some words (e.g. 'idiosyncratic', 'anachronistic') aren't their own
    headword in MW's Collegiate Dictionary - they're 'undefined run-ons'
    nested under a base entry (idiosyncrasy, anachronism), sharing that
    entry's definition but carrying their own part of speech/pronunciation/
    audio. Returns (base_entry, runon) if word matches one of these."""
    w = word.strip().lower()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        for runon in entry.get("uros", []):
            if runon.get("ure", "").replace("*", "").lower() == w:
                return entry, runon
    return None


def _audio_url(filename: str) -> str:
    """Per MW's documented rule (dictionaryapi.com/products/json): 'bix' ->
    bix/, 'gg' -> gg/, leading digit/punctuation -> number/, else the
    filename's first letter."""
    if filename.startswith("bix"):
        subdir = "bix"
    elif filename.startswith("gg"):
        subdir = "gg"
    elif not filename[0].isalpha():
        subdir = "number"
    else:
        subdir = filename[0]
    return f"{AUDIO_BASE_URL}/{subdir}/{filename}.mp3"


_TOKEN_RE = re.compile(r"\{/?\w+[^}]*\}")  # strips MW markup like {wi}...{/wi}, {bc}, {it}...{/it}


def _clean(text: str) -> str:
    return _TOKEN_RE.sub("", text).strip()


def _examples(entry: dict, limit: int = 2) -> list[str]:
    """Verbal-illustration (usage example) sentences for the entry's first
    sense that has any, up to `limit` - not every sense has more than one,
    some have none. Stops at the first sense with hits so examples always
    belong to the sense _sense_groups()[0] actually shows (not a later
    sense)."""
    for def_block in entry.get("def", []):
        for sense_group in def_block.get("sseq", []):
            for sense in sense_group:
                if sense[0] != "sense":
                    continue
                for token_kind, token_val in sense[1].get("dt", []):
                    if token_kind == "vis" and token_val:
                        return [_clean(v["t"]) for v in token_val[:limit]]
    return []


def _count_leaf_senses(nodes: list) -> int:
    """Counts leaf sense entries in a sense-sequence GROUP - a "bs" (base
    sense) or plain "sense" each count as one, a "pseq" (parenthesized
    sub-sequence) recurses into its own nested entries. This walks the
    same nodes in the same order MW's own `shortdef` generation does,
    which is what makes _sense_groups' slice-by-count trick line up."""
    count = 0
    for node_type, node_data in nodes:
        if node_type in ("bs", "sense"):
            count += 1
        elif node_type == "pseq":
            count += _count_leaf_senses(node_data)
    return count


def _sense_groups(entry: dict, limit: int = 3) -> list[str]:
    """One combined string per top-level sense GROUP (MW's own sseq
    grouping), instead of MW's flat `shortdef` field.

    shortdef flattens every leaf sense - including lettered sub-senses
    ("a", "b", ...) nested under a shared base sense - into separate
    top-level strings with no indication they're related. A base sense
    that reads "...such as" (introducing the sub-senses that complete
    it) then shows up as its own dangling entry, with those sub-senses
    right after as if they were independent definitions rather than
    completions of it - confirmed live for "step".

    This counts how many shortdef entries belong to each top-level
    sseq group (sseq and shortdef visit leaf senses in the same order),
    then slices and re-joins shortdef's own strings by that count -
    deliberately NOT re-extracting text from sseq's raw dt tokens
    directly. An earlier version did that and silently dropped words:
    MW's dt markup includes tokens like {a_link|circumstances} where
    the real word is an argument *inside* the tag, not plain text next
    to a simple open/close wrapper - naively stripping "everything in
    {}" (the right approach for the simpler {wi}...{/wi}-style markup
    in example sentences, see _examples) took the word with it.
    shortdef is MW's own already-correctly-resolved text, so reusing it
    verbatim sidesteps having to reimplement MW's full markup language.
    One consequence: since shortdef itself only goes as deep as MW
    chose to generate (for "step" that's exactly sense 1's 3 leaves,
    with nothing left over for sense 2/3), this can end up returning
    fewer than `limit` groups - that's correct, not a bug pulling in
    ungenerated text isn't possible here regardless of method."""
    shortdefs = entry.get("shortdef", [])
    if not shortdefs:
        return []
    groups: list[str] = []
    pos = 0
    for def_block in entry.get("def", []):
        for sense_group in def_block.get("sseq", []):
            if len(groups) >= limit or pos >= len(shortdefs):
                return groups
            n = _count_leaf_senses(sense_group)
            if n == 0:
                continue
            groups.append("; ".join(shortdefs[pos:pos + n]))
            pos += n
    return groups


def _synonyms_and_antonyms(word: str, part_of_speech: str, thesaurus_key: str, limit: int = 10) -> tuple[list[str], list[str]]:
    """Best-effort - thesaurus lookup failing (missing key, 404, network
    error) shouldn't block the definition lookup that already succeeded.
    Returns (synonyms, antonyms), each up to `limit` words. One request
    covers both (MW's thesaurus entry carries syns and ants together),
    where a separate call per list would double the network cost for
    no reason."""
    if not thesaurus_key:
        return [], []
    try:
        resp = requests.get(THESAURUS_URL.format(word.strip().lower()), params={"key": thesaurus_key}, timeout=8)
        resp.raise_for_status()
        entries = _matching_entries(resp.json(), word)
        # Prefer the thesaurus entry for the same part of speech as the
        # definition we picked, so synonyms/antonyms match the sense
        # shown - falls back to the first entry if MW's fl labels don't
        # line up.
        entry = next((e for e in entries if e.get("fl") == part_of_speech), entries[0] if entries else None)
        if not entry:
            return [], []
        meta = entry.get("meta", {})
        # Each is a list of GROUPS (one per thesaurus sense) - [0] is
        # the sense MW ranks first, matching the same "first sense wins"
        # principle _examples() and _sense_groups() already use for
        # definitions, so synonyms/antonyms line up with the meaning
        # actually shown rather than some other sense of the word.
        syn_groups = meta.get("syns", [])
        ant_groups = meta.get("ants", [])
        synonyms = syn_groups[0][:limit] if syn_groups else []
        antonyms = ant_groups[0][:limit] if ant_groups else []
        return synonyms, antonyms
    except (requests.RequestException, ValueError, KeyError, IndexError):
        return [], []


def _etymology(entry: dict) -> str:
    """Cleaned word-origin text from the entry's "et" field, if MW has
    one - not every word does (obscure coinages, some proper-noun-
    derived words, etc. often lack it)."""
    texts = [c for k, v in entry.get("et", []) if k == "text" and (c := _clean(v))]
    return "; ".join(texts)


def lookup_word(word: str) -> dict:
    """Returns {definition, part_of_speech, example, examples, synonyms,
    antonyms, etymology, phonetic, audio_url}. Raises LookupNotFound if
    MW has no entry for this word, or requests.RequestException on a
    network/timeout failure - callers should catch both and fall back to
    manual entry.

    `definition` holds up to 3 of MW's top-level senses (see
    _sense_groups - a base sense and its lettered sub-senses count as
    one, not one each), in MW's own editorial ranking order (most-common
    first - see module docstring), joined by "\\n". Most words only have
    one sense, so this is a no-op for them; callers that split on "\\n"
    get a 1-element list back either way, which is how app.py and
    grading.py tell "one sense" from "several" apart without a separate
    flag."""
    dict_key, thesaurus_key = _get_keys()
    resp = requests.get(DICT_URL.format(word.strip().lower()), params={"key": dict_key}, timeout=8)
    resp.raise_for_status()
    data = resp.json()
    if not data or isinstance(data[0], str):
        raise LookupNotFound(f"No dictionary entry found for '{word}'.")

    entries = _matching_entries(data, word)
    if entries:
        # First matching entry = MW's own top-ranked sense (see module docstring).
        entry = entries[0]
        part_of_speech = entry.get("fl", "")
        prs_list = entry.get("hwi", {}).get("prs", [])
    else:
        runon_match = _find_runon(data, word)
        if not runon_match:
            raise LookupNotFound(f"No dictionary entry found for '{word}'.")
        # Run-on form: definition/example come from the base entry (a
        # run-on has no definition of its own), but part of speech and
        # pronunciation are the run-on's own where MW provides them.
        entry, runon = runon_match
        part_of_speech = runon.get("fl", "") or entry.get("fl", "")
        prs_list = runon.get("prs") or entry.get("hwi", {}).get("prs", [])

    senses = _sense_groups(entry)
    if not senses:
        raise LookupNotFound(f"No definitions found for '{word}'.")

    phonetic = f"/{prs_list[0]['mw']}/" if prs_list and prs_list[0].get("mw") else ""
    audio_filename = next((p["sound"]["audio"] for p in prs_list if p.get("sound", {}).get("audio")), "")
    audio_url = _audio_url(audio_filename) if audio_filename else ""
    examples = _examples(entry)
    synonyms, antonyms = _synonyms_and_antonyms(word, part_of_speech, thesaurus_key)

    return {
        "definition": "\n".join(senses),
        "part_of_speech": part_of_speech,
        # Singular - what gets saved to the words table (schema has one
        # example column; unchanged since before this rewrite).
        "example": examples[0] if examples else "",
        # Plural - every example we found (up to 2), for the richer Add
        # Word preview card. Not persisted.
        "examples": examples,
        "synonyms": synonyms,
        "antonyms": antonyms,
        "etymology": _etymology(entry),
        "phonetic": phonetic,
        "audio_url": audio_url,
    }
