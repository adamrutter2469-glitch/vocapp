"""
vocapp - Phase 1 + Phase 2 + Phase 3
Phase 1: add word -> quiz word (typed definition) -> AI grade -> show
correct definition -> save attempt.
Phase 2: dictionary auto-lookup on add (definition/part of speech/
example/synonyms fill in automatically, still editable before saving).
Phase 3: spaced repetition (Quiz Me serves the most-overdue word, not a
random one), mastery/weak-word tracking, progress dashboard.
Phase 4 (polish - images, animations, mobile layout) comes later.
"""

from pathlib import Path

import requests
import pandas as pd
import streamlit as st
from PIL import Image
import db
import dictionary
import grading
import icons
import speaker

IMAGES_DIR = Path(__file__).parent / "images"

st.set_page_config(
    page_title="vocapp",
    page_icon=Image.open(IMAGES_DIR / "vocapp_book_only.png"),
    layout="centered",
)

# Custom CSS, scoped to specific elements via Streamlit's key -> CSS-class
# feature (any element/container given key="foo" gets a "st-key-foo" class
# on its wrapper - see https://docs.streamlit.io, "Style using CSS"). This
# is plain CSS with no onclick/event-handler attributes, so it doesn't hit
# the React error #231 trap documented in speaker.py (that was specifically
# about raw onclick="..." strings being parsed into React's onClick prop -
# a <style> block has no such prop and is the officially supported way to
# reskin native Streamlit widgets).
st.markdown(
    f"""
    <style>
    /* Book-icon buttons (Add Word's Look up button + each synonym's book
       icon): show the real logo image instead of a generic emoji glyph.
       font-size:0 collapses the emoji glyph itself to nothing while the
       background-image (unaffected by font-size) stays visible. */
    .st-key-lookup_btn button,
    .st-key-syn_book_0 button,
    .st-key-syn_book_1 button,
    .st-key-syn_book_2 button {{
        font-size: 0;
        background-image: url("{icons.BOOK_ICON_DATA_URI}");
        background-repeat: no-repeat;
        background-position: center;
        background-size: 20px 20px;
    }}

    /* Synonym pill: st.container(border=True, key="syn_box_N") draws the
       one outer box; these rules strip the 3 inner buttons' own
       borders/backgrounds so word + book icon + plus icon read as a
       single unit instead of 3 separate boxes side by side. */
    .st-key-syn_box_0 button, .st-key-syn_box_1 button, .st-key-syn_box_2 button {{
        border: none;
        background: transparent;
        box-shadow: none;
        padding: 0.15rem 0.35rem;
    }}
    .st-key-syn_box_0 [data-testid="stHorizontalBlock"],
    .st-key-syn_box_1 [data-testid="stHorizontalBlock"],
    .st-key-syn_box_2 [data-testid="stHorizontalBlock"] {{
        gap: 0.1rem;
        justify-content: flex-start;
    }}
    /* st.columns stretches each column proportionally to fill the row by
       default - that's what was pushing the book/plus icons far from the
       word. Shrink-wrap all 3 inner columns to their actual button width
       instead, so word/book/plus sit right next to each other. */
    .st-key-syn_box_0 [data-testid="stColumn"],
    .st-key-syn_box_1 [data-testid="stColumn"],
    .st-key-syn_box_2 [data-testid="stColumn"] {{
        width: auto !important;
        flex: 0 0 auto !important;
        min-width: 0 !important;
    }}

    /* The gap above the Add Word result card (word/definition/synonyms)
       was excessive - halved via a negative top margin on its wrapper. */
    .st-key-addword_result {{
        margin-top: -1.1rem;
    }}
    </style>
    """,
    unsafe_allow_html=True,
)

if "quiz_word" not in st.session_state:
    st.session_state.quiz_word = None
if "quiz_result" not in st.session_state:
    st.session_state.quiz_result = None
if "quiz_schedule" not in st.session_state:
    st.session_state.quiz_schedule = None
st.session_state.setdefault("quiz_form_version", 0)

_banner_l, _banner_c, _banner_r = st.columns([1, 2, 1])
with _banner_c:
    st.image(str(IMAGES_DIR / "vocapp_with_text.png"), use_container_width=True)

tab_quiz, tab_add, tab_words, tab_progress = st.tabs(["Quiz Me", "Add Word", "My Words", "Progress"])

