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
    got_right: list[str] = Field(description="Short phrases (2-6 words each) naming what the user's definition got right. Empty list if nothing.")
    got_missed: list[str] = Field(description="Short phrases naming important aspects of the meaning the user's definition missed or got wrong. Empty list if nothing missed.")
    note: str = Field(description="One short sentence of overall feedback, direct and specific, not generic praise.")


GRADING_SYSTEM_PROMPT = """You are grading a vocabulary quiz. The user was shown a word and typed their own \
definition from memory. Compare their definition to the reference definition and judge whether they \
demonstrate real understanding of the word's meaning - not whether their wording matches.

Score generously for paraphrase, synonyms, and partial credit for capturing the core sense even if a \
secondary nuance is missing. Score low only when the user's definition would mislead someone about what \
the word actually means, or is off-topic/blank.

Grading axis reference:
- 90-100: captures the core meaning fully, including most nuance
- 70-89: captures the core meaning, missing a secondary nuance
- 40-69: partially right - gets in the right neighborhood but misses something important
- 0-39: wrong, off-topic, or too vague to demonstrate understanding

Be specific in got_right/got_missed - name the actual concepts, not "accuracy" or "completeness" as abstractions."""


def grade_definition(word: str, reference_definition: str, user_answer: str) -> GradeResult:
    client = _get_client()
    user_msg = (
        f"Word: {word}\n"
        f"Reference definition: {reference_definition}\n"
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
