"""The node terminal's `_write_lock` is not held across a filesystem/subprocess wait (review
2026-09-22, ENG2-11), and the eval prologue's workdir build does not block the event loop.

`evaluate.py::_eval_write_terminal` took `async with self._write_lock:` and only then awaited
`anyio.to_thread.run_sync(self._substrate_fingerprint)` — `git rev-parse` / `git status` / `git diff`
with a 15-second deadline each, or an `rglob`+`stat` walk of a non-git tree. It was the ONLY `await`
under `_write_lock` in the engine, so every other writer that needs the lock — every sibling's
terminal, every repair row, every stage row — queued behind one node's git read. The substrate depends
on nothing the lock guards, so it is now read BEFORE the lock is taken; the terminal carries the same
value.

`_eval_prepare_workdir` materialized the node's workdir — a seed copy measured at up to 1,017 MB — on
the event loop itself, freezing every session turn, watcher and heartbeat for the copy's duration.
It writes only this lifecycle's own directory and appends only DIAGNOSTIC rows (`workspace_seeded`,
`node_build_delta`, invariant #1's thread-appendable set), so the driver now runs it in a worker.

Both properties are DRIVEN through the real `_evaluate` against a real engine.
"""
from __future__ import annotations

import threading

import anyio

from looplab.events.replay import fold
from looplab.runtime.command_eval import RunResult
from tests.factories import make_engine


def _engine_with_one_node(tmp_path):
    engine = make_engine(tmp_path / "run")
    engine.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": "r"}, "code": "print(1)"})

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None, start_stage=None):
        return RunResult(exit_code=0, stdout='{"metric": 0.5}', metric=0.5, timed_out=False,
                         stderr="")

    engine._run_eval = fake_run_eval
    return engine


def test_a_sibling_writer_is_not_queued_behind_this_nodes_substrate_read(tmp_path, monkeypatch):
    """While one node's terminal is reading the substrate (a git call that can take up to its full
    deadline), another writer must still be able to take `_write_lock`. The substrate here blocks
    until the sibling has written — under the old ordering that is a deadlock the bounded wait turns
    into a red test instead of a hung suite."""
    from looplab.engine import evaluate as evaluate_module

    handed: list = []
    real_record = evaluate_module.comparability_record

    def spy_record(**kwargs):
        handed.append(kwargs.get("substrate"))
        return real_record(**kwargs)

    monkeypatch.setattr(evaluate_module, "comparability_record", spy_record)
    engine = _engine_with_one_node(tmp_path)
    # A repo task's shape as far as the terminal reads it: the substrate is asked only when the
    # task has a repo spec. Nothing is seeded — the build is replaced below.
    engine._repo_spec = {"editables": []}
    engine._materialize = lambda node, workdir: workdir.mkdir(parents=True, exist_ok=True)
    reading = threading.Event()
    release = threading.Event()

    def slow_substrate() -> dict:
        reading.set()
        release.wait(20)
        return {"repo": "sha-substrate"}

    engine._substrate_fingerprint = slow_substrate
    sibling: dict = {}

    async def _main():
        async with anyio.create_task_group() as tg:
            tg.start_soon(engine._evaluate, 0, anyio.CapacityLimiter(1), None)
            try:
                await anyio.to_thread.run_sync(reading.wait, 20)
                assert reading.is_set(), "precondition: the terminal never read the substrate"
                with anyio.move_on_after(5) as scope:
                    async with engine._write_lock:          # a sibling's terminal, say
                        sibling["wrote"] = True
                sibling["timed_out"] = scope.cancelled_caught
            finally:
                release.set()

    anyio.run(_main)
    assert sibling == {"wrote": True, "timed_out": False}, (
        f"another writer waited on `_write_lock` behind this node's substrate read: {sibling}")
    # …and the terminal still builds its comparability record from the value that was read
    node = fold(engine.store.read_all()).nodes[0]
    assert node.status == "evaluated" and node.metric == 0.5
    assert handed == [{"repo": "sha-substrate"}], (
        f"the substrate read before the lock is not the one the terminal recorded: {handed}")


def test_the_workdir_is_built_off_the_event_loop(tmp_path):
    """The workdir build runs in a worker thread: a coroutine on the loop keeps turning while it
    runs. The build here waits for that coroutine to tick — on the loop itself it could never see
    one, and the bounded wait fails instead of hanging."""
    engine = _engine_with_one_node(tmp_path)
    ticks: list[int] = []
    built: dict = {}

    def slow_materialize(node, workdir):
        built["thread"] = threading.current_thread() is not threading.main_thread()
        for _ in range(200):                        # up to ~4 s for the loop to show it is alive
            if len(ticks) >= 3:
                break
            threading.Event().wait(0.02)
        built["ticks_seen"] = len(ticks)
        workdir.mkdir(parents=True, exist_ok=True)

    engine._materialize = slow_materialize

    async def _ticker():
        # bounded: a build that raises before recording must not leave this spinning forever
        while "ticks_seen" not in built and len(ticks) < 2000:
            ticks.append(1)
            await anyio.sleep(0.01)

    async def _main():
        async with anyio.create_task_group() as tg:
            tg.start_soon(_ticker)
            await engine._evaluate(0, anyio.CapacityLimiter(1), None)

    anyio.run(_main)
    assert built.get("thread") is True, "the workdir was built on the event loop's own thread"
    assert built.get("ticks_seen", 0) >= 3, (
        f"the event loop did not turn while the workdir was being built: {built}")
    assert fold(engine.store.read_all()).nodes[0].status == "evaluated"


def test_no_engine_writer_awaits_while_it_holds_the_write_lock():
    """The residue, by AST (CLAUDE.md's tier 3): no `await` — and no nested `async with`/`async for`,
    which await too — inside any `async with ... _write_lock` block in the engine package. The site
    above was the only one on 2026-09-22; a new one queues every writer behind whatever it waits on.
    The driven test above is the property; this keeps a second site from arriving quietly."""
    import ast

    from tests._source_scan import PKG, iter_trees

    offenders = []
    for path, tree in iter_trees(PKG / "engine"):
        for block in ast.walk(tree):
            if not (isinstance(block, ast.AsyncWith) and any(
                    "_write_lock" in ast.unparse(item.context_expr) for item in block.items)):
                continue
            for node in ast.walk(block):
                if node is block:
                    continue
                if isinstance(node, (ast.Await, ast.AsyncWith, ast.AsyncFor)):
                    offenders.append(f"{path.name}:{node.lineno}: {ast.unparse(node)[:90]}")
    assert offenders == [], "an `await` under `_write_lock`:\n  " + "\n  ".join(offenders)
