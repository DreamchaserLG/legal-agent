"""Court name normalisation for Canadian courts and tribunals.

``rag_chunks.court_level`` mixes full names, abbreviations and the occasional
dirty value (1,047 distinct strings for roughly 60 real institutions). The
retrieval filter needs one canonical key per court so that:

* the facet list shows "Supreme Court of Canada 264,003" instead of
  "Supreme Court of Canada" and "SCC" as two unrelated entries;
* selecting one court matches every spelling stored in the table.

The raw value is never rewritten — mapping happens at read time.
"""

from __future__ import annotations

# code: (English label, Chinese label, tier, aliases)
COURTS: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    # ---- apex / federal ----
    "scc": ("Supreme Court of Canada", "加拿大最高法院", "apex", ("Supreme Court of Canada", "SCC")),
    "fca": ("Federal Court of Appeal", "联邦上诉法院", "appeal", ("Federal Court of Appeal", "FCA")),
    "fc": ("Federal Court", "联邦法院", "superior", ("Federal Court", "FC")),
    "tcc": ("Tax Court of Canada", "加拿大税务法院", "superior", ("Tax Court of Canada", "TCC")),
    # ---- Ontario ----
    "onca": ("Ontario Court of Appeal", "安大略省上诉法院", "appeal", ("Ontario Court of Appeal", "ONCA")),
    "onsc": ("Ontario Superior Court of Justice", "安大略省高等法院", "superior", ("Ontario Superior Court of Justice", "ONSC")),
    "oncj": ("Ontario Court of Justice", "安大略省法院", "inferior", ("Ontario Court of Justice", "OCJ", "ONCJ")),
    # ---- British Columbia ----
    "bcca": ("British Columbia Court of Appeal", "不列颠哥伦比亚省上诉法院", "appeal", ("British Columbia Court of Appeal", "BCCA")),
    "bcsc": ("Supreme Court of British Columbia", "不列颠哥伦比亚省最高法院", "superior", ("Supreme Court of British Columbia", "BCSC")),
    "bcpc": ("Provincial Court of British Columbia", "不列颠哥伦比亚省法院", "inferior", ("Provincial Court of British Columbia", "BCPC")),
    # ---- Alberta ----
    "abca": ("Alberta Court of Appeal", "艾伯塔省上诉法院", "appeal", ("Alberta Court of Appeal", "ABCA")),
    "abqb": ("Court of King's Bench of Alberta", "艾伯塔省王座法院", "superior", ("Court of King's Bench of Alberta", "Court of Queen's Bench of Alberta", "ABQB")),
    # ---- Saskatchewan / Manitoba ----
    "skca": ("Saskatchewan Court of Appeal", "萨斯喀彻温省上诉法院", "appeal", ("Saskatchewan Court of Appeal", "SKCA")),
    "skqb": ("Court of King's Bench for Saskatchewan", "萨斯喀彻温省王座法院", "superior", ("Court of King's Bench for Saskatchewan", "SKQB")),
    "mbca": ("Manitoba Court of Appeal", "马尼托巴省上诉法院", "appeal", ("Manitoba Court of Appeal", "MBCA")),
    "mbqb": ("Court of King's Bench of Manitoba", "马尼托巴省王座法院", "superior", ("Court of King's Bench of Manitoba", "MBQB")),
    # ---- Quebec ----
    "qcca": ("Quebec Court of Appeal", "魁北克省上诉法院", "appeal", ("Quebec Court of Appeal", "Cour d'appel du Québec", "QCCA")),
    "qccs": ("Superior Court of Quebec", "魁北克省高等法院", "superior", ("Superior Court of Quebec", "Cour supérieure du Québec", "QCCS")),
    # ---- Atlantic ----
    "nsca": ("Nova Scotia Court of Appeal", "新斯科舍省上诉法院", "appeal", ("Nova Scotia Court of Appeal", "NSCA")),
    "nssc": ("Nova Scotia Supreme Court", "新斯科舍省最高法院", "superior", ("Nova Scotia Supreme Court", "NSSC")),
    "nspc": ("Nova Scotia Provincial Court", "新斯科舍省法院", "inferior", ("Nova Scotia Provincial Court", "NSPC")),
    "nssm": ("Nova Scotia Small Claims Court", "新斯科舍省小额索赔法院", "inferior", ("Nova Scotia Small Claims Court", "NSSM")),
    "nsfc": ("Nova Scotia Family Court", "新斯科舍省家庭法院", "inferior", ("Nova Scotia Family Court", "NSFC")),
    "nbca": ("New Brunswick Court of Appeal", "新不伦瑞克省上诉法院", "appeal", ("New Brunswick Court of Appeal", "NBCA")),
    "nbqb": ("Court of King's Bench of New Brunswick", "新不伦瑞克省王座法院", "superior", ("Court of King's Bench of New Brunswick", "NBQB")),
    "peica": ("Prince Edward Island Court of Appeal", "爱德华王子岛上诉法院", "appeal", ("Prince Edward Island Court of Appeal", "PECA", "PEICA")),
    "nlca": ("Newfoundland and Labrador Court of Appeal", "纽芬兰与拉布拉多上诉法院", "appeal", ("Newfoundland and Labrador Court of Appeal", "NLCA")),
    # ---- territories ----
    "ykca": ("Yukon Court of Appeal", "育空上诉法院", "appeal", ("Yukon Court of Appeal", "YKCA")),
    "nwtca": ("Northwest Territories Court of Appeal", "西北地区上诉法院", "appeal", ("Northwest Territories Court of Appeal", "NWTCA")),
    "nuca": ("Nunavut Court of Appeal", "努纳武特上诉法院", "appeal", ("Nunavut Court of Appeal", "NUCA")),
    # ---- federal boards and tribunals ----
    "sst": ("Social Security Tribunal", "社会保障仲裁庭", "tribunal", ("Social Security Tribunal", "SST")),
    "rad": ("Refugee Appeal Division", "难民上诉庭", "tribunal", ("Refugee Appeal Division", "RAD")),
    "rpd": ("Refugee Protection Division", "难民保护庭", "tribunal", ("Refugee Protection Division", "RPD")),
    "fpslreb": ("Federal Public Sector Labour Relations and Employment Board", "联邦公共部门劳资关系与就业委员会", "tribunal", ("FPSLREB", "Federal Public Sector Labour Relations and Employment Board")),
    "citt": ("Canadian International Trade Tribunal", "加拿大国际贸易法庭", "tribunal", ("Canadian International Trade Tribunal", "CITT")),
    "cirb": ("Canada Industrial Relations Board", "加拿大劳资关系委员会", "tribunal", ("Canada Industrial Relations Board", "CIRB")),
    "chrt": ("Canadian Human Rights Tribunal", "加拿大人权法庭", "tribunal", ("Canadian Human Rights Tribunal", "CHRT")),
    "ohstc": ("Occupational Health and Safety Tribunal Canada", "加拿大职业健康与安全法庭", "tribunal", ("Occupational Health and Safety Tribunal Canada", "OHSTC")),
    "tatc": ("Transportation Appeal Tribunal of Canada", "加拿大运输上诉法庭", "tribunal", ("Transportation Appeal Tribunal of Canada", "TATC")),
    "cart": ("Canada Agricultural Review Tribunal", "加拿大农业复议法庭", "tribunal", ("Canada Agricultural Review Tribunal", "CART")),
    "cmac": ("Canada Media Access Certification", "加拿大媒体访问认证机构", "tribunal", ("CMAC",)),
}

