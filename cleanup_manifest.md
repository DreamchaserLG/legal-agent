# 清理隔离清单

日期：2026-09-07

隔离目录：`_cleanup_quarantine_20260907/`

原则：先移动到隔离目录，不直接永久删除。项目验证通过后，再由用户决定是否彻底删除。

## 保留内容

- `app/`
- `sql/`
- `docs/`
- `data/eval/`
- `scripts/`
- `.env.example`
- `.env.clean.example`
- `.env.sqlite.example`
- `requirements.txt`
- `requirements-local-embedding.txt`
- `README.md`
- `PLAN.md`
- `Act.md`
- `Make.md`
- `result.md`
- `CODEX.md`
- `canlii_ingest.py`
- `rag_manage.py`
- `export_rag_data.py`
- `llm_healthcheck.py`
- Docker 与部署相关文件
- 本地 `venv/` 仅用于验证，仍保持 Git ignore

## 已隔离候选

- `.claude/`
- `data_archive/`
- `dist/`
- `drivers/`
- `logs/`
- `tmp/`
- `resume_pdf/`
- `_resume_images/`
- `__pycache__/`
- `_encoding_test.py`
- `_generate_ligu_resume.py`
- `_resume_preview_clear.png`
- `_resume_source.docx`
- `canlii_test.html`
- `crawl_batch.py`
- `crawl_canlii_batch.py`
- `crawl_canlii_full.py`
- `crawl_canlii_selenium.py`
- `crawl_canlii_uc.py`
- `crawl_distributed.py`
- `debug_canlii.html`
- `debug_page.html`
- `fix_py39.py`
- `proxies.txt`
- `run_crawl.bat`
- `run_crawl_fast.bat`
- `run_crawl_full.bat`
- `run_crawl_test.bat`
- `test_data_flow.py`
- `test_final_fix.py`
- `test_fixes.py`
- `FINAL_FIX_SUMMARY.md`
- `FINAL_REPAIR_SUMMARY.md`
- `REPAIR_SUMMARY.md`
- `REPAIR_SUMMARY_2.md`
- `legal_agent_practice_report.docx`
- `legal_agent_practice_report_4000.docx`
- `Legal_AI_Agent_PPT制作指南.docx`
- `Legal_AI_Agent_技术汇报_完整版.pptx`
- `实践主要内容_重写版.md`
- `曹敬雯研究生实践报告书.doc`
- `李固.pdf`
- `李固_preview.png`
- `李固_preview_new.png`
- `李固研究生实践报告正文_重写版.md`
- `计算机学院 研究生实践报告书-专硕-李固.doc`
- `crawl_canlii.log`
- `crawl_canlii_batch.log`
- `uvicorn-rag.err.log`
- `uvicorn-rag.log`

## 注意

隔离目录不进入 Git，也不进入最小运行压缩包。
