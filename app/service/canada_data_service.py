from __future__ import annotations

"""
加拿大法律数据服务 - 支持多种数据源
"""

import json
import re
import time
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine
from app.service.common_service import repair_text, sha256_text, upsert_source_item
from app.service.data_source_service import get_current_legislation_source, get_current_case_source


# 演示数据 - 加拿大联邦法规
DEMO_FEDERAL_ACTS = [
    {
        "title": "Criminal Code",
        "citation": "R.S.C., 1985, c. C-46",
        "source_url": "https://laws-lois.justice.gc.ca/eng/acts/C-46/",
        "summary": "The Criminal Code of Canada is the main federal statute dealing with criminal law in Canada.",
        "keywords": ["criminal", "code", "offence", "crime", "penalty"],
    },
    {
        "title": "Canadian Charter of Rights and Freedoms",
        "citation": "Part I of the Constitution Act, 1982",
        "source_url": "https://laws-lois.justice.gc.ca/eng/const/page-12.html",
        "summary": "The Canadian Charter of Rights and Freedoms is a bill of rights entrenched in the Constitution of Canada.",
        "keywords": ["charter", "rights", "freedoms", "constitution"],
    },
    {
        "title": "Divorce Act",
        "citation": "R.S.C., 1985, c. 3 (2nd Supp.)",
        "source_url": "https://laws-lois.justice.gc.ca/eng/acts/D-3.4/",
        "summary": "An Act respecting divorce and corollary relief.",
        "keywords": ["divorce", "marriage", "custody", "support"],
    },
    {
        "title": "Income Tax Act",
        "citation": "R.S.C., 1985, c. 1 (5th Supp.)",
        "source_url": "https://laws-lois.justice.gc.ca/eng/acts/I-3.3/",
        "summary": "An Act respecting the income tax.",
        "keywords": ["income", "tax", "taxation", "revenue"],
    },
    {
        "title": "Canada Labour Code",
        "citation": "R.S.C., 1985, c. L-2",
        "source_url": "https://laws-lois.justice.gc.ca/eng/acts/L-2/",
        "summary": "An Act to consolidate certain statutes respecting labour.",
        "keywords": ["labour", "labor", "employment", "workplace"],
    },
]

# 演示数据 - 安大略省法规
DEMO_ONTARIO_STATUTES = [
    {
        "title": "Residential Tenancies Act, 2006",
        "citation": "S.O. 2006, c. 17",
        "source_url": "https://www.ontario.ca/laws/statute/06r17",
        "summary": "An Act respecting residential tenancies.",
        "keywords": ["residential", "tenancy", "landlord", "tenant", "rent"],
    },
    {
        "title": "Employment Standards Act, 2000",
        "citation": "S.O. 2000, c. 41",
        "source_url": "https://www.ontario.ca/laws/statute/00e41",
        "summary": "An Act to establish minimum standards for employees.",
        "keywords": ["employment", "standards", "wages", "hours"],
    },
    {
        "title": "Occupiers' Liability Act",
        "citation": "R.S.O. 1990, c. O.2",
        "source_url": "https://www.ontario.ca/laws/statute/90o02",
        "summary": "An Act respecting the liability of occupiers of premises.",
        "keywords": ["occupier", "liability", "premises", "negligence"],
    },
    {
        "title": "Consumer Protection Act, 2002",
        "citation": "S.O. 2002, c. 30, Sched. A",
        "source_url": "https://www.ontario.ca/laws/statute/02c30",
        "summary": "An Act to protect consumers.",
        "keywords": ["consumer", "protection", "unfair", "practices"],
    },
    {
        "title": "Family Law Act",
        "citation": "R.S.O. 1990, c. F.3",
        "source_url": "https://www.ontario.ca/laws/statute/90f03",
        "summary": "An Act respecting family law.",
        "keywords": ["family", "law", "marriage", "support", "property"],
    },
]

