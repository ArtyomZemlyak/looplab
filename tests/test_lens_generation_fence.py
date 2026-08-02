"""The paid-concept-lens generation fence, in one place (doc 25 SR-04).

Three endpoints — recover, resolve-recovered, abandon — each wrote out the same two checks inside
the run sequencer, differing only in prose. They answer DIFFERENT questions and both are required:
the first says the CALLER is stale (the tab is acting on a generation the run has moved past), the
second says the SERVER's own prepared projection is (the run moved between materializing the concept
core and taking the sequencer). Collapsing them into one helper is exactly the change that could
quietly drop one of the two.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from looplab.serve.routers.runs import _assert_lens_generation


GENERATION = "a" * 64
MOVED = "b" * 64


class _Commands:
    def __init__(self, current, *, canonical=None):
        self.current = current
        self.canonical = canonical
        self.validated: list[Path] = []

    def validate_paths(self, rd: Path) -> Path:
        self.validated.append(rd)
        return self.canonical or rd

    def run_generation(self, rd: Path) -> str:
        return self.current


class _Srv:
    def __init__(self, commands):
        self.commands = commands


def _fence(current, core_generation, *, expected=GENERATION, canonical=None):
    srv = _Srv(_Commands(current, canonical=canonical))
    return _assert_lens_generation(
        srv, Path("/runs/demo"), core_generation=core_generation,
        expected_generation=expected,
        stale_message="stale caller", stale_remediation="reload",
        prepared_message="stale projection")


def _detail(exc_info) -> dict:
    return exc_info.value.detail


def test_a_current_run_passes_and_hands_back_the_validated_dir():
    """`validate_paths` may re-resolve the run dir, and the caller must keep the canonical one — a
    sequenced section that went on using the pre-validation path would be fencing one directory
    while writing to another."""
    canonical = Path("/runs/canonical")
    rd, generation = _fence(GENERATION, GENERATION, canonical=canonical)
    assert rd == canonical and generation == GENERATION


def test_a_caller_holding_a_stale_generation_is_refused():
    with pytest.raises(HTTPException) as exc:
        _fence(MOVED, MOVED)
    assert exc.value.status_code == 409
    assert _detail(exc)["code"] == "run_generation_changed"
    assert _detail(exc)["message"] == "stale caller"
    assert _detail(exc)["current_generation"] == MOVED


def test_a_run_with_no_durable_identity_is_refused_as_changed_not_as_current():
    """These three endpoints deliberately fold "no identity" into "changed" — none of them is the
    paying path, so there is nothing to distinguish for the caller. What must NOT happen is an
    empty generation comparing equal to something and letting the request through."""
    with pytest.raises(HTTPException) as exc:
        _fence("", "")
    assert exc.value.status_code == 409
    assert _detail(exc)["current_generation"] is None


def test_two_absent_generations_do_not_match_each_other():
    """The `not current_generation` clause carries no weight while `expected_generation` is
    regex-validated as 64 hex upstream — but it is what keeps this fence closed if that validation
    is ever relaxed, because "" == "" would otherwise read as a matching run."""
    with pytest.raises(HTTPException) as exc:
        _fence("", "", expected="")
    assert exc.value.status_code == 409
    assert _detail(exc)["message"] == "stale caller"


def test_a_projection_prepared_against_another_generation_is_refused_separately():
    """The caller is current, but the server materialized its concept core before the run moved.
    Serving that would answer a question about a run state nobody asked about."""
    with pytest.raises(HTTPException) as exc:
        _fence(GENERATION, MOVED)
    assert exc.value.status_code == 409
    assert _detail(exc)["message"] == "stale projection"
    assert _detail(exc)["current_generation"] == GENERATION


def test_a_projection_with_no_generation_at_all_is_refused():
    """A core that never recorded its generation cannot be shown to match; unknown fails closed."""
    for absent in (None, ""):
        with pytest.raises(HTTPException) as exc:
            _fence(GENERATION, absent)
        assert _detail(exc)["message"] == "stale projection"


def test_the_paths_are_validated_before_the_generation_is_read():
    """Reading a generation out of an unvalidated path would be reading it from wherever a
    traversal pointed, and then fencing the real run against that answer."""
    order = []

    class _Ordered(_Commands):
        def validate_paths(self, rd):
            order.append("validate")
            return super().validate_paths(rd)

        def run_generation(self, rd):
            order.append("generation")
            return super().run_generation(rd)

    srv = _Srv(_Ordered(GENERATION))
    _assert_lens_generation(
        srv, Path("/runs/demo"), core_generation=GENERATION, expected_generation=GENERATION,
        stale_message="m", stale_remediation="r", prepared_message="p")
    assert order == ["validate", "generation"]
