"""Chinese → English legal query translation.

Canada's corpus (A2AJ case law + Justice Laws XML) is English/French only, and
PostgreSQL full-text search uses English stemming. A Chinese query therefore
scores zero on the lexical channel even though BGE-M3 handles it on the vector
channel.

This module adds a lightweight query-translation layer:

* detect whether the query is predominantly Chinese;
* map Chinese legal phrases to their English equivalents with a curated
  dictionary (longest-match first, so "房屋出租" wins over "房屋");
* fall back to the original text when nothing matches.

No network call and no model load, so it stays on the hot path safely. A model
based fallback can be layered in later without changing the call sites.
"""

from __future__ import annotations

import re

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_TOKEN_SPLIT = re.compile(r"[，,。.；;：:、!?！？\s]+")

#: Curated legal phrase dictionary. Keep keys ordered-independent; the matcher
#: always tries longer keys first.
LEGAL_TERMS: dict[str, str] = {
    # ---- 租赁 / 不动产 ----
    "房屋出租": "residential tenancy",
    "房屋租赁": "residential tenancy",
    "住宅租赁": "residential tenancy",
    "租房合同": "residential tenancy agreement",
    "租房": "residential tenancy",
    "房租": "rent",
    "退租": "vacate",
    "二房东": "sublandlord",
    "合租": "shared accommodation",
    "商业租赁": "commercial lease",
    "租赁合同": "lease agreement",
    "租约解除": "lease termination",
    "租约终止": "lease termination",
    "租赁纠纷": "lease dispute",
    "租约": "lease agreement",
    "房东": "landlord",
    "出租人": "landlord",
    "租客": "tenant",
    "承租人": "tenant",
    "押金": "security deposit",
    "租金": "rent",
    "拖欠租金": "rent arrears",
    "欠租": "rent arrears",
    "驱逐": "eviction",
    "腾退": "vacate possession",
    "提前解约": "early termination",
    "提前解除": "early termination",
    "续租": "lease renewal",
    "转租": "sublet",
    "房屋": "premises",
    "物业": "property",
    "装修": "renovation",
    "漏水": "water damage",
    "维修": "repair and maintenance",
    "居住条件": "habitability",
    "买卖合同": "agreement of purchase and sale",
    "房产": "real property",
    "房屋买卖": "real estate sale",
    "购买房产": "real estate purchase",
    "卖方": "seller",
    "房产中介": "real estate agent",
    "中介": "broker",
    "重大失实陈述": "material misrepresentation",
    "欺诈": "fraudulent misrepresentation",
    "披露": "disclosure",
    "隐瞒": "concealment",
    "隐蔽瑕疵": "latent defect",
    "种过大麻": "cannabis grow operation",
    "种植大麻": "cannabis grow operation",
    "儿童情色录像": "child pornography",
    "儿童色情": "child pornography",
    "儿童性虐待材料": "child sexual abuse material",
    "访问相关材料": "accessing material",
    "浏览或访问": "accessing",
    "保存或持有": "possession",
    "存储到个人电脑": "computer file storage",
    "抵押": "mortgage",
    "产权": "title",
    "过户": "transfer of title",
    # ---- 合同 / 债务 ----
    "合同": "contract",
    "显失公平": "unconscionability",
    "不当影响": "undue influence",
    "撤销合同": "rescission of contract",
    "合同撤销": "rescission of contract",
    "撤销": "rescission",
    "违约": "breach of contract",
    "违约责任": "liability for breach",
    "解除合同": "termination of contract",
    "无效合同": "void contract",
    "定金": "deposit",
    "欠款": "debt",
    "债务": "debt",
    "利息": "interest",
    "担保": "guarantee",
    "赔偿": "damages",
    "损失": "loss",
    "违约金": "liquidated damages",
    # ---- 侵权 / 人身 ----
    "侵权": "tort",
    "过失": "negligence",
    "人身伤害": "personal injury",
    "交通事故": "motor vehicle accident",
    "医疗事故": "medical malpractice",
    "名誉权": "defamation",
    "诽谤": "defamation",
    "隐私": "privacy",
    "产品责任": "product liability",
    "精神损害": "mental distress",
    # ---- 劳动 ----
    "劳动争议": "employment dispute",
    "劳动合同": "employment contract",
    "解雇": "dismissal",
    "不当解雇": "wrongful dismissal",
    "工资": "wages",
    "加班费": "overtime pay",
    "工伤": "workplace injury",
    "竞业限制": "non-compete",
    "社保": "social insurance",
    # ---- 家庭 ----
    "离婚": "divorce",
    "抚养权": "custody",
    "抚养费": "child support",
    "财产分割": "division of property",
    "婚前协议": "prenuptial agreement",
    "继承": "inheritance",
    "遗嘱": "will",
    "遗产": "estate",
    # ---- 公司 / 商事 ----
    "公司": "corporation",
    "股东": "shareholder",
    "股权": "equity interest",
    "董事": "director",
    "清算": "liquidation",
    "破产": "bankruptcy",
    "合伙": "partnership",
    # ---- 知识产权 ----
    "知识产权": "intellectual property",
    "著作权": "copyright",
    "版权": "copyright",
    "商标": "trademark",
    "专利": "patent",
    "商业秘密": "trade secret",
    "侵权赔偿": "infringement damages",
    # ---- 刑事 / 程序 ----
    "刑事": "criminal",
    "量刑": "sentencing",
    "醉酒": "intoxication",
    "既往记录": "prior criminal record",
    "犯罪记录": "criminal record",
    "前科": "prior conviction",
    "加重减轻": "aggravating and mitigating factors",
    "加重": "aggravating factor",
    "减轻": "mitigating factor",
    "诈骗": "fraud",
    "盗窃": "theft",
    "殴打": "assault",
    "保释": "bail",
    "上诉": "appeal",
    "管辖": "jurisdiction",
    "诉讼时效": "limitation period",
    "证据": "evidence",
    "证人": "witness",
    "判决": "judgment",
    "裁定": "order",
    "执行": "enforcement",
    # ---- 行政 / 移民 ----
    "移民": "immigration",
    "签证": "visa",
    "永居": "permanent residence",
    "难民": "refugee",
    "行政处罚": "administrative penalty",
    # ---- 通用法律词 ----
    "纠纷": "dispute",
    "争议": "dispute",
    "责任": "liability",
    "权利": "right",
    "义务": "obligation",
    "法律": "law",
    "法院": "court",
    "判例": "case law",
    "法条": "statute",
    "法规": "legislation",
    "条款": "provision",
    "安大略": "Ontario",
    "魁北克": "Quebec",
    "不列颠哥伦比亚": "British Columbia",
    "加拿大": "Canada",
}

