from __future__ import annotations

import copy
import re
import time
from datetime import datetime

from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine, fetch_all, is_sqlite
from app.service.bilingual_service import (
    build_bilingual_keyword_bundle,
    build_display_pair,
    enrich_result_rows_bilingual,
)
from app.service.common_service import plain_text_preview, repair_text, split_keywords
from app.service.crawler_service import sync_all_sources
from app.service.ingestion_task_service import (
    enqueue_or_reuse_hydration_task,
    get_recent_terminal_hydration_task,
)
from app.service.legal_skill_service import (
    apply_legal_result_verification,
    enhance_legal_retrieval_keywords,
)
from app.service.module_service import build_module_packet, get_module_definition, normalize_module, resolve_source_for_module
from app.service.ofac_service import OFAC_DISCOVERY_PAGE, OFAC_SEARCH_PORTAL

VALID_SOURCES = {"all", "ofac", "canlii", "canada"}
VALID_SORTS = {"relevance", "recent"}
_SEARCH_CACHE: dict[tuple, tuple[float, dict]] = {}
_MAX_SCORE_PER_KEYWORD = 13.0


def _word_similarity_expr(column: str, keyword_param: str, threshold_param: str) -> str:
    """返回 word_similarity 表达式，兼容 SQLite"""
    if is_sqlite():
        return f"0"  # SQLite 不支持 word_similarity，用 LIKE 替代
    return f"word_similarity({column}, :{keyword_param}) >= :{threshold_param}"
_CANADA_SOURCE_CODES = {
    "a2aj_case",
    "a2aj_law",
    "a2aj_regulation",
    "canlii",
    "ca_federal_act",
    "ca_federal_regulation",
    "laws_lois_xml",
    "on_statute",
    "on_regulation",
    "manual_canada_case",
    "url_canada_case",
    "manual_canada_rule",
    "url_canada_rule",
    "legal_case",
    "legal_rule",
    "canada_law",
}
_LEGISLATION_SOURCE_CODES = {
    "a2aj_law",
    "a2aj_regulation",
    "ca_federal_act",
    "ca_federal_regulation",
    "laws_lois_xml",
    "legal_rule",
    "canada_law",
    "on_statute",
    "on_regulation",
    "manual_canada_rule",
    "url_canada_rule",
}
_LOCAL_CASE_SOURCE_CODES = {"a2aj_case", "canlii", "legal_case", "manual_canada_case", "url_canada_case"}
_SUPREME_COURT_CODES = {"scc", "uksc"}
_APPEAL_COURT_CODES = {"fca", "onca", "abca", "bcca", "mbca", "nbca", "nlca", "nsca", "ntca", "nuca", "qcca", "skca", "ykca", "pescad"}
_SUPERIOR_COURT_CODES = {"fc", "onsc", "abkb", "abqb", "bcsc", "mbkb", "mbqb", "nbkb", "nbqb", "nlsc", "nssc", "ntsc", "qccs", "skkb", "skqb", "yksc", "pecsc"}
_PROVINCIAL_COURT_CODES = {"oncj", "ocj", "qccq", "skpc", "yktc", "nstc", "nspc", "pecp", "ntpc", "nupc"}


def _source_code_in_clause(codes: set[str], params: dict, prefix: str = "source_code") -> str:
    values = sorted(codes)
    if is_sqlite():
        placeholders = []
        for index, value in enumerate(values):
            key = f"{prefix}_{index}"
            params[key] = value
            placeholders.append(f":{key}")
        return f"si.source_code IN ({', '.join(placeholders)})"
    params[prefix] = values
    return f"si.source_code = ANY(:{prefix})"



def _normalize_limit(value, default: int = 30) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, 100))



def _normalize_offset(value) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 0
    return max(0, parsed)



def _normalize_source(value: str) -> str:
    source = (value or "all").strip().lower()
    return source if source in VALID_SOURCES else "all"



def _normalize_sort(value: str) -> str:
    sort = (value or "relevance").strip().lower()
    return sort if sort in VALID_SORTS else "relevance"



def _normalize_keywords_input(value: str | list[str]) -> str:
    if isinstance(value, list):
        return ", ".join([str(item).strip() for item in value if str(item).strip()])
    return str(value or "").strip()



def _cache_ttl() -> int:
    return max(0, int(getattr(settings, "cache_ttl_seconds", 300)))



def _get_cached_result(cache: dict, key: tuple):
    ttl = _cache_ttl()
    if ttl <= 0:
        return None
    entry = cache.get(key)
    if not entry:
        return None
    expires_at, payload = entry
    if expires_at <= time.time():
        cache.pop(key, None)
        return None
    return copy.deepcopy(payload)



def _set_cached_result(cache: dict, key: tuple, value: dict):
    ttl = _cache_ttl()
    if ttl <= 0:
        return
    cache[key] = (time.time() + ttl, copy.deepcopy(value))



def clear_search_cache():
    _SEARCH_CACHE.clear()



def _similarity_threshold(keyword: str) -> float:
    size = len((keyword or "").strip())
    if size <= 4:
        return 0.95
    if size <= 7:
        return 0.75
    return 0.55



