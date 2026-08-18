"""
DuckDB storage layer for vocapp.

Schema, per the project plan:
  words          - one row per vocab word. Phase 2 adds synonyms/phonetic,
                    filled in by dictionary.py's auto-lookup - still
                    editable/overridable by hand.
  quiz_attempts  - one row per graded quiz attempt, so accuracy history
                    persists permanently (the whole point, per the plan:
                    "store your actual definition attempts permanently")

DB file lives at vocab.duckdb, next to this script - local-only storage,
no server, matches the "develop locally first" plan.
"""

import time
import duckdb
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import r2_storage

DB_PATH = Path(__file__).parent / "vocab.duckdb"

# The app's one fixed timezone for "what day is it" / "what day did this
# attempt happen on" - Streamlit Cloud's server runs on UTC, not the
# user's own clock, so relying on DuckDB's CURRENT_DATE (server time) or
# casting a stored timestamp straight to DATE silently buckets things by
# the WRONG day for anyone west of Greenwich - confirmed live: quiz
# counts were landing on a different day than the user expected. Every
# TIMESTAMP column still stores real UTC instants (unambiguous, correct
# storage practice) - only the "which day" logic below, done in Python
# rather than SQL, converts to this zone. Single-user personal app, so
# one hardcoded zone rather than a per-user setting.
LOCAL_TZ = ZoneInfo("America/Chicago")


def _today_local():
    return datetime.now(LOCAL_TZ).date()


def _local_day_utc_bounds(day):
    """[start, end) UTC instants spanning one full LOCAL_TZ calendar
    day - lets a "did this happen today" query stay a plain timestamp
    range comparison (which DuckDB handles natively) instead of needing
    a timezone-conversion SQL function (which needs DuckDB's icu
    extension, an extra thing that has to successfully install/load,
    including under whatever restricted environment a cloud deploy
    runs in - not worth the risk for something this fundamental)."""
    start_local = datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ)
    end_local = datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ) + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _to_local_date(dt):
    """A stored attempt_date/date_added comes back from DuckDB as a
    naive datetime - naive because the TIMESTAMP column itself has no
    timezone concept, but the value in it really is UTC (that's what
    every INSERT here writes) - so it's stamped UTC before converting,
    not just converted as if it were already LOCAL_TZ."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ).date()

# vocab.duckdb lives inside OneDrive's synced Documents folder, so every
# write (each quiz attempt, each word added) can get OneDrive to grab a
# brief file lock while it uploads the change - if a read lands in that
# same instant, duckdb.connect() raises IOException ("being used by
# another process") even though nothing in this app is holding the file.
# Retrying a few times with a short backoff rides out that window instead
# of surfacing it as a crash; a real, non-transient problem (missing
# file, corrupt DB, actual concurrent app instance) still raises once
# retries are exhausted.
_CONNECT_RETRIES = 5
_CONNECT_RETRY_DELAY_SECONDS = 0.2


def get_connection():
    # No-op after the first call in this process (see r2_storage's own
    # docstring) - pulling the R2 copy down before opening a connection
    # is what makes a freshly-started process (a Streamlit Cloud
    # container coming up after a redeploy, in particular) see the real
    # data instead of an empty local file.
    r2_storage.download_db()
    for attempt in range(_CONNECT_RETRIES):
        try:
            con = duckdb.connect(str(DB_PATH))
            break
        except duckdb.IOException:
            if attempt == _CONNECT_RETRIES - 1:
                raise
            time.sleep(_CONNECT_RETRY_DELAY_SECONDS * (attempt + 1))
    _ensure_schema(con)
    return con


def _ensure_schema(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS words (
            word            TEXT PRIMARY KEY,
            definition      TEXT NOT NULL,
            part_of_speech  TEXT,
            example         TEXT,
            date_added      TIMESTAMP NOT NULL
        )
    """)
    # Added in Phase 2 - IF NOT EXISTS makes this a safe no-op migration
    # against a database created under the Phase 1 schema.
    con.execute("ALTER TABLE words ADD COLUMN IF NOT EXISTS synonyms TEXT")
    con.execute("ALTER TABLE words ADD COLUMN IF NOT EXISTS phonetic TEXT")
    # Added for the Thesaurus/Advanced sub-tabs - antonyms alongside the
    # existing synonyms column, and etymology (blank for words saved
    # before this migration; only backfilled on a re-add/lookup).
    con.execute("ALTER TABLE words ADD COLUMN IF NOT EXISTS antonyms TEXT")
    con.execute("ALTER TABLE words ADD COLUMN IF NOT EXISTS etymology TEXT")
    # Real native-speaker pronunciation clip URL, when the dictionary API
    # has one for this word - empty string means fall back to browser TTS.
    con.execute("ALTER TABLE words ADD COLUMN IF NOT EXISTS audio_url TEXT")
    # Added in Phase 3 - SM-2-style spaced repetition state. DEFAULT
    # CURRENT_DATE on next_review_date means existing words (added before
    # this migration) become immediately due, same as a brand new word -
    # correct behavior, since they have no schedule yet either.
    con.execute("ALTER TABLE words ADD COLUMN IF NOT EXISTS repetition INTEGER DEFAULT 0")
    con.execute("ALTER TABLE words ADD COLUMN IF NOT EXISTS ease_factor DOUBLE DEFAULT 2.5")
    con.execute("ALTER TABLE words ADD COLUMN IF NOT EXISTS interval_days INTEGER DEFAULT 0")
    con.execute("ALTER TABLE words ADD COLUMN IF NOT EXISTS next_review_date DATE DEFAULT CURRENT_DATE")
    con.execute("CREATE SEQUENCE IF NOT EXISTS attempt_id_seq START 1")
    con.execute("""
        CREATE TABLE IF NOT EXISTS quiz_attempts (
            id            INTEGER PRIMARY KEY DEFAULT nextval('attempt_id_seq'),
            word          TEXT NOT NULL REFERENCES words(word),
            attempt_date  TIMESTAMP NOT NULL,
            your_answer   TEXT NOT NULL,
            accuracy      INTEGER NOT NULL,
            got_right     TEXT,   -- newline-joined bullet points
            got_missed    TEXT,   -- newline-joined bullet points
            note          TEXT
        )
    """)


