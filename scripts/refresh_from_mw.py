"""
One-off: re-run every existing word through the new Merriam-Webster-backed
dictionary.lookup_word() and overwrite its definition/part_of_speech/
example/synonyms/phonetic/audio_url. Needed because the old free API
(api.dictionaryapi.dev, Wiktionary-backed) picked senses in Wiktionary's
historical order rather than by commonness - e.g. "sanguine" was saved as
the archaic noun sense ("blood colour; red") instead of the everyday
adjective sense ("optimistic, confident"). See dictionary.py's module
docstring for the full story.

Uses db.add_word's upsert, which preserves the spaced-repetition schedule
fields (repetition/ease_factor/interval_days/next_review_date/date_added)
via COALESCE - this does not reset anyone's quiz progress, only the
reference content.

Run from the vocapp directory: python scripts/refresh_from_mw.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db
import dictionary

words = [w["word"] for w in db.get_all_words()]
changed, unchanged, failed = [], [], []
for word in words:
    try:
        old = db.get_word(word)
        info = dictionary.lookup_word(word)
        db.add_word(
            word, info["definition"], info["part_of_speech"], info["example"],
            info["synonyms"], info["phonetic"], info["audio_url"],
        )
        if old["definition"] != info["definition"] or old["part_of_speech"] != info["part_of_speech"]:
            changed.append(word)
            print(f"  CHANGED: {word}")
            print(f"    was: ({old['part_of_speech']}) {old['definition']}")
            print(f"    now: ({info['part_of_speech']}) {info['definition']}")
        else:
            unchanged.append(word)
    except dictionary.LookupNotFound:
        failed.append(word)
        print(f"  NO MW ENTRY: {word} - left as-is")
    except Exception as e:
        failed.append(word)
        print(f"  ERROR: {word} - {e} - left as-is")
    time.sleep(0.1)

print(f"\n{len(changed)} definitions changed, {len(unchanged)} already matched, {len(failed)} failed (left untouched).")
if failed:
    print("Failed:", ", ".join(failed))
