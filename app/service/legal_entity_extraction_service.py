from __future__ import annotations

"""Conservative Canadian legal entity/relation extraction.

The extractor preserves source spans and never treats model/retrieved text as
instructions.  It is suitable for local subset evaluation before any corpus-
wide LLM extraction is approved.
"""

import re
from typing import Any

from app.service.common_service import repair_text


_STATUTE = re.compile(
    r"\b([A-Z][A-Za-z0-9'().,&/\-]*(?:\s+(?:[A-Z][A-Za-z0-9'().,&/\-]*|of|and|the|for|to|in|on)){0,10}\s+(?:Act|Code|Rules|Regulation(?:s)?|Charter)(?:,\s*\d{4})?)\b"
)
_SECTION = re.compile(r"\b(?:s(?:ection)?\.?|ss\.?)\s*(\d+(?:\.\d+)?(?:\([0-9a-z]+\))*)", re.I)
_CASE_CITATION = re.compile(r"\b(19\d{2}|20\d{2})\s+(SCC|ONCA|ONSC|ONCJ|ONLTB|FC|FCA|BCCA|BCSC|ABCA|ABKB|NSSC)\s+(\d+)\b", re.I)
_COURTS = {
    "ONCA": "Court of Appeal for Ontario", "ONSC": "Ontario Superior Court of Justice",
    "ONCJ": "Ontario Court of Justice", "ONLTB": "Ontario Landlord and Tenant Board",
    "SCC": "Supreme Court of Canada", "FC": "Federal Court", "FCA": "Federal Court of Appeal",
}
_ISSUES = (
    (re.compile(r"租赁|租客|房东|tenant|landlord|tenancy|lease", re.I), "residential tenancy"),
    (re.compile(r"维修|漏水|霉菌|repair|maintenance|mould|habitability", re.I), "repair and habitability"),
    (re.compile(r"N12|驱逐|自用|报复|eviction|own use|bad faith|retaliat", re.I), "eviction, own use and bad faith"),
    (re.compile(r"解雇|雇主|雇员|dismissal|employment|employee|employer", re.I), "employment termination"),
    (re.compile(r"过失|受伤|negligence|injury|duty of care", re.I), "negligence and duty of care"),
)
_COREFERENCE = re.compile(
    r"\b(?:the company|the corporation|the employer|the landlord|the tenant|the applicant|the respondent)\b|"
    r"该公司|该雇主|该房东|该租客|申请人|答辩人|上述公司|上述通知",
    re.I,
)


def _entity(entity_type: str, name: str, span: str, confidence: float, **metadata: Any) -> dict:
    return {
        "entity_type": entity_type, "canonical_name": repair_text(name),
        "source_span": repair_text(span), "confidence": round(float(confidence), 4),
        "metadata": metadata,
    }


def extract_legal_entities(text: str, *, jurisdiction: str = "Canada") -> dict:
    clean = repair_text(text)
    entities: list[dict] = []
    relations: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(item: dict) -> None:
        key = (item["entity_type"], item["canonical_name"].casefold())
        if key not in seen:
            seen.add(key)
            entities.append(item)

    for match in _STATUTE.finditer(clean):
        add(_entity("Statute", match.group(1), match.group(0), 0.96, jurisdiction=jurisdiction))
    for match in _SECTION.finditer(clean):
        add(_entity("Section", f"section {match.group(1)}", match.group(0), 0.94, jurisdiction=jurisdiction))
    for match in _CASE_CITATION.finditer(clean):
        citation = match.group(0)
        court_code = match.group(2).upper()
        add(_entity("Case", citation, citation, 0.99, citation=citation, jurisdiction=jurisdiction))
        add(_entity("Court", _COURTS.get(court_code, court_code), court_code, 0.98, code=court_code))
        relations.append({
            "source": citation, "target": _COURTS.get(court_code, court_code),
            "relation_type": "DECIDED_BY", "evidence_span": citation, "confidence": 0.98,
        })
    for pattern, issue in _ISSUES:
        match = pattern.search(clean)
        if match:
            add(_entity("LegalIssue", issue, match.group(0), 0.82, jurisdiction=jurisdiction))
    mentions = [
        {"surface": match.group(0), "start": match.start(), "end": match.end(), "resolved_entity_id": None, "confidence": 0.0}
        for match in _COREFERENCE.finditer(clean)
    ]
    return {
        "entities": entities,
        "relations": relations,
        "coreference_mentions": mentions,
        "requires_coreference_resolution": bool(mentions),
        "extraction_method": "deterministic_canadian_legal_v1",
        "source_text_preserved": True,
    }

