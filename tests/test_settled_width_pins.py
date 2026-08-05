"""The two concurrency widths are AUTO by default — so the log, not the box, must own them.

`eval_parallel`/`llm_parallel` ship as `0` = AUTO, a SENTINEL resolved at startup off the live
machine. Three properties follow, and none of them held:

H-7  `config.snapshot.json` stores the sentinel, and `run_started` carried no parallelism key at all,
     so re-entry re-derived the width from whatever box it landed on. A 1-GPU run resumed on a 2-GPU
     host silently doubled its eval concurrency and flipped the build spine from the serial one to
     the concurrent-append seam (engine invariant #1) MID-LOG. `run_started` now pins the RESOLVED
     integers, exactly as `_resolve_speculation_depth` already does for its own AUTO sentinel
     (invariant #6): an AUTO re-entry ADOPTS the pin, an explicitly spelled disagreement FAILS CLOSED
     before any append, and a durable `budget_extend` still owns the axis it retuned.

H-6  With `llm_parallel` AUTO and `cli/__init__.py` wiring `role_factory` unconditionally, the
     documented offline smoke (`--backend toy`) fanned its builds out on any GPU box: 8 identical runs
     produced 3 distinct event orders. CLAUDE.md invariant #1 promises that "a settled build width of
     1 keeps the strict 'only the main task appends' behaviour, byte-identical", and `bench.py` — the
     capability-regression harness — advertises "Deterministic for the toy backend" while inheriting
     the same AUTO width. AUTO on a build that calls NO LLM now settles to 1: fan-out exists to
     overlap provider latency, and a toy build has none to overlap.

H-2  Both live-log watchdogs are `@in_llm_lane("enrichment")`, capped at 1 by
     `default_llm_lane_limits` — but the cap was welded to a finite global total, which AUTO does not
     activate. The broker was handed `{}`, i.e. DISABLED, so the lane annotation was decorative and
     2 concurrent evals x 2 LLM-calling watchdogs ran 4 unbounded background calls (16 on an 8-GPU
     host). The background caps now apply with or without a total; the foreground lanes do not.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import anyio
import pytest
from typer.testing import CliRunner

import looplab.cli.run_cmds as _run_cmds
import looplab.engine.orchestrator as _orch
import looplab.engine.resources as _resources
from looplab.adapters.toytask import ToyTask
from looplab.agents.roles import ToyObjectiveDeveloper, ToyResearcher
from looplab.core.llm_broker import (default_llm_lane_limits, llm_broker_scope, llm_lane_scope,
                                     llm_request_permit)
from looplab.core.models import RunState
from looplab.cli import app as _app
from looplab.engine.orchestrator import Engine, RunStartPinError, SettledWidthPinError
from looplab.engine.widths import settled_width_refusal
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

_ENGINE_DIR = Path(_orch.__file__).parent
_runner = CliRunner()


def _gpu_capable(monkeypatch) -> None:
    """Speak for a GPU-capable task.

    AUTO means "one experiment per detected GPU", so it is hardware-derived only for a task that can
    USE one; ToyTask declares `gpu_capable() -> False` (closed-form arithmetic) and therefore settles
    its own AUTO eval width to serial. Every property about the width FOLLOWING the box needs a task
    the box can serve — `gpu_capable` is the same TaskAdapter hook the resource scheduler already
    reads, so this is the production signal, not a test-only seam.
    """
    monkeypatch.setattr(ToyTask, "gpu_capable", lambda self: True)


def _engine(run_dir, *, llm_backed: bool = False, **kw) -> Engine:
    """A Toy engine. `llm_backed` marks the researcher the way an LLM role is marked in production:
    it carries the shared client (`agents/roles.py::LLMResearcher`), which is exactly what
    `_build_calls_an_llm` reads."""
    task = ToyTask()
    researcher = ToyResearcher(task.bounds, seed=task.seed, step=task.step)
    if llm_backed:
        researcher.client = object()
    return Engine(run_dir, task=task, researcher=researcher,
                  developer=ToyObjectiveDeveloper(), sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=2, max_nodes=2), n_seeds=2, max_nodes=2, **kw)


def _run_started(engine: Engine) -> dict:
    return next(e.data for e in engine.store.read_all() if e.type == "run_started")


def _entry(**run_started) -> RunState:
    """A folded run-start prefix, so the re-pin can be driven without a whole run."""
    payload = {"run_id": "r", "task_id": "t", "direction": "min"}
    payload.update(run_started)
    state = RunState()
    from looplab.events.replay import _on_run_started
    _on_run_started(state, None, payload, None)
    return state


# --------------------------------------------------------------------------- #
# H-7 — the RESOLVED width is pinned, not the AUTO sentinel
# --------------------------------------------------------------------------- #

def test_run_started_pins_the_resolved_widths_not_the_auto_sentinel(tmp_path, monkeypatch):
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / "pin", eval_parallel=0, llm_parallel=0, llm_backed=True)
    anyio.run(engine.run)

    data = _run_started(engine)
    # The integers this run actually executed at — NOT the `0` the snapshot stores. A sentinel in the
    # log would be re-resolved against the resuming box, which is the whole defect.
    assert data["eval_parallel"] == 2 and data["llm_parallel"] == 2
    assert fold(engine.store.read_all()).eval_parallel == 2


def test_an_auto_resume_adopts_the_pinned_width_on_a_smaller_box(tmp_path, monkeypatch):
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    _gpu_capable(monkeypatch)
    first = _engine(tmp_path / "run", eval_parallel=0, llm_parallel=0, llm_backed=True)
    anyio.run(first.run)
    assert first._eval_parallel == 2 and first._llm_parallel == 2

    # Same run directory, HALF the box (`CUDA_VISIBLE_DEVICES=0`). AUTO asked the box to decide at
    # LAUNCH; on re-entry the run's own log outranks a different box, or the log would carry two
    # execution treatments spliced together with nothing recording the change.
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0])
    resumed = _engine(tmp_path / "run", eval_parallel=0, llm_parallel=0, llm_backed=True)
    assert resumed._eval_parallel == 1 and resumed._llm_parallel == 1   # re-derived, before the re-pin
    anyio.run(resumed.run)
    assert resumed._eval_parallel == 2, "the resumed engine re-derived its eval width from the box"
    assert resumed._llm_parallel == 2, "the resumed engine re-derived its build width from the box"


def test_an_explicitly_spelled_mismatch_fails_closed_without_touching_the_log(tmp_path, monkeypatch):
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    _gpu_capable(monkeypatch)
    first = _engine(tmp_path / "run", eval_parallel=0, llm_parallel=0, llm_backed=True)
    anyio.run(first.run)
    before = len(first.store.read_all())

    resumed = _engine(tmp_path / "run", eval_parallel=4, llm_parallel=0, llm_backed=True)
    with pytest.raises(SettledWidthPinError, match=r"run_started pinned 2"):
        anyio.run(resumed.run)
    # A refused re-entry must leave the log it declined to trust EXACTLY as it found it — the CLI's
    # fatal-error path would otherwise write run_finished + finalization receipts on the way out.
    assert len(resumed.store.read_all()) == before
    assert isinstance(SettledWidthPinError("x"), RunStartPinError)


def test_an_explicit_width_matching_the_pin_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    _gpu_capable(monkeypatch)
    anyio.run(_engine(tmp_path / "run", eval_parallel=0, llm_parallel=0, llm_backed=True).run)

    # Spelling the pinned width explicitly is agreement, not a changed treatment.
    resumed = _engine(tmp_path / "run", eval_parallel=2, llm_parallel=2, llm_backed=True)
    anyio.run(resumed.run)
    assert (resumed._eval_parallel, resumed._llm_parallel) == (2, 2)


def test_a_durable_budget_extend_owns_the_axis_it_retuned(tmp_path, monkeypatch):
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    _gpu_capable(monkeypatch)
    first = _engine(tmp_path / "run", eval_parallel=0, llm_parallel=0, llm_backed=True)
    anyio.run(first.run)
    # The operator's supported mid-run retune. `_apply_control_overrides` re-applies it on EVERY turn,
    # so the launch flag no longer decides the running width — refusing the resume over it would be a
    # false alarm about a value that has no effect.
    first.store.append("budget_extend", {"eval_parallel": 4})

    resumed = _engine(tmp_path / "run", eval_parallel=4, llm_parallel=0, llm_backed=True)
    anyio.run(resumed.run)          # must not raise
    assert resumed._eval_parallel == 4


def test_a_log_written_before_the_pin_keeps_its_own_startup_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1, 2, 3])
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / "legacy", eval_parallel=0, llm_parallel=0, llm_backed=True)
    # An absent key folds to 0 = "not recorded". Inventing a width for it would be the very
    # re-derivation the pin prevents, so the engine neither adopts nor refuses.
    engine._repin_settled_widths(_entry())
    assert (engine._eval_parallel, engine._llm_parallel) == (4, 4)
    explicit = _engine(tmp_path / "legacy-x", eval_parallel=3, llm_backed=True)
    explicit._repin_settled_widths(_entry())
    assert explicit._eval_parallel == 3


@pytest.mark.parametrize("poison", [True, "2", 2.0, -1, 0, 2048, None])
def test_a_malformed_pin_is_not_a_width(tmp_path, poison):
    # Strict bounded ints, for the same reason speculation_depth is: a bool/string/float in a
    # hand-edited or foreign row must never reshape a run's execution treatment.
    engine = _engine(tmp_path / "poison", eval_parallel=3, llm_backed=True)
    engine._repin_settled_widths(_entry(eval_parallel=poison))
    assert engine._eval_parallel == 3
    assert fold([]).eval_parallel == 0


def test_the_pin_is_not_in_the_http_config_editors_refuse_list():
    """Both widths stay OUT of RUN_START_PINNED_FIELDS on purpose.

    That contract is the config editor's "start a new run to use different semantics" refuse-list,
    and both axes remain operator-mutable mid-run through the durable `budget_extend` control event —
    the same reason `trust_gate` is excluded from it. What re-entry owes them is narrower and lives in
    `_repin_settled_widths`.
    """
    from looplab.core.config import RUN_START_PINNED_FIELDS
    assert {"eval_parallel", "llm_parallel"}.isdisjoint(RUN_START_PINNED_FIELDS)


def test_the_calibration_evidence_schema_knows_the_two_new_keys():
    """The run_started WRITER gained two keys, so the evidence reader's exact-schema set must too.

    `speculation_quality` compares `set(started.data)` against this frozenset; a writer/reader drift
    here closes the speculation gate with a schema error instead of a real finding.
    """
    from looplab.search.speculation_quality import _CALIBRATION_RUN_STARTED_FIELDS
    assert {"eval_parallel", "llm_parallel"} <= _CALIBRATION_RUN_STARTED_FIELDS


# --------------------------------------------------------------------------- #
# H-6 — AUTO resolves off the box only where the box is the constraint
# --------------------------------------------------------------------------- #

def test_the_offline_smokes_auto_widths_both_settle_to_serial(tmp_path, monkeypatch):
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1, 2, 3])
    task = ToyTask()
    # The offline smoke's exact shape: a CPU-locked task and toy roles, but `role_factory` wired (the
    # CLI wires it unconditionally), so nothing downstream would have clamped the fan-out back to 1.
    toy = _engine(tmp_path / "toy", eval_parallel=0, llm_parallel=0,
                  role_factory=lambda: (ToyResearcher(task.bounds), ToyObjectiveDeveloper()))
    assert (toy._eval_parallel, toy._llm_parallel) == (1, 1)
    assert toy._build_calls_an_llm() is False and toy._task_gpu_capable() is False


def test_the_two_axes_narrow_independently(tmp_path, monkeypatch):
    """Each axis reads its OWN constraint — the box for evals, provider latency for builds.

    A GPU-capable task built by toy roles is the case that separates them: the eval width is real
    work the box can serve, while the build still has no provider call to overlap.
    """
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1, 2, 3])
    _gpu_capable(monkeypatch)
    task = ToyTask()
    split = _engine(tmp_path / "split", eval_parallel=0, llm_parallel=0,
                    role_factory=lambda: (ToyResearcher(task.bounds), ToyObjectiveDeveloper()))
    assert (split._eval_parallel, split._llm_parallel) == (4, 1)


def test_auto_build_width_still_follows_the_box_for_an_llm_backed_build(tmp_path, monkeypatch):
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1, 2, 3])
    _gpu_capable(monkeypatch)
    assert _engine(tmp_path / "llm", eval_parallel=0, llm_parallel=0,
                   llm_backed=True)._llm_parallel == 4
    # An external coding-agent Developer holds no client and declares `is_code_generating` instead.
    task = ToyTask()
    developer = ToyObjectiveDeveloper()
    developer.is_code_generating = True
    agentic = Engine(tmp_path / "agentic", task=task,
                     researcher=ToyResearcher(task.bounds), developer=developer,
                     sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=2),
                     eval_parallel=0, llm_parallel=0)
    assert agentic._llm_parallel == 4


def test_the_unified_agent_facade_does_not_hide_an_llm_backed_researcher(tmp_path, monkeypatch):
    """The shipped `unified_agent=True` shape, which the first version of this predicate misread.

    In unified mode `researcher IS developer` — one `UnifiedAgent` — and its
    `client`/`is_code_generating` forwarders come from `WrapsDeveloper`, so they describe the
    DEVELOPER stage only. On every adapter with a TEMPLATED Developer but an `LLMResearcher`
    (classification, regression, timeseries) both of `_build_calls_an_llm`'s probes therefore read
    the same client-less template, the whole product default answered "no LLM", and AUTO pinned the
    build width to 1 while the run called the provider once per node. Verified against a real run of
    `examples/classification_task.json`, whose `llm_usage` rows show the Researcher on the wire
    before the first node existed. The stand-in below reproduces exactly that shape: one object for
    both roles, no client of its own, an LLM-backed `researcher` stage and a templated `developer`.
    """
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1, 2, 3])
    _gpu_capable(monkeypatch)
    task = ToyTask()

    class _Facade:
        """`UnifiedAgent`'s shape: public per-stage backends, a Developer-facing `client` forwarder."""

        def __init__(self):
            self.researcher = ToyResearcher(task.bounds)
            self.researcher.client = object()      # the LLMResearcher marker
            self.developer = ToyObjectiveDeveloper()   # a template: no client, not code-generating
            self.stage_clients = []

        @property
        def client(self):                          # WrapsDeveloper: delegates to the DEVELOPER
            return getattr(self.developer, "client", None)

    facade = _Facade()
    assert facade.client is None and not getattr(facade, "is_code_generating", False)
    engine = Engine(tmp_path / "unified", task=task, researcher=facade, developer=facade,
                    sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=2),
                    eval_parallel=0, llm_parallel=0)
    assert engine._build_calls_an_llm() is True
    assert engine._llm_parallel == 4

    # …and the offline shape stays offline: the same facade with NO client anywhere still answers no,
    # so `--backend toy` keeps its serial, byte-reproducible build spine.
    facade.researcher.client = None
    offline = Engine(tmp_path / "unified-offline", task=task, researcher=facade, developer=facade,
                     sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=2),
                     eval_parallel=0, llm_parallel=0)
    assert offline._build_calls_an_llm() is False
    assert offline._llm_parallel == 1


