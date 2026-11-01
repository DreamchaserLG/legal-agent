import React, { memo, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const COPY = {
  zh: {
    research: "加拿大法律检索",
    title: "本地法规与案例检索",
    intro: "仅检索本地已入库并可核验的加拿大法律资料，不会在检索过程中从外网补充内容。",
    query: "检索内容",
    queryPlaceholder: "输入争议焦点、法规名称、案号或关键词",
    search: "检索",
    localOnly: "仅本地资料库",
    laws: "相关法律法规",
    lawsCopy: "每行一条；悬停、聚焦或点击即可查看完整条款内容及按法院排列的关联案例。",
    cases: "相关案例",
    casesCopy: "国家/联邦与省级/地方案例在同一列表中展示，可通过上方约束条件筛选。",
    all: "全部",
    national: "国家 / 联邦",
    local: "省级 / 地方",
    court: "法院",
    caseType: "案件类型",
    lawType: "法规类型",
    keywordFilter: "在当前结果中筛选",
    pageSize: "每页",
    previous: "上一页",
    next: "下一页",
    page: "页",
    items: "条",
    details: "查看完整信息",
    collapse: "收起信息",
    relatedCases: "关联案例",
    relatedLaws: "对应法律法规",
    content: "完整内容",
    summary: "摘要",
    judgment: "裁判结果",
    reason: "关联依据",
    source: "核验来源",
    detail: "查看详情",
    noLaws: "当前本地资料库未命中可核验的法规条款。",
    noCases: "当前约束条件下没有匹配案例。",
    noRelations: "暂无已核验的关联资料",
    moreRelations: "切换关联资料",
    localNotice: "本页不会触发 CanLII、政府网站或其他外部资源抓取。",
  },
  en: {
    research: "Canadian Legal Research",
    title: "Local legislation and case search",
    intro: "Searches only verified Canadian legal materials already stored locally. No external content is fetched during retrieval.",
    query: "Search query",
    queryPlaceholder: "Enter an issue, instrument, citation, docket, or keyword",
    search: "Search",
    localOnly: "Local repository only",
    laws: "Relevant legislation",
    lawsCopy: "One item per row. Hover, focus, or click to reveal the full provision and related cases ordered by court.",
    cases: "Relevant cases",
    casesCopy: "Federal/national and provincial/local authorities share one list and can be constrained above.",
    all: "All",
    national: "National / Federal",
    local: "Provincial / Local",
    court: "Court",
    caseType: "Case type",
    lawType: "Instrument type",
    keywordFilter: "Filter current results",
    pageSize: "Per page",
    previous: "Previous",
    next: "Next",
    page: "Page",
    items: "items",
    details: "Show full information",
    collapse: "Collapse information",
    relatedCases: "Related cases",
    relatedLaws: "Applicable legislation",
    content: "Full content",
    summary: "Summary",
    judgment: "Disposition",
    reason: "Relationship basis",
    source: "Verify source",
    detail: "Open detail",
    noLaws: "No verifiable provision was found in the local repository.",
    noCases: "No case matches the current constraints.",
    noRelations: "No verified related material is available",
    moreRelations: "Browse related material",
    localNotice: "This page does not trigger CanLII, government-site, or other external resource fetching.",
  },
};

const clean = (value) => String(value ?? "").trim();
const lower = (value) => clean(value).toLocaleLowerCase();
const first = (...values) => values.map(clean).find(Boolean) || "";

function identity(item, kind) {
  return first(item?.id, item?.case_id, item?.rule_id, item?.law_id, item?.detail_url, item?.source_url, `${kind}:${lower(item?.title)}`);
}

function unique(items, kind) {
  const seen = new Set();
  return (items || []).filter((item) => {
    if (!item) return false;
    const key = identity(item, kind);
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function sameRecord(a, b, kind) {
  const aId = identity(a, kind);
  const bId = identity(b, kind);
  return aId === bId || (lower(a?.title) && lower(a?.title) === lower(b?.title));
}

function caseScope(item) {
  if (item?.scope === "national_federal") return "national";
  if (item?.scope === "provincial_local") return "local";
  const court = lower(`${item?.court_level || ""} ${item?.court_name || ""}`);
  return /(supreme court of canada|federal court|federal court of appeal|tax court of canada|national)/.test(court)
    ? "national"
    : "local";
}

function prepareRelations(modulePacket = {}, supplementalLaws = [], supplementalCases = []) {
  const packetLaws = modulePacket.relevant_laws || [];
  const packetCases = modulePacket.case_law_rows || [];
  const nestedLaws = [...packetCases, ...supplementalCases].flatMap((item) => item?.rules || []);
  const nestedCases = [...packetLaws, ...supplementalLaws].flatMap((item) => item?.related_cases || []);
  const laws = unique([...packetLaws, ...supplementalLaws, ...nestedLaws], "law");
  const cases = unique([...packetCases, ...supplementalCases, ...nestedCases], "case");

  const resolvedLaws = laws.map((law) => {
    const reverseCases = cases.filter((caseItem) =>
      (caseItem.rules || []).some((rule) => sameRecord(rule, law, "law")) ||
      (caseItem.linked_law_titles || []).some((title) => lower(title) === lower(law.title)),
    );
    return { ...law, related_cases: unique([...(law.related_cases || []), ...reverseCases], "case") };
  });

  const resolvedCases = cases.map((caseItem) => {
    const titleRules = (caseItem.linked_law_titles || [])
      .map((title) => resolvedLaws.find((law) => lower(law.title) === lower(title)))
      .filter(Boolean);
    const reverseLaws = resolvedLaws.filter((law) =>
      (law.related_cases || []).some((related) => sameRecord(related, caseItem, "case")),
    );
    return { ...caseItem, rules: unique([...(caseItem.rules || []), ...titleRules, ...reverseLaws], "law") };
  });
  return { laws: resolvedLaws, cases: resolvedCases };
}

function readPayload(id) {
  const node = document.getElementById(id);
  if (!node) return null;
  try {
    return JSON.parse(node.textContent || "{}");
  } catch (error) {
    console.error("Unable to parse legal relations payload", error);
    return null;
  }
}

const Meta = memo(function Meta({ children }) {
  return children ? <span className="lr-meta">{children}</span> : null;
});

function LinkButton({ href, children, external = false }) {
  if (!href) return null;
  return (
    <a className="lr-link" href={href} target={external ? "_blank" : undefined} rel={external ? "noreferrer noopener" : undefined}>
      {children}
    </a>
  );
}

const MiniCase = memo(function MiniCase({ item, t }) {
  return (
    <article className="lr-mini-card">
      <Meta>{first(item.court_level, item.court_name, item.case_type)}</Meta>
      <h4>{first(item.title, "Untitled case")}</h4>
      <p>{first(item.summary, item.facts, item.excerpt, t.noRelations)}</p>
      <div className="lr-mini-footer">
        <span>{first(item.judgment_date, item.published_at)}</span>
        <LinkButton href={first(item.detail_url, item.source_url, item.url)} external>{t.source}</LinkButton>
      </div>
    </article>
  );
});

const MiniLaw = memo(function MiniLaw({ item, t }) {
  return (
    <article className="lr-mini-card">
      <Meta>{first(item.article_no, item.legal_type, item.rule_level)}</Meta>
      <h4>{first(item.title, "Untitled instrument")}</h4>
      <p>{first(item.article_summary, item.reason, item.match_reason, t.noRelations)}</p>
      <div className="lr-mini-footer">
        <span>{first(item.country, "Canada")}</span>
        <LinkButton href={first(item.detail_url, item.source_url)} external={Boolean(item.source_url && !item.detail_url)}>{t.detail}</LinkButton>
      </div>
    </article>
  );
});

function AssociationRail({ items, kind, t }) {
  const pageSize = 3;
  const [page, setPage] = useState(0);
  const ordered = useMemo(() => {
    const copy = [...(items || [])];
    if (kind === "case") {
      copy.sort((a, b) => first(a.court_level, a.court_name).localeCompare(first(b.court_level, b.court_name)));
    }
    return copy;
  }, [items, kind]);
  const pageCount = Math.max(1, Math.ceil(ordered.length / pageSize));
  const safePage = Math.min(page, pageCount - 1);
  const visible = ordered.slice(safePage * pageSize, safePage * pageSize + pageSize);
  if (!ordered.length) return <p className="lr-empty-inline">{t.noRelations}</p>;
  return (
    <div className="lr-association">
      <div className="lr-association-grid">
        {visible.map((item) => kind === "case"
          ? <MiniCase key={identity(item, "case")} item={item} t={t} />
          : <MiniLaw key={identity(item, "law")} item={item} t={t} />)}
      </div>
      {pageCount > 1 ? (
        <div className="lr-rail-controls" aria-label={t.moreRelations}>
          <button type="button" onClick={() => setPage((value) => (value - 1 + pageCount) % pageCount)} aria-label={t.previous}>←</button>
          <span>{safePage + 1} / {pageCount}</span>
          <button type="button" onClick={() => setPage((value) => (value + 1) % pageCount)} aria-label={t.next}>→</button>
        </div>
      ) : null}
    </div>
  );
}

function Pager({ page, setPage, total, pageSize, setPageSize, t }) {
  const count = Math.max(1, Math.ceil(total / pageSize));
  const safePage = Math.min(page, count - 1);
  return (
    <div className="lr-pager">
      <label>{t.pageSize}
        <select value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value)); setPage(0); }}>
          {[5, 10, 15, 20].map((value) => <option key={value} value={value}>{value}</option>)}
        </select>
      </label>
      <span>{total} {t.items}</span>
      <button type="button" disabled={safePage === 0} onClick={() => setPage(safePage - 1)}>← {t.previous}</button>
      <strong>{t.page} {safePage + 1} / {count}</strong>
      <button type="button" disabled={safePage >= count - 1} onClick={() => setPage(safePage + 1)}>{t.next} →</button>
    </div>
  );
}

