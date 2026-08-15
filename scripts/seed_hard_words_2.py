"""
One-off: seed 100 more hard/GRE-level vocab words via the same dictionary
auto-lookup path the Add Word UI uses. Second batch - see seed_hard_words.py
for the original ~20. Skips anything already in the database instead of
overwriting it, since a re-lookup could clobber a word the user has already
been quizzed on (repetition/schedule are preserved by db.add_word's upsert,
but there's no reason to touch existing rows here at all).

Run from the vocapp directory: python scripts/seed_hard_words_2.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db
import dictionary

WORDS = [
    "abstemious", "abstruse", "acerbic", "admonish", "alacrity",
    "ameliorate", "anachronistic", "antediluvian", "apocryphal", "arcane",
    "ascetic", "assiduous", "audacious", "avuncular", "baleful",
    "banal", "beguile", "bellicose", "bombastic", "bucolic",
    "cajole", "castigate", "chicanery", "circumlocution", "circumspect",
    "clandestine", "cogent", "complaisant", "conflagration", "contumacious",
    "convivial", "copious", "credulous", "cupidity", "dearth",
    "debacle", "decorous", "deleterious", "demagogue", "deride",
    "desultory", "diaphanous", "didactic", "diffident", "disparate",
    "dogmatic", "dour", "ebullient", "eclectic", "effrontery",
    "egregious", "eloquent", "empirical", "enervate", "ennui",
    "equivocate", "erudite", "esoteric", "euphemism", "execrable",
    "exigent", "extol", "facetious", "fatuous", "fecund",
    "felicitous", "fervid", "flagrant", "florid", "fortuitous",
    "fractious", "garrulous", "glib", "gregarious", "hackneyed",
    "harangue", "hegemony", "histrionic", "hubris", "iconoclast",
    "idiosyncratic", "ignominious", "immutable", "impecunious", "imperious",
    "implacable", "impertinent", "impetuous", "impervious", "impugn",
    "incendiary", "incongruous", "indefatigable", "ineffable", "inexorable",
    "ingenuous", "inimical", "iniquity", "insidious", "insolent",
]

existing = {w["word"].lower() for w in db.get_all_words()}

ok, skipped, failed = [], [], []
for word in WORDS:
    if word.lower() in existing:
        skipped.append(word)
        print(f"  skipped (already have it): {word}")
        continue
    try:
        info = dictionary.lookup_word(word)
        db.add_word(
            word, info["definition"], info["part_of_speech"], info["example"],
            info["synonyms"], info["phonetic"], info["audio_url"],
        )
        ok.append(word)
        print(f"  added: {word}  ({info['part_of_speech']})")
    except dictionary.LookupNotFound:
        failed.append(word)
        print(f"  NO ENTRY: {word}")
    except Exception as e:
        failed.append(word)
        print(f"  ERROR: {word} - {e}")
    time.sleep(0.3)  # polite pacing against a free public API

print(f"\n{len(ok)} added, {len(skipped)} already present, {len(failed)} failed.")
if failed:
    print("Failed (add these manually in the app):", ", ".join(failed))
