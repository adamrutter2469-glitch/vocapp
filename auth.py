"""
Gates the whole app behind Google sign-in (Streamlit's built-in st.login,
OIDC via Authlib - see requirements.txt) plus an invite list. Two
separate layers, both needed:

- st.login() proves WHO is signing in (a real Google account) - but on
  its own it would let ANYONE with a Google account in, not just the
  specific people this app is meant for.
- The invite list is what actually restricts access to those people. It
  lives in the database (db.allowed_users) and is managed by the owner
  from the Members page - no code change or deploy to invite or remove
  someone. It's checked on every rerun (not just at login), so removing
  someone locks them out on their very next rerun, not their next login.

OWNER_EMAILS stays hardcoded here and is checked FIRST, independent of
the database: if the database is unreachable or the table is ever
emptied by mistake, the owner can still sign in to fix it. Owners are
also who sees the Members page (see is_owner).

Requires a [auth] block in .streamlit/secrets.toml (gitignored, not
committed - see .streamlit/secrets.toml.example) with a Google OAuth
client's credentials. NOTE: while that Google OAuth app is in "Testing"
mode, Google itself ALSO limits sign-in to the test users listed in the
Google Cloud console - being on the invite list here isn't enough on its
own until the app is published to Production there.
"""

import time

import streamlit as st

import db

# Always allowed, whatever the database says - and the only people who
# can open the Members page / manage the invite list.
OWNER_EMAILS = {
    "adamrutter2469@gmail.com",
}

# How often a denied visitor's rerun may trigger a fresh pull from R2 to
# re-check the invite list (see require_login) - an invite is usually
# added right before the person tries to sign in, but a denied session
# shouldn't be able to make every rerun re-download the database.
_DENIED_REFRESH_SECONDS = 30


def is_owner(email) -> bool:
    return bool(email) and email.strip().lower() in OWNER_EMAILS


def _is_allowed(email: str) -> bool:
    if is_owner(email):
        return True
    try:
        if db.is_email_allowed(email):
            return True
        # Not on the (possibly stale, once-per-process) cached copy -
        # check R2's current state before turning someone away, at most
        # once per _DENIED_REFRESH_SECONDS per session.
        last = st.session_state.get("_allow_refresh_ts", 0.0)
        if time.time() - last < _DENIED_REFRESH_SECONDS:
            return False
        st.session_state["_allow_refresh_ts"] = time.time()
        return db.is_email_allowed(email, refresh=True)
    except Exception:
        st.error("Couldn't check the invite list right now - please try again in a moment.")
        st.stop()


def require_login() -> str:
    """Blocks the rest of the script (st.stop()) until the current viewer
    is both logged in AND on the invite list (or an owner). Returns the
    logged-in user's email on success - callers use this as the per-user
    id."""
    if not st.user.is_logged_in:
        st.markdown("## vocapp")
        st.write("Sign in to continue.")
        st.button("Log in with Google", on_click=st.login)
        st.stop()

    email = st.user.email
    if not _is_allowed(email):
        st.error(f"{email} isn't invited to this app yet.")
        st.button("Log out", on_click=st.logout, key="auth_denied_logout")
        st.stop()

    return email
