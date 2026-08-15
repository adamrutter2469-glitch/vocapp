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

    /* Hide Streamlit's native "Press Enter to apply" hint under the Add
       Word input - that instruction doesn't apply here (Look up/Add are
       separate buttons, not Enter-to-submit), so it's just noise. Matches
       on a substring since the widget key is versioned (add_word_0,
       add_word_1, ...) to reset the field after every Add. */
    [class*="st-key-add_word_"] [data-testid="InputInstructions"] {{
        display: none;
    }}

    /* My Words toolbar row: shrink every column (outer 3 clusters, and
       the inner filter+sort / select+clear pairs) to its actual content
       width instead of stretching proportionally - same fix as the
       synonym pills above, needed here for the same reason (dead space
       after each left-aligned button was the real cause of "too spaced
       out", not the gap setting). The outer row then gets
       space-between so the 3 clusters spread to the row's edges instead
       of bunching at the left. */
    .st-key-words_toolbar_row [data-testid="stColumn"] {{
        width: auto !important;
        flex: 0 0 auto !important;
        min-width: 0 !important;
    }}
    .st-key-words_toolbar_row > div > [data-testid="stHorizontalBlock"] {{
        justify-content: space-between;
    }}

    /* My Words filter/sort popovers - default width was ~320px, halved.
       Popovers render in a portal straight under <body> (not inside our
       normal block-container tree), so this can't be scoped via the
       st-key trick used elsewhere; targeted directly since these are
       the only popovers in the app, so both get the same sizing. */
    [data-testid="stPopoverBody"] {{
        width: 200px !important;
        min-width: 200px !important;
    }}

    /* My Words sticky-footer pagination arrows - dropped the "Prev"/"Next"
       text down to bare < > glyphs, bumped up so a single character still
       reads clearly. */
    .st-key-words_prev_footer button, .st-key-words_next_footer button {{
        font-size: 1.2rem;
        font-weight: 700;
        line-height: 1;
    }}

    /* Sticky footer (Prev/page-info/Next) pinned to the bottom of the
       viewport so it's reachable without scrolling back up through a full
       page of expanders. max-width + auto margins keep it aligned with
       the centered page content instead of spanning the full viewport. */
    .st-key-words_sticky_footer {{
        position: fixed;
        left: 0;
        right: 0;
        bottom: 0;
        z-index: 999;
        max-width: 736px;
        margin: 0 auto;
        padding: 0.5rem 1rem;
        background: #FFFFFF;
        border-top: 1px solid rgba(0, 29, 86, 0.15);
        box-shadow: 0 -2px 10px rgba(0, 29, 86, 0.08);
    }}
    /* Center the </Prev  page-info  Next> cluster as a group in the
       footer, instead of Prev/Next stretching to the footer's edges with
       the page-info text sitting off-center between them. Shrink each
       column to its own content width first (same trick as the synonym
       pills in Add Word) so the row hugs its content and centering the
       row centers the text, not just the row's own already-full width. */
    .st-key-words_sticky_footer [data-testid="stHorizontalBlock"] {{
        justify-content: center;
        gap: 0.75rem;
    }}
    .st-key-words_sticky_footer [data-testid="stColumn"] {{
        width: auto !important;
        flex: 0 0 auto !important;
        min-width: 0 !important;
    }}
    /* Room at the bottom of the page so the fixed footer never covers the
       last couple of words in the list. Applies to every tab (Streamlit
       keeps all tab panels in one shared block container), but only My
       Words actually renders the footer, so it's just a bit of harmless
       extra scroll space elsewhere. */
    [data-testid="stMainBlockContainer"] {{
        padding-bottom: 4rem;
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
# Paginated (20/page) rather than rendering every word's expander at
# once - each expander carries its own speaker iframe + delete button,
# so the widget count (not the DB query, which stays cheap at this
# scale) is what would slow the page down as the word list grows.
WORDS_PAGE_SIZE = 20
SORT_OPTIONS = ["Newest added", "Oldest added", "A → Z", "Z → A", "Highest accuracy", "Lowest accuracy"]
FILTER_OPTIONS = ["All", "Mastered", "Learning", "Needs Work"]
st.session_state.setdefault("words_page", 0)
st.session_state.setdefault("confirm_bulk_delete", False)
st.session_state.setdefault("words_sort", SORT_OPTIONS[0])
st.session_state.setdefault("words_filter", FILTER_OPTIONS[0])


def _word_status(w):
    """Same bucketing as db.get_progress_stats() - Mastered requires a
    real streak (repetition >= 3), not just one lucky high score."""
    n, avg, rep = w["times_quizzed"], w["avg_accuracy"], w["repetition"] or 0
    if n and rep >= 3 and avg is not None and avg >= 80:
        return "Mastered"
    if n and avg is not None and avg < 60:
        return "Needs Work"
    return "Learning"


def _sort_words(words, sort_choice):
    # db.get_all_words() already comes back newest-first, so that case is
    # just a pass-through; oldest-first is its exact reverse.
    if sort_choice == "Oldest added":
        return list(reversed(words))
    if sort_choice == "A → Z":
        return sorted(words, key=lambda w: w["word"].lower())
    if sort_choice == "Z → A":
        return sorted(words, key=lambda w: w["word"].lower(), reverse=True)
    if sort_choice == "Highest accuracy":
        # Never-quizzed words (avg_accuracy is None) always sink to the
        # bottom regardless of direction - they don't have a score to rank.
        return sorted(words, key=lambda w: (w["avg_accuracy"] is None, -(w["avg_accuracy"] or 0)))
    if sort_choice == "Lowest accuracy":
        return sorted(words, key=lambda w: (w["avg_accuracy"] is None, w["avg_accuracy"] or 0))
    return words


def _words_prev_page():
    st.session_state["words_page"] -= 1


def _words_next_page():
    st.session_state["words_page"] += 1


def _reset_words_page():
    st.session_state["words_page"] = 0


def _select_all(words):
    for w in words:
        st.session_state[f"sel_{w['word']}"] = True


def _clear_selection(words):
    for w in words:
        key = f"sel_{w['word']}"
        if key in st.session_state:
            st.session_state[key] = False


def _start_bulk_confirm():
    st.session_state["confirm_bulk_delete"] = True


def _cancel_bulk_confirm():
    st.session_state["confirm_bulk_delete"] = False


def _do_bulk_delete(selected):
    for word in selected:
        db.delete_word(word)
        st.session_state.pop(f"sel_{word}", None)
    st.session_state["confirm_bulk_delete"] = False
    st.session_state["bulk_delete_msg"] = f"Deleted {len(selected)} word(s)."
    # Any of the deleted words could've been the current quiz word.
    st.session_state.quiz_word = None
    st.session_state.quiz_result = None
    st.session_state.quiz_schedule = None
    st.session_state["quiz_form_version"] += 1


def _do_single_delete(word):
    db.delete_word(word)
    st.session_state.pop(f"sel_{word}", None)


with tab_words:
    all_words = db.get_all_words()
    if not all_words:
        st.info("No words yet.")
    else:
        filter_choice = st.session_state["words_filter"]
        filtered = all_words if filter_choice == "All" else [w for w in all_words if _word_status(w) == filter_choice]

        if not filtered:
            st.info(f"No words in '{filter_choice}' right now.")
        else:
            words = _sort_words(filtered, st.session_state["words_sort"])
            total = len(words)
            total_pages = -(-total // WORDS_PAGE_SIZE)  # ceil division
            # Clamp in case the filtered count shrank since the page was
            # set (e.g. deleting the last word on the last page).
            st.session_state["words_page"] = max(0, min(st.session_state["words_page"], total_pages - 1))
            page = st.session_state["words_page"]
            start = page * WORDS_PAGE_SIZE
            end = min(start + WORDS_PAGE_SIZE, total)
            page_words = words[start:end]

            # Selection is tracked per-word (checkbox key = sel_<word>) and
            # persists across pages/filter/sort changes - counted here
            # against every word, not just what's currently filtered into
            # view, so the count and the trash icon's enabled state stay
            # accurate even for words selected under a different filter.
            selected_words = [w["word"] for w in all_words if st.session_state.get(f"sel_{w['word']}", False)]

            def _nav_row(key_suffix):
                """Prev/page-info/Next - used only by the sticky footer now."""
                c_prev, c_info, c_next = st.columns([0.5, 3, 0.5], gap="small")
                with c_prev:
                    st.button("<", key=f"words_prev_{key_suffix}", on_click=_words_prev_page, disabled=(page == 0), help="Previous page")
                with c_info:
                    st.markdown(
                        f"<div style='padding-top:0.4rem;'>"
                        f"Page {page + 1} of {total_pages} &nbsp;·&nbsp; {start + 1}-{end} of {total}"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                with c_next:
                    st.button(">", key=f"words_next_{key_suffix}", on_click=_words_next_page, disabled=(page >= total_pages - 1), help="Next page")

            # Two tight clusters - filter+sort on the left, and Select
            # All/Clear All/Trash all sharing one uniform small gap on the
            # right (so Clear All sits exactly as close to Trash as it
            # does to Select All) - spread apart via CSS space-between.
            # Flat proportional columns left big dead-space gaps after
            # each button, since a column's width and its button's actual
            # (much narrower) content width are two different things.
            with st.container(key="words_toolbar_row"):
                c_left, c_right = st.columns([1, 1])
                with c_left:
                    c_filter, c_sort = st.columns(2, gap="small")
                    with c_filter:
                        with st.popover("🔽", help="Filter"):
                            st.radio(
                                "Filter by", FILTER_OPTIONS, key="words_filter",
                                on_change=_reset_words_page, label_visibility="collapsed",
                            )
                    with c_sort:
                        with st.popover("⇅", help="Sort"):
                            st.radio(
                                "Sort by", SORT_OPTIONS, key="words_sort",
                                on_change=_reset_words_page, label_visibility="collapsed",
                            )
                with c_right:
                    c_selall, c_clearall, c_trash = st.columns(3, gap="small")
                    with c_selall:
                        st.button("Select Page", key="select_all_btn", on_click=_select_all, args=(page_words,))
                    with c_clearall:
                        st.button("Clear All", key="clear_sel_btn", on_click=_clear_selection, args=(words,))
                    with c_trash:
                        st.button(
                            "🗑️", key="trash_btn", on_click=_start_bulk_confirm,
                            disabled=(len(selected_words) == 0), help="Delete selected",
                        )

            if "bulk_delete_msg" in st.session_state:
                st.success(st.session_state.pop("bulk_delete_msg"))

            if st.session_state["confirm_bulk_delete"]:
                st.warning(f"Delete {len(selected_words)} word(s)? This can't be undone - their quiz history goes too.")
                cc1, cc2 = st.columns(2)
                with cc1:
                    st.button("Yes, delete", key="confirm_bulk_delete_btn", type="primary",
                               on_click=_do_bulk_delete, args=(selected_words,))
                with cc2:
                    st.button("Cancel", key="cancel_bulk_delete_btn", on_click=_cancel_bulk_confirm)

            for w in page_words:
                avg = f"{w['avg_accuracy']:.0f}%" if w["avg_accuracy"] is not None else "not quizzed yet"
                row_check, row_expander = st.columns([1, 11])
                with row_check:
                    st.checkbox("Select", key=f"sel_{w['word']}", label_visibility="collapsed")
                with row_expander:
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
                        st.button("Delete", key=f"del_{w['word']}", on_click=_do_single_delete, args=(w["word"],))

            # Sticky footer - fixed to the bottom of the viewport (CSS below)
            # rather than a plain row, so Prev/page-info/Next stay reachable
            # without scrolling back up through a full page of expanders.
            with st.container(key="words_sticky_footer"):
                _nav_row("footer")

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
