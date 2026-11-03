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

## 2026-09-21 庭审模拟回归测试

执行命令：`python scripts/test_hearing_workflow.py`

结果：通过。测试使用确定性本地案例和法规候选，关闭 LLM 调用后完整运行七个庭审阶段；每个非最终阶段均产生我方、对方代理人和审判长消息。最终报告成功生成，概率位于 0 至 100 区间且包含置信区间、未校准状态、引用和免责声明；风险输出覆盖实体、程序、证据、执行四类。另已执行 `python -m compileall -q app` 与 `git diff --check`，均通过。

## 2026-09-24 交付约束文档更新验证

变更范围：仅更新 `CODEX.md` 与本节，不涉及应用代码、数据库结构、依赖或运行配置。

验证用例：确认 `CODEX.md` 已明确规定“代码改动后覆盖正常、异常、边界、权限及回归场景并记录测试结果”，且已明确规定“测试通过后单独提交并推送 GitHub”；确认本次推送不会包含工作区既有未提交文件。

执行命令：`git push origin main`；`Get-Content -Encoding UTF8 CODEX.md`；`Get-Content -Encoding UTF8 TEST.md`。

预期结果：远端已包含上一提交 `496cf66 feat: 增加法律 Skill 协作与只读 MCP 工具适配`；两项约束文本可正常以 UTF-8 中文读取；未暂存、提交或推送其他工作区改动。

实际结果：通过。Git 返回 `Everything up-to-date`，说明 `496cf66` 已同步到 `origin/main`；两份文档均已写入并可按 UTF-8 读取。本次为文档治理更新，未执行应用测试；后续任何代码改动必须依照新增约束执行测试、续写本文件、提交并推送。

## 2026-09-25 案情分析深度解析改造验证

变更范围：新增结构化 `QueryPlan`、规则化 `DeepAnalysis`、确定性质量门禁及审计表，接入 `/analysis`、`/api/analyze-search` 和 `/api/analyze`，并新增通过/降级两种展示。`DEEP_ANALYSIS_ENABLED` 默认关闭，旧路径保持不变。

### 自动化专项测试

执行命令：`python scripts/test_deep_analysis.py`

预期：覆盖 Schema 合法/非法值、LLM JSON 校验及规则降级、空输入、超过 10000 字输入、英文输入、SQL/HTML 注入文本、无证据输入、四类风险、强弱评分、质量门禁通过/失败、审计写入、功能开关、未登录 API 拦截、模板完整/降级展示和旧 QueryPlan 调用兼容。

实际：最终 `16` 项全部通过，耗时约 `0.17s`。高质量证据包通过门禁；无证据输入首个降级原因为“引用可追溯率低于阈值”，四类风险均降到 `0.3` 以下并要求人工复核；通过门禁的模板包含争点、风险、评分、缺口和引用，且不包含完整输入原文；失败门禁隐藏完整争点和风险区块。

修复记录：首次直接运行因脚本未把仓库根目录加入 `sys.path` 而失败；修复入口后，第二次运行发现旧 QueryPlan 构造重复传入 `keywords`，以及超长测试样本实际未超过 20000 字符。两项均修正并重新运行，最终无失败，不以失败轮次作为交付结果。

结论：通过。

### 真实数据库案例验证

执行方式：在同一 Python 进程临时设置 `settings.deep_analysis_enabled=True`、`settings.deep_analysis_max_llm_calls=0`，调用 `analyze_sentence_search(..., module='canada', local_only=True)`；没有修改 `.env` 的默认开关。

输入场景：安大略省房东与租客签订租赁合同，支付押金后房屋漏水和霉菌，租客用邮件和照片三次要求维修，房东未处理并发出终止通知，租客请求减租、返还支出和赔偿。

预期：输出不是输入原文复制；事实不少于 5 条、争点不少于 2 条、四类风险各至少 1 条，且引用来自当前数据库；门禁结果可解释。