# ------------------------------------------------------------
# Quiz Me
# ------------------------------------------------------------
with tab_quiz:
    if st.session_state.quiz_word is None:
        w = db.next_due_word()
        if w is not None:
            st.session_state.quiz_word = w
            st.session_state.quiz_result = None

    if st.session_state.quiz_word is None:
        # Nothing due per the spaced-repetition schedule right now.
        soonest, soonest_date = db.soonest_upcoming()
        if soonest is None:
            st.info("No words yet - add some in the **Add Word** tab first.")
        else:
            st.success(f"✅ All caught up! Next word due {soonest_date:%b %d, %Y}.")
            if st.button("Quiz anyway (practice)"):
                st.session_state.quiz_word = soonest
                st.session_state.quiz_result = None
                st.session_state["quiz_form_version"] += 1
                st.rerun()

    if st.session_state.quiz_word:
        word_row = db.get_word(st.session_state.quiz_word)
        speaker.word_header(word_row["word"], word_row.get("audio_url", ""))
        caption_bits = []
        if word_row["part_of_speech"]:
            caption_bits.append(word_row["part_of_speech"])
        if word_row["phonetic"]:
            caption_bits.append(word_row["phonetic"])
        if caption_bits:
            st.caption("  •  ".join(caption_bits))

        if st.session_state.quiz_result is None:
            answer = st.text_area(
                "Your definition", key=f"answer_box_{st.session_state.quiz_form_version}",
                height=100, placeholder="Type your definition...", label_visibility="collapsed",
            )
            if st.button("Submit", type="primary"):
                if not answer.strip():
                    st.warning("Type something first.")
                else:
                    with st.spinner("Grading..."):
                        try:
                            result = grading.grade_definition(
                                word_row["word"], word_row["definition"], answer
                            )
                            db.save_attempt(
                                word_row["word"], answer, result.accuracy,
                                result.got_right, result.got_missed, result.note,
                            )
                            st.session_state.quiz_schedule = db.update_schedule(
                                word_row["word"], result.accuracy
                            )
                            st.session_state.quiz_result = result
                            st.session_state.last_answer = answer
                            st.rerun()
                        except RuntimeError as e:
                            st.error(str(e))
        else:
            r = st.session_state.quiz_result
            st.markdown(f"**Your answer:** {st.session_state.last_answer}")
            color = "green" if r.accuracy >= 70 else ("orange" if r.accuracy >= 40 else "red")
            st.markdown(f"### :{color}[{r.accuracy}% correct]")
            st.markdown(f"**Dictionary definition:** {word_row['definition']}")
            if word_row["example"]:
                st.markdown(f"*Example: {word_row['example']}*")
            if word_row["synonyms"]:
                st.caption(f"Synonyms: {word_row['synonyms']}")
            if r.got_right:
                st.markdown("**What you got right:**")
                for item in r.got_right:
                    st.markdown(f"- {item}")
            if r.got_missed:
                st.markdown("**What you missed:**")
                for item in r.got_missed:
                    st.markdown(f"- {item}")
            st.caption(r.note)

            sched = st.session_state.quiz_schedule
            if sched:
                st.caption(
                    f"📅 Next review: {sched['next_review_date']:%b %d, %Y} "
                    f"(in {sched['interval_days']} day(s))"
                )

            if st.button("Next word ->"):
                st.session_state.quiz_word = None
                st.session_state.quiz_result = None
                st.session_state.quiz_schedule = None
                st.session_state["quiz_form_version"] += 1
                st.rerun()

# ------------------------------------------------------------
# Add Word
# ------------------------------------------------------------
# Lookup-only: the user never types their own definition (dictionary
# accuracy was the whole point of switching to Merriam-Webster - see
# dictionary.py), so there's no manual-entry fallback here. The bottom
# of the tab stays blank until a lookup - via the book button, or a
# synonym chip - actually succeeds; addword_result holds that lookup's
# data and is what "Add" saves.

# Streamlit gotcha: popping a keyed widget's session_state entry does NOT
# reliably reset that widget on the next run - the frontend can keep
# showing the stale value. The bulletproof fix is to version the widget
# key itself, so "clearing the form" means rendering a brand-new widget
# with no prior state, not mutating an existing one.
st.session_state.setdefault("form_version", 0)
st.session_state.setdefault("addword_result", None)
st.session_state.setdefault("addword_looked_up_word", "")


