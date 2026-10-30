"""
Output-side policy (Spec §32, §35): checks the model's answer BEFORE it is returned, so
a successful injection still cannot (a) leak the system prompt, (b) exfiltrate data to an
attacker-chosen URL, or (c) smuggle a rendered image that phones home.

Checks:
  - canary: a random per-request marker is added to the system prompt; seeing it in the
    answer proves the prompt was echoed.
  - system-prompt leak: any run of 8 consecutive words from the system prompt appearing in
    the answer.
  - markdown images are never allowed (auto-fetching renderers turn them into exfil).
  - every http(s) URL in the answer must appear verbatim in one of the supplied sources.

Limits: a model can still be talked into misleading (but well-formed, cited) content;
encoded or paraphrased leaks of the prompt pass; URLs copied from a poisoned source are
allowed through (that source should have been quarantined by the injection detector).
"""

from __future__ import annotations

import re

from packages.domain.errors import OutputPolicyViolation

_URL = re.compile(r"https?://[^\s)\]>\"']+", re.IGNORECASE)
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_SHINGLE = 8


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", text.lower())


def check_output(answer: str, *, system_prompt: str, canary: str, source_texts: list[str]) -> None:
    if canary in answer:
        raise OutputPolicyViolation(reason="canary_leak")

    answer_words = _words(answer)
    prompt_words = _words(system_prompt)
    if len(answer_words) >= _SHINGLE and len(prompt_words) >= _SHINGLE:
        prompt_shingles = {
            tuple(prompt_words[i : i + _SHINGLE]) for i in range(len(prompt_words) - _SHINGLE + 1)
        }
        for i in range(len(answer_words) - _SHINGLE + 1):
            if tuple(answer_words[i : i + _SHINGLE]) in prompt_shingles:
                raise OutputPolicyViolation(reason="system_prompt_leak")

    if _MD_IMAGE.search(answer):
        raise OutputPolicyViolation(reason="markdown_image")

    corpus = "\n".join(source_texts)
    for url in _URL.findall(answer):
        if url.rstrip(".,;:") not in corpus:
            raise OutputPolicyViolation(reason="untrusted_url")
