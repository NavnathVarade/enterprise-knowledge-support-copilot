"""
Offline extractive baseline "LLM" used ONLY to exercise the answer-evaluation harness
without a network provider.

It is NOT a language model. It picks the source sentence with the highest content-word
overlap with the question, quotes it, and cites its source id; it abstains when overlap is
below a threshold. Scores obtained with it say something about retrieval + context +
citation plumbing + the harness, and NOTHING about how a real model would answer.
"""

from __future__ import annotations

import re

from packages.llm.base import LLMRequest, LLMResponse

from rag.evaluation.metrics import lexical_support

_SOURCE = re.compile(r'<source id="(S\d+)"[^>]*>\n(.*?)\n</source>', re.DOTALL)
_QUESTION = re.compile(r"<question>\n(.*?)\n</question>", re.DOTALL)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


class ExtractiveBaselineProvider:
    name = "extractive-baseline"
    model = "extractive-baseline-v1"

    def __init__(self, min_overlap: float = 0.3) -> None:
        self.min_overlap = min_overlap

    async def complete(self, request: LLMRequest) -> LLMResponse:
        body = request.messages[0].content
        q = _QUESTION.search(body)
        question = q.group(1) if q else ""
        best: tuple[float, str, str] | None = None
        for sid, text in _SOURCE.findall(body):
            for sentence in _SENTENCE.split(text):
                sentence = sentence.strip().lstrip("#").strip()
                if not sentence:
                    continue
                score = lexical_support(question, sentence)
                if score is not None and (best is None or score > best[0]):
                    best = (score, sid, sentence)
        if best is None or best[0] < self.min_overlap:
            out = "INSUFFICIENT_EVIDENCE"
        else:
            out = f"{best[2]} [{best[1]}]"
        return LLMResponse(out, self.name, self.model, 0, 0, "end_turn", 0.0)
