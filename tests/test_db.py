"""
Tests for db.py against a throwaway embedded PostgreSQL (pgserver) - never
touches the real database.

    pip install -r requirements-dev.txt
    pytest tests -q
"""

import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pgserver = pytest.importorskip("pgserver")

U1 = "alice@example.com"
U2 = "bob@example.com"
OWNER = "adamrutter2469@gmail.com"


@pytest.fixture(scope="session", autouse=True)
def _postgres():
    import os
    d = tempfile.mkdtemp()
    srv = pgserver.get_server(d, cleanup_mode="stop")
    os.environ["DATABASE_URL"] = srv.get_uri()
    yield
    srv.cleanup()


@pytest.fixture()
def db():
    import db as module
    con = module.get_connection()
    con.execute(
        "TRUNCATE quiz_attempts, user_words, user_settings, app_ideas, word_content RESTART IDENTITY"
    )
    con.close()
    return module


def _add(db, user, word, **kw):
    db.add_word(user, word, kw.get("definition", f"def of {word}"), "noun", "ex",
                ["syn"], "/x/", "", ["ant"], "ety")


# ---------------- schema / connection ----------------

def test_schema_is_idempotent_and_seeds_invites_once(db):
    con = db.get_connection()
    first = con.execute("SELECT COUNT(*) FROM allowed_users").fetchone()[0]
    con.close()
    assert first == 3
    db.remove_allowed_user("riley.kaitlyn96@gmail.com")
    db._schema_ready = False          # force the DDL to run again
    db.get_connection().close()
    assert not db.is_email_allowed("riley.kaitlyn96@gmail.com")   # not re-seeded


def test_numeric_averages_come_back_as_float(db):
    _add(db, U1, "alpha")
    db.save_attempt(U1, "alpha", "a", 70, "")
    db.save_attempt(U1, "alpha", "b", 81, "")
    w = db.get_all_words(U1)[0]
    assert isinstance(w["avg_accuracy"], float) and w["avg_accuracy"] == 75.5


def test_aware_and_naive_datetimes_both_store_as_utc(db):
    _add(db, U1, "alpha")
    now = datetime.now(timezone.utc)
    con = db.get_connection()
    con.execute("INSERT INTO quiz_attempts (user_id, word, attempt_date, your_answer, accuracy) VALUES (?,?,?,?,?)",
                [U1, "alpha", now, "x", 50])
    stored = con.execute("SELECT attempt_date FROM quiz_attempts").fetchone()[0]
    con.close()
    assert stored.tzinfo is None
    assert abs((stored - now.replace(tzinfo=None)).total_seconds()) < 1


# ---------------- words / active ----------------

def test_add_word_is_per_user_and_shares_content(db):
    _add(db, U1, "alpha")
    _add(db, U2, "alpha", definition="corrected def")
    assert db.get_word(U1, "alpha")["definition"] == "corrected def"   # shared content updated
    assert len(db.get_all_words(U1)) == 1 and len(db.get_all_words(U2)) == 1


def test_deactivate_hides_word_and_readd_reactivates(db):
    _add(db, U1, "alpha")
    db.deactivate_word(U1, "alpha")
    assert db.get_all_words(U1) == []
    assert [w["word"] for w in db.get_all_words(U1, include_inactive=True)] == ["alpha"]
    assert db.get_progress_stats(U1)["total"] == 0
    assert db.next_due_word(U1) is None
    _add(db, U1, "alpha")
    assert db.get_word(U1, "alpha")["active"] is True


def test_delete_word_removes_only_this_users_data(db):
    _add(db, U1, "alpha"); _add(db, U2, "alpha")
    db.save_attempt(U1, "alpha", "a", 60, "")
    db.save_attempt(U2, "alpha", "a", 60, "")
    db.delete_word(U1, "alpha")
    assert db.get_word(U1, "alpha") is None and db.get_attempts(U1, "alpha") == []
    assert db.get_word(U2, "alpha") is not None and len(db.get_attempts(U2, "alpha")) == 1


# ---------------- scheduling / stats ----------------

def test_sm2_schedule_progresses_and_resets(db):
    _add(db, U1, "alpha")
    s1 = db.update_schedule(U1, "alpha", 90)
    s2 = db.update_schedule(U1, "alpha", 90)
    assert (s1["repetition"], s1["interval_days"]) == (1, 1)
    assert (s2["repetition"], s2["interval_days"]) == (2, 6)
    s3 = db.update_schedule(U1, "alpha", 10)
    assert (s3["repetition"], s3["interval_days"]) == (0, 1)


def test_progress_buckets(db):
    for w in ("m", "n", "l"):
        _add(db, U1, w)
    for _ in range(3):
        db.save_attempt(U1, "m", "x", 95, ""); db.update_schedule(U1, "m", 95)
    db.save_attempt(U1, "n", "x", 20, ""); db.update_schedule(U1, "n", 20)
    s = db.get_progress_stats(U1)
    assert (s["total"], s["mastered"], s["needs_work"], s["learning"]) == (3, 1, 1, 1)