def add_word(word: str, definition: str, part_of_speech: str = "", example: str = "",
             synonyms: list[str] | None = None, phonetic: str = "", audio_url: str = "",
             antonyms: list[str] | None = None, etymology: str = ""):
    """Upsert - re-adding an existing word overwrites its definition, so
    corrections don't require deleting first. Spaced-repetition schedule
    fields are preserved across a correction (COALESCE from the existing
    row), same as date_added - editing a definition shouldn't reset
    progress on that word."""
    con = get_connection()
    w = word.strip()
    con.execute(
        """
        INSERT OR REPLACE INTO words
            (word, definition, part_of_speech, example, synonyms, phonetic, audio_url,
             antonyms, etymology, date_added,
             repetition, ease_factor, interval_days, next_review_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                COALESCE((SELECT date_added FROM words WHERE word = ?), ?),
                COALESCE((SELECT repetition FROM words WHERE word = ?), 0),
                COALESCE((SELECT ease_factor FROM words WHERE word = ?), 2.5),
                COALESCE((SELECT interval_days FROM words WHERE word = ?), 0),
                COALESCE((SELECT next_review_date FROM words WHERE word = ?), ?))
        """,
        [w, definition.strip(), part_of_speech.strip(), example.strip(),
         ", ".join(synonyms) if synonyms else "", phonetic.strip(), audio_url.strip(),
         ", ".join(antonyms) if antonyms else "", etymology.strip(),
         w, datetime.now(timezone.utc), w, w, w, w, _today_local()],
    )
    con.close()
    r2_storage.upload_db()


def set_audio_url(word: str, audio_url: str):
    """Backfill helper - update just the audio clip for an existing word
    without touching its definition or anything else."""
    con = get_connection()
    con.execute("UPDATE words SET audio_url = ? WHERE word = ?", [audio_url.strip(), word])
    con.close()
    r2_storage.upload_db()


def delete_word(word: str):
    con = get_connection()
    con.execute("DELETE FROM quiz_attempts WHERE word = ?", [word])
    con.execute("DELETE FROM words WHERE word = ?", [word])
    con.close()
    r2_storage.upload_db()