# 演示数据 - 加拿大案例
DEMO_CANADA_CASES = [
    {
        "title": "R. v. Jordan",
        "court_name": "Supreme Court of Canada",
        "court_level": "Supreme Court",
        "case_type": "Criminal",
        "summary": "The Supreme Court of Canada established new frameworks for determining unreasonable delay under section 11(b) of the Charter.",
        "facts": "The accused was charged with multiple drug offences. The case took over 5 years to reach trial.",
        "judgment_result": "Charges stayed due to unreasonable delay.",
        "judgment_date": "2016-07-08",
        "source_url": "https://www.canlii.org/en/ca/scc/doc/2016/2016scc27/2016scc27.html",
        "related_laws": ["Canadian Charter of Rights and Freedoms"],
    },
    {
        "title": "R. v. Oakes",
        "court_name": "Supreme Court of Canada",
        "court_level": "Supreme Court",
        "case_type": "Criminal",
        "summary": "The Supreme Court established the Oakes test for determining whether limits on Charter rights are justified under section 1.",
        "facts": "The accused was charged with possession of a narcotic. The reverse onus provision was challenged.",
        "judgment_result": "The provision was found to violate section 11(d) and was not saved by section 1.",
        "judgment_date": "1986-02-28",
        "source_url": "https://www.canlii.org/en/ca/scc/doc/1986/1986canlii46/1986canlii46.html",
        "related_laws": ["Canadian Charter of Rights and Freedoms"],
    },
    {
        "title": "Matthws v. Ocean Nutrition Canada Ltd.",
        "court_name": "Supreme Court of Canada",
        "court_level": "Supreme Court",
        "case_type": "Employment",
        "summary": "The Supreme Court addressed the reasonable notice period and bonus entitlement upon termination.",
        "facts": "A senior employee was constructively dismissed and claimed bonus entitlement during the notice period.",
        "judgment_result": "Employee entitled to bonus during reasonable notice period.",
        "judgment_date": "2020-10-09",
        "source_url": "https://www.canlii.org/en/ca/scc/doc/2020/2020scc26/2020scc26.html",
        "related_laws": ["Employment Standards Act, 2000"],
    },
    {
        "title": "Bhasin v. Hrynew",
        "court_name": "Supreme Court of Canada",
        "court_level": "Supreme Court",
        "case_type": "Contract",
        "summary": "The Supreme Court recognized a general organizing principle of good faith in contract law.",
        "facts": "A commercial agent challenged the non-renewal of his agreement.",
        "judgment_result": "Damages awarded for breach of duty of good faith.",
        "judgment_date": "2014-11-13",
        "source_url": "https://www.canlii.org/en/ca/scc/doc/2014/2014scc71/2014scc71.html",
        "related_laws": [],
    },
    {
        "title": "Mustapha v. Culligan of Canada Ltd.",
        "court_name": "Supreme Court of Canada",
        "court_level": "Supreme Court",
        "case_type": "Negligence",
        "summary": "The Supreme Court addressed the standard of foreseeability in negligence claims for psychological injury.",
        "facts": "The plaintiff discovered a dead fly in a bottle of water and suffered psychological injury.",
        "judgment_result": "Claim dismissed - injury was not reasonably foreseeable.",
        "judgment_date": "2008-10-17",
        "source_url": "https://www.canlii.org/en/ca/scc/doc/2008/2008scc27/2008scc27.html",
        "related_laws": ["Occupiers' Liability Act"],
    },
    {
        "title": "Douez v. Facebook, Inc.",
        "court_name": "Supreme Court of Canada",
        "court_level": "Supreme Court",
        "case_type": "Privacy",
        "summary": "The Supreme Court addressed the enforceability of forum selection clauses in consumer contracts.",
        "facts": "A BC resident sued Facebook for privacy violations. Facebook relied on a forum selection clause.",
        "judgment_result": "Forum selection clause not enforceable in this case.",
        "judgment_date": "2017-06-23",
        "source_url": "https://www.canlii.org/en/ca/scc/doc/2017/2017scc33/2017scc33.html",
        "related_laws": [],
    },
]


