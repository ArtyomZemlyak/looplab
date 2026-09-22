"""The `posix_only` gate skips exactly what it names, only off POSIX, and refuses to name nothing.

The gate is the answer to one measured family (review 2026-09-22, GitHub Actions run 35785582444):
tests whose SUBJECT is a POSIX mechanism — the bash bench harness, fork, process groups, flock, CPU
affinity, mode bits — failing on the Windows leg for reasons no Windows change could fix. A gate
that skipped too much would hide the other families (a real Windows defect reads the same as a
skip in a green report), so its three properties are driven here through the hook itself, on this
box, with the platform predicate switched: marked items skip off POSIX with the mechanism in the
reason, unmarked items are untouched, POSIX runs everything, and a nameless gate is refused on
EVERY platform rather than discovered by the Windows leg a CI cycle late.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import _posix_gates

_CONFTEST = Path(__file__).with_name("conftest.py")


def _hooks():
    """The suite's own conftest, loaded as a private module so the hook under test is the real one."""
    spec = importlib.util.spec_from_file_location("_looplab_tests_conftest_under_test", _CONFTEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Item:
    """The three members the hook reads: a node id, a closest marker, and add_marker."""

    def __init__(self, nodeid: str, mark=None):
        self.nodeid = nodeid
        self._mark = mark.mark if mark is not None else None
        self.added: list = []

    def get_closest_marker(self, name):
        return self._mark if self._mark is not None and self._mark.name == name else None

    def add_marker(self, marker):
        self.added.append(marker.mark if hasattr(marker, "mark") else marker)


def _run(monkeypatch, *, posix: bool, items):
    hooks = _hooks()
    monkeypatch.setattr(hooks, "_on_posix", lambda: posix)
    hooks.pytest_collection_modifyitems(None, items)
    return items


def test_off_posix_a_gated_test_is_skipped_and_the_reason_names_its_mechanism(monkeypatch):
    gated = _Item("tests/test_x.py::test_harness", _posix_gates.BASH_HARNESS)
    plain = _Item("tests/test_x.py::test_portable")
    _run(monkeypatch, posix=False, items=[gated, plain])

    assert [m.name for m in gated.added] == ["skip"]
    reason = gated.added[0].kwargs["reason"]
    assert reason.startswith("POSIX-only mechanism: ")
    assert "bash" in reason and "WSL" in reason, reason
    assert plain.added == [], "an ungated test must never be skipped by the gate"


def _named_gates() -> dict:
    return {name: value for name, value in vars(_posix_gates).items()
            if isinstance(value, pytest.MarkDecorator)}


def test_on_posix_nothing_is_skipped(monkeypatch):
    items = [_Item(f"tests/test_x.py::test_{name}", gate) for name, gate in _named_gates().items()]
    _run(monkeypatch, posix=True, items=items)
    assert [item.added for item in items] == [[]] * len(items)


@pytest.mark.parametrize("posix", [True, False], ids=["posix", "windows"])
@pytest.mark.parametrize("nameless", [pytest.mark.posix_only(), pytest.mark.posix_only("  ")],
                         ids=["no-argument", "blank"])
def test_a_gate_that_names_no_mechanism_is_refused_on_every_platform(monkeypatch, posix, nameless):
    with pytest.raises(pytest.UsageError, match="needs the POSIX mechanism it gates"):
        _run(monkeypatch, posix=posix, items=[_Item("tests/test_x.py::test_anonymous", nameless)])


def test_every_named_gate_names_a_mechanism():
    """The shared spellings are gates too: each must survive the refusal above."""
    gates = _named_gates()
    assert gates, "tests/_posix_gates.py defines no gate"
    for name, gate in gates.items():
        assert gate.mark.name == "posix_only", name
        assert gate.mark.args and str(gate.mark.args[0]).strip(), name