实际：约 `10.47s` 完成，`deep_analysis_status=ready`；输出 `13` 条结构化事实、`5` 个推理争点、`4` 类风险、`5` 条引用，质量门禁通过。争点包括“维修与适居义务违反是否达到法定救济条件”“合同终止、占有返还或驱逐程序是否合法有效”等，并非输入中的原句。摘要按当事人、时间线、核心行为、诉求与地域、现有材料重新组织。

先验候选性能：数据库候选查询由逐特征重复扫描改为合并查询后，实测约 `0.63s`，返回 `12` 条法规和 `12` 条案例候选；前两条法规为 `Residential Tenancy Act` 与 `Residential Tenancies Act`。候选仅使用数据库 ID，不由 LLM 编造编号。

审计：真实调用创建并写入 `quality_gate_runs`，最新记录 `passed=true`；专项 SQLite 审计测试另验证 `tenant_id='tenant-a'`、`user_id=7` 可写入并查询。

结论：通过。

### 回归与静态验证

- `python -m compileall app scripts`：通过。
- `python scripts/test_agent_skill_collaboration.py`：通过，6 个技能步骤和 4 个只读工具保持正常。
- `python scripts/test_hearing_workflow.py`：通过，7 个庭审阶段完成，风险数 `5`，概率报告 `59.7`；本次未修改庭审服务。
- Jinja 模板加载：`analyze.html` 和 `partials/deep_analysis_sections.html` 均通过；专项测试完成有证据和降级两种实际渲染。
- `git diff --check`：通过，仅输出 Windows 工作区既有 LF/CRLF 提示，无空白错误。
- `scripts/evaluate_case_retrieval.py --help` 与 `scripts/verify_full_vectorization.py --help`：通过，既有 CLI 接口未变。
- 以进程级 `DEEP_ANALYSIS_ENABLED=true` 启动 Uvicorn 后请求 `http://127.0.0.1:8000/health`：返回 `{"status":"ok","app":"Legal Demo MVP"}`，服务可用；未改写 `.env`。

权限与隔离：未登录 `/api/analyze-search` 的测试确认在调用分析服务前返回 `401`。深度分析没有新增按 case 或 tenant 读取数据的接口，仍复用现有登录门禁和 `get_history(..., user_id=...)` 所有者过滤；当前数据库 `search_histories` 无样本，无法执行真实跨用户历史记录的正反样本对照，此项为剩余风险。

未覆盖风险：本机未安装 Playwright，未进行真实浏览器截图和像素级移动端检查；已完成模板加载、实际渲染内容和响应式 CSS 静态验证。真实外部 LLM 调用未执行，测试使用模拟合法/非法 JSON 验证 Schema 接受、精确 `source_span` 校验和失败降级；真实案例验收使用确定性规则路径。功能默认关闭，启用方式为 `DEEP_ANALYSIS_ENABLED=true`。

## 2026-09-27 安大略民事上诉模拟文本版验证

变更范围：新增九阶段安大略民事上诉文本状态机、上诉方/被上诉方/观察者三种练习模式、六类角色与工具边界、案卷和引用核验、逐轮评分、检查点回滚、租户隔离 API、交互页面及训练报告。详细职责见 `docs/ontario_civil_appeal_simulation.md`。

### 专项自动化测试

执行命令：`python scripts/test_appeal_workflow.py`

实际结果：`26` 项全部通过，耗时约 `0.15s`。覆盖三种练习模式完整九阶段顺序、中文与英文审查标准识别、用户发言评分和引用追踪、无依据及检索异常路径、LLM 非法引用降级、未知争点/案卷/依据拒绝、法官越权拒绝、回答必须对应法官问题、上诉答复不得新增争点、已完成流程禁止继续、合法及非法回滚、租户隔离、未登录页面/API、空值和超长输入、SQL/HTML 文本按数据处理、工具白名单越权、无数值胜率输出。

