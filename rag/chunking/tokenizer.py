"""
Token counting.

A documented APPROXIMATION, not an exact tokenizer: ~4 characters per token, the same
heuristic both OpenAI's and Anthropic's own documentation give when an exact tokenizer
isn't available (Anthropic doesn't publish a standalone local tokenizer library).
`tiktoken` was evaluated and deliberately NOT used here -- it fetches its vocabulary
file from a remote host at first use (openaipublic.blob.core.windows.net), which is
both unavailable in this development environment's network allowlist and an unwanted
runtime dependency on an external file host in production. A self-contained heuristic
avoids that failure mode entirely.

This is fine for chunk-sizing decisions, where being off by a modest margin doesn't
change whether a chunk fits a budget. It is NOT to be relied on for exact prompt-token
billing math -- the LLM gateway (Phase 19) must use the provider's actual usage
response for that, never this estimate.
"""

from __future__ import annotations

import math

_CHARS_PER_TOKEN = 4.0


def count_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))
