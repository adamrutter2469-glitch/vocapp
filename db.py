"""
DuckDB storage layer for vocapp.

Schema (multi-user, see _migrate_legacy_single_user_schema for how this
came from the original single-user shape):
  word_content   - one row per distinct word, SHARED across every user.
                    Dictionary content (definition/synonyms/etymology/...)
                    is the same regardless of who looked it up, so this
                    is cached once - a friend adding a word you already
                    have reuses this row instead of a second MW lookup.
  user_words     - one row per (user, word): THIS user's own "my word
                    list" membership plus their own SM-2 schedule state.
                    Two different users studying the same word have two
                    independent rows here, each on their own schedule.
  quiz_attempts  - one row per graded quiz attempt, tagged with user_id -
                    so accuracy history persists permanently per user.

DB file lives at vocab.duckdb, next to this script.
"""

import random
import threading
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
# rather than SQL, converts to this zone. Every user of this app is
# assumed to be in this zone (a handful of friends, not a public app) -
# a real per-user timezone setting would be the next thing to add if
# that stops being true.
LOCAL_TZ = ZoneInfo("America/Chicago")

# Who all data created before the multi-user migration belonged to - the
# app had exactly one user before this schema existed. See
# _migrate_legacy_single_user_schema.
_LEGACY_OWNER_EMAIL = "adamrutter2469@gmail.com"


def _today_local():
    return datetime.now(LOCAL_TZ).date()


def today_local():
    """Public wrapper - app.py's Progress tab needs "today" (in the
    app's one fixed timezone, see LOCAL_TZ above) to compute a "last 30
    days" window, without reaching into this module's private helper."""
    return _today_local()


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
# retries are exhausted. This also happens to help with the OTHER source
# of the same IOException now that several people can use the app at
# once - two people's writes landing close together on Streamlit Cloud's
# one shared process. Fine at "a handful of friends" scale; if this ever
# needs to handle real concurrent load, the right fix is a single
# long-lived shared connection (or a real multi-writer database) instead
# of opening/closing a new one per call, not a bigger retry count here.
_CONNECT_RETRIES = 5
_CONNECT_RETRY_DELAY_SECONDS = 0.2

# _ensure_schema's DDL only needs to actually run once per process -
# guarded so it does, rather than re-running on every single
# get_connection() call (previously every one, of which a single script
# rerun triggers many - one per db.py function call). That mattered
# because it's a real, reproducible crash, not just a theoretical one:
# confirmed live, two concurrent Streamlit sessions in this one process
# each opening their own connection and racing to run this DDL raised
# `_duckdb.TransactionException: Catalog write-write conflict on alter
# with "word_content"`. The lock's own double-checked-locking shape
# (check, acquire, check again) is what keeps a second thread that was
# already blocked on the lock from redundantly re-running the DDL the
# first thread just finished, once it gets its turn.
_schema_lock = threading.Lock()
_schema_ready = False


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
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        _create_schema(con)
        _schema_ready = True