修复记录：浏览器首次使用中文“采用了错误的损害赔偿法律测试”时，该争点被归为混合问题。补充“法律测试、法律适用、适用法律、法律原则、解释法律”等中文法律问题表达，并增加回归用例后，结果修正为 `question_of_law / correctness`；事实认定争点保持 `mixed_fact_and_law / palpable_and_overriding_error`。

### 真实浏览器端到端测试

测试环境：隔离 SQLite 数据库、隔离测试账号、Microsoft Edge 153 无头模式；服务地址 `http://127.0.0.1:8012`。输入为安大略住宅装修合同上诉案例，包含两项上诉理由和三项额外案卷材料。

实际结果：登录、创建模拟和九阶段推进全部成功，共产生 `10` 条庭审消息、`5` 个代理人评分，最终九个阶段均为完成状态并显示训练报告。桌面视口 `1416x1108` 与移动视口 `390x844` 均无页面横向溢出；桌面可见控件重叠数为 `0`，移动端阶段条使用横向滚动。最终报告显示案卷可追溯率、法律依据可追溯率、争点覆盖率、角色合规率和修正方案；未出现数值胜诉概率，只显示不预测案件结果的免责声明。

隔离数据库未装载加拿大法律语料，因此本次浏览器测试检索到的候选法律依据为 `0`。流程按设计继续并明确提示“没有检索到候选法律依据”，法律依据可追溯率为 `0%`，修正方案要求补充可核验判例或法规。带两项固定候选依据的自动化测试另行验证了有依据路径。

截图保存在 `tmp/appeal-desktop-initial.png`、`tmp/appeal-desktop-report.png` 和 `tmp/appeal-mobile-final.png`。

### 回归与静态检查

- `python -m compileall -q app scripts`：通过。
- `python scripts/test_deep_analysis.py`：`16` 项通过。
- `python scripts/test_agent_skill_collaboration.py`：通过，`6` 个技能步骤和 `4` 个只读工具正常。
- `python scripts/test_hearing_workflow.py`：既有七阶段庭审流程通过。
- 应用导入：标题为 `Legal Demo MVP`，路由总数 `111`；新增页面与四个上诉 API 均已注册。
- `git diff --check`：通过，仅有工作区既有 LF/CRLF 提示。

剩余风险：候选依据的时效性依赖本地法律语料更新时间，当前文本版未接入在线效力核验；外部 LLM 的真实网络调用未作为稳定测试条件，专项测试通过合法/非法结构化响应和无模型确定性路径验证边界。有限测试不能穷举所有法律事实组合，当前覆盖的是正常、异常、边界、权限、隔离、回滚、降级、注入文本和响应式布局等可重复验证类别。

## 2026-09-29 Hryniak 真实案例与卡住问题修复验证

变更范围：使用 `Hryniak v. Mauldin, 2014 SCC 7` 的真实公开判决、双方 factum、Rule 20、`Combined Air` 和 `Housen` 构造可追溯测试案卷；补充持久化模拟恢复入口、案卷/法律依据工具审计、受超时保护的浏览器端到端执行器，以及 SQLite `user_profiles` 初始化缺失修复。完整测评见 `docs/appeal_real_case_hryniak_evaluation.md`。

### 卡住原因与失败回归

原临时 Uvicorn PID `40860` 从 2026-09-28 16:50 起一直监听 `8016`，日志只有启动完成且没有后续请求。外围执行链未设置总超时、阶段错误结果和 `finally` 清理，导致后台服务存活但任务没有推进。进程已按 PID 终止并确认端口关闭。

有界执行器第一次运行在约 4 秒内按预期失败并清理服务，错误为全新 SQLite 缺少 `user_profiles`，导致 `/api/auth/register` 返回 `500`。修复表初始化后，同一执行器完成全流程，并在日志中明确记录 Edge 和 Uvicorn 均已停止。