const LawRow = memo(function LawRow({ law, index, t }) {
  const [expanded, setExpanded] = useState(false);
  const related = law.related_cases || [];
  return (
    <article className={`lr-row lr-law-row${expanded ? " is-expanded" : ""}`} tabIndex="0">
      <div className="lr-row-head">
        <span className="lr-index">{String(index + 1).padStart(2, "0")}</span>
        <div className="lr-row-title">
          <h3>{law.title}</h3>
          <p>{first(law.article_summary, law.reason, law.article_text, t.noLaws)}</p>
        </div>
        <div className="lr-row-tags">
          <Meta>{law.article_no}</Meta><Meta>{first(law.legal_type, law.rule_level)}</Meta>
          <Meta>{related.length} {t.relatedCases}</Meta>
        </div>
        <button type="button" className="lr-expand" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
          {expanded ? t.collapse : t.details} <span>⌄</span>
        </button>
      </div>
      <div className="lr-row-detail">
        <div className="lr-copy-panel">
          <span className="lr-label">{t.content}</span>
          <p>{first(law.article_text, law.article_summary, law.reason, t.noLaws)}</p>
          {law.match_reason ? <p><strong>{t.reason}:</strong> {law.match_reason}</p> : null}
          <div className="lr-actions">
            <LinkButton href={law.detail_url}>{t.detail}</LinkButton>
            <LinkButton href={law.source_url} external>{t.source}</LinkButton>
          </div>
        </div>
        <div className="lr-related-panel">
          <span className="lr-label">{t.relatedCases}</span>
          <AssociationRail items={related} kind="case" t={t} />
        </div>
      </div>
    </article>
  );
});

