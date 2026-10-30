"""Chunker CPU throughput (Spec §119 "document ingestion throughput", chunking stage only).

Measures `chunk_document` on synthetic paragraph text of several document sizes. Excludes
parsing, embedding, database writes and Kafka. Single process, single thread.

    PYTHONPATH=. python -m scripts.bench_chunking --out result.json
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

from rag.chunking.chunker import chunk_document
from rag.parsing.base import ParsedDocument

from scripts.bench_retrieval import _hardware


def _make_text(words: int, rng: random.Random) -> str:
    vocab = [f"term{i}" for i in range(2000)]
    paras, remaining = [], words
    while remaining > 0:
        n = min(remaining, rng.randint(40, 160))
        paras.append(" ".join(rng.choice(vocab) for _ in range(n)) + ".")
        remaining -= n
    return "\n\n".join(paras)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--repeats", type=int, default=5)
    args = ap.parse_args()
    # Deterministic synthetic benchmark text, not a security use of randomness.
    rng = random.Random(7)  # nosec B311
    rows = []
    for words in (1_000, 10_000, 100_000):
        parsed = ParsedDocument(text=_make_text(words, rng))
        chunk_document(parsed)  # warm-up
        times = []
        n_chunks = 0
        for _ in range(args.repeats):
            t = time.perf_counter()
            n_chunks = len(chunk_document(parsed))
            times.append(time.perf_counter() - t)
        best, med = min(times), sorted(times)[len(times) // 2]
        rows.append(
            {
                "words": words,
                "chars": len(parsed.text),
                "chunks": n_chunks,
                "median_seconds": round(med, 4),
                "best_seconds": round(best, 4),
                "median_words_per_second": round(words / med),
                "median_chunks_per_second": round(n_chunks / med),
            }
        )
    out = {"hardware": _hardware({}), "synthetic_text": True, "results": rows}
    args.out.write_text(json.dumps(out, indent=2))
    for r in rows:
        print(r)


if __name__ == "__main__":
    main()
