"""
PostgreSQL storage layer for vocapp (hosted on Neon; connection string in
the DATABASE_URL environment variable - .env locally, Streamlit Cloud's
Secrets there, which also surfaces as env vars).

Moved here from a single DuckDB file synced whole-file through Cloudflare
R2: every write used to download and re-upload the entire database
(~16 MB, 10-20 seconds per save) and two processes could silently
overwrite each other's changes. A real server database makes each write
a few-millisecond row change and lets any number of sessions write at
once. The query code below is deliberately unchanged in shape - see
_Conn for the small wrapper that keeps the `con = get_connection();
con.execute(sql, params)...; con.close()` style working.

Schema (multi-user):
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
  user_settings, app_ideas, allowed_users - see _create_schema.
"""

import os
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import psycopg
from dotenv import load_dotenv
from psycopg.adapt import Loader
from psycopg.types.datetime import DatetimeNoTzDumper
from psycopg_pool import ConnectionPool

load_dotenv()

# The app's one fixed timezone for "what day is it" / "what day did this
# attempt happen on" - Streamlit Cloud's server runs on UTC, not the
# user's own clock, so relying on the database's CURRENT_DATE (server
# time) or casting a stored timestamp straight to DATE silently buckets
# things by the WRONG day for anyone west of Greenwich - confirmed live:
# quiz counts were landing on a different day than the user expected.
# Every TIMESTAMP column still stores real UTC instants (unambiguous,
# correct storage practice) - only the "which day" logic below, done in
# Python rather than SQL, converts to this zone. Every user of this app
# is assumed to be in this zone (a handful of friends, not a public app)
# - a real per-user timezone setting would be the next thing to add if
# that stops being true.
LOCAL_TZ = ZoneInfo("America/Chicago")

# Who all data created before the multi-user migration belonged to - the
# app had exactly one user before the schema was split per-user.
_LEGACY_OWNER_EMAIL = "adamrutter2469@gmail.com"

# Everyone who was in auth.ALLOWED_EMAILS when that moved into the
# allowed_users table - used only to seed the table the first time it's
# created (see _create_schema).
_SEED_ALLOWED_EMAILS = [
    "adamrutter2469@gmail.com",
    "riley.kaitlyn96@gmail.com",
    "bartelmealex@gmail.com",
]


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
    range comparison instead of needing a timezone-conversion SQL
    function."""
    start_local = datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ)
    end_local = datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ) + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _to_local_date(dt):
    """A stored attempt_date/date_added comes back as a naive datetime -
    naive because the TIMESTAMP column itself has no timezone concept,
    but the value in it really is UTC (that's what every INSERT here
    writes - see _UtcNaiveDatetimeDumper) - so it's stamped UTC before
    converting, not just converted as if it were already LOCAL_TZ."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ).date()


# ---------------------------------------------------------------------
# Connection layer
# ---------------------------------------------------------------------

class _UtcNaiveDatetimeDumper(DatetimeNoTzDumper):
    """Every datetime goes to the database as a naive UTC `timestamp`,
    whatever it arrives as. The code passes timezone-aware UTC datetimes
    (datetime.now(timezone.utc), _local_day_utc_bounds) and the columns
    are plain TIMESTAMP holding UTC - left alone, the driver would send
    aware values as timestamptz and let the SERVER's session timezone
    decide the conversion, which can't be pinned on a pooled connection
    (Neon's pooler rejects the `options` startup parameter and resets
    SET between transactions). Doing the conversion here removes that
    dependency entirely."""

    def upgrade(self, obj, format):
        # The driver asks a dumper registered for a type whether a more
        # specific one fits this particular value (that's how it normally
        # picks timestamptz vs timestamp); this one handles every
        # datetime itself, aware or naive, so it's already the answer.
        return self

    def dump(self, obj):
        if obj.tzinfo is not None:
            obj = obj.astimezone(timezone.utc).replace(tzinfo=None)
        return super().dump(obj)


class _NumericAsFloat(Loader):
    """AVG()/ROUND() come back from Postgres as `numeric` (Decimal in
    Python); everything downstream - formatting, pandas/altair, plain
    arithmetic mixed with floats - expects the float DuckDB used to
    return. Converting at the driver means no per-query ::float casts."""

    def load(self, data):
        return float(bytes(data).decode())


_pool = None
_pool_lock = threading.Lock()


