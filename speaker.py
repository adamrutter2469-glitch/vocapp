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
CSS (iframes are isolated) - word_header's text_color param is how
app.py's Dark Mode setting (see PAL there) reaches in here despite
that; everything else below (background, button styling) is
transparent/colorless already, so it never needed its own dark variant.
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


def word_header(word: str, audio_url: str = "", height_px: int = 40, text_color: str = "#001D56"):
    """Word rendered as a header-sized line with a speaker icon hugging
    it directly (flexbox, gap - not fixed-width columns), for the main
    quiz display.

    height_px used to default to 56 - the iframe's own content (2rem
    text + icon) only ever renders 37px tall (measured), so the extra
    19px was dead space sitting below the word, inside the iframe,
    invisible but still pushing whatever comes after it (the part-of-
    speech/pronunciation caption in both Quiz Me and Add Word) further
    down. 40px keeps a few px of headroom rather than clipping right at
    the content's exact height.

    text_color defaults to the app's own light-mode ink (#001D56) for
    any caller that doesn't pass one, but app.py's own call sites pass
    PAL['text'] - this iframe is a separate document (components.html),
    invisible to the page's own CSS (confirmed live: toggling Dark Mode
    left this word rendered in the original dark navy, unreadable
    against the new dark page background, since the override CSS in
    app.py's <style> block never reaches inside an iframe's own
    document), so the color has to be threaded in as an argument
    instead of just inherited or overridden from outside."""
    js = _pronounce_js(word, audio_url)
    components.html(
        f"""
        <style>html, body {{ margin: 0; padding: 0; background: transparent; }}</style>
        <div style="display:flex; align-items:center; gap:10px;
                     font-family:'Source Sans Pro', sans-serif;">
            <span style="font-size:2rem; font-weight:700; color:{text_color};">{word}</span>
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
        <style>html, body {{ margin: 0; padding: 0; background: transparent; }}</style>
        <button onclick='{js}' title="Play pronunciation" style="
            font-size: {size_px}px; background: transparent; border: none;
            cursor: pointer; padding: 0; line-height: 1;
        ">🔊</button>
        """,
        height=size_px + 12,
    )
