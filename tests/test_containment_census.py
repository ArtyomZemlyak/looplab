"""Containment made countable (doc 52 row 14; doc 50 XP-03).

The house exception posture is contain-and-continue, and until this census nothing MEASURED it:
670 blind handlers under `looplab/`, 64 with no annotation at all, 103 with a `# noqa: BLE001` and
no reason, and no linter configured anywhere — so the annotations documented nothing, and a
contained failure left no mark on the span it happened in. Four rules, each driven below:

1. THE ALLOW-LIST IS THE ANNOTATION. Every blind handler that does not re-raise carries
   `# noqa: BLE001 — <why this is safe to contain>`. The 103 that still say nothing are listed in
   `tests/data/containment_unreviewed.txt`, keyed by `path::qualname#ordinal`, and that list may
   only SHRINK: reviewing a site means writing its reason and deleting its row, and a NEW blind
   handler with no reason is red rather than one more line of cargo.
2. THE PAID-CALL FUNNEL. Every blind handler around a paid call in the run path re-raises
   `BudgetExceeded` first — a swallowed spend stop lets a run keep billing past the limit set to
   stop it, which is what `verifier.py::verify` did at a SELECTION site (doc 50 AG-01).
3. `contain(reason, exc)` stamps the enclosing span and counts, and refuses the budget stop.
4. `looplab timings` reports the count, and a run that contained nothing is byte-identical.

The census is re-derived by AST here, so the suite needs no `ruff`; `[tool.ruff]` with `BLE` is
the command-line twin (`python -m ruff check looplab`) and is pinned to stay configured.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from _source_scan import PKG, iter_trees

from looplab.core import tracing
from looplab.core.containment import (
    CONTAINED_ATTR, CONTAINED_EVENT, contain, containment_counts, reset_containment_counts)
from looplab.core.llm import BudgetExceeded

ROOT = PKG.parent
UNREVIEWED = ROOT / "tests" / "data" / "containment_unreviewed.txt"
HAS_REASON = re.compile(r"noqa:\s*BLE001\s*[-—–:]\s*\S")
HAS_NOQA = re.compile(r"noqa:\s*BLE001")
# The run path: where a paid call's BudgetExceeded is the operator's spend ceiling ending the run.
# `serve/` is deliberately outside it — its loops answer one HTTP request under their own error
# envelope, and a budget stop there is surfaced by that envelope, not by ending a run.
RUN_PATH = ("engine", "agents", "adapters", "search", "trust", "tools")
PAID = frozenset({"complete", "complete_text", "forced_structured", "drive_tool_loop",
                  "agentic_text", "structured_judge", "run_phase", "_pilot_emit"})


def _is_blind(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if t is None:
        return True
    names = list(t.elts) if isinstance(t, ast.Tuple) else [t]
    return any(getattr(n, "id", getattr(n, "attr", "")) in ("Exception", "BaseException")
               for n in names)


def _is_budget(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if t is None:
        return False
    names = list(t.elts) if isinstance(t, ast.Tuple) else [t]
    return any(getattr(n, "id", getattr(n, "attr", "")) == "BudgetExceeded" for n in names)


def _reraises(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(n, ast.Raise) for n in ast.walk(handler))


def _called(nodes) -> set[str]:
    out: set[str] = set()
    for node in nodes:
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                out.add(getattr(n.func, "attr", None) or getattr(n.func, "id", None) or "")
    return out


def _blind_handlers():
    """Every blind, non-re-raising handler under `looplab/` with its stable key and source line."""
    for path, tree in iter_trees():
        lines = path.read_text(encoding="utf-8").splitlines()
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        def qualname(node) -> str:
            parts = []
            cur = parents.get(node)
            while cur is not None:
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    parts.append(cur.name)
                cur = parents.get(cur)
            return ".".join(reversed(parts)) or "<module>"

        ordinal: dict = {}
        for handler in ast.walk(tree):
            if not isinstance(handler, ast.ExceptHandler) or not _is_blind(handler):
                continue
            if _reraises(handler):
                continue
            q = qualname(handler)
            k = ordinal.get(q, 0)
            ordinal[q] = k + 1
            rel = path.relative_to(ROOT).as_posix()
            yield f"{rel}::{q}#{k}", rel, handler.lineno, lines[handler.lineno - 1]


def _unreviewed() -> list[str]:
    return [ln.strip() for ln in UNREVIEWED.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ------------------------------------------------------------------ 1. the allow-list

def test_every_blind_handler_states_its_reason_or_is_listed_unreviewed():
    """MUTATION: add `except Exception: pass` anywhere under looplab/ -> red, naming the site and
    the two ways out (a reason, or — for a site you are not reviewing today — its key here)."""
    listed = set(_unreviewed())
    offenders = []
    for key, rel, lineno, line in _blind_handlers():
        if HAS_REASON.search(line) or key in listed:
            continue
        offenders.append(f"{rel}:{lineno} [{key}]  {line.strip()[:80]}")
    assert not offenders, (
        "blind handler(s) with no stated reason. Write `# noqa: BLE001 — <why this is safe to "
        "contain>` on the except line, or add the key to tests/data/containment_unreviewed.txt "
        "if it is a pre-existing site you are deliberately not reviewing yet:\n  "
        + "\n  ".join(offenders))


def test_every_blind_handler_is_annotated_at_all():
    """The linter's own rule, re-derived without the linter: no bare blind except survives."""
    bare = [f"{rel}:{lineno}" for key, rel, lineno, line in _blind_handlers()
            if not HAS_NOQA.search(line)]
    assert not bare, bare


