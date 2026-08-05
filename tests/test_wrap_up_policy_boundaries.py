"""Where the wrap-up softening applies — the two boundaries `7b11e7ad` drew in the wrong place.

That commit gave the endpoint preflight two policies: REFUSE a run that can still propose, WARN an
entry point that can only complete a wrap-up. Both defects below are about the boundary of that
warning rather than the warning itself, and neither was reachable by the tests it shipped with,
which are single-process and pin only `is_wrap_up(kind)` — a pure function of a string.

  1. IN TIME. `resume` chose refuse-vs-warn from a fold taken BEFORE the singleton lock, and decided
     to LIFT the run from a re-fold taken INSIDE it. Between them sits the handoff wait, whose whole
     purpose is to let the previous owner finish its wrap-up — i.e. to produce exactly the
     `pending_finalize` -> `finished` transition that invalidates the earlier answer. Two real
     processes (a `finalize` holding the singleton, a `resume` arriving ~1s later, endpoint at a
     closed port) turned a 3-node run into a 6-node one: three identical `x=0,y=0` fallback nodes, a
     metric flat at 10.0, `finished=True`, exit 0 — the measurement `preflight_role_endpoints`'s own
     docstring cites as why the refusal exists. A SINGLE-PROCESS test cannot express that, so the
     tests here either drive real subprocesses or inject the transition at the handoff boundary.

  2. IN SPACE. `core/llm.py::validate_bound_profiles` is the endpoint probe's neighbour at the same
     gate and got no wrap-up policy at all, so `looplab finalize` on a finished run whose
     API-key/endpoint pair had gone incomplete still died with `LLMError: LLM credential preflight
     failed`, exit 1, zero artifacts — the identical dead end, one function earlier. Its fix needs
     one thing the endpoint's did not, and there is a test below for exactly that: a dead endpoint
     still BUILDS its clients, an unusable credential does not, so warning alone would only move the
     same `LLMError` down into `make_roles`.

New FILE for the same reason `test_flipped_defaults_operator_surface.py` is one: these cut across
`agents` (the two gates), `cli` (which boundary each command is on) and the process-level lifecycle.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from looplab.core.config import Settings
from looplab.core.errors import LLMError
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold

_DEAD = "http://127.0.0.1:9/v1"          # the discard port: never listening, refused instantly
_TASK_DOC = ('{"id":"t","kind":"quadratic","goal":"g","direction":"min",'
             '"bounds":{"x":[-10.0,10.0],"y":[-10.0,10.0]},"seed":7,"step":1.5}')


class _DeadClient:
    """A transport whose endpoint is not there (the shape `_probe_role_endpoints` measures)."""

    def __init__(self, **_kwargs):
        pass

    def probe(self, _messages, **_kwargs):
        raise LLMError(f"LLM request to {_DEAD} failed: Connection error.")


@pytest.fixture()
def dead_endpoint(monkeypatch):
    from looplab.agents import factory

    monkeypatch.setattr(factory, "make_llm_client", lambda *a, **k: _DeadClient())


@pytest.fixture()
def live_endpoint(monkeypatch):
    from looplab.agents import factory

    class _Ok:
        def __init__(self, **_kwargs):
            pass

        def probe(self, _messages, **_kwargs):
            return None

    monkeypatch.setattr(factory, "make_llm_client", lambda *a, **k: _Ok())


def _stopped_run_with_a_pending_finalize(run_dir: Path) -> EventStore:
    """The exact boundary the race starts from: a STOPped run someone has just asked to finalize.

    `classify_prior_run` calls this `pending_finalize` — wrap-up only — which is what makes the
    endpoint probe warn instead of refusing. The run still has budget left, so lifting it later is
    not a no-op: it starts proposing.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "task.snapshot.json").write_text(_TASK_DOC, encoding="utf-8")
    (run_dir / "config.snapshot.json").write_text(
        json.dumps({"backend": "llm", "llm_base_url": _DEAD, "max_nodes": 6}), encoding="utf-8")
    store = EventStore(run_dir / "events.jsonl")
    store.append("run_started", {"run_id": run_dir.name, "task_id": "t", "goal": "g",
                                 "direction": "min"})
    store.append("pause", {})
    store.append("run_abort", {"reason": "finalized"})
    return store