def get_all_words():
    """Words joined with attempt stats: times_quizzed, avg_accuracy, last_quizzed."""
    con = get_connection()
    rows = con.execute("""
        SELECT
            w.word, w.definition, w.part_of_speech, w.example, w.synonyms, w.phonetic,
            w.audio_url, w.antonyms, w.etymology, w.date_added, w.next_review_date,
            w.interval_days, w.repetition,
            COUNT(a.id)                    AS times_quizzed,
            ROUND(AVG(a.accuracy), 1)      AS avg_accuracy,
            MAX(a.attempt_date)            AS last_quizzed
        FROM words w
        LEFT JOIN quiz_attempts a ON a.word = w.word
        GROUP BY w.word, w.definition, w.part_of_speech, w.example, w.synonyms, w.phonetic,
                 w.audio_url, w.antonyms, w.etymology, w.date_added, w.next_review_date,
                 w.interval_days, w.repetition
        ORDER BY w.date_added DESC
    """).fetchall()
    cols = [d[0] for d in con.description]
    con.close()
    return [dict(zip(cols, r)) for r in rows]


def get_word(word: str):
    con = get_connection()
    row = con.execute(
        """SELECT word, definition, part_of_speech, example, synonyms, phonetic, audio_url,
                  antonyms, etymology
           FROM words WHERE word = ?""",
        [word],
    ).fetchone()
    con.close()
    if row is None:
        return None
    return {
        "word": row[0], "definition": row[1], "part_of_speech": row[2],
        "example": row[3], "synonyms": row[4], "phonetic": row[5], "audio_url": row[6],
        "antonyms": row[7], "etymology": row[8],
    }


def random_word():
    """A random word to quiz on. Returns None if the deck is empty.
    Superseded by next_due_word() for normal quizzing (Phase 3) - kept
    as a building block / fallback."""
    con = get_connection()
    row = con.execute(
        "SELECT word FROM words USING SAMPLE 1"
    ).fetchone()
    con.close()
    return row[0] if row else None


def next_due_word():
    """A random word among whichever are due per the spaced-repetition
    schedule - not "the most overdue," deliberately: with 50 words due
    on a given day, always serving strict next_review_date/date_added
    order made the deck feel like it was replaying in the same fixed
    (effectively alphabetical, since that's how date_added tended to
    sort) sequence every time. The due-ness gate itself is untouched -
    this only randomizes WHICH of the due words comes up next, not
    whether a word counts as due. Returns None if nothing is due today.

    Among due words, one already quizzed today still sorts behind every
    due word that hasn't been - the SM-2 schedule alone doesn't always
    push a word past "due today" (a missed word gets interval_days=1,
    i.e. due again TOMORROW, not later today, so this isn't covering for
    a scheduling bug; it's for decks with only a couple of words due at
    once, where without this a just-answered word could otherwise be
    the only - or the random pick - thing left to show, landing it right
    back in front of you). If every due word has already been seen
    today, falls back to whichever was seen longest ago today, so a
    second pass still spreads out rather than looping the same word."""
    today = _today_local()
    day_start, day_end = _local_day_utc_bounds(today)
    con = get_connection()
    row = con.execute("""
        SELECT w.word FROM words w
        LEFT JOIN (
            SELECT word, MAX(attempt_date) AS last_today
            FROM quiz_attempts
            WHERE attempt_date >= ? AND attempt_date < ?
            GROUP BY word
        ) today ON today.word = w.word
        WHERE w.next_review_date <= ?
        ORDER BY (today.word IS NOT NULL) ASC, today.last_today ASC, RANDOM()
        LIMIT 1
    """, [day_start, day_end, today]).fetchone()
    con.close()
    return row[0] if row else None


