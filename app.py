"""
vocapp - Phase 1 + Phase 2
Phase 1: add word -> quiz word (typed definition) -> AI grade -> show
correct definition -> save attempt.
Phase 2: dictionary auto-lookup on add (definition/part of speech/
example/synonyms fill in automatically, still editable before saving).
Phase 3 (spaced repetition, mastery scoring) comes later.
"""

import requests
import streamlit as st
import db
import dictionary
import grading

st.set_page_config(page_title="vocapp", page_icon="📚", layout="centered")

if "quiz_word" not in st.session_state:
    st.session_state.quiz_word = None
if "quiz_result" not in st.session_state:
    st.session_state.quiz_result = None

st.title("📚 vocapp")

tab_quiz, tab_add, tab_words = st.tabs(["Quiz Me", "Add Word", "My Words"])

# ------------------------------------------------------------
# Quiz Me
# ------------------------------------------------------------
with tab_quiz:
    if st.session_state.quiz_word is None:
        w = db.random_word()
        if w is None:
            st.info("No words yet - add some in the **Add Word** tab first.")
        else:
            st.session_state.quiz_word = w
            st.session_state.quiz_result = None

    if st.session_state.quiz_word:
        word_row = db.get_word(st.session_state.quiz_word)
        header = word_row["word"]
        st.header(header)
        caption_bits = []
        if word_row["part_of_speech"]:
            caption_bits.append(word_row["part_of_speech"])
        if word_row["phonetic"]:
            caption_bits.append(word_row["phonetic"])
        if caption_bits:
            st.caption("  •  ".join(caption_bits))

        if st.session_state.quiz_result is None:
            answer = st.text_area("Type your definition:", key="answer_box", height=100)
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

            if st.button("Next word ->"):
                st.session_state.quiz_word = None
                st.session_state.quiz_result = None
                st.rerun()

# ------------------------------------------------------------
# Add Word
# ------------------------------------------------------------


# Streamlit gotcha: popping a keyed widget's session_state entry does NOT
# reliably reset that widget on the next run - the frontend can keep
# showing the stale value. The bulletproof fix is to version the widget
# keys themselves, so "clearing the form" means rendering brand-new
# widgets with no prior state, not mutating existing ones.
st.session_state.setdefault("form_version", 0)


def _keys():
    v = st.session_state["form_version"]
    return {
        "word": f"add_word_{v}", "definition": f"definition_input_{v}",
        "pos": f"pos_input_{v}", "example": f"example_input_{v}",
        "synonyms": f"synonyms_input_{v}",
    }


def _do_lookup():
    k = _keys()
    word = st.session_state.get(k["word"], "").strip()
    if not word:
        st.session_state["add_word_msg"] = ("warning", "Type a word first.")
        return
    try:
        info = dictionary.lookup_word(word)
        st.session_state[k["definition"]] = info["definition"]
        st.session_state[k["pos"]] = info["part_of_speech"]
        st.session_state[k["example"]] = info["example"]
        st.session_state[k["synonyms"]] = ", ".join(info["synonyms"])
        st.session_state["phonetic_lookup"] = info["phonetic"]
        st.session_state["add_word_msg"] = (
            "success", "Filled in from the dictionary - review and adjust, then hit Add.",
        )
    except dictionary.LookupNotFound:
        st.session_state["add_word_msg"] = (
            "warning", f"No dictionary entry for '{word}' - fill in the definition yourself below.",
        )
    except requests.RequestException:
        st.session_state["add_word_msg"] = (
            "error", "Dictionary lookup failed (network error) - fill in the definition yourself below.",
        )


def _do_add():
    k = _keys()
    word = st.session_state.get(k["word"], "").strip()
    definition = st.session_state.get(k["definition"], "").strip()
    if not word or not definition:
        st.session_state["add_word_msg"] = ("warning", "Word and definition are required.")
        return
    pos = st.session_state.get(k["pos"], "")
    example = st.session_state.get(k["example"], "")
    synonyms_list = [s.strip() for s in st.session_state.get(k["synonyms"], "").split(",") if s.strip()]
    phonetic = st.session_state.get("phonetic_lookup", "")
    db.add_word(word, definition, pos, example, synonyms_list, phonetic)
    st.session_state.pop("phonetic_lookup", None)
    st.session_state["form_version"] += 1  # next render uses fresh, empty widget keys
    st.session_state.quiz_word = None  # this word may now be the only one - force Quiz Me to re-pick
    st.session_state["add_word_msg"] = ("success", f"Added **{word}**.")


with tab_add:
    st.subheader("Add a word")
    k = _keys()
    st.text_input("Word", key=k["word"], placeholder="e.g. fastidious")
    c1, c2 = st.columns(2)
    with c1:
        st.button("Look up", on_click=_do_lookup, help="Auto-fill from the dictionary")
    with c2:
        st.button("Add", type="primary", on_click=_do_add)

    st.text_area("Definition", key=k["definition"], height=80)
    st.text_input("Part of speech (optional)", key=k["pos"])
    st.text_area("Example sentence (optional)", key=k["example"], height=60)
    st.text_input("Synonyms, comma-separated (optional)", key=k["synonyms"])
    if st.session_state.get("phonetic_lookup"):
        st.caption(f"Pronunciation: {st.session_state['phonetic_lookup']}")

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
                if w["times_quizzed"] > 0:
                    st.markdown("**Attempt history:**")
                    for a in db.get_attempts(w["word"]):
                        st.markdown(f"- {a['attempt_date']:%b %d}: {a['accuracy']}% — \"{a['your_answer']}\"")
                if st.button("Delete", key=f"del_{w['word']}"):
                    db.delete_word(w["word"])
                    st.rerun()