### 执行命令与结果

- `python scripts/test_sqlite_auth_init.py`：2 项通过，覆盖全新数据库注册写入用户扩展资料，以及重复初始化幂等性。
- `python scripts/test_appeal_real_case_hryniak.py`：1 项通过，覆盖 5 项真实来源依据、九阶段完成、引用追踪、角色工具边界和无胜诉概率输出。
- `python scripts/test_appeal_workflow.py`：26 项通过，既有正常、异常、边界、权限、租户隔离、回滚和降级路径无回归。
- `python scripts/run_appeal_real_case_e2e.py --startup-timeout 25 --request-timeout 15 --browser-timeout 30`：通过，最终一轮约 9 秒完成；九阶段均执行，服务与浏览器均自动关闭。
- `python -m compileall -q app scripts`：通过。

### 端到端实际结果

初始页面显示 3 项上诉问题、9 项案卷记录和 5 项候选法律依据；最终产生 10 条消息、5 次代理人评分和 39 条 Agent 工具审计记录。桌面 `1416x1108` 与移动 `390x844` 均无横向溢出，九个阶段均显示完成，训练报告可见。案卷可追溯率和法律依据可追溯率均为 `100%`，争点覆盖率为 `66.7%`，角色合规率为 `100%`；我方上诉律师为 `80.3`，被上诉方律师为 `90.5`。

截图保存于 `docs/assets/appeal-real-case-hryniak-initial.png`、`docs/assets/appeal-real-case-hryniak-final.png`、`docs/assets/appeal-real-case-hryniak-report.png` 和 `docs/assets/appeal-real-case-hryniak-mobile.png`。

剩余风险：公开网页内容由外部检索后以带 URL 的快照注入测试案卷，庭审 Agent 只读取快照，尚未在产品内自主调用实时 `web_search/web_fetch`；法规历史版本与判例后续效力仍需专业 citator 和人工复核。评分衡量训练表现，不代表真实案件胜诉概率或法院意见。

## 2026-09-29 安大略民事一审举证质证流程验证

变更范围：新增独立的安大略民事事实审理状态机和 `/api/civil-trial` 接口；`/hearing` 改为多庭次审判工作台。新流程包含双方开庭陈述、逐名证人主询问/交叉询问/有限复询、专家资格审查、证物提交、异议与法官裁定、自动休庭和续庭、回复证据审查、结案陈词、逐争点判决及独立律师训练分。旧 `/api/hearing` 和上诉模块保留。完整设计和截图见 `docs/安大略民事审判模拟测试报告.md`。

### 专项服务测试

执行命令：`python scripts\test_civil_trial_workflow.py`

预期：具体案卷跨多个庭次完成；证物覆盖准入、限缩准入和排除；休庭期间禁止普通推进；原告、被告、事实证人、专家、书记员、证据核验、法官和评估代理均留下受白名单约束的工具轨迹；跨租户读取和证人检索越权被拒绝；缺少一方证人、未知基础证人和完成后继续推进被拒绝；判决不输出胜诉概率或算法证据分。

实际：6 项测试全部通过，约 `0.18s`。完整流程使用 4 名证人和 7 件证物，形成 4 个庭次、89 条庭审事件、21 条证言回答和 3 次异议裁定；证物结果为准入 5、限缩准入 1、排除 1。三个争点均生成证明责任、采信/排除材料和可信性理由，判决为原告部分胜诉。角色越界计数为 0；原告和被告律师训练分分别为 97 和 91，且评分明确不参与判决。另验证未登录 API 在业务服务调用前返回 401，页面包含证言和法律依据入口且不显示胜诉概率。

结论：正常、异常、状态边界、角色权限和租户隔离路径通过。

### 浏览器端到端测试

执行命令：`python scripts\run_civil_trial_e2e.py`