def _previous_owner_completes_its_wrap_up(store: EventStore) -> None:
    """What the handoff wait is WAITING FOR: the owner holding the lock finishes the wrap-up.

    After this the log is a plain finished run with a complete terminal projection — `resume`'s own
    classifier calls it `finished`, the one boundary `resume` LIFTS.
    """
    finish = store.append("run_finished", {"reason": "finalized", "finalization_required": True})
    store.append("finalization_finished", {"finish_seq": finish.seq})


def _handoff_singleton(store: EventStore, attempts: list):
    """A singleton lock that is held by someone else exactly once, and that someone finishes.

    The in-process spelling of the two-process race: `resume` loses the lock, and while it waits the
    previous owner completes the wrap-up the wait exists for. Injecting the transition here rather
    than mocking a clock is the point — this IS the boundary, and a same-process test that never
    crosses it cannot fail the way the shipped code did.
    """
    @contextmanager
    def _singleton(_run_dir):
        attempts.append(True)
        if len(attempts) == 1:
            yield False
            _previous_owner_completes_its_wrap_up(store)
        else:
            yield True

    return _singleton


# ---------------------------------------------------------------------------------------------
# 1. the boundary in TIME: `resume` may not decide refuse-vs-warn on the far side of the wait
# ---------------------------------------------------------------------------------------------

def test_the_handoff_wait_produces_exactly_the_transition_that_invalidates_an_entry_fold(tmp_path):
    """The premise, pinned on the classifier itself so the tests below cannot pass vacuously."""
    from looplab.cli.run_cmds import classify_prior_run, is_wrap_up

    def _kind(events):
        return classify_prior_run(fold(events), events)

    store = _stopped_run_with_a_pending_finalize(tmp_path / "run")
    before = store.read_all()
    assert _kind(before) == "pending_finalize" and is_wrap_up(_kind(before))
    _previous_owner_completes_its_wrap_up(store)
    after = store.read_all()
    # The SAME command, the SAME log, one handoff later: no longer wrap-up-only.
    assert _kind(after) == "finished" and not is_wrap_up(_kind(after))


def test_a_resume_whose_boundary_becomes_liftable_during_the_handoff_refuses(
        tmp_path, monkeypatch, dead_endpoint):
    """The reproduction, with the transition injected at the boundary it really happens at.

    Shipped behaviour: the entry fold said `pending_finalize`, so the probe only warned; the lock
    came free with the run plainly `finished`, so the same command lifted it and proposed against a
    dead endpoint. The decision and the lift now read ONE fold, taken under the lock, so the probe
    that runs is the refusing one.
    """
    import looplab.cli.run_cmds as cmds

    run_dir = tmp_path / "handoff"
    store = _stopped_run_with_a_pending_finalize(run_dir)
    attempts: list = []
    monkeypatch.setattr(cmds, "_engine_singleton", _handoff_singleton(store, attempts))
    monkeypatch.setattr(cmds.time, "sleep", lambda _delay: None)

    with pytest.raises(LLMError) as raised:
        # A direct call leaves Typer's OptionInfo sentinels in place; name both explicitly.
        cmds.resume(run_dir, task_file=run_dir / "task.snapshot.json", max_nodes=None)

    assert len(attempts) == 2, "the wait never happened — the race was not exercised"
    # The refusal, with its own reason: this run CAN still propose, so the wrap-up softening is off.
    assert "LLM endpoint preflight failed" in str(raised.value)
    assert "empty fallback proposals" in str(raised.value)
    # and nothing was lifted on the way to it
    events = store.read_all()
    assert not any(event.type == "resume" for event in events)
    assert fold(events).finished


