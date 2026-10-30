# Phase 24 — Prompt-Injection Defenses (Spec §32, §35)

**Principle:** the LLM is never the security boundary. Injection defense here limits *damage* and *propagation*; it does not claim to make a model immune.

## Layers (outermost first)
| # | Layer | Where | What it does |
|---|---|---|---|
| 1 | Authorization before retrieval | Phases 16–18 | A document the user cannot read never reaches the model, injected or not |
| 2 | **Quarantine of suspicious chunks** | `rag/security/injection.py`, `rag/context/builder.py` | Chunks at/above `quarantine_at` (default HIGH) are dropped before context assembly; MEDIUM chunks are kept and carry `injection_signals`. Dropped chunks do not pin versions or consume budget. Logged by id/signals only, never text |
| 3 | Untrusted-data framing + escaping | Phase 20 prompt + `prompt_builder.py` | Sources are declared untrusted data; `<`, `>`, `&`, `"` are escaped so text cannot close the data block |
| 4 | **Canary token** | `rag/generation/answer.py` | Random per-request marker appended after the versioned template (template hash unchanged); in the answer ⇒ blocked |
| 5 | **Output policy** | `rag/security/output_policy.py` | Blocks system-prompt echo (8-word shingles), any markdown image, and any URL not present verbatim in a supplied source → `OutputPolicyViolation` (HTTP 502, nothing returned) |
| 6 | Citation validation | Phase 20 | Fabricated or missing citations are blocked |

## Detector rules (heuristic)
HIGH: instruction override ("ignore/disregard … previous … instructions"), role hijack ("you are now", "developer mode", "new instructions:"), prompt exfiltration ("reveal … your instructions"), delimiter spoofing (`</source>`, `<system>`). MEDIUM: fake `SYSTEM:` role lines, demands for secrets, markdown-image or send-to-URL exfiltration, persona switch, runs of hidden characters. Two MEDIUM signals escalate to HIGH. Text is NFKC-normalized and zero-width characters stripped first, so full-width and zero-width tricks are caught.

## Evidence (all run; see tests)
- Seeded `prompt_injection` document ("Vendor Onboarding Notice") is HIGH and quarantined end-to-end through the real context builder.
- **No false positives on the rest of the synthetic corpus**, and 6 benign sentences using trigger-adjacent words are not flagged. This is measured on synthetic text only; **real-world precision/recall is NOT YET MEASURED.**
- A model that *obeys* an injection (scripted fake) is stopped by layers 4–5: echoing the system prompt, leaking the canary, and exfiltrating through a markdown image each raise `OutputPolicyViolation`; a normal answer still passes.

## Known misses (pinned by tests so nobody over-claims)
Paraphrase ("set aside everything you were told earlier"), translation, base64 and letter-spaced text are **not detected** by layer 2. Layers 3–5 and authorization still apply to them, but a determined attacker can still talk a model into producing *misleading, well-formed, cited* text. Detection of that needs model-based classifiers and red-team evaluation (Phase 28).

## Trade-offs
- Quarantine drops the **whole chunk**, so legitimate text sharing a chunk with an injected paragraph is lost for that query (precision of safety over recall of content). Sanitizing/redacting lines was rejected: partial redaction can leave a working payload.
- A URL copied from a poisoned-but-undetected source is allowed through (it is "in the sources").
- A legitimate answer that quotes 8+ words of the system prompt, or links to a URL only the model knew, is blocked (fail closed).

## Not built
Query-side blocking (user text is not treated as hostile; it is separated from instructions by framing), ML-based injection classifier, per-tenant allow/deny tuning, audit events to Kafka for quarantines (logs only for now), and indirect-injection tests against a real LLM — **no real model was run**, so actual model susceptibility is NOT YET MEASURED.

Tests: 36 new unit + 2 new integration (plus a corrected assertion in the Phase 20 test, which now expects the canary suffix on the system prompt).
