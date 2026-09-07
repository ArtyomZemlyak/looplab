"""`origin` is provenance the SERVER derives, so a client may not assert it.

The inject normalizer already refuses a client that supplies any of the fork receipt's stamped
fields (`fork_receipt_forged`, "derived by the server and must not be supplied"). `origin` is the
same kind of claim and was the one a caller could write.

WHAT IT ASSERTS. `_import_cross_run_source` mints it from the source node it has just read — run id,
node id, that node's `robust_metric`, its lifecycle generation. The fold keeps it verbatim, the DAG
renders it as a verified cross-run seed CARRYING A METRIC, and `routers/reviews.py::_SUMMARY_OMIT_KEYS`
scrubs it from every review capability precisely because it discloses the portfolio. A submitted
`origin` is therefore a measured result in another run — one that need not exist.

THE ORDERING IS THE FIX. The refusal runs BEFORE the import, because the import mints this key: once
it has run, a server-derived `origin` and a submitted one are the same dict.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from looplab.serve.control_validation import _normalize_inject_node


class _Ctx:
    """The intake surface this normalizer touches, and nothing else."""

    def __init__(self, data):
        self.data = data

    def state(self):
        raise AssertionError("this test must be refused before it reads run state")


def _payload(**kw):
    return {"idea": {"rationale": "r", "params": {}}, "code": "print(1)", **kw}


def test_a_submitted_origin_is_refused_by_name():
    """MUTATION: drop the check -> the dict rides into the durable event and the DAG shows a
    cross-run seed with a metric nobody measured."""
    with pytest.raises(HTTPException) as info:
        _normalize_inject_node(_Ctx(_payload(
            origin={"run_id": "other", "node_id": 7, "metric": 0.99})))

    assert info.value.status_code == 400
    assert info.value.detail["code"] == "origin_forged"
    assert "must not be supplied" in info.value.detail["message"]
    assert "source_run" in info.value.detail["remediation"], (
        "a refusal owes the caller the legitimate way to do what they were trying to do")


@pytest.mark.parametrize("value", [None, {}, "", 0], ids=["null", "empty", "blank", "zero"])
def test_the_key_is_refused_however_it_is_spelled(value):
    """PRESENCE, not truthiness. `origin: null` is still a caller writing a server-derived field,
    and admitting the falsy spellings is how a check like this gets bypassed.

    MUTATION: test `data.get("origin")` instead of `"origin" in data` -> every case here passes
    through and the null one lands on the durable row.
    """
    with pytest.raises(HTTPException) as info:
        _normalize_inject_node(_Ctx(_payload(origin=value)))

    assert info.value.detail["code"] == "origin_forged"


def test_the_refusal_precedes_the_import_that_mints_it():
    """THE ORDERING IS THE FIX, and this is what pins it.

    `_import_cross_run_source` sets `data["origin"]` itself, so a check placed after it cannot tell
    a server-derived value from a submitted one. Driven by supplying BOTH a forged origin and a
    legitimate cross-run reference: if the check ran later, the import would overwrite the forgery
    and the request would be accepted with no complaint.

    MUTATION: move the check below the import -> `_Ctx.state()` raises AssertionError instead, which
    is this test saying the import ran when it should never have been reached.
    """
    with pytest.raises(HTTPException) as info:
        _normalize_inject_node(_Ctx(_payload(
            origin={"run_id": "forged", "node_id": 1, "metric": 1.0},
            source_run="real-run", source_node=2)))

    assert info.value.detail["code"] == "origin_forged"


def test_the_server_derived_path_is_untouched():
    """The regression this could most easily cause: refusing the one caller allowed to produce it.

    `_import_cross_run_source` writes `data["origin"]` AFTER the guard, so the legitimate seed path
    must still reach it. Asserted structurally — driving the real import needs a run directory — by
    the fact that the guard reads the SUBMITTED payload and the import writes afterwards.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(_normalize_inject_node))
    guard_line = next(
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant)
        and node.left.value == "origin")
    import_line = next(
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_import_cross_run_source")

    assert guard_line < import_line, (
        "the guard must precede the import; after it, a minted origin is indistinguishable from a "
        "submitted one and the legitimate seed path would be refused")
