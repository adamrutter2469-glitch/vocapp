"""
Pronunciation playback.

Two paths, both free:
  - audio_url set  -> play the dictionary API's real recording
  - audio_url empty, OR the recording fails to load/play at runtime
    (the free API's media hosting has turned out to be flaky - confirmed
    live, a word that played fine minutes earlier started 502ing) ->
    fall back to the browser's built-in Web Speech API (speechSynthesis).
    No API key, no cost, works in every modern browser, just lower
    voice quality than a real recording.

Everything here renders via components.html (its own iframe), not
st.markdown(unsafe_allow_html=True) - tried that first for the inline
word+icon header, but Streamlit's markdown renderer parses raw HTML into
actual React elements rather than treating it as an opaque blob, so a
plain `onclick="..."` string attribute gets fed into React's `onClick`
prop - which requires a real function reference, not a string - and
throws "Minified React error #231: Expected `onClick` listener to be a
function, instead got a value of `string` type" on every click. Confirmed
live (WebFetched React's own source to decode the minified error rather
than guess). components.html's iframe is genuinely raw HTML/JS with no
React involved, so it doesn't hit this.

Trade-off: content inside these iframes can't inherit Streamlit's theme
CSS (iframes are isolated), so styling below is hardcoded for the light
theme - a known Phase-4-polish gap, not attempted here.
"""

import json
import streamlit.components.v1 as components


def _pronounce_js(word: str, audio_url: str) -> str:
    word_js = json.dumps(word)
    say = (
        "window.speechSynthesis.cancel();"
        f"window.speechSynthesis.speak(new SpeechSynthesisUtterance({word_js}));"
    )
    if not audio_url:
        return say
    url_js = json.dumps(audio_url)
    return (
        f"var a=new Audio({url_js});"
        f"var say=function(){{{say}}};"
        "a.onerror=say;a.play().catch(say);"
    )


def word_header(word: str, audio_url: str = "", height_px: int = 56):
    """Word rendered as a header-sized line with a speaker icon hugging
    it directly (flexbox, gap - not fixed-width columns), for the main
    quiz display."""
    js = _pronounce_js(word, audio_url)
    components.html(
        f"""
        <div style="display:flex; align-items:center; gap:10px;
                     font-family:'Source Sans Pro', sans-serif;">
            <span style="font-size:2rem; font-weight:700; color:#001D56;">{word}</span>
            <button onclick='{js}' title="Play pronunciation" style="
                font-size: 22px; background: transparent; border: none;
                cursor: pointer; padding: 0; line-height: 1;
            ">🔊</button>
        </div>
        """,
        height=height_px,
    )


def play_button(word: str, audio_url: str = "", size_px: int = 26):
    """Standalone icon-only button for places with no adjacent text to
    build a combined word_header() for (e.g. inside a My Words expander,
    where the word itself is already shown as the expander's title)."""
    js = _pronounce_js(word, audio_url)
    components.html(
        f"""
        <button onclick='{js}' title="Play pronunciation" style="
            font-size: {size_px}px; background: transparent; border: none;
            cursor: pointer; padding: 0; line-height: 1;
        ">🔊</button>
        """,
        height=size_px + 12,
    )