def test_an_explicitly_spelled_toy_width_is_still_honoured(tmp_path, monkeypatch):
    # The narrowing is a resolution of AUTO, not a veto: an operator who asks a toy run to fan out
    # (a concurrency test) gets the fan-out, and its nondeterministic byte order with it.
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1, 2, 3])
    assert _engine(tmp_path / "explicit", eval_parallel=0, llm_parallel=3)._llm_parallel == 3


def test_the_offline_toy_run_has_one_byte_order(tmp_path, monkeypatch):
    """The property CLAUDE.md states unconditionally and `bench.py:6` advertises.

    The engine is the sole writer only while the settled build width is 1; above it, independent
    worker threads append their own node's events and the log's byte ORDER (never the folded state)
    becomes nondeterministic. This drives the offline smoke's exact configuration — AUTO widths on a
    multi-GPU box, `role_factory` wired — and pins the order signature to one value.
    """
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1, 2, 3])
    task = ToyTask()
    signatures = set()
    for attempt in range(3):
        engine = _engine(tmp_path / f"det{attempt}", eval_parallel=0, llm_parallel=0,
                         role_factory=lambda: (ToyResearcher(task.bounds), ToyObjectiveDeveloper()))
        anyio.run(engine.run)
        signatures.add(tuple(
            (event.type, event.data.get("node_id")) for event in engine.store.read_all()))
    assert len(signatures) == 1, "identical toy runs produced different event orders"


