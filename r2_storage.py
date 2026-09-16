"""
Cloudflare R2 sync for vocab.duckdb.

Streamlit Community Cloud's containers are ephemeral - anything written
only to local disk is lost on redeploy or restart. R2 is meant to be
the durable copy once this is deployed there; the local .duckdb file
(see db.py) stays a plain local file otherwise, just pulled fresh once
per process at startup and pushed back up after every write. db.py's
own schema/queries don't change at all - this only wraps around them.

R2 is Cloudflare's S3-compatible object storage, so this is just
boto3's regular S3 client pointed at R2's endpoint - no Cloudflare-
specific SDK involved.

Credentials come from the environment - .env locally via python-dotenv
(same pattern as ANTHROPIC_API_KEY/MW_*_KEY in grading.py/dictionary.py),
or Streamlit Community Cloud's own Secrets UI once deployed there, which
also surfaces secrets as environment variables. If they're missing,
every function here is a no-op: R2 sync layers on top of local storage,
it doesn't replace the "just run it locally" path that's worked all
along, so a machine without R2 configured just keeps working local-only.

KNOWN LIMITATION - whole-file, last-writer-wins, no real per-row merge:
upload_db() pushes the WHOLE local file on every write, so if the
local copy it's pushing is stale, it silently overwrites whatever
happened on R2 since. Confirmed live (2026-09-12): the deployed
Streamlit Cloud app had been running long enough that its own local
copy predated a batch of app_ideas.status edits made directly against
a local vocab.duckdb via a one-off script (then pushed with
upload_db()) - the next ordinary write the LIVE app made (a new app
idea being submitted) re-uploaded ITS OWN still-stale local file and
silently wiped those status edits back out, with no error anywhere.
Not purely academic - a user (app_ideas #19, and again #23/#24) hit
this before anyone caught it in code.

MITIGATION (app_ideas #23/#24) - download_db() no longer pulls only
once per process, ever: db.get_connection(fresh=True), used by every
db.py function that goes on to call upload_db(), forces a fresh pull
right before that write. This doesn't add real multi-writer locking
(two genuinely simultaneous writes, in two different processes, in
the same instant, could still race) - it shrinks the staleness window
that actually caused every observed incident so far from "this
process's entire lifetime" (hours to days, for a long-lived Streamlit
Cloud container) down to "the instant right before this one write."
For a <=5-person app whose writes are seconds apart at the busiest,
that's enough to make the failure mode above not reproduce in
practice, without the complexity of real per-row conflict resolution.
Read-only functions still use the cheap once-per-process cache
(get_connection() with no argument) - reads were never what caused
data loss, so there's no reason to pay an R2 round-trip for those.
"""

import os
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

DB_PATH = Path(__file__).parent / "vocab.duckdb"
R2_OBJECT_KEY = "vocab.duckdb"
# Pulled from the bucket's own dashboard URL
# (dash.cloudflare.com/<ACCOUNT_ID>/r2/default/buckets/vocapp) - not a
# secret, just an identifier, but still overridable via env in case this
# ever needs to point somewhere else.
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "5e1c73883e6504594b707418b8775e1b")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "vocapp")

_client = None
_client_checked = False
_downloaded_this_process = False


def _get_client():
    """None if R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY aren't set - the
    signal every other function here uses to no-op instead of raising."""
    global _client, _client_checked
    if _client_checked:
        return _client
    _client_checked = True
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    if access_key and secret_key:
        _client = boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
        )
    return _client


def download_db(force: bool = False):
    """Pulls the R2 copy down over the local DB_PATH.

    Cached by default (force=False, the plain read path via
    db.get_connection()) - once per process, ever; every call after the
    first (successful or not) is a no-op, since a read doesn't need
    anything fresher than "whatever this process already has."

    force=True (db.get_connection(fresh=True), used by every write - see
    this module's KNOWN LIMITATION/MITIGATION above) always re-pulls,
    bypassing the cache, so a write builds on the actual latest R2 state
    instead of a possibly hours-stale local copy.

    Silently does nothing if R2 isn't configured, or if the object
    doesn't exist yet in the bucket (first-ever run before anything has
    uploaded a seed copy - db.py's own _ensure_schema creates a fresh
    local file in that case, same as always)."""
    global _downloaded_this_process
    if _downloaded_this_process and not force:
        return
    _downloaded_this_process = True
    client = _get_client()
    if client is None:
        return
    try:
        client.download_file(R2_BUCKET_NAME, R2_OBJECT_KEY, str(DB_PATH))
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey"):
            raise


def upload_db():
    """Pushes the local DB_PATH up to R2 - called after every write in
    db.py (add_word, save_attempt, update_schedule, delete_word,
    set_audio_url). No-op if R2 isn't configured."""
    client = _get_client()
    if client is None:
        return
    client.upload_file(str(DB_PATH), R2_BUCKET_NAME, R2_OBJECT_KEY)
