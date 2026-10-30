from __future__ import annotations

import uuid

from rag.comparison.diff import Block, diff_versions


def b(h: str, heading: str | None = None, seq: int = 0) -> Block:
    return Block(uuid.uuid4(), h, heading, seq)


def test_identical_versions_have_no_changes() -> None:
    a = [b("1", "A", 0), b("2", "B", 1)]
    c = [Block(uuid.uuid4(), x.content_hash, x.heading_path, x.sequence) for x in a]
    d = diff_versions(a, c)
    assert not d.has_changes and d.unchanged_count == 2


def test_modified_pairs_by_heading_added_and_removed() -> None:
    before = [b("1", "Intro"), b("2", "Limits"), b("3", "Old section")]
    after = [b("1", "Intro"), b("2x", "Limits"), b("9", "New section")]
    d = diff_versions(before, after)
    assert d.unchanged_count == 1
    assert [(p.before.content_hash, p.after.content_hash) for p in d.modified] == [("2", "2x")]
    assert [x.content_hash for x in d.removed] == ["3"]
    assert [x.content_hash for x in d.added] == ["9"]


def test_reordering_is_not_a_change() -> None:
    d = diff_versions([b("1"), b("2")], [b("2"), b("1")])
    assert not d.has_changes


def test_duplicate_hashes_are_counted_not_collapsed() -> None:
    d = diff_versions([b("1"), b("1"), b("1")], [b("1")])
    assert d.unchanged_count == 1 and len(d.removed) == 2 and not d.added
    d2 = diff_versions([b("1")], [b("1"), b("1")])
    assert len(d2.added) == 1 and d2.unchanged_count == 1


def test_null_heading_never_pairs_as_modified() -> None:
    d = diff_versions([b("1", None)], [b("2", None)])
    assert not d.modified and len(d.removed) == 1 and len(d.added) == 1


def test_empty_sides() -> None:
    assert len(diff_versions([], [b("1")]).added) == 1
    assert len(diff_versions([b("1")], []).removed) == 1
    assert not diff_versions([], []).has_changes