def _configure(conn):
    conn.adapters.register_dumper(datetime, _UtcNaiveDatetimeDumper)
    conn.adapters.register_loader("numeric", _NumericAsFloat)


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            url = os.environ.get("DATABASE_URL")
            if not url:
                raise RuntimeError(
                    "DATABASE_URL not set - add the Postgres (Neon) connection "
                    "string to .env locally, or to Streamlit Cloud's Secrets."
                )
            pool = ConnectionPool(
                url,
                min_size=1,
                max_size=int(os.environ.get("DB_POOL_MAX", "8")),
                # autocommit: a plain read is ONE network round trip (no
                # BEGIN/COMMIT around it) - see _Conn for how writes get
                # a real transaction anyway. Round trips are what cost
                # time on a hosted database (~65ms each from a home
                # connection), where a local file cost nothing.
                # prepare_threshold=None: no server-side prepared
                # statements - Neon's pooler (PgBouncer, transaction
                # mode) hands each transaction a possibly-different
                # backend connection, where a statement prepared on an
                # earlier one doesn't exist.
                kwargs={"prepare_threshold": None, "autocommit": True},
                configure=_configure,
                # Neon suspends an idle database after ~5 minutes and
                # drops its connections. Retiring ours well before that
                # (max_idle) keeps a dead one from being handed out; the
                # old per-checkout validity ping (check=) cost a whole
                # extra round trip on EVERY call, so it's gone - the
                # one-shot retry in _Conn.execute covers the rare stale
                # connection that still slips through.
                max_idle=180,
                max_lifetime=1800,
                open=False,
            )
            pool.open(wait=True, timeout=30)
            _pool = pool
    return _pool


_READ_ONLY_PREFIXES = ("select", "show")

# DB_TRACE=1 prints every statement and how long it took to stderr - for
# counting how many round trips a page load makes (each costs real time
# against a hosted database).
_TRACE = bool(os.environ.get("DB_TRACE"))


class _Conn:
    """The tiny slice of DuckDB's connection API this module's functions
    use - execute(sql, params) returning something with fetchone()/
    fetchall(), .description, .close() - on top of a pooled psycopg
    connection, so the data functions below didn't need rewriting.

    - `?` placeholders are translated to psycopg's `%s` (and literal `%`
      escaped) in execute().
    - Reads (SELECT) run bare: one round trip, no transaction. The
      FIRST statement that isn't a read opens a transaction (BEGIN) and
      everything from there to close() is committed together - so a
      multi-statement write (delete_word, add_word, initialize_new_user)
      is atomic, where DuckDB autocommitted each statement separately.
      (Reads that come BEFORE the first write in the same function, like
      update_schedule's lookup of the current schedule, are outside that
      transaction - fine at this app's scale: one session's own
      word-schedule isn't contended.)
    - If the connection turns out to be dead on its very first statement
      (a stale pooled socket after the database was suspended), it's
      swapped for a fresh one and the statement retried once.
    - If a caller raises before close(), __del__ rolls back and returns
      the connection to the pool rather than leaking it."""

    def __init__(self, pool, raw):
        self._pool = pool
        self._raw = raw
        self._cur = None
        self._in_tx = False
        self._ran_any = False

    @staticmethod
    def _translate(sql, params):
        if params is None:
            return sql
        return sql.replace("%", "%%").replace("?", "%s")

    def begin(self):
        if not self._in_tx:
            self._raw.execute("BEGIN")
            self._in_tx = True

    def commit(self):
        if self._in_tx:
            self._raw.execute("COMMIT")
            self._in_tx = False

    def execute(self, sql, params=None):
        if not self._in_tx and not sql.lstrip()[:6].lower().startswith(_READ_ONLY_PREFIXES):
            self._begin_with_retry()
        translated = self._translate(sql, params)
        _t0 = time.perf_counter() if _TRACE else 0.0
        try:
            self._cur = self._raw.execute(translated, params)
        except psycopg.OperationalError:
            if self._ran_any or self._in_tx:
                raise
            self._replace_connection()
            self._cur = self._raw.execute(translated, params)
        self._ran_any = True
        if _TRACE:
            print(f"[db {(time.perf_counter() - _t0) * 1000:5.0f}ms] {' '.join(sql.split())[:90]}",
                  file=sys.stderr, flush=True)
        return self._cur

    def _begin_with_retry(self):
        try:
            self.begin()
        except psycopg.OperationalError:
            if self._ran_any:
                raise
            self._replace_connection()
            self.begin()

    def _replace_connection(self):
        old, self._raw = self._raw, None
        self._in_tx = False
        self._pool.putconn(old)        # broken connections are discarded by the pool
        self._raw = self._pool.getconn()

    def executemany(self, sql, seq):
        self.begin()
        with self._raw.cursor() as cur:
            cur.executemany(self._translate(sql, [None]), seq)

    @property
    def description(self):
        return self._cur.description if self._cur is not None else None

    def close(self):
        raw = self._raw
        if raw is None:
            return
        try:
            self.commit()
        except Exception:
            try:
                raw.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            self._raw = None
            self._pool.putconn(raw)

    def __del__(self):
        raw = getattr(self, "_raw", None)
        if raw is not None:
            try:
                if getattr(self, "_in_tx", False):
                    raw.execute("ROLLBACK")
            except Exception:
                pass
            finally:
                self._pool.putconn(raw)
                self._raw = None