# --------------------------------------------------------------------------- #
# H-2 — the background lane caps apply with or without a finite total
# --------------------------------------------------------------------------- #

def _max_concurrency(broker, lane: str, width: int) -> int:
    """Drive `width` real borrows through the production admission path and report the peak."""
    live = 0
    peak = 0
    lock = threading.Lock()
    entered = threading.Semaphore(0)
    release = threading.Event()

    def work() -> None:
        nonlocal live, peak
        with llm_broker_scope(broker), llm_lane_scope(lane), llm_request_permit():
            with lock:
                live += 1
                peak = max(peak, live)
            entered.release()
            release.wait(5)
            with lock:
                live -= 1

    threads = [threading.Thread(target=work, daemon=True) for _ in range(width)]
    for thread in threads:
        thread.start()
    # Wait for as many as the lane can admit at once, then let everyone go.
    for _ in range(width):
        if not entered.acquire(timeout=0.75):
            break
    release.set()
    for thread in threads:
        thread.join(5)
    return peak


def test_stock_defaults_bound_the_watchdog_lane(tmp_path, monkeypatch):
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / "lanes", eval_parallel=0, llm_parallel=0, llm_backed=True)
    snapshot = engine._llm_broker.snapshot()
    # AUTO is not a positive canonical value, so there is still no finite global ceiling...
    assert snapshot["total"] is None
    # ...but the broker is live, and the background lanes are bounded. `{}` used to mean DISABLED:
    # `llm_request_permit` never even borrowed, so the lane annotation bounded nothing.
    assert snapshot["enabled"] is True
    assert snapshot["lane_limits"]["enrichment"] == 1
    assert _max_concurrency(engine._llm_broker, "enrichment", 4) == 1


