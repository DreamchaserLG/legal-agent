# Clean Package Setup

This package is a sanitized runtime package for the Legal Demo MVP.

It intentionally excludes:

- `.env`
- API keys and private credentials
- local databases
- local model weights
- logs
- cache files
- personal documents
- historical debug HTML files
- previous `dist` packages
- virtual environments

## 1. Install Dependencies

Base application:

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

If you use local BGE-M3 embedding:

```powershell
pip install -r requirements-local-embedding.txt
```

For GPU PyTorch, install the correct CUDA build from the official PyTorch guide.

## 2. Configure Environment

Copy the clean template:

```powershell
copy .env.clean.example .env
```

Then edit `.env`.

Required database setting:

```dotenv
DATABASE_URL=postgresql+psycopg2://postgres:change_me@127.0.0.1:5432/legal_demo
```

For a lightweight SQLite run:

```dotenv
DATABASE_URL=sqlite:///./data/legal_demo.sqlite3
```

Required session setting:

```dotenv
SESSION_SECRET=replace-with-a-long-random-string
```

## 3. Configure LLM

Use one provider:

```dotenv
LLM_PROVIDER=custom
CUSTOM_API_KEY=your_api_key
CUSTOM_MODEL=your_model_name
CUSTOM_BASE_URL=https://your-openai-compatible-endpoint/v1
```

Or:

```dotenv
LLM_PROVIDER=openai
OPENAI_API_KEY=your_api_key
OPENAI_MODEL=gpt-4o-mini
OPENAI_BASE_URL=https://api.openai.com/v1
```

Do not commit `.env`.

## 4. Configure Local BGE-M3 Embedding

Download BGE-M3 separately. Model weights are not included in this package.

Example:

```dotenv
EMBEDDING_PROVIDER=local
EMBEDDING_MODEL=bge-m3
EMBEDDING_DIMENSION=1024
EMBEDDING_LOCAL_MODEL_PATH=D:\environment\embeddingModel\bge-m3
EMBEDDING_DEVICE=auto
EMBEDDING_MAX_LENGTH=2048
EMBEDDING_BATCH_SIZE=16
```

If you do not have BGE-M3 yet, use diagnostic hash embedding:

```dotenv
EMBEDDING_PROVIDER=hash
EMBEDDING_MODEL=local-hash-embedding
EMBEDDING_DIMENSION=384
```

Hash embedding only verifies the pipeline. It is not semantic retrieval.

## 5. Initialize and Run

Run the web app:

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Check RAG/vector status:

```powershell
python rag_manage.py status
python rag_manage.py vector-status
```

Rebuild indexes:

```powershell
python rag_manage.py rebuild --source canada
python rag_manage.py rebuild-vectors --source canada --module canada
```

Search:

```powershell
python rag_manage.py hybrid-search "Ontario tenant eviction repair issue" --module canada --limit 8
```

Evaluate retrieval:

```powershell
python rag_manage.py eval-retrieval --limit 10 --output docs\retrieval_eval_report_canada.json
```

Hydrate new CanLII data and rebuild changed vectors:

```powershell
python rag_manage.py hydrate-canlii "Ontario tenant eviction repair issue" --target-count 3 --module canada
```

## 6. Package Contents

The clean package contains only runtime source, templates, static files, CLI tools, safe configuration templates, and the retrieval evaluation dataset.

It does not include local indexed data. After configuring the environment, rebuild RAG and vector indexes on the target machine.
