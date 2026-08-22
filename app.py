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

import html
import re
from pathlib import Path

import requests
import altair as alt
import pandas as pd
import streamlit as st
from PIL import Image
import db
import dictionary
import frequency
import grading
import speaker
import trends
import usage_examples

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
    /* The gap above the Add Word result card (word/definition/synonyms)
       was excessive - cut down via a negative top margin on its wrapper.
       -1.1rem had shrunk it all the way to ~0 (measured: -1.6px, i.e. the
       search bar and word were essentially touching) - backed off to
       -0.5rem for a real but modest ~8px gap instead. */
    .st-key-addword_result {{
        margin-top: -0.5rem;
    }}

    /* Clickable-word definition text (_render_clickable_text): each real
       word in a definition is its own st.popover trigger (Look up / Add),
       but should still read as a normal flowing sentence, not a wall of
       bordered buttons. [class*="st-key-defword_"] matches every such
       row regardless of its specific key suffix (word + sense index -
       keys are dynamic per lookup, so a single static selector can't
       name them all; same substring-match trick as the Add Word input's
       versioned key below). row/wrap turns Streamlit's default one-
       widget-per-line stacking into a paragraph that wraps across lines
       like real text; the button styling strips the popover trigger
       down from "button" to "word" (no border/background, bold to match
       the plain-text tokens sitting next to it, underline only on hover
       so it's still discoverable as clickable). */
    [class*="st-key-defword_"] {{
        flex-direction: row;
        flex-wrap: wrap;
        align-items: baseline;
        row-gap: 0.1rem;
        column-gap: 0.5rem;
        /* Gap between senses, measured (not guessed) down to a real 30%
           cut. Streamlit puts its own ~16px base gap between sibling
           containers regardless of this margin, so margin-bottom alone
           barely moved the total visible gap (measured: 0.15rem still
           produced an 18.4px total gap either way). Getting an actual
           30% reduction of that 18.4px (-> ~12.9px) means pulling back
           INTO Streamlit's own base gap with a negative margin here,
           not just shrinking what little margin we control. The last
           sense keeps margin-bottom at the original 0.15rem (below) so
           the gap down to Usage/examples - 18.4px, unmeasured-but-
           unchanged - stays exactly where it was. */
        margin-bottom: -0.195rem;
    }}
    [class*="st-key-defword_"][class*="_last"] {{
        margin-bottom: 0.15rem;
    }}
    [class*="st-key-defword_"] [data-testid="stElementContainer"],
    [class*="st-key-defword_"] [data-testid="stLayoutWrapper"] {{
        flex: 0 0 auto !important;
        width: fit-content !important;
        /* Streamlit's own base styles put a ~16px min-width on these
           wrappers - invisible for any real word (its natural content
           width already clears 16px), but it padded out single-letter
           words like "a" to that floor, making them look stretched with
           extra trailing space compared to their neighbors. */
        min-width: 0 !important;
    }}
    /* Plain-text tokens (unclickable punctuation, the sense-number
       prefix) stay at normal weight by default - only the clickable
       words themselves (styled via the button rule below) are bold.
       The "N." prefix is still bold despite this: it's written as
       markdown **N.** (see _render_clickable_text), which produces a
       <strong> tag that renders bold on its own regardless of its
       parent <p>'s weight. Font-size is nudged down slightly (16px -
       15px) purely to correct an optical illusion, not a real size
       difference - confirmed via computed styles that normal-weight
       and the buttons' bold text were BOTH already set to 16px, but a
       normal-weight glyph's thinner stroke fills less of its own
       character box than a bold glyph at the identical declared size,
       which reads as "looks bigger" next to bold neighbors. */
    [class*="st-key-defword_"] [data-testid="stMarkdown"] p {{
        margin: 0;
        font-weight: 400;
        font-size: 15px;
    }}
    [class*="st-key-defword_"] button {{
        border: none;
        background: transparent;
        box-shadow: none;
        padding: 0;
        margin: 0;
        font-weight: 700;
        font-size: inherit;
        color: inherit;
    }}
    [class*="st-key-defword_"] button:hover {{
        text-decoration: underline;
        color: #0270FE;
    }}
    /* st.popover renders its own "expand_more" chevron glyph next to the
       label by default - hidden here so a word looks like plain text
       until clicked, with nothing visually marking it as interactive.
       Scoped to defword_ popovers only; My Words' Filter/Sort popovers
       keep their chevron, since those are meant to read as buttons.
       Hiding just the icon span (display:none, confirmed 0 width) isn't
       enough on its own - its own wrapper div (aria-hidden="true",
       flagged by Streamlit as decorative) sizes itself independently of
       that now-empty content and was still reserving a fixed 16px
       square next to every word, which is exactly the extra gap this
       was supposed to remove. Hiding that wrapper collapses the whole
       reserved slot instead of just what's inside it. */
    [class*="st-key-defword_"] button div[aria-hidden="true"] {{
        display: none;
    }}
    /* Hide Streamlit's native "Press Enter to apply" hint under the Add
       Word input - that instruction doesn't apply here (Look up/Add are
       separate buttons, not Enter-to-submit), so it's just noise. Matches
       on a substring since the widget key is versioned (add_word_0,
       add_word_1, ...) to reset the field after every Add. */
    [class*="st-key-add_word_"] [data-testid="InputInstructions"] {{
        display: none;
    }}

    /* My Words toolbar row: shrink every column to its actual content
       width instead of stretching proportionally - a column's width and
       its button's actual (much narrower) content width are two
       different things, and dead space after each left-aligned button
       was the real cause of "too spaced out", not the gap setting. Now
       that the search box is a fixed 152px rather than growing to fill
       the row, the row has real left-over space again - left as
       flex-start (the default) rather than space-between, so Filter/
       Sort/Select Page/Clear All/Trash sit right up against the search
       box and each other, with any unused space landing after Trash
       instead of getting distributed as gaps between them. */
    .st-key-words_toolbar_row [data-testid="stColumn"] {{
        width: auto !important;
        flex: 0 0 auto !important;
        min-width: 0 !important;
    }}
    /* ...except the search box's column - a fixed width rather than
       shrink-wrapping to its (much narrower) placeholder text, but not
       growing to fill the row either: 152px is half its old fill-the-
       row width (measured at ~304px once the other 5 buttons' own
       width was accounted for). */
    .st-key-words_toolbar_row [data-testid="stColumn"]:has(.st-key-words_search_col) {{
        flex: 0 0 152px !important;
        width: 152px !important;
        min-width: 0 !important;
    }}

    /* Add Word's toolbar row - same shrink-wrap-everything base as My
       Words' above, but unlike My Words' search box, this one does NOT
       grow to fill the row - it's a fixed, deliberately-narrowed width
       (see .st-key-addword_search_col below) with Look Up/Add Word
       sitting right up against it, not pinned off at the row's far
       right edge. */
    .st-key-addword_toolbar_row [data-testid="stColumn"] {{
        width: auto !important;
        flex: 0 0 auto !important;
        min-width: 0 !important;
    }}
    /* ~40% narrower than this column's old fill-the-row width (roughly
       488px in a 704px-wide row once the two buttons' own width was
       accounted for) - 300px is that 488px minus ~40%. */
    .st-key-addword_toolbar_row [data-testid="stColumn"]:has(.st-key-addword_search_col) {{
        width: 300px !important;
    }}

    /* Advanced tab's Peak usage / Lowest usage / trend-note row: two
       small stat cards shrink-wrapped to their own content, with the
       trend note (longer, variable-length sentence) filling whatever
       width is left to their right - same shrink-then-grow trick as
       the toolbar rows above, just applied to 3 columns instead of 2. */
    [class*="st-key-trend_stat_row_"] [data-testid="stColumn"] {{
        width: auto !important;
        flex: 0 0 auto !important;
        min-width: 0 !important;
    }}
    [class*="st-key-trend_stat_row_"] [data-testid="stColumn"]:has([class*="st-key-trend_note_"]) {{
        flex: 1 1 auto !important;
        min-width: 160px !important;
    }}
    [class*="st-key-trend_stat_peak_"], [class*="st-key-trend_stat_low_"] {{
        border: 1px solid rgba(0, 29, 86, 0.15);
        border-radius: 8px;
        padding: 0.5rem 0.9rem;
        min-width: 96px;
        /* Streamlit wraps a single st.markdown call's content in its own
           internal flex row, auto-sized for ONE line - a second
           block-level line inside that same call (label above value)
           renders past that wrapper's bottom edge instead of growing
           it (confirmed via getBoundingClientRect: the wrapper's own
           reported height came in ~14px short of its two children's
           actual combined height, every time, regardless of any CSS
           height/min-height overrides on that inner wrapper itself -
           whatever sizes it isn't reading our CSS). Padding the OUTER
           card (which we do control) past what the mis-measured inner
           wrapper reports is what actually stops the text from
           visually poking out past the card's own border. */
        min-height: 62px;
    }}
    [class*="st-key-trend_stat_peak_"] .stat-label,
    [class*="st-key-trend_stat_low_"] .stat-label {{
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        font-weight: 700;
        color: rgba(0, 29, 86, 0.55);
    }}
    [class*="st-key-trend_stat_peak_"] .stat-value,
    [class*="st-key-trend_stat_low_"] .stat-value {{
        font-size: 17px;
        font-weight: 700;
        font-variant-numeric: tabular-nums;
    }}
    /* Trend note gets the same border as the stat cards plus a left
       accent stripe, marking it as the interpretive one of the three -
       the other two are bare facts read off the chart, this one is a
       computed judgment call (see trends.trend_summary's FLAT_THRESHOLD_PCT). */
    [class*="st-key-trend_note_"] {{
        border: 1px solid rgba(0, 29, 86, 0.15);
        border-left: 3px solid #5BABFB;
        border-radius: 8px;
        padding: 0.5rem 0.9rem;
        background: #EAF2FE;
        /* Same under-reported-inner-wrapper issue and same fix as the
           stat cards above - the direction sentence and the per-
           million detail line are two lines inside one markdown call. */
        min-height: 58px;
    }}
    [class*="st-key-trend_note_"] .trend-arrow {{
        color: #0270FE;
        margin-right: 0.3rem;
    }}
    [class*="st-key-trend_note_"] .trend-direction {{
        font-weight: 700;
    }}
    [class*="st-key-trend_note_"] .trend-detail {{
        display: block;
        margin-top: 0.15rem;
        font-size: 11.5px;
        color: rgba(0, 29, 86, 0.65);
        font-variant-numeric: tabular-nums;
    }}
    [class*="st-key-trend_note_"] [data-testid="stMarkdown"] p {{
        margin: 0;
        font-size: 13px;
        line-height: 1.5;
    }}

    /* Popovers - My Words' filter/sort, and every clickable definition
       word's Look up/Add (_render_clickable_text) - all narrowed from
       the ~320px default. Popovers render in a portal straight under
       <body> (not inside our normal block-container tree), so this
       can't be scoped via the st-key trick used elsewhere; every
       popover in the app wants roughly this width anyway, so one
       unscoped rule covers all of them. */
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

    /* Quiz Me's Submit/No Clue pair - same shrink-wrap as elsewhere so
       No Clue sits right next to Submit instead of far off to the right
       of a wide proportional column. */
    .st-key-quiz_submit_row [data-testid="stColumn"] {{
        width: auto !important;
        flex: 0 0 auto !important;
        min-width: 0 !important;
    }}
    /* Word header (word + speaker icon, rendered via speaker.word_header
       as an iframe component) down to the part-of-speech/pronunciation
       caption below it, in both Quiz Me and Add Word - measured at 35px
       total (19px of dead space baked into the iframe's own fixed
       height, since its content only ever renders 37px tall against a
       56px iframe, plus Streamlit's normal 16px inter-element gap after
       it). Trimming the iframe's own height (see speaker.word_header)
       handles the first 19px; this negative margin closes the
       remaining bit needed to land on a 60% cut overall (35px -> 14px).
       Substring match, not an exact key - there are 3 call sites (Quiz
       Me pre/post-grading, Add Word), each needing its OWN distinct key
       (StreamlitDuplicateElementKey if two elements share one literal
       key, even across different tabs - confirmed live), but all 3
       want identical spacing, so "word_header_row" is a common prefix
       on every one of them rather than 3 separate near-duplicate rules. */
    [class*="st-key-word_header_row"] {{
        margin-bottom: -0.3125rem;
    }}
    /* Quiz Me's "Dictionary definition:" heading sat a full 16px
       (Streamlit's default inter-element gap, measured) above the first
       numbered sense - halved to 8px with a negative margin-bottom on
       the heading's own wrapper, same technique as the defword_ sense-
       to-sense spacing above. */
    .st-key-quiz_def_heading {{
        margin-bottom: -0.5rem;
    }}
    /* Same halving for the "Example:" line's own gap down to Synonyms -
       measured at the same 16px baseline as the heading above. */
    .st-key-quiz_def_example {{
        margin-bottom: -0.5rem;
    }}
    /* Every defword_ row specific to Quiz Me (senses, synonyms, antonyms
       - "defword_quiz" is only ever a substring of THESE keys, never
       Add Word's own defword_ keys, so this can't bleed into Add Word's
       spacing) gets its gap to whatever follows halved too - 18.4px
       (measured) down to 9.2px, regardless of whether that row happens
       to be the last of its kind (between senses, last sense -> Example,
       and Synonyms -> Antonyms all use this same defword_ row component
       and so share this same natural 18.4px baseline). !important
       because the shared, lower-specificity-losing [class*="st-key-
       defword_"][class*="_last"] rule above would otherwise still win
       on the _last-suffixed ones (equal source-order doesn't matter
       once specificity differs). */
    [class*="st-key-defword_quiz"] {{
        margin-bottom: -0.425rem !important;
    }}

    /* Header row: the logo overlays the top-right corner of the tabs
       row instead of sitting above it in its own banner row. Tried
       doing this with a real flex row first (tabs + logo as flex
       siblings), but Streamlit gives every container's children
       flex:1 1 0% / align-items:stretch by default for its normal
       vertical stacking - flipping just header_row to row-direction
       left that stretch behavior in place one level down, so the
       logo's wrapper kept inflating to 100% width via a circular
       auto-basis-vs-stretched-child loop. Absolute positioning sits
       outside that whole flex system, so it sidesteps the fight
       entirely: the tabs stay a normal untouched full-width block, and
       the logo overlays on top, positioned purely by pixels. -58px is
       (tab bar height 40px) - (logo height 98px), so the logo's
       bottom edge lines up with the tabs' underline and it grows
       upward into the header's blank space above, instead of downward
       over the word card underneath. */
    .st-key-header_row {{
        position: relative;
    }}
    .st-key-header_logo {{
        position: absolute;
        top: -58px;
        right: 0;
        z-index: 2;
        /* Every st.container is width:100% of its parent by default
           (that's a base Streamlit style, separate from the flex
           stretching fought above) - still true once absolutely
           positioned, which is why "right: 0" alone wasn't enough to
           shrink this to the image's actual width. */
        width: fit-content;
    }}
    /* Below ~480px the 4 tab labels alone eat most of the row, and the
       147px logo starts overlapping "Progress" - simplest fix is to
       drop the logo on narrow screens rather than shrink it further
       (it'd stop being recognizable). The tabs still work fine full-
       width on their own without it. */
    @media (max-width: 480px) {{
        .st-key-header_logo {{
            display: none;
        }}
    }}

    /* Progress tab: mastery donut. All of a card's content is written as
       ONE st.markdown call using <span> (never <div>) for every line -
       a <div> inside what Streamlit renders as a <p> gets auto-closed
       and reparented by the browser's own HTML parser (a <p> can't
       legally contain a <div>), which silently escapes that content
       from the wrapper Streamlit sizes around; a <span> stays a valid
       child of <p> even styled display:block, so the wrapper still
       measures its real content. min-height stays on as a second,
       independent safety net regardless. */
    .st-key-progress_donut_card {{
        display: flex;
        flex-direction: column;
        align-items: center;
        text-align: center;
        border: 1px solid rgba(0, 29, 86, 0.15);
        border-radius: 10px;
        padding: 16px 18px;
        min-height: 300px;
    }}
    .st-key-progress_donut_card .donut-wrap {{
        display: block;
        position: relative;
        width: 148px;
        height: 148px;
        margin: 6px auto 14px;
    }}
    .st-key-progress_donut_card .donut {{
        display: block;
        width: 148px;
        height: 148px;
        border-radius: 50%;
    }}
    .st-key-progress_donut_card .donut-hole {{
        display: flex;
        position: absolute;
        top: 18px; left: 18px;
        width: 112px; height: 112px;
        border-radius: 50%;
        background: #FFFFFF;
        flex-direction: column;
        align-items: center;
        justify-content: center;
    }}
    .st-key-progress_donut_card .donut-n {{
        display: block;
        font-size: 26px;
        font-weight: 800;
        font-variant-numeric: tabular-nums;
        line-height: 1;
    }}
    .st-key-progress_donut_card .donut-lbl {{
        display: block;
        font-size: 10.5px;
        color: rgba(0, 29, 86, 0.55);
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-top: 3px;
    }}
    .st-key-progress_donut_card .dl-row {{
        display: flex;
        align-items: center;
        gap: 7px;
        font-size: 12.5px;
        width: 100%;
        max-width: 200px;
        margin: 3px auto 0;
    }}
    .st-key-progress_donut_card .dl-dot {{
        display: inline-block;
        width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0;
    }}
    .st-key-progress_donut_card .dl-lbl {{ color: rgba(0, 29, 86, 0.65); flex: 1; text-align: left; }}
    .st-key-progress_donut_card .dl-val {{ font-weight: 700; font-variant-numeric: tabular-nums; }}

    /* Progress tab: streak / average-accuracy stat cards, right of the
       donut - same span-based single-call approach and min-height
       safety net as the donut card above. */
    .st-key-progress_streak_card, .st-key-progress_accuracy_card {{
        border: 1px solid rgba(0, 29, 86, 0.15);
        border-radius: 10px;
        padding: 16px 18px;
        min-height: 90px;
        display: flex;
        flex-direction: column;
        justify-content: center;
    }}
    .st-key-progress_streak_card {{ border-color: #5BABFB; margin-bottom: 14px; }}
    .st-key-progress_streak_card .stat-top, .st-key-progress_accuracy_card .stat-top {{
        display: flex;
        align-items: baseline;
        gap: 8px;
    }}
    .st-key-progress_streak_card .stat-icon {{ font-size: 20px; line-height: 1; }}
    .st-key-progress_streak_card .stat-value, .st-key-progress_accuracy_card .stat-value {{
        font-size: 28px;
        font-weight: 800;
        font-variant-numeric: tabular-nums;
        line-height: 1;
    }}
    .st-key-progress_streak_card .stat-label, .st-key-progress_accuracy_card .stat-label {{
        display: block;
        font-size: 12px;
        font-weight: 700;
        color: rgba(0, 29, 86, 0.55);
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-top: 6px;
    }}
    .st-key-progress_streak_card .stat-note {{
        display: block;
        font-size: 11.5px;
        color: rgba(0, 29, 86, 0.55);
        margin-top: 3px;
        line-height: 1.4;
    }}

    /* Progress tab: legend under the accuracy/words-quizzed combo chart -
       everything inline (no stacked lines), so this one needs neither
       the span trick nor a min-height override. */
    .st-key-progress_chart_legend .cl-row {{
        display: inline-flex;
        align-items: center;
        gap: 6px;
        margin-right: 20px;
        font-size: 12px;
        color: rgba(0, 29, 86, 0.65);
    }}
    .st-key-progress_chart_legend .cl-swatch-bar {{
        display: inline-block;
        width: 14px; height: 10px; border-radius: 2px;
        background: #DCE8FB;
        border-top: 2px solid #5BABFB;
        vertical-align: middle;
    }}
    .st-key-progress_chart_legend .cl-swatch-line {{
        display: inline-block;
        width: 16px; height: 2.5px;
        background: #0270FE;
        border-radius: 2px;
        vertical-align: middle;
    }}
    </style>
    """,
    unsafe_allow_html=True,
)

def _definition_senses(definition: str) -> list[str]:
    """dictionary.py's lookup_word() joins up to 3 senses with "\n" -
    split back apart here for display. A plain single-sense definition
    (the common case, and every word saved before this feature existed)
    is just a 1-element list, which is how the 3 call sites below tell
    "one sense, keep the existing single-line styling" apart from
    "several senses, render as a numbered list" without a separate flag."""
    return definition.split("\n")


def _render_difficulty_badge(word: str) -> None:
    """Small colored "Advanced" / "Common" / etc. label from
    frequency.py's offline wordfreq lookup - cheap enough (no network
    call) to show everywhere a word appears, unlike trends.usage_trend's
    live (and unofficial/best-effort) network call, which stays scoped
    to Add Word alone - see that call site's comment."""
    label, color, _ = frequency.difficulty(word)
    st.caption(f":{color}[{label} vocabulary]")


if "quiz_word" not in st.session_state:
    st.session_state.quiz_word = None
if "quiz_result" not in st.session_state:
    st.session_state.quiz_result = None
if "quiz_schedule" not in st.session_state:
    st.session_state.quiz_schedule = None
st.session_state.setdefault("quiz_form_version", 0)

# Tabs and logo share one header row instead of the logo getting a full
# banner row of its own above them - reclaims that row for quiz content.
# Deliberately NOT st.columns here: every tab's content (Quiz Me, Add
# Word, My Words, Progress - the whole app) lives inside the tabs
# widget, so nesting st.tabs() itself inside a column would shrink
# every tab's width down to that column's share, not just the tab bar.
# Instead, tabs and the logo are two plain siblings inside header_row,
# and CSS below turns that row into a flex row (see .st-key-header_row)
# so the logo sits inline at the row's right edge without touching the
# tabs' own width. The logo is sized to roughly half its old banner
# height (147px wide, ~98px tall at its 1.5:1 aspect ratio).
# key= + on_change="rerun" makes the active tab readable/settable via
# st.session_state["main_tab"] - by default a tab has no such handle at
# all. _run_lookup uses that to jump to Add Word on every "Look up"
# click, from anywhere (Quiz Me's feedback words, Add Word's own
# Thesaurus, wherever a clickable word shows up). As a side effect,
# on_change="rerun" also makes every tab's body lazy - only the active
# one actually runs each rerun - which is fine here: every session_state
# default these tabs rely on is already initialized at module level,
# outside any tab body (see the setdefault calls above/below), not
# inside one, so nothing depends on an inactive tab's code having run.
with st.container(key="header_row"):
    tab_quiz, tab_add, tab_words, tab_progress = st.tabs(
        ["Quiz Me", "Add Word", "My Words", "Progress"],
        key="main_tab", on_change="rerun",
    )
    with st.container(key="header_logo"):
        st.image(str(IMAGES_DIR / "vocapp_with_text.png"), width=147)

# Word-lookup/add helpers and the clickable-word renderer live here, ahead
# of every tab that uses them - Quiz Me (below) now renders clickable
# definitions/synonyms/antonyms too, not just Add Word, and Quiz Me's
# `with tab_quiz:` block runs earlier in the script than Add Word's own
# section, so these need to be defined before Quiz Me, not between the
# two (Streamlit re-runs this whole script top to bottom every time, so a
# def appearing textually after its first call site would NameError).

# Streamlit gotcha: popping a keyed widget's session_state entry does NOT
# reliably reset that widget on the next run - the frontend can keep
# showing the stale value. The bulletproof fix is to version the widget
# key itself, so "clearing the form" means rendering a brand-new widget
# with no prior state, not mutating an existing one.
st.session_state.setdefault("form_version", 0)
st.session_state.setdefault("addword_result", None)
st.session_state.setdefault("addword_looked_up_word", "")
# Same versioned-key trick, for every clickable word's Look up/Add
# popover (_render_clickable_text) - st.popover's open/closed state is
# its own client-side UI state, independent of Streamlit reruns, so
# clicking a button inside one and triggering a rerun does NOT close it
# on its own. Bumping this after every Look up/Add click (see
# _do_lookup_word/_do_add_word) forces that popover to remount under a
# new key next render, which drops the stale "open" state along with it -
# whether the click resulted in success or a "already in your list"
# warning either way.
st.session_state.setdefault("popover_version", 0)


def _word_key():
    return f"add_word_{st.session_state['form_version']}"


_MSG_ICONS = {"success": "✅", "warning": "⚠️", "error": "🚫"}


def _set_msg(kind, text):
    # st.toast() instead of an inline st.success/warning/error box: those
    # boxes lived inside tab_add's own render, and Streamlit keeps every
    # tab's last-rendered content sitting in the DOM (just hidden) when
    # you switch tabs - switching tabs doesn't rerun the script, so an
    # inline box stayed frozen on screen showing stale text ("Added
    # zephyr") no matter how long you'd been on a different tab, until
    # some unrelated interaction happened to trigger a rerun. A toast
    # renders as a top-right overlay outside any tab's DOM and auto-
    # dismisses on its own after a few seconds, so it can't get stuck
    # like that.
    st.toast(text, icon=_MSG_ICONS.get(kind))


def _run_lookup(word):
    """Shared by the main Look Up button and every clickable word's
    popover (definitions, synonyms, antonyms alike - see
    _render_clickable_text/_do_lookup_word)."""
    # Always land on Add Word - whether this lookup was triggered from
    # its own search box, a synonym click while already there, or a
    # word clicked in Quiz Me's feedback (a different top-level tab
    # entirely). Set unconditionally, before the lookup even resolves,
    # so a failed lookup's warning/error toast is also seen on the page
    # that's about to display it, not wherever the click happened to be.
    st.session_state["main_tab"] = "Add Word"
    try:
        info = dictionary.lookup_word(word)
        st.session_state["addword_result"] = info
        # MW's own spelling/capitalization (see dictionary.lookup_word's
        # docstring), not whatever case was typed/clicked - this is what
        # ends up both displayed as the word header and saved via _save.
        st.session_state["addword_looked_up_word"] = info["word"]
        # However the lookup was triggered - typing a fresh word into
        # the search box, or clicking a word inside Thesaurus/Advanced -
        # land back on Definition rather than leaving whatever sub-tab
        # happened to be open showing the NEW word's data there, which
        # reads as "did my click even do anything?" more than as
        # "you're now looking at a different word."
        st.session_state["addword_subtab"] = "Definition"
    except dictionary.LookupNotFound:
        st.session_state["addword_result"] = None
        st.session_state["addword_looked_up_word"] = ""
        _set_msg("warning", f"No dictionary entry found for '{word}'.")
    except requests.RequestException:
        st.session_state["addword_result"] = None
        st.session_state["addword_looked_up_word"] = ""
        _set_msg("error", "Dictionary lookup failed (network error) - try again.")


def _reset_form_after_add(clear_search=True):
    # clear_search is False when the add came from a clickable-word
    # popover (a synonym, an antonym, a word inside the definition)
    # rather than the main Word field/Add Word button - that word is
    # usually NOT the one currently searched/displayed, so wiping the
    # search box and the looked-up result out from under whatever the
    # user was actually looking at (e.g. "circumspect"'s Definition tab,
    # just because they quick-added one of its synonyms) is exactly the
    # "my search disappeared" bug this guards against.
    if clear_search:
        st.session_state["form_version"] += 1  # next render uses a fresh, empty Word field
        st.session_state["addword_result"] = None
        st.session_state["addword_looked_up_word"] = ""
    # Only force Quiz Me to re-pick if it doesn't already have a word in
    # play - the deck being empty, or "all caught up" with nothing due,
    # are the cases this word could actually change. If a quiz is
    # already in progress (a word showing, possibly already graded),
    # adding some unrelated word - including via a clickable-word
    # popover from right inside Quiz Me's own feedback screen - shouldn't
    # yank that away out from under the user; they just added a word,
    # they didn't ask to abandon what they were looking at.
    if st.session_state.get("quiz_word") is None:
        st.session_state.quiz_result = None
        st.session_state.quiz_schedule = None
        st.session_state["quiz_form_version"] += 1


def _save(word, info, clear_search=True):
    # db.add_word() is an upsert (see its docstring) - re-adding an
    # existing word refreshes its definition rather than erroring or
    # duplicating, which is useful for corrections. But that also means
    # accidentally re-adding a word you forgot you already had silently
    # "succeeds" with no sign anything was different - check first so
    # the message can tell those two cases apart.
    already_had_it = db.get_word(word) is not None
    db.add_word(
        word, info["definition"], info["part_of_speech"], info["example"],
        info["synonyms"], info["phonetic"], info["audio_url"],
        info["antonyms"], info["etymology"],
    )
    _reset_form_after_add(clear_search=clear_search)
    if already_had_it:
        _set_msg("warning", f"**{word}** is already in your list.")
    else:
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
        # cached["word"] (MW's own spelling), not the typed word - see
        # dictionary.lookup_word's docstring.
        _save(cached["word"], cached)
        return
    try:
        info = dictionary.lookup_word(word)
    except dictionary.LookupNotFound:
        _set_msg("error", f"'{word}' isn't in the dictionary - check the spelling.")
        return
    except requests.RequestException:
        _set_msg("error", "Dictionary lookup failed (network error) - try again.")
        return
    _save(info["word"], info)


def _do_lookup_word(word):
    """Shared by every clickable word rendered via _render_clickable_text
    - definitions, synonyms, antonyms alike."""
    st.session_state["popover_version"] += 1  # see the setdefault's comment above
    st.session_state[_word_key()] = word
    _run_lookup(word)


def _do_add_word(word):
    """Shared by every clickable word rendered via _render_clickable_text
    - definitions, synonyms, antonyms alike. clear_search=False - this
    word is a related word (a synonym, antonym, a word inside a
    definition), not necessarily the one currently searched/displayed,
    so adding it shouldn't clear the Word field or the lookup result
    still on screen (see _reset_form_after_add)."""
    st.session_state["popover_version"] += 1  # see the setdefault's comment above
    try:
        info = dictionary.lookup_word(word)
    except dictionary.LookupNotFound:
        _set_msg("error", f"'{word}' isn't in the dictionary.")
        return
    except requests.RequestException:
        _set_msg("error", "Dictionary lookup failed (network error) - try again.")
        return
    _save(info["word"], info, clear_search=False)


# Single-letter words ("a", "I") count as clickable too, same as every
# other word - the * (not +) is what allows a 1-character match. They're
# no more useful to look up than any short word, but singling them out
# as a separate "plain text" rendering path (as an earlier version of
# this did) meant they needed their own font-size/weight/spacing rules
# to avoid looking inconsistent with real words - simpler and more
# robust to just let every actual word (however short) go through the
# same popover styling. Only genuinely wordless tokens (MW's " : "
# clause separator, stray punctuation) still fall through to plain text.
_CLICKABLE_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")


def _render_clickable_text(text, key_prefix, prefix=None):
    """Renders `text` word-by-word so each real word is its own click
    target - a small popover offering Look up / Add - while still
    reading like normal prose rather than a wall of bordered buttons
    (see .st-key-clickable-text CSS above: the popover trigger is
    stripped down to look like plain text, and the row is a flex-wrap
    container so words wrap across lines like a real paragraph instead
    of Streamlit's default one-widget-per-line stacking).

    Tokens with no real word in them (MW's " : " clause separator,
    stray punctuation) render as plain unclickable text instead of a
    pointless empty popover - so a comma or colon doesn't turn into a
    dead click target. Same plain-text treatment for `prefix` (e.g. a
    "1." sense number) - rendered as the row's first item, inline with
    the words that follow, instead of on its own line above them.

    key_prefix must be unique per rendered string (callers include the
    sense/example index) so two definitions' word popovers, both
    starting their token count at 0, don't collide on widget keys."""
    tokens = [t for t in text.split(" ") if t]
    with st.container(key=key_prefix):
        if prefix:
            st.markdown(f"**{prefix}**")
        for i, token in enumerate(tokens):
            m = _CLICKABLE_WORD_RE.search(token)
            if not m:
                st.markdown(token)
                continue
            with st.popover(token, key=f"{key_prefix}_pop_{i}_{st.session_state['popover_version']}"):
                clean_word = m.group(0)
                st.button(
                    "🔍 Look up", key=f"{key_prefix}_lookup_{i}",
                    on_click=_do_lookup_word, args=(clean_word,), width="stretch",
                )
                st.button(
                    "➕ Add to My Words", key=f"{key_prefix}_add_{i}",
                    on_click=_do_add_word, args=(clean_word,), width="stretch",
                )


# grading.GradeResult.feedback carries its own inline markup - <right>
# around phrases the grader says the user got right, <wrong> around what
# they missed - written by the LLM as instructed in
# grading.GRADING_SYSTEM_PROMPT, not by anything in this file. Escaping
# the WHOLE raw string first (so any stray real <, >, & in the model's
# prose can't be mistaken for markup or break the HTML) turns our own
# <right>/<wrong> markers into escaped &lt;right&gt;/&lt;wrong&gt; too -
# matching against THAT escaped form, then substituting in real <b> tags
# around the (already-escaped, so still safe) captured text, is what
# keeps this from being an HTML-injection hole despite the content being
# LLM-generated. A tag the model forgot to close, or any other malformed
# markup, just fails to match and shows as literal escaped text instead
# of crashing or producing broken HTML.
_FEEDBACK_RIGHT_RE = re.compile(r"&lt;right&gt;(.*?)&lt;/right&gt;", re.DOTALL)
_FEEDBACK_WRONG_RE = re.compile(r"&lt;wrong&gt;(.*?)&lt;/wrong&gt;", re.DOTALL)


def _render_grading_feedback(feedback: str):
    escaped = html.escape(feedback)
    escaped = _FEEDBACK_RIGHT_RE.sub(r"<b style='color:#1E9E64;'>\1</b>", escaped)
    escaped = _FEEDBACK_WRONG_RE.sub(r"<b style='color:#C94A4A;'>\1</b>", escaped)
    st.markdown(escaped, unsafe_allow_html=True)


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

        if st.session_state.quiz_result is not None:
            # Next word lives up here (top-right, beside the word) once an
            # answer's been graded - no need to scroll past the feedback
            # to move on.
            c_word, c_next = st.columns([3, 1])
            with c_word:
                with st.container(key="word_header_row_quiz_active"):
                    speaker.word_header(word_row["word"], word_row.get("audio_url", ""))
            with c_next:
                if st.button("Next word →", key="next_word_btn_top"):
                    st.session_state.quiz_word = None
                    st.session_state.quiz_result = None
                    st.session_state.quiz_schedule = None
                    st.session_state["quiz_form_version"] += 1
                    st.rerun()
        else:
            with st.container(key="word_header_row_quiz_pending"):
                speaker.word_header(word_row["word"], word_row.get("audio_url", ""))

        caption_bits = []
        if word_row["part_of_speech"]:
            caption_bits.append(word_row["part_of_speech"])
        if word_row["phonetic"]:
            caption_bits.append(word_row["phonetic"])
        if caption_bits:
            st.caption("  •  ".join(caption_bits))
        _render_difficulty_badge(word_row["word"])

        if st.session_state.quiz_result is None:
            answer = st.text_area(
                "Your definition", key=f"answer_box_{st.session_state.quiz_form_version}",
                height=100, placeholder="Type your definition...", label_visibility="collapsed",
            )
            # Shrink-wrapped so the two buttons sit right next to each
            # other instead of spread across a wide proportional column -
            # same fix applied to My Words' toolbar (see CSS above).
            with st.container(key="quiz_submit_row"):
                c_submit, c_noclue = st.columns(2, gap="small")
                with c_submit:
                    submit_clicked = st.button("Submit", type="primary", key="submit_btn")
                with c_noclue:
                    no_clue_clicked = st.button(
                        "No Clue", key="no_clue_btn",
                        help="Log this as a 0% miss instead of typing something just to submit",
                    )

            if submit_clicked:
                if not answer.strip():
                    st.warning("Type something first.")
                else:
                    with st.spinner("Grading..."):
                        try:
                            result = grading.grade_definition(
                                word_row["word"], word_row["definition"], answer
                            )
                            db.save_attempt(word_row["word"], answer, result.accuracy, result.feedback)
                            st.session_state.quiz_schedule = db.update_schedule(
                                word_row["word"], result.accuracy
                            )
                            st.session_state.quiz_result = result
                            st.session_state.last_answer = answer
                            st.rerun()
                        except RuntimeError as e:
                            st.error(str(e))

            if no_clue_clicked:
                # No AI grading call needed - there's nothing to grade, so
                # this is a straight, automatic 0%/miss. "*silence*" (not
                # blank or whatever leftover text sat in the box) is what
                # gets logged as the answer, both here and in My Words'
                # attempt history, so it reads clearly as "skipped" rather
                # than a real, low-effort typed guess.
                result = grading.GradeResult(
                    accuracy=0, feedback="No definition provided - marked as a miss.",
                )
                db.save_attempt(word_row["word"], "*silence*", result.accuracy, result.feedback)
                st.session_state.quiz_schedule = db.update_schedule(word_row["word"], result.accuracy)
                st.session_state.quiz_result = result
                st.session_state.last_answer = "*silence*"
                st.rerun()
        else:
            r = st.session_state.quiz_result
            st.markdown(f"**Your answer:** {st.session_state.last_answer}")
            color = "green" if r.accuracy >= 70 else ("orange" if r.accuracy >= 40 else "red")
            st.markdown(f"### :{color}[{r.accuracy}% correct]")
            # Feedback comes right under the score, ahead of the
            # dictionary reference material below - it's the direct
            # answer to "how did I do," so it shouldn't require
            # scrolling past the definition/synonyms/antonyms to reach.
            # Single output block instead of separate got-right/got-missed
            # lists - the feedback string itself carries <right>/<wrong>
            # markup around the key phrases, rendered as bold green/red
            # inline (see _render_grading_feedback).
            _render_grading_feedback(r.feedback)
            def_senses = _definition_senses(word_row["definition"])
            # Always numbered, even for a single sense - see the matching
            # comment in the Add Word section for why (a single *merged*
            # sense should read the same as a single *genuinely one-
            # sense* word, not differently depending on which it was).
            # Every real word here is its own click target (look up /
            # add), same as Add Word - "defword_" in the key prefix is
            # what makes the existing .st-key-defword_* CSS (spacing,
            # chevron-hiding, the plain-text-vs-button styling) apply
            # here too, for free.
            with st.container(key="quiz_def_heading"):
                st.markdown("**Dictionary definition:**")
            for i, s in enumerate(def_senses, 1):
                row_key = f"defword_quizdef_{word_row['word']}_{i}" + ("_last" if i == len(def_senses) else "")
                _render_clickable_text(s, key_prefix=row_key, prefix=f"{i}.")
            if word_row["example"]:
                with st.container(key="quiz_def_example"):
                    st.markdown(f"*Example: {word_row['example']}*")
            if word_row["synonyms"]:
                _render_clickable_text(
                    word_row["synonyms"], key_prefix=f"defword_quizsyn_{word_row['word']}_last",
                    prefix="Synonyms:",
                )
            if word_row["antonyms"]:
                _render_clickable_text(
                    word_row["antonyms"], key_prefix=f"defword_quizant_{word_row['word']}_last",
                    prefix="Antonyms:",
                )

            sched = st.session_state.quiz_schedule
            if sched:
                st.caption(
                    f"📅 Next review: {sched['next_review_date']:%b %d, %Y} "
                    f"(in {sched['interval_days']} day(s))"
                )

# ------------------------------------------------------------
# Add Word
# ------------------------------------------------------------
# Lookup-only: the user never types their own definition (dictionary
# accuracy was the whole point of switching to Merriam-Webster - see
# dictionary.py), so there's no manual-entry fallback here. The bottom
# of the tab stays blank until a lookup - via the book button, or a
# synonym chip - actually succeeds; addword_result holds that lookup's
# data and is what "Add" saves.
with tab_add:
    # No title - the tab label ("Add Word") already says what this is,
    # and this row is the first thing on the tab now instead of sitting
    # below one. Same toolbar-row pattern as My Words' filter/search row
    # (see .st-key-addword_toolbar_row CSS): every column shrink-wraps
    # to its actual content width EXCEPT the word input's, which grows
    # to fill the row - same :has() override trick, same reason (a
    # search-style input should read as wide, not collapse to its
    # placeholder's width).
    with st.container(key="addword_toolbar_row"):
        c_word, c_lookup, c_add = st.columns([3, 1, 1.2], gap="small")
        with c_word:
            with st.container(key="addword_search_col"):
                st.text_input(
                    "Word", key=_word_key(), placeholder="🔎 Word to add...", label_visibility="collapsed",
                )
        with c_lookup:
            st.button("Look Up", key="lookup_btn", on_click=_do_lookup, help="Look up in the dictionary")
        with c_add:
            st.button("Add Word", key="add_btn", on_click=_do_add, help="Add to My Words")

    result = st.session_state.get("addword_result")
    if result:
        with st.container(key="addword_result"):
            with st.container(key="word_header_row_addword"):
                speaker.word_header(st.session_state["addword_looked_up_word"], result.get("audio_url", ""))
            meta_bits = [b for b in (result["part_of_speech"], result["phonetic"]) if b]
            if meta_bits:
                st.caption("  •  ".join(meta_bits))
            looked_up = st.session_state["addword_looked_up_word"]
            _render_difficulty_badge(looked_up)

            # Everything past the word header splits into sub-tabs
            # instead of one long scroll - Definition (senses + usage
            # examples), Thesaurus (synonyms/antonyms), Advanced (usage
            # trend + etymology). Nesting st.tabs() here is safe in a
            # way nesting st.columns() around the *outer* Quiz
            # Me/Add Word/... tabs wasn't (see header_row's comment,
            # much earlier in this file) - that problem was specifically
            # about squeezing st.tabs() itself inside a column, which
            # drags every tab PANEL's width down with it; a plain
            # container like this one doesn't have that issue.
            #
            # key + on_change="rerun" is what makes the active tab
            # readable/settable via st.session_state["addword_subtab"]
            # at all (Streamlit tabs are pure client-side UI state by
            # default) - _run_lookup uses that to jump back to
            # Definition after every lookup, however it was triggered.
            # As a side effect, on_change="rerun" also switches tabs
            # from "every tab's content computes on every rerun
            # regardless of which is open" to lazy (only the active
            # tab's code runs) - a genuine bonus here, since it means
            # Advanced's trend/etymology network call only fires while
            # Advanced is actually the open tab, not on every rerun.
            tab_definition, tab_thesaurus, tab_examples, tab_advanced = st.tabs(
                ["Definition", "Thesaurus", "Examples", "Advanced"],
                key="addword_subtab", on_change="rerun",
            )

            with tab_definition:
                def_senses = _definition_senses(result["definition"])
                # Every real word in the definition is its own click
                # target (look up / add) - see _render_clickable_text.
                # key_prefix includes the looked-up word so re-looking-
                # up a different word doesn't collide with this word's
                # still-mounted keys.
                for i, s in enumerate(def_senses, 1):
                    # Always numbered, even when there's only one sense -
                    # a single merged sense (see dictionary.py's
                    # _sense_groups: a base sense + its lettered sub-
                    # senses, like "step", collapses to one combined
                    # item) still reads as an enumerated/joined list, so
                    # it gets a "1." the same as any other sense would.
                    sense_prefix = f"{i}."
                    # Last sense gets a distinguishing "_last" key suffix
                    # so CSS can give it its own margin-bottom (see
                    # .st-key-defword_..._last below) - keeps the gap
                    # down to Usage unchanged while the gaps BETWEEN
                    # senses shrink independently.
                    row_key = f"defword_{looked_up}_{i}" + ("_last" if i == len(def_senses) else "")
                    _render_clickable_text(s, key_prefix=row_key, prefix=sense_prefix)

            with tab_thesaurus:
                # Comma-separated and clickable, same word-popover
                # treatment as the definition text above (reusing
                # _render_clickable_text directly - it already renders
                # whatever punctuation sits between words as plain
                # text, so joining with ", " and letting it split on
                # spaces gives "word," "word," "word" for free, with
                # each trailing comma just along for the ride in the
                # button's own label). "defword_" in the key prefix
                # is deliberate, not just a name - it's what makes the
                # existing .st-key-defword_* CSS (chevron-hiding,
                # spacing, the no-min-width fix) apply here too, instead
                # of needing a parallel set of rules for what's visually
                # the same kind of row.
                if result["synonyms"]:
                    st.caption("Synonyms")
                    _render_clickable_text(
                        ", ".join(result["synonyms"]), key_prefix=f"defword_syn_{looked_up}_last",
                    )
                else:
                    st.caption("No synonyms found for this word.")
                if result["antonyms"]:
                    st.caption("Antonyms")
                    _render_clickable_text(
                        ", ".join(result["antonyms"]), key_prefix=f"defword_ant_{looked_up}_last",
                    )
                else:
                    st.caption("No antonyms found for this word.")

            with tab_examples:
                # Live network call (freedictionaryapi.com), same lazy-tab
                # pattern as Advanced's trend lookup below - only fires
                # while this tab is actually open. MW's own examples
                # (result["examples"], up to 2) are already in hand from
                # the lookup that already ran; this just tops them up to
                # 3 total, best-effort.
                examples = usage_examples.combined_examples(
                    looked_up, result["part_of_speech"], result["examples"],
                )
                if examples:
                    for ex in examples:
                        st.markdown(f"- *{ex}*")
                else:
                    st.caption("No usage examples available for this word.")

            with tab_advanced:
                # Etymology leads the tab - it's the more stable, "read
                # once" fact about a word. Usage-over-time (live network
                # call, see below) is the more exploratory piece, so it
                # follows.
                if result["etymology"]:
                    st.caption("Etymology")
                    st.markdown(result["etymology"])
                else:
                    st.caption("No etymology available for this word.")

                # Live network call to an unofficial Google endpoint (see
                # trends.py) - unlike the difficulty badge above
                # (offline, always shown), this is best-effort and
                # scoped to Add Word only: it's one lookup for the
                # single word being looked up here, not something worth
                # firing off for every word on a 20-per-page My Words
                # listing.
                trend = trends.usage_trend(looked_up)
                if trend:
                    summary = trends.trend_summary(trend)
                    arrow = {"rising": "↑", "falling": "↓", "flat": "→"}[summary["direction"]]
                    direction_label = {"rising": "Rising", "falling": "Falling", "flat": "Flat"}[summary["direction"]]
                    # +1: window_start_year and window_end_year are both
                    # inclusive endpoints (e.g. 2000 and 2019 span 20
                    # years of data, not 19).
                    window_span = summary["window_end_year"] - summary["window_start_year"] + 1
                    # +.0f always includes the sign (+28%, -66%, +1%) -
                    # reads fine even for "flat", where a tiny +1%/-1%
                    # reinforces "barely moved" rather than needing its
                    # own separate wording.
                    change_phrase = f"{summary['pct_change']:+.0f}% since {summary['window_start_year']}"

                    with st.container(key=f"trend_stat_row_{looked_up}"):
                        stat_peak_col, stat_low_col, trend_note_col = st.columns([1, 1, 3], gap="small")
                        with stat_peak_col:
                            with st.container(key=f"trend_stat_peak_{looked_up}"):
                                st.markdown(
                                    f"<div class='stat-label'>Peak usage</div>"
                                    f"<div class='stat-value'>{summary['peak_year']}</div>",
                                    unsafe_allow_html=True,
                                )
                        with stat_low_col:
                            with st.container(key=f"trend_stat_low_{looked_up}"):
                                st.markdown(
                                    f"<div class='stat-label'>Lowest usage</div>"
                                    f"<div class='stat-value'>{summary['low_year']}</div>",
                                    unsafe_allow_html=True,
                                )
                        with trend_note_col:
                            with st.container(key=f"trend_note_{looked_up}"):
                                st.markdown(
                                    f"<span class='trend-arrow'>{arrow}</span>"
                                    f"<span class='trend-direction'>{direction_label}</span> "
                                    f"over the last {window_span} years; {change_phrase}."
                                    f"<span class='trend-detail'>"
                                    f"{summary['window_start_value']:.2f} → {summary['window_end_value']:.2f} "
                                    f"per million words ({summary['window_start_year']}–{summary['window_end_year']})"
                                    f"</span>",
                                    unsafe_allow_html=True,
                                )

                    trend_df = pd.DataFrame(
                        {"Year": trend["years"], "Uses per million words": trend["per_million"]}
                    )
                    # st.line_chart's default number formatting adds
                    # thousands-separators to any large-enough numeric
                    # axis, which turns years into "1,800", "1,900", ...
                    # - an explicit Altair chart is what it takes to
                    # override that (format="d" - a plain integer, no
                    # grouping separator).
                    year_chart = (
                        alt.Chart(trend_df)
                        .mark_line(color="#0270FE")
                        .encode(
                            x=alt.X("Year:Q", axis=alt.Axis(format="d"), title="Year"),
                            y=alt.Y("Uses per million words:Q", title="Uses per million words"),
                        )
                    )
                    st.altair_chart(year_chart, use_container_width=True)
                else:
                    st.caption("No usage-over-time data available for this word.")

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
st.session_state.setdefault("words_search", "")


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
        search_query = st.session_state["words_search"].strip().lower()
        filtered = all_words if filter_choice == "All" else [w for w in all_words if _word_status(w) == filter_choice]
        if search_query:
            filtered = [w for w in filtered if search_query in w["word"].lower()]

        # Sort/paginate up front, empty-safe, so the toolbar below (which
        # holds the filter control itself) always renders - even when the
        # current filter matches zero words. It used to live inside the
        # "filtered is non-empty" branch, which meant picking a filter
        # with no matches hid the only control that could change it back.
        words = _sort_words(filtered, st.session_state["words_sort"])
        total = len(words)
        total_pages = -(-total // WORDS_PAGE_SIZE)  # ceil division
        # Clamp in case the filtered count shrank since the page was
        # set (e.g. deleting the last word on the last page, or a filter
        # change leaving fewer/zero pages than the stored page number).
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

        # Search box first, taking the remaining space on the far left,
        # then Filter, Sort, Select Page, Clear All, Trash shrink-wrapped
        # to their own content after it (a column's width and its
        # button's actual, much narrower content width are two different
        # things, hence the shrink-wrap) - the search column is
        # deliberately left to grow (see the CSS's
        # :has(.st-key-words_search_col) override) so it reads as a search
        # bar instead of collapsing to its placeholder's width.
        with st.container(key="words_toolbar_row"):
            c_search, c_filter, c_sort, c_selall, c_clearall, c_trash = st.columns(
                [3, 0.6, 0.6, 1, 1, 1], gap="small",
            )
            with c_search:
                with st.container(key="words_search_col"):
                    st.text_input(
                        "Search", key="words_search", placeholder="🔎 Search words...",
                        on_change=_reset_words_page, label_visibility="collapsed",
                    )
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

        if not filtered:
            if search_query:
                scope = f" in '{filter_choice}'" if filter_choice != "All" else ""
                st.info(f"No words matching '{search_query}'{scope}.")
            else:
                st.info(f"No words in '{filter_choice}' right now.")
        else:
            for w in page_words:
                avg = f"{w['avg_accuracy']:.0f}%" if w["avg_accuracy"] is not None else "not quizzed yet"
                row_check, row_expander = st.columns([1, 11], gap="xsmall")
                with row_check:
                    st.checkbox("Select", key=f"sel_{w['word']}", label_visibility="collapsed")
                with row_expander:
                    with st.expander(f"{w['word']}  —  {avg}"):
                        speaker.play_button(w["word"], w.get("audio_url", ""))
                        def_senses = _definition_senses(w["definition"])
                        # Always numbered, even for a single sense - see
                        # the matching comment in the Add Word section.
                        st.markdown("**Definition:**")
                        for i, s in enumerate(def_senses, 1):
                            st.markdown(f"{i}. {s}")
                        meta_bits = [b for b in (w["part_of_speech"], w["phonetic"]) if b]
                        if meta_bits:
                            st.caption("  •  ".join(meta_bits))
                        _render_difficulty_badge(w["word"])
                        if w["example"]:
                            st.markdown(f"*Example: {w['example']}*")
                        if w["synonyms"]:
                            st.caption(f"Synonyms: {w['synonyms']}")
                        if w["antonyms"]:
                            st.caption(f"Antonyms: {w['antonyms']}")
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

        # Donut (mastery composition) + streak/accuracy stat stack,
        # replacing the old 4-tile Total/Mastered/Learning/Needs Work
        # row - one glance at composition instead of reading 4 numbers
        # separately.
        c_donut, c_stats = st.columns([1, 1], gap="medium")
        with c_donut:
            total = stats["total"]
            # Percent-of-circle boundaries for the conic-gradient, in
            # Mastered -> Learning -> Needs Work order (matching the
            # legend below). A 0-width slice (e.g. 0 Mastered) just
            # doesn't render - no special-casing needed.
            mastered_pct = stats["mastered"] / total * 100
            learning_end_pct = mastered_pct + (stats["learning"] / total * 100)
            with st.container(key="progress_donut_card"):
                st.markdown(
                    "<span class='donut-wrap'>"
                    f"<span class='donut' style='background: conic-gradient("
                    f"#1E9E64 0% {mastered_pct:.3f}%, "
                    f"#0270FE {mastered_pct:.3f}% {learning_end_pct:.3f}%, "
                    f"#C94A4A {learning_end_pct:.3f}% 100%);'></span>"
                    "<span class='donut-hole'>"
                    f"<span class='donut-n'>{total}</span>"
                    "<span class='donut-lbl'>words</span>"
                    "</span>"
                    "</span>"
                    "<span class='dl-row'>"
                    "<span class='dl-dot' style='background:#1E9E64;'></span>"
                    "<span class='dl-lbl'>Mastered</span>"
                    f"<span class='dl-val'>{stats['mastered']}</span>"
                    "</span>"
                    "<span class='dl-row'>"
                    "<span class='dl-dot' style='background:#0270FE;'></span>"
                    "<span class='dl-lbl'>Learning</span>"
                    f"<span class='dl-val'>{stats['learning']}</span>"
                    "</span>"
                    "<span class='dl-row'>"
                    "<span class='dl-dot' style='background:#C94A4A;'></span>"
                    "<span class='dl-lbl'>Needs Work</span>"
                    f"<span class='dl-val'>{stats['needs_work']}</span>"
                    "</span>",
                    unsafe_allow_html=True,
                )
        with c_stats:
            # 10+ words/day, per the "consecutive days with at least 10
            # words quizzed" ask - see db.get_quiz_streak's docstring for
            # exactly how today's still-in-progress count is handled.
            streak = db.get_quiz_streak(threshold=10)
            with st.container(key="progress_streak_card"):
                st.markdown(
                    "<span class='stat-top'>"
                    "<span class='stat-icon'>🔥</span>"
                    f"<span class='stat-value'>{streak}</span>"
                    "</span>"
                    "<span class='stat-label'>Day streak (10+ words/day)</span>",
                    unsafe_allow_html=True,
                )
            if stats["overall_avg"] is not None:
                with st.container(key="progress_accuracy_card"):
                    st.markdown(
                        "<span class='stat-top'>"
                        f"<span class='stat-value'>{stats['overall_avg']}%</span>"
                        "</span>"
                        "<span class='stat-label'>Average Accuracy</span>",
                        unsafe_allow_html=True,
                    )

        # Combo chart: bars for daily quiz volume, a line for daily
        # average accuracy - replaces the two separate line/bar charts.
        # Different units (word count vs. percent), so they get their
        # own independent y-scales rather than sharing one axis; with
        # both axes hidden entirely (no numbers requested), every value
        # is written directly on its own mark instead - the count near
        # the BOTTOM of each bar, clear of the line, which sits higher.
        acc_trend = db.get_daily_accuracy_trend()
        words_trend = db.get_daily_words_quizzed_trend()
        if len(acc_trend) >= 2 and len(words_trend) >= 2:
            st.subheader("Accuracy over time")
            acc_df = pd.DataFrame(acc_trend, columns=["date", "avg_accuracy"])
            words_df = pd.DataFrame(words_trend, columns=["date", "words_quizzed"])
            # Plotted as an ordinal category ("Aug 14"), not a continuous
            # temporal scale - date:T's automatic tick-interval picker
            # chose an interval finer than a day for a 2-day-wide domain
            # (hours, going by the pixel spacing it produced) and then
            # formatted every one of those sub-day ticks with "%b %d"
            # anyway, so the same day label printed many times over
            # ("Aug 14" 8 times, confirmed live). There's no continuous
            # timeline to interpolate here - just 3 discrete daily
            # buckets - so ordinal sidesteps the whole tick-interval
            # question: exactly one tick per actual date, always.
            date_order = sorted(set(acc_df["date"]) | set(words_df["date"]))
            date_labels = {d: d.strftime("%b %d") for d in date_order}
            label_order = [date_labels[d] for d in date_order]
            acc_df["date_label"] = acc_df["date"].map(date_labels)
            words_df["date_label"] = words_df["date"].map(date_labels)
            # 30% up each bar's own height - always inside the bar
            # (unlike a fixed pixel offset, which could sit above a
            # very short bar), and reads as "near the bottom" either way.
            words_df["label_y"] = words_df["words_quizzed"] * 0.3
            words_df["zero"] = 0

            # Bars and line share ONE literal y-scale/domain rather than
            # Vega-Lite's own dual-independent-scale resolution - tried
            # that first (resolve_scale(y="independent") on 2 nested
            # alt.layer() groups), and the spec it produced was valid
            # (checked via combo.to_dict()) but rendered broken in the
            # browser: the line/point/text layer vanished entirely and
            # the date axis repeated once per sub-layer. Altair 6.2.2
            # targets the Vega-Lite v6 schema; Streamlit 1.61.1 bundles
            # its own (older) vega-embed runtime, and nested-layer scale
            # resolution is exactly the kind of newer feature that can
            # silently no-op on an older renderer. Manually rescaling
            # accuracy onto the bars' own count-based axis sidesteps the
            # feature entirely - one shared scale, one axis, no
            # resolve() call, so it only depends on basic layering,
            # which works fine. The label TEXT still shows the real
            # 0-100 accuracy value - only its plotted position is
            # transformed, into the upper part of the shared scale so
            # it reads as its own line above the bars, not squashed by
            # them.
            words_max = float(words_df["words_quizzed"].max())
            y_domain_max = words_max * 1.15 if words_max > 0 else 10.0
            acc_min = float(acc_df["avg_accuracy"].min())
            acc_max = float(acc_df["avg_accuracy"].max())
            if acc_max > acc_min:
                acc_df["plot_y"] = y_domain_max * (0.35 + 0.55 * (acc_df["avg_accuracy"] - acc_min) / (acc_max - acc_min))
            else:
                acc_df["plot_y"] = y_domain_max * 0.6
            shared_scale = alt.Scale(domain=[0, y_domain_max])
            # Vega-Lite's own x-axis feature turned out unreliable for
            # this specific layered chart in this Streamlit version - an
            # axis set on exactly one layer (others axis=None) rendered
            # no axis at all; set identically on every layer, it
            # rendered once PER layer instead of merging into one
            # (both confirmed live, independent of whether the date
            # field was temporal or ordinal). Simplest fix that doesn't
            # depend on that feature working: skip it entirely and draw
            # the date strings as one more plain text layer, exactly
            # like the value labels above them - same trick, not
            # dependent on axis merging behavior at all.
            date_x = alt.X("date_label:O", sort=label_order, axis=None)
            bar = (
                alt.Chart(words_df)
                .mark_bar(color="#DCE8FB", stroke="#5BABFB", strokeWidth=1.5, size=44,
                          cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
                .encode(
                    x=date_x,
                    y=alt.Y("words_quizzed:Q", axis=None, scale=shared_scale),
                    tooltip=[alt.Tooltip("date_label:O", title="Date"),
                             alt.Tooltip("words_quizzed:Q", title="Words quizzed")],
                )
            )
            bar_labels = (
                alt.Chart(words_df)
                .mark_text(fontWeight="bold", fontSize=11, color="#001D56")
                .encode(x=date_x, y=alt.Y("label_y:Q", axis=None, scale=shared_scale),
                        text=alt.Text("words_quizzed:Q"))
            )
            date_labels_layer = (
                alt.Chart(words_df)
                .mark_text(dy=16, fontSize=11, color="#94A6CC")
                .encode(x=date_x, y=alt.Y("zero:Q", axis=None, scale=shared_scale), text=alt.Text("date_label:O"))
            )
            line = (
                alt.Chart(acc_df)
                .mark_line(color="#0270FE", strokeWidth=2.5)
                .encode(
                    x=date_x,
                    y=alt.Y("plot_y:Q", axis=None, scale=shared_scale),
                    tooltip=[alt.Tooltip("date_label:O", title="Date"),
                             alt.Tooltip("avg_accuracy:Q", title="Accuracy", format=".1f")],
                )
            )
            line_points = (
                alt.Chart(acc_df)
                .mark_point(color="#0270FE", filled=True, size=40)
                .encode(x=date_x, y=alt.Y("plot_y:Q", axis=None, scale=shared_scale))
            )
            line_labels = (
                alt.Chart(acc_df)
                .mark_text(dy=-10, fontWeight="bold", fontSize=11, color="#0270FE")
                .encode(x=date_x, y=alt.Y("plot_y:Q", axis=None, scale=shared_scale),
                        text=alt.Text("avg_accuracy:Q", format=".1f"))
            )
            combo = alt.layer(bar, bar_labels, date_labels_layer, line, line_points, line_labels).properties(height=260)
            st.altair_chart(combo, use_container_width=True)

            with st.container(key="progress_chart_legend"):
                st.markdown(
                    "<span class='cl-row'><span class='cl-swatch-bar'></span>Words quizzed</span>"
                    "<span class='cl-row'><span class='cl-swatch-line'></span>Accuracy</span>",
                    unsafe_allow_html=True,
                )
        elif acc_trend or words_trend:
            st.caption("Quiz on a few more days to see a trend here.")