def test_the_foreground_lanes_stay_unbounded_without_a_total(tmp_path, monkeypatch):
    # Under a finite total the `1` on `engine` is a FAIRNESS share of a budget the operator asked
    # for. With no budget it would be an absolute serialization of the main task's own foreground
    # work — including the per-eval repair loop, which inherits this fallback lane and would then
    # serialize across an otherwise parallel eval batch.
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    engine = _engine(tmp_path / "fg", eval_parallel=0, llm_parallel=0, llm_backed=True)
    limits = engine._llm_broker.snapshot()["lane_limits"]
    assert limits["build"] is None and limits["engine"] is None
    assert _max_concurrency(engine._llm_broker, "build", 6) == 6
    assert _max_concurrency(engine._llm_broker, "engine", 6) == 6


def test_a_finite_total_keeps_its_historical_allocation():
    # The finite branch is unchanged, keys and order included: build may consume the whole total
    # while it is the only demand; every other lane is one concurrent request.
    assert default_llm_lane_limits(4) == {
        "build": 4, "deep_research": 1, "novelty_dedup": 1, "enrichment": 1, "engine": 1}
    assert list(default_llm_lane_limits(4)) == list(default_llm_lane_limits(None))


def test_both_live_log_watchdogs_are_still_in_the_capped_lane():
    """CLAUDE.md names both per-eval watchdogs as the enrichment-lane producers.

    The cap above is worth nothing if a watchdog stops declaring the lane, and that rename would be
    silent — `normalize_llm_lane` sends an unknown label to the unbounded `engine` fallback.
    """
    for module in ("train_monitor.py", "asha_monitor.py"):
        source = (_ENGINE_DIR / module).read_text(encoding="utf-8")
        assert '@in_llm_lane("enrichment")' in source, f"{module} left the capped lane"


