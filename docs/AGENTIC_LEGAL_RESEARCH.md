# Agentic Legal Research Architecture

The Canadian-law analysis page uses a bounded research loop rather than treating a complex case as one search query. The interface may accept Chinese or English, but language never changes the jurisdiction: this research pipeline only reasons over Canadian authorities.

## Runtime flow

1. Extract source-grounded facts and disputed issues.
2. Generate separate rule and precedent queries.
3. Search lexical, semantic and legal-graph indexes in parallel.
4. Measure rule/case coverage for every issue.
5. Generate narrower follow-up queries from unresolved gaps.
6. Stop on complete coverage, diminishing returns, timeout or the configured round limit.
7. Bind the resulting evidence pool into deep analysis and expose the research trace in the UI.

Retrieved and downloaded text is always untrusted data. It cannot select tools or alter system behavior.

## Contextual index

`rag_chunks` retains exact `text_content` for quotations and adds `context_prefix` and `contextual_text` for retrieval. The prefix carries document identity, citation, jurisdiction, document type, court, section and chunk position. `content_hash` is computed from contextual text, so changed context is re-embedded automatically.

## Corpus governance

Corpus changes are fetched only from approved Canadian sources such as Justice Laws, the Supreme Court of Canada, federal/provincial court sites, official gazettes, Ontario government/court sites and CanLII. They are built outside the live index and represented by immutable source snapshots and versioned changesets. A builder and reviewer must use different vendors. Only an independently approved changeset can become a staged release; activation atomically changes the active release pointer and preserves a rollback target.

Large corpus and vector artifacts should remain in versioned object/database storage. Git pull requests should contain manifests, parsers, mappings, schema migrations, structured diffs and evaluation reports rather than raw vector files.

## Operational sequence

1. Rebuild the RAG index so existing chunks receive contextual fields.
2. Rebuild embeddings because contextual content changes the content hash.
3. Rebuild the legal graph.
4. Run retrieval evaluation before activating the new index.
5. Keep the prior corpus/index version until production checks pass.

The research corpus and update pipeline are Canadian-only. A non-Canadian query is not reinterpreted using another country's law; it is returned as unsupported jurisdiction/coverage instead.