def _word_key():
    return f"add_word_{st.session_state['form_version']}"


def _set_msg(kind, text):
    st.session_state["add_word_msg"] = (kind, text)


def _run_lookup(word):
    """Shared by the book button and every synonym chip's book icon."""
    try:
        info = dictionary.lookup_word(word)
        st.session_state["addword_result"] = info
        st.session_state["addword_looked_up_word"] = word
        st.session_state.pop("add_word_msg", None)  # the card itself is the confirmation
    except dictionary.LookupNotFound:
        st.session_state["addword_result"] = None
        st.session_state["addword_looked_up_word"] = ""
        _set_msg("warning", f"No dictionary entry found for '{word}'.")
    except requests.RequestException:
        st.session_state["addword_result"] = None
        st.session_state["addword_looked_up_word"] = ""
        _set_msg("error", "Dictionary lookup failed (network error) - try again.")


def _reset_form_after_add():
    st.session_state["form_version"] += 1  # next render uses a fresh, empty Word field
    st.session_state["addword_result"] = None
    st.session_state["addword_looked_up_word"] = ""
    st.session_state.quiz_word = None  # this word may now be the only one - force Quiz Me to re-pick
    st.session_state.quiz_result = None
    st.session_state.quiz_schedule = None
    st.session_state["quiz_form_version"] += 1


def _save(word, info):
    db.add_word(
        word, info["definition"], info["part_of_speech"], info["example"],
        info["synonyms"], info["phonetic"], info["audio_url"],
    )
    _reset_form_after_add()
    _set_msg("success", f"Added **{word}**.")


def _do_lookup():
    word = st.session_state.get(_word_key(), "").strip()
    if not word:
        _set_msg("warning", "Type a word first.")
        return
    _run_lookup(word)


def _do_add():
    word = st.session_state.get(_word_key(), "").strip()
    if not word:
        _set_msg("warning", "Type a word first.")
        return
    # Reuse the cached lookup if it's for this exact word; otherwise (Add
    # pressed without Look up, or the word field changed since) verify
    # against the dictionary right here rather than saving unverified.
    cached = st.session_state.get("addword_result")
    if cached and st.session_state.get("addword_looked_up_word", "").lower() == word.lower():
        _save(word, cached)
        return
    try:
        info = dictionary.lookup_word(word)
    except dictionary.LookupNotFound:
        _set_msg("error", f"'{word}' isn't in the dictionary - check the spelling.")
        return
    except requests.RequestException:
        _set_msg("error", "Dictionary lookup failed (network error) - try again.")
        return
    _save(word, info)


def _do_lookup_synonym(syn):
    st.session_state[_word_key()] = syn
    _run_lookup(syn)


def _do_add_synonym(syn):
    try:
        info = dictionary.lookup_word(syn)
    except dictionary.LookupNotFound:
        _set_msg("error", f"'{syn}' isn't in the dictionary.")
        return
    except requests.RequestException:
        _set_msg("error", "Dictionary lookup failed (network error) - try again.")
        return
    _save(syn, info)