def test_the_same_handoff_still_lifts_when_the_endpoint_answers(
        tmp_path, monkeypatch, live_endpoint):
    """The fix must not turn every handoff into a refusal: with a model to propose WITH, the lift
    this command exists for still happens."""
    import looplab.cli.run_cmds as cmds

    run_dir = tmp_path / "handoff-live"
    store = _stopped_run_with_a_pending_finalize(run_dir)
    attempts: list = []
    monkeypatch.setattr(cmds, "_engine_singleton", _handoff_singleton(store, attempts))
    monkeypatch.setattr(cmds.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(cmds, "_run_engine_guarded",
                        lambda eng: fold(eng.store.read_all()))
    monkeypatch.setattr(cmds, "_print_result", lambda _state: None)

    cmds.resume(run_dir, task_file=run_dir / "task.snapshot.json", max_nodes=None)

    assert len(attempts) == 2
    events = store.read_all()
    assert sum(event.type == "resume" for event in events) == 1
    assert not fold(events).finished


def test_a_legitimate_wrap_up_resume_still_warns_and_completes(tmp_path, dead_endpoint):
    """The behaviour `7b11e7ad` added, unchanged: a run stranded at a `finalization_pending`
    boundary is wrapped up without a model rather than refused, and says what that cost."""
    from typer.testing import CliRunner
    from looplab.cli import app

    run_dir = tmp_path / "pending"
    run_dir.mkdir()
    (run_dir / "task.snapshot.json").write_text(_TASK_DOC, encoding="utf-8")
    (run_dir / "config.snapshot.json").write_text(
        json.dumps({"backend": "llm", "llm_base_url": _DEAD}), encoding="utf-8")
    store = EventStore(run_dir / "events.jsonl")
    store.append("run_started", {"run_id": "pending", "task_id": "t", "goal": "g",
                                 "direction": "min"})
    store.append("run_finished", {"reason": "done", "finalization_required": True})

    result = CliRunner().invoke(app, ["resume", str(run_dir)])

    assert result.exit_code == 0, result.output
    assert "unreachable while wrapping up" in result.output
    assert "endpoint preflight failed" not in result.output
    state = fold(store.read_all())
    assert state.finished and not state.finalization_pending()
    assert not any(event.type == "resume" for event in store.read_all())


def test_a_wrap_up_engine_whose_boundary_moved_never_reaches_the_loop(tmp_path, monkeypatch):
    """The backstop, at the one point all three commands enter the loop.

    `resume` is fixed at the source, but `finalize` and `run` also build their engine from a fold and
    a fold is a snapshot. A `wrap_up_only` engine is a promise — "this entry point can only complete
    the terminal already on disk" — and that promise is what downgraded the refusal to a warning, so
    it is re-checked against the log rather than trusted for the length of a wrap-up.
    """
    import looplab.cli.run_cmds as cmds

    run_dir = tmp_path / "moved"
    run_dir.mkdir()
    store = EventStore(run_dir / "events.jsonl")
    store.append("run_started", {"run_id": "moved", "task_id": "t", "goal": "g",
                                 "direction": "min"})
    store.append("run_abort", {"reason": "finalized"})

    class _Eng:
        def __init__(self):
            self.store = EventStore(run_dir / "events.jsonl")
            self.wrap_up_only = True
            self.wrap_up_degradation_warning = "⚠ LLM endpoint unreachable while wrapping up: …"
            self.ran = False

        async def run(self):
            self.ran = True
            return fold(self.store.read_all())

    eng = _Eng()
    # `pending_finalize`: the promise holds, so the loop runs exactly as before.
    assert cmds._run_engine_guarded(eng) is not None and eng.ran

    # …and now something lifts the run out from under it, which is the case that must not search.
    store.append("resume", {})
    eng.ran = False
    with pytest.raises(LLMError) as raised:
        cmds._run_engine_guarded(eng)
    assert not eng.ran
    assert "WRAP-UP" in str(raised.value) and "never start new work" in str(raised.value)
    # the operator is told what to type instead, and why the model mattered here
    assert "looplab resume" in str(raised.value) and "looplab finalize" in str(raised.value)
    assert "unreachable while wrapping up" in str(raised.value)


def test_an_engine_built_outside_the_cli_carries_no_promise_to_check():
    """Compatibility stubs (a lightweight object in a command-level test) have no `wrap_up_only`,
    so the backstop must not fold their log or invent a promise they never made."""
    import looplab.cli.run_cmds as cmds

    class _Stub:
        store = None

    cmds._refuse_wrap_up_engine_that_could_propose(_Stub())    # no attribute access on `store`


# ---------------------------------------------------------------------------------------------
# the same race, end to end, in real processes — the shape a single-process test cannot reach
# ---------------------------------------------------------------------------------------------

# The previous OWNER, as a real process holding the real OS lock on `engine.lock`, doing what a
# `finalize` does in the order it does it: hold the singleton, complete the wrap-up, release. It
# plays that part rather than shelling out to `looplab finalize` for one reason — determinism. A
# literal `finalize` reproduces this every time by hand (it is how the defect was found) but its
# wrap-up on a small run can outrun a fresh interpreter's startup, and a race guard that sometimes
# has the `resume` child win the lock FIRST passes without ever crossing the boundary it guards.
# Holding for a fixed window and asserting the child is still alive makes "it waited" a fact.
_RACE = textwrap.dedent("""
    import subprocess, sys, time, json
    from pathlib import Path
    from looplab.cli import _engine_singleton
    from looplab.events.eventstore import EventStore

    run_dir = Path(sys.argv[1])
    store = EventStore(run_dir / "events.jsonl")
    with _engine_singleton(run_dir) as ok:
        assert ok, "the fixture's run dir was already locked"
        resume = subprocess.Popen([sys.executable, "-m", "looplab.cli", "resume", str(run_dir)],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                  cwd=str(run_dir.parent))
        time.sleep(4.0)                     # long enough for a fresh interpreter to reach the wait
        waited = resume.poll() is None
        # …and now the previous owner finishes the wrap-up the wait exists for.
        finish = store.append("run_finished", {"reason": "finalized",
                                               "finalization_required": True})
        store.append("finalization_finished", {"finish_seq": finish.seq})
    r_out, _ = resume.communicate(timeout=300)
    print(json.dumps({"waited": waited, "resume_code": resume.returncode, "resume_out": r_out}))
""")


def test_a_real_resume_process_that_waits_out_a_wrap_up_never_lifts_the_run(tmp_path):
    """The original reproduction, in real processes: a `resume` that waits for the singleton while
    the owner holding it completes the wrap-up, against a closed port.

    Before: `resume` printed "This run is over, so no proposal can degrade", then "run was finished —
    resuming", then ran the remaining budget as identical `x=0,y=0` fallback nodes and exited 0.
    """
    run_dir = tmp_path / "race"
    _stopped_run_with_a_pending_finalize(run_dir)
    # A spawned process reads the developer's real `.env` (tests/conftest.py neutralizes it only
    # in-process), and the run's endpoint is a closed port — so state the credential tier the run
    # actually has instead of inheriting one bound somewhere else. An empty pair is "no credential
    # configured", which is what pointing a run at a local model looks like.
    # PYTHONPATH pins the children to THIS tree rather than whatever `pip install -e` last pointed
    # at — a subprocess test that silently exercises a different checkout proves nothing.
    repo_root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "LOOPLAB_LLM_API_KEY": "", "LOOPLAB_LLM_API_KEY_BASE_URL": "",
           "PYTHONPATH": os.pathsep.join([str(repo_root), os.environ.get("PYTHONPATH", "")])}
    proc = subprocess.run([sys.executable, "-c", _RACE, str(run_dir)],
                          capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=600)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    outcome = json.loads(proc.stdout.strip().splitlines()[-1])

    assert outcome["waited"], "the resume process never entered the handoff wait"
    resume_out = outcome["resume_out"]
    # The lift itself, by the line it prints. This is what exit 0 used to be built on.
    assert "run was finished — resuming" not in resume_out, resume_out
    # It re-decided on the boundary it actually got, so the probe it ran is the refusing one.
    assert outcome["resume_code"] != 0, resume_out
    assert "empty fallback proposals" in resume_out, resume_out

    state = fold(EventStore(run_dir / "events.jsonl").read_all())
    assert state.finished and not state.finalization_pending()
    # The measurement itself: no node was created against the dead endpoint.
    assert not state.nodes, {n.id: n.idea.params for n in state.nodes.values()}