def _slugify(value: str) -> str:
    """生成URL友好的slug"""
    slug = re.sub(r"[^a-z0-9]+", "-", repair_text(value).lower()).strip("-")
    return slug[:220] or f"law-{int(time.time())}"


def _store_demo_law(law_data: Dict, source_code: str) -> int:
    """存储演示法规数据"""
    title = repair_text(law_data["title"])
    normalized_title = title.lower()
    slug = _slugify(title)

    # 存储到source_items
    item_id = upsert_source_item(
        source_code=source_code,
        source_uid=sha256_text(title + "|" + source_code),
        title=title,
        item_url=law_data.get("source_url", ""),
        published_at=None,
        summary=law_data.get("summary", "")[:1000],
        raw_text=law_data.get("summary", ""),
        raw_json={
            "citation": law_data.get("citation", ""),
            "keywords": law_data.get("keywords", []),
            "source_type": "demo",
        },
    )
    return item_id


def _store_demo_case(case_data: Dict) -> int:
    """存储演示案例数据"""
    title = repair_text(case_data["title"])
    source_uid = sha256_text(title + "|" + case_data.get("judgment_date", ""))

    # 存储到source_items
    item_id = upsert_source_item(
        source_code="canlii",
        source_uid=source_uid,
        title=title,
        item_url=case_data.get("source_url", ""),
        published_at=case_data.get("judgment_date"),
        summary=case_data.get("summary", "")[:1000],
        raw_text=case_data.get("facts", "") + " " + case_data.get("summary", ""),
        raw_json={
            "court_name": case_data.get("court_name", ""),
            "court_level": case_data.get("court_level", ""),
            "case_type": case_data.get("case_type", ""),
            "facts": case_data.get("facts", ""),
            "judgment_result": case_data.get("judgment_result", ""),
            "related_laws": case_data.get("related_laws", []),
            "source_type": "demo",
        },
    )
    return item_id


def sync_demo_data() -> Dict:
    """同步演示数据"""
    stats = {
        "federal_acts": 0,
        "ontario_statutes": 0,
        "cases": 0,
        "relations": 0,
    }

    # 存储联邦法规
    for law in DEMO_FEDERAL_ACTS:
        _store_demo_law(law, "ca_federal_act")
        stats["federal_acts"] += 1

    # 存储安大略省法规
    for law in DEMO_ONTARIO_STATUTES:
        _store_demo_law(law, "on_statute")
        stats["ontario_statutes"] += 1

    # 存储案例
    for case in DEMO_CANADA_CASES:
        _store_demo_case(case)
        stats["cases"] += 1

    return stats


def sync_canada_data() -> Dict:
    """根据配置同步加拿大数据"""
    legislation_source = get_current_legislation_source()
    case_source = get_current_case_source()

    if legislation_source == "demo" or case_source == "demo":
        return sync_demo_data()

    # 其他数据源的同步逻辑
    # TODO: 实现其他数据源的同步
    return {"error": "Unsupported data source"}


def get_canada_data_stats() -> Dict:
    """获取加拿大数据统计"""
    with engine.connect() as conn:
        # 统计法规数量
        laws_count = conn.execute(
            text("SELECT COUNT(*) FROM source_items WHERE source_code IN ('ca_federal_act', 'ca_federal_regulation', 'on_statute', 'on_regulation')")
        ).scalar()

        # 统计案例数量
        cases_count = conn.execute(
            text("SELECT COUNT(*) FROM source_items WHERE source_code = 'canlii'")
        ).scalar()

        # 统计关系数量
        relations_count = conn.execute(
            text("SELECT COUNT(*) FROM canada_case_law_links")
        ).scalar()

    return {
        "laws_count": laws_count,
        "cases_count": cases_count,
        "relations_count": relations_count,
    }
