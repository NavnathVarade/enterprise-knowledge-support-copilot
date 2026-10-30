"""
Heuristic prompt-injection detection for retrieved text and user queries (Spec §32, §35).

REFERENCE IMPLEMENTATION -- A HEURISTIC, NOT A GUARANTEE. Pattern rules catch common,
blatant instruction-override attempts; they are trivially evaded by paraphrase,
translation, or encoding (see docs/security/PHASE_24_PROMPT_INJECTION_DESIGN.md for the
documented misses). They are one layer next to the ones that actually bound the damage:
authorization before retrieval, untrusted-data prompt framing with escaping, citation
validation, and output-side policy (canary / prompt-leak / exfiltration checks).

Text is normalized first (NFKC, zero-width characters removed, whitespace collapsed) so
full-width letters and invisible characters cannot trivially hide a phrase.
"""

from __future__ import annotations

import enum
import re
import unicodedata
from dataclasses import dataclass


class InjectionRisk(enum.IntEnum):
    NONE = 0
    MEDIUM = 1
    HIGH = 2


@dataclass(frozen=True)
class InjectionAssessment:
    risk: InjectionRisk
    signals: tuple[str, ...]


_ZERO_WIDTH = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]"  # zero-width + bidi controls, escaped on purpose
)

_I = re.IGNORECASE | re.DOTALL
# (signal name, pattern, is_high)
_RULES: list[tuple[str, re.Pattern[str], bool]] = [
    ("override_instructions", re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass)\b.{0,40}\b(?:all|any|every|previous|prior|"
        r"above|earlier|preceding|system|your)\b.{0,30}\b(?:instructions?|rules?|prompts?|"
        r"directions?|guidelines|constraints|restrictions)\b", _I), True),
    ("role_hijack", re.compile(
        r"\byou are now\b|\bnew (?:system )?instructions?\s*:|\bdeveloper mode\b|\bjailbreak\b|"
        r"\bdan mode\b|\bfrom now on,? you\b", _I), True),
    ("prompt_exfiltration", re.compile(
        r"\b(?:reveal|show|print|repeat|output|leak|disclose|display)\b.{0,40}\b(?:system prompt|"
        r"hidden prompt|your instructions|initial prompt|these instructions|your prompt)\b", _I), True),
    ("delimiter_spoof", re.compile(r"</?\s*(?:sources?|question|system|instructions?)\b", _I), True),
    ("fake_role_line", re.compile(r"(?:^|\n|---)\s*(?:system|assistant|developer)\s*:", _I), False),
    ("sensitive_data_demand", re.compile(
        r"\b(?:reveal|disclose|expose|exfiltrate|leak|dump)\b.{0,60}\b(?:restricted|confidential|"
        r"secret|passwords?|credentials?|api[ _-]?keys?|compensation|salary)\b", _I), False),
    ("exfil_markdown_image", re.compile(r"!\[[^\]]*\]\(\s*https?://", _I), False),
    ("send_to_url", re.compile(
        r"\b(?:send|post|forward|email|upload|transmit)\b.{0,60}\bhttps?://", _I), False),
    ("persona_switch", re.compile(r"\b(?:act as|pretend (?:to be|you are)|roleplay as)\b", _I), False),
]  # fmt: skip


def normalize_for_detection(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH.sub("", text)
    return re.sub(r"[ \t\r\f\v]+", " ", text)


def assess_text(text: str) -> InjectionAssessment:
    raw_zero_width = len(_ZERO_WIDTH.findall(text))
    normalized = normalize_for_detection(text)
    signals: list[str] = []
    high = False
    medium_count = 0
    for name, pattern, is_high in _RULES:
        if pattern.search(normalized):
            signals.append(name)
            if is_high:
                high = True
            else:
                medium_count += 1
    if raw_zero_width >= 3:
        signals.append("hidden_characters")
        medium_count += 1
    if high or medium_count >= 2:
        risk = InjectionRisk.HIGH
    elif medium_count == 1:
        risk = InjectionRisk.MEDIUM
    else:
        risk = InjectionRisk.NONE
    return InjectionAssessment(risk, tuple(signals))