# --------------------------------------------------------------------------- #
# H-7b — the refusal is a CLI PREFLIGHT, so it cannot mutate what it refuses
# --------------------------------------------------------------------------- #

def _pinned_run(tmp_path, name: str, width: int) -> Path:
    """A finished toy run whose `run_started` pins `eval_parallel=width`, via the real CLI."""
    out = tmp_path / name
    result = _runner.invoke(_app, [
        "run", "--no-genesis", "--kind", "quadratic", "--goal", "min (x-3)^2", "--direction", "min",
        "--backend", "toy", "--out", str(out),
        "-s", f"eval_parallel={width}", "-s", "max_nodes=2", "-s", "n_seeds=2"])
    assert result.exit_code == 0, result.output
    assert fold(EventStore(out / "events.jsonl").read_all()).eval_parallel == width
    return out


def _run_dir_bytes(out: Path) -> tuple[bytes, bytes]:
    return ((out / "events.jsonl").read_bytes(), (out / "config.snapshot.json").read_bytes())


def test_run_refuses_a_disagreeing_width_before_the_snapshot_and_reopen_writes(tmp_path):
    """`RunStartPinError`'s whole point — untouched on refusal — did not hold for the width pin.

    The receipt has a CLI preflight; the width check existed only inside `Engine.run`, which `run`
    reaches AFTER `_publish_run_snapshots` and the `run_reopened` append. Measured on a real run dir
    before the fix: `config.snapshot.json` 2 -> 3, events 38 -> 39, THEN the refusal — whose remedy
    told the operator to restore the value in the file it had just clobbered.
    """
    out = _pinned_run(tmp_path, "pinned", 2)
    before = _run_dir_bytes(out)
    result = _runner.invoke(_app, [
        "run", "--no-genesis", "--kind", "quadratic", "--goal", "min (x-3)^2", "--direction", "min",
        "--backend", "toy", "--out", str(out),
        "-s", "eval_parallel=3", "-s", "max_nodes=2", "-s", "n_seeds=2"])
    assert isinstance(result.exception, SettledWidthPinError)
    assert _run_dir_bytes(out) == before


