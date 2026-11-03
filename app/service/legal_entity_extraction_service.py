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
_CASE_NAME = re.compile(
    r"\b([A-Z][A-Za-z0-9&'’.,()\- ]{1,80}?)\s+v\.?\s+([A-Z][A-Za-z0-9&'’.,()\- ]{1,80}?)(?=,|\s+(?:19|20)\d{2}\b|\n|$)"
)
_ORGANIZATION = re.compile(
    r"\b([A-Z][A-Za-z0-9&'’.-]*(?:\s+[A-Z][A-Za-z0-9&'’.-]*){0,8}\s+"
    r"(?:Inc\.?|Ltd\.?|Limited|Corp\.?|Corporation|Company|LLP|LP|ULC))\b"
)
_DEFINED_ALIAS = re.compile(
    r"(?P<name>[A-Z][A-Za-z0-9&.,'’()\- ]{2,100}?)\s*\(\s*(?:the\s+)?[\"'“‘](?P<alias>[A-Z][A-Za-z0-9&\- ]{1,40})[\"'”’]\s*\)"
)
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
    r"\b(?:the company|this company|the corporation|the employer|the employee|the landlord|the tenant|the applicant|the respondent|the appellant|the accused|the Crown)\b|"
    r"该公司|该雇主|该房东|该租客|申请人|答辩人|上述公司|上述通知",
    re.I,
)
_SENTENCING_FACTORS = (
    (re.compile(r"\b(?:prior record|criminal record|previous conviction|antecedents?)\b|前科|犯罪记录", re.I), "prior criminal record"),
    (re.compile(r"\b(?:intoxication|intoxicated|drunk|alcohol)\b|醉酒", re.I), "intoxication"),
    (re.compile(r"\b(?:remorse|guilty plea|rehabilitation)\b|悔罪|认罪|改造", re.I), "remorse and rehabilitation"),
    (re.compile(r"\b(?:aggravat(?:ing|ed)|mitigat(?:ing|ed))\b|从重|从轻", re.I), "aggravating or mitigating factor"),
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
    for match in _CASE_NAME.finditer(clean):
        case_name = f"{repair_text(match.group(1)).strip()} v {repair_text(match.group(2)).strip()}"
        add(_entity("Case", case_name, match.group(0), 0.9, jurisdiction=jurisdiction))
    for match in _ORGANIZATION.finditer(clean):
        add(_entity("Organization", match.group(1), match.group(0), 0.93, jurisdiction=jurisdiction))
    for match in _DEFINED_ALIAS.finditer(clean):
        name = repair_text(match.group("name")).strip(" ,")
        alias = repair_text(match.group("alias")).strip()
        add(_entity("Organization", name, match.group(0), 0.9, jurisdiction=jurisdiction, defined_alias=alias))
        relations.append({
            "source": alias, "target": name, "relation_type": "ALIASES",
            "evidence_span": match.group(0), "confidence": 0.96,
        })
    for pattern, issue in _ISSUES:
        match = pattern.search(clean)
        if match:
            add(_entity("LegalIssue", issue, match.group(0), 0.82, jurisdiction=jurisdiction))
    for pattern, factor in _SENTENCING_FACTORS:
        match = pattern.search(clean)
        if match:
            add(_entity("SentencingFactor", factor, match.group(0), 0.86, jurisdiction=jurisdiction))
    mentions = [
        {"surface": match.group(0), "start": match.start(), "end": match.end(), "resolved_entity_id": None, "confidence": 0.0}
        for match in _COREFERENCE.finditer(clean)
    ]
    return {
        "entities": entities,
        "relations": relations,
        "coreference_mentions": mentions,
        "requires_coreference_resolution": bool(mentions),
        "extraction_method": "deterministic_canadian_legal_v2",
        "source_text_preserved": True,
    }
