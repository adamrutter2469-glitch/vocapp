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

Fields still land in editable inputs before saving - auto-lookup fills the
form, it doesn't bypass it.
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


def _first_example(entry: dict) -> str:
    """First verbal-illustration (usage example) sentence for the entry's
    first sense, if MW included one - not every sense has one."""
    for def_block in entry.get("def", []):
        for sense_group in def_block.get("sseq", []):
            for sense in sense_group:
                if sense[0] != "sense":
                    continue
                for token_kind, token_val in sense[1].get("dt", []):
                    if token_kind == "vis" and token_val:
                        return _clean(token_val[0]["t"])
    return ""


def _synonyms(word: str, part_of_speech: str, thesaurus_key: str) -> list[str]:
    """Best-effort - thesaurus lookup failing (missing key, 404, network
    error) shouldn't block the definition lookup that already succeeded."""
    if not thesaurus_key:
        return []
    try:
        resp = requests.get(THESAURUS_URL.format(word.strip().lower()), params={"key": thesaurus_key}, timeout=8)
        resp.raise_for_status()
        entries = _matching_entries(resp.json(), word)
        # Prefer the thesaurus entry for the same part of speech as the
        # definition we picked, so synonyms match the sense shown - falls
        # back to the first entry if MW's fl labels don't line up.
        entry = next((e for e in entries if e.get("fl") == part_of_speech), entries[0] if entries else None)
        if not entry:
            return []
        syn_groups = entry.get("meta", {}).get("syns", [])
        return syn_groups[0][:3] if syn_groups else []
    except (requests.RequestException, ValueError, KeyError, IndexError):
        return []


def lookup_word(word: str) -> dict:
    """Returns {definition, part_of_speech, example, synonyms, phonetic,
    audio_url}. Raises LookupNotFound if MW has no entry for this word,
    or requests.RequestException on a network/timeout failure - callers
    should catch both and fall back to manual entry."""
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

    shortdefs = entry.get("shortdef", [])
    if not shortdefs:
        raise LookupNotFound(f"No definitions found for '{word}'.")

    phonetic = f"/{prs_list[0]['mw']}/" if prs_list and prs_list[0].get("mw") else ""
    audio_filename = next((p["sound"]["audio"] for p in prs_list if p.get("sound", {}).get("audio")), "")
    audio_url = _audio_url(audio_filename) if audio_filename else ""

    return {
        "definition": shortdefs[0],
        "part_of_speech": part_of_speech,
        "example": _first_example(entry),
        "synonyms": _synonyms(word, part_of_speech, thesaurus_key),
        "phonetic": phonetic,
        "audio_url": audio_url,
    }
