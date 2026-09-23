"""The engine folds through ONE seam: `orchestrator.fold`, reached via `shared.py::engine_fold`.

Review 2026-09-22, ENG1-04 (step 0). CLAUDE.md called the module-global `fold` in `orchestrator.py`
"the seam every helper that folds must reach through", and tests patch it to steer what the Engine
sees. Fourteen engine modules instead bound `looplab.events.replay.fold` at import time, so every
such patch silently covered only what happened to live in `orchestrator.py`, and each mixin
extraction narrowed it further with nothing going red. Two halves:

* a DRIVEN check — a real run with the seam wrapped must see folds coming from modules OTHER than
  `orchestrator.py` (the property the seam exists for);
* an AST check over `looplab/engine/`, for the residue a driven run cannot reach: no module binds
  `replay.fold` itself, at module level or inside a function, except the two named readers that are
  not the Engine's decision path.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import anyio

from factories import make_engine

ENGINE = Path(__file__).resolve().parents[1] / "looplab" / "engine"

# Modules that fold a FINISHED log for a reader outside the run loop. Routing them through the
# seam would import the whole orchestrator into an export/CLI path for no test's benefit; each
# carries its reason, and the list only shrinks.
NOT_THE_ENGINE_LOOP = {
    "bundle.py": "`looplab export-bundle` folds a finished run to write its summary row",
    "cross_run_index.py": "the cross-run index CLI folds each archived run it indexes",
}


def _binds_replay_fold(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.ImportFrom) and node.module == "looplab.events.replay"
                and any(alias.name == "fold" for alias in node.names)):
            lines.append(node.lineno)
    return lines


def test_no_engine_module_binds_the_replay_fold_itself():
    offenders = []
    for path in sorted(ENGINE.glob("*.py")):
        if path.name in {"orchestrator.py"} | set(NOT_THE_ENGINE_LOOP):
            continue
        lines = _binds_replay_fold(ast.parse(path.read_text(encoding="utf-8-sig")))
        offenders += [f"{path.name}:{line}" for line in lines]
    assert not offenders, (
        f"{offenders} bind `looplab.events.replay.fold` directly, which silently narrows every "
        "`monkeypatch.setattr(orch, 'fold', …)` — import `engine_fold as fold` from "
        "`looplab.engine.shared` instead")


def test_the_exemptions_still_exist_and_still_need_to_be_exempt():
    """Shrink-only: an exemption whose module stopped binding `replay.fold` (or vanished) is a row
    that would silently license the next direct import there."""
    for name in NOT_THE_ENGINE_LOOP:
        path = ENGINE / name
        assert path.exists(), f"{name} is exempt but no longer exists — delete its row"
        assert _binds_replay_fold(ast.parse(path.read_text(encoding="utf-8-sig"))), (
            f"{name} no longer binds `replay.fold` — delete its exemption row")


def test_a_patched_seam_sees_the_folds_of_the_other_engine_modules(tmp_path, monkeypatch):
    from looplab.engine import orchestrator

    real = orchestrator.fold
    callers: set[str] = set()

    def counting_fold(events):
        frame = sys._getframe(1)
        if frame.f_code.co_name == "engine_fold":      # reached through the seam: who called it?
            frame = frame.f_back
        callers.add(frame.f_globals.get("__name__", "?"))
        return real(events)

    monkeypatch.setattr(orchestrator, "fold", counting_fold)
    state = anyio.run(make_engine(tmp_path / "run", n_seeds=2, max_nodes=3).run)
    assert state.finished
    others = {name for name in callers if name != "looplab.engine.orchestrator"}
    # The toy run evaluates, finalizes and writes its lessons: each of those folds lives outside
    # orchestrator.py, and each used to be invisible to this exact patch.
    assert {"looplab.engine.evaluate", "looplab.engine.finalize"} <= others, (
        f"the seam saw folds only from {sorted(callers)}")


def test_the_card_sessions_fold_memo_follows_a_patch_of_the_seam(tmp_path, monkeypatch):
    """`_fold_current` memoizes a fold per observed tail, keyed on "the fold callable itself" so a
    memo cannot outlive a swap of the function. Since step 0 the module's `fold` is `engine_fold` —
    ONE stable object that resolves `orchestrator.fold` at call time — so keying on it alone missed
    exactly the patch tests make (review 2026-09-22, ES1-08: the docstring's "documented patch seam"
    was `speculation.fold`, which no test patches). Driven: same tail, seam swapped, next read.
    """
    from looplab.engine import orchestrator

    engine = make_engine(tmp_path / "run")
    engine.store.append("run_started", {"run_id": "run", "task_id": "t", "direction": "min"})
    _events, first = engine._fold_current()
    assert engine._fold_current()[1] is first, "an unmoved tail must be served from the memo"

    swapped = object()
    monkeypatch.setattr(orchestrator, "fold", lambda events: swapped)
    assert engine._fold_current()[1] is swapped, (
        "the memo served the previous fold's answer after the seam was swapped")