def test_streak_counts_consecutive_days_meeting_threshold(db):
    _add(db, U1, "alpha")
    con = db.get_connection()
    today = datetime.now(timezone.utc)
    for days_ago in (1, 2):                        # two full days, none today
        for _ in range(3):
            con.execute("INSERT INTO quiz_attempts (user_id, word, attempt_date, your_answer, accuracy) VALUES (?,?,?,?,?)",
                        [U1, "alpha", today - timedelta(days=days_ago, hours=-1), "x", 80])
    con.close()
    assert db.get_quiz_streak(U1, threshold=3) in (2, 3)   # boundary day depends on local midnight
    assert db.get_quiz_streak(U1, threshold=10) == 0


# ---------------- settings / ideas ----------------

def test_settings_default_share_on_and_roundtrip_with_avatar(db):
    assert db.get_user_settings("nobody@example.com")["share_progress"] is True
    db.save_user_settings(U1, "Al", False, True, 15, True, "Left", "star", "navy", "sky")
    s = db.get_user_settings(U1)
    assert (s["alias"], s["daily_word_target"], s["dark_mode"], s["handedness"]) == ("Al", 15, True, "Left")
    assert (s["avatar_icon"], s["avatar_primary"], s["avatar_secondary"]) == ("star", "navy", "sky")


def test_app_idea_ids_increment_and_status_updates(db):
    a = db.add_app_idea(U1, "one"); b = db.add_app_idea(U1, "two", "Bug Fix")
    assert b == a + 1
    db.update_app_idea_status(a, "Completed")
    statuses = {i["id"]: i["status"] for i in db.get_all_app_ideas()}
    assert statuses[a] == "Completed" and statuses[b] == "Submitted"


# ---------------- invites / onboarding ----------------

def test_invite_list_is_case_insensitive_and_remove_keeps_data(db):
    assert db.add_allowed_user("New.Friend@Gmail.com", OWNER) is True
    assert db.add_allowed_user("new.friend@gmail.com", OWNER) is False
    assert db.is_email_allowed("  NEW.friend@gmail.com ")
    _add(db, "new.friend@gmail.com", "alpha")
    db.remove_allowed_user("new.friend@gmail.com")
    assert not db.is_email_allowed("new.friend@gmail.com")
    assert db.get_word("new.friend@gmail.com", "alpha") is not None


def test_new_user_gets_shared_words_defaults_and_no_phantom_adds(db):
    _add(db, U1, "alpha"); _add(db, U1, "beta"); _add(db, U2, "beta")
    db.save_user_settings(U1, "Al", False, True)
    db.deactivate_word(U1, "beta"); db.deactivate_word(U2, "beta")     # inactive for everyone
    _add(db, U2, "gamma")
    new = "newbie@example.com"
    assert db.is_new_user(new)
    seeded = db.initialize_new_user(new)
    assert seeded == 2                                                  # alpha + gamma (beta inactive everywhere)
    assert db.initialize_new_user(new) == 0 and not db.is_new_user(new)
    assert db.get_user_settings(new)["share_progress"] is True
    row = [r for r in db.get_social_leaderboard() if r["user_id"] == new][0]
    assert row["added"] == 0                                            # seeded words don't count as "added this week"


# ---------------- social ----------------

def test_leaderboard_window_is_utc_correct_and_opt_in_only(db):
    _add(db, U1, "alpha")
    db.save_user_settings(U1, "Al", False, True)
    db.save_user_settings(U2, "Bo", False, False)                       # not sharing
    con = db.get_connection()
    now = datetime.now(timezone.utc)
    for hours_ago in (1, 24 * 7 - 1, 24 * 7 + 1):                       # in, in, just outside the 7-day window
        con.execute("INSERT INTO quiz_attempts (user_id, word, attempt_date, your_answer, accuracy) VALUES (?,?,?,?,?)",
                    [U1, "alpha", now - timedelta(hours=hours_ago), "x", 50])
    con.close()
    board = db.get_social_leaderboard()
    assert [r["user_id"] for r in board] == [U1]
    assert board[0]["quizzes"] == 2


# ---------------- concurrency ----------------

def test_concurrent_writes_all_land(db):
    _add(db, U1, "alpha")
    errors = []

    def worker(i):
        try:
            for j in range(5):
                db.save_attempt(U1, "alpha", f"{i}-{j}", 50, "")
        except Exception as e:   # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert not errors
    assert len(db.get_attempts(U1, "alpha")) == 50


def test_concurrent_first_login_seeds_once(db):
    _add(db, U1, "alpha"); _add(db, U1, "beta")
    results = []
    threads = [threading.Thread(target=lambda: results.append(db.initialize_new_user("race@example.com")))
               for _ in range(6)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert sorted(results) == [0, 0, 0, 0, 0, 2]
