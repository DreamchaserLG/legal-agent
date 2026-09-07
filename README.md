# Legal Agent

FastAPI + PostgreSQL demo for a bilingual legal research workflow with three user-facing pages:

- `关键词检索 / Keyword Search`
- `案例分析 / Case Analysis`
- `判决预测 / Outcome Prediction`

The current product is organized around two domain modules:

- `Canada Module`
  - data source: `CanLII`
  - focus: statutes, regulations, court decisions, court-level grouping
- `U.S. Company Sanctions Module`
  - data source: `OFAC`
  - focus: sanctioned companies, removal paths, licensing, remediation, and sanctions status transitions

The system keeps keyword retrieval, case analysis, prediction, bilingual display, async ingestion, and local data archiving in one workflow.

## Module Design

### Canada Module

Use this module when the input is a Canadian dispute or legal scenario.

Main outputs:

- structured case summary
- governing law or regulation candidates
- related cases grouped by court level
- bilingual deep-analysis answer
- prediction with reasons, risks, and supporting cases

Court-level grouping is intentionally emphasized:

- Supreme Court / highest authority
- Court of Appeal
- Federal Court / Federal Court of Appeal
- Provincial or territorial superior courts
- Lower courts, tribunals, or other authorities

### U.S. Company Sanctions Module

Use this module when the research target is a company affected by U.S. sanctions.

Main outputs:

- OFAC-related governing rules and procedures
- sanctioned-company hits from local OFAC data
- transition analysis from sanctioned to non-sanctioned status
- delisting, petition, licensing, disclosure, and remediation paths
- bilingual deep-analysis answer
- prediction with reasons, risks, and supporting records

This module is intentionally narrower than the Canada module. It does not try to act as a general U.S. case-law system. It is focused on sanctions compliance and sanctions removal workflows.

## Bilingual Specification

The application is designed to support both Chinese and English input.

### Input Rules

- Chinese input is accepted directly.
- English input is accepted directly.
- Mixed Chinese-English input is also accepted.

### Processing Flow

1. detect the input language
2. keep the original text unchanged
3. build a bilingual analysis pack
4. generate retrieval-friendly English keywords when the input is Chinese
5. run retrieval against the module-bound source
6. translate result previews for display
7. persist bilingual analysis and bilingual preview content into the database

### Retrieval Rules

- `Canada Module` always resolves to `CanLII`
- `U.S. Company Sanctions Module` always resolves to `OFAC`
- the front end and back end both enforce this mapping

### Storage Rules

Bilingual content is stored in these places:

- `agent_runs.structured_analysis`
- `agent_predictions.raw_json`
- `agent_chat_logs.raw_json`
- `source_items.raw_json.translations.preview`

This avoids repeating the same translation work and improves repeat-query latency.

## Local Data Strategy

There are two distinct goals:

1. grow the local research corpus for repeated use
2. keep a local file archive for later training, review, or export

### Database Storage

Remote records are stored in:

- `source_items`
- `item_keywords`
- `sync_logs`
- `ingestion_tasks`

User-entered case analysis and prediction records are stored in:

- `agent_runs`
- `agent_predictions`
- `agent_chat_logs`

### Local File Archive

Every `source_items` upsert now also writes a local JSON archive file.

Default paths:

- per-item archive:
  - `data_archive/source_items/<source_code>/<source_uid>.json`
- JSONL snapshots:
  - `data_archive/exports/source-items-all-<timestamp>.jsonl`
  - `data_archive/exports/source-items-canlii-<timestamp>.jsonl`
  - `data_archive/exports/source-items-ofac-<timestamp>.jsonl`

The archive is intended for:

- later model training preparation
- offline inspection
- dataset export
- corpus backup

### Archive Endpoints

- `GET /api/archive/status`
- `POST /api/archive/export?source=all`
- `POST /api/archive/rebuild?source=all`

Recommended usage:

1. use `rebuild` once after enabling the feature to export all existing records
2. rely on automatic per-item archive writes for later updates
3. use `export` when you want a fresh snapshot file for training or transfer

## Async Ingestion Strategy

When local results are below the requested threshold, the application can create an ingestion task instead of blocking the page.

Current model:

1. the web process writes `ingestion_tasks`
2. the standalone worker claims tasks with `FOR UPDATE SKIP LOCKED`
3. the worker fetches remote records
4. new records are written into PostgreSQL
5. the same records are written into `data_archive`
6. the page polls task status and refreshes

Important behavior:

- repeated zero-result queries do not keep spawning new tasks
- brand-new case inputs can still trigger one extra hydration pass to grow the corpus
- repeated analysis of the same case can reuse stored history while still running retrieval

