"""The Developer-crash transaction has one spelling (doc 25 EC-03).

A Developer that cannot finish returns its error IN BAND as the node's code. Left pending, the eval
runs the PARENT's carried-over entrypoint and inherits the PARENT's metric — a false success that
pollutes the search. So the node is FAILED, and the run is PAUSED as a circuit-breaker.

That pair was hand-spelled at five sites, and its pause reasons had already drifted into four
different strings. Nothing failed when a copy drifted: a missing field or a reordered pair produces
a run that looks paused and a node that looks failed, right up until a replay disagrees.

What is shared is the RECORDS — event types, order, field names, defaults. What is NOT shared, on
purpose, is how each site appends them: the speculation sites use one tail CAS, the fan-out queues
its pause for the main task (a worker-written EV_PAUSE races EV_RESUME for byte position), and the
serial sites append sequentially. This file pins the shared half and the two rules that make the
per-site half safe.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from looplab.engine.node_build import developer_crash_records
from looplab.events.types import EV_NODE_FAILED, EV_PAUSE

_PKG = Path(__file__).resolve().parents[1] / "looplab"


def test_the_pair_is_terminal_then_pause_with_the_documented_fields():
    records = developer_crash_records(7, 2, "(developer error: LLM unreachable)", "why it paused")
    assert [event_type for event_type, _data in records] == [EV_NODE_FAILED, EV_PAUSE], (
        "the terminal must come FIRST — a pause naming a node with no terminal reads as an "
        "operator freeze, and a replay that stops between the two would see exactly that")
    terminal, pause = (data for _type, data in records)
    assert terminal == {"node_id": 7, "generation": 2,
                        "error": "(developer error: LLM unreachable)",
                        "reason": "developer_crash", "eval_seconds": 0.0}
    assert pause == {"node_id": 7, "generation": 2, "reason": "why it paused"}
    # `eval_seconds: 0.0` is not decoration: the fold adds it to the cumulative eval budget, and a
    # crashed build consumed none of it.
    assert terminal["eval_seconds"] == 0.0


def test_the_pause_names_the_same_node_and_generation_as_its_terminal():
    """A pause stamped with another generation is not a circuit-breaker for THIS build — the fold
    keys the pause on (node_id, generation) and a reset bumps the generation."""
    terminal, pause = (data for _type, data in
                       developer_crash_records(3, 5, "(developer error: x)", "r"))
    assert (pause["node_id"], pause["generation"]) == (terminal["node_id"], terminal["generation"])


def test_the_recovery_mode_appends_a_pause_alone():
    """`_close_developer_sentinel_once` recovers a node that is ALREADY terminal with its pause
    lost. A second terminal there would break the one-terminal-per-node invariant."""
    records = developer_crash_records(4, 0, "(developer error: x)", "recovered", terminal=False)
    assert [event_type for event_type, _data in records] == [EV_PAUSE]
    assert records[0][1] == {"node_id": 4, "generation": 0, "reason": "recovered"}


def test_the_reason_is_the_callers_but_the_rest_is_not():
    """Four sites, four situations an operator must be able to tell apart — but only the prose
    differs. Anything a replay reads is fixed here."""
    first = developer_crash_records(1, 0, "(developer error: a)", "fresh build")
    second = developer_crash_records(1, 0, "(developer error: a)", "operator inject")
    assert first[1][1]["reason"] != second[1][1]["reason"]
    assert first[0] == second[0]                       # the terminal is byte-identical
    assert first[1][1]["node_id"] == second[1][1]["node_id"]


ENGINE_SITES = ["engine/orchestrator.py", "engine/speculation.py"]


@pytest.mark.parametrize("relative", ENGINE_SITES)
def test_no_engine_module_respells_the_pair(relative):
    """Source guard, in the spirit of the seam registries in CLAUDE.md.

    `"reason": "developer_crash"` is the signature of a hand-built terminal. It may appear only in
    the builder; a READ of `error_reason == "developer_crash"` is a different thing and is not
    matched by this pattern."""
    source = (_PKG / relative).read_text(encoding="utf-8")
    offenders = [
        f"{relative}:{number}" for number, line in enumerate(source.splitlines(), 1)
        if '"reason": "developer_crash"' in line.split("#", 1)[0]
    ]
    assert not offenders, (
        "these sites build the Developer-crash terminal themselves instead of calling "
        f"node_build.developer_crash_records: {offenders}")
    assert "developer_crash_records" in source, (
        f"{relative} no longer references the shared builder — it either lost its crash handling "
        "or re-spelled the transaction some other way")


def test_the_source_guard_can_see_a_reintroduced_copy():
    """A scan that matches nothing is indistinguishable from one whose pattern is broken."""
    builder = inspect.getsource(developer_crash_records)
    assert '"reason": "developer_crash"' in builder, (
        "the pattern no longer matches even the definition site, so it would match nothing anywhere")


def test_the_fanout_site_still_queues_its_pause_instead_of_appending_it():
    """The worker-seam rule from CLAUDE.md invariant #1, which the extraction must not flatten.

    `_create_node_scoped` runs in an `anyio.to_thread` worker under the build fan-out. EV_PAUSE is a
    FOLDED, run-GLOBAL event outside the worker seam (which allows only a worker's OWN node's
    node_created / node_failed / per-node audit). It is splice-neutral for `paused` alone but NOT
    against a concurrent EV_RESUME, so the MAIN task appends it after the join."""
    # utf-8-sig: orchestrator.py carries a BOM, which `ast.parse` rejects as a non-printable char.
    source = (_PKG / "engine/orchestrator.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    create_node = next(node for node in ast.walk(tree)
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and node.name == "_create_node_scoped")
    body = ast.unparse(create_node)
    assert "crash_terminal, _crash_pause = developer_crash_records(" in body, (
        "the fan-out must take the terminal from the shared builder and leave the pause queued")
    assert "_request_create_pause" in body, (
        "the fan-out stopped requesting the pause — a worker-appended EV_PAUSE races EV_RESUME")
    assert "self.store.append(EV_PAUSE" not in body and "(EV_PAUSE," not in body, (
        "the fan-out appends EV_PAUSE directly from a worker thread; it must queue it for the "
        "main task via _request_create_pause")
