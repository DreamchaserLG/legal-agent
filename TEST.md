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
