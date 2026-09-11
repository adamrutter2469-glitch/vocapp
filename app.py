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
from datetime import timedelta
from pathlib import Path

import requests
import altair as alt
import pandas as pd
import streamlit as st
from PIL import Image
import auth
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

    /* My Words: each word's checkbox+expander row, narrowed 20% and
       centered per user request ("the width of the tile...is too
       large. Reduce by 20%"). Substring match, not an exact key - one
       distinct st.container key per word (the word itself, same
       pattern as the sel_ checkbox key below it), all sharing this
       width treatment. Scales row_check and row_expander down together
       since they're flex-sized off this container's own width, so
       their 1:11 proportion (checkbox : expander) is unaffected. */
    [class*="st-key-word_row_"] {{
        max-width: 80%;
        margin: 0 auto;
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

    /* Top bar: Menu icon on the left, identity (alias or email)
       centered, Log out icon on the right - see .st-key-nav_sidebar
       below for where the old tab bar/logo went. Plain flex row, not
       st.columns - st.columns' own children default to flex:1 1 0% /
       align-items:stretch, which fights arbitrary-width content like
       buttons (this exact problem, and why a plain container's direct
       children sidestep it, is documented at more length below on
       .st-key-nav_sidebar and was originally worked out for the old
       header_row this replaced). position:relative + the identity
       block's own position:absolute (below) is what gets it TRULY
       centered on the bar regardless of the Menu/Log out icons'
       widths, rather than just "centered in whatever space is left
       over" the way a 3-way justify-content split would - the same
       centering approach already worked out for the drawer's own logo. */
    .st-key-top_bar {{
        position: relative;
        display: flex;
        flex-direction: row;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 0.5rem;
        /* Same blue as a primary button (e.g. Submit) - confirmed live
           via getComputedStyle, not eyeballed, since "primaryColor" in
           .streamlit/config.toml (#0270FE) is a theme token Streamlit
           applies through its own internal styling, not something this
           file's own CSS can just reference by name. */
        background-color: #0270FE;
        /* 0.42rem, not the original 0.6rem - measured live (59.2px
           tall beforehand) and picked to land the bar's total height
           at ~90% of that, not just an eyeballed smaller number.
           Square corners now, not the 8px this started with. */
        padding: 0.42rem 1rem;
        border-radius: 0;
    }}
    .st-key-top_bar_identity {{
        position: absolute;
        left: 50%;
        top: 50%;
        transform: translate(-50%, -50%);
        /* Without this, the container stays Streamlit's default
           width:100% (same width as top_bar itself) - translateX(-50%)
           then centers that full-width BOX, which does nothing
           visible, and the caption text inside still reads as flush
           left (confirmed live). Centering has to be based on the
           text's own real width, not the row's. */
        width: fit-content;
    }}
    .st-key-top_bar_identity [data-testid="stCaptionContainer"] {{
        margin: 0;
        white-space: nowrap;
        /* White, readable against the bar's own blue fill - Streamlit's
           caption styling otherwise sets its own muted grey via a more
           specific rule, hence !important. 17px is 14px (this
           caption's own previous size, measured live) + ~20%. */
        color: #FFFFFF !important;
        font-size: 17px;
    }}
    /* font-weight specifically needs the nested <p>, not just its
       wrapper above - same "the real text lives one level deeper, with
       its own competing style" issue already hit (and fixed the same
       way) on the Menu/Log out buttons' own labels. */
    .st-key-top_bar_identity [data-testid="stCaptionContainer"] p {{
        font-weight: 700 !important;
    }}
    /* Menu: plain white icon, not a bordered button box - transparent
       background so the bar's own blue fill (above) shows through.
       Text label ("Navigation") and Log out's own text were both tried
       and then dropped again - icon-only for both, back to how this
       started. font-size bumped 30% (1.3rem -> 1.69rem) per user
       request ("make menu icon larger 30%"). */
    .st-key-menu_toggle_btn button {{
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
        color: #FFFFFF;
        font-size: 1.69rem;
        padding: 0.2rem 0.4rem;
    }}
    /* Log out: the inline SVG "door with an exit arrow" icon (the
       standard logout glyph, e.g. Feather/Lucide's own "log-out" icon)
       as a background-image, not the 🚪 door EMOJI first tried here -
       that rendered as a plain placeholder box (confirmed live, even
       on this Windows-flagged browser), a real risk of the same
       failure for at least some viewers rather than a guaranteed
       cross-platform glyph. An SVG baked directly into the CSS doesn't
       depend on any emoji font being installed at all. The button's
       own text ("Logout") stays in the DOM for accessibility - only
       hidden visually (color:transparent), not removed. */
    .st-key-logout_btn button {{
        background-color: transparent !important;
        background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='white' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4'/><polyline points='16 17 21 12 16 7'/><line x1='21' y1='12' x2='9' y2='12'/></svg>");
        background-repeat: no-repeat;
        background-position: center;
        background-size: 20px 20px;
        border: none !important;
        box-shadow: none !important;
        color: transparent;
        width: 32px;
        min-width: 0 !important;
        height: 32px;
        padding: 0;
    }}

    /* Left nav drawer (Quiz Me / Add Word / My Words / Progress) - only
       actually rendered (see app.py) while st.session_state["nav_open"]
       is True, so this CSS only has to style it, not hide/show it.
       position:fixed makes it overlay the page rather than push
       content over, which sidesteps needing real flex/grid page-level
       layout just to make room for a collapsible column. */
    .st-key-nav_sidebar {{
        position: fixed;
        top: 0;
        left: 0;
        height: 100vh;
        /* 40% narrower than the original 240px. No horizontal padding
           here at all (unlike the original, which had 1rem both
           sides) - the nav buttons are meant to run edge-to-edge now,
           so their padding has to come from somewhere that ISN'T a
           shared ancestor of theirs; see .st-key-nav_header_row, which
           adds its own left/right padding back just for the logo/✕
           row instead. */
        width: min(144px, 80vw);
        background: #FFFFFF;
        box-shadow: 2px 0 16px rgba(0, 29, 86, 0.18);
        z-index: 3000;
        /* Top padding well past 60px - Streamlit's own native toolbar
           (Deploy/Stop/⋮) is a fixed-position element covering roughly
           the page's top 60px with a z-index that beats content
           underneath it (same issue worked out earlier for the
           signed-in-as badge) - anything placed in that strip renders
           fine but silently can't be clicked, confirmed live for the
           ✕ close button before this fix (elementFromPoint at its own
           coordinates returned the toolbar's Deploy button, not it).
           64px is the minimum that clears it. */
        padding-top: 64px;
        overflow-y: auto;
        /* Streamlit lays out a container's direct children (header
           row, then each nav button) as a flex column with a real
           `gap` property, not just per-child margins - confirmed live
           earlier this session (account_row's DOM carried
           direction="column" alongside an actual CSS gap). Zeroed out
           entirely so the buttons stack with no space between them at
           all - the header row still gets its own visual separation
           from the button list via its own margin-bottom below. */
        gap: 0;
    }}
    /* Logo alone at the top of the drawer, centered on the row's full
       width. This row used to also carry a ✕ close button pinned to
       its top-right corner - dropped per user request (Menu already
       toggles the drawer open/closed, so a dedicated close icon was
       redundant), which is why this is back to a plain centered row
       instead of the position:relative/absolute pairing a two-item
       row needed. */
    .st-key-nav_header_row {{
        display: flex;
        justify-content: center;
        padding: 0 0.5rem;
        margin-bottom: 0.25rem;
    }}
    /* margin-bottom:0 kills a ~16px default bottom margin Streamlit
       puts on an element's own wrapper (confirmed live - the image's
       real rendered height and this row's own box height didn't
       match, and that gap was extra dead space stacking on top of
       this row's own margin-bottom above). */
    .st-key-nav_header_row [data-testid="stElementContainer"] {{
        width: fit-content;
        margin-bottom: 0;
    }}
    /* Nav buttons: full drawer width, touching (no gap - see the
       sidebar's own gap:0 above), square corners - reads as one stack
       of solid rectangles rather than a list of separate pill-shaped
       buttons. */
    .st-key-nav_sidebar [data-testid="stButton"] button {{
        width: 100%;
        justify-content: flex-start;
        border-radius: 0;
    }}

    /* About page: a bounded, independently-scrolling box for the plain-
       text summary, rather than just however tall the page happens to
       run. */
    .st-key-about_scroll {{
        max-height: 55vh;
        overflow-y: auto;
        border: 1px solid rgba(0, 29, 86, 0.15);
        border-radius: 8px;
        padding: 1rem 1.25rem;
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

    /* Progress tab: the combo chart itself gets an explicit pixel width
       (see chart_width in app.py, scaled to however many days/bars it's
       showing) rather than stretching to the container - this wrapper
       is what lets it overflow and scroll horizontally instead of
       getting clipped when that width exceeds the page. */
    .st-key-progress_chart_scroll {{
        overflow-x: auto;
        overflow-y: hidden;
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
        background: #5BABFB;
        border-top: 2px solid #001D56;
        vertical-align: middle;
    }}
    .st-key-progress_chart_legend .cl-swatch-line {{
        display: inline-block;
        width: 16px; height: 2.5px;
        background: #001D56;
        border-radius: 2px;
        vertical-align: middle;
    }}

    </style>
    """,
    unsafe_allow_html=True,
)

# Every viewer has to sign in with an allowlisted Google account before
# anything else on the page renders - see auth.py. Stashed in
# st.session_state, not a bare module-level variable - Streamlit reruns
# this whole script inside the SAME shared module namespace for every
# session on Streamlit Cloud's one shared process (confirmed by how
# r2_storage.py's own module-level caching already relies on exactly
# that persistence), so a plain `current_user_email = ...` here would be
# a mutable global two different friends' concurrent reruns could race
# on and clobber each other's identity mid-script. st.session_state is
# the one thing Streamlit actually guarantees is isolated per browser
# session.
st.session_state["user_id"] = auth.require_login()


def _uid() -> str:
    """This session's signed-in user's email - the id every db.py call
    below scopes its data by."""
    return st.session_state["user_id"]


# Navigation: a Menu button in the top bar toggles a left-side drawer
# (see .st-key-nav_sidebar CSS) listing the same 4 sections that used
# to be st.tabs() - replaced because the ask was specifically for a
# hamburger-menu drawer, not a tab bar. current_page drives which
# section's code runs below (each former `with tab_x:` block is now
# `if st.session_state["current_page"] == "X":`, otherwise unchanged -
# a plain if still only executes the matching section's body, so the
# old tabs' "inactive tab's code doesn't run" property carries over
# for free, no elif chain needed).
st.session_state.setdefault("current_page", "Quiz Me")
st.session_state.setdefault("nav_open", False)
_PAGES = ["Quiz Me", "Add Word", "My Words", "Progress"]
# Account/meta pages - listed at the bottom of the drawer, visually set
# apart from the 4 core pages above by a divider (see the drawer's own
# render below), not mixed into the same button stack.
_UTILITY_PAGES = ["Settings", "About", "App Ideas"]


def _toggle_nav():
    st.session_state["nav_open"] = not st.session_state["nav_open"]


def _select_page(page):
    st.session_state["current_page"] = page
    st.session_state["nav_open"] = False


def _display_identity() -> str:
    """The Settings-page alias, if the user's set one - otherwise their
    email. Checked on every rerun (one cheap query) rather than cached,
    so saving a new alias in Settings is reflected here immediately."""
    alias = db.get_user_settings(_uid())["alias"]
    return alias if alias else st.session_state["user_id"]


with st.container(key="top_bar"):
    st.button("☰", key="menu_toggle_btn", on_click=_toggle_nav)
    with st.container(key="top_bar_identity"):
        st.caption(f"Welcome {_display_identity()}!")
    st.button("Logout", key="logout_btn", on_click=st.logout)

if st.session_state["nav_open"]:
    with st.container(key="nav_sidebar"):
        with st.container(key="nav_header_row"):
            st.image(str(IMAGES_DIR / "vocapp_with_text.png"), width=125)
        for _page in _PAGES:
            st.button(
                _page, key=f"nav_btn_{_page}", on_click=_select_page, args=(_page,),
                type="primary" if _page == st.session_state["current_page"] else "secondary",
                use_container_width=True,
            )
        # Zero margin, not the small gap this had before - the ask was
        # for the utility pages to line up flush with the core 4, same
        # as they already do with each other (sidebar's gap:0 already
        # handles that between buttons; this hr is the one other direct
        # child of that flex column, so its own margin was the actual
        # source of the visible gap above Settings).
        st.markdown(
            "<hr style='margin: 0; border: none; "
            "border-top: 1px solid rgba(0, 29, 86, 0.15);'>",
            unsafe_allow_html=True,
        )
        for _page in _UTILITY_PAGES:
            st.button(
                _page, key=f"nav_btn_{_page}", on_click=_select_page, args=(_page,),
                type="primary" if _page == st.session_state["current_page"] else "secondary",
                use_container_width=True,
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

# Word-lookup/add helpers and the clickable-word renderer live here, ahead
# of every page that uses them - Quiz Me (below) now renders clickable
# definitions/synonyms/antonyms too, not just Add Word, and Quiz Me's
# `if current_page == "Quiz Me":` block runs earlier in the script than
# Add Word's own section, so these need to be defined before Quiz Me,
# not between the two (Streamlit re-runs this whole script top to
# bottom every time, so a def appearing textually after its first call
# site would NameError).

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
    # st.toast() instead of an inline st.success/warning/error box:
    # under the old tabs-based nav, an inline box on Add Word's page
    # stayed frozen in the (hidden but still-rendered) DOM showing
    # stale text ("Added zephyr") no matter how long you'd since
    # switched tabs, since switching tabs didn't rerun the script. A
    # toast renders as a top-right overlay outside any page's own DOM
    # and auto-dismisses on its own after a few seconds, so it can't
    # get stuck that way regardless of how navigation happens to work.
    # like that.
    st.toast(text, icon=_MSG_ICONS.get(kind))


def _run_lookup(word):
    """Shared by the main Look Up button and every clickable word's
    popover (definitions, synonyms, antonyms alike - see
    _render_clickable_text/_do_lookup_word)."""
    # Always land on Add Word - whether this lookup was triggered from
    # its own search box, a synonym click while already there, or a
    # word clicked in Quiz Me's feedback (a different page entirely).
    # Set unconditionally, before the lookup even resolves, so a failed
    # lookup's warning/error toast is also seen on the page that's
    # about to display it, not wherever the click happened to be.
    st.session_state["current_page"] = "Add Word"
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
    already_had_it = db.get_word(_uid(), word) is not None
    db.add_word(
        _uid(), word, info["definition"], info["part_of_speech"], info["example"],
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
if st.session_state["current_page"] == "Quiz Me":
    if st.session_state.quiz_word is None:
        w = db.next_due_word(_uid())
        if w is not None:
            st.session_state.quiz_word = w
            st.session_state.quiz_result = None

    if st.session_state.quiz_word is None:
        # Nothing due per the spaced-repetition schedule right now.
        soonest, soonest_date = db.soonest_upcoming(_uid())
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
        word_row = db.get_word(_uid(), st.session_state.quiz_word)

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
                            db.save_attempt(_uid(), word_row["word"], answer, result.accuracy, result.feedback)
                            st.session_state.quiz_schedule = db.update_schedule(
                                _uid(), word_row["word"], result.accuracy
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
                db.save_attempt(_uid(), word_row["word"], "*silence*", result.accuracy, result.feedback)
                st.session_state.quiz_schedule = db.update_schedule(_uid(), word_row["word"], result.accuracy)
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
if st.session_state["current_page"] == "Add Word":
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
            # trend + etymology). st.tabs() nested inside a plain
            # container like this one is fine - the problem elsewhere in
            # this file was specifically squeezing st.tabs() inside an
            # st.columns() column, which drags every tab PANEL's width
            # down with it; a plain container doesn't have that issue.
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
        db.delete_word(_uid(), word)
        st.session_state.pop(f"sel_{word}", None)
    st.session_state["confirm_bulk_delete"] = False
    st.session_state["bulk_delete_msg"] = f"Deleted {len(selected)} word(s)."
    # Any of the deleted words could've been the current quiz word.
    st.session_state.quiz_word = None
    st.session_state.quiz_result = None
    st.session_state.quiz_schedule = None
    st.session_state["quiz_form_version"] += 1


def _do_single_delete(word):
    db.delete_word(_uid(), word)
    st.session_state.pop(f"sel_{word}", None)


if st.session_state["current_page"] == "My Words":
    all_words = db.get_all_words(_uid())
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
                # Keyed wrapper (substring-matched in CSS, same "word text
                # as part of the key" pattern the sel_ checkbox below
                # already uses) so this row's overall width can be
                # narrowed 20% per user request ("the tile...is too
                # large. Reduce by 20%") without touching the toolbar
                # row above it.
                with st.container(key=f"word_row_{w['word']}"):
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
                                for a in db.get_attempts(_uid(), w["word"]):
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
if st.session_state["current_page"] == "Progress":
    stats = db.get_progress_stats(_uid())
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
            # Threshold is user-editable now (Settings' Daily Word
            # Target), not a flat 10 - see db.get_quiz_streak's
            # docstring for exactly how today's still-in-progress count
            # is handled.
            daily_target = db.get_user_settings(_uid())["daily_word_target"]
            streak = db.get_quiz_streak(_uid(), threshold=daily_target)
            with st.container(key="progress_streak_card"):
                st.markdown(
                    "<span class='stat-top'>"
                    "<span class='stat-icon'>🔥</span>"
                    f"<span class='stat-value'>{streak}</span>"
                    "</span>"
                    f"<span class='stat-label'>Day streak ({daily_target}+ words/day)</span>",
                    unsafe_allow_html=True,
                )
            if stats["overall_avg"] is not None:
                with st.container(key="progress_accuracy_card"):
                    st.markdown(
                        "<span class='stat-top'>"
                        f"<span class='stat-value'>{stats['overall_avg']:.1f}%</span>"
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
        acc_trend = db.get_daily_accuracy_trend(_uid())
        words_trend = db.get_daily_words_quizzed_trend(_uid())
        has_alltime_trend = len(acc_trend) >= 2 and len(words_trend) >= 2
        if has_alltime_trend:
            st.subheader("Accuracy over time")

            # Defaults to a rolling last 3 weeks - the full history
            # eventually produces enough bars that a fixed per-bar width
            # (see chart_width below) would need real horizontal
            # scrolling to stay readable; 3 weeks is the common case
            # that still fits without it, with "All time" one click away.
            st.session_state.setdefault("progress_chart_range", "Last 3 weeks")
            st.radio(
                "Date range", ["Last 3 weeks", "All time"], key="progress_chart_range",
                horizontal=True, label_visibility="collapsed",
            )
            if st.session_state["progress_chart_range"] == "Last 3 weeks":
                cutoff = db.today_local() - timedelta(days=20)
                acc_trend = [(d, v) for d, v in acc_trend if d >= cutoff]
                words_trend = [(d, v) for d, v in words_trend if d >= cutoff]

        if len(acc_trend) >= 2 and len(words_trend) >= 2:
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
            # Per-bar step: DEFAULT_WINDOW_DAYS bars (the default "Last
            # 3 weeks" view) would exactly fill the chart's real
            # measured width (PROGRESS_CHART_WIDTH_PX, from
            # .st-key-progress_chart_scroll's getBoundingClientRect -
            # Streamlit's centered layout caps it there regardless of
            # viewport size) at zero gap - then narrowed another 30% on
            # top of that per its own ask, still touching edge to edge
            # (the darker fill's own stroke outline is what keeps
            # adjacent bars visually separable - see mark_bar below).
            # Net effect: the default view no longer fills the full
            # width edge to edge (some blank space on the right instead)
            # - an accepted trade-off for bars this much narrower being
            # possible at all. Beyond 3 weeks ("All time" with a longer
            # history), the chart keeps growing at the same per-bar step
            # instead of cramming more bars into a fixed width, and
            # .st-key-progress_chart_scroll's overflow-x handles the
            # rest.
            PROGRESS_CHART_WIDTH_PX = 704
            DEFAULT_WINDOW_DAYS = 21
            # 0.7 was the original per-bar fill fraction; the extra 0.85
            # on top is a further 15% narrower per user request ("make
            # the bar widths 15% less").
            BAR_STEP_PX = (PROGRESS_CHART_WIDTH_PX / DEFAULT_WINDOW_DAYS) * 0.7 * 0.85
            MIN_CHART_WIDTH_PX = 300
            chart_width = max(MIN_CHART_WIDTH_PX, len(date_order) * BAR_STEP_PX)
            # "8/14" not "Aug 14" - shorter, and strftime's portable
            # cross-platform codes don't include a no-leading-zero month/
            # day (%-m/%-d is Linux/Mac only, not Windows) - built by
            # hand instead so it doesn't depend on the OS's strftime.
            date_labels = {d: f"{d.month}/{d.day}" for d in date_order}
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
                # Light blue fill / dark blue stroke - swapped from the
                # original dark fill / light stroke per user request
                # ("bars...light blue, labels dark blue").
                .mark_bar(color="#5BABFB", stroke="#001D56", strokeWidth=1, size=BAR_STEP_PX,
                          cornerRadiusTopLeft=2, cornerRadiusTopRight=2)
                .encode(
                    x=date_x,
                    y=alt.Y("words_quizzed:Q", axis=None, scale=shared_scale),
                    tooltip=[alt.Tooltip("date_label:O", title="Date"),
                             alt.Tooltip("words_quizzed:Q", title="Words quizzed")],
                )
            )
            bar_labels = (
                alt.Chart(words_df)
                # Dark blue, not white - the bar fill above is light now,
                # so white text would no longer have enough contrast.
                .mark_text(fontWeight="bold", fontSize=9, color="#001D56", angle=270)
                .encode(x=date_x, y=alt.Y("label_y:Q", axis=None, scale=shared_scale),
                        text=alt.Text("words_quizzed:Q"))
            )
            date_labels_layer = (
                alt.Chart(words_df)
                # dx, not dy, is what pushes this DOWN the screen, away
                # from the bars sitting right above at y=0 - a mark_text
                # offset is applied in the text's own local frame BEFORE
                # its `angle` rotation, and at angle=270 that swaps the
                # two axes (confirmed live: dy=8 was actually landing as
                # a same-size sideways shift, not a downward one, which
                # is what caused the labels to visibly overlap the bars'
                # bottom edge - measured a -1.2px gap, i.e. true overlap,
                # at dx=-8). -15 leaves a clean ~6px gap below the bars.
                .mark_text(dx=-15, fontSize=10, color="#94A6CC", angle=270)
                .encode(x=date_x, y=alt.Y("zero:Q", axis=None, scale=shared_scale), text=alt.Text("date_label:O"))
            )
            # Dark blue throughout (line, points, labels) - was the
            # medium #0270FE brand blue, changed per user request
            # ("Line on line chart and labels of line chart dark blue").
            line = (
                alt.Chart(acc_df)
                .mark_line(color="#001D56", strokeWidth=2.5)
                .encode(
                    x=date_x,
                    y=alt.Y("plot_y:Q", axis=None, scale=shared_scale),
                    tooltip=[alt.Tooltip("date_label:O", title="Date"),
                             alt.Tooltip("avg_accuracy:Q", title="Accuracy %", format=".0f")],
                )
            )
            line_points = (
                alt.Chart(acc_df)
                .mark_point(color="#001D56", filled=True, size=40)
                .encode(x=date_x, y=alt.Y("plot_y:Q", axis=None, scale=shared_scale))
            )
            line_labels = (
                alt.Chart(acc_df)
                .mark_text(dy=-10, fontWeight="bold", fontSize=11, color="#001D56")
                .encode(x=date_x, y=alt.Y("plot_y:Q", axis=None, scale=shared_scale),
                        text=alt.Text("avg_accuracy:Q", format=".0f"))
            )
            combo = alt.layer(bar, bar_labels, date_labels_layer, line, line_points, line_labels).properties(
                height=260, width=chart_width,
            )
            with st.container(key="progress_chart_scroll"):
                st.altair_chart(combo, use_container_width=False)

            with st.container(key="progress_chart_legend"):
                st.markdown(
                    "<span class='cl-row'><span class='cl-swatch-bar'></span>Words quizzed</span>"
                    "<span class='cl-row'><span class='cl-swatch-line'></span>Accuracy %</span>",
                    unsafe_allow_html=True,
                )
        elif has_alltime_trend:
            # Enough all-time data to have shown the toggle at all, just
            # none of it falls within the currently-selected "Last 3
            # weeks" window (e.g. a long break) - "quiz more" would be
            # misleading advice here.
            st.caption("No activity in the last 3 weeks - try \"All time\".")
        elif acc_trend or words_trend:
            st.caption("Quiz on a few more days to see a trend here.")

# ------------------------------------------------------------
# Settings
# ------------------------------------------------------------
# Per-user preferences (db.user_settings) - alias is purely cosmetic
# for now (not shown anywhere else yet); the two Yes/No toggles are
# storage-only today, ahead of the features they'll actually gate
# (a shared community word list, and letting other users see your
# progress) - see each one's own caption below.
if st.session_state["current_page"] == "Settings":
    st.subheader("Settings")
    _settings = db.get_user_settings(_uid())
    st.session_state.setdefault("settings_alias", _settings["alias"])
    st.session_state.setdefault(
        "settings_auto_add", "Yes" if _settings["auto_add_community_words"] else "No"
    )
    st.session_state.setdefault(
        "settings_share_progress", "Yes" if _settings["share_progress"] else "No"
    )
    st.session_state.setdefault("settings_daily_target", _settings["daily_word_target"])

    st.text_input(
        "Alias", key="settings_alias", max_chars=10,
        help="A short display name, 10 characters max.",
    )
    st.selectbox("Auto-Add Community Words", ["No", "Yes"], key="settings_auto_add")
    st.caption("Automatically add new words other users add to your own list.")
    st.selectbox("Share My Progress", ["No", "Yes"], key="settings_share_progress")
    st.caption("Let other users see your accuracy and streak.")
    st.number_input(
        "Daily Word Target", key="settings_daily_target", min_value=1, max_value=100, step=1,
    )
    st.caption("How many words a day counts toward your Progress tab streak.")

    if st.button("Save Settings", type="primary"):
        db.save_user_settings(
            _uid(),
            st.session_state["settings_alias"],
            st.session_state["settings_auto_add"] == "Yes",
            st.session_state["settings_share_progress"] == "Yes",
            st.session_state["settings_daily_target"],
        )
        st.toast("Settings saved.", icon="✅")

# ------------------------------------------------------------
# About
# ------------------------------------------------------------
if st.session_state["current_page"] == "About":
    st.subheader("About vocapp")
    with st.container(key="about_scroll"):
        st.markdown(
            """
**Quiz Me** — Your daily practice queue. The app serves a word that's
due for review under a spaced-repetition schedule: type your own
definition from memory, get it graded, and see exactly what you got
right and missed. Answer well and a word's next review stretches
further out; miss it and it comes back sooner.

**Add Word** — Look up any word to see its definition, part of speech,
pronunciation, synonyms and antonyms, real usage examples, etymology,
and how its usage has trended over time - then add it to your list
with one click.

**My Words** — Every word you've added, with your accuracy history and
next review date, plus search, sort, and filter tools. Delete words
you no longer want to study.

**Progress** — Your overall stats at a glance: how many words are
Mastered, Learning, or Needs Work, your quiz streak, and a chart of
your daily accuracy and quiz volume over time.

**Settings** — Personalize your account: a short display alias, your
daily word target for the Progress streak, and preferences for
community word sharing and progress visibility.

**App Ideas** — Have a suggestion? Type it here. Every idea is saved
and reviewed to help decide what to build next.
            """
        )

# ------------------------------------------------------------
# App Ideas
# ------------------------------------------------------------
# Free-text suggestions, saved per user (db.app_ideas) - reviewed
# centrally (see the owner-only section below) to help decide what to
# build next, rather than needing a separate feedback channel.
def _format_idea_id(idea_id: int) -> str:
    """The db's own auto-increment id, zero-padded to 4 digits - "ID-0001"
    and up, per user request. Not a separate counter: whatever id
    app_idea_id_seq actually assigned IS the idea's number, just
    formatted for display."""
    return f"ID-{idea_id:04d}"


if st.session_state["current_page"] == "App Ideas":
    st.subheader("App Ideas")
    st.caption("Have a suggestion for the app? Type it below - every idea gets reviewed.")

    IDEA_TYPES = ["Improvement", "Bug Fix"]
    IDEA_STATUSES = ["Submitted", "Rejected", "Completed"]

    st.session_state.setdefault("app_idea_version", 0)
    # Versioned key, not a plain one cleared via session_state after
    # submit - popping/reassigning an already-instantiated widget's key
    # doesn't reliably reset it in Streamlit (same gotcha this file's
    # form-clearing logic elsewhere already works around); a fresh key
    # after each submit is what actually guarantees an empty box. Both
    # the type dropdown and the text area share one version counter so
    # they reset together.
    _idea_type_key = f"app_idea_type_{st.session_state['app_idea_version']}"
    _idea_key = f"app_idea_draft_{st.session_state['app_idea_version']}"
    st.selectbox("Idea Type", IDEA_TYPES, key=_idea_type_key)
    st.text_area(
        "Your idea", key=_idea_key, label_visibility="collapsed",
        placeholder='e.g. "Add a dark mode" or "Let me filter by part of speech"',
    )
    if st.button("Submit Idea", type="primary"):
        _idea_text = st.session_state[_idea_key].strip()
        if _idea_text:
            _new_id = db.add_app_idea(_uid(), _idea_text, st.session_state[_idea_type_key])
            st.session_state["app_idea_version"] += 1
            st.toast(f"Thanks! Your idea ({_format_idea_id(_new_id)}) has been submitted.", icon="✅")
            st.rerun()
        else:
            st.warning("Type something first.")

    # This user's own ideas, grouped into one expander per status
    # (Submitted/Completed/Rejected - same expander-per-group shape as
    # "All submitted ideas (owner view)" below) instead of the one flat
    # "Your submitted ideas" list this replaced, so it's clear at a
    # glance what's still pending vs. already acted on.
    _my_ideas = db.get_app_ideas(_uid())
    for _status in ["Submitted", "Completed", "Rejected"]:
        _status_ideas = [i for i in _my_ideas if i["status"] == _status]
        with st.expander(f"{_status} ({len(_status_ideas)})"):
            if not _status_ideas:
                st.caption("No ideas here yet.")
            for _idea in _status_ideas:
                st.markdown(
                    f"- **{_format_idea_id(_idea['id'])}** · {_idea['submitted_at']:%b %d, %Y} · "
                    f"**{_idea['idea_type']}** — {_idea['idea_text']}"
                )

    # Owner-only: every user's ideas in one place, so reviewing them
    # doesn't require going around the app to query the database
    # directly. Status is editable only here - everyone else's own
    # list above (including the owner's own ideas there) is read-only,
    # since status is meant to reflect what's actually been reviewed/
    # built, not something a submitter sets themselves.
    if _uid() == db._LEGACY_OWNER_EMAIL:
        with st.expander("All submitted ideas (owner view)"):
            # Grouped Submitted -> Completed -> Rejected (same order as
            # the three sections above), not just newest-first - a
            # stable sort, so within each group it's still newest-first
            # exactly as get_all_app_ideas() already returned it.
            _STATUS_SORT_ORDER = ["Submitted", "Completed", "Rejected"]
            _all_ideas = sorted(db.get_all_app_ideas(), key=lambda i: _STATUS_SORT_ORDER.index(i["status"]))
            if not _all_ideas:
                st.caption("No ideas submitted yet.")
            for _idea in _all_ideas:
                _status_col, _text_col = st.columns([1, 3], gap="small")
                with _status_col:
                    st.selectbox(
                        "Status", IDEA_STATUSES, index=IDEA_STATUSES.index(_idea["status"]),
                        key=f"idea_status_{_idea['id']}", label_visibility="collapsed",
                        on_change=lambda _id=_idea["id"]: db.update_app_idea_status(
                            _id, st.session_state[f"idea_status_{_id}"]
                        ),
                    )
                with _text_col:
                    st.markdown(
                        f"**{_format_idea_id(_idea['id'])}** · **{_idea['user_id']}** "
                        f"({_idea['submitted_at']:%b %d, %Y}) · **{_idea['idea_type']}** — {_idea['idea_text']}"
                    )
