# P3 retrieval development set

The 18 authored queries (14 English, four Chinese) are synthetic development prompts. Labels refer to the unchanged English τ-bench retail policy at revision `da0835f6c67baef58bdda18f2a3b037dfb3a1754`. The policy SHA-256 is `2c9652afbce57d6e087768d37cda64d31c53d50b3e3225cfdb791bac66466467`. No official tasks, independent human relevance judgments, or final business outcomes were scored.

`queries.json` provides query text and language. `labels.json` supplies required clauses as complete UTF-8 line spans and exact source text. Multiple clauses are equally weighted. The scorer verifies both files against their frozen SHA-256 and checks the labels against the pinned original bytes. Labels and excerpts are used after retrieval.

Scoring uses the primary chunk citations at ranks 1, 3, and 5. A citation must identify the source document/version and a real source block, preserve its heading path and kind, and identify a valid chunk character range. Evidence credit requires this range to contain the entire labeled line text, excluding its line terminator. Parent context is recorded separately and gives no retrieval credit. A duplicate evidence line earns credit once, at its first matching rank. Recall is the fraction of required lines found, MRR uses the first required line found, and evidence nDCG discounts each line by 1/log2(rank+1) at its first appearance, normalized by the ideal with all lines at rank one. Citation precision is the proportion of returned locators verified against source provenance. It measures locator integrity, not answer correctness. Query failures count as zero for retrieval metrics and remain visible.

The comparison uses fixed and structure chunks with the retriever defaults (450-character window, 70-character overlap), candidate depth 20, top 5, and the same query and corpus vectors across vector backends. It reports each mode and language independently. Dense results use cosine similarity. Timings include per-query search wall time and available retriever stages. Shared query embedding is measured separately and added to search wall time as an estimate for uncached lookup. Index preparation and model loading are outside query wall time. Synthetic role/date/version probes use a separately labeled copy of the same policy to exercise access boundaries.

Frozen before any P3 retrieval run in this set.

- Queries SHA-256 `8441fb4f78e338116a3c37697641c46ad43ebb03cfcc098bd6ff6d91455457f9`
- Labels SHA-256 `c79bb2a32ee1a5f7a58386e7db52cda02c1b8e2dcfe176aed855ce38958cf1eb`

With the pinned P1 bundle already downloaded, run the lexical comparison from the repository root:

```sh
python3 scripts/evaluate_tau_rag.py .runtime/tau-retail/source/policy.md \
  --output .runtime/tau-p3-bm25.json
```

The Python entry point `evaluate(policy_path, embed=..., rerank=..., index_factories=...)` runs real vector stores when provided. With multiple stores it also reports per-query top-five overlap and exact rank agreement.

Pass `--model-cache PATH` to run the pinned local embedding and reranker using in-memory cosine search. Add `--database-stores` to run Milvus and pgvector against the same vectors. Those services and the model dependencies must be available before running the database comparison.

Use `--chunk-size` and `--overlap` to record an explicit configuration in a new report. The historical 800/120 windows exceeded the embedding token limit and are retained as a truncation case. The current model adapter rejects such inputs.
