# 本地数据库关联数据补充测试报告

检查日期：2026-09-11  
数据库：`legal_demo`（PostgreSQL）  
执行结论：**已停止，未对数据库执行任何写入、索引创建或表结构修改。**

## 1. 执行范围与停止依据

本次仅探查本地 PostgreSQL 的既有表结构，并校验三份关联 CSV 是否能无损映射到既有关系表。

已发现结构不匹配：数据库中不存在“法规 -> 法规”关系表，无法承载 `statute_to_statute_links.csv` 中的 `statute_id`、`cited_statute_id`、`citation_raw`、`citation_normalized`。根据任务约束“发现表结构与关联数据所需字段不匹配，立即停止，不自动修改表结构”，后续的 CSV 追加、索引补齐、完整性检查和查询验证均未执行。

本报告不代表数据导入结果；当前表记录数均为探查时已有数据。

## 2. CSV 输入校验

| 文件 | 表头 | 记录数 |
| --- | --- | ---: |
| `outputs/a2aj_links/case_to_case_links.csv` | `case_id`, `cited_case_id`, `relation` | 1,032,998 |
| `outputs/a2aj_links/case_to_statute_links.csv` | `case_id`, `statute_citation_raw`, `statute_citation_normalized`, `statute_id_matched` | 102,755 |
| `outputs/a2aj_links/statute_to_statute_links.csv` | `statute_id`, `cited_statute_id`, `citation_raw`, `citation_normalized` | 5,134 |

其中，判例 -> 法规 CSV 有 27,369 条未匹配法规引用（`statute_id_matched` 为空）；法规 -> 法规 CSV 有 1,896 条未匹配被引法规（`cited_statute_id` 为空）。CSV 文件未被修改。

## 3. 相关表探查结果

数据库共有 28 张基础表。下列 10 张是案例、法规、原始来源及现有关系直接相关的表；字段、主键、索引和记录数均由 PostgreSQL 系统目录读取。

### `source_items`

- 当前记录数：241,369；主键：`id`。
- 字段：`id bigint`，`source_code varchar`，`source_uid varchar`，`title text`，`item_url text`，`published_at timestamp`，`summary text`，`raw_text text`，`raw_json jsonb`，`created_at timestamp`，`updated_at timestamp`。
- 索引：`source_items_pkey (id)`，`source_items_source_code_source_uid_key (source_code, source_uid)`，`idx_source_items_published`，`idx_source_items_source`，`idx_source_items_source_updated`，`idx_source_items_updated`，以及 `title`、`summary`、`raw_text` 的 Trigram 检索索引。

### `legal_cases`

- 当前记录数：224,376；主键：`id`。
- 字段：`id bigint`，`title text`，`country text`，`court_name text`，`court_level text`，`court_rank integer`，`case_type text`，`summary text`，`facts text`，`judgment_result text`，`judgment_date date`，`source_url text`，`source_site text`，`raw_text text`，`source_item_id bigint`，`source_code varchar`，`external_uid text`，`normalized_title text`，`created_at timestamp`，`updated_at timestamp`。
- 索引：`legal_cases_pkey (id)`，`uq_legal_cases_identity`，`uq_legal_cases_source_item (source_item_id)`，`uq_legal_cases_source_item_full`，`uq_legal_cases_source_url`，`idx_legal_cases_country_level`，`idx_legal_cases_normalized_title`。

### `canada_laws`

- 当前记录数：12,726；主键：`id`。
- 字段：`id bigint`，`source_item_id bigint`，`source_code varchar`，`source_uid text`，`title text`，`normalized_title text`，`slug varchar`，`citation text`，`jurisdiction text`，`law_level text`，`law_kind text`，`source_url text`，`aliases_json jsonb`，`origin varchar`，`created_at timestamp`，`updated_at timestamp`。
- 索引：`canada_laws_pkey (id)`，`uq_canada_laws_normalized_title (normalized_title)`，`uq_canada_laws_slug (slug)`，`uq_canada_laws_source_item (source_item_id)`。
- 注意：`citation` 当前没有单列索引。

### `legal_rules`

- 当前记录数：12,726；主键：`id`。
- 字段：`id bigint`，`title text`，`country text`，`legal_type text`，`article_no text`，`article_text text`，`article_summary text`，`source_url text`，`source_site text`，`source_item_id bigint`，`canada_law_id bigint`，`normalized_title text`，`slug varchar`，`rule_level text`，`citation text`，`created_at timestamp`，`updated_at timestamp`。
- 索引：`legal_rules_pkey (id)`，`uq_legal_rules_canada_law`，`uq_legal_rules_canada_law_full`，`uq_legal_rules_identity`，`uq_legal_rules_slug`，`uq_legal_rules_source_item`，`uq_legal_rules_source_item_full`，`uq_legal_rules_source_url`，`idx_legal_rules_normalized_title`。

### `a2aj_case_citations`

