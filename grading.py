"""
AI grading: compares a user's typed definition against the reference
definition using an LLM (not a string match - see project plan, "The AI
grading system"). Uses Claude Haiku 4.5 - cheap and plenty capable for a
short structured judgment call like this (~$2/month at 50 quizzes/day).
"""

import os
from pydantic import BaseModel, Field
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

_client = None


def _get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set - create a .env file next to this "
                "script with ANTHROPIC_API_KEY=sk-ant-..."
            )
        _client = Anthropic(api_key=api_key)
    return _client


class GradeResult(BaseModel):
    accuracy: int = Field(description="0-100 score for how well the user's definition captures the word's meaning")
    feedback: str = Field(description=(
        "1-2 sentences of direct, specific feedback, written as flowing prose spoken "
        "to the user - not a list, not generic praise. Wrap the exact phrase(s) naming "
        "what they got right in <right>...</right> tags, and the exact phrase(s) naming "
        "what they missed or got wrong in <wrong>...</wrong> tags, e.g.: 'You captured "
        "the <right>sense of things being separate and distant</right> but missed that "
        "disparate specifically means <wrong>they differ in quality, character, or "
        "nature - not just physical location</wrong>.' Each tagged phrase must name real "
        "content (the actual concept), never a vague stand-in like 'accuracy' or 'the "
        "definition'. Omit <right>...</right> entirely if nothing was right; omit "
        "<wrong>...</wrong> entirely if nothing was missed."
    ))


GRADING_SYSTEM_PROMPT = """You are grading a vocabulary quiz. The user was shown a word and typed their own \
definition from memory. Compare their definition to the reference definition and judge whether they \
demonstrate real understanding of the word's meaning - not whether their wording matches.

The reference definition sometimes lists more than one numbered sense of the word (a word can have 2-3 \
distinct meanings). When it does, grade in two explicit steps:
  1. Identify which single listed sense the user's answer is actually describing (by content, not by \
which one happens to be numbered first - the numbering is MW's most-common-usage ordering, not a ranking \
of which sense the user was supposed to answer).
  2. Grade ONLY against that one sense, as if the other listed senses did not exist. A full, accurate \
description of that one sense is a complete answer worth 90-100, in full - not "missing" anything, even \
though other senses exist. Never tag a *different* sense's content as <wrong>...</wrong>; only tag \
something as missed if it belongs to the same sense the user was already describing.

Score generously for paraphrase, synonyms, and partial credit for capturing the core sense even if a \
secondary nuance is missing. Score low only when the user's definition would mislead someone about what \
the word actually means, or is off-topic/blank.

Grading axis reference:
- 90-100: captures the core meaning fully, including most nuance
- 70-89: captures the core meaning, missing a secondary nuance
- 40-69: partially right - gets in the right neighborhood but misses something important
- 0-39: wrong, off-topic, or too vague to demonstrate understanding

Be specific in the tagged phrases inside feedback - name the actual concepts the user got right or missed, \
never "accuracy" or "completeness" as abstractions."""


def _format_reference(definition: str) -> str:
    """reference_definition may hold up to 3 senses joined by "\n" (see
    dictionary.py's lookup_word) - numbered here so the prompt reads
    unambiguously as several distinct senses rather than one run-on
    definition. A plain single-sense definition (the common case) passes
    through unchanged."""
    senses = definition.split("\n")
    if len(senses) == 1:
        return senses[0]
    return "\n".join(f"{i}. {s}" for i, s in enumerate(senses, 1))


def grade_definition(word: str, reference_definition: str, user_answer: str) -> GradeResult:
    client = _get_client()
    user_msg = (
        f"Word: {word}\n"
        f"Reference definition: {_format_reference(reference_definition)}\n"
        f"User's typed definition: {user_answer}"
    )
    response = client.messages.parse(
        model="claude-haiku-4-5",
        max_tokens=500,
        system=GRADING_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        output_format=GradeResult,
    )
    return response.parsed_output
