"""
DuckDB storage layer for vocapp.

Phase 1 schema, per the project plan:
  words          - one row per vocab word, definition typed in manually
                    (dictionary auto-lookup is Phase 2)
  quiz_attempts  - one row per graded quiz attempt, so accuracy history
                    persists permanently (the whole point, per the plan:
                    "store your actual definition attempts permanently")

DB file lives at vocab.duckdb, next to this script - local-only storage,
no server, matches the "develop locally first" plan.
"""

import duckdb
from pathlib import Path
from datetime import datetime, timezone

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


def add_word(word: str, definition: str, part_of_speech: str = "", example: str = ""):
    """Upsert - re-adding an existing word overwrites its definition, so
    corrections don't require deleting first."""
    con = get_connection()
    con.execute(
        """
        INSERT OR REPLACE INTO words (word, definition, part_of_speech, example, date_added)
        VALUES (?, ?, ?, ?, COALESCE((SELECT date_added FROM words WHERE word = ?), ?))
        """,
        [word.strip(), definition.strip(), part_of_speech.strip(), example.strip(),
         word.strip(), datetime.now(timezone.utc)],
    )
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
            w.word, w.definition, w.part_of_speech, w.example, w.date_added,
            COUNT(a.id)                    AS times_quizzed,
            ROUND(AVG(a.accuracy), 1)      AS avg_accuracy,
            MAX(a.attempt_date)            AS last_quizzed
        FROM words w
        LEFT JOIN quiz_attempts a ON a.word = w.word
        GROUP BY w.word, w.definition, w.part_of_speech, w.example, w.date_added
        ORDER BY w.date_added DESC
    """).fetchall()
    cols = [d[0] for d in con.description]
    con.close()
    return [dict(zip(cols, r)) for r in rows]


def get_word(word: str):
    con = get_connection()
    row = con.execute(
        "SELECT word, definition, part_of_speech, example FROM words WHERE word = ?",
        [word],
    ).fetchone()
    con.close()
    if row is None:
        return None
    return {"word": row[0], "definition": row[1], "part_of_speech": row[2], "example": row[3]}


def random_word():
    """A random word to quiz on. Returns None if the deck is empty."""
    con = get_connection()
    row = con.execute(
        "SELECT word FROM words USING SAMPLE 1"
    ).fetchone()
    con.close()
    return row[0] if row else None


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