## Compliance Boundary

This matters for the data strategy.

### CanLII

CanLII's current Terms of Use prohibit bulk or systematic downloading of documents, including programmatic downloading. Their FAQ also makes clear that CanLII is a publishing and search service, not a general unrestricted bulk-download source.

That means this demo should not be treated as a full-site CanLII crawler.

The current compliant strategy is:

- retrieve data on demand from configured or discovered database pages
- keep local copies only for materials already retrieved through the application workflow
- export locally stored CanLII records from PostgreSQL into local archive files

Operational command:

```powershell
# Show database/archive coverage
python canlii_ingest.py status

# Sync CanLII RSS metadata, rebuild per-item archive files, and export JSONL
python canlii_ingest.py run

# Run through a single local outbound proxy
python canlii_ingest.py --proxy http://127.0.0.1:7897 run

# Add keyword-scoped hydration with limited case text
python canlii_ingest.py run --keywords "tenant eviction" --target-count 20

# Import CanLII official API case metadata.
# Requires CANLII_API_KEY and does not bulk-download case full text.
python canlii_ingest.py api-metadata --database onca --database scc

# Test the API importer with small limits before a long run
python canlii_ingest.py api-metadata --max-databases 1 --max-cases-per-database 20
```

Optional network settings:

```dotenv
# Optional single outbound proxy, for example a local corporate/Clash proxy.
# This is not used for multi-IP rotation or bypassing rate limits.
CANLII_HTTP_PROXY=http://127.0.0.1:7897
CANLII_REQUEST_DELAY_SECONDS=2.0
CANLII_CASE_TEXT_CHAR_LIMIT=6000
CANLII_API_KEY=your_canlii_api_key
CANLII_API_BASE_URL=http://api.canlii.org/v1
CANLII_API_PAGE_SIZE=100
```

Legacy full-site or distributed CanLII crawler scripts should not be used for this project. `crawl_distributed.py` now exits with a deprecation message and points to `canlii_ingest.py`.

Official references:

- CanLII Terms of Use: https://www.canlii.org/info/terms.html
- CanLII FAQ: https://www.canlii.org/info/faq.html

### OFAC

OFAC provides an official Sanctions List Service and related downloadable datasets. OFAC is therefore the correct source for local snapshot building in the U.S. sanctions module.

Official references:

- OFAC Sanctions List Service: https://ofac.treasury.gov/sanctions-list-service
- OFAC Additional Sanctions Lists: https://ofac.treasury.gov/other-ofac-sanctions-lists
- OFAC removal guidance: https://ofac.treasury.gov/specially-designated-nationals-list-sdn-list/filing-a-petition-for-removal-from-an-ofac-list
- OFAC license application page: https://ofac.treasury.gov/ofac-license-application-page

## SQL

Use the current schema file:

```powershell
psql -U postgres -d legal_demo -f sql/legal_agent_demo.sql
```

## Environment

Create `.env` from `.env.example`.

Minimum required settings:

```dotenv
DATABASE_URL=postgresql+psycopg2://postgres:change_me@127.0.0.1:5432/legal_demo
LLM_PROVIDER=spark
SPARK_API_KEY=your_api_key
SPARK_API_SECRET=your_api_secret
SPARK_APP_ID=your_app_id
SPARK_MODEL=Spark Ultra-32K
SPARK_DOMAIN=4.0Ultra
SPARK_BASE_URL=wss://spark-api.xf-yun.com/v4.0/chat
CANLII_DATABASE_PAGES=https://www.canlii.org/en/on/onca/,https://www.canlii.org/en/on/onsc/
LOCAL_ARCHIVE_ENABLED=true
LOCAL_ARCHIVE_DIR=data_archive
LOCAL_ARCHIVE_EXPORT_DIR=data_archive/exports
```

If model credentials are empty, retrieval still works. Analysis, deep analysis, and prediction fall back to weaker local behavior.

## Reliability Guardrails

The app now exposes `data_readiness` in analysis and prediction responses. Treat prediction fields as guarded unless `data_readiness.status` is `ready`.

Key fields:

- `data_readiness.status`: `ready`, `partial`, or `insufficient`
- `data_readiness.corpus`: local corpus counts and CanLII quality indicators
- `data_readiness.evidence`: matched laws, matched cases, and retrieval keywords for the current query
- `data_readiness.missing`: missing data classes such as `canlii_case_metadata`, `case_text_or_authorized_summaries`, or `case_law_relations`
- `data_readiness.recommended_actions`: concrete import or enrichment actions
- `data_readiness.field_contract.guarded_fields`: fields that must not be trusted unless the data status is ready

Useful checks:

```powershell
python llm_healthcheck.py
python canlii_ingest.py --json status
```

## Local RAG Index

The model should not receive the whole database or be fine-tuned just to "know" the local corpus. The executable path is:

```text
local PostgreSQL data -> rag_chunks -> retrieval evidence_context -> 8B model prompt
```

Build and inspect the local RAG index:

```powershell
# Rebuild chunks from source_items, legal_cases, legal_rules, and canada_laws
python rag_manage.py rebuild --source canada

# Check index coverage
python rag_manage.py status

# Test retrieval before trusting model output
python rag_manage.py search "Ontario tenant eviction unpaid rent repair issue" --module canada --limit 8

# Export JSONL for a server-side vector database or embedding job
python rag_manage.py export --source canada

# Compatibility wrapper: rebuild then export
python export_rag_data.py --source canada
```

Runtime APIs:

- `GET /api/rag/status`
- `GET /api/rag/search?query=...&module=canada`
- `POST /api/rag/rebuild?source=canada`
- `POST /api/rag/export?source=canada`

`analysis_result.rag_context` is now passed into prediction and agent-chat calls as `evidence_context`. The model prompt explicitly instructs the 8B model to use only retrieved local evidence and return insufficient evidence when the chunks do not support an answer.

For small remote 8B models, prefer deterministic settings:

```dotenv
LLM_PROVIDER=custom
CUSTOM_TEMPERATURE=0.1
CUSTOM_STRICT_JSON_MODE=true
LLM_TIMEOUT=30
LLM_RETRY_COUNT=1
LLM_CIRCUIT_BREAKER_SECONDS=120
RELIABLE_ANSWER_MODE=true
RELIABLE_MIN_SUPPORT_ITEMS=1
RELIABLE_MAX_CONFIDENCE_WITHOUT_CASES=0.25
RAG_ENABLED=true
RAG_CHUNK_SIZE=1800
RAG_CHUNK_OVERLAP=180
RAG_MAX_CONTEXT_ITEMS=8
```

The repository does not seed demo accounts by default. If you need /admin on a fresh database, set INITIAL_ADMIN_* before the first startup.

## Run

Web app:

```powershell
cd d:\workcatlog\Pycharmproject\legal-demo
.\venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8020
```

Standalone ingestion worker:

```powershell
cd d:\workcatlog\Pycharmproject\legal-demo
.\venv\Scripts\python.exe -m app.worker
```

Then open:

```text
http://127.0.0.1:8020/
```

## Linux Deployment

Recommended production split:

1. `nginx`
2. `fastapi web`
3. `standalone ingestion worker`
4. `postgresql`
5. `llm service`

### Web

Use multiple workers only for the web API.

Example:

```bash
/opt/legal-demo/venv/bin/uvicorn app.main:app \
  --host 127.0.0.1 \
  --port 8020 \
  --workers 4 \
  --proxy-headers
```

### Worker

Run the ingestion worker as a separate process:

```bash
/opt/legal-demo/venv/bin/python -m app.worker
```

### Nginx

Use Nginx for:

- TLS termination
- reverse proxy
- connection buffering
- static asset caching
- basic rate limiting

### Environment File

Store production secrets outside the repo, for example:

```bash
/etc/legal-demo/legal-demo.env
```

### systemd Suggestion

Run separate services:

- `legal-demo-web.service`
- `legal-demo-worker.service`

This prevents task duplication and keeps the ingestion pipeline independent from the HTTP layer.

## Main Pages

- `/`
  - homepage, module selection, keyword search, case analysis entry
- `/results`
  - keyword retrieval results
- `/analyze`
  - bilingual structured case analysis + related materials
- `/predict`
  - bilingual outcome prediction + process explanation + supporting items

## Main APIs

- `GET /api/search`
- `GET /api/analyze-search`
- `GET /api/predict`
- `POST /api/agent-chat`
- `GET /api/ingestion-tasks/{task_id}`
- `GET /api/archive/status`
- `GET /api/rag/status`
- `GET /api/rag/search`
- `POST /api/archive/export`
- `POST /api/archive/rebuild`
- `POST /api/rag/rebuild`
- `POST /api/rag/export`
- `POST /api/sync/ofac`
- `POST /api/sync/canlii`
- `POST /api/sync/all`

## Notes For Training Preparation

If you later train a dedicated legal agent, the current archive layout gives you three useful layers of data:

1. raw retrieved records
2. bilingual structured analysis
3. bilingual prediction and deep-analysis outputs

That means you can later build:

- retrieval datasets
- bilingual analysis datasets
- prediction datasets
- sanctions-remediation datasets

This demo is still a research system, not legal advice.