const CaseRow = memo(function CaseRow({ item, index, t }) {
  const [expanded, setExpanded] = useState(false);
  const laws = item.rules || [];
  return (
    <article className={`lr-row lr-case-row${expanded ? " is-expanded" : ""}`} tabIndex="0">
      <div className="lr-row-head">
        <span className="lr-index">{String(index + 1).padStart(2, "0")}</span>
        <div className="lr-row-title">
          <h3>{item.title}</h3>
          <p>{first(item.summary, item.facts, item.excerpt, t.noCases)}</p>
        </div>
        <div className="lr-row-tags">
          <Meta>{caseScope(item) === "national" ? t.national : t.local}</Meta>
          <Meta>{first(item.court_level, item.court_name)}</Meta>
          <Meta>{laws.length} {t.relatedLaws}</Meta>
        </div>
        <button type="button" className="lr-expand" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
          {expanded ? t.collapse : t.details} <span>⌄</span>
        </button>
      </div>
      <div className="lr-row-detail">
        <div className="lr-copy-panel">
          <span className="lr-label">{t.summary}</span>
          <p>{first(item.summary, item.facts, item.excerpt, t.noCases)}</p>
          {item.judgment_result ? <p><strong>{t.judgment}:</strong> {item.judgment_result}</p> : null}
          {item.match_reason ? <p><strong>{t.reason}:</strong> {item.match_reason}</p> : null}
          <div className="lr-actions">
            <LinkButton href={first(item.detail_url, item.source_url, item.url)} external>{t.source}</LinkButton>
          </div>
        </div>
        <div className="lr-related-panel">
          <span className="lr-label">{t.relatedLaws}</span>
          <AssociationRail items={laws} kind="law" t={t} />
        </div>
      </div>
    </article>
  );
});

