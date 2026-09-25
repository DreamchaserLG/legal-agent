# 案情分析深度解析测试报告

日期：2026-09-25

## 结论

阶段 A-E 已实现并通过自动化、真实数据库案例和既有流程回归验证。深度解析默认关闭；设置 `DEEP_ANALYSIS_ENABLED=true` 后启用。

## 关键验收结果

- `python scripts/test_deep_analysis.py`：16 项通过。
- `python -m compileall app scripts`：通过。
- `python scripts/test_agent_skill_collaboration.py`：通过。
- `python scripts/test_hearing_workflow.py`：通过。
- Jinja `analyze.html` 与深度解析局部模板加载、通过门禁渲染、降级渲染：通过。
- `git diff --check`：通过，无空白错误。
- Uvicorn 以进程级 `DEEP_ANALYSIS_ENABLED=true` 启动，`http://127.0.0.1:8000/health` 返回正常。

真实数据库租赁案例通过 `analyze_sentence_search(..., local_only=True)` 验证：

- 完整耗时约 10.47 秒；数据库先验候选查询约 0.63 秒。
- 13 条可回链结构化事实。
- 5 个推理争点。
- 实体、程序、证据、执行 4 类风险。
- 5 条法规/类案引用。
- 质量门禁通过，审计记录写入 `quality_gate_runs`。
- 结构化摘要不等于输入原文，页面不把原始输入作为分析结果展示。

## 边界

本机未安装 Playwright，因此没有执行真实浏览器截图和像素级移动端检查；已完成模板实际渲染断言和响应式 CSS 静态检查。真实外部 LLM 未调用，使用模拟合法/非法 JSON 覆盖 Schema 校验和规则降级，真实数据库验收使用确定性规则路径。当前 `search_histories` 无记录，未能构造真实跨用户历史记录正反样本；未登录 API 门禁测试通过，历史所有者过滤逻辑保持不变。

详细命令、预期、实际结果及首次失败修复记录见 `TEST.md` 的“2026-09-25 案情分析深度解析改造验证”。
