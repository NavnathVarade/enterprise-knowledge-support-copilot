# Phase 27 — Evaluation Framework (Spec §28)

Run: `make eval-routing | eval-retrieval | eval-answer` (or `python -m scripts.run_eval <suite>`).
Exit code 1 on a violated `--min METRIC=FLOOR`; any restricted-document leak fails retrieval
runs (`--max-leaks`, default 0).

## What exists
| Piece | File |
|---|---|
| Pure metrics: recall@k, precision@k, MRR, nDCG@k, nearest-rank percentiles, Wilson 95% CI, routing confusion/per-route P/R, abstention rates, lexical support | `rag/evaluation/metrics.py` |
| Strict JSONL loaders (line-numbered errors, duplicate ids, unknown labels rejected) | `rag/evaluation/dataset.py` |
| Fresh-tenant corpus loader through the real chunk/embed path; restricted docs get **no ACL** | `rag/evaluation/corpus.py` |
| Routing / retrieval / end-to-end answer evaluators | `routing_eval.py`, `retrieval_eval.py`, `answer_eval.py` |
| Offline extractive baseline (explicitly **not** an LLM) | `baseline.py` |
| Reports carrying n, providers, caveats, per-case misses | `report.py` |
| Labeled data | `data/eval/*.jsonl` |

Design rules: a metric over zero labeled cases is `None`, never 0 or 1; a missing metric
fails a threshold; every accuracy ships with a confidence interval; every report names what
it was measured with. The harness proves it can detect failure: a test re-runs retrieval
with the restricted document misconfigured as public and requires the leak to be flagged.

## Measured results (this sandbox, deterministic providers — read the caveats)
| Suite | n | Result |
|---|---|---|
| Routing (`rules-v1`) | 52 | accuracy **0.808** (95% CI 0.681–0.892); 10 misses listed by the run |
| Retrieval (FTS + deterministic vectors + lexical rerank) | 12 labeled + 2 leakage-only | recall@1/3/5 = 1.0, MRR 1.0, nDCG@5 1.0, restricted leaks **0** |
| Answer (extractive baseline) | 8 answerable + 4 unanswerable | answered 6/8 (0.75); abstained on 4/4 unanswerable (incl. restricted doc); facts present 6/6; cited only labeled-relevant docs 6/6 |

**How to read them honestly**
- Routing set is hand-written *dev* data; the rules were authored against similar phrasings.
  It is optimistic and **not** a held-out estimate. Treat 0.81 as an upper-ish bound.
- Retrieval is a 12-document corpus whose queries share vocabulary with their documents, and
  the embeddings are not semantic. 1.0 shows the pipeline and authorization work end to end;
  it says **nothing** about retrieval quality on a real corpus. The set is too easy to
  discriminate between systems.
- The answer suite used an extractive baseline, not an LLM. It validates the harness,
  routing-to-answer plumbing, citations and abstention path only.
- Faithfulness/hallucination with a real model, real-embedding retrieval quality, and
  latency under load: **NOT YET MEASURED**.

## Defects the harness surfaced (fixed this phase)
1. `ACCESS_CHECK` matched "How long **do I have to** request a refund?" (bare "to").
   Now requires an access/permission/view/open/read/see/download/edit verb.
2. `SQL_ANALYTICS` matched any "how many" ("How many weeks of parental leave…").
   Now requires a data noun (tickets, orders, incidents, …).

## Known misses left unfixed on purpose (not tuned to the dev set)
"How do I request a new laptop?" (→ hybrid), "Explain the on-call escalation process" and
"SLA for priority 1 tickets" (→ support analysis on the word *tickets*/*process*), "payment
failures for customer 4821", "denied access" phrasing, document-title lookup that contains
"Incident", bare error-code search, "list the steps…", and "how many payment failures …
explain" (payment failures is not a data noun). Rule-based routing has a ceiling; the
documented next step is a labeled, held-out set and a measured learned/LLM fallback.

## Not built
Held-out/blind test split, LLM- or NLI-judged faithfulness, human labeling workflow,
real-provider runs, chunk-level relevance labels, regression tracking across versions,
significance tests between two systems.