def test_the_unreviewed_list_only_shrinks_and_names_live_sites():
    """A row whose site now carries a reason (or is gone) is STALE and must be deleted — the list
    is the backlog of the review, and a backlog that keeps closed rows is the drift this repo's
    open-item rules exist to end. MUTATION: write a reason at a listed site without deleting its
    row -> red."""
    live = {key: line for key, _rel, _lineno, line in _blind_handlers()}
    rows = _unreviewed()
    assert len(rows) == len(set(rows)), "duplicate rows"
    stale = [r for r in rows if r not in live or HAS_REASON.search(live[r])]
    assert not stale, "delete these rows from tests/data/containment_unreviewed.txt:\n  " + "\n  ".join(stale)
    # The ratchet: the number here is the size of the review backlog on 2026-09-06 and may go
    # DOWN with every review; a larger list is a new site slipped in under the old ones.
    assert len(rows) <= 103, len(rows)


def test_a_reason_is_text_and_not_a_bare_dash():
    """`# noqa: BLE001 —` with nothing after it would satisfy a lazier regex."""
    assert not HAS_REASON.search("except Exception:  # noqa: BLE001 —")
    assert not HAS_REASON.search("except Exception:  # noqa: BLE001")
    assert HAS_REASON.search("except Exception:  # noqa: BLE001 — best-effort cleanup")
    assert HAS_REASON.search("except Exception:  # noqa: BLE001 - best-effort cleanup")


# ------------------------------------------------------------------ 2. the paid-call funnel

def _paid_try_blocks():
    for path, tree in iter_trees():
        rel = path.relative_to(PKG).as_posix()
        if rel.split("/", 1)[0] not in RUN_PATH:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            if not (_called(node.body) & PAID):
                continue
            yield path.relative_to(ROOT).as_posix(), node


def test_every_blind_handler_around_a_paid_call_in_the_run_path_reraises_the_budget_stop():
    """THE FUNNEL (doc 50 AG proposal 2). MUTATION: delete the `except BudgetExceeded: raise`
    above any of the thirteen sites this change added it to -> red, naming the site.

    `resilient` is the same rule as a function and is exempt by construction — its blind handler
    sits after its own `except BudgetExceeded: raise`."""
    offenders = []
    for rel, node in _paid_try_blocks():
        guarded = False
        for handler in node.handlers:
            if _is_budget(handler) and _reraises(handler):
                guarded = True
            elif _is_blind(handler) and not guarded and not _reraises(handler):
                offenders.append(f"{rel}:{handler.lineno}")
    assert not offenders, (
        "a blind `except` around a paid call swallows BudgetExceeded — add `except BudgetExceeded: "
        "raise` BEFORE it (core/containment.py explains):\n  " + "\n  ".join(offenders))


def test_the_funnel_census_is_not_vacuous():
    """The guard above proves nothing if it finds no paid try blocks."""
    assert sum(1 for _ in _paid_try_blocks()) >= 13