- 当前记录数：1,039,332；主键：`id`。
- 字段：`id bigint`，`citing_item_id bigint`，`cited_citation text`，`normalized_citation text`，`cited_item_id bigint`（可空），`match_method varchar`，`created_at timestamp`，`updated_at timestamp`。
- 索引：`a2aj_case_citations_pkey (id)`，`a2aj_case_citations_citing_item_id_normalized_citation_key (citing_item_id, normalized_citation)`，`idx_a2aj_case_citations_citing_item (citing_item_id)`，`idx_a2aj_case_citations_cited_item (cited_item_id)`。

### `case_case_citations`

- 当前记录数：933,750；主键：`id`。
- 字段：`id bigint`，`citing_case_id bigint`，`cited_case_id bigint`，`relation_source varchar`，`match_score numeric`，`created_at timestamp`，`updated_at timestamp`。
- 索引：`case_case_citations_pkey (id)`，`case_case_citations_citing_case_id_cited_case_id_relation_s_key (citing_case_id, cited_case_id, relation_source)`，`idx_case_case_citations_citing (citing_case_id)`，`idx_case_case_citations_cited (cited_case_id)`。

### `canada_case_law_links`

- 当前记录数：111,391；主键：`id`。
- 字段：`id bigint`，`case_item_id bigint`，`law_id bigint`，`matched_alias text`，`match_source varchar`，`match_score numeric`，`evidence_excerpt text`，`created_at timestamp`，`updated_at timestamp`。
- 索引：`canada_case_law_links_pkey (id)`，`uq_canada_case_law_links_pair (case_item_id, law_id)`，`idx_canada_case_law_links_case (case_item_id)`，`idx_canada_case_law_links_law (law_id)`。

### `case_rule_relations`

- 当前记录数：110,410；主键：`id`。
- 字段：`id bigint`，`case_id bigint`，`rule_id bigint`，`relation_type text`，`match_score numeric`，`match_reason text`，`created_at timestamp`。
- 索引：`case_rule_relations_pkey (id)`，`uq_case_rule_relations_pair (case_id, rule_id)`，`idx_case_rule_relations_case (case_id, match_score DESC)`，`idx_case_rule_relations_rule (rule_id, match_score DESC)`。

### `case_votes`

- 当前记录数：0；主键：`id`。
- 字段：`id bigint`，`case_id bigint`，`user_id bigint`，`vote_type varchar`，`reason text`，`created_at timestamp`，`updated_at timestamp`。
- 索引：`case_votes_pkey (id)`，`case_votes_case_id_user_id_key (case_id, user_id)`，`idx_case_votes_case (case_id)`。

### `item_keywords`

- 当前记录数：5,785,043；主键：`id`。
- 字段：`id bigint`，`item_id bigint`，`keyword varchar`，`created_at timestamp`。
- 索引：`item_keywords_pkey (id)`，`item_keywords_item_id_keyword_key (item_id, keyword)`，`idx_item_keywords_keyword`，`idx_item_keywords_keyword_lower`。

## 4. CSV 与既有关系表的映射评估

### 判例 -> 判例：`case_to_case_links.csv`

- 候选表：`a2aj_case_citations`（保存原始引用文字，可保存未解析目标）和 `case_case_citations`（只保存双方均已解析后的内部案例 ID）。
- 差异：CSV 使用 `case_id`、`cited_case_id` 两个引用字符串；候选表使用 `source_items.id` 或 `legal_cases.id`。可以通过 `source_items.raw_json` 的 A2AJ citation 字段进行查询映射，但 CSV 本身不包含内部 ID。
- 结论：可在确认“以 A2AJ citation 精确解析到内部 ID，无法解析的目标仅写入 `a2aj_case_citations`”后写入；本次未执行。

### 判例 -> 法规：`case_to_statute_links.csv`

- 候选表：`canada_case_law_links`。
- 差异：候选表要求非空 `case_item_id`、`law_id`、`matched_alias`、`match_source`、`match_score`、`evidence_excerpt`，而 CSV 使用案例引用字符串和法规引用字符串；CSV 中另有 27,369 条未匹配法规引用，无法写入 `law_id NOT NULL` 的关系表。
- 结论：已匹配行可以在确认内部 ID 映射及补充字段取值规则后写入；未匹配行没有可承载的既有表。本次未执行。

### 法规 -> 法规：`statute_to_statute_links.csv`

- 候选表：**无**。
- 差异：数据库没有拥有“引用法规 ID”和“被引用法规 ID”两个方向字段的既有表；`canada_case_law_links` 的左侧固定为案例，不能替代法规 -> 法规关系。
- 结论：这是阻止执行的决定性结构不匹配。未创建表、未变更任何既有表。

## 5. 未执行项目

以下操作在发现上述结构不匹配后均未执行：

- 三份 CSV 的追加/冲突跳过写入。
- 缺失索引创建；因此也未向 `canada_laws.citation` 增加索引。
- 悬空引用、覆盖率、孤立节点统计。
- 六类关联查询验证。

## 6. 需要确认的最小决策

要继续且不损失 CSV 中的未匹配关系，需要确认是否允许新增一张法规 -> 法规关系表，并确认未匹配的判例 -> 法规引用应保存在何处。确认后可继续采用既有 `a2aj_case_citations`、`canada_case_law_links` 和新增的法规关系表完成追加、索引和验证；不会清空任何表或修改原始 CSV。
