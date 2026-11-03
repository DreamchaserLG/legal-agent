from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from app.service.common_service import repair_text
from app.service.legal_entity_extraction_service import extract_legal_entities


INDEX_VERSION = "legal-context-v2"

_PARAGRAPH_NUMBER = re.compile(r"^\s*(?:\[(\d{1,5})\]|(?:para(?:graph)?\.?\s+)(\d{1,5})\b)", re.I)
_SECTION_NUMBER = re.compile(
    r"^\s*(?:(?:section|s\.)\s+\d+[\w().-]*|\d+[\w.-]*\s*\(\d+\)|PART\s+[IVXLC\d]+|SCHEDULE\b)",
    re.I,
)
_CASE_HEADING = re.compile(
    r"^(?:facts?|background|issues?|analysis|reasons?(?:\s+for\s+judgment)?|decision|disposition|order|conclusion|remedy|sentence|sentencing)\s*:?$",
    re.I,
)
_DEFINED_ALIAS = re.compile(
    r"(?P<name>[A-Z][A-Za-z0-9&.,'’()\- ]{2,100}?)\s*\(\s*(?:the\s+)?[\"'“‘](?P<alias>[A-Z][A-Za-z0-9&\- ]{1,40})[\"'”’]\s*\)"
)
_ORG = re.compile(
    r"\b([A-Z][A-Za-z0-9&'’.-]*(?:\s+[A-Z][A-Za-z0-9&'’.-]*){0,8}\s+"
    r"(?:Inc\.?|Ltd\.?|Limited|Corp\.?|Corporation|Company|LLP|LP|ULC))\b"
)
_AMBIGUOUS_REFERENCES = (
    "the company", "this company", "the corporation", "the employer", "the employee",
    "the landlord", "the tenant", "the appellant", "the respondent", "the applicant",
    "the accused", "the Crown", "该公司", "该企业", "申请人", "被申请人",
)


@dataclass
class LegalChunk:
    text: str
    chunk_index: int
    section_id: str
    section_title: str
    paragraph_start: int | None
    paragraph_end: int | None
    token_count: int
    context_prefix: str = ""
    contextual_text: str = ""
    entities: list[dict[str, Any]] = field(default_factory=list)
    disambiguation: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "chunk_index": self.chunk_index,
            "section_id": self.section_id,
            "section_title": self.section_title,
            "paragraph_start": self.paragraph_start,
            "paragraph_end": self.paragraph_end,
            "token_count": self.token_count,
            "context_prefix": self.context_prefix,
            "contextual_text": self.contextual_text,
            "entities": self.entities,
            "disambiguation": self.disambiguation,
            "index_version": INDEX_VERSION,
        }


@dataclass
class _Segment:
    text: str
    section_id: str
    section_title: str
    paragraph_number: int | None


def _default_token_count(value: str) -> int:
    # Deterministic fallback for unit tests and SQLite-only installations.  The
    # production caller supplies the active embedding tokenizer.
    return max(1, len(re.findall(r"[A-Za-z0-9]+|[^\sA-Za-z0-9]", value)))


def _is_heading(value: str) -> bool:
    clean = repair_text(value).strip()
    if not clean or len(clean) > 180:
        return False
    if _SECTION_NUMBER.match(clean) or _CASE_HEADING.match(clean):
        return True
    letters = [char for char in clean if char.isalpha()]
    return bool(len(letters) >= 4 and clean == clean.upper() and not clean.endswith((".", ";")))