function FilterChip({ active, onClick, children }) {
  return <button type="button" className={`lr-filter-chip${active ? " is-active" : ""}`} onClick={onClick}>{children}</button>;
}

function LegalRelationsExplorer({ data, compact = false }) {
  const lang = data.lang === "en" ? "en" : "zh";
  const t = COPY[lang];
  const { laws, cases } = useMemo(
    () => prepareRelations(data.modulePacket, data.supplementalLaws, data.supplementalCases),
    [data.modulePacket, data.supplementalLaws, data.supplementalCases],
  );
  const [lawQuery, setLawQuery] = useState("");
  const [lawType, setLawType] = useState("");
  const [lawPage, setLawPage] = useState(0);
  const [lawPageSize, setLawPageSize] = useState(5);
  const [scope, setScope] = useState("all");
  const [court, setCourt] = useState("");
  const [caseType, setCaseType] = useState("");
  const [caseQuery, setCaseQuery] = useState("");
  const [casePage, setCasePage] = useState(0);
  const [casePageSize, setCasePageSize] = useState(5);

  const lawTypes = useMemo(() => [...new Set(laws.map((law) => first(law.legal_type, law.rule_level)).filter(Boolean))].sort(), [laws]);
  const courts = useMemo(() => [...new Set(cases.map((item) => first(item.court_level, item.court_name)).filter(Boolean))].sort(), [cases]);
  const caseTypes = useMemo(() => [...new Set(cases.map((item) => item.case_type).filter(Boolean))].sort(), [cases]);
  const filteredLaws = useMemo(() => laws.filter((law) => {
    const haystack = lower(`${law.title} ${law.article_no || ""} ${law.article_summary || ""} ${law.article_text || ""}`);
    return (!lawType || first(law.legal_type, law.rule_level) === lawType) && (!lawQuery || haystack.includes(lower(lawQuery)));
  }), [laws, lawType, lawQuery]);
  const filteredCases = useMemo(() => cases.filter((item) => {
    const haystack = lower(`${item.title} ${item.summary || ""} ${item.facts || ""} ${item.match_reason || ""}`);
    return (scope === "all" || caseScope(item) === scope)
      && (!court || first(item.court_level, item.court_name) === court)
      && (!caseType || item.case_type === caseType)
      && (!caseQuery || haystack.includes(lower(caseQuery)));
  }), [cases, scope, court, caseType, caseQuery]);

  const visibleLaws = filteredLaws.slice(lawPage * lawPageSize, lawPage * lawPageSize + lawPageSize);
  const visibleCases = filteredCases.slice(casePage * casePageSize, casePage * casePageSize + casePageSize);

  return (
    <div className={`lr-explorer${compact ? " is-compact" : ""}`}>
      <section className="lr-section">
        <div className="lr-section-head">
          <div><span className="lr-eyebrow">{t.localOnly}</span><h2>{t.laws}</h2><p>{t.lawsCopy}</p></div>
        </div>
        <div className="lr-filter-line">
          <label><span>{t.lawType}</span><select value={lawType} onChange={(event) => { setLawType(event.target.value); setLawPage(0); }}><option value="">{t.all}</option>{lawTypes.map((value) => <option key={value}>{value}</option>)}</select></label>
          <label className="lr-grow"><span>{t.keywordFilter}</span><input value={lawQuery} onChange={(event) => { setLawQuery(event.target.value); setLawPage(0); }} /></label>
        </div>
        <div className="lr-list">
          {visibleLaws.length ? visibleLaws.map((law, index) => <LawRow key={identity(law, "law")} law={law} index={lawPage * lawPageSize + index} t={t} />) : <div className="lr-empty">{t.noLaws}</div>}
        </div>
        <Pager page={lawPage} setPage={setLawPage} total={filteredLaws.length} pageSize={lawPageSize} setPageSize={setLawPageSize} t={t} />
      </section>

      <section className="lr-section">
        <div className="lr-section-head">
          <div><span className="lr-eyebrow">{t.localOnly}</span><h2>{t.cases}</h2><p>{t.casesCopy}</p></div>
        </div>
        <div className="lr-filter-line lr-case-constraints">
          <div className="lr-scope" role="group" aria-label={t.cases}>
            <FilterChip active={scope === "all"} onClick={() => { setScope("all"); setCasePage(0); }}>{t.all}</FilterChip>
            <FilterChip active={scope === "national"} onClick={() => { setScope("national"); setCasePage(0); }}>{t.national}</FilterChip>
            <FilterChip active={scope === "local"} onClick={() => { setScope("local"); setCasePage(0); }}>{t.local}</FilterChip>
          </div>
          <label><span>{t.court}</span><select value={court} onChange={(event) => { setCourt(event.target.value); setCasePage(0); }}><option value="">{t.all}</option>{courts.map((value) => <option key={value}>{value}</option>)}</select></label>
          <label><span>{t.caseType}</span><select value={caseType} onChange={(event) => { setCaseType(event.target.value); setCasePage(0); }}><option value="">{t.all}</option>{caseTypes.map((value) => <option key={value}>{value}</option>)}</select></label>
          <label className="lr-grow"><span>{t.keywordFilter}</span><input value={caseQuery} onChange={(event) => { setCaseQuery(event.target.value); setCasePage(0); }} /></label>
        </div>
        <div className="lr-list">
          {visibleCases.length ? visibleCases.map((item, index) => <CaseRow key={identity(item, "case")} item={item} index={casePage * casePageSize + index} t={t} />) : <div className="lr-empty">{t.noCases}</div>}
        </div>
        <Pager page={casePage} setPage={setCasePage} total={filteredCases.length} pageSize={casePageSize} setPageSize={setCasePageSize} t={t} />
      </section>
    </div>
  );
}