def _insert_search_log(input_text: str | list[str], result_count: int):
    query_text = _normalize_keywords_input(input_text)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO sync_logs (source_code, status, message)
                VALUES ('search', 'success', :message)
                """
            ),
            {"message": f"query={query_text}, results={result_count}"},
        )



def _fmt_dt(value):
    if not value:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return repair_text(str(value))



def _clip(text, length=240):
    if not text:
        return ""
    cleaned = plain_text_preview(text)
    if len(cleaned) <= length:
        return cleaned
    return cleaned[:length] + "..."


def _normalize_relevance_score(score_value, keyword_count: int) -> float:
    try:
        raw_score = float(score_value or 0)
    except (TypeError, ValueError):
        raw_score = 0.0

    denominator = max(1.0, float(max(1, keyword_count)) * _MAX_SCORE_PER_KEYWORD)
    normalized = max(0.0, min(raw_score / denominator, 1.0))
    return round(normalized, 4)


def _court_level_label(level_value, court_code: str = "") -> str:
    try:
        level = int(level_value or 0)
    except (TypeError, ValueError):
        level = 0
    code = str(court_code or "").strip().upper()
    if level >= 5:
        return "最高法院"
    if level == 4:
        return "上诉法院"
    if level == 3:
        return "高等法院 / 联邦法院"
    if level == 2:
        return "省级 / 地方法院"
    if level == 1:
        return "Tribunal / Other"
    return code or "-"


def _court_level_sql() -> tuple[str, str]:
    database_page_expr = "LOWER(COALESCE(si.raw_json->>'database_page', ''))"
    item_url_expr = "LOWER(COALESCE(si.item_url, ''))"
    court_code_expr = (
        "COALESCE("
        f"substring({database_page_expr} from '/([a-z0-9]+)/?$'), "
        f"substring({item_url_expr} from '/([a-z0-9]+)/doc/'), "
        "''"
        ")"
    )

    def code_list(values: set[str]) -> str:
        return ", ".join(f"'{value}'" for value in sorted(values))

    court_level_expr = f"""
    CASE
        WHEN si.source_code <> 'canlii' THEN 0
        WHEN {court_code_expr} IN ({code_list(_SUPREME_COURT_CODES)}) THEN 5
        WHEN {court_code_expr} IN ({code_list(_APPEAL_COURT_CODES)}) THEN 4
        WHEN {court_code_expr} IN ({code_list(_SUPERIOR_COURT_CODES)}) THEN 3
        WHEN {court_code_expr} IN ({code_list(_PROVINCIAL_COURT_CODES)}) THEN 2
        WHEN {court_code_expr} <> '' THEN 1
        ELSE 0
    END
    """
    return court_code_expr, court_level_expr


def _decorate_result_rows(rows: list[dict], keyword_count: int) -> list[dict]:
    decorated = []
    for row in rows:
        item = dict(row)
        item["raw_score"] = row.get("score", 0)
        item["score"] = _normalize_relevance_score(row.get("score", 0), keyword_count)
        item["relevance_score"] = item["score"]
        item["court_level"] = int(row.get("court_level") or 0)
        item["court_code"] = str(row.get("court_code") or "").strip().lower()
        item["court_level_label"] = _court_level_label(item["court_level"], item["court_code"])
        decorated.append(item)
    return decorated



def _source_title_pair(original_title: str, preview: dict | None) -> dict:
    preview = preview or {}
    original = repair_text(original_title)
    english = repair_text(preview.get("title_en")) or original
    chinese = repair_text(preview.get("title_zh"))
    secondary = chinese if chinese and chinese not in {original, english} else ""
    return {"primary": english or original, "secondary": secondary}


def _build_ofac_card(row: dict, query_language: str) -> dict:
    meta = row.get("raw_json") or {}
    preview = ((meta.get("translations") or {}).get("preview") or {})
    title_pair = _source_title_pair(meta.get("sdn_name") or row.get("title"), preview)
    summary_pair = build_display_pair(
        meta.get("remarks") or row.get("summary"),
        {"summary_zh": preview.get("summary_zh"), "summary_en": preview.get("summary_en")},
        query_language,
    )
    aliases = meta.get("aliases") or []
    addresses = meta.get("addresses") or []

    fields = [
        ("Record", meta.get("ent_num")),
        ("Type", meta.get("sdn_type")),
        ("Program", meta.get("program")),
        ("Title", meta.get("title_name")),
        ("Flag", meta.get("vess_flag")),
        ("Owner", meta.get("vess_owner")),
    ]
    fields = [(label, repair_text(value)) for label, value in fields if repair_text(value)]

    return {
        "id": row["id"],
        "source_code": "ofac",
        "source_label": "OFAC Record",
        "title": repair_text(meta.get("sdn_name") or row.get("title")),
        "title_primary": title_pair["primary"],
        "title_secondary": title_pair["secondary"],
        "subtitle": " / ".join(
            [repair_text(part) for part in [meta.get("sdn_type"), meta.get("program")] if repair_text(part)]
        ),
        "published_at": _fmt_dt(row.get("published_at")),
        "summary": _clip(meta.get("remarks") or row.get("summary"), 280),
        "summary_primary": summary_pair["primary"],
        "summary_secondary": summary_pair["secondary"],
        "excerpt": "",
        "url": meta.get("official_search_url") or row.get("item_url") or OFAC_SEARCH_PORTAL,
        "source_url": meta.get("source_csv_url") or ((meta.get("source_urls") or {}).get("sdn")) or OFAC_DISCOVERY_PAGE,
        "score": row.get("score", 0),
        "fields": fields,
        "aliases": aliases[:10],
        "addresses": addresses[:6],
    }


def _build_canlii_card(row: dict, query_language: str) -> dict:
    meta = row.get("raw_json") or {}
    preview = ((meta.get("translations") or {}).get("preview") or {})
    title_pair = _source_title_pair(row.get("title"), preview)
    summary_pair = build_display_pair(
        row.get("summary"),
        {"summary_zh": preview.get("summary_zh"), "summary_en": preview.get("summary_en")},
        query_language,
    )
    database_page = meta.get("database_page", "")

    fields = [
        ("Date", _fmt_dt(row.get("published_at"))),
        ("Court Level", row.get("court_level_label")),
        ("Court Code", str(row.get("court_code") or "").upper() or "-"),
    ]
    fields = [(label, repair_text(value)) for label, value in fields if value and value != "-"]

    return {
        "id": row["id"],
        "source_code": "canlii",
        "source_label": "Case",
        "title": repair_text(row.get("title")),
        "title_primary": title_pair["primary"],
        "title_secondary": title_pair["secondary"],
        "subtitle": _clip(database_page, 80),
        "published_at": _fmt_dt(row.get("published_at")),
        "summary": _clip(row.get("summary"), 260),
        "summary_primary": summary_pair["primary"],
        "summary_secondary": summary_pair["secondary"],
        "excerpt": _clip(row.get("raw_text"), 800),
        "url": row.get("item_url"),
        "source_url": database_page if str(database_page).startswith("http") else "",
        "score": row.get("score", 0),
        "fields": fields,
        "aliases": [],
        "addresses": [],
    }


def _build_legislation_card(row: dict, query_language: str) -> dict:
    meta = row.get("raw_json") or {}
    preview = ((meta.get("translations") or {}).get("preview") or {})
    title_pair = _source_title_pair(row.get("title"), preview)
    summary_pair = build_display_pair(
        row.get("summary") or row.get("raw_text"),
        {"summary_zh": preview.get("summary_zh"), "summary_en": preview.get("summary_en")},
        query_language,
    )
    source_code = str(row.get("source_code") or "")
    jurisdiction = repair_text(meta.get("jurisdiction") or "Canada")
    level = str(meta.get("level") or ("federal" if source_code.startswith("ca_federal_") else "provincial")).lower()
    kind = str(meta.get("kind") or ("act" if "act" in source_code or "statute" in source_code else "regulation")).lower()
    citation = repair_text(meta.get("citation") or meta.get("code") or "")
    fields = [
        ("Jurisdiction", jurisdiction),
        ("Level", "Federal" if level == "federal" else "Provincial / Local"),
        ("Type", {"act": "Act", "statute": "Statute", "regulation": "Regulation"}.get(kind, kind.title())),
    ]
    if citation:
        fields.append(("Citation", citation))
    return {
        "id": row["id"],
        "source_code": source_code,
        "source_label": "Official Law",
        "title": repair_text(row.get("title")),
        "title_primary": title_pair["primary"],
        "title_secondary": title_pair["secondary"],
        "subtitle": citation,
        "published_at": _fmt_dt(row.get("published_at")),
        "summary": _clip(row.get("summary") or row.get("raw_text"), 280),
        "summary_primary": summary_pair["primary"],
        "summary_secondary": summary_pair["secondary"],
        "excerpt": _clip(row.get("raw_text"), 900),
        "url": row.get("item_url"),
        "source_url": meta.get("xml_url") or meta.get("source_csv_url") or meta.get("index_url") or "",
        "score": row.get("score", 0),
        "fields": fields,
        "aliases": [],
        "addresses": [],
    }


def _prepare_cards(rows: list[dict], query_language: str) -> dict:
    groups = {"legislation": [], "canlii": [], "ofac": [], "other": []}

    for row in rows:
        source_code = row.get("source_code")
        if source_code in _LEGISLATION_SOURCE_CODES:
            groups["legislation"].append(_build_legislation_card(row, query_language))
            continue
        if source_code == "ofac":
            groups["ofac"].append(_build_ofac_card(row, query_language))
            continue
        if row.get("source_kind") == "case" or source_code in _LOCAL_CASE_SOURCE_CODES:
            groups["canlii"].append(_build_canlii_card(row, query_language))
            continue

        meta = row.get("raw_json") or {}
        preview = ((meta.get("translations") or {}).get("preview") or {})
        title_pair = _source_title_pair(row.get("title"), preview)
        summary_pair = build_display_pair(
            row.get("summary"),
            {"summary_zh": preview.get("summary_zh"), "summary_en": preview.get("summary_en")},
            query_language,
        )
        groups["other"].append(
            {
                "id": row["id"],
                "source_code": source_code,
                "source_label": repair_text(str(source_code or "record").upper()),
                "title": repair_text(row.get("title")),
                "title_primary": title_pair["primary"],
                "title_secondary": title_pair["secondary"],
                "subtitle": "",
                "published_at": _fmt_dt(row.get("published_at")),
                "summary": _clip(row.get("summary"), 260),
                "summary_primary": summary_pair["primary"],
                "summary_secondary": summary_pair["secondary"],
                "excerpt": _clip(row.get("raw_text"), 800),
                "url": row.get("item_url"),
                "source_url": meta.get("source_url", ""),
                "score": row.get("score", 0),
                "fields": [],
                "aliases": [],
                "addresses": [],
            }
        )

    return groups


def _can_use_fast_rag_search(module: str, source: str) -> bool:
    return (
        bool(getattr(settings, "search_fast_rag_enabled", True))
        and bool(getattr(settings, "rag_enabled", True))
        and module == "canada"
        and source in {"all", "canada", "canlii"}
    )


def _fast_rag_source_filter(source: str) -> str:
    if source == "canlii":
        return "case"
    return "canada"


_FAST_ZH_QUERY_EXPANSIONS = [
    (("遗产", "继承", "遗嘱", "信托", "受托"), ("estate", "inheritance", "will", "trustee", "fiduciary duty")),
    (("合同", "违约", "协议", "履行"), ("contract", "breach", "damages", "performance")),
    (("租赁", "租客", "房东", "驱逐", "退租", "解除租约"), ("tenant", "landlord", "lease", "eviction", "termination")),
    (("劳动", "雇佣", "解雇", "赔偿金"), ("employment", "dismissal", "termination", "reasonable notice")),
    (("侵权", "过失", "损害", "赔偿"), ("negligence", "duty of care", "causation", "damages")),
    (("婚姻", "离婚", "抚养", "监护"), ("family", "divorce", "custody", "child support")),
    (("隐私", "个人信息", "数据"), ("privacy", "personal information", "PIPEDA", "consent")),
]
_FAST_ASSOCIATION_STOP_TERMS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "into",
    "under",
    "case",
    "cases",
    "law",
    "laws",
    "act",
    "acts",
    "rule",
    "rules",
    "regulation",
    "regulations",
    "canada",
    "canadian",
}


def _fast_zh_query_expansions(text_value: str) -> list[str]:
    expansions = []
    for markers, terms in _FAST_ZH_QUERY_EXPANSIONS:
        if any(marker in text_value for marker in markers):
            expansions.extend(terms)
    return expansions


def _fast_rag_keywords(
    keywords_input: str | list[str],
    keywords: list[str],
    skill_profile: dict,
    limit: int = 6,
) -> list[str]:
    raw_text = _normalize_keywords_input(keywords_input)
    base_terms = split_keywords(keywords_input)
    entities = skill_profile.get("entities") or {}
    priority_terms = []
    for key in ("case_citations", "statute_references", "section_references", "courts"):
        priority_terms.extend(entities.get(key) or [])
    translated_terms = _fast_zh_query_expansions(" ".join([raw_text] + list(keywords or [])))

    selected = []
    seen = set()
    for value in priority_terms + base_terms + translated_terms + list(keywords or []):
        clean = repair_text(value)
        key = clean.lower()
        if not clean or key in seen:
            continue
        if " " in clean and clean not in priority_terms and len(selected) >= max(2, len(base_terms)):
            continue
        selected.append(clean)
        seen.add(key)
        if len(selected) >= limit:
            break
    return selected or list(keywords or [])[:limit]


def _fast_case_scope(level_label: str, source_code: str) -> str:
    text_value = repair_text(" ".join([level_label, source_code])).lower()
    if any(token in text_value for token in ("supreme", "federal", "scc", "fca")):
        return "national_federal"
    if any(token in text_value for token in ("appeal", "superior", "court", "provincial", "tribunal", "board")):
        return "provincial_local"
    return "provincial_local"


def _fast_case_columns(cases: list[dict], limit: int = 4) -> list[dict]:
    national = [item for item in cases if item.get("scope") == "national_federal"][:limit]
    local = [item for item in cases if item.get("scope") == "provincial_local"][:limit]
    return [
        {
            "key": "national_federal",
            "label": "国家 / 联邦法院",
            "label_en": "National / Federal Courts",
            "description": "本地索引命中的全国性或联邦体系案例。",
            "items": national,
            "empty_copy": "当前没有命中更高位阶或联邦体系案例。",
        },
        {
            "key": "provincial_local",
            "label": "省级 / 地方法院",
            "label_en": "Provincial / Local Courts",
            "description": "本地索引命中的省级、地方或专门机构案例。",
            "items": local,
            "empty_copy": "当前没有命中省级或地方层面的直接案例。",
        },
    ]


def _fast_row_from_rag_item(item: dict, query_language: str) -> dict:
    meta = dict(item.get("metadata") or {})
    source_kind = repair_text(item.get("source_kind")) or "document"
    source_code = repair_text(item.get("source_code"))
    citation = repair_text(item.get("citation") or meta.get("citation"))
    jurisdiction = repair_text(item.get("jurisdiction") or meta.get("jurisdiction") or meta.get("country") or "Canada")
    document_type = repair_text(item.get("document_type") or meta.get("legal_type") or meta.get("case_type") or source_kind)
    level_label = repair_text(item.get("court_level") or meta.get("court_level") or meta.get("court_name") or "")
    raw_score = float(item.get("score") or 0)
    score = round(max(0.0, min(raw_score, 1.0)), 4)
    raw_json = {
        **meta,
        "citation": citation,
        "jurisdiction": jurisdiction,
        "document_type": document_type,
        "source_kind": source_kind,
        "retrieval_channel": "rag_fast",
    }
    if source_kind == "law":
        raw_json.setdefault("kind", document_type)
        raw_json.setdefault("level", meta.get("law_level") or "federal")
        raw_json.setdefault("source_url", item.get("source_url"))
    if source_kind == "case":
        raw_json.setdefault("database_page", item.get("source_url"))
    return {
        "id": int(item.get("chunk_id") or item.get("source_id") or 0),
        "source_id": int(item.get("source_id") or 0),
        "source_table": repair_text(item.get("source_table")),
        "source_kind": source_kind,
        "source_code": source_code,
        "source_uid": repair_text(item.get("source_uid")),
        "title": repair_text(item.get("title")) or "本地资料",
        "item_url": repair_text(item.get("source_url")),
        "published_at": item.get("published_at") or None,
        "summary": _clip(item.get("excerpt"), 360),
        "raw_text": repair_text(item.get("excerpt")),
        "raw_json": raw_json,
        "score": score,
        "raw_score": raw_score,
        "absolute_match_score": raw_score,
        "similarity_score": score,
        "relevance_score": score,
        "court_level": 0,
        "court_code": "",
        "court_level_label": level_label,
        "query_language": query_language,
    }


def _fast_row_key(row: dict) -> str:
    source_kind = repair_text(row.get("source_kind")).lower()
    title_key = repair_text(row.get("title")).lower()
    source_uid = repair_text(row.get("source_uid")).lower()
    if source_kind == "case":
        if title_key:
            return f"case:title:{title_key}"
        if source_uid:
            return f"case:uid:{source_uid}"
    if source_kind == "law" and title_key:
        citation = repair_text((row.get("raw_json") or {}).get("citation")).lower()
        return f"law:title:{title_key}:{citation}"
    source_table = repair_text(row.get("source_table")).lower()
    source_id = row.get("source_id")
    if source_table and source_id:
        return f"{source_kind}:{source_table}:{source_id}"
    if source_uid:
        return f"{source_kind}:uid:{source_uid}"
    return f"{source_kind}:title:{title_key}"


def _fast_identity_key(row: dict) -> str:
    source_table = repair_text(row.get("source_table")).lower()
    source_id = int(row.get("source_id") or 0)
    if source_table and source_id:
        return f"{source_table}:{source_id}"
    return _fast_row_key(row)


def _dedupe_fast_rows(rows: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for row in rows:
        key = _fast_row_key(row)
        current = best.get(key)
        if current is None or float(row.get("raw_score") or 0) > float(current.get("raw_score") or 0):
            best[key] = row
    return list(best.values())


def _apply_fast_similarity_scores(rows: list[dict]) -> list[dict]:
    max_by_kind: dict[str, float] = {}
    for row in rows:
        source_kind = repair_text(row.get("source_kind")).lower() or "document"
        max_by_kind[source_kind] = max(max_by_kind.get(source_kind, 0.0), float(row.get("raw_score") or 0.0))
    for row in rows:
        source_kind = repair_text(row.get("source_kind")).lower() or "document"
        raw_score = max(0.0, float(row.get("raw_score") or 0.0))
        denominator = max(1.0, max_by_kind.get(source_kind, 0.0))
        similarity = round(max(0.0, min(raw_score / denominator, 1.0)), 4)
        row["absolute_match_score"] = round(raw_score, 4)
        row["similarity_score"] = similarity
        row["score"] = similarity
        row["relevance_score"] = similarity
    return rows


def _fast_assoc_tokens(*values) -> set[str]:
    text_value = " ".join([plain_text_preview(value) for value in values if repair_text(value)]).lower()
    tokens = re.findall(r"[a-z][a-z0-9'/-]{2,}", text_value)
    return {token for token in tokens if token not in _FAST_ASSOCIATION_STOP_TERMS and len(token) >= 4}


def _fast_law_case_match_score(law: dict, case: dict) -> float:
    law_terms = _fast_assoc_tokens(
        law.get("title"),
        law.get("citation"),
        law.get("article_no"),
        law.get("article_summary"),
        law.get("reason"),
    )
    case_terms = _fast_assoc_tokens(
        case.get("title"),
        case.get("summary"),
        case.get("excerpt"),
        case.get("match_reason"),
    )
    overlap = law_terms & case_terms
    case_score = float(case.get("similarity_score") or case.get("match_score") or case.get("score") or 0)
    law_score = float(law.get("similarity_score") or law.get("match_score") or law.get("score") or 0)
    overlap_score = min(0.25, len(overlap) * 0.05)
    combined = case_score * 0.7 + law_score * 0.15 + overlap_score
    return round(max(0.0, min(combined, 1.0)), 4)


def _fast_law_entry(row: dict) -> dict:
    meta = row.get("raw_json") or {}
    source_uid = repair_text(row.get("source_uid"))
    detail_url = ""
    if row.get("source_table") in {"legal_rules", "canada_laws"} and source_uid:
        detail_url = f"/law/canada/{source_uid}"
    citation = repair_text(meta.get("citation"))
    legal_type = repair_text(meta.get("document_type") or meta.get("legal_type") or meta.get("kind") or "法规")
    return {
        "rule_id": row.get("source_id") or row.get("id"),
        "law_id": row.get("source_id") or row.get("id"),
        "title": repair_text(row.get("title")),
        "country": repair_text(meta.get("country") or meta.get("jurisdiction") or "加拿大"),
        "level": repair_text(meta.get("level") or meta.get("law_level") or "本地法源"),
        "legal_type": legal_type,
        "rule_level": repair_text(meta.get("rule_level") or meta.get("level") or legal_type),
        "article_no": citation,
        "citation": citation,
        "article_text": "",
        "article_summary": repair_text(row.get("summary")),
        "reason": "本地 RAG 索引直接命中，适合作为当前检索或案情分析的主要法律入口。",
        "match_reason": "本地 RAG 索引直接命中。",
        "match_score": row.get("similarity_score") or row.get("score") or 0,
        "similarity_score": row.get("similarity_score") or row.get("score") or 0,
        "source_url": repair_text(row.get("item_url") or meta.get("source_url")),
        "detail_url": detail_url,
        "linked_case_count": 0,
        "national_case_count": 0,
        "local_case_count": 0,
        "related_cases": [],
        "case_columns": [],
        "origin": "rag_fast",
    }


def _fast_rule_from_law(law: dict, match_score: float | None = None) -> dict:
    return {
        "rule_id": law.get("rule_id") or law.get("law_id"),
        "title": repair_text(law.get("title")),
        "country": law.get("country") or "加拿大",
        "legal_type": law.get("legal_type") or law.get("rule_level") or "法规",
        "article_no": law.get("article_no") or law.get("citation") or "",
        "article_text": law.get("article_text") or "",
        "article_summary": law.get("article_summary") or law.get("reason") or "",
        "source_url": law.get("source_url") or "",
        "detail_url": law.get("detail_url") or "",
        "source_site": "本地资料库",
        "rule_level": law.get("rule_level") or law.get("level") or "",
        "match_score": match_score if match_score is not None else law.get("similarity_score") or law.get("match_score") or 0.55,
        "match_reason": "与当前检索命中的本地法规共同出现在 RAG 结果中，暂按快速分析关联展示。",
    }


def _fast_rule_from_relation(row: dict) -> dict:
    slug = repair_text(row.get("rule_slug"))
    detail_url = f"/law/canada/{slug}" if slug else ""
    return {
        "rule_id": int(row.get("rule_id") or 0),
        "title": repair_text(row.get("rule_title")),
        "country": repair_text(row.get("rule_country") or "加拿大"),
        "legal_type": repair_text(row.get("legal_type") or "法规"),
        "article_no": repair_text(row.get("article_no") or row.get("citation")),
        "article_text": "",
        "article_summary": repair_text(row.get("article_summary")),
        "source_url": repair_text(row.get("rule_source_url")),
        "detail_url": detail_url,
        "source_site": "本地正式关联表",
        "rule_level": repair_text(row.get("rule_level")),
        "match_score": float(row.get("relation_score") or 0),
        "match_reason": repair_text(row.get("match_reason")) or "来自本地 case_rule_relations 正式关联。",
        "relation_type": repair_text(row.get("relation_type")),
    }


def _fast_law_from_formal_rule(rule: dict, case: dict) -> dict:
    similarity = round(
        max(float(case.get("similarity_score") or 0), float(rule.get("match_score") or 0) * 0.9),
        4,
    )
    return {
        "rule_id": int(rule.get("rule_id") or 0),
        "law_id": int(rule.get("rule_id") or 0),
        "title": repair_text(rule.get("title")),
        "country": repair_text(rule.get("country") or "加拿大"),
        "level": repair_text(rule.get("rule_level") or "本地正式关联"),
        "legal_type": repair_text(rule.get("legal_type") or "法规"),
        "rule_level": repair_text(rule.get("rule_level") or "本地正式关联"),
        "article_no": repair_text(rule.get("article_no")),
        "citation": repair_text(rule.get("article_no")),
        "article_text": "",
        "article_summary": repair_text(rule.get("article_summary")),
        "reason": "由本次命中的案例正式关联反向补充，适合作为当前案情的法规锚点。",
        "match_reason": repair_text(rule.get("match_reason")) or "来自本地 case_rule_relations 正式关联。",
        "match_score": similarity,
        "similarity_score": similarity,
        "source_url": repair_text(rule.get("source_url")),
        "detail_url": repair_text(rule.get("detail_url")),
        "linked_case_count": 0,
        "national_case_count": 0,
        "local_case_count": 0,
        "related_cases": [],
        "case_columns": [],
        "origin": "case_rule_relations",
        "_relation_key": f"legal_rules:{int(rule.get('rule_id') or 0)}" if int(rule.get("rule_id") or 0) else "",
        "_formal_rule_id": int(rule.get("rule_id") or 0),
    }


def _fast_relation_context(case_rows: list[dict], law_rows: list[dict]) -> dict:
    if is_sqlite():
        return {"case_rules": {}, "case_ids": {}, "law_cases": {}}

    case_keys_by_case_id: dict[int, list[str]] = {}
    case_keys_by_source_item_id: dict[int, list[str]] = {}
    for row in case_rows:
        source_id = int(row.get("source_id") or 0)
        if not source_id:
            continue
        key = _fast_identity_key(row)
        if repair_text(row.get("source_table")).lower() == "legal_cases":
            case_keys_by_case_id.setdefault(source_id, []).append(key)
        elif repair_text(row.get("source_table")).lower() == "source_items":
            case_keys_by_source_item_id.setdefault(source_id, []).append(key)

    law_keys_by_rule_id: dict[int, list[str]] = {}
    law_keys_by_canada_law_id: dict[int, list[str]] = {}
    law_keys_by_source_item_id: dict[int, list[str]] = {}
    for row in law_rows:
        source_id = int(row.get("source_id") or 0)
        if not source_id:
            continue
        key = _fast_identity_key(row)
        table = repair_text(row.get("source_table")).lower()
        if table == "legal_rules":
            law_keys_by_rule_id.setdefault(source_id, []).append(key)
        elif table == "canada_laws":
            law_keys_by_canada_law_id.setdefault(source_id, []).append(key)
        elif table == "source_items":
            law_keys_by_source_item_id.setdefault(source_id, []).append(key)

    relation_columns = """
        crr.match_score AS relation_score,
        crr.relation_type,
        crr.match_reason,
        lc.id AS legal_case_id,
        lc.source_item_id AS case_source_item_id,
        lc.title AS case_title,
        lc.summary AS case_summary,
        lc.raw_text AS case_raw_text,
        lc.source_url AS case_source_url,
        lc.judgment_date,
        lc.court_level,
        lc.court_name,
        lc.source_code AS case_source_code,
        lr.id AS rule_id,
        lr.canada_law_id,
        lr.source_item_id AS rule_source_item_id,
        lr.title AS rule_title,
        lr.country AS rule_country,
        lr.legal_type,
        lr.article_no,
        lr.article_summary,
        lr.source_url AS rule_source_url,
        lr.slug AS rule_slug,
        lr.rule_level,
        lr.citation
    """

    case_rules: dict[str, list[dict]] = {}
    case_ids_by_key: dict[str, int] = {}
    case_clauses = []
    case_params: dict = {}
    if case_keys_by_case_id:
        case_params["case_ids"] = sorted(case_keys_by_case_id)
        case_clauses.append("lc.id = ANY(:case_ids)")
    if case_keys_by_source_item_id:
        case_params["case_source_item_ids"] = sorted(case_keys_by_source_item_id)
        case_clauses.append("lc.source_item_id = ANY(:case_source_item_ids)")
    if case_clauses:
        sql = f"""
        SELECT {relation_columns}
        FROM case_rule_relations crr
        JOIN legal_cases lc ON lc.id = crr.case_id
        JOIN legal_rules lr ON lr.id = crr.rule_id
        WHERE {" OR ".join(case_clauses)}
        ORDER BY lc.id ASC, crr.match_score DESC, lr.title ASC
        """
        with engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(text(sql), case_params).mappings().all()]
        for relation in rows:
            keys = []
            legal_case_id = int(relation.get("legal_case_id") or 0)
            case_source_item_id = int(relation.get("case_source_item_id") or 0)
            keys.extend(case_keys_by_case_id.get(legal_case_id, []))
            keys.extend(case_keys_by_source_item_id.get(case_source_item_id, []))
            for key in keys:
                case_ids_by_key[key] = legal_case_id
                case_rules.setdefault(key, []).append(_fast_rule_from_relation(relation))

    law_cases: dict[str, list[dict]] = {}
    law_clauses = []
    law_params: dict = {}
    if law_keys_by_rule_id:
        law_params["rule_ids"] = sorted(law_keys_by_rule_id)
        law_clauses.append("lr.id = ANY(:rule_ids)")
    if law_keys_by_canada_law_id:
        law_params["canada_law_ids"] = sorted(law_keys_by_canada_law_id)
        law_clauses.append("lr.canada_law_id = ANY(:canada_law_ids)")
    if law_keys_by_source_item_id:
        law_params["rule_source_item_ids"] = sorted(law_keys_by_source_item_id)
        law_clauses.append("lr.source_item_id = ANY(:rule_source_item_ids)")
    if law_clauses:
        sql = f"""
        SELECT {relation_columns}
        FROM case_rule_relations crr
        JOIN legal_cases lc ON lc.id = crr.case_id
        JOIN legal_rules lr ON lr.id = crr.rule_id
        WHERE {" OR ".join(law_clauses)}
        ORDER BY crr.match_score DESC, lc.judgment_date DESC NULLS LAST, lc.id DESC
        LIMIT 500
        """
        with engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(text(sql), law_params).mappings().all()]
        for relation in rows:
            keys = []
            rule_id = int(relation.get("rule_id") or 0)
            canada_law_id = int(relation.get("canada_law_id") or 0)
            rule_source_item_id = int(relation.get("rule_source_item_id") or 0)
            keys.extend(law_keys_by_rule_id.get(rule_id, []))
            keys.extend(law_keys_by_canada_law_id.get(canada_law_id, []))
            keys.extend(law_keys_by_source_item_id.get(rule_source_item_id, []))
            for key in keys:
                law_cases.setdefault(key, []).append(relation)

    for key, rules in case_rules.items():
        rules.sort(key=lambda item: float(item.get("match_score") or 0), reverse=True)
        case_rules[key] = rules[:4]

    return {"case_rules": case_rules, "case_ids": case_ids_by_key, "law_cases": law_cases}


def _fast_related_case_from_relation(row: dict, query_case: dict | None = None) -> dict:
    similarity = float((query_case or {}).get("similarity_score") or row.get("relation_score") or 0)
    level_label = repair_text(row.get("court_level") or row.get("court_name") or "本地案例")
    return {
        "id": int(row.get("legal_case_id") or 0),
        "case_id": int(row.get("legal_case_id") or 0),
        "title": repair_text(row.get("case_title")),
        "title_primary": repair_text(row.get("case_title")),
        "summary": plain_text_preview(row.get("case_summary"))[:360],
        "summary_primary": plain_text_preview(row.get("case_summary"))[:360],
        "excerpt": plain_text_preview(row.get("case_raw_text"))[:900],
        "url": repair_text(row.get("case_source_url")),
        "source_url": repair_text(row.get("case_source_url")),
        "published_at": str(row.get("judgment_date") or "")[:10],
        "judgment_date": str(row.get("judgment_date") or "")[:10],
        "score": similarity,
        "match_score": similarity,
        "similarity_score": similarity,
        "absolute_match_score": float(row.get("relation_score") or 0),
        "law_case_similarity": float(row.get("relation_score") or 0),
        "relevance_label": "相似度",
        "relevance_level": "medium",
        "match_reason": repair_text(row.get("match_reason")) or "来自本地正式案例-法规关联。",
        "country": "加拿大",
        "case_type": "案例",
        "court_level": level_label,
        "court_level_label": level_label,
        "source_code": repair_text(row.get("case_source_code")),
        "source_kind": "case",
        "rules": [_fast_rule_from_relation(row)],
        "scope": _fast_case_scope(level_label, row.get("case_source_code", "")),
        "relation_status": "formal_relation",
        "is_retrieved_result": bool(query_case),
        "supporting_arguments": [],
        "opposing_arguments": [],
    }


def _fast_case_entry(
    row: dict,
    laws: list[dict],
    *,
    formal_rules: list[dict] | None = None,
    formal_case_id: int | None = None,
) -> dict:
    meta = row.get("raw_json") or {}
    level_label = repair_text(row.get("court_level_label") or meta.get("court_level") or meta.get("court_name") or "本地案例")
    source_url = repair_text(row.get("item_url") or meta.get("source_url") or meta.get("database_page"))
    similarity = float(row.get("similarity_score") or row.get("score") or 0)
    if formal_rules:
        rules = formal_rules[:4]
        relation_status = "formal_relation"
    else:
        ranked_laws = sorted(
            laws,
            key=lambda law: _fast_law_case_match_score(law, {**row, "similarity_score": similarity}),
            reverse=True,
        )
        rules = [
            _fast_rule_from_law(law, _fast_law_case_match_score(law, {**row, "similarity_score": similarity}))
            for law in ranked_laws[:2]
        ]
        relation_status = "retrieved_pending_relation"
    return {
        "id": row.get("id"),
        "case_id": formal_case_id or (row.get("source_id") if row.get("source_table") == "legal_cases" else None),
        "title": repair_text(row.get("title")),
        "title_primary": repair_text(row.get("title")),
        "title_secondary": "",
        "summary": plain_text_preview(row.get("summary"))[:360],
        "summary_primary": plain_text_preview(row.get("summary"))[:360],
        "summary_secondary": "",
        "excerpt": plain_text_preview(row.get("raw_text"))[:900],
        "url": source_url,
        "source_url": source_url,
        "published_at": str(row.get("published_at") or "")[:10],
        "judgment_date": str(row.get("published_at") or "")[:10],
        "score": float(row.get("score") or 0),
        "match_score": similarity,
        "similarity_score": similarity,
        "absolute_match_score": row.get("absolute_match_score") or row.get("raw_score") or 0,
        "relevance_label": "相似度",
        "relevance_level": "medium",
        "match_reason": "本地 RAG 案例索引命中相近事实或法律争点，并按相似度排序。",
        "country": repair_text(meta.get("country") or meta.get("jurisdiction") or "加拿大"),
        "case_type": repair_text(meta.get("case_type") or meta.get("document_type") or row.get("source_code") or "案例"),
        "court_level": level_label,
        "court_level_label": level_label,
        "court_code": "",
        "source_code": row.get("source_code", ""),
        "source_kind": row.get("source_kind", ""),
        "rules": rules,
        "scope": _fast_case_scope(level_label, row.get("source_code", "")),
        "relation_status": relation_status,
        "is_retrieved_result": True,
        "supporting_arguments": [],
        "opposing_arguments": [],
    }


def _fast_module_packet(rows: list[dict], module: str, query_language: str) -> dict:
    definition = get_module_definition(module)
    law_rows = sorted(
        [row for row in rows if row.get("source_kind") == "law"],
        key=lambda row: float(row.get("similarity_score") or row.get("score") or 0),
        reverse=True,
    )
    case_rows = sorted(
        [row for row in rows if row.get("source_kind") == "case"],
        key=lambda row: float(row.get("similarity_score") or row.get("score") or 0),
        reverse=True,
    )
    selected_law_rows = law_rows[:8]
    selected_case_rows = case_rows[:12]
    relation_context = _fast_relation_context(selected_case_rows, selected_law_rows)
    laws = []
    for row in selected_law_rows:
        law = _fast_law_entry(row)
        law["_relation_key"] = _fast_identity_key(row)
        laws.append(law)
    if not laws and case_rows:
        laws = [
            {
                "rule_id": 0,
                "law_id": 0,
                "title": "本地资料命中",
                "country": "加拿大",
                "level": "本地资料",
                "legal_type": "法规 / 案例",
                "rule_level": "本地资料",
                "article_no": "",
                "citation": "",
                "article_text": "",
                "article_summary": "当前关键词先命中了本地案例，暂未命中可以直接锚定的法规条目。",
                "reason": "当前关键词先命中了本地案例，暂未命中可以直接锚定的法规条目。",
                "match_reason": "本地 RAG 索引命中。",
                "source_url": "",
                "detail_url": "",
                "linked_case_count": 0,
                "national_case_count": 0,
                "local_case_count": 0,
                "related_cases": [],
                "case_columns": [],
                "origin": "rag_fast",
                "_relation_key": "",
            }
        ]
    cases = []
    cases_by_id: dict[int, dict] = {}
    for row in selected_case_rows:
        key = _fast_identity_key(row)
        formal_rules = relation_context.get("case_rules", {}).get(key, [])
        formal_case_id = relation_context.get("case_ids", {}).get(key)
        case = _fast_case_entry(row, laws, formal_rules=formal_rules, formal_case_id=formal_case_id)
        cases.append(case)
        if case.get("case_id"):
            cases_by_id[int(case["case_id"])] = case

    law_dedupe = {
        (repair_text(law.get("title")).lower(), repair_text(law.get("citation") or law.get("article_no")).lower())
        for law in laws
    }
    for case in cases:
        for rule in case.get("rules") or []:
            if not rule.get("rule_id") or case.get("relation_status") != "formal_relation":
                continue
            key = (repair_text(rule.get("title")).lower(), repair_text(rule.get("article_no")).lower())
            if key in law_dedupe:
                continue
            law_dedupe.add(key)
            laws.append(_fast_law_from_formal_rule(rule, case))

    laws.sort(
        key=lambda law: (
            1 if law.get("origin") == "case_rule_relations" else 0,
            float(law.get("similarity_score") or law.get("match_score") or 0),
        ),
        reverse=True,
    )
    laws = laws[:8]

    for law in laws:
        formal_rule_id = int(law.get("_formal_rule_id") or 0)
        if formal_rule_id:
            related_cases = []
            for case in cases:
                matched_rule = next(
                    (
                        rule
                        for rule in case.get("rules") or []
                        if int(rule.get("rule_id") or 0) == formal_rule_id
                    ),
                    None,
                )
                if not matched_rule:
                    continue
                related = dict(case)
                related["law_case_similarity"] = float(matched_rule.get("match_score") or case.get("similarity_score") or 0)
                related["relation_status"] = "formal_relation"
                related_cases.append(related)
            related_cases.sort(
                key=lambda case: (
                    float(case.get("similarity_score") or 0),
                    float(case.get("law_case_similarity") or 0),
                ),
                reverse=True,
            )
            related_cases = related_cases[:4]
        else:
            formal_related_rows = relation_context.get("law_cases", {}).get(law.get("_relation_key", ""), [])
            related_cases = []
            seen_related = set()
            for relation in formal_related_rows:
                legal_case_id = int(relation.get("legal_case_id") or 0)
                if not legal_case_id or legal_case_id in seen_related:
                    continue
                seen_related.add(legal_case_id)
                related_cases.append(_fast_related_case_from_relation(relation, cases_by_id.get(legal_case_id)))
                if len(related_cases) >= 4:
                    break
            if related_cases:
                related_cases.sort(
                    key=lambda case: (
                        float(case.get("similarity_score") or 0),
                        float(case.get("law_case_similarity") or 0),
                    ),
                    reverse=True,
                )
            else:
                related_cases = sorted(
                    [dict(case) for case in cases],
                    key=lambda case: _fast_law_case_match_score(law, case),
                    reverse=True,
                )[:4]
                for case in related_cases:
                    case["law_case_similarity"] = _fast_law_case_match_score(law, case)
                    case["relation_status"] = "retrieved_pending_relation"
        law["related_cases"] = related_cases
        law["case_columns"] = _fast_case_columns(related_cases)
        law["linked_case_count"] = len(related_cases)
        law["national_case_count"] = len([item for item in related_cases if item.get("scope") == "national_federal"])
        law["local_case_count"] = len([item for item in related_cases if item.get("scope") == "provincial_local"])
        law.pop("_relation_key", None)
        law.pop("_formal_rule_id", None)
    return {
        "module_code": module,
        "module_label": definition.get("label", "加拿大法规与案例模块"),
        "module_label_en": definition.get("label_en", ""),
        "focus_title": "本地资料快速命中",
        "focus_title_en": "Local Materials Fast Retrieval",
        "focus_copy": "当前结果来自本地 RAG 分块索引，法规与案例分路检索；案例按相似度排序，并挂载到相关法规下展示。",
        "focus_copy_en": "Results come from the local RAG index.",
        "notice": "快速检索不会实时访问远程网页；正式关联优先使用本地关系表，不足时按本次案例相似度临时挂载到相关法规下。",
        "notice_en": "",
        "relevant_laws": laws[:8],
        "case_law_rows": cases,
        "authority_groups": [],
        "transition_playbook": [],
        "suggested_questions": definition.get("question_prompts", []),
        "suggested_questions_en": definition.get("question_prompts_en", []),
        "agent_placeholder": definition.get("agent_placeholder", ""),
        "query_language": query_language,
    }


def _search_items_fast_rag(
    *,
    keywords_input: str | list[str],
    keywords: list[str],
    limit: int,
    offset: int,
    source: str,
    sort: str,
    module: str,
    query_language: str,
    keyword_bundle: dict,
    skill_profile: dict,
) -> dict | None:
    if not _can_use_fast_rag_search(module, source):
        return None
    try:
        from app.service.rag_service import rag_search

        search_keywords = _fast_rag_keywords(keywords_input, keywords, skill_profile)
        query_text = _normalize_keywords_input(keywords_input) or " ".join(search_keywords)
        if isinstance(keywords_input, list):
            query_text = " ".join(search_keywords)
        fetch_limit = max(limit, min(offset + limit + 1, 30))
        if source == "canlii":
            case_target = fetch_limit
            law_target = 0
        else:
            case_target = max(4, min(12, fetch_limit // 2))
            law_target = max(4, min(12, fetch_limit - case_target))

        law_items = []
        if law_target:
            law_result = rag_search(
                query_text,
                keywords=search_keywords,
                module=module,
                source_filter="law",
                limit=law_target,
            )
            law_items = law_result.get("items") or []
        case_result = rag_search(
            query_text,
            keywords=search_keywords,
            module=module,
            source_filter="case",
            limit=case_target,
        )
        case_items = case_result.get("items") or []
    except Exception:
        return None

    law_rows = _dedupe_fast_rows([_fast_row_from_rag_item(item, query_language) for item in law_items])
    case_rows = _dedupe_fast_rows([_fast_row_from_rag_item(item, query_language) for item in case_items])
    all_rows = _apply_fast_similarity_scores(law_rows + case_rows)
    if sort == "recent":
        all_rows.sort(key=lambda row: str(row.get("published_at") or ""), reverse=True)
    else:
        law_rows = sorted(
            [row for row in all_rows if row.get("source_kind") == "law"],
            key=lambda row: float(row.get("similarity_score") or 0),
            reverse=True,
        )
        case_rows = sorted(
            [row for row in all_rows if row.get("source_kind") == "case"],
            key=lambda row: float(row.get("similarity_score") or 0),
            reverse=True,
        )
        all_rows = law_rows + case_rows
    rows = all_rows[offset : offset + limit]
    grouped_results = _prepare_cards(rows, query_language)
    source_counts = {"legislation": 0, "canlii": 0, "ofac": 0, "other": 0}
    for row in rows:
        source_kind = row.get("source_kind")
        source_code = row.get("source_code")
        if source_kind == "law" or source_code in _LEGISLATION_SOURCE_CODES:
            source_counts["legislation"] += 1
        elif source_kind == "case" or source_code in _LOCAL_CASE_SOURCE_CODES:
            source_counts["canlii"] += 1
        elif source_kind == "ofac" or source_code == "ofac":
            source_counts["ofac"] += 1
        else:
            source_counts["other"] += 1

    has_next = len(all_rows) > offset + limit
    previous_offset = max(offset - limit, 0)
    next_offset = offset + limit
    total = offset + len(rows) + (1 if has_next else 0)
    payload = {
        "input_text": _normalize_keywords_input(keywords_input),
        "keywords": search_keywords,
        "total": total,
        "page_count": len(rows),
        "offset": offset,
        "source": source,
        "sort": sort,
        "module_code": module,
        "module_profile": get_module_definition(module),
        "query_language": query_language,
        "bilingual_query": keyword_bundle,
        "results": rows,
        "grouped_results": grouped_results,
        "source_counts": source_counts,
        "has_previous": offset > 0,
        "has_next": has_next,
        "previous_offset": previous_offset,
        "next_offset": next_offset,
        "cache_status": "miss",
        "legal_skill_profile": skill_profile,
        "retrieval_entities": skill_profile.get("entities", {}),
        "legal_skills": skill_profile.get("skills", []),
        "search_backend": "rag_chunks_tsvector",
        "rag_status": "ok",
        "fast_retrieval_keywords": search_keywords,
    }
    payload["module_packet"] = _fast_module_packet(all_rows[: max(limit, 12)], module, query_language)
    return payload



def search_items(
    keywords_input: str | list[str],
    limit: int = 30,
    offset: int = 0,
    source: str = "all",
    sort: str = "relevance",
    module: str = "canada",
    refresh: bool = False,
    display_language: str | None = None,
    keyword_weights: dict[str, float] | None = None,
):
    module = normalize_module(module)
    keyword_bundle = build_bilingual_keyword_bundle(keywords_input, module)
    keywords = keyword_bundle.get("retrieval_keywords") or split_keywords(keywords_input)
    skill_profile = enhance_legal_retrieval_keywords(
        keywords_input,
        module=module,
        base_keywords=keywords,
        keyword_weights=keyword_weights,
    )
    keywords = skill_profile.get("keywords") or keywords
    keyword_weights = skill_profile.get("keyword_weights") or keyword_weights or {}
    limit = _normalize_limit(limit)
    offset = _normalize_offset(offset)
    source = resolve_source_for_module(module, source)
    sort = _normalize_sort(sort)
    query_language = str(display_language or keyword_bundle.get("query_language") or "zh")
    cache_key = ("search", module, tuple(keywords), limit, offset, source, sort, query_language)

    if not keywords:
        return {
            "input_text": _normalize_keywords_input(keywords_input),
            "keywords": [],
            "total": 0,
            "page_count": 0,
            "offset": offset,
            "source": source,
            "sort": sort,
            "module_code": module,
            "module_profile": get_module_definition(module),
            "query_language": query_language,
            "bilingual_query": keyword_bundle,
            "results": [],
            "grouped_results": {"legislation": [], "canlii": [], "ofac": [], "other": []},
            "source_counts": {"legislation": 0, "canlii": 0, "ofac": 0, "other": 0},
            "has_previous": False,
            "has_next": False,
            "previous_offset": 0,
            "next_offset": 0,
            "cache_status": "miss",
            "legal_skill_profile": skill_profile,
            "retrieval_entities": skill_profile.get("entities", {}),
            "legal_skills": skill_profile.get("skills", []),
            "module_packet": build_module_packet(
                module,
                {
                    "input_text": _normalize_keywords_input(keywords_input),
                    "module_code": module,
                    "query_language": query_language,
                    "results": [],
                    "analysis": {},
                    "intake_outline": {},
                },
                refresh=refresh,
            ),
        }

    if not refresh:
        cached = _get_cached_result(_SEARCH_CACHE, cache_key)
        if cached is not None:
            cached["cache_status"] = "hit"
            return cached

    fast_payload = _search_items_fast_rag(
        keywords_input=keywords_input,
        keywords=keywords,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        module=module,
        query_language=query_language,
        keyword_bundle=keyword_bundle,
        skill_profile=skill_profile,
    )
    if fast_payload is not None:
        _set_cached_result(_SEARCH_CACHE, cache_key, fast_payload)
        return fast_payload

    conditions = []
    score_parts = []
    params = {"limit": limit, "offset": offset}
    court_code_expr, court_level_expr = _court_level_sql()

    for index, keyword in enumerate(keywords):
        key = f"kw{index}"
        exact_key = f"kw_exact{index}"
        sim_key = f"kw_sim{index}"
        params[exact_key] = keyword.lower()
        params[key] = f"%{keyword.lower()}%"
        params[sim_key] = _similarity_threshold(keyword)
        try:
            weight = float((keyword_weights or {}).get(keyword, (keyword_weights or {}).get(keyword.lower(), 1.0)))
        except (TypeError, ValueError):
            weight = 1.0
        if is_sqlite():
            # SQLite: 只用 LIKE 匹配
            conditions.append(
                f"""
                LOWER(COALESCE(si.title, '')) LIKE :{key}
                OR LOWER(COALESCE(si.summary, '')) LIKE :{key}
                OR LOWER(COALESCE(si.raw_text, '')) LIKE :{key}
                OR EXISTS (
                    SELECT 1
                    FROM item_keywords ik
                    WHERE ik.item_id = si.id
                      AND LOWER(ik.keyword) LIKE :{key}
                )
                """
            )
            score_parts.append(
                f"""
                (CASE WHEN LOWER(COALESCE(si.title, '')) LIKE :{key} THEN 5 ELSE 0 END
                + CASE WHEN LOWER(COALESCE(si.summary, '')) LIKE :{key} THEN 3 ELSE 0 END
                + CASE WHEN LOWER(COALESCE(si.raw_text, '')) LIKE :{key} THEN 1 ELSE 0 END
                + CASE WHEN EXISTS (
                    SELECT 1
                    FROM item_keywords ik
                    WHERE ik.item_id = si.id
                      AND LOWER(ik.keyword) LIKE :{key}
                  ) THEN 2 ELSE 0 END) * {weight}
                """
            )
        else:
            # PostgreSQL: 使用 word_similarity
            conditions.append(
                f"""
                LOWER(COALESCE(si.title, '')) LIKE :{key}
                OR LOWER(COALESCE(si.summary, '')) LIKE :{key}
                OR LOWER(COALESCE(si.raw_text, '')) LIKE :{key}
                OR word_similarity(LOWER(COALESCE(si.title, '')), :{exact_key}) >= :{sim_key}
                OR EXISTS (
                    SELECT 1
                    FROM item_keywords ik
                    WHERE ik.item_id = si.id
                      AND (
                          LOWER(ik.keyword) LIKE :{key}
                          OR word_similarity(LOWER(ik.keyword), :{exact_key}) >= :{sim_key}
                      )
                )
                """
            )
            score_parts.append(
                f"""
                (CASE WHEN LOWER(COALESCE(si.title, '')) LIKE :{key} THEN 5 ELSE 0 END
                + CASE WHEN LOWER(COALESCE(si.summary, '')) LIKE :{key} THEN 3 ELSE 0 END
                + CASE WHEN LOWER(COALESCE(si.raw_text, '')) LIKE :{key} THEN 1 ELSE 0 END
                + CASE WHEN word_similarity(LOWER(COALESCE(si.title, '')), :{exact_key}) >= :{sim_key} THEN 2 ELSE 0 END
                + CASE WHEN EXISTS (
                    SELECT 1
                    FROM item_keywords ik
                    WHERE ik.item_id = si.id
                      AND (
                          LOWER(ik.keyword) LIKE :{key}
                          OR word_similarity(LOWER(ik.keyword), :{exact_key}) >= :{sim_key}
                      )
                ) THEN 2 ELSE 0 END) * {weight}
                """
            )

    source_clause = ""
    if source in {"ofac", "canlii"}:
        source_clause = "AND si.source_code = :source"
        params["source"] = source
    elif source == "canada":
        source_clause = f"AND {_source_code_in_clause(_CANADA_SOURCE_CODES, params, 'canada_source')}"

    where_clause = f"({' OR '.join(conditions)}) {source_clause}"
    score_expr = " + ".join(score_parts)
    order_clause = (
        "score DESC, court_level DESC, COALESCE(si.published_at, si.updated_at, si.created_at) DESC"
        if sort == "relevance"
        else "COALESCE(si.published_at, si.updated_at, si.created_at) DESC, score DESC, court_level DESC"
    )

    count_sql = f"""
    SELECT COUNT(*) AS total
    FROM source_items si
    WHERE {where_clause}
    """
    count_rows = fetch_all(count_sql, params)
    total_matches = int(count_rows[0]["total"]) if count_rows else 0

    source_count_sql = f"""
    SELECT si.source_code, COUNT(*) AS total
    FROM source_items si
    WHERE {where_clause}
    GROUP BY si.source_code
    """
    source_count_rows = fetch_all(source_count_sql, params)
    source_counts = {"legislation": 0, "canlii": 0, "ofac": 0, "other": 0}
    for row in source_count_rows:
        key = row.get("source_code")
        count = int(row.get("total") or 0)
        if key in _LEGISLATION_SOURCE_CODES:
            source_counts["legislation"] += count
        elif key in {"canlii", "manual_canada_case", "url_canada_case"}:
            source_counts["canlii"] += count
        elif key in source_counts:
            source_counts[key] = count
        else:
            source_counts["other"] += count

    sql = f"""
    SELECT
        si.id,
        si.source_code,
        si.source_uid,
        si.title,
        si.item_url,
        si.published_at,
        si.summary,
        si.raw_text,
        si.raw_json,
        si.created_at,
        si.updated_at,
        ({score_expr}) AS score,
        {court_code_expr} AS court_code,
        ({court_level_expr}) AS court_level
    FROM source_items si
    WHERE {where_clause}
    ORDER BY {order_clause}
    OFFSET :offset
    LIMIT :limit
    """

    rows = _decorate_result_rows(fetch_all(sql, params), len(keywords))
    for row in rows:
        row["query_language"] = query_language
    rows = enrich_result_rows_bilingual(rows, query_language=query_language)
    rows = apply_legal_result_verification(rows, skill_profile, sort=sort)
    grouped_results = _prepare_cards(rows, query_language)
    _insert_search_log(keywords_input, len(rows))

    previous_offset = max(offset - limit, 0)
    next_offset = offset + limit

    payload = {
        "input_text": _normalize_keywords_input(keywords_input),
        "keywords": keywords,
        "total": total_matches,
        "page_count": len(rows),
        "offset": offset,
        "source": source,
        "sort": sort,
        "module_code": module,
        "module_profile": get_module_definition(module),
        "query_language": query_language,
        "bilingual_query": keyword_bundle,
        "results": rows,
        "grouped_results": grouped_results,
        "source_counts": source_counts,
        "has_previous": offset > 0,
        "has_next": next_offset < total_matches,
        "previous_offset": previous_offset,
        "next_offset": next_offset,
        "cache_status": "miss",
        "legal_skill_profile": skill_profile,
        "retrieval_entities": skill_profile.get("entities", {}),
        "legal_skills": skill_profile.get("skills", []),
    }
    payload["module_packet"] = build_module_packet(module, payload, refresh=refresh)
    _set_cached_result(_SEARCH_CACHE, cache_key, payload)
    return payload



def _remote_search_threshold(limit: int) -> int:
    configured = max(1, int(getattr(settings, "remote_search_trigger_count", 1)))
    return max(limit, configured)



def _should_hydrate_remotely(
    result: dict,
    keywords: list[str],
    offset: int,
    limit: int,
    *,
    force_hydration: bool = False,
    hydration_target_count: int | None = None,
) -> bool:
    if not getattr(settings, "remote_search_enabled", True):
        return False
    if not getattr(settings, "async_hydration_enabled", True):
        return False
    if not keywords:
        return False
    if offset > 0:
        return False
    threshold = _remote_search_threshold(limit)
    if hydration_target_count:
        threshold = max(threshold, max(1, int(hydration_target_count)))
    if force_hydration:
        return True
    return int(result.get("total") or 0) < _remote_search_threshold(limit)


def _should_skip_remote_hydration_after_zero_result(result: dict, recent_terminal_task: dict | None) -> bool:
    if not recent_terminal_task:
        return False
    if not recent_terminal_task.get("is_terminal"):
        return False
    if str(recent_terminal_task.get("status") or "").strip().lower() != "completed":
        return False
    if int(result.get("total") or 0) > 0:
        return False
    return int(recent_terminal_task.get("result_total_after") or 0) <= 0


def search_with_remote_hydration(
    keywords_input: str | list[str],
    limit: int = 30,
    offset: int = 0,
    source: str = "all",
    sort: str = "relevance",
    module: str = "canada",
    refresh: bool = False,
    origin_page: str = "search",
    force_hydration: bool = False,
    hydration_target_count: int | None = None,
    hydration_reason: str = "",
    display_language: str | None = None,
    local_only: bool = False,
    keyword_weights: dict[str, float] | None = None,
):
    normalized_limit = _normalize_limit(limit)
    normalized_offset = _normalize_offset(offset)
    normalized_sort = _normalize_sort(sort)
    normalized_module = normalize_module(module)
    normalized_source = resolve_source_for_module(normalized_module, source)

    result = search_items(
        keywords_input,
        limit=normalized_limit,
        offset=normalized_offset,
        source=normalized_source,
        sort=normalized_sort,
        module=normalized_module,
        refresh=refresh,
        display_language=display_language,
        keyword_weights=keyword_weights,
    )

    remote_fetch = {
        "status": "local_only",
        "processed": 0,
        "sources": {},
        "message": "当前直接使用本地数据检索。",
    }
    search_strategy = "local_only"

    if not local_only and _should_hydrate_remotely(
        result,
        result.get("keywords", []),
        normalized_offset,
        normalized_limit,
        force_hydration=force_hydration,
        hydration_target_count=hydration_target_count,
    ):
        recent_terminal_task = get_recent_terminal_hydration_task(
            keywords=result.get("keywords", []),
            source_filter=normalized_source,
            desired_count=hydration_target_count or normalized_limit,
        )
        if _should_skip_remote_hydration_after_zero_result(result, recent_terminal_task):
            remote_fetch = {
                "status": "zero_result_cached",
                "processed": int(recent_terminal_task.get("items_processed") or 0),
                "sources": recent_terminal_task.get("sources", {}),
                "message": "同一组关键词最近已经补抓过且仍然没有结果，本次直接返回 0。",
            }
            search_strategy = "local_zero_result"
        else:
            task = enqueue_or_reuse_hydration_task(
                query_text=_normalize_keywords_input(keywords_input),
                keywords=result.get("keywords", []),
                source_filter=normalized_source,
                desired_count=hydration_target_count or normalized_limit,
                current_local_count=int(result.get("total") or 0),
                origin_page=origin_page,
            )
            if task:
                terminal_without_growth = (
                    task.get("is_terminal")
                    and int(task.get("result_total_after") or 0) <= int(result.get("total") or 0)
                )
                if force_hydration and hydration_reason == "new_case_enrichment":
                    remote_message = (
                        f"这是一条新案情。系统已在本地命中 {int(result.get('total') or 0)} 条结果，"
                        f"但仍会额外扩充外部资料，目标补到 {int(hydration_target_count or normalized_limit)} 条附近。"
                    )
                elif terminal_without_growth:
                    remote_message = f"当前只找到 {int(result.get('total') or 0)} 条可用结果，冷却期内不会重复补抓同一请求。"
                else:
                    remote_message = task.get("message") or "本地命中不足，已提交后台扩库任务，完成后页面会自动刷新。"
                remote_fetch = {
                    "status": task.get("status", "queued"),
                    "processed": int(task.get("items_processed") or 0),
                    "sources": task.get("sources", {}),
                    "message": remote_message,
                    "task": task,
                }
                search_strategy = "local_then_async_enrichment" if force_hydration else "local_then_async"

    payload = {
        **result,
        "module_code": normalized_module,
        "module_profile": get_module_definition(normalized_module),
        "remote_fetch": remote_fetch,
        "search_strategy": "local_only" if local_only else search_strategy,
        "hydration_target_count": hydration_target_count or normalized_limit,
        "force_hydration": bool(force_hydration),
        "hydration_reason": hydration_reason,
    }
    if local_only:
        payload["remote_fetch"] = {
            "status": "local_only",
            "processed": 0,
            "sources": {},
            "message": "当前仅使用本地数据库匹配，不会实时访问远程网页。",
        }
    return payload


def _merge_local_and_realtime(local_result: dict, canlii_realtime: dict) -> dict:
    """Merge local DB results with real-time CanLII results."""
    realtime_items = canlii_realtime.get("items") or []
    if not realtime_items:
        return local_result

    existing_urls = {
        (row.get("item_url") or row.get("url") or "").rstrip("/")
        for row in local_result.get("results", [])
    }

    new_canlii_cards = []
    for item in realtime_items:
        url = (item.get("url") or "").rstrip("/")
        if url and url in existing_urls:
            continue
        existing_urls.add(url)
        new_canlii_cards.append({
            "id": f"rt_{hash(url) % 10**8}",
            "source_code": "canlii",
            "source_label": "Case (Real-time)",
            "title": repair_text(item.get("title", "")),
            "title_primary": repair_text(item.get("title", "")),
            "title_secondary": "",
            "subtitle": repair_text(item.get("citation", "")),
            "published_at": _fmt_dt(item.get("date", "")),
            "summary": _clip(item.get("summary", ""), 260),
            "summary_primary": _clip(item.get("summary", ""), 260),
            "summary_secondary": "",
            "excerpt": "",
            "url": item.get("url", ""),
            "source_url": item.get("database", ""),
            "score": 0.5,
            "fields": [],
            "realtime_source": item.get("source", "canlii"),
        })

    result = dict(local_result)
    result["results"] = list(local_result.get("results", [])) + new_canlii_cards

    grouped = dict(result.get("grouped_results", {}))
    grouped["canlii"] = list(grouped.get("canlii", [])) + new_canlii_cards
    result["grouped_results"] = grouped

    result["total"] = int(result.get("total") or 0) + len(new_canlii_cards)
    source_counts = dict(result.get("source_counts", {}))
    source_counts["canlii"] = int(source_counts.get("canlii") or 0) + len(new_canlii_cards)
    result["source_counts"] = source_counts

    return result


def search_with_canlii_realtime(
    keywords_input: str | list[str],
    limit: int = 30,
    offset: int = 0,
    source: str = "all",
    sort: str = "relevance",
    module: str = "canada",
    refresh: bool = False,
    display_language: str | None = None,
    keyword_weights: dict[str, float] | None = None,
) -> dict:
    """
    Combined search: local DB + real-time CanLII API/RSS.
    Returns merged results with source attribution.
    """
    local_result = search_items(
        keywords_input,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        module=module,
        refresh=refresh,
        display_language=display_language,
        keyword_weights=keyword_weights,
    )

    canlii_realtime = {"items": [], "method": "none", "count": 0}
    keywords = local_result.get("keywords") or split_keywords(keywords_input)

    if (
        keywords
        and getattr(settings, "canlii_realtime_search_enabled", True)
        and module in ("canada", "all")
        and source in ("all", "canlii", "canada")
    ):
        from app.service.canlii_service import search_canlii_by_keywords_realtime
        max_realtime = max(5, int(getattr(settings, "canlii_realtime_search_max_items", 20)))
        canlii_realtime = search_canlii_by_keywords_realtime(
            keywords,
            max_items=max_realtime,
            use_api=bool(settings.canlii_api_key),
            use_rss=True,
        )

    merged_result = _merge_local_and_realtime(local_result, canlii_realtime)
    merged_result["canlii_realtime"] = {
        "method": canlii_realtime.get("method", "none"),
        "count": canlii_realtime.get("count", 0),
    }
    merged_result["search_strategy"] = "local_plus_realtime"

    return merged_result



def search_and_optionally_sync(
    keywords_input: str | list[str],
    sync_first: bool = False,
    limit: int = 30,
    offset: int = 0,
    source: str = "all",
    sort: str = "relevance",
    module: str = "canada",
    refresh: bool = False,
    origin_page: str = "search",
    display_language: str | None = None,
):
    sync_info = None
    keyword_weights = None
    normalized_limit = _normalize_limit(limit)
    normalized_offset = _normalize_offset(offset)
    normalized_sort = _normalize_sort(sort)
    normalized_module = normalize_module(module)
    normalized_source = resolve_source_for_module(normalized_module, source)

    if sync_first:
        sync_info = sync_all_sources()
        clear_search_cache()
        result = search_items(
            keywords_input,
            limit=normalized_limit,
            offset=normalized_offset,
            source=normalized_source,
            sort=normalized_sort,
            module=normalized_module,
            refresh=True,
            display_language=display_language,
            keyword_weights=keyword_weights,
        )
        result["remote_fetch"] = {
            "status": "manual_sync",
            "processed": 0,
            "sources": sync_info.get("sources", {}),
            "message": "已执行手动同步，当前结果来自同步后的本地库。",
        }
        result["search_strategy"] = "manual_sync_then_local"
    else:
        result = search_with_remote_hydration(
            keywords_input,
            limit=normalized_limit,
            offset=normalized_offset,
            source=normalized_source,
            sort=normalized_sort,
            module=normalized_module,
            refresh=refresh,
            origin_page=origin_page,
            display_language=display_language,
        )

    return {
        "keywords_input": _normalize_keywords_input(keywords_input),
        "limit": normalized_limit,
        "offset": normalized_offset,
        "source": normalized_source,
        "sort": normalized_sort,
        "module_code": normalized_module,
        "module_profile": get_module_definition(normalized_module),
        "sync_info": sync_info,
        **result,
    }