# ---------------------------------------------------------------------------------------------
# 2. the boundary in SPACE: the credential gate is the endpoint probe's neighbour, not its opposite
# ---------------------------------------------------------------------------------------------

@pytest.fixture()
def incomplete_credential_pair(monkeypatch):
    """The operator-visible state defect 2 is about: a key whose endpoint binding is gone.

    `bound_api_key_for` calls this "an incomplete key+endpoint pair" and refuses before transport —
    correctly, for a run that is about to spend money on it.
    """
    monkeypatch.setenv("LOOPLAB_LLM_API_KEY", "sk-orphaned-key")
    monkeypatch.delenv("LOOPLAB_LLM_API_KEY_BASE_URL", raising=False)


def test_the_credential_gate_warns_where_it_refuses_for_a_run_that_can_still_propose(
        incomplete_credential_pair):
    """One gate, two policies — the split the endpoint probe already had."""
    from looplab.agents import preflight
    from looplab.core.llm import validate_bound_profiles

    settings = Settings(backend="llm", llm_model="m", llm_base_url=_DEAD)
    with pytest.raises(LLMError) as refused:
        validate_bound_profiles(settings)
    assert "incomplete key+endpoint pair" in str(refused.value)

    warning = preflight.wrap_up_credential_warning(settings)
    assert warning is not None
    assert "incomplete key+endpoint pair" in warning
    # the refusal's header is replaced, not repeated: this is not a failed preflight any more
    assert "LLM credential preflight failed" not in warning
    # the same tail the endpoint policy prints, so the operator reads one story either way
    assert "no proposal can degrade" in warning
    assert "(report unavailable)" in warning
    for still_lands in ("budget summary", "cost roll-up", "tree.html", "trace.json"):
        assert still_lands in warning
    assert "marked COMPLETE" in warning and "will NOT" in warning
    # and the one thing this gate has to say that the endpoint one does not
    assert "NO credential at all" in warning