def _create_schema(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS word_content (
            word            TEXT PRIMARY KEY,
            definition      TEXT NOT NULL,
            part_of_speech  TEXT,
            example         TEXT,
            synonyms        TEXT,
            phonetic        TEXT,
            audio_url       TEXT,
            antonyms        TEXT,
            etymology       TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS user_words (
            user_id           TEXT NOT NULL,
            word              TEXT NOT NULL REFERENCES word_content(word),
            date_added        TIMESTAMP NOT NULL,
            repetition        INTEGER DEFAULT 0,
            ease_factor       DOUBLE DEFAULT 2.5,
            interval_days     INTEGER DEFAULT 0,
            next_review_date  DATE DEFAULT CURRENT_DATE,
            PRIMARY KEY (user_id, word)
        )
    """)
    con.execute("CREATE SEQUENCE IF NOT EXISTS attempt_id_seq START 1")
    con.execute("""
        CREATE TABLE IF NOT EXISTS quiz_attempts (
            id            INTEGER PRIMARY KEY DEFAULT nextval('attempt_id_seq'),
            user_id       TEXT NOT NULL,
            word          TEXT NOT NULL REFERENCES word_content(word),
            attempt_date  TIMESTAMP NOT NULL,
            your_answer   TEXT NOT NULL,
            accuracy      INTEGER NOT NULL,
            got_right     TEXT,   -- newline-joined bullet points
            got_missed    TEXT,   -- newline-joined bullet points
            note          TEXT
        )
    """)
    # One row per user - the Settings page (see app.py). alias is
    # capped at 10 chars by the UI's max_chars, not enforced here too -
    # this table isn't touched by anything else that could put a longer
    # value in it. auto_add_community_words/share_progress are plain
    # settings storage only, for now - neither one has an actual effect
    # yet (auto-adding from a shared community word list, and a way for
    # others to see your progress, are both still-unbuilt features).
    # daily_word_target feeds the Progress tab's streak card (see
    # get_quiz_streak) - how many words/day counts as "kept the streak
    # going", user-editable instead of the flat 10 it used to be.
    con.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id                   TEXT PRIMARY KEY,
            alias                     TEXT,
            auto_add_community_words  BOOLEAN DEFAULT FALSE,
            share_progress            BOOLEAN DEFAULT FALSE,
            daily_word_target         INTEGER DEFAULT 10
        )
    """)
    # daily_word_target was added after this table already existed in
    # deployed DBs - see the same-shaped app_ideas migration below for
    # why this needs its own ALTER (CREATE TABLE IF NOT EXISTS is a
    # no-op against an existing table) and why it can't carry NOT NULL.
    con.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS daily_word_target INTEGER DEFAULT 10")
    con.execute("CREATE SEQUENCE IF NOT EXISTS app_idea_id_seq START 1")
    con.execute("""
        CREATE TABLE IF NOT EXISTS app_ideas (
            id            INTEGER PRIMARY KEY DEFAULT nextval('app_idea_id_seq'),
            user_id       TEXT NOT NULL,
            idea_text     TEXT NOT NULL,
            submitted_at  TIMESTAMP NOT NULL,
            idea_type     TEXT NOT NULL DEFAULT 'Improvement',
            status        TEXT NOT NULL DEFAULT 'Submitted'
        )
    """)
    # idea_type/status were added after this table already existed in
    # deployed DBs - CREATE TABLE IF NOT EXISTS above is a no-op against
    # those, so the columns need adding explicitly too. IF NOT EXISTS
    # here makes this safe to run against a fresh DB as well, where the
    # CREATE TABLE just created them already.
    # No NOT NULL here (unlike the CREATE TABLE above) - DuckDB's ALTER
    # TABLE ADD COLUMN doesn't support adding a column with a constraint
    # (confirmed live: raises "Adding columns with constraints not yet
    # supported"). The DEFAULT alone is enough in practice - every row
    # is written through add_app_idea(), which always supplies both.
    con.execute("ALTER TABLE app_ideas ADD COLUMN IF NOT EXISTS idea_type TEXT DEFAULT 'Improvement'")
    con.execute("ALTER TABLE app_ideas ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'Submitted'")
    _migrate_legacy_single_user_schema(con)


def _migrate_legacy_single_user_schema(con):
    """One-time migration from the pre-multiuser schema (a single `words`
    table carrying both dictionary content AND this app's one-and-only
    SM-2 schedule, plus a `quiz_attempts` table with no user_id column)
    into the word_content/user_words split above.

    Runs on every connection but is a no-op after the first successful
    run - guarded by checking for the legacy `words` table AND the
    absence of its own backup, so a second run (e.g. a fresh container
    on the next redeploy) doesn't try to re-migrate data that's already
    been moved and had `words` renamed out of the way. The legacy tables
    are kept around renamed rather than dropped - cheap insurance
    against a migration bug, costs nothing to leave them.

    All migrated data is attributed to _LEGACY_OWNER_EMAIL - correct,
    since the app had exactly one user (that account) for everything
    created before this migration existed."""
    tables = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()}
    if "words" not in tables or "words_pre_multiuser_backup" in tables:
        return

    con.execute("""
        INSERT INTO word_content (word, definition, part_of_speech, example, synonyms,
                                   phonetic, audio_url, antonyms, etymology)
        SELECT word, definition, part_of_speech, example, synonyms,
               phonetic, audio_url, antonyms, etymology
        FROM words
    """)
    con.execute("""
        INSERT INTO user_words (user_id, word, date_added, repetition, ease_factor,
                                 interval_days, next_review_date)
        SELECT ?, word, date_added, repetition, ease_factor, interval_days, next_review_date
        FROM words
    """, [_LEGACY_OWNER_EMAIL])

    # quiz_attempts: _ensure_schema's CREATE TABLE IF NOT EXISTS above
    # was a no-op for a legacy DB (a table by that name already existed,
    # just under the old shape with no user_id column) - detect that and
    # move its data into the new shape via rename + recreate + reinsert,
    # rather than fighting DuckDB's limited ALTER-constraint support (no
    # clean way to repoint an existing FK from words(word) to
    # word_content(word) in place).
    cols = {r[1] for r in con.execute("PRAGMA table_info('quiz_attempts')").fetchall()}
    if "user_id" not in cols:
        con.execute("ALTER TABLE quiz_attempts RENAME TO quiz_attempts_legacy")
        con.execute("""
            CREATE TABLE quiz_attempts (
                id            INTEGER PRIMARY KEY DEFAULT nextval('attempt_id_seq'),
                user_id       TEXT NOT NULL,
                word          TEXT NOT NULL REFERENCES word_content(word),
                attempt_date  TIMESTAMP NOT NULL,
                your_answer   TEXT NOT NULL,
                accuracy      INTEGER NOT NULL,
                got_right     TEXT,
                got_missed    TEXT,
                note          TEXT
            )
        """)
        con.execute("""
            INSERT INTO quiz_attempts (id, user_id, word, attempt_date, your_answer,
                                        accuracy, got_right, got_missed, note)
            SELECT id, ?, word, attempt_date, your_answer, accuracy, got_right, got_missed, note
            FROM quiz_attempts_legacy
        """, [_LEGACY_OWNER_EMAIL])
        # Dropped rather than kept as a renamed backup (unlike `words`
        # below) - it still holds a foreign key pointing at `words`,
        # which blocks renaming `words` out of the way while anything
        # still references it (confirmed live: DuckDB's ALTER TABLE
        # RENAME refuses with a DependencyException in exactly this
        # case). Safe to drop outright rather than work around that:
        # every row was just copied into the new quiz_attempts table
        # above, with nothing lost.
        con.execute("DROP TABLE quiz_attempts_legacy")

    con.execute("ALTER TABLE words RENAME TO words_pre_multiuser_backup")


