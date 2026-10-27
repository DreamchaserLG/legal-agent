/* ============================================================
   英文界面补译引擎 (i18n-en.js)
   - 只在 <html lang> 以 en 开头时生效（中英切换 = /lang/en）
   - 精确短语词典 + 锚定正则规则，两类都不命中则保持原文
   - 只改写界面文案；检索/案情/法规正文等动态内容不会被改写
   - 覆盖运行时 JS 渲染内容（MutationObserver）
   版本：2026-10-27
   ============================================================ */
(() => {
    const lang = (document.documentElement.getAttribute('lang') || '').toLowerCase();
    if (!lang.startsWith('en')) return;
    if (window.__I18N_EN_ACTIVE__) return;
    window.__I18N_EN_ACTIVE__ = true;

    /* ---------- 精确短语词典 ---------- */
    const DICT = {
        // base / shell
        '个人信息': 'Profile', '关闭': 'Close', '用户名': 'Username', '角色': 'Role',
        '注册时间': 'Registered', '最近登录': 'Last login', '修改资料': 'Edit profile',
        '姓名': 'Full name', '邮箱': 'Email', '手机号': 'Mobile', '组织': 'Organisation',
        '国家偏好': 'Country preference', '法律类型偏好': 'Legal-area preference', '备注': 'Notes',
        '保存资料': 'Save profile', '修改密码': 'Change password', '当前密码': 'Current password',
        '新密码': 'New password', '确认新密码': 'Confirm new password', '更新密码': 'Update password',
        '偏好与复盘摘要': 'Preferences and review summary', '我的使用习惯': 'My usage profile',
        '记录去标识化统计': 'Record de-identified statistics',
        '这里只汇总表达长度、证据绑定数量和复盘类别，不保存原始案情，不参与代理或法官判断。':
            'Only expression length, evidence-binding counts and review categories are aggregated. Raw case text is never stored, and none of this feeds counsel or judge decisions.',
        '已复盘': 'Reviews completed', '表达习惯': 'Communication style', '暂无足够数据': 'Not enough data yet',
        '证据绑定': 'Evidence bindings', '事实绑定': 'Fact bindings', '期望回复方式': 'Preferred reply style',
        '简洁': 'Concise', '均衡': 'Balanced', '详细': 'Detailed', '重点关注（用逗号分隔）': 'Focus areas (comma separated)',
        '自定义说明': 'Custom note', '保存偏好': 'Save preferences', '清除摘要与历史': 'Clear summary and history',
        '常见复盘重点': 'Frequent review topics', '暂无记录': 'No records yet',
        '针对性建议': 'Targeted suggestions', '完成复盘后将在此显示建议': 'Suggestions appear here after your first review',
        '法律检索助手': 'Legal Research Assistant', '正在分析案情': 'Analysing the case',
        '系统正在整理本地案例、法律法规和分析结果，请稍候。': 'The system is compiling local cases, legislation and analysis results. Please wait.',
        '准备请求': 'Preparing request', '请不要重复提交': 'Please do not resubmit',
        '确定清除全部偏好摘要和去标识化历史吗？此操作不可恢复。': 'Clear all preference summaries and de-identified history? This cannot be undone.',
        '偏好摘要与历史已清除': 'Preference summary and history cleared', '清除失败': 'Failed to clear',
        '例如：证据完整性，合同履行': 'e.g., completeness of evidence, contract performance',
        '可随时修改；只用于个人查看。': 'Editable at any time; visible only to you.',
        '模型已就绪': 'Model ready', '本地检索模式': 'Local retrieval mode',

        // analyze
        '结构化梳理案情，再进入庭审模拟': 'Structure the case record, then move into hearing simulation',
        '这里优先整理案件概览、关键事实、争议焦点、法律关系、证据关注点和风险点，随后通过固定庭审阶段完成受控评估。':
            'Case overview, key facts, disputed issues, legal relations, evidence focus and risk points are organised first, then assessed through fixed hearing stages.',
        '如果同一案情已经保存过结果，系统会直接复用已有记录，不再重复消耗分析时间。':
            'If this case has been analysed before, the saved record is reused instead of re-running the analysis.',
        '研究模块': 'Research module', '案情输入': 'Case description', '结果数量': 'Result count',
        '数据源': 'Data source', '排序方式': 'Sort order', '相关度优先': 'Relevance first', '时间优先': 'Most recent first',
        '开始分析': 'Start analysis', '进入庭审模拟': 'Open hearing simulation',
        '历史复用': 'Reused from history', '本地分析': 'Local analysis', '已完成': 'Completed',
        '分析结果': 'Analysis result', '本次案情已完成结构化分析': 'This case has been structured successfully',
        '当前结果基于本地案例、法律法规和模型推理生成，不会实时抓取远程网页。':
            'Results are produced from local cases, legislation and model reasoning; no live remote crawling is performed.',
        '命中已有历史记录，系统已直接复用原记录，不会新增重复历史。':
            'A saved record was matched, so the existing result was reused and no duplicate history was created.',
        '当前案情': 'Current case', '查看历史详情': 'View history detail', '导出 PDF': 'Export PDF',
        '案情概览': 'Case overview', '未知风险': 'Unknown risk', '查看原始案情输入': 'View original case text',
        '关键事实': 'Key facts', '当前没有提取到稳定的关键事实。': 'No stable key facts were extracted.',
        '争议焦点': 'Disputed issues', '当前没有提炼出明确的争议焦点。': 'No clear disputed issues were identified.',
        '法律关系': 'Legal relations', '当前没有整理出明确的法律关系条目。': 'No clear legal relations were organised.',
        '证据关注点': 'Evidence focus', '当前没有额外的证据提示。': 'No additional evidence notes.',
        '风险点分析': 'Risk analysis', '当前没有单独提炼出的风险点。': 'No standalone risk points were extracted.',
        '下一步': 'Next step', '结构化分析已完成': 'Structured analysis complete',
        '当前案情已完成结构化梳理，包括关键事实、争议焦点、法律关系和风险点。':
            'The case has been structured, including key facts, disputed issues, legal relations and risk points.',
        '如需形成最终辅助评估，请进入庭审模拟，完成陈述、举证、质证、辩论和评议。':
            'For a final assisted assessment, open hearing simulation and complete openings, evidence, cross-examination, argument and deliberation.',
        '相关法律法规与案例': 'Relevant legislation and cases', '与本案相关的法律法规和案例': 'Legislation and cases related to this case',
        '关联法规': 'Linked legislation', '相关案例': 'Related cases',
        '例如：租赁合同纠纷、劳动解除争议、侵权损害赔偿，或 OFAC 制裁合规风险。':
            'e.g., a tenancy dispute, a wrongful-dismissal dispute, tort damages, or OFAC sanctions compliance risk.',
        '上一条相关案例': 'Previous related case', '下一条相关案例': 'Next related case', '相关案例滑动列表': 'Related case slider',

        // predict
        '从律师的判断顺序出发，给出可读的分析与预测结论': 'Follows a lawyer’s reasoning order to produce a readable analysis and outlook',
        '同一案情再次输入时，系统会优先复用已保存结果，减少重复等待和重复消耗。':
            'Re-entering the same case reuses the saved result, avoiding repeated waiting and cost.',
        '案情描述': 'Case description', '开始预测': 'Run outlook', '返回案情分析': 'Back to case analysis',
        '本地预览': 'Local preview', '本次预测所依据的案情信息': 'Case information behind this outlook',
        '查看已保存记录': 'View saved record', '切换到案情分析页': 'Go to case analysis',
        '数据可靠性': 'Data reliability', '当前数据不足以支撑可靠预测': 'Current data cannot support a reliable outlook',
        '当前数据可支撑研究型判断': 'Current data can support a research-grade assessment',
        '可疑全文': 'Suspicious full text', '事实摘要': 'Fact summary',
        '当前没有提炼出独立事实摘要。': 'No standalone fact summary was extracted.',
        '请求事项': 'Relief sought', '当前没有识别出明确请求事项。': 'No clear relief was identified.',
        '关键词': 'Keywords', '预测依据': 'Basis of the outlook',
        '本次预测使用的案情分析案例与法律法规': 'Cases and legislation used by this outlook',
        '以下案例和法规来自案情分析阶段的命中结果，预测结论会优先围绕这些材料展开，而不是脱离案例空凭判断。':
            'The cases and legislation below come from the analysis stage; the outlook is built around them rather than from unsupported speculation.',
        '推理过程': 'Reasoning', '律师视角的判断路径': 'How a lawyer would reason through it',
        '这里只展示可直接阅读的判断步骤，不展示系统内部完整推理链。':
            'Only readable reasoning steps are shown, not the full internal chain.',
        '预测结论': 'Outcome outlook', '可靠性警告': 'Reliability warning', '证据质量': 'Evidence quality',
        '结论说明': 'Conclusion notes', '支持判断的要点': 'Points supporting the assessment', '不确定因素': 'Uncertainties',
        '页面操作': 'Page actions', '下载 PDF 报告': 'Download PDF report', '预测标签': 'Outlook label',
        '置信度': 'Confidence', '法域': 'Jurisdiction', '未识别': 'Not identified', '未明确请求事项': 'No clear relief specified',
        '命中支持案例数': 'Supporting cases matched', '执行摘要': 'Executive summary',
        '当前案情命中已有结构化分析记录，系统已复用原结果并更新到同一条历史中。':
            'An existing structured analysis was matched and reused, updating the same history record.',
        '当前预测命中已有预测记录，系统已直接复用上次结果，避免重复等待。':
            'An existing outlook was matched, so the previous result was reused to avoid waiting.',
        '例如：家庭成员围绕遗嘱效力、房产处分与信托义务发生争议；或一家公司因涉嫌帮助他人规避 OFAC 制裁而面临调查。':
            'e.g., family members disputing will validity, disposition of property and trustee duties; or a company investigated for allegedly helping another party evade OFAC sanctions.',
        '上一条预测依据案例': 'Previous supporting case', '下一条预测依据案例': 'Next supporting case', '预测依据案例滑动列表': 'Supporting case slider',

        // histories
        '我的案件历史记录': 'My case history',
        '同一案情会自动去重，只保留一条有效历史记录。你可以在这里查看列表，也可以切换到图谱视图。':
            'Duplicate cases are merged into one valid record. Browse the list here, or switch to the graph view.',
        '列表视图': 'List view', '图谱视图': 'Graph view', '案件类型': 'Case type', '国家': 'Country',
        '法院级别': 'Court level', '法律类型': 'Legal area', '筛选记录': 'Filter records', '新建案情分析': 'New case analysis',
        '待预测': 'Outlook pending', '当前还没有稳定的预测结论': 'No stable outlook yet', '尚未进行预测': 'Outlook not generated',
        '查看详情': 'View detail', '删除': 'Delete', '首页': 'First', '上一页': 'Previous', '下一页': 'Next', '末页': 'Last',
        '当前没有符合条件的历史记录': 'No history record matches the filters',
        '提交一次案情分析后，这里会保存去重后的案件历史，并可切换到图谱视图查看它们之间的关联。':
            'After one case analysis, the de-duplicated history is saved here and can be explored in the graph view.',
        '删除失败': 'Delete failed', '合同 / 劳动 / 侵权': 'Contract / Employment / Tort',

        // history detail
        '历史详情': 'History detail',
        '这里展示的是已经保存的结构化分析与预测结果。打开本页不会重新预测，也不会新增历史记录。':
            'This page shows the saved structured analysis and outlook. Opening it does not re-run the outlook or create a new record.',
        '记录概览': 'Record overview', '风险等级': 'Risk level', '创建时间': 'Created', '最近访问': 'Last viewed',
        '访问次数': 'Views', '通过庭审模拟重新评估': 'Re-assess via hearing simulation', '返回列表': 'Back to list',
        '查看图谱': 'Open graph', '删除此记录': 'Delete this record', '已保存结果': 'Saved result',
        '结构化分析与预测结果': 'Structured analysis and outlook', '当前没有保存关键事实条目。': 'No key facts were saved.',
        '当前没有保存争议焦点条目。': 'No disputed issues were saved.', '当前没有单独整理出的法律关系。': 'No separate legal relations were organised.',
        '当前没有保存风险点。': 'No risk points were saved.', '预测结果': 'Outlook', '综合判断': 'Overall assessment',
        '当前没有稳定的预测结论。': 'No stable outlook yet.', '当前记录还没有完整预测': 'This record has no complete outlook',
        '这条历史记录保留了结构化分析结果，但还没有完整预测结论。': 'This record keeps the structured analysis but has no complete outlook yet.',
        '进入预测页后，系统会在现有分析基础上补齐预测结论、相关法规和支撑案例。':
            'Open the outlook page to complete the conclusion, linked legislation and supporting cases on top of the existing analysis.',
        '可继续预测': 'Continue to outlook', '相关法规': 'Related legislation', '涉及法律法规': 'Legislation involved',
        '法规': 'Legislation', '当前没有保存条文摘要。': 'No provision summary was saved.', '法规详情': 'Provision detail',
        '原始来源': 'Original source', '当前记录没有保存法规锚点': 'This record has no legislation anchor saved',
        '后续在预测页重新评估后，系统会自动补齐法规信息并更新这条历史记录。':
            'Re-assessing on the outlook page will backfill legislation and update this record automatically.',
        '案例与法规对应': 'Cases mapped to legislation', '相关案例与对应法规': 'Related cases and their legislation',
        '每条案例都放在独立大框内，左侧是案例，右侧是与该案例对应的法规。':
            'Each case sits in its own panel: the case on the left, its linked legislation on the right.',
        '国家 / 联邦': 'National / Federal', '国家 / 联邦案例': 'National / Federal cases', '案例': 'Case',
        '加拿大': 'Canada', '对应法律法规': 'Linked legislation', '来源链接': 'Source link',
        '地区 / 地方': 'Provincial / Local', '地区 / 地方案例': 'Provincial / Local cases',
        '当前记录还没有补齐相关案例': 'Related cases have not been completed for this record',
        '进入预测页后，系统会在本地案例库中检索与当前案情对应的案例，并把它们归到相关法规下面。':
            'Opening the outlook page searches the local case library for matching cases and groups them under the relevant legislation.',
        '当前记录暂时没有保存相关案例': 'No related cases are saved for this record',
        '如果需要补齐案例代表，可以把当前案情带入预测页重新评估。':
            'To add representative cases, take this case into the outlook page and re-assess it.',
        '赞成': 'Agree', '反对': 'Disagree', '请输入赞成或反对的理由...': 'Explain why you agree or disagree...',
        '添加理由': 'Add reason', '取消': 'Cancel', '提交': 'Submit',

        // graph
        '知识图谱': 'Knowledge graph', '案情关系知识图谱': 'Case relationship knowledge graph',
        '节点 = 历史案情，连线 = 案情关联。拖拽节点、滚轮缩放、拖拽画布平移。':
            'Nodes = saved cases, edges = case relations. Drag nodes, scroll to zoom, drag the canvas to pan.',
        '法律法规': 'Legislation', '开始日期': 'Start date', '结束日期': 'End date', '刷新图谱': 'Refresh graph',
        '拖拽节点': 'Drag nodes', '滚轮缩放': 'Scroll to zoom', '拖画布平移': 'Drag canvas to pan',
        '按案件类型': 'By case type', '按风险等级': 'By risk level', '按国家': 'By country', '重置': 'Reset',
        '节点': 'Nodes', '关系': 'Relations', '图例': 'Legend', '高风险': 'High risk', '中风险': 'Medium risk',
        '低风险': 'Low risk', '节点大小': 'Node size', '访问较少': 'Rarely viewed', '访问适中': 'Moderately viewed',
        '访问频繁': 'Frequently viewed', '当前没有可展示的知识图谱节点': 'No graph node to display',
        '先完成至少一条案情分析，再回来查看图谱关系。': 'Complete at least one case analysis, then return to explore the graph.',
        '未命名': 'Untitled', '未知': 'Unknown', '类型': 'Type', '法院': 'Court', '预测': 'Outlook', '风险点': 'Risk points',
        '点击查看详情': 'Click to view detail', '加拿大 / 美国': 'Canada / United States',
        '国家 / 联邦 / 地区 / 地方': 'Federal / National / Regional / Local', '法规名称关键词': 'Legislation name keywords',
        '搜索节点...': 'Search nodes...',

        // law detail
        '当前页面展示该法律法规下全部已建立关系的案例，并按国家/联邦与省级/地方分栏。':
            'This page lists every linked case under this instrument, split into national/federal and provincial/local columns.',
        '法规概览': 'Instrument overview', '总案例数': 'Total cases', '省级 / 地方': 'Provincial / Local',
        '查看条文内容': 'View provision text', '查看法规原文': 'Open original text', '返回上一页': 'Go back', '返回首页': 'Go home',
        '关联案例': 'Linked cases', '该法规下的全部已关联案例': 'All cases linked to this instrument',
        '查看案例': 'View case',

        // appeal
        '安大略民事上诉模拟': 'Ontario Civil Appeal Simulation',
        '基于固定案卷完成上诉方陈述、被上诉方回应、合议庭提问和有限答复。':
            'Work through appellant submissions, respondent responses, panel questions and a limited reply, all on a fixed record.',
        '案件名称': 'Case name', '案情摘要': 'Case summary', '原审决定及主要理由': 'Decision below and main reasons',
        '上诉理由': 'Grounds of appeal', '请求裁定': 'Order sought', '其他案卷材料': 'Other record materials',
        '练习角色': 'Practice role', '上诉方': 'Appellant', '被上诉方': 'Respondent', '观察完整模拟': 'Observe the full simulation',
        '建立上诉模拟': 'Create appeal simulation', '上诉记录': 'Appeal record', '案卷审查': 'Record review',
        '进行中': 'In progress', '庭审文字记录': 'Hearing transcript', '陈述与合议庭问题': 'Submissions and panel questions',
        '本轮陈述': 'This round’s submission', '本轮处理的上诉问题': 'Grounds addressed this round', '引用案卷': 'Record citations',
        '引用法律依据': 'Authority citations', '上诉问题': 'Grounds of appeal', '案卷边界': 'Record boundary',
        '候选法律依据': 'Candidate authorities', '逐轮评分': 'Per-round scoring', '引文待核验': 'Citation pending verification',
        '没有检索到候选法律依据。': 'No candidate authority was retrieved.', '暂无法律依据候选。': 'No authority candidate yet.',
        '完成代理人发言后显示评分。': 'Scores appear after counsel submissions.', '训练反馈': 'Training feedback',
        '安大略民事上诉表现报告': 'Ontario civil appeal performance report', '修正方案': 'Corrective actions',
        '每行填写一项上诉理由': 'One ground per line', '每行填写一项已经进入原审案卷的材料': 'One record item per line',
        '上诉审理阶段': 'Appeal stages', '回滚到此阶段': 'Roll back to this stage',
        '书记员': 'Court clerk', '上诉方代理人': 'Appellant counsel', '被上诉方代理人': 'Respondent counsel',
        '合议庭': 'Panel', '评估服务': 'Evaluation service', '系统': 'System',
        '正在建立案卷并检索候选法律依据。': 'Building the record and searching candidate authorities.',
        '上诉模拟已建立。': 'Appeal simulation created.',
        '正在核验角色边界、案卷引用和法律依据。': 'Checking role boundaries, record citations and authorities.',
        '本阶段已完成。': 'Stage completed.', '正在恢复上诉模拟。': 'Restoring the appeal simulation.',
        '上诉模拟已恢复。': 'Appeal simulation restored.', '模拟已完成': 'Simulation complete', '提交本轮陈述': 'Submit this round',
        '请求失败': 'Request failed',

        // module sections
        '当前输入对应的法律法规': 'Legislation matching this input',
        '按关联案例数量和关键词匹配度排序，高亮显示最相关的法条。':
            'Sorted by linked-case count and keyword match, with the most relevant provisions highlighted.',
        '法规类型': 'Instrument type', '全部': 'All', '法域': 'Jurisdiction',
        '悬停或点击查看条文内容与按法院归类的关联案例': 'Hover or click to view the provision text and linked cases grouped by court',
        '法规内容': 'Provision text',
        '当前记录未提供摘要，请打开法规原文核验。': 'No summary is stored; open the original instrument to verify.',
        '查看法规详情': 'View instrument detail', '核验来源': 'Verify source', '对应案例 · 按法院': 'Linked cases · by court',
        '案例记录': 'Case record', '暂无已核验的关联案例。': 'No verified linked case.',
        '当前筛选条件下没有匹配的法规，请调整分类或关键词。':
            'No legislation matches the current filters. Adjust the category or keywords.',
        '当前没有识别出明确的法规锚点': 'No clear legislation anchor was identified',
        '可以补充更具体的争议焦点、法域、法院级别，或者直接补充你怀疑适用的法规名称。':
            'Add a more specific disputed issue, jurisdiction, court level, or the name of the instrument you suspect applies.',
        '案例与法规': 'Cases and legislation', '已建立关系的案例与法律法规': 'Cases and legislation with established links',
        '待确认关联': 'Relation pending confirmation', '支持观点': 'Supporting arguments', '反对观点': 'Opposing arguments',
        '未找到与当前案情匹配的案例及法律法规': 'No case or legislation matches this case',
        '请尝试补充更多案情信息，或者继续扩充本地案例和法规数据。':
            'Add more case detail, or extend the local case and legislation dataset.',
        '规则': 'Rules', '对应的 OFAC 规则与处理路径': 'Applicable OFAC rules and paths', '相关材料': 'Related materials',
        'OFAC 相关材料与处理路径': 'OFAC materials and response paths', '查看材料': 'View material', '查看来源': 'View source',
        '检索法规名称 / 条文 / 关键词': 'Search instrument name / provision / keyword',
        '检索案例名称 / 摘要 / 关键词': 'Search case name / summary / keyword',
        '上一条国家或联邦案例': 'Previous national/federal case', '下一条国家或联邦案例': 'Next national/federal case',
        '国家或联邦案例滑动列表': 'National/federal case slider', '上一条地区或地方案例': 'Previous provincial/local case',
        '下一条地区或地方案例': 'Next provincial/local case', '地区或地方案例滑动列表': 'Provincial/local case slider',
        '展开法规与案例详情': 'Expand instrument and case details',

        // search sections
        '最近一次同步': 'Last sync', '法规结果': 'Legislation results', '法规命中': 'Legislation matches',
        '查看法规页面': 'Open instrument page', '查看官方来源': 'Open official source', '案例结果': 'Case results',
        '案例命中': 'Case matches', 'OFAC 记录命中': 'OFAC record matches', '查看官方页面': 'Open official page',
        '其他命中材料': 'Other matched materials', '查看记录': 'View record',

        // deep analysis
        '证据化深度解析': 'Evidence-backed deep analysis', '案情焦点、证据边界与风险评估': 'Case focus, evidence boundaries and risk assessment',
        '质量门禁通过': 'Quality gate passed', '降级展示': 'Degraded display',
        '深度解析未通过质量门禁': 'Deep analysis did not pass the quality gate',
        '当前证据不足以支持完整分析。': 'Current evidence cannot support a full analysis.',
        '以下仅展示可追溯引用、待补事项和人工复核要求。':
            'Only traceable citations, outstanding items and manual-review requirements are shown below.',
        '案情结构化摘要': 'Structured case summary', '可回链事实形成的案件轮廓': 'Case outline built from traceable facts',
        '争议焦点详情': 'Disputed issue detail', '需要裁判或进一步核验的问题': 'Issues requiring adjudication or further verification',
        '本方立场': 'Our position', '可能抗辩': 'Likely defences', '法律依据': 'Legal basis',
        '正式关系已核验，条文摘要待人工确认。': 'The formal relation is verified; the provision summary awaits manual confirmation.',
        '未检索到直接依据': 'No direct authority retrieved', '类案支持': 'Analogous case support',
        '已检索到案例，裁判要点待人工核验。': 'Cases were retrieved; the ratio awaits manual verification.',
        '未检索到可绑定的类案支持': 'No bindable analogous case retrieved', '风险预测矩阵': 'Risk prediction matrix',
        '触发条件与潜在后果': 'Triggers and potential consequences', '实体': 'Substantive', '程序': 'Procedural',
        '证据': 'Evidence', '执行': 'Enforcement', '实体风险': 'Substantive risk', '程序风险': 'Procedural risk',
        '证据风险': 'Evidence risk', '执行风险': 'Enforcement risk', '触发条件': 'Trigger', '潜在后果': 'Potential consequence',
        '需要人工复核': 'Manual review required', '案件强弱评分': 'Case strength score', '基于当前证据状态的规则评分': 'Rule-based score from the current evidence state',
        '证据完整度': 'Evidence completeness', '法律依据充分度': 'Sufficiency of legal basis', '类案支持度': 'Analogous case support',
        '程序合规度': 'Procedural compliance', '对方抗辩强度': 'Opposing defence strength', '综合状态': 'Overall state',
        '未完成事项': 'Outstanding items', '证据缺口清单': 'Evidence gap list', '按优先级补齐事实和材料': 'Close facts and materials by priority',
        '高': 'High', '中': 'Medium', '低': 'Low', '为什么需要：': 'Why needed: ', '补齐后的影响：': 'Impact once closed: ',
        '待补材料': 'Materials outstanding', '下一步取证清单': 'Next evidence-collection list', '引用清单': 'Citation list',
        '已检索法规与类案': 'Retrieved legislation and analogous cases', '暂无标准引文': 'No standard citation yet',
        '未检索到可追溯引用，必须由人工完成法律检索。':
            'No traceable citation was retrieved; legal research must be completed manually.',
        '置信度边界与人工复核': 'Confidence bounds and manual review', '结论适用范围': 'Scope of the conclusion',
        '风险类型筛选': 'Risk type filter',

        // task banner
        '资料更新': 'Data update', '后台资料整理任务': 'Background data task', '任务已创建。': 'Task created.',
        '任务状态已更新。': 'Task status updated.',

        // agent chat
        '关联深度分析': 'Linked deep analysis', '围绕当前结果继续提问': 'Keep asking about the current result',
        '这里会围绕当前已经命中的法律法规、案例或 OFAC 材料继续回答；如果同样的问题之前已经问过，系统会优先复用已有答案。':
            'Questions are answered against the legislation, cases or OFAC materials already matched; identical questions reuse the previous answer.',
        '问题': 'Question', '开始关联分析': 'Start linked analysis', '关联分析': 'Linked analysis', '回答': 'Answer',
        '支持要点': 'Supporting points', '注意事项': 'Cautions', '关联法律法规': 'Linked legislation', '参考资料': 'Reference materials',
        '建议继续追问': 'Suggested follow-ups', '请输入要继续追问的问题。': 'Enter a follow-up question.',
        '正在基于当前结果生成关联分析，请稍候。': 'Generating a linked analysis from the current result. Please wait.',
        '关联分析失败。': 'Linked analysis failed.', '当前没有附带法规链接': 'No legislation link attached',
        '当前没有附带参考资料': 'No reference material attached',
        '例如：当前最关键的法规是哪一条？哪些案例最值得优先核对？':
            'e.g., Which instrument is decisive here? Which cases should be checked first?',
        '关联片段': 'Matched excerpt', '支持论点': 'Supporting arguments', '暂无支持论点分析': 'No supporting-argument analysis yet',
        '反对论点': 'Opposing arguments', '暂无反对论点分析': 'No opposing-argument analysis yet',

        // memo
        '返回案例分析': 'Back to case analysis', '下载预测报告 PDF': 'Download outlook report (PDF)', '打印页面': 'Print page',
        '案件预测报告': 'Case outlook report',
        '当前没有输入案情。请先在判决预测页生成结果，再下载 PDF。':
            'No case input found. Generate a result on the outlook page first, then download the PDF.',
        '本报告汇总案例分析、支持案例比较与预测结论。下载 PDF 与页面展示使用同一套内容结构，以减少样式和排版偏差。':
            'This report consolidates the case analysis, supporting-case comparison and outlook. The PDF and the web view share one content structure to minimise layout drift.',
        '对象': 'Subject', '案件研判记录': 'Case assessment record', '模型': 'Model', '日期': 'Date', '主题': 'Subject',
        '事实': 'Facts', '未提炼出独立事实段落。': 'No standalone fact paragraph was extracted.',
        '未提炼出稳定争议焦点。': 'No stable disputed issue was extracted.', '未提炼出明确请求事项。': 'No clear relief was extracted.',
        '检索关键词': 'Search keywords', '结论': 'Conclusion', '可能胜出方': 'Likely prevailing party',
        '预测理由': 'Reasoning', '相似点': 'Similarities', '差异点': 'Differences',
        '当前没有检索到可支持预测的案例记录。': 'No case record was retrieved to support the outlook.',
        '支持理由': 'Supporting reasons', '当前没有提炼出稳定的支持理由。': 'No stable supporting reason was extracted.',
        '本报告仅用于 demo 展示，结论依赖当前数据覆盖范围、检索质量和模型稳定性，不构成正式法律意见。':
            'This report is for demonstration only. Its conclusions depend on data coverage, retrieval quality and model stability, and are not formal legal advice.',

        // account
        '这里可以维护你的基本信息，并查看最近的查询记录。': 'Maintain your basic details and review recent queries.',
        '电话': 'Phone', '机构': 'Organisation', '最近查询记录': 'Recent queries', '查看全部历史': 'View all history',
        '恢复本次结果': 'Restore this result', '当前还没有历史记录': 'No history yet',
        '进行一次检索、分析或预测后，这里会自动保存记录。': 'Records are saved here automatically after a search, analysis or outlook.',

        // common admin / misc
        '时间': 'Time', '操作': 'Actions', '状态': 'Status', '数据类型': 'Data type', '用户': 'User',
        '条文编号': 'Provision no.', '自动建立案例与法规关联': 'Auto-link cases and legislation',
        '员工': 'Employee', '雇佣': 'Employment', '侵权': 'Tort', '合同': 'Contract', '消费者保护': 'Consumer protection',
        '刑法': 'Criminal', '风险': 'Risk', '分析': 'Analysis', '检索': 'Search', '结果': 'Results', '详情': 'Detail',
        '来源': 'Source', '相关度': 'Relevance', '匹配依据': 'Match basis', '匹配原因': 'Match reason',
        '处理结果': 'Outcome', '条文': 'Provision', '法规原文': 'Original text', '总页数': 'Total pages',
    };

    /* ---------- 锚定正则规则（仅整串匹配才替换） ---------- */
    const RULES = [
        [/^共\s*(\d+)\s*条$/, (m) => `${m[1]} item(s)`],
        [/^共\s*(\d+)\s*条记录，第\s*(\d+)\s*\/\s*(\d+)\s*页$/, (m) => `${m[1]} record(s) · page ${m[2]} of ${m[3]}`],
        [/^共\s*(\d+)\s*条\s*·\s*第\s*(\d+)\s*\/\s*(\d+)\s*页$/, (m) => `${m[1]} item(s) · page ${m[2]} of ${m[3]}`],
        [/^第\s*(\d+)\s*页$/, (m) => `Page ${m[1]}`],
        [/^处理\s*(\d+)\s*条$/, (m) => `${m[1]} processed`],
        [/^已处理\s*(\d+)\s*条$/, (m) => `${m[1]} processed`],
        [/^\s*(\d+)\s*个关联案例$/, (m) => `${m[1]} linked cases`],
        [/^关联案例\s*(\d+)$/, (m) => `Linked cases: ${m[1]}`],
        [/^关联案例\s*(\d+)\s*$/, (m) => `Linked cases: ${m[1]}`],
        [/^访问\s*(\d+)$/, (m) => `Views ${m[1]}`],
        [/^(\d+)\s*次$/, (m) => `${m[1]} time(s)`],
        [/^(\d+)\s*项$/, (m) => `${m[1]} item(s)`],
        [/^条文\s*(.+)$/, (m) => `Section ${m[1]}`],
        [/^法规\s*(\d+)$/, (m) => `Legislation: ${m[1]}`],
        [/^案例\s*(\d+)$/, (m) => `Cases: ${m[1]}`],
        [/^命中法规\s*(\d+)$/, (m) => `Legislation matched: ${m[1]}`],
        [/^命中案例\s*(\d+)$/, (m) => `Cases matched: ${m[1]}`],
        [/^命中支持案例数\s*(\d+)$/, (m) => `Supporting cases matched: ${m[1]}`],
        [/^可疑全文\s*(\d+)$/, (m) => `Suspicious full text: ${m[1]}`],
        [/^置信度\s*([\d.]+)%$/, (m) => `Confidence ${m[1]}%`],
        [/^本轮\s*(\d+)\s*\/\s*100$/, (m) => `This round ${m[1]}/100`],
        [/^上限\s*(\d+)%$/, (m) => `Cap ${m[1]}%`],
        [/^强度\s*(\d+)%$/, (m) => `Strength ${m[1]}%`],
        [/^引用可追溯率\s*(\d+)%$/, (m) => `Citation traceability ${m[1]}%`],
        [/^证据完整度\s*(\d+)%$/, (m) => `Evidence completeness ${m[1]}%`],
        [/^低置信度\s*(\d+)%$/, (m) => `Low confidence ${m[1]}%`],
        [/^整体置信度\s*(\d+)%$/, (m) => `Overall confidence ${m[1]}%`],
        [/^人工复核\s*(\d+)\s*项$/, (m) => `Manual review: ${m[1]} item(s)`],
        [/^案卷可追溯\s*([\d.]+)%$/, (m) => `Record traceability ${m[1]}%`],
        [/^法律依据可追溯\s*([\d.]+)%$/, (m) => `Authority traceability ${m[1]}%`],
        [/^争点覆盖\s*([\d.]+)%$/, (m) => `Issue coverage ${m[1]}%`],
        [/^角色合规\s*([\d.]+)%$/, (m) => `Role compliance ${m[1]}%`],
        [/^匹配依据：\s*(.+)$/, (m) => `Match basis: ${m[1]}`],
        [/^匹配原因：\s*(.+)$/, (m) => `Match reason: ${m[1]}`],
        [/^关联理由：\s*(.+)$/, (m) => `Link reason: ${m[1]}`],
        [/^处理结果：\s*(.+)$/, (m) => `Outcome: ${m[1]}`],
        [/^数据状态\s*(.+)$/, (m) => `Data status: ${m[1]}`],
        [/^当前：\s*(.+)$/, (m) => `Current: ${translate(m[1])}`],
        [/^运行\s*(.+)$/, (m) => `Run ${translate(m[1])}`],
        [/^引用：\s*(.+)$/, (m) => `Cited: ${m[1]}`],
        [/^（筛选后\s*(\d+)\s*条）$/, (m) => `(${m[1]} after filtering)`],
        [/^原始值\s*(.+)$/, (m) => `Raw value ${m[1]}`],
        [/^(\d+)\s*优先级$/, (m) => `${translate(m[1])} priority`],
    ];

    function translate(text) {
        const raw = String(text || '');
        const key = raw.trim();
        if (!key) return raw;
        if (DICT[key]) return DICT[key];
        if (DICT[raw]) return DICT[raw];
        for (const rule of RULES) {
            const match = key.match(rule[0]);
            if (match) return rule[1](match);
        }
        return raw;
    }

    /* ---------- 跳过区域：动态业务正文、代码、法条原文 ---------- */
    const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'CODE', 'PRE', 'TEXTAREA', 'NOSCRIPT']);
    const SKIP_ATTR = ['data-i18n-skip', 'data-lang-text'];

    function shouldSkip(node) {
        if (SKIP_TAGS.has(node.nodeName)) return true;
        let parent = node.parentElement;
        while (parent) {
            for (const attr of SKIP_ATTR) {
                if (parent.hasAttribute && parent.hasAttribute(attr)) return true;
            }
            if (parent.nodeName === 'SCRIPT' || parent.nodeName === 'STYLE') return true;
            parent = parent.parentElement;
        }
        return false;
    }

    function translateTextNodes(root) {
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
            acceptNode(node) {
                if (!node.nodeValue || !/[\u4e00-\u9fff]/.test(node.nodeValue)) return NodeFilter.FILTER_REJECT;
                return shouldSkip(node) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
            },
        });
        const targets = [];
        while (walker.nextNode()) targets.push(walker.currentNode);
        targets.forEach((node) => {
            const next = translate(node.nodeValue);
            if (next !== node.nodeValue) node.nodeValue = next;
        });
    }

    const TRANS_ATTRS = ['placeholder', 'title', 'aria-label', 'alt'];

    function translateAttributes(root) {
        if (root.nodeType !== 1 && root.nodeType !== 9) return;
        const scope = root.nodeType === 9 ? document.body : root;
        if (!scope || !scope.querySelectorAll) return;
        scope.querySelectorAll('*').forEach((element) => {
            if (shouldSkip(element)) return;
            TRANS_ATTRS.forEach((attr) => {
                const value = element.getAttribute(attr);
                if (!value || !/[\u4e00-\u9fff]/.test(value)) return;
                const next = translate(value);
                if (next !== value) element.setAttribute(attr, next);
            });
        });
    }

    function run(root) {
        translateTextNodes(root);
        translateAttributes(root);
    }

    function start() {
        run(document.body);
        const observer = new MutationObserver((mutations) => {
            for (const mutation of mutations) {
                mutation.addedNodes.forEach((node) => {
                    if (node.nodeType === 1) {
                        if (!shouldSkip(node)) run(node);
                    } else if (node.nodeType === 3) {
                        if (!shouldSkip(node) && /[\u4e00-\u9fff]/.test(node.nodeValue || '')) {
                            const next = translate(node.nodeValue);
                            if (next !== node.nodeValue) node.nodeValue = next;
                        }
                    }
                });
                if (mutation.type === 'characterData' && mutation.target.nodeType === 3) {
                    const node = mutation.target;
                    if (!shouldSkip(node) && /[\u4e00-\u9fff]/.test(node.nodeValue || '')) {
                        const next = translate(node.nodeValue);
                        if (next !== node.nodeValue) node.nodeValue = next;
                    }
                }
            }
        });
        observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
