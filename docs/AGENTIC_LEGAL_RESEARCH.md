# Agentic Legal Research Architecture

The Canadian-law analysis page uses a bounded research loop rather than treating a complex case as one search query. The interface may accept Chinese or English, but language never changes the jurisdiction: this research pipeline only reasons over Canadian authorities.

## Runtime flow

1. Extract source-grounded facts and disputed issues.
2. Generate separate rule and precedent queries.
3. Search lexical and legal-graph indexes in parallel; enable the semantic channel only within its configured latency budget.
4. Measure rule/case coverage for every issue.
5. Generate narrower follow-up queries from unresolved gaps.
6. Stop on complete coverage, diminishing returns, timeout or the configured round limit.
7. Bind the resulting evidence pool into deep analysis and stream safe progress events to the UI over SSE.

The page exposes stage names, issued queries, evidence counts, coverage gaps and stop reasons. It does not expose private model chain-of-thought. Analysis runs are owner-scoped, bounded, and held in process memory for the configured TTL; a multi-node deployment should replace that store with Redis or a database-backed queue.

Retrieved and downloaded text is always untrusted data. It cannot select tools or alter system behavior.

## Contextual index

`rag_chunks` retains exact `text_content` for quotations and adds a compact `context_prefix`. The prefix carries document identity, citation, jurisdiction, document type, court, section and chunk position. Existing rows do not duplicate the complete text into `contextual_text`; vectorization composes prefix plus original text at read time. `content_hash` changes when the prefix changes, so contextual embeddings are rebuilt automatically and incrementally.

The local PostgreSQL migration is additive and resumable:

```powershell
python -m app.service.context_index_migration_service --schema-only
python -m app.service.context_index_migration_service --batch-size 10000
```

Each batch commits independently, uses `SKIP LOCKED`, and is restricted to Canadian source codes/tables. Re-run the second command until `remaining` reaches zero, then rebuild embeddings in controlled batches.

## Corpus governance

Corpus changes are fetched only from approved Canadian sources such as Justice Laws, the Supreme Court of Canada, federal/provincial court sites, official gazettes, Ontario government/court sites and CanLII. They are built outside the live index and represented by immutable source snapshots and versioned changesets. A builder and reviewer must use different vendors. Only an independently approved changeset can become a staged release; activation atomically changes the active release pointer and preserves a rollback target.

Large corpus and vector artifacts should remain in versioned object/database storage. Git pull requests should contain manifests, parsers, mappings, schema migrations, structured diffs and evaluation reports rather than raw vector files.

## Operational sequence

1. Add contextual schema fields and backfill prefixes in bounded batches.
2. Rebuild embeddings because contextual content changes the content hash.
3. Rebuild the legal graph.
4. Run retrieval evaluation before activating the new index.
5. Keep the prior corpus/index version until production checks pass.

The research corpus and update pipeline are Canadian-only. A non-Canadian query is not reinterpreted using another country's law; it is returned as unsupported jurisdiction/coverage instead.