def add_word(user_id: str, word: str, definition: str, part_of_speech: str = "", example: str = "",
             synonyms: list[str] | None = None, phonetic: str = "", audio_url: str = "",
             antonyms: list[str] | None = None, etymology: str = ""):
    """Upsert, in two parts:

    word_content (shared dictionary data) always gets the latest lookup
    written over whatever was there - a correction from any user fixes
    it for everyone, same as the old single-user "re-adding a word
    overwrites its definition" behavior, just no longer tied to one
    user's row.

    user_words (this user's own list membership + schedule) is ON
    CONFLICT DO NOTHING, not DO UPDATE - re-adding a word you already
    have is a content correction (handled above), not a reason to reset
    YOUR review schedule or date_added. Was previously "ON CONFLICT DO
    UPDATE SET (everything except the schedule columns)" on one shared
    table; splitting the two concerns into two tables/statements makes
    that same rule simpler to see at a glance instead of requiring an
    exclusion list."""
    con = get_connection()
    w = word.strip()
    con.execute(
        """
        INSERT INTO word_content
            (word, definition, part_of_speech, example, synonyms, phonetic, audio_url,
             antonyms, etymology)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (word) DO UPDATE SET
            definition = EXCLUDED.definition,
            part_of_speech = EXCLUDED.part_of_speech,
            example = EXCLUDED.example,
            synonyms = EXCLUDED.synonyms,
            phonetic = EXCLUDED.phonetic,
            audio_url = EXCLUDED.audio_url,
            antonyms = EXCLUDED.antonyms,
            etymology = EXCLUDED.etymology
        """,
        [w, definition.strip(), part_of_speech.strip(), example.strip(),
         ", ".join(synonyms) if synonyms else "", phonetic.strip(), audio_url.strip(),
         ", ".join(antonyms) if antonyms else "", etymology.strip()],
    )
    con.execute(
        """
        INSERT INTO user_words (user_id, word, date_added, repetition, ease_factor, interval_days, next_review_date)
        VALUES (?, ?, ?, 0, 2.5, 0, ?)
        ON CONFLICT (user_id, word) DO NOTHING
        """,
        [user_id, w, datetime.now(timezone.utc), _today_local()],
    )
    con.close()
    r2_storage.upload_db()