def _paragraph_number(value: str) -> int | None:
    match = _PARAGRAPH_NUMBER.match(value)
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def _segments(text_value: str) -> list[_Segment]:
    clean = repair_text(text_value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not clean:
        return []
    section_title = "Document opening"
    section_id = "opening"
    section_counter = 0
    output: list[_Segment] = []
    current: list[str] = []

    def flush() -> None:
        if not current:
            return
        body = "\n".join(current).strip()
        current.clear()
        if body:
            output.append(_Segment(body, section_id, section_title, _paragraph_number(body)))

    for raw_line in clean.split("\n"):
        line = raw_line.strip()
        if not line:
            flush()
            continue
        if _is_heading(line):
            flush()
            section_counter += 1
            section_title = line
            section_id = f"section-{section_counter}"
            # Headings are both structural metadata and source text. Keeping
            # them in the first segment of the section preserves quotable
            # content while still enforcing a hard section boundary.
            current.append(line)
            continue
        if _PARAGRAPH_NUMBER.match(line) and current:
            flush()
        current.append(line)
    flush()
    return output


def _split_oversized(
    segment: _Segment,
    *,
    max_tokens: int,
    token_count: Callable[[str], int],
    token_encode: Callable[[str], list[int]] | None,
    token_decode: Callable[[list[int]], str] | None,
) -> list[_Segment]:
    if token_count(segment.text) <= max_tokens:
        return [segment]
    sentences = [part.strip() for part in re.split(r"(?<=[.!?;:])\s+|\n+", segment.text) if part.strip()]
    pieces: list[str] = []
    current: list[str] = []
    for sentence in sentences:
        candidate = " ".join(current + [sentence]).strip()
        if current and token_count(candidate) > max_tokens:
            pieces.append(" ".join(current))
            current = []
        if token_count(sentence) <= max_tokens:
            current.append(sentence)
            continue
        if token_encode and token_decode:
            ids = token_encode(sentence)
            pieces.extend(token_decode(ids[start : start + max_tokens]).strip() for start in range(0, len(ids), max_tokens))
        else:
            words = re.findall(r"\S+", sentence)
            pieces.extend(" ".join(words[start : start + max_tokens]) for start in range(0, len(words), max_tokens))
    if current:
        pieces.append(" ".join(current))
    return [
        _Segment(piece, segment.section_id, segment.section_title, segment.paragraph_number)
        for piece in pieces
        if repair_text(piece)
    ]


def _document_parties(title: str) -> list[str]:
    match = re.split(r"\s+(?:v\.?|vs\.?)\s+", repair_text(title), maxsplit=1, flags=re.I)
    return [part.strip(" ,") for part in match if part.strip(" ,")][:2] if len(match) == 2 else []


def _alias_map(text_before_and_chunk: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for match in _DEFINED_ALIAS.finditer(text_before_and_chunk):
        name = repair_text(match.group("name")).strip(" ,")
        alias = repair_text(match.group("alias")).strip()
        if name and alias:
            aliases[alias.casefold()] = name
    return aliases


def _disambiguation(chunk_text: str, prior_text: str, title: str) -> dict[str, Any]:
    combined = f"{prior_text[-6000:]}\n{chunk_text}"
    aliases = _alias_map(combined)
    organizations = []
    for value in _ORG.findall(combined):
        clean = repair_text(value)
        if clean and clean.casefold() not in {item.casefold() for item in organizations}:
            organizations.append(clean)
    references = [value for value in _AMBIGUOUS_REFERENCES if value.casefold() in chunk_text.casefold()]
    resolved: list[dict[str, str]] = []
    for reference in references:
        key = reference.casefold().removeprefix("the ").removeprefix("this ")
        target = aliases.get(key)
        if not target and key in {"company", "corporation"} and len(organizations) == 1:
            target = organizations[0]
        if target:
            resolved.append({"surface": reference, "resolved_to": target, "basis": "defined_alias_or_unique_organization"})
    return {
        "document_parties": _document_parties(title),
        "defined_aliases": [{"alias": alias, "entity": entity} for alias, entity in sorted(aliases.items())],
        "ambiguous_references": references,
        "resolved_references": resolved,
        "unresolved_references": [item for item in references if item not in {row["surface"] for row in resolved}],
        "source_text_preserved": True,
    }


def _prefix(doc: dict[str, Any], chunk: LegalChunk) -> str:
    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    fields = [
        ("Document", doc.get("title")),
        ("Citation", metadata.get("citation") or metadata.get("article_no")),
        ("Jurisdiction", metadata.get("jurisdiction") or metadata.get("province") or "Canada"),
        ("Court", metadata.get("court_level") or metadata.get("court_name")),
        ("Document type", metadata.get("document_type") or metadata.get("case_type") or metadata.get("legal_type")),
        ("Section", chunk.section_title),
        ("Paragraphs", (
            f"{chunk.paragraph_start}-{chunk.paragraph_end}"
            if chunk.paragraph_start is not None and chunk.paragraph_end is not None
            else "unnumbered"
        )),
    ]
    parties = chunk.disambiguation.get("document_parties") or []
    if parties:
        fields.append(("Document parties", "; ".join(parties)))
    resolved = chunk.disambiguation.get("resolved_references") or []
    if resolved:
        fields.append(("Defined references", "; ".join(f"{row['surface']} = {row['resolved_to']}" for row in resolved)))
    entity_names = [repair_text(item.get("canonical_name")) for item in chunk.entities if repair_text(item.get("canonical_name"))]
    if entity_names:
        fields.append(("Legal entities", "; ".join(entity_names[:12])))
    return " | ".join(f"{label}: {repair_text(value)}" for label, value in fields if repair_text(value))[:2400]


def split_legal_document(
    text_value: str,
    *,
    doc: dict[str, Any] | None = None,
    target_tokens: int = 384,
    max_tokens: int = 512,
    min_tokens: int = 96,
    token_count: Callable[[str], int] | None = None,
    token_encode: Callable[[str], list[int]] | None = None,
    token_decode: Callable[[list[int]], str] | None = None,
) -> list[dict[str, Any]]:
    """Split Canadian legal material on legal structure, without sliding overlap."""
    document = dict(doc or {})
    counter = token_count or _default_token_count
    target = max(64, int(target_tokens))
    maximum = max(target, int(max_tokens))
    # Retained in the public signature for tuning manifests. Short legal
    # sections are intentionally not merged across hard structural boundaries.
    _ = max(1, min(int(min_tokens), target))
    raw_segments: list[_Segment] = []
    for segment in _segments(text_value):
        raw_segments.extend(
            _split_oversized(
                segment, max_tokens=maximum, token_count=counter,
                token_encode=token_encode, token_decode=token_decode,
            )
        )
    if not raw_segments:
        return []

    groups: list[list[_Segment]] = []
    current: list[_Segment] = []
    for segment in raw_segments:
        candidate = "\n\n".join(item.text for item in current + [segment])
        section_changed = bool(current and segment.section_id != current[-1].section_id)
        if current and (
            counter(candidate) > maximum
            or section_changed
            or counter("\n\n".join(item.text for item in current)) >= target
        ):
            groups.append(current)
            current = []
        current.append(segment)
    if current:
        groups.append(current)

    chunks: list[LegalChunk] = []
    prior_text = ""
    for index, group in enumerate(groups):
        chunk_text = "\n\n".join(item.text for item in group).strip()
        paragraph_numbers = [item.paragraph_number for item in group if item.paragraph_number is not None]
        extracted = extract_legal_entities(chunk_text, jurisdiction="Canada")
        chunk = LegalChunk(
            text=chunk_text,
            chunk_index=index,
            section_id=group[0].section_id,
            section_title=group[0].section_title,
            paragraph_start=min(paragraph_numbers) if paragraph_numbers else None,
            paragraph_end=max(paragraph_numbers) if paragraph_numbers else None,
            token_count=counter(chunk_text),
            entities=extracted.get("entities") or [],
            disambiguation=_disambiguation(chunk_text, prior_text, repair_text(document.get("title"))),
        )
        chunk.context_prefix = _prefix(document, chunk)
        # The normalized source body is preserved after the separator; only
        # the retrieval representation receives contextual anchors.
        chunk.contextual_text = f"{chunk.context_prefix}\n\n{chunk.text}".strip()
        chunks.append(chunk)
        prior_text = f"{prior_text}\n{chunk_text}"[-12000:]
    return [chunk.as_dict() for chunk in chunks]