# ------------------------------------------------------------------ 3. contain()

def test_contain_counts_and_never_raises_for_an_ordinary_failure():
    reset_containment_counts()
    try:
        raise ValueError("boom")
    except ValueError as exc:
        contain("unit test", exc)
    contain("unit test")
    assert containment_counts() == {"unit test": 2}


def test_contain_refuses_the_budget_stop():
    """Adopting the helper at a site is adopting the funnel."""
    with pytest.raises(BudgetExceeded):
        try:
            raise BudgetExceeded("spend ceiling")
        except BudgetExceeded as exc:
            contain("would swallow the budget stop", exc)


def test_contain_stamps_the_enclosing_span(tmp_path):
    """Driven through a real Tracer: the count and the event land on the durable span record."""
    from looplab.core.tracing import JsonlSpanExporter, Tracer

    tr = Tracer(JsonlSpanExporter(tmp_path / "spans.jsonl"), run_id="r")
    with tr.span("operation", kind="operation") as sp:
        try:
            raise RuntimeError("secret sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234")
        except RuntimeError as exc:
            contain("driven", exc)
            contain("driven again", exc)
        assert sp.attributes[CONTAINED_ATTR] == 2
    tr.shutdown()
    rows = [__import__("json").loads(ln) for ln in (tmp_path / "spans.jsonl").read_text().splitlines()]
    row = next(r for r in rows if r.get("name") == "operation")
    assert row["attributes"][CONTAINED_ATTR] == 2
    events = [e for e in row["events"] if e["name"] == CONTAINED_EVENT]
    assert [e["reason"] for e in events] == ["driven", "driven again"]
    assert all(e["exc"] == "RuntimeError" for e in events)


def test_contain_is_a_no_op_stamp_outside_any_span():
    assert tracing.current_span_handle() is None
    contain("nowhere")           # must not raise


def test_resilient_counts_its_containments():
    from looplab.agents.tool_loop import resilient

    reset_containment_counts()
    assert resilient(lambda: 1 / 0, lambda: "safe", reason="strategist") == "safe"
    assert containment_counts() == {"strategist": 1}
    with pytest.raises(BudgetExceeded):
        resilient(lambda: (_ for _ in ()).throw(BudgetExceeded("stop")), lambda: "safe")


# ------------------------------------------------------------------ 4. timings

def _timings_output(tmp_path, spans: list[dict]) -> str:
    import json

    from typer.testing import CliRunner

    from looplab.cli import app

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "spans.jsonl").write_text("\n".join(json.dumps(s) for s in spans) + "\n")
    return CliRunner().invoke(app, ["timings", str(run_dir)]).output


def _span(**over) -> dict:
    base = {"span_id": "a", "trace_id": "t", "name": "evaluate", "kind": "operation",
            "start": 1.0, "duration_s": 2.0, "attributes": {"node_id": 1}, "events": []}
    base.update(over)
    return base


def test_timings_reports_contained_failures_and_is_byte_identical_without_them(tmp_path):
    clean = _timings_output(tmp_path / "clean", [_span()])
    assert "contained failures" not in clean
    stamped = _timings_output(tmp_path / "stamped", [_span(
        attributes={"node_id": 1, CONTAINED_ATTR: 2},
        events=[{"name": CONTAINED_EVENT, "reason": "watchdog tick", "exc": "AttributeError"},
                {"name": CONTAINED_EVENT, "reason": "watchdog tick", "exc": "AttributeError"}])])
    assert "contained failures: 2 across 1 span(s)" in stamped
    assert "2 × watchdog tick (AttributeError)" in stamped
    # Everything before the roll-up is the historical report byte for byte.
    assert stamped.split("\ncontained failures")[0] == clean


# ------------------------------------------------------------------ the linter is configured

def test_the_linter_is_configured_for_exactly_this_rule():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.ruff]" in text and "[tool.ruff.lint]" in text
    assert re.search(r'select\s*=\s*\["BLE"\]', text), "BLE is the one rule; no style rule is enabled"
    assert re.search(r'"ruff>=[0-9.]+"', text), "ruff belongs to the dev extras"
    assert not (ROOT / ".ruff.toml").exists(), "one config home, pyproject"