def soonest_upcoming():
    """(word, next_review_date) for whichever word comes due soonest,
    regardless of whether it's due yet - used for the "all caught up,
    quiz ahead of schedule anyway" fallback. Kept as the single genuinely
    soonest-due word (not randomized like next_due_word) - "ahead of
    schedule" only makes sense pointed at what's actually closest, not
    a random pick from the whole deck. Returns (None, None) if the deck
    is empty.

    Same "already quizzed today sorts last" rule as next_due_word() -
    without it, practice mode on a small deck could hand back the word
    you just answered, since a just-missed word's 1-day reschedule can
    easily be the earliest next_review_date in the whole deck."""
    today = _today_local()
    day_start, day_end = _local_day_utc_bounds(today)
    con = get_connection()
    row = con.execute("""
        SELECT w.word, w.next_review_date FROM words w
        LEFT JOIN (
            SELECT word, MAX(attempt_date) AS last_today
            FROM quiz_attempts
            WHERE attempt_date >= ? AND attempt_date < ?
            GROUP BY word
        ) today ON today.word = w.word
        ORDER BY (today.word IS NOT NULL) ASC, today.last_today ASC,
                 w.next_review_date ASC
        LIMIT 1
    """, [day_start, day_end]).fetchone()
    con.close()
    return (row[0], row[1]) if row else (None, None)


def update_schedule(word: str, accuracy: int):
    """SM-2-inspired spaced-repetition update. The AI grader returns a
    continuous 0-100 accuracy score rather than SM-2's discrete 0-5
    "quality" rating, so this maps accuracy onto quality buckets first,
    then applies the standard SM-2 interval/ease-factor update. Returns
    the new schedule so the caller can show "next review in N days."
    """
    con = get_connection()
    row = con.execute(
        "SELECT repetition, ease_factor, interval_days FROM words WHERE word = ?", [word]
    ).fetchone()
    if row is None:
        con.close()
        return None
    repetition, ease_factor, interval_days = row
    repetition = repetition or 0
    ease_factor = ease_factor or 2.5
    interval_days = interval_days or 0

    if accuracy >= 90:
        quality = 5
    elif accuracy >= 70:
        quality = 4
    elif accuracy >= 40:
        quality = 3
    elif accuracy >= 20:
        quality = 2
    else:
        quality = 0

    if quality < 3:
        # Missed it - schedule resets, see it again tomorrow rather
        # than waiting out whatever long interval it had built up.
        repetition = 0
        interval_days = 1
    else:
        repetition += 1
        if repetition == 1:
            interval_days = 1
        elif repetition == 2:
            interval_days = 6
        else:
            interval_days = round(interval_days * ease_factor)
        ease_factor = max(1.3, ease_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)))

    next_review_date = _today_local() + timedelta(days=interval_days)
    con.execute(
        """
        UPDATE words SET repetition = ?, ease_factor = ?, interval_days = ?, next_review_date = ?
        WHERE word = ?
        """,
        [repetition, ease_factor, interval_days, next_review_date, word],
    )
    con.close()
    r2_storage.upload_db()
    return {"repetition": repetition, "interval_days": interval_days, "next_review_date": next_review_date}