# _ensure_schema's DDL only needs to actually run once per process -
# guarded so it does, rather than on every get_connection() call. The
# lock's double-checked shape (check, acquire, check again) keeps a
# second thread that was blocked on it from redundantly re-running the
# DDL the first thread just finished. Across PROCESSES (a redeploy
# overlapping the old container) the DDL is serialized by a Postgres
# advisory lock inside _create_schema instead.
_schema_lock = threading.Lock()
_schema_ready = False


def get_connection():
    pool = _get_pool()
    con = _Conn(pool, pool.getconn())
    try:
        _ensure_schema(con)
    except Exception:
        con.close()
        raise
    return con


def _ensure_schema(con):
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        _create_schema(con)
        # Commit the DDL NOW, in its own transaction - not left riding on
        # the caller's: if that caller later raised before close(), the
        # rollback would silently undo the schema while _schema_ready
        # stayed True.
        con.commit()
        _schema_ready = True


_SCHEMA_DDL = """
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
    );
    -- active (app_ideas #18) - per user, per word, deliberately NOT on
    -- word_content: deactivating a word for yourself has no effect on
    -- anyone else who also has it in their own list. Defaults TRUE so
    -- every word on every user's list - present and future - starts
    -- (and stays, until someone actually deactivates it) active.
    CREATE TABLE IF NOT EXISTS user_words (
        user_id           TEXT NOT NULL,
        word              TEXT NOT NULL REFERENCES word_content(word),
        date_added        TIMESTAMP NOT NULL,
        repetition        INTEGER DEFAULT 0,
        ease_factor       DOUBLE PRECISION DEFAULT 2.5,
        interval_days     INTEGER DEFAULT 0,
        next_review_date  DATE DEFAULT CURRENT_DATE,
        active            BOOLEAN DEFAULT TRUE,
        PRIMARY KEY (user_id, word)
    );
    CREATE SEQUENCE IF NOT EXISTS attempt_id_seq START 1;
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
    );
    -- Nearly every query filters quiz_attempts by user - cheap indexes
    -- now that this is a real server (DuckDB scanned a local file and
    -- didn't need them).
    CREATE INDEX IF NOT EXISTS quiz_attempts_user_date_idx ON quiz_attempts (user_id, attempt_date);
    CREATE INDEX IF NOT EXISTS quiz_attempts_user_word_idx ON quiz_attempts (user_id, word);
    -- One row per user - the Settings page (see app.py). alias is capped
    -- at 10 chars by the UI's max_chars, not enforced here too.
    -- daily_word_target feeds the Progress tab's streak card; handedness
    -- picks which side the floating Menu button/drawer sit on; avatar_*
    -- (app_ideas #30) are the Social leaderboard's avatar, stored as
    -- short KEYS ("star", "navy", "sky") - app.py owns what each looks
    -- like, so retuning a palette needs no data migration. NULL = never
    -- picked (the leaderboard falls back to a letter-in-a-circle).
    CREATE TABLE IF NOT EXISTS user_settings (
        user_id                   TEXT PRIMARY KEY,
        alias                     TEXT,
        auto_add_community_words  BOOLEAN DEFAULT FALSE,
        share_progress            BOOLEAN DEFAULT FALSE,
        daily_word_target         INTEGER DEFAULT 10,
        dark_mode                 BOOLEAN DEFAULT FALSE,
        handedness                TEXT DEFAULT 'Right',
        avatar_icon               TEXT,
        avatar_primary            TEXT,
        avatar_secondary          TEXT
    );
    CREATE SEQUENCE IF NOT EXISTS app_idea_id_seq START 1;
    CREATE TABLE IF NOT EXISTS app_ideas (
        id            INTEGER PRIMARY KEY DEFAULT nextval('app_idea_id_seq'),
        user_id       TEXT NOT NULL,
        idea_text     TEXT NOT NULL,
        submitted_at  TIMESTAMP NOT NULL,
        idea_type     TEXT NOT NULL DEFAULT 'Improvement',
        status        TEXT NOT NULL DEFAULT 'Submitted'
    );
"""