function SearchApp({ data }) {
  const lang = data.lang === "en" ? "en" : "zh";
  const t = COPY[lang];
  return (
    <div className="lr-search-page">
      <section className="lr-search-hero">
        <div className="shell lr-search-hero-grid">
          <div>
            <span className="lr-eyebrow">{t.research}</span>
            <h1>{data.keywords || t.title}</h1>
            <p>{t.intro}</p>
            <div className="lr-local-badge"><span aria-hidden="true">✓</span>{t.localNotice}</div>
          </div>
          <form action="/results" method="get" className="lr-search-form">
            <label><span>{t.query}</span><input name="keywords" defaultValue={data.keywords || ""} placeholder={t.queryPlaceholder} /></label>
            <input type="hidden" name="module" value="canada" />
            <input type="hidden" name="source" value="all" />
            <input type="hidden" name="sort" value="relevance" />
            <input type="hidden" name="limit" value="5" />
            <input type="hidden" name="offset" value="0" />
            <button type="submit">{t.search}</button>
          </form>
        </div>
      </section>
      {data.keywords ? (
        <div className="shell"><LegalRelationsExplorer data={data} /></div>
      ) : (
        <div className="shell lr-empty lr-search-empty">{t.intro}</div>
      )}
    </div>
  );
}

document.querySelectorAll("[data-results-react-root]").forEach((root) => {
  const data = readPayload(root.dataset.payload);
  if (data) createRoot(root).render(<SearchApp data={data} />);
});

document.querySelectorAll("[data-legal-relations-root]").forEach((root) => {
  const data = readPayload(root.dataset.payload);
  if (data) createRoot(root).render(<LegalRelationsExplorer data={data} compact />);
});
