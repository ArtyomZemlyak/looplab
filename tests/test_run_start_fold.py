"""`fold_run_start`: the run-start record's fields without the fold (review 2026-09-22, SRV2-11).

`GET /api/runs/{id}/config` overlays the run-start PINS (and the declared environment) on the
snapshot, and it paid a whole fold for them — O(log), the card ledger's finalize on top — on every
request: measured on the reviewer's 1,141-event toy log, 28 of the route's 43 ms. Every value that
overlay reads is written by ONE fold handler, `run_started`'s, so `events/replay.py::fold_run_start`
replays only those rows through that same handler.

The equality with `fold` is a property of the fold's STRUCTURE, so it is held two ways: by AST (the
handler reads nothing but `st.run_id` and no fold context; no other handler writes what it owns,
bar `trust_gate`, which the overlay never reads) and driven over logs that exercise every branch the
handler has — a real run, FIRST START WINS, a start that established no identity, malformed pins, a
later trust-gate change, no start at all.
"""
from __future__ import annotations

import ast
import pathlib

import anyio
import pytest
from fastapi.testclient import TestClient

from looplab.core.config import RUN_START_PINNED_FIELDS, run_start_pinned_settings
from looplab.core.models import Event
from looplab.events import replay
from looplab.events.replay import fold, fold_run_start
from tests.factories import make_engine

PKG = pathlib.Path(replay.__file__).resolve().parent


def _handler() -> ast.FunctionDef:
    tree = ast.parse((PKG / "replay.py").read_text(encoding="utf-8"))
    return next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "_on_run_started")


def _owned() -> set[str]:
    """Every `st.<field>` the run-start handler assigns."""
    out = set()
    for node in ast.walk(_handler()):
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else [])
        for target in targets:
            if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                    and target.value.id == "st"):
                out.add(target.attr)
    return out


# The one field the handler writes that is not the run-start's ALONE — and why that is safe here.
NOT_OWNED_ALONE = {"trust_gate": "`trust_gate_changed` moves it; the /config overlay never reads it"}


def test_the_handler_reads_nothing_but_the_identity_it_writes():
    fn = _handler()
    reads = {n.attr for n in ast.walk(fn)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
             and n.value.id == "st" and isinstance(n.ctx, ast.Load)}
    assert reads == {"run_id"}, f"the run-start handler reads more of the state: {sorted(reads)}"
    ctx = [ast.unparse(n) for n in ast.walk(fn)
           if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "ctx"]
    assert not ctx, f"the run-start handler reads the fold context: {ctx}"


def test_no_other_handler_writes_what_the_run_start_owns():
    owned = _owned()
    assert {"run_id", "eval_env", "card_driven_selection", "speculation_depth_pinned"} <= owned
    files = sorted(PKG.glob("replay*.py")) + [PKG / "card_ledger.py"]
    writers = set()
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if func.name == "_on_run_started" and path.name == "replay.py":
                continue
            for node in ast.walk(func):
                targets = (node.targets if isinstance(node, ast.Assign)
                           else [node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign))
                           else [])
                for target in targets:
                    while isinstance(target, ast.Subscript):
                        target = target.value
                    if isinstance(target, ast.Attribute) and target.attr in owned:
                        writers.add((target.attr, f"{path.name}::{func.name}"))
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Attribute)
                        and node.func.value.attr in owned
                        and node.func.attr in {"append", "extend", "update", "pop", "clear",
                                               "remove", "add", "discard", "insert", "setdefault"}):
                    writers.add((node.func.value.attr, f"{path.name}::{func.name}"))
    assert {field for field, _ in writers} == set(NOT_OWNED_ALONE), sorted(writers)
    assert not set(NOT_OWNED_ALONE) & RUN_START_PINNED_FIELDS


def _rows(*rows) -> list[Event]:
    return [Event(seq=i, ts=float(i), type=t, data=d) for i, (t, d) in enumerate(rows)]


_START = {"run_id": "r", "task_id": "t", "direction": "max", "card_driven_selection": True,
          "speculation_depth": 2, "select_verifier": True, "select_verifier_samples": 5,
          "verifier_ci_tie": True, "holdout_fraction": 0.25, "holdout_select": True,
          "require_approval": True, "eval_env": {"SEED": "7", "N": 3}}

LOGS = {
    "one start": _rows(("run_started", _START)),
    "first start wins": _rows(("run_started", _START),
                              ("run_started", {**_START, "direction": "min",
                                               "card_driven_selection": False,
                                               "eval_env": {"OTHER": "x"}})),
    "a start without identity is folded over": _rows(
        ("run_started", {**_START, "run_id": "", "speculation_depth": 9}),
        ("run_started", {**_START, "speculation_depth": 1})),
    "malformed pins": _rows(("run_started", {**_START, "speculation_depth": "4",
                                             "card_driven_selection": "yes",
                                             "require_approval": 1, "eval_env": ["x"],
                                             "holdout_fraction": float("nan")})),
    "a later trust-gate change": _rows(("run_started", {**_START, "trust_gate": "audit"}),
                                       ("trust_gate_changed", {"trust_gate": "block"})),
    "no start": _rows(("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                        "idea": {"operator": "draft", "params": {}},
                                        "code": ""})),
}


@pytest.fixture(scope="module")
def toy_log(tmp_path_factory):
    engine = make_engine(tmp_path_factory.mktemp("toy") / "run", n_seeds=2, max_nodes=3)
    anyio.run(engine.run)
    return engine.store.read_all()


def _logs(toy_log):
    return {**LOGS, "a real toy run": toy_log}


@pytest.mark.parametrize("name", [*LOGS, "a real toy run"])
def test_the_run_start_read_equals_the_fold_on_every_field_it_owns(name, toy_log):
    events = _logs(toy_log)[name]
    full, start = fold(events), fold_run_start(events)
    for field in sorted(_owned() - set(NOT_OWNED_ALONE)):
        assert getattr(start, field) == getattr(full, field), (name, field)
    assert run_start_pinned_settings(start) == run_start_pinned_settings(full), name


def test_the_config_route_does_not_fold_the_log(tmp_path, monkeypatch):
    """Driven: a GET of the config panel makes no full fold, and still overlays the pins."""
    from looplab.serve import appstate
    from looplab.serve.routers import runs
    from looplab.serve.server import make_app

    engine = make_engine(tmp_path / "demo", n_seeds=2, max_nodes=2)
    anyio.run(engine.run)
    folds = []

    def counting(events):
        folds.append(1)
        return fold(events)

    monkeypatch.setattr(appstate, "fold", counting)
    monkeypatch.setattr(runs, "fold", counting)
    response = TestClient(make_app(tmp_path)).get("/api/runs/demo/config")
    assert response.status_code == 200, response.text
    meta = response.json()["_looplab_config_meta"]
    assert set(meta["run_start_pinned_fields"]) == set(
        run_start_pinned_settings(fold(engine.store.read_all())))
    assert folds == [], f"GET /config folded the log {len(folds)} time(s)"
