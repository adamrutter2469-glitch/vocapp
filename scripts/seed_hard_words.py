"""
One-off: seed ~20 hard/GRE-level vocab words via the same dictionary
auto-lookup path the Add Word UI uses. Not part of the app itself - a
batch alternative to clicking "Look up" + "Add" twenty times by hand.

Run from the vocapp directory: python scripts/seed_hard_words.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db
import dictionary

WORDS = [
    "perspicacious", "mendacious", "obsequious", "perfunctory", "surreptitious",
    "vociferous", "taciturn", "ephemeral", "ubiquitous", "cacophony",
    "pernicious", "sycophant", "equanimity", "profligate", "recalcitrant",
    "inscrutable", "sanguine", "laconic", "capricious", "pusillanimous",
]

ok, failed = [], []
for word in WORDS:
    try:
        info = dictionary.lookup_word(word)
        db.add_word(
            word, info["definition"], info["part_of_speech"], info["example"],
            info["synonyms"], info["phonetic"],
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

print(f"\n{len(ok)} added, {len(failed)} failed.")
if failed:
    print("Failed (add these manually in the app):", ", ".join(failed))
