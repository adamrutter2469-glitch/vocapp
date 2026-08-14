"""
vocapp - Phase 1 MVP
Add word -> quiz word (typed definition) -> AI grade -> show correct
definition -> save attempt. Per the project plan's phased build-out;
Phase 2 (dictionary API, synonyms, examples) and Phase 3 (spaced
repetition, mastery scoring) come later.
"""

import streamlit as st
import db
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
        st.header(word_row["word"])
        if word_row["part_of_speech"]:
            st.caption(word_row["part_of_speech"])

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
with tab_add:
    st.subheader("Add a word")
    with st.form("add_word_form", clear_on_submit=True):
        word = st.text_input("Word")
        definition = st.text_area("Definition", height=80)
        part_of_speech = st.text_input("Part of speech (optional)")
        example = st.text_area("Example sentence (optional)", height=60)
        submitted = st.form_submit_button("Add", type="primary")
        if submitted:
            if not word.strip() or not definition.strip():
                st.warning("Word and definition are required.")
            else:
                db.add_word(word, definition, part_of_speech, example)
                st.session_state.quiz_word = None  # force Quiz Me to re-pick, this word may now be the only one
                st.success(f"Added **{word.strip()}**.")
                st.rerun()

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
                if w["part_of_speech"]:
                    st.caption(w["part_of_speech"])
                if w["example"]:
                    st.markdown(f"*Example: {w['example']}*")
                st.caption(f"Quizzed {w['times_quizzed']} time(s)"
                           + (f", last on {w['last_quizzed']:%b %d, %Y}" if w["last_quizzed"] else ""))
                if w["times_quizzed"] > 0:
                    st.markdown("**Attempt history:**")
                    for a in db.get_attempts(w["word"]):
                        st.markdown(f"- {a['attempt_date']:%b %d}: {a['accuracy']}% — \"{a['your_answer']}\"")
                if st.button("Delete", key=f"del_{w['word']}"):
                    db.delete_word(w["word"])
                    st.rerun()
