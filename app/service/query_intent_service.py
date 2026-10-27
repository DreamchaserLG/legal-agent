"""Query intent detection — stop vague input before it becomes an empty result.

A user typing "法律问题" or "怎么办" is not asking for a ranked authority list;
they need guidance. Running retrieval anyway produces "no match", which reads
as "the system is broken". This module classifies the input first so the page
can show guidance instead of an empty state.

Intents
-------
``empty``      no input at all
``greeting``   hello / 在吗 / hi
``too_short``  one or two characters, no legal signal
``vague``      generic words only ("咨询一下", "法律问题", "怎么办")
``clear``      contains a legal term, citation, or concrete facts
"""

from __future__ import annotations

import re

from app.service.query_translation_service import LEGAL_TERMS

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

GREETINGS = ("你好", "您好", "在吗", "hi", "hello", "hey", "早上好", "下午好", "晚上好", "谢谢", "多谢", "thanks")

#: Phrases that signal "I have a problem" without saying anything actionable.
VAGUE_PHRASES = (
    "法律问题", "法律咨询", "咨询一下", "咨询", "问一下", "我想问", "想问",
    "怎么办", "怎么处理", "如何处理", "帮帮我", "求助", "有没有人",
    "打个官司", "打官司", "我的问题", "一个问题", "想了解", "了解一下",
    "legal question", "help me", "question", "how do i", "what should i do",
)

#: English legal vocabulary that marks a query as substantive.
EN_SIGNALS = (
    "court", "act", "section", "regulation", "statute", "liability", "liable",
    "contract", "lease", "tenant", "landlord", "rent", "deposit", "dismissal",
    "negligence", "damages", "appeal", "judgment", "custody", "divorce",
    "immigration", "refugee", "copyright", "trademark", "patent", "privacy",
    "fraud", "injunction", "breach", "remedy", "jurisdiction", "evidence",
)

_CITATION_RE = re.compile(r"\b\d{4}\s+[A-Z]{2,6}\s+\d+|\bS\.?C\.?\s?\d{4}\b|\bs\.\s?\d+", re.I)


def _cjk_count(text: str) -> int:
    return len(_CJK_RE.findall(text))


def analyze_intent(text: str) -> dict:
    """Classify the query and return a hint the UI can render."""
    clean = (text or "").strip()
    if not clean:
        return {"intent": "empty", "reason": "no input"}

    lowered = clean.lower()
    cjk = _cjk_count(clean)

    if lowered in GREETINGS or (len(clean) <= 6 and any(g in lowered for g in GREETINGS)):
        return {"intent": "greeting", "reason": "greeting only"}

    # a bare citation is always a valid query
    if _CITATION_RE.search(clean):
        return {"intent": "clear", "reason": "citation"}

    if cjk:
        if cjk <= 2 and len(clean) <= 4:
            return {"intent": "too_short", "reason": "too few characters"}
    else:
        words = [w for w in re.split(r"\s+", clean) if w]
        if len(words) <= 1 and len(clean) < 6:
            return {"intent": "too_short", "reason": "too few words"}

    # Strip generic phrasing first: "法律问题" contains "法律", which is itself a
    # dictionary term, so a plain term check would wrongly mark it as clear.
    residue = clean
    vague_hits: list[str] = []
    for phrase in VAGUE_PHRASES:
        if phrase in residue.lower():
            vague_hits.append(phrase)
            residue = re.sub(re.escape(phrase), "", residue, flags=re.I)
    residue = residue.strip(" ，,。.？?！!、;；:：")
    if vague_hits and len(residue) < 2:
        return {"intent": "vague", "reason": vague_hits[0], "matched": vague_hits[:3]}

    if any(term in clean for term in LEGAL_TERMS):
        return {"intent": "clear", "reason": "legal term"}

    tokens = re.findall(r"[a-zA-Z]{3,}", lowered)
    if any(signal in tokens or signal in lowered for signal in EN_SIGNALS):
        return {"intent": "clear", "reason": "legal vocabulary"}

    if vague_hits:
        return {"intent": "vague", "reason": vague_hits[0], "matched": vague_hits[:3]}

    # Chinese text with no legal vocabulary is almost always a preamble
    if cjk and cjk >= 3:
        return {"intent": "vague", "reason": "no legal term"}

    return {"intent": "clear", "reason": "substantive"}


def needs_guidance(result: dict) -> bool:
    """True when the UI should show guidance instead of search results."""
    return result.get("intent") in {"greeting", "too_short", "vague"}