def test_resume_refuses_a_disagreeing_width_before_lifting_the_run(tmp_path):
    """The lift is the append that made the byte-unchanged property vacuous.

    `resume` on a FINISHED (or paused) run appends `resume` first, so the refusal that followed had
    already changed the log. A second identical `resume` then looked byte-clean only because the run
    was no longer in a liftable state — i.e. exactly not the case an operator hits.
    """
    out = _pinned_run(tmp_path, "lift", 2)
    snapshot = out / "config.snapshot.json"
    snapshot.write_text(json.dumps({**json.loads(snapshot.read_text()), "eval_parallel": 3}))
    before = (out / "events.jsonl").read_bytes()

    for _ in range(2):                       # the FIRST attempt must already be byte-clean
        result = _runner.invoke(_app, ["resume", str(out)])
        assert isinstance(result.exception, SettledWidthPinError)
        assert (out / "events.jsonl").read_bytes() == before


def test_an_agreeing_width_still_reopens_and_does_work(tmp_path):
    """The control: the preflight refuses a CHANGED treatment, it does not block re-entry."""
    out = _pinned_run(tmp_path, "agree", 2)
    before = len(EventStore(out / "events.jsonl").read_all())
    result = _runner.invoke(_app, [
        "run", "--no-genesis", "--kind", "quadratic", "--goal", "min (x-3)^2", "--direction", "min",
        "--backend", "toy", "--out", str(out),
        "-s", "eval_parallel=2", "-s", "max_nodes=3", "-s", "n_seeds=2"])
    assert result.exit_code == 0, result.output
    assert len(EventStore(out / "events.jsonl").read_all()) > before