# Canonical UTF-8 entries.  Some historical deployments carried a mojibake
# version of the original table; these explicit keys keep Chinese Canadian
# intake deterministic and are intentionally scoped to retrieval vocabulary.
LEGAL_TERMS.update({
    "安大略省": "Ontario",
    "加拿大": "Canada",
    "租房": "residential tenancy",
    "租赁": "residential tenancy",
    "房东": "landlord",
    "租客": "tenant",
    "承租人": "tenant",
    "出租人": "landlord",
    "维修": "repair maintenance",
    "漏水": "water damage leak",
    "霉菌": "mould habitability",
    "适居": "habitability",
    "驱逐": "eviction",
    "自住": "landlord own use",
    "善意": "good faith",
    "收房": "termination possession",
    "自用": "landlord own use N12",
    "报复性驱逐": "retaliatory eviction",
    "通知": "notice",
    "救济": "remedy",
    "判例": "case law",
    "法规": "legislation",
    "义务": "obligation",
    "证据": "evidence",
    "损害赔偿": "damages",
})

#: Filler words that carry no retrieval signal.
_STOPWORDS = (
    "请问", "我想", "咨询", "一下", "相关的", "相关", "有关", "关于",
    "如何", "怎么", "怎样", "能否", "是否", "可以", "应该", "需要",
    "问题", "情况", "方面", "这个", "那个", "我们", "他们", "我的",
    "的", "了", "吗", "呢", "吧", "和", "与", "及", "或",
)

_MAX_TERMS = 12

#: Generic English legal words. They carry no retrieval signal on their own and,
#: because the full-text channel ANDs terms, combining them with a domain noun
#: ("tort trademark") collapses recall. Dropped whenever a more specific term exists.
_GENERIC_EN = {
    "tort",
    "dispute",
    "law",
    "court",
    "statute",
    "legislation",
    "case law",
    "right",
    "obligation",
    "provision",
    "legal",
}

