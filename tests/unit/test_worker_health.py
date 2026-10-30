from __future__ import annotations

import os
from pathlib import Path

import pytest
from packages.workers.health import Heartbeat, check


def test_missing_file_is_unhealthy(tmp_path: Path) -> None:
    assert check(str(tmp_path / "hb"), 60) is False
    assert Heartbeat(str(tmp_path / "hb")).age_seconds() is None


def test_fresh_beat_is_healthy_and_stale_is_not(tmp_path: Path) -> None:
    hb = Heartbeat(str(tmp_path / "hb"))
    hb.beat()
    assert check(str(tmp_path / "hb"), 60) is True
    mtime = (tmp_path / "hb").stat().st_mtime
    assert check(str(tmp_path / "hb"), 60, now=mtime + 61) is False
    assert check(str(tmp_path / "hb"), 60, now=mtime + 59) is True


def test_beat_refreshes_an_old_file(tmp_path: Path) -> None:
    p = tmp_path / "hb"
    p.touch()
    os.utime(p, (1, 1))
    hb = Heartbeat(str(p))
    assert (hb.age_seconds() or 0) > 1000
    hb.beat()
    assert (hb.age_seconds() or 1e9) < 5


def test_path_comes_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EKC_HEARTBEAT_FILE", str(tmp_path / "env-hb"))
    Heartbeat().beat()
    assert (tmp_path / "env-hb").exists()
