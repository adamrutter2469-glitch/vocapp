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

import duckdb
from pathlib import Path
from datetime import datetime, timezone, timedelta

DB_PATH = Path(__file__).parent / "vocab.duckdb"


def get_connection():
    con = duckdb.connect(str(DB_PATH))
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
             synonyms: list[str] | None = None, phonetic: str = "", audio_url: str = ""):
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
            (word, definition, part_of_speech, example, synonyms, phonetic, audio_url, date_added,
             repetition, ease_factor, interval_days, next_review_date)
        VALUES (?, ?, ?, ?, ?, ?, ?,
                COALESCE((SELECT date_added FROM words WHERE word = ?), ?),
                COALESCE((SELECT repetition FROM words WHERE word = ?), 0),
                COALESCE((SELECT ease_factor FROM words WHERE word = ?), 2.5),
                COALESCE((SELECT interval_days FROM words WHERE word = ?), 0),
                COALESCE((SELECT next_review_date FROM words WHERE word = ?), CURRENT_DATE))
        """,
        [w, definition.strip(), part_of_speech.strip(), example.strip(),
         ", ".join(synonyms) if synonyms else "", phonetic.strip(), audio_url.strip(),
         w, datetime.now(timezone.utc), w, w, w, w],
    )
    con.close()


def set_audio_url(word: str, audio_url: str):
    """Backfill helper - update just the audio clip for an existing word
    without touching its definition or anything else."""
    con = get_connection()
    con.execute("UPDATE words SET audio_url = ? WHERE word = ?", [audio_url.strip(), word])
    con.close()


def delete_word(word: str):
    con = get_connection()
    con.execute("DELETE FROM quiz_attempts WHERE word = ?", [word])
    con.execute("DELETE FROM words WHERE word = ?", [word])
    con.close()


def get_all_words():
    """Words joined with attempt stats: times_quizzed, avg_accuracy, last_quizzed."""
    con = get_connection()
    rows = con.execute("""
        SELECT
            w.word, w.definition, w.part_of_speech, w.example, w.synonyms, w.phonetic,
            w.audio_url, w.date_added, w.next_review_date, w.interval_days, w.repetition,
            COUNT(a.id)                    AS times_quizzed,
            ROUND(AVG(a.accuracy), 1)      AS avg_accuracy,
            MAX(a.attempt_date)            AS last_quizzed
        FROM words w
        LEFT JOIN quiz_attempts a ON a.word = w.word
        GROUP BY w.word, w.definition, w.part_of_speech, w.example, w.synonyms, w.phonetic,
                 w.audio_url, w.date_added, w.next_review_date, w.interval_days, w.repetition
        ORDER BY w.date_added DESC
    """).fetchall()
    cols = [d[0] for d in con.description]
    con.close()
    return [dict(zip(cols, r)) for r in rows]


def get_word(word: str):
    con = get_connection()
    row = con.execute(
        "SELECT word, definition, part_of_speech, example, synonyms, phonetic, audio_url FROM words WHERE word = ?",
        [word],
    ).fetchone()
    con.close()
    if row is None:
        return None
    return {
        "word": row[0], "definition": row[1], "part_of_speech": row[2],
        "example": row[3], "synonyms": row[4], "phonetic": row[5], "audio_url": row[6],
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
    """The most-overdue word per the spaced-repetition schedule - "which
    word am I most likely to forget right now," per the project plan.
    Returns None if nothing is due today."""
    con = get_connection()
    row = con.execute("""
        SELECT word FROM words
        WHERE next_review_date <= CURRENT_DATE
        ORDER BY next_review_date ASC, date_added ASC
        LIMIT 1
    """).fetchone()
    con.close()
    return row[0] if row else None


def soonest_upcoming():
    """(word, next_review_date) for whichever word comes due soonest,
    regardless of whether it's due yet - used for the "all caught up,
    quiz ahead of schedule anyway" fallback. Returns (None, None) if
    the deck is empty."""
    con = get_connection()
    row = con.execute(
        "SELECT word, next_review_date FROM words ORDER BY next_review_date ASC LIMIT 1"
    ).fetchone()
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

    next_review_date = datetime.now(timezone.utc).date() + timedelta(days=interval_days)
    con.execute(
        """
        UPDATE words SET repetition = ?, ease_factor = ?, interval_days = ?, next_review_date = ?
        WHERE word = ?
        """,
        [repetition, ease_factor, interval_days, next_review_date, word],
    )
    con.close()
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


def get_weak_words(limit: int = 10):
    """Words with the lowest average accuracy, among words quizzed at
    least once - "Which words am I struggling with?" per the plan."""
    con = get_connection()
    rows = con.execute("""
        SELECT w.word, ROUND(AVG(a.accuracy), 1) AS avg_accuracy, COUNT(a.id) AS n
        FROM words w JOIN quiz_attempts a ON a.word = w.word
        GROUP BY w.word
        ORDER BY avg_accuracy ASC
        LIMIT ?
    """, [limit]).fetchall()
    con.close()
    return [{"word": r[0], "avg_accuracy": r[1], "times_quizzed": r[2]} for r in rows]


def get_daily_accuracy_trend():
    """(date, avg_accuracy) per day across every attempt - the
    progress-over-time chart."""
    con = get_connection()
    rows = con.execute("""
        SELECT CAST(attempt_date AS DATE) AS day, ROUND(AVG(accuracy), 1) AS avg_accuracy
        FROM quiz_attempts
        GROUP BY day
        ORDER BY day
    """).fetchall()
    con.close()
    return rows


def save_attempt(word: str, your_answer: str, accuracy: int,
                  got_right: list[str], got_missed: list[str], note: str):
    con = get_connection()
    con.execute(
        """
        INSERT INTO quiz_attempts (word, attempt_date, your_answer, accuracy, got_right, got_missed, note)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [word, datetime.now(timezone.utc), your_answer, accuracy,
         "\n".join(got_right), "\n".join(got_missed), note],
    )
    con.close()


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
