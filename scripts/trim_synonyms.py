"""
One-off: dictionary.py used to cap synonyms at 8; now capped at 3. Existing
words already saved with more than 3 need trimming so the app is consistent
regardless of when a word was added. Doesn't re-hit the dictionary API -
just re-saves each word's own stored synonyms, first 3 only. Uses
db.add_word's upsert, which preserves the spaced-repetition schedule fields
via COALESCE, so this doesn't reset anyone's quiz progress.

Run from the vocapp directory: python scripts/trim_synonyms.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db

trimmed = 0
for w in db.get_all_words():
    current = [s.strip() for s in (w["synonyms"] or "").split(",") if s.strip()]
    if len(current) <= 3:
        continue
    db.add_word(
        w["word"], w["definition"], w["part_of_speech"], w["example"],
        current[:3], w["phonetic"], w.get("audio_url", ""),
    )
    trimmed += 1
    print(f"  trimmed: {w['word']}  ({len(current)} -> 3)")

print(f"\n{trimmed} word(s) trimmed to 3 synonyms.")
