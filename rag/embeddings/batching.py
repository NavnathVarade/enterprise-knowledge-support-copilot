"""Pure batching helper -- splits a list into fixed-size batches, preserving order."""

from __future__ import annotations


def batched(items: list[str], batch_size: int) -> list[list[str]]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    return [items[i : i + batch_size] for i in range(0, len(items), batch_size)]