环境：隔离 SQLite、隔离账号、Microsoft Edge 无头模式；每次使用随机空闲端口，并为启动、HTTP 请求、浏览器等待和最多 80 次流程动作设置上限；`finally` 中关闭 Edge 和 Uvicorn。

第一次在文件沙箱内启动 Edge 时约 19 秒失败，错误为临时浏览器配置目录访问被拒绝。服务端日志显示注册和创建庭审均返回 200，且浏览器未向 `/hearing` 发出请求，确认不是工作流卡死。按环境权限要求在沙箱外运行同一有界命令后通过，最后一次约 8 秒完成并明确停止两个进程。

实际：47 次推进/续庭动作后完成 4 个庭次。页面显示 89 条庭审记录、4 名完成作证的证人、3 项逐争点认定和 2 份律师评分。七个阶段均为完成；完成态发言表单已隐藏。桌面 `1440x1100` 和移动端 `390x844` 均无横向溢出，移动阶段条使用横向滚动。截图保存于：

- `docs/assets/civil-trial-northlake-initial.png`
- `docs/assets/civil-trial-northlake-evidence.png`
- `docs/assets/civil-trial-northlake-final.png`
- `docs/assets/civil-trial-northlake-judgment.png`
- `docs/assets/civil-trial-northlake-mobile.png`

### 回归与静态检查

- `python -m compileall -q app scripts`：通过。
- `python scripts\test_hearing_workflow.py`：旧七阶段接口通过，最终规则报告为 59.7；用于兼容回归，不再作为 `/hearing` 主页面。
- `python scripts\test_appeal_workflow.py`：26 项通过，固定九阶段、权限、租户隔离、回滚和无胜诉概率输出均无回归。
- `python scripts\test_appeal_real_case_hryniak.py`：1 项通过，真实 Hryniak 上诉案卷和来源可追溯测试无回归。

剩余风险：该确定性案卷没有穷尽陪审团审判、缺席审判、证人特权、翻译证人、远程出庭或复杂专家会议。实体法律依据仍依赖本地 RAG 语料更新；程序规则提供官方 URL，但法规历史版本和后续效力仍需专业 citator 与律师复核。用户提供的知乎文章在当前环境返回反爬验证页，未把无法读取的正文当作已核验来源；流程以用户明确要求和安大略法院官方资料为依据。

## 2026-10-12 用户偏好与复盘摘要专项验证

变更范围：移除“用户复盘 Agent”的产品与架构定位，新增独立的数据库偏好服务。系统只保存去标识化的提交次数、表达长度、事实/证据编号绑定数量、法域和规则归类后的复盘重点；使用确定性代码聚合历史摘要，不调用 LLM，也不向代理 Agent 或法官上下文注入画像。个人资料弹窗支持查看摘要、修正显式偏好、暂停记录和清除历史。

执行与结果：

- `python -m compileall -q app tests`：通过。
- `python -m unittest discover -s tests -v`：38 项全部通过，耗时约 `4.76s`。
- `python scripts/test_user_preference_api.py`：1 项通过，耗时约 `0.13s`；隔离 SQLite 中完成注册会话、偏好读取、开启记录、显式偏好编辑、页面访问和历史清除。
- 新增的 4 项专项测试全部通过：原始发言不进入观察记录、跨次聚合正确、同一运行重复处理保持幂等、显式偏好可修正、关闭记录后不新增观察、历史可清除。
- `base.html` 与 `agent_run.html` 目标内联脚本经 Node.js 语法解析：通过。
- FastAPI 应用导入与 OpenAPI 生成：通过；`/api/users/me/preferences`、`/api/users/me/preferences/tracking` 和 `/api/users/me/preferences/history` 路径均已注册。

中间状态：首次运行新增测试时，中文样例字符数断言写为 15，实际 Unicode 字符数为 14；核对后确认业务统计正确并修正测试断言。API 测试第一次因使用现有初始化器不支持的 `sqlite+pysqlite` URL 前缀而无法打开临时库，改用项目支持的 `sqlite:///`；第二次所有业务断言通过但 Windows 临时文件仍被数据库连接占用，增加 `engine.dispose()` 后第三次正常退出。最终联合验证无失败项。