def set_audio_url(word: str, audio_url: str):
    """Backfill helper - update just the audio clip for an existing word
    without touching its definition or anything else. No user_id: audio
    is dictionary content (word_content), shared like everything else
    there."""
    con = get_connection()
    con.execute("UPDATE word_content SET audio_url = ? WHERE word = ?", [audio_url.strip(), word])
    con.close()
    r2_storage.upload_db()


def delete_word(user_id: str, word: str):
    """Removes the word from THIS user's list and quiz history only -
    word_content (the shared dictionary cache) is left alone, since
    another user may still have the same word in their own list. A
    word_content row with no user_words referencing it just sits there
    unused afterward - harmless, not worth an extra query to garbage-
    collect it."""
    con = get_connection()
    con.execute("DELETE FROM quiz_attempts WHERE user_id = ? AND word = ?", [user_id, word])
    con.execute("DELETE FROM user_words WHERE user_id = ? AND word = ?", [user_id, word])
    con.close()
    r2_storage.upload_db()


def get_all_words(user_id: str):
    """This user's words joined with attempt stats: times_quizzed,
    avg_accuracy, last_quizzed - scoped to their own quiz_attempts only,
    even though the word itself may exist in other users' lists too."""
    con = get_connection()
    rows = con.execute("""
        SELECT
            wc.word, wc.definition, wc.part_of_speech, wc.example, wc.synonyms, wc.phonetic,
            wc.audio_url, wc.antonyms, wc.etymology, uw.date_added, uw.next_review_date,
            uw.interval_days, uw.repetition,
            COUNT(a.id)                    AS times_quizzed,
            ROUND(AVG(a.accuracy), 1)      AS avg_accuracy,
            MAX(a.attempt_date)            AS last_quizzed
        FROM user_words uw
        JOIN word_content wc ON wc.word = uw.word
        LEFT JOIN quiz_attempts a ON a.word = uw.word AND a.user_id = uw.user_id
        WHERE uw.user_id = ?
        GROUP BY wc.word, wc.definition, wc.part_of_speech, wc.example, wc.synonyms, wc.phonetic,
                 wc.audio_url, wc.antonyms, wc.etymology, uw.date_added, uw.next_review_date,
                 uw.interval_days, uw.repetition
        ORDER BY uw.date_added DESC
    """, [user_id]).fetchall()
    cols = [d[0] for d in con.description]
    con.close()
    return [dict(zip(cols, r)) for r in rows]


def get_word(user_id: str, word: str):
    """Dictionary content for `word`, but only if it's actually in THIS
    user's list (word_content existing globally - e.g. a friend already
    added it - doesn't count; this answers "is it in MY list", same as
    the old single-user version answered "does it exist at all")."""
    con = get_connection()
    row = con.execute(
        """SELECT wc.word, wc.definition, wc.part_of_speech, wc.example, wc.synonyms, wc.phonetic,
                  wc.audio_url, wc.antonyms, wc.etymology
           FROM user_words uw JOIN word_content wc ON wc.word = uw.word
           WHERE uw.user_id = ? AND uw.word = ?""",
        [user_id, word],
    ).fetchone()
    con.close()
    if row is None:
        return None
    return {
        "word": row[0], "definition": row[1], "part_of_speech": row[2],
        "example": row[3], "synonyms": row[4], "phonetic": row[5], "audio_url": row[6],
        "antonyms": row[7], "etymology": row[8],
    }