def test_the_width_preflight_runs_before_every_write_its_command_owns():
    """Source-order guard, because the property is ORDERING and a passing call proves nothing.

    Moving `_preflight_settled_widths` one line down — below `_publish_run_snapshots` or below the
    `EV_RESUME` append — restores the exact defect with every behavioural test above still green on
    the *other* command. Both call sites are checked against the write they must precede.
    """
    import ast

    tree = ast.parse(Path(_run_cmds.__file__).read_text(encoding="utf-8"))
    commands = {node.name: node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef)}

    def lines_of(command: str, predicate) -> list[int]:
        return sorted(node.lineno for node in ast.walk(commands[command])
                      if isinstance(node, ast.Call) and predicate(node))

    def named(name: str):
        return lambda node: isinstance(node.func, ast.Name) and node.func.id == name

    def appends(event: str):
        return lambda node: (isinstance(node.func, ast.Attribute) and node.func.attr == "append"
                             and any(isinstance(a, ast.Name) and a.id == event for a in node.args))

    for command, write_name, write_predicate in (
            ("run", "_publish_run_snapshots", named("_publish_run_snapshots")),
            ("resume", "the EV_RESUME append", appends("EV_RESUME"))):
        guards = lines_of(command, named("_preflight_settled_widths"))
        writes = lines_of(command, write_predicate)
        assert guards, f"`{command}` no longer calls _preflight_settled_widths at all"
        assert writes, f"`{command}` no longer contains {write_name} — re-point this guard"
        assert max(guards) < min(writes), (
            f"in `{command}`, _preflight_settled_widths no longer precedes {write_name} — the "
            "refusal mutates the log it refuses to trust")


def test_the_refusal_names_the_knob_the_command_actually_reads():
    """`run` never READS config.snapshot.json — it writes it from the launch settings.

    So the original single remedy ("put it back in this run's config.snapshot.json / launch
    settings") sent half of its readers to edit a file with no effect on the command they ran.
    """
    run_text = settled_width_refusal("eval_parallel", resolved=3, recorded=2, source="run")
    resume_text = settled_width_refusal("eval_parallel", resolved=3, recorded=2, source="resume")
    assert "-s eval_parallel=" in run_text and "LOOPLAB_EVAL_PARALLEL" in run_text
    assert "never reads it back" in run_text
    assert "config.snapshot.json, which `resume` restores" in resume_text
    # `null` is the natural JSON spelling of "no opinion" and is NOT AUTO — it is the legacy
    # fallback, so it resolves to `max_parallel` (1) and refuses again. Say so in the refusal.
    for text in (run_text, resume_text):
        assert "`null` is NOT AUTO" in text


def test_a_null_width_is_the_legacy_fallback_not_auto(tmp_path, monkeypatch):
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / "null", eval_parallel=None, llm_parallel=None, llm_backed=True)
    # `None` means "use max_parallel/parallel_build", whose defaults are 1 — an EXPLICIT 1, not AUTO.
    assert (engine._eval_parallel, engine._llm_parallel) == (1, 1)
    assert engine._eval_parallel_startup_auto is False
    with pytest.raises(SettledWidthPinError, match=r"`null` is NOT AUTO"):
        engine._repin_settled_widths(_entry(eval_parallel=2))