def get_progress_stats():
    """Total/Mastered/Learning/Needs Work counts + overall average
    accuracy, per the project plan's progress dashboard. Every word
    falls into exactly one of the three buckets (including never-quizzed
    words, bucketed as Learning - they're in the pipeline, just untested):
      Mastered:   quizzed, repetition >= 3 (schedule has stretched out
                  several reviews) AND avg accuracy >= 80
      Needs Work: quizzed at least once, avg accuracy < 60
      Learning:   everything else
    """
    con = get_connection()
    row = con.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN stats.n > 0 AND stats.rep >= 3 AND stats.avg_accuracy >= 80 THEN 1 ELSE 0 END) AS mastered,
            SUM(CASE WHEN stats.n > 0 AND stats.avg_accuracy < 60 THEN 1 ELSE 0 END) AS needs_work,
            ROUND(AVG(CASE WHEN stats.n > 0 THEN stats.avg_accuracy END), 1) AS overall_avg
        FROM (
            SELECT w.word, w.repetition AS rep, COUNT(a.id) AS n, AVG(a.accuracy) AS avg_accuracy
            FROM words w LEFT JOIN quiz_attempts a ON a.word = w.word
            GROUP BY w.word, w.repetition
        ) stats
    """).fetchone()
    con.close()
    total, mastered, needs_work, overall_avg = row
    mastered = mastered or 0
    needs_work = needs_work or 0
    return {
        "total": total, "mastered": mastered, "needs_work": needs_work,
        "learning": total - mastered - needs_work, "overall_avg": overall_avg,
    }


def get_daily_accuracy_trend():
    """(date, avg_accuracy) per day across every attempt, bucketed by
    LOCAL_TZ (see its own comment - not the server's own UTC day) - the
    progress-over-time chart. Grouped in Python rather than
    `CAST(attempt_date AS DATE)`/`GROUP BY` in SQL - the row count here
    is small (one row per quiz attempt, ever), so fetching everything
    and bucketing it here costs nothing, and it keeps the timezone
    conversion out of SQL entirely (see LOCAL_TZ's comment on why)."""
    con = get_connection()
    rows = con.execute("SELECT attempt_date, accuracy FROM quiz_attempts").fetchall()
    con.close()
    by_day = {}
    for attempt_date, accuracy in rows:
        day = _to_local_date(attempt_date)
        by_day.setdefault(day, []).append(accuracy)
    return sorted((day, round(sum(vals) / len(vals), 1)) for day, vals in by_day.items())


def get_daily_words_quizzed_trend():
    """(date, total_attempts) per day, bucketed by LOCAL_TZ - how much
    quizzing happened each day. Counts every attempt, not distinct
    words - quizzing the same word twice in one day (a miss seen again,
    a "No Clue" retry) counts twice, matching "how much quizzing did I
    do today" rather than "how many different words did I touch.\""""
    con = get_connection()
    rows = con.execute("SELECT attempt_date FROM quiz_attempts").fetchall()
    con.close()
    by_day = {}
    for (attempt_date,) in rows:
        day = _to_local_date(attempt_date)
        by_day[day] = by_day.get(day, 0) + 1
    return sorted(by_day.items())


def get_quiz_streak(threshold: int = 10):
    """Current streak of consecutive LOCAL_TZ days with at least
    `threshold` words quizzed, walking backward from today.

    Today doesn't break the streak just for being incomplete - it's
    still in progress, so if today hasn't hit the threshold yet, the
    walk starts from yesterday instead. From there, every day has to
    both clear the threshold AND be an unbroken run of calendar days
    (a day with zero attempts has no row at all, so a gap in the dict
    lookup - defaulting to 0 - ends the streak the same as a too-low
    count would)."""
    con = get_connection()
    rows = con.execute("SELECT attempt_date FROM quiz_attempts").fetchall()
    con.close()
    if not rows:
        return 0
    counts = {}
    for (attempt_date,) in rows:
        day = _to_local_date(attempt_date)
        counts[day] = counts.get(day, 0) + 1
    today = _today_local()
    day = today if counts.get(today, 0) >= threshold else today - timedelta(days=1)
    streak = 0
    while counts.get(day, 0) >= threshold:
        streak += 1
        day -= timedelta(days=1)
    return streak


def save_attempt(word: str, your_answer: str, accuracy: int, feedback: str):
    """got_right/got_missed (separate bullet lists) are superseded by a
    single feedback string with inline <right>/<wrong> tags (see
    grading.GradeResult) - the columns stay (older rows still have real
    data in them) but new attempts just write "" to both and put the
    whole tagged feedback in note, rather than a schema migration to
    drop two now-unused columns."""
    con = get_connection()
    con.execute(
        """
        INSERT INTO quiz_attempts (word, attempt_date, your_answer, accuracy, got_right, got_missed, note)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [word, datetime.now(timezone.utc), your_answer, accuracy, "", "", feedback],
    )
    con.close()
    r2_storage.upload_db()


def get_attempts(word: str):
    con = get_connection()
    rows = con.execute(
        """
        SELECT attempt_date, your_answer, accuracy, got_right, got_missed, note
        FROM quiz_attempts WHERE word = ? ORDER BY attempt_date DESC
        """,
        [word],
    ).fetchall()
    con.close()
    return [
        {
            "attempt_date": r[0], "your_answer": r[1], "accuracy": r[2],
            "got_right": (r[3] or "").splitlines(), "got_missed": (r[4] or "").splitlines(),
            "note": r[5],
        }
        for r in rows
    ]