完整设计、数据边界和页面说明见 `docs/用户偏好与复盘摘要改造.md`。

## 2026-11-03 加拿大法律 RAG 上下文分块、多跳检索与本地 HNSW 升级

### 变更与边界

移除历史非加拿大审判遗留、旧近似索引模块及旧重叠切分；实现 `legal-context-v2`、保守消歧、证据化实体关系、一至三跳检索、HNSW-only 查询保护以及查询级 SSE 进度。执行范围只包含本机代码、隔离样本和全量本机 HNSW 构建；未执行整库上下文回填、远端迁移或部署。

### 可复现命令

```powershell
$template = Get-Content app\templates\analyze.html -Raw -Encoding utf8
$script = [regex]::Match($template, '<script>([\s\S]*?)</script>').Groups[1].Value
$script | node --check
python -m compileall -q app scripts tests
python -m unittest discover -s tests -v
python scripts\build_hnsw_index.py --monitor
python scripts\build_hnsw_index.py --plan --m 16 --ef-construction 64
```

隔离样本实际执行：使用 `BUILD_HNSW_SAMPLE` 确认令牌从现有 BGE-M3 向量复制 5,000 条至非日志评测表，建立真实 pgvector HNSW，并对三个跨领域加拿大法律查询分别运行精确余弦与 HNSW 查询计划。

### 预期与实际

- 预期：全部 Python 可编译；前端脚本无语法错误。实际：通过。
- 预期：正常、异常、边界、用户隔离、加拿大法域限制和既有功能回归均通过。实际：55/55 项测试通过，耗时 1.751 秒。
- 预期：章节是硬边界，标题和正文仍可引用；唯一别名可解析，多主体时不猜测。实际：专项测试通过。
- 预期：图路径保留关系来源、遍历方向和 chunk 证据。实际：正向路径专项测试通过。
- 预期：无 valid/ready HNSW 时不得对大表隐式顺序扫描。实际：返回 `ann_unavailable`，且不会调用 query embedding；专项测试通过。
- 预期：查询完成即推送进度，人工补查只消费一次，取消成为真正终态。实际：专项测试通过。
- 预期：API 仅暴露新版 HNSW 与流式接口。实际：OpenAPI 共 139 条路径，必需路径全部存在，旧索引路径为 0。
- 预期：隔离样本 Recall@10 不低于 0.90，查询计划使用 HNSW 且没有顺序扫描。实际：三个查询 Recall@10 均为 1.00；平均精确检索 21.611ms，平均 HNSW 6.318ms，延迟降低 70.8%，约 3.42 倍加速；全部使用 HNSW 且无顺序扫描。

### 首次失败与修正

第一次全量 HNSW 构建受数据库会话 `statement_timeout` 影响，在约 12 秒后失败并留下 invalid 索引。已只清理该无效目标索引，在构建会话显式设置 `statement_timeout=0` 后重新启动。回归过程中新增的图方向测试最初错误地要求每个去重后的结果都必须大于 0 跳；实际入口实体可直接命中证据。断言已改为直接验证遍历器的有向边元数据，随后全套测试通过。

### 当前状态与未覆盖风险

全量 HNSW 仍在本机后台构建，构建完成前 `valid=false/ready=false` 属正常状态，应用不会使用半成品。完成后必须补做全量 `EXPLAIN`、真实混合检索、加拿大 30+ 条/8+ 领域评测和页面端到端测试。当前 5,000 条样本不能替代全量业务准确率；结构化分块和多跳推理的提升幅度必须在整库 `legal-context-v2` 回填后用同语料、同模型、同查询的旧单轮基线做 A/B 才能最终确认。
