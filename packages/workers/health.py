"""
Worker liveness via heartbeat file (workers have no HTTP port by default).

The worker loop calls `Heartbeat.beat()` every iteration; the Kubernetes liveness probe runs
`python -m packages.workers.health --max-age N`, which exits non-zero when the file is older
than N seconds (or missing), so a hung event loop gets the pod restarted. `max-age` must
exceed the longest legitimate gap between beats: retry backoff (<= 60s per attempt, up to
5 attempts) plus one slow handler. The manifests use 900s. Per-attempt provider timeouts are
what bound a genuinely stuck call; this probe is the backstop, not the first line of defense.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# An emptyDir mounted at /tmp in the pod, not a shared temp dir.
DEFAULT_PATH = "/tmp/ekc-heartbeat"  # noqa: S108  # nosec B108


class Heartbeat:
    def __init__(self, path: str | None = None) -> None:
        self._path = Path(path or os.environ.get("EKC_HEARTBEAT_FILE", DEFAULT_PATH))

    def beat(self) -> None:
        self._path.touch()
        os.utime(self._path, None)

    def age_seconds(self, now: float | None = None) -> float | None:
        try:
            return (now if now is not None else time.time()) - self._path.stat().st_mtime
        except FileNotFoundError:
            return None


def check(path: str | None, max_age: float, now: float | None = None) -> bool:
    age = Heartbeat(path).age_seconds(now)
    return age is not None and age <= max_age


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m packages.workers.health")
    parser.add_argument("--max-age", type=float, default=900.0)
    parser.add_argument("--path", default=None)
    args = parser.parse_args()
    if not check(args.path, args.max_age):
        print("heartbeat missing or stale", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
