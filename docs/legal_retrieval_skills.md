# Legal Retrieval Skills

This project keeps legal retrieval skills as lightweight internal rules instead of adding heavy runtime dependencies.

## Included skills

- LawThinker Explore-Verify-Memorize
  - Source: https://github.com/yxy-919/LawThinker-agent
  - Use in this project: keep retrieval evidence explicit, verify whether hits contain matching legal entities, and preserve reusable context for later prediction.

- LegalBench-RAG passage precision
  - Source: https://github.com/zeroentropy-cc/legalbenchrag
  - Use in this project: prefer exact citations, statute titles, sections, and authority snippets over broad keyword overlap.

- LexNLP-style legal entity extraction
  - Source: https://github.com/LexPredict/lexpredict-lexnlp
  - Use in this project: extract case citations, statute titles, section references, and court signals before search.

- CaseLink authority-aware reranking
  - Source: https://github.com/yanran-tang/CaseLink
  - Use in this project: boost cases with stronger authority signals and clearer case-to-rule links.

- eyecite citation validation
  - Source: https://github.com/freelawproject/eyecite
  - Use in this project: detect legal citations and authority mentions in generated reasoning so unsupported citations can be flagged.

- RAGAS-style context precision
  - Source: https://github.com/explodinggradients/ragas
  - Use in this project: compute a lightweight evidence-quality score before allowing high-confidence prediction.

- Guardrails grounded generation
  - Source: https://github.com/NVIDIA/NeMo-Guardrails
  - Use in this project: cap prediction confidence when retrieved cases, laws, or authority links are weak.

## Code entry points

- `app/service/legal_skill_service.py`
  - skill catalog
  - legal entity extraction
  - domain keyword expansion
  - result verification and reranking signals

- `app/service/evidence_quality_service.py`
  - prediction evidence-quality score
  - unsupported case/citation/statute detection
  - confidence cap from evidence strength

- `app/service/analysis_service.py`
  - applies skill-enhanced keywords during local and model-based analysis

- `app/service/search_service.py`
  - applies skill-enhanced keywords before SQL search
  - applies skill verification to search results before grouping

- `app/service/legal_data_service.py`
  - keeps case-rule matching compatible with SQLite and PostgreSQL

- `app/templates/predict.html`
  - shows evidence quality next to prediction reasoning

- `app/templates/partials/memo_content.html`
  - includes evidence quality in exported prediction reports
