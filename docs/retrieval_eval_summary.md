# Legal Retrieval Evaluation Summary

## Scope

This evaluation validates the current Canada legal hybrid retrieval stack after adding:

- structured legal query planning
- case/law split retrieval
- BGE-M3 local vector search
- local legal reranking
- CanLII hydration and incremental vector rebuild pipeline

Dataset:

```text
data/eval/canada_retrieval_eval.json
```

Report:

```text
docs/retrieval_eval_report_canada.json
```

## Test Command

```powershell
python rag_manage.py eval-retrieval --limit 10 --output docs\retrieval_eval_report_canada.json
```

## Result

```text
status: completed
dataset: canada_residential_tenancy_retrieval_v1
cases: 20
hit@1: 0.9
hit@3: 1.0
hit@5: 1.0
hit@10: 1.0
mrr: 0.95
misses@10: 0
```

## Interpretation

The current retrieval stack can reliably retrieve expected Ontario residential tenancy / ONLTB evidence within Top-3 for this initial evaluation set.

One failure found during the first run was the query:

```text
Ontario commercial tenancies landlord and tenant act statute
```

It was initially misrouted toward residential tenancy results. The query planner and reranker were adjusted to recognize commercial tenancy, boost Commercial Tenancies Act / Landlord and Tenant Act title matches, and penalize residential drift for commercial queries. The final run passed all Top-10 checks.

## Current Limitation

This is an initial focused evaluation set, not a full legal benchmark. It mainly covers Ontario residential tenancy, eviction, repair, vital services, and a small commercial tenancy regression case.

Next expansion should add labeled cases for:

- employment termination
- contract breach
- negligence
- fraud / misrepresentation
- injunctions
- administrative review
- federal immigration or tax review
