# Foreign-related Legal AI

A bilingual Canadian legal-intelligence workbench for source-linked research, case analysis, hearing preparation, appeal analysis, and human–agent review.

## Product scope

- Canadian legislation and case-law retrieval
- Hybrid lexical, vector, and graph retrieval
- Evidence-grounded RAG with citation and hallucination guardrails
- Structured case analysis and Canadian hearing workflows
- Human–agent review with an auditable event trail
- Chinese and English interface switching

Architecture details: [Agentic legal research, contextual indexing, and corpus governance](docs/AGENTIC_LEGAL_RESEARCH.md).

This project provides decision support only. Every authority and generated conclusion must be reviewed by qualified Canadian counsel before reliance.

## Requirements

- Python 3.11+
- PostgreSQL 16 with pgvector, or the configured SQLite development mode
- An optional OpenAI-compatible model endpoint

## Local setup

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.clean.example .env
```

Edit `.env` locally. Never commit the real file. At minimum, configure the database, a long random session secret, and—when model features are enabled—the selected provider endpoint and API key.

```dotenv
DATABASE_URL=postgresql+psycopg2://postgres:change_me@127.0.0.1:5432/legal_demo
SESSION_SECRET=replace-with-a-long-random-string
LLM_PROVIDER=custom
CUSTOM_MODEL=your-model-name
CUSTOM_BASE_URL=https://your-openai-compatible-endpoint.example/v1
CUSTOM_API_KEY=
```

Start the application:

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. The health endpoint is `http://127.0.0.1:8000/health`.

## Docker

```powershell
docker compose up --build
```

The compose file binds PostgreSQL and the web application to localhost by default. Production deployments must use unique database credentials, a strong session secret, restricted allowed hosts, TLS, and an external secret manager.

## Security and privacy

- Real `.env` files, API keys, database files, archives, logs, tests, screenshots, and private working notes are ignored by Git.
- Example environment files contain placeholders only.
- Legal source text and user submissions must be handled according to applicable licence, confidentiality, and retention requirements.
- The repository must be scanned for secrets before every public push.