def random_word(user_id: str):
    """A random word from this user's list. Returns None if their deck
    is empty. Superseded by next_due_word() for normal quizzing
    (Phase 3) - kept as a building block / fallback."""
    con = get_connection()
    rows = con.execute("SELECT word FROM user_words WHERE user_id = ?", [user_id]).fetchall()
    con.close()
    return random.choice(rows)[0] if rows else None


def next_due_word(user_id: str):
    """A random word among whichever of THIS user's words are due per
    the spaced-repetition schedule - not "the most overdue," deliberately:
    with 50 words due on a given day, always serving strict
    next_review_date/date_added order made the deck feel like it was
    replaying in the same fixed (effectively alphabetical, since that's
    how date_added tended to sort) sequence every time. The due-ness
    gate itself is untouched - this only randomizes WHICH of the due
    words comes up next, not whether a word counts as due. Returns None
    if nothing is due today.

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
        SELECT uw.word FROM user_words uw
        LEFT JOIN (
            SELECT word, MAX(attempt_date) AS last_today
            FROM quiz_attempts
            WHERE user_id = ? AND attempt_date >= ? AND attempt_date < ?
            GROUP BY word
        ) today ON today.word = uw.word
        WHERE uw.user_id = ? AND uw.next_review_date <= ?
        ORDER BY (today.word IS NOT NULL) ASC, today.last_today ASC, RANDOM()
        LIMIT 1
    """, [user_id, day_start, day_end, user_id, today]).fetchone()
    con.close()
    return row[0] if row else None


def soonest_upcoming(user_id: str):
    """(word, next_review_date) for whichever of THIS user's words comes
    due soonest, regardless of whether it's due yet - used for the "all
    caught up, quiz ahead of schedule anyway" fallback. Kept as the
    single genuinely soonest-due word (not randomized like
    next_due_word) - "ahead of schedule" only makes sense pointed at
    what's actually closest, not a random pick from the whole deck.
    Returns (None, None) if the deck is empty.

    Same "already quizzed today sorts last" rule as next_due_word() -
    without it, practice mode on a small deck could hand back the word
    you just answered, since a just-missed word's 1-day reschedule can
    easily be the earliest next_review_date in the whole deck."""
    today = _today_local()
    day_start, day_end = _local_day_utc_bounds(today)
    con = get_connection()
    row = con.execute("""
        SELECT uw.word, uw.next_review_date FROM user_words uw
        LEFT JOIN (
            SELECT word, MAX(attempt_date) AS last_today
            FROM quiz_attempts
            WHERE user_id = ? AND attempt_date >= ? AND attempt_date < ?
            GROUP BY word
        ) today ON today.word = uw.word
        WHERE uw.user_id = ?
        ORDER BY (today.word IS NOT NULL) ASC, today.last_today ASC,
                 uw.next_review_date ASC
        LIMIT 1
    """, [user_id, day_start, day_end, user_id]).fetchone()
    con.close()
    return (row[0], row[1]) if row else (None, None)


