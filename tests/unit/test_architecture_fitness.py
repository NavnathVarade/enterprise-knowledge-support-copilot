"""Architecture fitness: the declared layering holds and there are no import cycles between units.
Also proves the checker itself detects violations (otherwise a green result is meaningless)."""

from __future__ import annotations

import pytest
from scripts import architecture_check as ac


def test_repository_has_no_import_cycles_between_units() -> None:
    assert ac.cycles(ac.build_graph(eager=True)) == []


def test_repository_respects_the_declared_layers() -> None:
    assert ac.layer_violations(ac.build_graph(eager=True)) == []


def test_every_unit_is_classified_into_a_layer() -> None:
    assert ac.unknown_units(ac.build_graph(eager=True)) == [], "add new packages to LAYERS"


def test_no_layer_entry_is_stale() -> None:
    graph = ac.build_graph(eager=True)
    seen = set(graph) | {w for ws in graph.values() for w in ws}
    missing = sorted(set(ac.LAYERS) - seen)
    # Units that nothing imports and that import nothing are legitimate only if they exist.
    for unit in missing:
        top, name = unit.split(".")
        assert (ac.ROOT / top / name).is_dir(), f"LAYERS lists {unit}, which does not exist"


def test_nothing_imports_an_application_entry_point_except_apps() -> None:
    graph = ac.build_graph(eager=True)
    offenders = [s for s, ds in graph.items() if "apps.api" in ds and not s.startswith("apps.")]
    assert offenders == []


def test_only_the_reviewed_lazy_imports_exist() -> None:
    """A lazy import is how a cycle hides. New ones must be reviewed and added here on purpose."""
    eager, lazy = ac.build_graph(eager=True), ac.build_graph(eager=False)
    hidden = {
        k: sorted(v - eager.get(k, set())) for k, v in lazy.items() if v - eager.get(k, set())
    }
    assert hidden == {
        "apps.api": [
            "packages.workers",  # dev-only local event dispatcher, loaded only when enabled
            "rag.ingestion",  # route-local; rag.ingestion is a leaf
        ],
        "packages.config": [
            "packages.security"
        ],  # known_keys check; breaks config<->security cycle
    }


def test_the_checker_detects_a_cycle_and_an_upward_import() -> None:
    graph = {
        "a.x": {"a.y"},
        "a.y": {"a.z"},
        "a.z": {"a.x"},
        "packages.domain": {"packages.database"},
    }
    assert ac.cycles(graph) == [["a.x", "a.y", "a.z"]]
    assert ac.layer_violations(graph) == [
        "packages.domain (layer 0) imports packages.database (layer 1)"
    ]


@pytest.mark.parametrize("eager", [True, False])
def test_type_checking_imports_are_not_runtime_edges(
    eager: bool, tmp_path: pytest.TempPathFactory
) -> None:
    import ast

    tree = ast.parse(
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import packages.database.x\n"
        "def f():\n    import packages.security.y\n"
    )
    found = ac._imports(tree, eager_only=eager)
    assert "packages.database.x" not in found
    assert ("packages.security.y" in found) is (not eager)
