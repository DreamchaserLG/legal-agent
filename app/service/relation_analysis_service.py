from __future__ import annotations

"""
关系分析服务 - 分析案例与法规之间的关系
"""

import re
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

from sqlalchemy import text

from app.core.database import engine
from app.service.common_service import repair_text


# 法律名称匹配模式
LAW_NAME_PATTERNS = [
    # 匹配 "XXX Act" 格式
    r'\b([A-Z][A-Za-z\s]+(?:Act|Code|Regulation|Rule|Charter|Convention))\b',
    # 匹配 "R.S.C., c. X" 格式的引用
    r'\b(R\.S\.C\.,\s*\d{4},\s*c\.\s*[A-Z0-9]+)\b',
    # 匹配 "S.C. c. X" 格式的引用
    r'\b(S\.C\.\s*\d{4},\s*c\.\s*[A-Z0-9]+)\b',
    # 匹配 "S.O. c. X" 格式的引用
    r'\b(S\.O\.\s*\d{4},\s*c\.\s*[A-Z0-9]+)\b',
    # 匹配 "s. X" 或 "section X" 格式
    r'\b(?:s\.|section)\s*(\d+(?:\.\d+)?)\b',
    # 匹配常见法规缩写
    r'\b(Criminal Code|Criminal Code of Canada|CC|CPC|CRA|PIPEDA|CBCA|OBCA)\b',
]

# 法律主题到法规的映射 (从RSS描述中推断)
TOPIC_TO_LAW_PATTERNS = {
    r'\bfraud\b|\bfraudulent\b|\bactus reus\b|\bdeprivation\b': 'Criminal Code',
    r'\btheft\b|\bstealing\b|\bstolen\b': 'Criminal Code',
    r'\bassault\b|\bthreatening\b': 'Criminal Code',
    r'\bdrug\b|\bnarcotics\b|\bcontrolled substance\b': 'Criminal Code',
    r'\bmurder\b|\bhomicide\b': 'Criminal Code',
    r'\bsexual assault\b': 'Criminal Code',
    r'\bmisrepresentation\b|\bdeceit\b|\bdishonesty\b': 'Criminal Code',
    r'\bsecurities\b|\binvestor\b|\bdisclosure\b': 'Ontario Securities Act',
    r'\bbankruptcy\b|\binsolvency\b|\bcreditor\b': 'Bankruptcy and Insolvency Act',
    r'\bemployment\b|\bwrongful dismissal\b|\btermination\b|\bseverance\b': 'Employment Standards Act',
    r'\bdivorce\b|\bcustody\b|\bspousal\b': 'Divorce Act',
    r'\blandlord\b|\btenant\b|\beviction\b|\brent\b': 'Residential Tenancies Act',
    r'\bimmigration\b|\brefugee\b|\bdeportation\b': 'Immigration and Refugee Protection Act',
    r'\btrademark\b|\bpatent\b|\bcopyright\b': 'Trade-marks Act',
    r'\btort\b|\bnegligence\b|\bduty of care\b': 'Negligence Act',
    r'\bcontract\b|\bbreach\b|\bconsideration\b': 'Sale of Goods Act',
    r'\bcharter\b|\bsection 7\b|\bsection 11\b|\bsection 15\b': 'Canadian Charter of Rights and Freedoms',
    r'\breal estate\b|\bproperty\b|\bmortgage\b|\bconveyance\b': 'Land Titles Act',
}

# 关系类型定义
RELATION_TYPES = {
    "cited": {
        "label": "引用",
        "label_en": "Cited",
        "description": "案例中引用了该法规",
        "weight": 1.0,
    },
    "applied": {
        "label": "适用",
        "label_en": "Applied",
        "description": "案例中适用了该法规",
        "weight": 0.9,
    },
    "explained": {
        "label": "解释",
        "label_en": "Explained",
        "description": "案例中解释了该法规的含义",
        "weight": 0.8,
    },
    "distinguished": {
        "label": "区分",
        "label_en": "Distinguished",
        "description": "案例中区分了该法规的适用范围",
        "weight": 0.7,
    },
    "related": {
        "label": "相关",
        "label_en": "Related",
        "description": "案例与法规相关",
        "weight": 0.6,
    },
}


def extract_law_references(text: str) -> List[Dict]:
    """从文本中提取法律引用"""
    references = []
    seen = set()

    # 1. 正则匹配明确的法规名称
    for pattern in LAW_NAME_PATTERNS:
        matches = re.finditer(pattern, text, re.IGNORECASE)
        for match in matches:
            ref_text = match.group(1).strip()
            if ref_text and ref_text not in seen:
                seen.add(ref_text)
                references.append({
                    "text": ref_text,
                    "start": match.start(),
                    "end": match.end(),
                    "context": text[max(0, match.start() - 50):min(len(text), match.end() + 50)],
                    "source": "regex",
                })

    # 2. 法律主题推断
    for topic_pattern, law_name in TOPIC_TO_LAW_PATTERNS.items():
        if law_name in seen:
            continue
        if re.search(topic_pattern, text, re.IGNORECASE):
            seen.add(law_name)
            references.append({
                "text": law_name,
                "start": 0,
                "end": 0,
                "context": text[:200],
                "source": "topic_inference",
            })

    return references


