"""
Gates the whole app behind Google sign-in (Streamlit's built-in st.login,
OIDC via Authlib - see requirements.txt) plus a small hardcoded allowlist,
since this is a small (<=5 people) friends-and-family app, not a public
one. Two separate layers, both needed:

- st.login() proves WHO is signing in (a real Google account) - but on
  its own it would let ANYONE with a Google account in, not just the
  specific people this app is meant for.
- ALLOWED_EMAILS is what actually restricts access to those people -
  checked fresh on every rerun (not just once at login time), so
  removing someone from this list locks them out on their very next
  rerun, not just their next login.

Requires a [auth] block in .streamlit/secrets.toml (gitignored, not
committed - see .streamlit/secrets.toml.example) with a Google OAuth
client's credentials.

NOTE - this only gates ACCESS. Everyone on ALLOWED_EMAILS still shares
the same word list/progress for now - per-user data (separate word
lists, separate accuracy) is a follow-up step, not part of this one.
"""

import streamlit as st

# The only people allowed into the app, regardless of whether their
# Google login succeeds. Update this set (and nothing else in this file)
# to add or remove a friend.
ALLOWED_EMAILS = {
    "adamrutter2469@gmail.com",
    "riley.kaitlyn96@gmail.com",
}


def require_login() -> str:
    """Blocks the rest of the script (st.stop()) until the current viewer
    is both logged in AND on ALLOWED_EMAILS. Returns the logged-in user's
    email on success - callers use this as the per-user id once per-user
    data scoping exists."""
    if not st.user.is_logged_in:
        st.markdown("## vocapp")
        st.write("Sign in to continue.")
        st.button("Log in with Google", on_click=st.login)
        st.stop()

    email = st.user.email
    if email not in ALLOWED_EMAILS:
        st.error(f"{email} isn't invited to this app yet.")
        st.button("Log out", on_click=st.logout, key="auth_denied_logout")
        st.stop()

    return email