#: Synonym groups for retrieval expansion. Statute wording differs from colloquial
#: legal English ("residential tenancy" vs "tenant" vs "lease"), so a single
#: phrase misses most of the corpus. Terms from a matched group are appended as
#: extra keywords, which the retrieval layer turns into an OR fallback query.
SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("residential tenancy", "residential tenancies", "tenancy", "tenancies", "tenant", "tenants", "landlord", "landlords", "lease", "leases", "rent"),
    ("security deposit", "deposit", "deposits", "rent deposit"),
    ("eviction", "evict", "possession", "terminate tenancy"),
    ("copyright", "copyrights", "authorial", "infringement"),
    ("trademark", "trade-mark", "trademarks"),
    ("patent", "patents", "patentability"),
    ("trade secret", "confidential information"),
    ("wrongful dismissal", "dismissal", "termination of employment", "just cause"),
    ("employment", "employee", "employer", "workplace"),
    ("negligence", "negligent", "duty of care", "standard of care"),
    ("personal injury", "injury", "injured"),
    ("contract", "contracts", "contractual", "agreement"),
    ("unconscionability", "unconscionable bargain", "inequality of bargaining power", "improvident bargain"),
    ("undue influence", "influence", "relationship of dependency"),
    ("rescission", "rescind", "set aside", "equitable remedy"),
    ("breach of contract", "breach", "repudiation"),
    ("damages", "compensation", "remedy", "remedies"),
    ("fraud", "fraudulent", "misrepresentation"),
    ("defamation", "libel", "slander"),
    ("privacy", "private information", "intrusion upon seclusion"),
    ("custody", "parenting", "access", "child support"),
    ("divorce", "separation", "family law"),
    ("immigration", "visa", "permanent residence", "sponsorship"),
    ("refugee", "refugee protection", "asylum"),
    ("bankruptcy", "insolvency", "liquidation"),
    ("limitation period", "limitations", "time limit"),
    ("sentencing", "sentence", "aggravating factor", "mitigating factor"),
    ("intoxication", "self-induced intoxication", "extreme intoxication", "drunkenness"),
    ("prior criminal record", "criminal record", "prior conviction", "record of convictions"),
    ("landlord own use", "own use", "personal use", "N12"),
    ("good faith", "bona fide", "genuine intention"),
    ("jurisdiction", "forum", "venue"),
)

_GROUP_INDEX: dict[str, tuple[str, ...]] = {}
for _group in SYNONYM_GROUPS:
    for _term in _group:
        _GROUP_INDEX[_term] = _group


def synonym_terms(translated: str, limit: int = 6) -> list[str]:
    """Return extra retrieval keywords drawn from synonym groups.

    Only groups actually hit by the translated query contribute, and the
    original phrase itself is excluded so the primary query stays precise.
    """
    text = (translated or "").lower()
    if not text:
        return []
    collected: list[str] = []
    seen: set[str] = {text}
    # longest group members first so "residential tenancy" is matched before "rent"
    for term in sorted(_GROUP_INDEX, key=len, reverse=True):
        if term not in text:
            continue
        for candidate in _GROUP_INDEX[term]:
            if candidate in seen:
                continue
            seen.add(candidate)
            collected.append(candidate)
            if len(collected) >= limit:
                return collected
    return collected


def contains_chinese(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def detect_language(text: str) -> str:
    """Return ``zh``, ``en`` or ``mixed`` based on CJK character ratio."""
    text = text or ""
    if not text.strip():
        return "en"
    cjk = len(_CJK_RE.findall(text))
    letters = len(re.findall(r"[A-Za-z]", text))
    if cjk == 0:
        return "en"
    if letters == 0:
        return "zh"
    return "zh" if cjk >= letters * 0.3 else "mixed"


def translate_query(text: str) -> dict:
    """Translate a Chinese legal query into English search terms.

    Returns a dict with:
    ``source_lang``  detected language
    ``translated``   English query (empty string when nothing matched)
    ``matched``      list of (zh, en) pairs that were used
    ``method``       ``dictionary`` | ``none`` | ``passthrough``
    """
    clean = (text or "").strip()
    lang = detect_language(clean)
    if lang == "en" or not clean:
        return {"source_lang": lang, "translated": "", "matched": [], "method": "passthrough"}

    remaining = clean
    for word in _STOPWORDS:
        remaining = remaining.replace(word, " ")

    matched: list[tuple[str, str]] = []
    seen_en: set[str] = set()
    # longest key first so that "房屋出租" is consumed before "房屋"
    for key in sorted(LEGAL_TERMS, key=len, reverse=True):
        if key not in remaining:
            continue
        english = LEGAL_TERMS[key]
        if english in seen_en:
            remaining = remaining.replace(key, " ")
            continue
        matched.append((key, english))
        seen_en.add(english)
        remaining = remaining.replace(key, " ")
        if len(matched) >= _MAX_TERMS:
            break

    if not matched:
        return {"source_lang": lang, "translated": "", "matched": [], "method": "none"}

    terms = [english for _, english in matched]
    if len(terms) > 1:
        specific = [term for term in terms if term not in _GENERIC_EN]
        if specific:
            terms = specific

    english_terms = re.findall(r"[A-Za-z][A-Za-z0-9'-]*", remaining)
    translated = " ".join(dict.fromkeys([*terms, *english_terms]))
    return {
        "source_lang": lang,
        "translated": translated,
        "matched": matched,
        "method": "dictionary",
    }