def _create_schema(con):
    # Every statement but allowed_users' conditional seeding goes out as
    # ONE multi-statement string (one network round trip instead of
    # eleven - DDL statements were costing 150-280ms EACH against a
    # hosted database, ~2s on every server start). Transaction-scoped
    # advisory lock serializes concurrent first-boots (two containers
    # during a redeploy); released at commit.
    con.begin()
    con.execute("SELECT pg_advisory_xact_lock(727274)")
    # allowed_users - who may sign in (the owner stays hardcoded in
    # auth.OWNER_EMAILS as an always-allowed fallback, so a database
    # problem can never lock them out). Emails are stored lowercase.
    # Seeded ONCE, when the table doesn't exist yet - not on every schema
    # run, or a friend the owner removed would silently reappear.
    _allowed_table_existed = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = 'allowed_users'"
    ).fetchone()[0]
    con.execute(_SCHEMA_DDL + """
        CREATE TABLE IF NOT EXISTS allowed_users (
            email     TEXT PRIMARY KEY,
            added_by  TEXT,
            added_at  TIMESTAMP
        );
    """)
    if not _allowed_table_existed:
        for _email in _SEED_ALLOWED_EMAILS:
            con.execute(
                "INSERT INTO allowed_users (email, added_by, added_at) VALUES (?, 'seed', ?) "
                "ON CONFLICT (email) DO NOTHING",
                [_email, datetime.now(timezone.utc)],
            )


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
    CONFLICT DO NOTHING for everything except `active`, not a full DO
    UPDATE - re-adding a word you already have is a content correction
    (handled above), not a reason to reset YOUR review schedule or
    date_added. Was previously "ON CONFLICT DO UPDATE SET (everything
    except the schedule columns)" on one shared table; splitting the
    two concerns into two tables/statements makes that same rule
    simpler to see at a glance instead of requiring an exclusion list.

    `active` IS force-set back to TRUE on conflict (app_ideas #18/#21)
    - re-adding a word you'd deactivated is read as "I want this back,"
    same as explicitly hitting Activate, so it shouldn't require a
    separate step. This is a no-op (TRUE -> TRUE) for a word that was
    already active, so it never surprises anyone re-adding an active
    word purely for a definition refresh."""
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
        ON CONFLICT (user_id, word) DO UPDATE SET active = TRUE
        """,
        [user_id, w, datetime.now(timezone.utc), _today_local()],
    )
    con.close()


def set_audio_url(word: str, audio_url: str):
    """Backfill helper - update just the audio clip for an existing word
    without touching its definition or anything else. No user_id: audio
    is dictionary content (word_content), shared like everything else
    there."""
    con = get_connection()
    con.execute("UPDATE word_content SET audio_url = ? WHERE word = ?", [audio_url.strip(), word])
    con.close()


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


def deactivate_word(user_id: str, word: str):
    """Turns a word off for THIS user only (app_ideas #18) - unlike
    delete_word, quiz_attempts history and the user_words row itself are
    both left alone, so past stats/streaks stay intact and the word can
    still be found (e.g. via My Words' "show inactive" toggle). It just
    stops being served (next_due_word/soonest_upcoming) and stops
    counting toward Progress's Mastered/Learning/Needs Work snapshot,
    both of which filter on uw.active.

    No standalone reactivate/activate_word counterpart (app_ideas #21) -
    re-adding a deactivated word via Add Word's own "Add Word" button
    already flips active back to TRUE by itself (see add_word's ON
    CONFLICT), and that's deliberately the ONLY way back in: searching
    a word in Add Word is never read as "deactivate this," per user
    request, so this function has no UI counterpart there either -
    Quiz Me's deactivate icon is the only place a word gets turned off
    at all."""
    con = get_connection()
    con.execute("UPDATE user_words SET active = FALSE WHERE user_id = ? AND word = ?", [user_id, word])
    con.close()