def test_a_usable_credential_produces_no_wrap_up_warning():
    from looplab.agents import preflight

    assert preflight.wrap_up_credential_warning(
        Settings(backend="llm", llm_model="m", llm_base_url=_DEAD)) is None
    assert preflight.wrap_up_credential_warning(Settings(backend="toy")) is None


def test_warning_alone_would_only_have_moved_the_same_error_into_make_roles(
        incomplete_credential_pair):
    """WHY the credential policy needs a second half that the endpoint policy does not.

    A dead endpoint still BUILDS its clients and fails at request time — which is exactly the silent
    degradation the refusal exists for, and exactly why warning is enough there. An unusable
    credential fails inside `make_llm_client`, so a gate that only warned would hand the identical
    `LLMError` to `make_roles` a few lines later and the wrap-up would still exit 1 with zero
    artifacts. `credential_free_wrap_up_settings` is what makes the warning true.
    """
    from looplab.adapters.tasks import validate_task
    from looplab.agents.factory import make_roles
    from looplab.agents.preflight import credential_free_wrap_up_settings
    from looplab.core.llm import validate_bound_profiles

    settings = Settings(backend="llm", llm_model="m", llm_base_url=_DEAD)
    task = validate_task(json.loads(_TASK_DOC))
    with pytest.raises(LLMError) as leaked:
        make_roles(task, settings, None)
    assert "incomplete key+endpoint pair" in str(leaked.value)

    degraded = credential_free_wrap_up_settings(settings)
    validate_bound_profiles(degraded)                 # the gate itself is now satisfied…
    researcher, developer = make_roles(task, degraded, None)   # …and so is every client it builds
    assert researcher is not None and developer is not None
    # Degraded, not bypassed: the credential is DROPPED, so nothing carries a key bound elsewhere.
    assert not degraded.llm_api_key and degraded.llm_api_key_base_url is None
    # …and only on the copy. The caller still publishes the operator's real configuration.
    assert settings.llm_api_key and settings.backend == "llm"


def test_a_profile_credential_that_is_unset_degrades_the_same_way(monkeypatch):
    """The other spelling of an unusable credential: a role bound to a profile whose api_key_env is
    not set. Same gate, same policy — and the same construction failure without the second half."""
    from looplab.agents.preflight import (credential_free_wrap_up_settings,
                                          wrap_up_credential_warning)
    from looplab.core.llm import make_llm_client_for, validate_bound_profiles

    monkeypatch.delenv("VENDOR_KEY", raising=False)
    settings = Settings(backend="llm", llm_model="m", llm_base_url=_DEAD,
                        llm_profile="vendor",
                        llm_profiles={"vendor": {"model": "m", "base_url": _DEAD,
                                                 "api_key_env": "VENDOR_KEY"}})
    with pytest.raises(LLMError):
        validate_bound_profiles(settings)
    assert "VENDOR_KEY" in (wrap_up_credential_warning(settings) or "")

    degraded = credential_free_wrap_up_settings(settings)
    validate_bound_profiles(degraded)
    make_llm_client_for(degraded, role="researcher")
    # the profile keeps everything that says WHERE the run pointed; only the credential is dropped
    assert degraded.llm_profiles["vendor"]["base_url"] == _DEAD
    assert "api_key_env" not in degraded.llm_profiles["vendor"]