def analyze_case_law_relations(case_text: str, case_id: int) -> List[Dict]:
    """分析案例与法规的关系"""
    relations = []

    # 提取法律引用
    references = extract_law_references(case_text)

    for ref in references:
        # 确定关系类型
        relation_type = _determine_relation_type(ref["context"])

        # 计算匹配分数
        match_score = _calculate_match_score(ref, relation_type)

        relations.append({
            "case_id": case_id,
            "law_reference": ref["text"],
            "relation_type": relation_type,
            "match_score": match_score,
            "evidence_excerpt": ref["context"],
            "match_reason": f"在案例文本中发现对 {ref['text']} 的引用",
        })

    return relations


def _determine_relation_type(context: str) -> str:
    """根据上下文确定关系类型"""
    context_lower = context.lower()

    # 检查是否是"适用"关系
    apply_keywords = ["applied", "applies", "applying", "under", "pursuant to", "根据", "依据", "适用"]
    if any(keyword in context_lower for keyword in apply_keywords):
        return "applied"

    # 检查是否是"解释"关系
    explain_keywords = ["interpreted", "interprets", "interpreting", "解释", "释义"]
    if any(keyword in context_lower for keyword in explain_keywords):
        return "explained"

    # 检查是否是"区分"关系
    distinguish_keywords = ["distinguished", "distinguishes", "区分", "区别"]
    if any(keyword in context_lower for keyword in distinguish_keywords):
        return "distinguished"

    # 默认为"引用"关系
    return "cited"


def _calculate_match_score(ref: Dict, relation_type: str) -> float:
    """计算匹配分数"""
    base_score = RELATION_TYPES[relation_type]["weight"]

    # 根据引用文本长度调整分数
    text_length = len(ref["text"])
    if text_length > 20:
        base_score += 0.05
    if text_length > 40:
        base_score += 0.05

    return min(1.0, base_score)


def store_relations(relations: List[Dict]) -> int:
    """存储关系到数据库"""
    stored_count = 0

    with engine.begin() as conn:
        for relation in relations:
            # 查找法规ID
            law_id = _find_law_id(conn, relation["law_reference"])
            if not law_id:
                continue

            # 存储关系
            conn.execute(
                text("""
                    INSERT INTO canada_case_law_links (
                        case_item_id,
                        law_id,
                        matched_alias,
                        match_source,
                        match_score,
                        evidence_excerpt,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        :case_item_id,
                        :law_id,
                        :matched_alias,
                        :match_source,
                        :match_score,
                        :evidence_excerpt,
                        CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (case_item_id, law_id)
                    DO UPDATE SET
                        matched_alias = EXCLUDED.matched_alias,
                        match_source = EXCLUDED.match_source,
                        match_score = EXCLUDED.match_score,
                        evidence_excerpt = EXCLUDED.evidence_excerpt,
                        updated_at = CURRENT_TIMESTAMP
                """),
                {
                    "case_item_id": relation["case_id"],
                    "law_id": law_id,
                    "matched_alias": relation["law_reference"],
                    "match_source": relation["relation_type"],
                    "match_score": relation["match_score"],
                    "evidence_excerpt": relation["evidence_excerpt"],
                }
            )
            stored_count += 1

    return stored_count


def _find_law_id(conn, law_reference: str) -> Optional[int]:
    """查找法规ID"""
    # 首先尝试精确匹配
    result = conn.execute(
        text("""
            SELECT id FROM canada_laws
            WHERE LOWER(title) = LOWER(:reference)
               OR LOWER(citation) = LOWER(:reference)
            LIMIT 1
        """),
        {"reference": law_reference}
    ).scalar()

    if result:
        return result

    # 尝试模糊匹配
    result = conn.execute(
        text("""
            SELECT id FROM canada_laws
            WHERE LOWER(title) LIKE LOWER(:pattern)
               OR LOWER(citation) LIKE LOWER(:pattern)
            LIMIT 1
        """),
        {"pattern": f"%{law_reference}%"}
    ).scalar()

    return result


def analyze_all_cases() -> Dict:
    """分析所有案例与法规的关系"""
    stats = {
        "cases_analyzed": 0,
        "relations_found": 0,
        "relations_stored": 0,
    }

    with engine.connect() as conn:
        # 获取所有案例 (包含summary/RSS描述)
        cases = conn.execute(
            text("""
                SELECT id, title, summary, raw_text
                FROM source_items
                WHERE source_code = 'canlii'
            """)
        ).fetchall()

        for case in cases:
            case_id = case[0]
            case_text = f"{case[1]} {case[2]} {case[3]}"

            # 分析关系
            relations = analyze_case_law_relations(case_text, case_id)
            stats["relations_found"] += len(relations)

            # 存储关系
            if relations:
                stored = store_relations(relations)
                stats["relations_stored"] += stored

            stats["cases_analyzed"] += 1

    return stats


def get_relation_stats() -> Dict:
    """获取关系统计"""
    with engine.connect() as conn:
        # 统计关系数量
        total_relations = conn.execute(
            text("SELECT COUNT(*) FROM canada_case_law_links")
        ).scalar()

        # 按关系类型统计
        type_stats = conn.execute(
            text("""
                SELECT match_source, COUNT(*) as count
                FROM canada_case_law_links
                GROUP BY match_source
            """)
        ).fetchall()

        # 按法规统计
        law_stats = conn.execute(
            text("""
                SELECT l.title, COUNT(*) as case_count
                FROM canada_case_law_links cl
                JOIN canada_laws l ON l.id = cl.law_id
                GROUP BY l.title
                ORDER BY case_count DESC
                LIMIT 10
            """)
        ).fetchall()

    return {
        "total_relations": total_relations,
        "by_type": {row[0]: row[1] for row in type_stats},
        "top_laws": [{"title": row[0], "case_count": row[1]} for row in law_stats],
    }