def get_all_words(user_id: str, include_inactive: bool = False):
    """This user's words joined with attempt stats: times_quizzed,
    avg_accuracy, last_quizzed - scoped to their own quiz_attempts only,
    even though the word itself may exist in other users' lists too.

    Active-only by default (app_ideas #18) - a deactivated word is
    meant to disappear from the list you actually work with day to
    day, per user request ("inactive words should be hidden by
    default"); include_inactive=True is what My Words' own "show
    inactive" toggle passes to reveal them anyway. active itself is
    included in every returned dict either way, so a caller showing
    both can still tell which is which."""
    con = get_connection()
    rows = con.execute(f"""
        SELECT
            wc.word, wc.definition, wc.part_of_speech, wc.example, wc.synonyms, wc.phonetic,
            wc.audio_url, wc.antonyms, wc.etymology, uw.date_added, uw.next_review_date,
            uw.interval_days, uw.repetition, uw.active,
            COUNT(a.id)                    AS times_quizzed,
            ROUND(AVG(a.accuracy), 1)      AS avg_accuracy,
            MAX(a.attempt_date)            AS last_quizzed
        FROM user_words uw
        JOIN word_content wc ON wc.word = uw.word
        LEFT JOIN quiz_attempts a ON a.word = uw.word AND a.user_id = uw.user_id
        WHERE uw.user_id = ? {"" if include_inactive else "AND uw.active"}
        GROUP BY wc.word, wc.definition, wc.part_of_speech, wc.example, wc.synonyms, wc.phonetic,
                 wc.audio_url, wc.antonyms, wc.etymology, uw.date_added, uw.next_review_date,
                 uw.interval_days, uw.repetition, uw.active
        ORDER BY uw.date_added DESC
    """, [user_id]).fetchall()
    cols = [d[0] for d in con.description]
    con.close()
    return [dict(zip(cols, r)) for r in rows]


