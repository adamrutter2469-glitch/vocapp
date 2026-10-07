"""
One-off: copy the vocapp DuckDB database into Postgres (DATABASE_URL).

    python scripts/migrate_duckdb_to_postgres.py                 # use ./vocab.duckdb
    python scripts/migrate_duckdb_to_postgres.py --from-r2       # pull R2's latest copy first
    python scripts/migrate_duckdb_to_postgres.py --truncate      # wipe the target tables first (re-runs)

Safe to re-run: it refuses to write into a target that already has data
unless --truncate is passed, and never touches the DuckDB source (a
--from-r2 download goes to a temp file, not over ./vocab.duckdb).

What it does, in order:
  1. Creates the schema in Postgres (db._create_schema - same code the app uses).
  2. Copies word_content, user_words, quiz_attempts, user_settings,
     app_ideas, allowed_users, keeping every id/timestamp as-is, all in
     ONE transaction - a failure part-way leaves the target untouched.
  3. Advances the two id sequences past the copied ids (otherwise the
     next quiz attempt or idea would collide with an existing id).
  4. Verifies: row counts per table plus a few content checksums, and
     exits non-zero if anything differs.
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb

import db

# (table, columns in copy order) - dependency order: word_content first.
TABLES = [
    ("word_content", ["word", "definition", "part_of_speech", "example", "synonyms",
                      "phonetic", "audio_url", "antonyms", "etymology"]),
    ("user_words", ["user_id", "word", "date_added", "repetition", "ease_factor",
                    "interval_days", "next_review_date", "active"]),
    ("quiz_attempts", ["id", "user_id", "word", "attempt_date", "your_answer", "accuracy",
                       "got_right", "got_missed", "note"]),
    ("user_settings", ["user_id", "alias", "auto_add_community_words", "share_progress",
                       "daily_word_target", "dark_mode", "handedness",
                       "avatar_icon", "avatar_primary", "avatar_secondary"]),
    ("app_ideas", ["id", "user_id", "idea_text", "submitted_at", "idea_type", "status"]),
    ("allowed_users", ["email", "added_by", "added_at"]),
]

# Cheap content checksums, same SQL on both sides.
CHECKS = [
    ("quiz_attempts.sum(accuracy)", "SELECT COALESCE(SUM(accuracy), 0) FROM quiz_attempts"),
    ("quiz_attempts.max(attempt_date)", "SELECT MAX(attempt_date) FROM quiz_attempts"),
    ("user_words.sum(repetition)", "SELECT COALESCE(SUM(repetition), 0) FROM user_words"),
    ("user_words.active count", "SELECT COUNT(*) FROM user_words WHERE active"),
    ("user_words.max(next_review_date)", "SELECT MAX(next_review_date) FROM user_words"),
    ("word_content.sum(length(definition))", "SELECT COALESCE(SUM(LENGTH(definition)), 0) FROM word_content"),
    ("app_ideas by status", "SELECT status, COUNT(*) FROM app_ideas GROUP BY status ORDER BY status"),
]


def pull_from_r2() -> Path:
    import r2_storage
    client = r2_storage._get_client()
    if client is None:
        sys.exit("R2 credentials not configured (.env) - can't pull from R2.")
    tmp = Path(tempfile.mkdtemp()) / "vocab_from_r2.duckdb"
    client.download_file(r2_storage.R2_BUCKET_NAME, r2_storage.R2_OBJECT_KEY, str(tmp))
    print(f"Pulled R2 copy -> {tmp} ({tmp.stat().st_size / 1e6:.1f} MB)")
    return tmp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(Path(__file__).resolve().parent.parent / "vocab.duckdb"))
    ap.add_argument("--from-r2", action="store_true", help="download R2's current copy to a temp file and use that")
    ap.add_argument("--truncate", action="store_true", help="empty the target tables before copying")
    args = ap.parse_args()

    source = pull_from_r2() if args.from_r2 else Path(args.source)
    if not source.exists():
        sys.exit(f"Source not found: {source}")
    src = duckdb.connect(str(source), read_only=True)

    # 1. schema (this also seeds allowed_users with the 3 default emails -
    #    replaced below by the source's real list).
    db.get_connection().close()

    pool = db._get_pool()
    with pool.connection() as conn:
        cur = conn.cursor()

        existing = {t: cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t, _ in TABLES}
        # allowed_users is seeded by schema creation - that alone doesn't count as "has data".
        meaningful = {t: n for t, n in existing.items() if t != "allowed_users" and n}
        if meaningful and not args.truncate:
            sys.exit(f"Target already has data {meaningful} - re-run with --truncate to replace it.")
        if args.truncate:
            print("Truncating target tables...")
            cur.execute("TRUNCATE quiz_attempts, user_words, user_settings, app_ideas, allowed_users, word_content")
        else:
            cur.execute("DELETE FROM allowed_users")  # drop the schema's seed rows

        # 2. copy
        for table, cols in TABLES:
            rows = src.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
            if rows:
                placeholders = ", ".join(["%s"] * len(cols))
                cur.executemany(
                    f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})", rows
                )
            print(f"  {table:15s} {len(rows):6d} rows copied")

        # 3. sequences
        for table, seq in (("quiz_attempts", "attempt_id_seq"), ("app_ideas", "app_idea_id_seq")):
            top = cur.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0]
            if top is None:
                cur.execute(f"SELECT setval('{seq}', 1, false)")
            else:
                cur.execute(f"SELECT setval('{seq}', %s, true)", [top])
            print(f"  {seq} -> next id {(top or 0) + 1}")
        conn.commit()

    # 4. verify
    print("\nVerifying...")
    problems = []
    with pool.connection() as conn:
        cur = conn.cursor()
        for table, _ in TABLES:
            a = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            b = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            ok = a == b
            print(f"  {'OK  ' if ok else 'FAIL'} {table:15s} duckdb={a} postgres={b}")
            if not ok:
                problems.append(table)
        for label, sql in CHECKS:
            a = src.execute(sql).fetchall()
            b = cur.execute(sql).fetchall()
            ok = [tuple(r) for r in a] == [tuple(r) for r in b]
            print(f"  {'OK  ' if ok else 'FAIL'} {label}: {a if len(str(a)) < 80 else '...'}")
            if not ok:
                print(f"       duckdb={a}\n       postgres={b}")
                problems.append(label)

    if problems:
        sys.exit(f"\nMIGRATION VERIFICATION FAILED: {problems}")
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