def update_schedule(user_id: str, word: str, accuracy: int):
    """SM-2-inspired spaced-repetition update, scoped to this user's own
    schedule for `word`. The AI grader returns a continuous 0-100
    accuracy score rather than SM-2's discrete 0-5 "quality" rating, so
    this maps accuracy onto quality buckets first, then applies the
    standard SM-2 interval/ease-factor update. Returns the new schedule
    so the caller can show "next review in N days."
    """
    con = get_connection()
    row = con.execute(
        "SELECT repetition, ease_factor, interval_days FROM user_words WHERE user_id = ? AND word = ?",
        [user_id, word],
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
        UPDATE user_words SET repetition = ?, ease_factor = ?, interval_days = ?, next_review_date = ?
        WHERE user_id = ? AND word = ?
        """,
        [repetition, ease_factor, interval_days, next_review_date, user_id, word],
    )
    con.close()
    r2_storage.upload_db()
    return {"repetition": repetition, "interval_days": interval_days, "next_review_date": next_review_date}


def get_progress_stats(user_id: str):
    """Total/Mastered/Learning/Needs Work counts + overall average
    accuracy for THIS user, per the project plan's progress dashboard.
    Every word falls into exactly one of the three buckets (including
    never-quizzed words, bucketed as Learning - they're in the pipeline,
    just untested):
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
            SELECT uw.word, uw.repetition AS rep, COUNT(a.id) AS n, AVG(a.accuracy) AS avg_accuracy
            FROM user_words uw LEFT JOIN quiz_attempts a ON a.word = uw.word AND a.user_id = uw.user_id
            WHERE uw.user_id = ?
            GROUP BY uw.word, uw.repetition
        ) stats
    """, [user_id]).fetchone()
    con.close()
    total, mastered, needs_work, overall_avg = row
    mastered = mastered or 0
    needs_work = needs_work or 0
    return {
        "total": total, "mastered": mastered, "needs_work": needs_work,
        "learning": total - mastered - needs_work, "overall_avg": overall_avg,
    }


def get_daily_accuracy_trend(user_id: str):
    """(date, avg_accuracy) per day across this user's attempts,
    bucketed by LOCAL_TZ (see its own comment - not the server's own UTC
    day) - the progress-over-time chart. Grouped in Python rather than
    `CAST(attempt_date AS DATE)`/`GROUP BY` in SQL - the row count here
    is small (one row per quiz attempt, ever), so fetching everything
    and bucketing it here costs nothing, and it keeps the timezone
    conversion out of SQL entirely (see LOCAL_TZ's comment on why)."""
    con = get_connection()
    rows = con.execute(
        "SELECT attempt_date, accuracy FROM quiz_attempts WHERE user_id = ?", [user_id]
    ).fetchall()
    con.close()
    by_day = {}
    for attempt_date, accuracy in rows:
        day = _to_local_date(attempt_date)
        by_day.setdefault(day, []).append(accuracy)
    return sorted((day, round(sum(vals) / len(vals), 1)) for day, vals in by_day.items())


def get_daily_words_quizzed_trend(user_id: str):
    """(date, total_attempts) per day for this user, bucketed by
    LOCAL_TZ - how much quizzing happened each day. Counts every
    attempt, not distinct words - quizzing the same word twice in one
    day (a miss seen again, a "No Clue" retry) counts twice, matching
    "how much quizzing did I do today" rather than "how many different
    words did I touch.\""""
    con = get_connection()
    rows = con.execute(
        "SELECT attempt_date FROM quiz_attempts WHERE user_id = ?", [user_id]
    ).fetchall()
    con.close()
    by_day = {}
    for (attempt_date,) in rows:
        day = _to_local_date(attempt_date)
        by_day[day] = by_day.get(day, 0) + 1
    return sorted(by_day.items())


def get_quiz_streak(user_id: str, threshold: int = 10):
    """This user's current streak of consecutive LOCAL_TZ days with at
    least `threshold` words quizzed, walking backward from today.

    Today doesn't break the streak just for being incomplete - it's
    still in progress, so if today hasn't hit the threshold yet, the
    walk starts from yesterday instead. From there, every day has to
    both clear the threshold AND be an unbroken run of calendar days
    (a day with zero attempts has no row at all, so a gap in the dict
    lookup - defaulting to 0 - ends the streak the same as a too-low
    count would)."""
    con = get_connection()
    rows = con.execute(
        "SELECT attempt_date FROM quiz_attempts WHERE user_id = ?", [user_id]
    ).fetchall()
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


def save_attempt(user_id: str, word: str, your_answer: str, accuracy: int, feedback: str):
    """got_right/got_missed (separate bullet lists) are superseded by a
    single feedback string with inline <right>/<wrong> tags (see
    grading.GradeResult) - the columns stay (older rows still have real
    data in them) but new attempts just write "" to both and put the
    whole tagged feedback in note, rather than a schema migration to
    drop two now-unused columns."""
    con = get_connection()
    con.execute(
        """
        INSERT INTO quiz_attempts (user_id, word, attempt_date, your_answer, accuracy, got_right, got_missed, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [user_id, word, datetime.now(timezone.utc), your_answer, accuracy, "", "", feedback],
    )
    con.close()
    r2_storage.upload_db()


def get_attempts(user_id: str, word: str):
    con = get_connection()
    rows = con.execute(
        """
        SELECT attempt_date, your_answer, accuracy, got_right, got_missed, note
        FROM quiz_attempts WHERE user_id = ? AND word = ? ORDER BY attempt_date DESC
        """,
        [user_id, word],
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


def get_user_settings(user_id: str) -> dict:
    """This user's saved Settings-page preferences. Defaults (blank
    alias, both toggles off, a 10-word daily target) for a user who's
    never saved any yet, rather than None/crashing - Settings' own UI
    relies on always getting a real dict back."""
    con = get_connection()
    row = con.execute(
        "SELECT alias, auto_add_community_words, share_progress, daily_word_target "
        "FROM user_settings WHERE user_id = ?",
        [user_id],
    ).fetchone()
    con.close()
    if row is None:
        return {"alias": "", "auto_add_community_words": False, "share_progress": False, "daily_word_target": 10}
    return {
        "alias": row[0] or "",
        "auto_add_community_words": bool(row[1]),
        "share_progress": bool(row[2]),
        # NULL for any row saved before this column existed, not just a
        # never-saved user (row is None above) - same 10-word fallback
        # either way.
        "daily_word_target": row[3] if row[3] is not None else 10,
    }


def save_user_settings(
    user_id: str, alias: str, auto_add_community_words: bool, share_progress: bool, daily_word_target: int = 10,
):
    con = get_connection()
    con.execute(
        """
        INSERT INTO user_settings (user_id, alias, auto_add_community_words, share_progress, daily_word_target)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (user_id) DO UPDATE SET
            alias = EXCLUDED.alias,
            auto_add_community_words = EXCLUDED.auto_add_community_words,
            share_progress = EXCLUDED.share_progress,
            daily_word_target = EXCLUDED.daily_word_target
        """,
        [user_id, alias.strip()[:10], auto_add_community_words, share_progress, daily_word_target],
    )
    con.close()
    r2_storage.upload_db()


def add_app_idea(user_id: str, idea_text: str, idea_type: str = "Improvement"):
    con = get_connection()
    con.execute(
        "INSERT INTO app_ideas (user_id, idea_text, submitted_at, idea_type, status) "
        "VALUES (?, ?, ?, ?, 'Submitted')",
        [user_id, idea_text.strip(), datetime.now(timezone.utc), idea_type],
    )
    con.close()
    r2_storage.upload_db()


def get_app_ideas(user_id: str) -> list[dict]:
    """This user's own submitted ideas, newest first - shown back to
    them on the App Ideas page so they can see what they've already
    suggested, including where each one stands (Submitted/Rejected/
    Completed)."""
    con = get_connection()
    rows = con.execute(
        "SELECT idea_text, submitted_at, idea_type, status FROM app_ideas "
        "WHERE user_id = ? ORDER BY submitted_at DESC",
        [user_id],
    ).fetchall()
    con.close()
    return [{"idea_text": r[0], "submitted_at": r[1], "idea_type": r[2], "status": r[3]} for r in rows]


def get_all_app_ideas() -> list[dict]:
    """Every user's submitted ideas, newest first - for the owner-only
    review section of the App Ideas page (see app.py), so ideas can be
    reviewed centrally without needing a separate admin tool. Includes
    the row id so the owner view can update an idea's status."""
    con = get_connection()
    rows = con.execute(
        "SELECT id, user_id, idea_text, submitted_at, idea_type, status "
        "FROM app_ideas ORDER BY submitted_at DESC"
    ).fetchall()
    con.close()
    return [
        {"id": r[0], "user_id": r[1], "idea_text": r[2], "submitted_at": r[3], "idea_type": r[4], "status": r[5]}
        for r in rows
    ]


def update_app_idea_status(idea_id: int, status: str):
    """Owner-only (enforced in app.py, not here) - marks an idea
    Submitted/Rejected/Completed once it's been reviewed or built."""
    con = get_connection()
    con.execute("UPDATE app_ideas SET status = ? WHERE id = ?", [status, idea_id])
    con.close()
    r2_storage.upload_db()