def get_word(user_id: str, word: str):
    """Dictionary content for `word`, but only if it's actually in THIS
    user's list (word_content existing globally - e.g. a friend already
    added it - doesn't count; this answers "is it in MY list", same as
    the old single-user version answered "does it exist at all").
    Returns it regardless of active status (Add Word's own "already in
    your list" check - see app.py's _save - needs to see an inactive
    word too, not just active ones); `active` is included in the
    returned dict so a caller can tell which."""
    con = get_connection()
    row = con.execute(
        """SELECT wc.word, wc.definition, wc.part_of_speech, wc.example, wc.synonyms, wc.phonetic,
                  wc.audio_url, wc.antonyms, wc.etymology, uw.active
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
        "antonyms": row[7], "etymology": row[8], "active": row[9],
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
    second pass still spreads out rather than looping the same word.

    Deactivated words (app_ideas #18) are excluded via uw.active - a
    word someone's turned off shouldn't keep coming up in their queue."""
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
        WHERE uw.user_id = ? AND uw.active AND uw.next_review_date <= ?
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
    easily be the earliest next_review_date in the whole deck.

    Deactivated words (app_ideas #18) are excluded via uw.active, same
    as next_due_word - a deactivated word shouldn't surface here either."""
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
        WHERE uw.user_id = ? AND uw.active
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

    Deactivated words (app_ideas #18) are excluded via uw.active - this
    is a snapshot of the deck you're actively working, not a lifetime
    record, so a word you've turned off shouldn't still occupy a slot
    in Mastered/Learning/Needs Work. quiz_attempts history itself is
    untouched (see get_daily_accuracy_trend/get_daily_words_quizzed_trend),
    so streaks and the historical accuracy chart stay accurate even
    after a word's deactivated."""
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
            WHERE uw.user_id = ? AND uw.active
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
    alias, all toggles off, a 10-word daily target, right-handed) for
    a user who's never saved any yet, rather than None/crashing -
    Settings' own UI relies on always getting a real dict back."""
    con = get_connection()
    row = con.execute(
        "SELECT alias, auto_add_community_words, share_progress, daily_word_target, dark_mode, handedness, "
        "avatar_icon, avatar_primary, avatar_secondary "
        "FROM user_settings WHERE user_id = ?",
        [user_id],
    ).fetchone()
    con.close()
    if row is None:
        return {
            # share_progress defaults ON for someone with no saved row
            # (per user request) - matches initialize_new_user, which
            # writes that same default as a real row on first login.
            "alias": "", "auto_add_community_words": False, "share_progress": True,
            "daily_word_target": 10, "dark_mode": False, "handedness": "Right",
            "avatar_icon": None, "avatar_primary": None, "avatar_secondary": None,
        }
    return {
        "alias": row[0] or "",
        "auto_add_community_words": bool(row[1]),
        "share_progress": bool(row[2]),
        # NULL for any row saved before this column existed, not just a
        # never-saved user (row is None above) - same 10-word fallback
        # either way.
        "daily_word_target": row[3] if row[3] is not None else 10,
        "dark_mode": bool(row[4]),
        "handedness": row[5] or "Right",
        "avatar_icon": row[6], "avatar_primary": row[7], "avatar_secondary": row[8],
    }


def save_user_settings(
    user_id: str, alias: str, auto_add_community_words: bool, share_progress: bool,
    daily_word_target: int = 10, dark_mode: bool = False, handedness: str = "Right",
    avatar_icon: str | None = None, avatar_primary: str | None = None,
    avatar_secondary: str | None = None,
):
    con = get_connection()
    con.execute(
        """
        INSERT INTO user_settings
            (user_id, alias, auto_add_community_words, share_progress, daily_word_target, dark_mode, handedness,
             avatar_icon, avatar_primary, avatar_secondary)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (user_id) DO UPDATE SET
            alias = EXCLUDED.alias,
            auto_add_community_words = EXCLUDED.auto_add_community_words,
            share_progress = EXCLUDED.share_progress,
            daily_word_target = EXCLUDED.daily_word_target,
            dark_mode = EXCLUDED.dark_mode,
            handedness = EXCLUDED.handedness,
            avatar_icon = EXCLUDED.avatar_icon,
            avatar_primary = EXCLUDED.avatar_primary,
            avatar_secondary = EXCLUDED.avatar_secondary
        """,
        [
            user_id, alias.strip()[:10], auto_add_community_words, share_progress,
            daily_word_target, dark_mode, handedness,
            avatar_icon, avatar_primary, avatar_secondary,
        ],
    )
    con.close()


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def is_email_allowed(email: str) -> bool:
    """Whether `email` is on the invite list."""
    con = get_connection()
    row = con.execute(
        "SELECT 1 FROM allowed_users WHERE email = ?", [_normalize_email(email)]
    ).fetchone()
    con.close()
    return row is not None


def list_allowed_users() -> list[dict]:
    """The invite list for the owner's Members page, each with what's
    knowable WITHOUT a write at sign-in time: `joined` (they have a
    user_settings row - i.e. they've signed in at least once and been
    set up), their word count, and last_quiz (most recent quiz attempt).
    Owner-first, then oldest invite first."""
    con = get_connection()
    rows = con.execute(
        """
        SELECT a.email, a.added_by, a.added_at,
               (SELECT COUNT(*) FROM user_settings s WHERE s.user_id = a.email) > 0 AS joined,
               (SELECT COUNT(*) FROM user_words w WHERE w.user_id = a.email) AS words,
               (SELECT MAX(q.attempt_date) FROM quiz_attempts q WHERE q.user_id = a.email) AS last_quiz
        FROM allowed_users a
        ORDER BY (a.email = ?) DESC, a.added_at ASC, a.email ASC
        """,
        [_LEGACY_OWNER_EMAIL],
    ).fetchall()
    con.close()
    return [
        {"email": r[0], "added_by": r[1], "added_at": r[2], "joined": bool(r[3]),
         "words": r[4], "last_quiz": r[5]}
        for r in rows
    ]


def add_allowed_user(email: str, added_by: str) -> bool:
    """Invites `email`. Returns False if they were already on the list
    (nothing changes), True if newly added. Validation (is it an email
    at all) is the caller's job - see app.py's Members page."""
    email = _normalize_email(email)
    con = get_connection()
    # One atomic statement: a row comes back only if THIS call inserted
    # it, so two simultaneous invites of the same address can't both
    # report "newly added".
    inserted = con.execute(
        "INSERT INTO allowed_users (email, added_by, added_at) VALUES (?, ?, ?) "
        "ON CONFLICT (email) DO NOTHING RETURNING email",
        [email, added_by, datetime.now(timezone.utc)],
    ).fetchone()
    con.close()
    return inserted is not None


def remove_allowed_user(email: str) -> None:
    """Revokes sign-in access. Deliberately leaves the person's words,
    quiz history and settings alone - this is about access, not data, so
    re-inviting them later puts everything back exactly as it was. (The
    owner can't be locked out regardless: auth.OWNER_EMAILS is checked
    first, independent of this table.)"""
    con = get_connection()
    con.execute("DELETE FROM allowed_users WHERE email = ?", [_normalize_email(email)])
    con.close()


def is_new_user(user_id: str) -> bool:
    """True if this user has never had a user_settings row - the marker
    for "first login" (a row is written by initialize_new_user, or by
    Settings' own Save, so every existing user has one). Cheap cached
    read, safe to call every session."""
    con = get_connection()
    row = con.execute("SELECT 1 FROM user_settings WHERE user_id = ?", [user_id]).fetchone()
    con.close()
    return row is None


def initialize_new_user(user_id: str) -> int:
    """First-login setup for a user with no settings row yet; returns how
    many words they were given (0 if they weren't actually new - e.g.
    two sessions racing, or someone who got a row in the meantime).

    - A user_settings row with the defaults, share_progress ON (per user
      request - the Social tab only shows people who opted in, and a new
      friend should appear without having to find that toggle).
    - Their word list seeded with every word that's active on anyone
      else's list, so Quiz Me/My Words aren't empty on day one. Each
      gets a fresh schedule (due today) like any newly added word, but
      keeps its ORIGINAL date_added (the earliest across the people who
      have it), capped at 30 days ago - stamping them all "now" would
      flood the Social tab's leaderboard ("words added this week") and
      activity feed with fake "added" events, and even the original
      dates would, for the handful someone else added in the last week
      (confirmed in testing: 12 phantom adds on a copy of the real data).

    The settings row is the "already initialized" marker, claimed
    atomically: ON CONFLICT DO NOTHING ... RETURNING yields a row only
    for the one call that actually inserted it, so two sessions racing
    on a friend's first login can't both seed the word list. Everything
    here is one transaction (see _Conn), so a failure part-way leaves
    no half-initialized user behind."""
    con = get_connection()
    claimed = con.execute(
        "INSERT INTO user_settings "
        "(user_id, alias, auto_add_community_words, share_progress, daily_word_target, dark_mode, handedness) "
        "VALUES (?, '', FALSE, TRUE, 10, FALSE, 'Right') "
        "ON CONFLICT (user_id) DO NOTHING RETURNING user_id",
        [user_id],
    ).fetchone()
    if not claimed:
        con.close()
        return 0
    con.execute(
        """
        INSERT INTO user_words (user_id, word, date_added, repetition, ease_factor, interval_days, next_review_date)
        SELECT ?, word, LEAST(MIN(date_added), ?), 0, 2.5, 0, ?
        FROM user_words
        WHERE active AND user_id != ?
        GROUP BY word
        ON CONFLICT (user_id, word) DO NOTHING
        """,
        [
            user_id,
            # Naive UTC, matching how the TIMESTAMP column's own values
            # come back - see _to_local_date's docstring.
            (datetime.now(timezone.utc) - timedelta(days=30)).replace(tzinfo=None),
            _today_local(),
            user_id,
        ],
    )
    seeded = con.execute("SELECT COUNT(*) FROM user_words WHERE user_id = ?", [user_id]).fetchone()[0]
    con.close()
    return seeded


def add_app_idea(user_id: str, idea_text: str, idea_type: str = "Improvement") -> int:
    """Returns the new idea's id - app_idea_id_seq's nextval, the same
    number displayed everywhere as "ID-0001" (see app.py's format_idea_id)
    - so the submission confirmation can show it immediately."""
    con = get_connection()
    idea_id = con.execute(
        "INSERT INTO app_ideas (user_id, idea_text, submitted_at, idea_type, status) "
        "VALUES (?, ?, ?, ?, 'Submitted') RETURNING id",
        [user_id, idea_text.strip(), datetime.now(timezone.utc), idea_type],
    ).fetchone()[0]
    con.close()
    return idea_id


def get_app_ideas(user_id: str) -> list[dict]:
    """This user's own submitted ideas, newest first - shown back to
    them on the App Ideas page so they can see what they've already
    suggested, including where each one stands (Submitted/Rejected/
    Completed)."""
    con = get_connection()
    rows = con.execute(
        "SELECT id, idea_text, submitted_at, idea_type, status FROM app_ideas "
        "WHERE user_id = ? ORDER BY submitted_at DESC",
        [user_id],
    ).fetchall()
    con.close()
    return [{"id": r[0], "idea_text": r[1], "submitted_at": r[2], "idea_type": r[3], "status": r[4]} for r in rows]


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


def _display_name(user_id: str, alias: str) -> str:
    """alias if they've set one; otherwise a readable fallback derived
    from their email rather than showing the raw address on the Social
    tab (app_ideas #10) - "riley.kaitlyn96@gmail.com" -> "Riley". Takes
    just the part before the first '.' or '@' and capitalizes it; for
    an email with no '.' before the '@' this is cruder (the whole
    local-part, capitalized) but still better than the full address."""
    if alias:
        return alias
    local_part = user_id.split("@")[0]
    return local_part.split(".")[0].capitalize()


def _mastered_this_window(con, user_id: str, cutoff) -> list[str]:
    """Words meeting the normal Mastered bar (repetition >= 3, avg
    accuracy >= 80 - same as get_progress_stats) with at least one
    attempt since `cutoff`. Shared by get_social_leaderboard (count)
    and get_social_feed (the words themselves) so the two can't drift
    apart on what counts.

    This is an approximation, not an exact "just crossed into Mastered"
    event log - a word mastered weeks ago that's simply revisited in
    the window still counts. No schema change (a real mastery-event
    table) felt worth it for a social feature's nice-to-have "recently
    mastered" list; revisit if that approximation turns out to matter
    in practice."""
    rows = con.execute(
        """
        SELECT uw.word
        FROM user_words uw JOIN quiz_attempts a ON a.word = uw.word AND a.user_id = uw.user_id
        WHERE uw.user_id = ? AND uw.active
        GROUP BY uw.word, uw.repetition
        HAVING uw.repetition >= 3 AND AVG(a.accuracy) >= 80 AND MAX(a.attempt_date) >= ?
        """,
        [user_id, cutoff],
    ).fetchall()
    return [r[0] for r in rows]


def get_social_leaderboard(days: int = 7) -> list[dict]:
    """One row per user who's opted in via Settings' Share My Progress
    toggle (app_ideas #10) - nobody appears here without explicitly
    turning that on, same privacy gate the setting already promised
    before this was the feature using it. Ranked by quizzes taken in
    the last `days` days, not lifetime totals - a cumulative ranking
    would just always favor whoever's used the app longest; a rolling
    window stays fair and gives everyone a fresh start regularly.

    Each row: user_id, display_name (see _display_name), quizzes/
    added/mastered counts in the window, and streak - reusing
    get_quiz_streak with THIS user's own daily_word_target, the exact
    same definition their own Progress tab's streak already uses.

    Includes opted-in users with zero activity in the window too
    (sorted to the bottom) rather than hiding them - seeing who's quiet
    this week is as real a signal as seeing who's active."""
    con = get_connection()
    opted_in = con.execute(
        "SELECT user_id, alias, daily_word_target, avatar_icon, avatar_primary, avatar_secondary "
        "FROM user_settings WHERE share_progress = TRUE"
    ).fetchall()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = []
    for user_id, alias, daily_target, avatar_icon, avatar_primary, avatar_secondary in opted_in:
        quizzes = con.execute(
            "SELECT COUNT(*) FROM quiz_attempts WHERE user_id = ? AND attempt_date >= ?",
            [user_id, cutoff],
        ).fetchone()[0]
        added = con.execute(
            "SELECT COUNT(*) FROM user_words WHERE user_id = ? AND date_added >= ?",
            [user_id, cutoff],
        ).fetchone()[0]
        mastered = len(_mastered_this_window(con, user_id, cutoff))
        rows.append({
            "user_id": user_id,
            "display_name": _display_name(user_id, alias),
            "quizzes": quizzes,
            "added": added,
            "mastered": mastered,
            "streak": get_quiz_streak(user_id, threshold=daily_target or 10),
            # app_ideas #30 - raw keys (None if never picked); app.py
            # resolves them to real colors/glyph.
            "avatar_icon": avatar_icon,
            "avatar_primary": avatar_primary,
            "avatar_secondary": avatar_secondary,
        })
    con.close()
    rows.sort(key=lambda r: r["quizzes"], reverse=True)
    return rows


def get_social_feed(days: int = 7, limit: int = 15) -> list[dict]:
    """Cross-user "added" and "mastered" events from the last `days`
    days, newest first, capped at `limit` (app_ideas #10) - same opt-in
    (user_settings.share_progress) and same Mastered approximation as
    get_social_leaderboard (see _mastered_this_window's docstring).
    Each event: kind ("added"/"mastered"), display_name, word, when."""
    con = get_connection()
    opted_in = con.execute(
        "SELECT user_id, alias FROM user_settings WHERE share_progress = TRUE"
    ).fetchall()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    events = []
    for user_id, alias in opted_in:
        name = _display_name(user_id, alias)
        added = con.execute(
            "SELECT word, date_added FROM user_words WHERE user_id = ? AND date_added >= ?",
            [user_id, cutoff],
        ).fetchall()
        for word, when in added:
            events.append({"kind": "added", "display_name": name, "word": word, "when": when})
        for word in _mastered_this_window(con, user_id, cutoff):
            # _mastered_this_window doesn't return the triggering
            # attempt's own timestamp (just the word) - re-fetch it
            # here only for the words that actually qualified, rather
            # than threading a second return value through a function
            # also used just for a plain count in the leaderboard.
            last_q = con.execute(
                "SELECT MAX(attempt_date) FROM quiz_attempts WHERE user_id = ? AND word = ?",
                [user_id, word],
            ).fetchone()[0]
            events.append({"kind": "mastered", "display_name": name, "word": word, "when": last_q})
    con.close()
    events.sort(key=lambda e: e["when"], reverse=True)
    return events[:limit]