# --------------------------------------------------------------------------- #
# H-7c — AUTO derives the width from what CUDA will actually expose
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("fence,expected", [
    # A typo'd fence naming devices a two-GPU box does not have. CUDA parses an ordinal list left to
    # right and stops at the first index that does not exist, so this exposes TWO devices — verified
    # against `torch.cuda.device_count()` on the box that produced this test. Counting the tokens
    # made AUTO derive 8, and `run_started` then pinned that 8 permanently, so a transient env typo
    # became the durable treatment every later resume ADOPTS.
    ("0,1,2,3,4,5,6,7", ["0", "1"]),
    ("0,1", ["0", "1"]),
    ("1", ["1"]),
    ("7", []),                       # CUDA: 0 visible devices, not one device named `7`
    ("", []),
    ("-1", []),
    ("0,0,0", []),
    # Opaque selectors are left EXACTLY as spelled: nvidia-smi's numeric index cannot bound a
    # UUID/MIG token, and a MIG fence legitimately names more instances than there are GPUs.
    ("GPU-aaa,GPU-bbb,GPU-ccc", ["GPU-aaa", "GPU-bbb", "GPU-ccc"]),
])
def test_an_ordinal_fence_is_truncated_the_way_cuda_truncates_it(monkeypatch, fence, expected):
    monkeypatch.setattr(_resources, "detect_gpus",
                        lambda: [{"index": 0, "mem_free_mib": 1}, {"index": 1, "mem_free_mib": 1}])
    assert _resources.schedulable_cuda_tokens(
        _resources.cuda_visible_device_tokens(fence)) == expected


def test_an_unprobeable_box_keeps_the_fence_exactly_as_spelled(monkeypatch):
    """Fail-open, always. Truncating on a probe that cannot answer would INVENT a smaller box."""
    for broken in (lambda: (_ for _ in ()).throw(OSError("no nvidia-smi")),   # probe raises
                   lambda: [],                                                # no inventory
                   lambda: [{"index": 3, "mem_free_mib": 1}]):                # not the 0..n-1 space
        monkeypatch.setattr(_resources, "detect_gpus", broken)
        assert _resources.schedulable_cuda_tokens(["0", "1", "2", "9"]) == ["0", "1", "2", "9"]


def test_auto_width_follows_the_truncated_fence_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(_resources, "detect_gpus",
                        lambda: [{"index": 0, "mem_free_mib": 1}, {"index": 1, "mem_free_mib": 1}])
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0,1,2,3,4,5,6,7")
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / "typo", eval_parallel=0, llm_parallel=0, llm_backed=True)
    assert engine._gpu_ids == [0, 1]
    assert (engine._eval_parallel, engine._llm_parallel) == (2, 2)
    # The physical map must survive the truncation, or every pinned reservation fails closed.
    assert engine._gpu_physical_ids == {0: "0", 1: "1"}


def test_a_mid_run_gpu_count_change_cannot_move_the_pin(tmp_path, monkeypatch):
    """AUTO reads the box ONCE, at construction — never again while the log is being written.

    The control for the whole pin: if any later decision re-derived the width, the run would change
    execution treatment part-way through its own log with nothing recording it, which is the defect
    the `run_started` pin exists to make impossible. Nothing between construction and the terminal is
    allowed to notice that the box grew.
    """
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / "grow", eval_parallel=0, llm_parallel=0, llm_backed=True)
    assert (engine._eval_parallel, engine._llm_parallel) == (2, 2)

    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1, 2, 3])   # the box "grows" mid-run
    anyio.run(engine.run)
    assert (engine._eval_parallel, engine._llm_parallel) == (2, 2)
    data = _run_started(engine)
    assert (data["eval_parallel"], data["llm_parallel"]) == (2, 2)
