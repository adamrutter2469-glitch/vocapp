"""
Pronunciation playback: a small embedded HTML/JS button, since Streamlit
has no native "play this audio on click" widget (st.audio renders a full
player bar, not an icon button).

Two paths, both free:
  - audio_url set  -> play the dictionary API's real recording
  - audio_url empty, OR the recording fails to load/play at runtime
    (the free API's media hosting has turned out to be flaky - confirmed
    live, a word that played fine minutes earlier started 502ing) ->
    fall back to the browser's built-in Web Speech API (speechSynthesis).
    No API key, no cost, works in every modern browser, just lower
    voice quality than a real recording.
"""

import json
import streamlit.components.v1 as components


def play_button(word: str, audio_url: str = ""):
    word_js = json.dumps(word)
    url_js = json.dumps(audio_url) if audio_url else "null"
    components.html(
        f"""
        <script>
        function vocappPlay(word, url) {{
            var say = function() {{
                window.speechSynthesis.cancel();
                window.speechSynthesis.speak(new SpeechSynthesisUtterance(word));
            }};
            if (!url) {{ say(); return; }}
            var a = new Audio(url);
            a.onerror = say;          // load failure (404/502/CORS/etc.)
            a.play().catch(say);      // playback failure (bad/empty source, blocked, ...)
        }}
        </script>
        <button onclick='vocappPlay({word_js}, {url_js})' title="Play pronunciation" style="
            font-size: 16px; background: #fff; border: 1px solid #d0d0d0;
            border-radius: 6px; padding: 4px 12px; cursor: pointer;
            font-family: "Source Sans Pro", sans-serif;
        ">🔊 Play</button>
        """,
        height=38,
    )