with tab_add:
    st.subheader("Add a word")
    # Word input roughly half-width with the two icon buttons hugging its
    # right edge (trailing empty column just absorbs the rest of the row).
    c_word, c_lookup, c_add, _c_spacer = st.columns([3, 0.6, 0.6, 4], gap="small")
    with c_word:
        st.text_input(
            "Word", key=_word_key(), placeholder="Type a word...", label_visibility="collapsed",
        )
    with c_lookup:
        st.button("📖", key="lookup_btn", on_click=_do_lookup, help="Look up in the dictionary")
    with c_add:
        st.button("➕", key="add_btn", on_click=_do_add, help="Add to My Words")

    result = st.session_state.get("addword_result")
    if result:
        with st.container(key="addword_result"):
            speaker.word_header(st.session_state["addword_looked_up_word"], result.get("audio_url", ""))
            meta_bits = [b for b in (result["part_of_speech"], result["phonetic"]) if b]
            if meta_bits:
                st.caption("  •  ".join(meta_bits))
            st.markdown(f"**{result['definition']}**")
            for ex in result["examples"]:
                st.markdown(f"- *{ex}*")

            if result["synonyms"]:
                st.caption("Synonyms")
                # MAX_SYNONYMS is capped at 3 (dictionary.py's _synonyms) -
                # syn_box_0/1/2 keys and their CSS above assume that cap.
                # Outer columns put the pills side by side in one line;
                # each pill's own inner columns are shrink-wrapped via CSS
                # (see the .st-key-syn_box_N rules above) so word/book/plus
                # sit tight together instead of stretching across the pill.
                pill_cols = st.columns(len(result["synonyms"]), gap="small")
                for i, syn in enumerate(result["synonyms"]):
                    with pill_cols[i], st.container(key=f"syn_box_{i}", border=True):
                        word_col, book_col, plus_col = st.columns([3, 1, 1], gap="small")
                        with word_col:
                            st.button(syn, key=f"syn_word_{i}", on_click=_do_lookup_synonym, args=(syn,))
                        with book_col:
                            st.button("📖", key=f"syn_book_{i}", on_click=_do_lookup_synonym, args=(syn,), help=f"Look up '{syn}'")
                        with plus_col:
                            st.button("➕", key=f"syn_plus_{i}", on_click=_do_add_synonym, args=(syn,), help=f"Add '{syn}'")

    if "add_word_msg" in st.session_state:
        kind, msg = st.session_state.pop("add_word_msg")
        getattr(st, kind)(msg)

# ------------------------------------------------------------
# My Words
# ------------------------------------------------------------
with tab_words:
    words = db.get_all_words()
    if not words:
        st.info("No words yet.")
    else:
        st.caption(f"{len(words)} word(s)")
        for w in words:
            avg = f"{w['avg_accuracy']:.0f}%" if w["avg_accuracy"] is not None else "not quizzed yet"
            with st.expander(f"{w['word']}  —  {avg}"):
                speaker.play_button(w["word"], w.get("audio_url", ""))
                st.markdown(f"**Definition:** {w['definition']}")
                meta_bits = [b for b in (w["part_of_speech"], w["phonetic"]) if b]
                if meta_bits:
                    st.caption("  •  ".join(meta_bits))
                if w["example"]:
                    st.markdown(f"*Example: {w['example']}*")
                if w["synonyms"]:
                    st.caption(f"Synonyms: {w['synonyms']}")
                st.caption(f"Quizzed {w['times_quizzed']} time(s)"
                           + (f", last on {w['last_quizzed']:%b %d, %Y}" if w["last_quizzed"] else ""))
                if w["next_review_date"]:
                    st.caption(f"Next review: {w['next_review_date']:%b %d, %Y}")
                if w["times_quizzed"] > 0:
                    st.markdown("**Attempt history:**")
                    for a in db.get_attempts(w["word"]):
                        st.markdown(f"- {a['attempt_date']:%b %d}: {a['accuracy']}% — \"{a['your_answer']}\"")
                if st.button("Delete", key=f"del_{w['word']}"):
                    db.delete_word(w["word"])
                    st.rerun()

# ------------------------------------------------------------
# Progress
# ------------------------------------------------------------
with tab_progress:
    stats = db.get_progress_stats()
    if stats["total"] == 0:
        st.info("No words yet.")
    else:
        st.subheader("Vocabulary Progress")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Words", stats["total"])
        c2.metric("Mastered", stats["mastered"])
        c3.metric("Learning", stats["learning"])
        c4.metric("Needs Work", stats["needs_work"])
        if stats["overall_avg"] is not None:
            st.metric("Average Definition Accuracy", f"{stats['overall_avg']}%")

        trend = db.get_daily_accuracy_trend()
        if len(trend) >= 2:
            st.subheader("Accuracy over time")
            df = pd.DataFrame(trend, columns=["date", "avg_accuracy"]).set_index("date")
            st.line_chart(df)
        elif trend:
            st.caption("Quiz on a few more days to see an accuracy trend here.")

        weak = db.get_weak_words()
        if weak:
            # Ranked lowest-first, not the same <60% cutoff the "Needs Work"
            # tile above uses - named to avoid implying they always agree.
            st.subheader("Your weakest words")
            for w in weak:
                st.markdown(f"- **{w['word']}** — {w['avg_accuracy']}% avg ({w['times_quizzed']} attempt(s))")