_ALIAS_INDEX: dict[str, str] = {}
for _code, (_en, _zh, _tier, _aliases) in COURTS.items():
    for _alias in _aliases:
        _ALIAS_INDEX[_alias.strip().lower()] = _code
    _ALIAS_INDEX[_code] = _code


def resolve_court(value: str | None) -> str | None:
    """Return the canonical court code for a raw value, or ``None``."""
    raw = (value or "").strip()
    if not raw:
        return None
    return _ALIAS_INDEX.get(raw.lower())


def court_label(code: str, lang: str = "en") -> str:
    entry = COURTS.get(code)
    if not entry:
        return code
    return entry[1] if lang == "zh" else entry[0]


def court_aliases(code: str) -> list[str]:
    """All raw spellings that map to ``code`` (used for IN filtering)."""
    entry = COURTS.get(code)
    if not entry:
        return [code]
    return list(entry[3])


def normalize_court_list(rows: list[dict], lang: str = "en") -> list[dict]:
    """Merge raw ``court_level`` counts into canonical court entries.

    ``rows`` items must provide ``value`` (raw string) and ``count``.
    Unknown values are kept as-is so no data silently disappears.
    """
    merged: dict[str, dict] = {}
    for row in rows:
        raw = str(row.get("value") or "").strip()
        if not raw:
            continue
        code = resolve_court(raw)
        if code:
            entry = COURTS[code]
            bucket = merged.setdefault(
                code,
                {
                    "code": code,
                    "value": code,
                    "label": entry[1] if lang == "zh" else entry[0],
                    "label_en": entry[0],
                    "label_zh": entry[1],
                    "tier": entry[2],
                    "count": 0,
                },
            )
        else:
            # keep unmapped values visible rather than dropping them
            bucket = merged.setdefault(
                f"raw:{raw.lower()}",
                {
                    "code": f"raw:{raw.lower()}",
                    "value": raw,
                    "label": raw,
                    "label_en": raw,
                    "label_zh": raw,
                    "tier": "other",
                    "count": 0,
                },
            )
        bucket["count"] += int(row.get("count") or 0)
    ordered = sorted(merged.values(), key=lambda item: item["count"], reverse=True)
    return ordered
