from __future__ import annotations

import pytest
from packages.domain.document_lifecycle import assert_valid_transition, is_valid_transition
from packages.domain.errors import IllegalStatusTransition


@pytest.mark.unit
@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("uploaded", "validating"),
        ("validating", "processing"),
        ("processing", "parsed"),
        ("parsed", "chunked"),
        ("chunked", "embedding"),
        ("embedding", "indexing"),
        ("indexing", "active"),
        ("active", "archived"),
        ("active", "deleted"),
        ("active", "validating"),  # re-processing an active document
        ("processing_failed", "validating"),  # retry
        ("quarantined", "deleted"),
        ("archived", "active"),  # unarchive
    ],
)
def test_valid_transitions_are_accepted(current: str, target: str) -> None:
    assert_valid_transition(current, target)  # must not raise
    assert is_valid_transition(current, target)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("uploaded", "active"),  # can't skip the whole pipeline
        ("deleted", "active"),  # deleted is terminal
        ("deleted", "validating"),
        ("active", "uploaded"),  # can't go backwards past the pipeline
        ("chunked", "active"),  # can't skip embedding/indexing
        ("processing", "active"),
    ],
)
def test_illegal_transitions_are_rejected(current: str, target: str) -> None:
    with pytest.raises(IllegalStatusTransition):
        assert_valid_transition(current, target)
    assert not is_valid_transition(current, target)


@pytest.mark.unit
def test_deleted_is_terminal() -> None:
    """Section 71: deletion should not be silently reversible via this state machine."""
    for target in (
        "uploaded",
        "validating",
        "processing",
        "parsed",
        "chunked",
        "embedding",
        "indexing",
        "active",
        "archived",
        "quarantined",
        "deleted",
    ):
        assert not is_valid_transition("deleted", target)


@pytest.mark.unit
def test_unknown_status_is_rejected() -> None:
    with pytest.raises(IllegalStatusTransition):
        assert_valid_transition("uploaded", "not_a_real_status")
    with pytest.raises(IllegalStatusTransition):
        assert_valid_transition("not_a_real_status", "uploaded")
