"""Evaluation report: JSON-serializable, always carries n, the providers used, and an
explicit caveat list, so a number can never be quoted without its context."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvalReport:
    suite: str  # routing | retrieval | answer
    dataset: str
    n_cases: int
    metrics: dict[str, Any]
    measured_with: dict[str, str]  # providers/models actually used
    caveats: list[str] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)  # per-case misses

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str)

    def to_markdown(self) -> str:
        lines = [f"# Evaluation: {self.suite} ({self.dataset}, n={self.n_cases})", ""]
        lines.append(
            "**Measured with:** " + ", ".join(f"{k}={v}" for k, v in self.measured_with.items())
        )
        lines += ["", "| metric | value |", "|---|---|"]
        for k, v in self.metrics.items():
            if isinstance(v, (dict, list)):
                continue
            lines.append(f"| {k} | {_fmt(v)} |")
        if self.caveats:
            lines += ["", "**Caveats**", *[f"- {c}" for c in self.caveats]]
        if self.failures:
            lines += ["", f"**Misses ({len(self.failures)})**"]
            lines += [f"- `{f.get('id')}`: {f.get('detail', '')}" for f in self.failures[:50]]
        return "\n".join(lines) + "\n"


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def check_thresholds(report: EvalReport, minimums: dict[str, float]) -> list[str]:
    """Returns violations. A missing/None metric is a violation (not silently passing)."""
    violations = []
    for name, floor in minimums.items():
        value = report.metrics.get(name)
        if value is None:
            violations.append(f"{name}: no value (required >= {floor})")
        elif value < floor:
            violations.append(f"{name}: {value:.3f} < required {floor}")
    return violations
