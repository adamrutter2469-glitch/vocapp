"""One-off: fetch pronunciation audio URLs for words added before the
audio_url column existed. Doesn't touch definitions/examples/etc -
just fills in the one new field via db.set_audio_url()."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db
import dictionary

words = [w["word"] for w in db.get_all_words()]
found, missing = 0, 0
for word in words:
    try:
        info = dictionary.lookup_word(word)
        if info["audio_url"]:
            db.set_audio_url(word, info["audio_url"])
            found += 1
            print(f"  audio found: {word}")
        else:
            missing += 1
            print(f"  no audio clip: {word} - will use browser TTS")
    except Exception as e:
        missing += 1
        print(f"  lookup failed: {word} - {e}")
    time.sleep(0.3)

print(f"\n{found} words got a real audio clip, {missing} will fall back to browser text-to-speech.")