def test_finalize_produces_its_artifacts_with_an_incomplete_credential_pair(
        tmp_path, incomplete_credential_pair):
    """The reproduction: `looplab finalize` on a finished run whose credential pair had gone
    incomplete raised `LLMError: LLM credential preflight failed`, exit 1, not one artifact — and
    left the run `finalization_pending` forever, with its spend stranded in `.llm-usage-outbox`."""
    from typer.testing import CliRunner
    from looplab.cli import app

    run_dir = tmp_path / "credential-pending"
    run_dir.mkdir()
    (run_dir / "task.snapshot.json").write_text(_TASK_DOC, encoding="utf-8")
    (run_dir / "config.snapshot.json").write_text(
        json.dumps({"backend": "llm", "llm_base_url": _DEAD}), encoding="utf-8")
    store = EventStore(run_dir / "events.jsonl")
    store.append("run_started", {"run_id": "credential-pending", "task_id": "t", "goal": "g",
                                 "direction": "min"})
    store.append("run_finished", {"reason": "done", "finalization_required": True})

    result = CliRunner().invoke(app, ["finalize", str(run_dir)])

    assert result.exit_code == 0, result.output
    assert "LLM credential preflight failed" not in result.output
    assert "credential unusable while wrapping up" in result.output
    # the closing line is not the silent half of a degradation
    assert "WITHOUT the model" in result.output
    state = fold(store.read_all())
    assert state.finished and not state.finalization_pending()
    # the artifacts the refusal used to cost, every one of them model-free
    for artifact in ("trace.json", "tree.html", "readmodel.sqlite"):
        assert (run_dir / artifact).exists(), f"{artifact} missing: {result.output}"


def test_the_endpoint_probe_is_not_run_a_second_time_after_a_credential_warning(
        tmp_path, monkeypatch, incomplete_credential_pair):
    """Both gates warn on a wrap-up, but the operator reads ONE degradation.

    A probe issued after the credential was dropped would measure a configuration this run never had
    (credential-free), and could only repeat the sentence already printed.
    """
    from looplab import cli
    from looplab.adapters.tasks import validate_task
    from looplab.agents import preflight

    monkeypatch.setattr(preflight, "wrap_up_endpoint_warning",
                        lambda *_a, **_k: pytest.fail("probed after the credential gate warned"))
    run_dir = tmp_path / "one-warning"
    run_dir.mkdir()
    engine = cli._engine(run_dir, validate_task(json.loads(_TASK_DOC)),
                         Settings(backend="llm", llm_model="m", llm_base_url=_DEAD, max_nodes=1),
                         crash_after=None, wrap_up_only=True)
    assert "credential unusable while wrapping up" in engine.wrap_up_degradation_warning
    assert engine.wrap_up_degradation_warning.count("no proposal can degrade") == 1
    assert engine.wrap_up_only is True


def test_a_run_that_can_still_propose_still_refuses_on_a_broken_credential(
        tmp_path, incomplete_credential_pair):
    """The softening is scoped to the wrap-up on BOTH gates. A `resume` that would lift a stopped
    run can still spend money on a model, so the credential refusal stands — and it stands FIRST,
    before the endpoint is ever probed."""
    from typer.testing import CliRunner
    from looplab.cli import app

    run_dir = tmp_path / "liftable"
    run_dir.mkdir()
    (run_dir / "task.snapshot.json").write_text(_TASK_DOC, encoding="utf-8")
    (run_dir / "config.snapshot.json").write_text(
        json.dumps({"backend": "llm", "llm_base_url": _DEAD}), encoding="utf-8")
    store = EventStore(run_dir / "events.jsonl")
    store.append("run_started", {"run_id": "liftable", "task_id": "t", "goal": "g",
                                 "direction": "min"})
    store.append("pause", {})

    result = CliRunner().invoke(app, ["resume", str(run_dir)])

    assert result.exit_code == 1
    assert "LLM credential preflight failed" in str(result.exception)
    assert not any(event.type == "resume" for event in store.read_all())
