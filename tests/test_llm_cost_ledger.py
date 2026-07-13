"""Durable LLM-cost ledger: restart, role-graph, finalize, and recovery contracts."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import threading
from types import SimpleNamespace

import orjson
import pytest

from looplab.core.llm import CostAccountant
from looplab.core.models import Event
from looplab.engine.costs import reconcile_cost_accountants, reconcile_usage_outbox
from looplab.engine.finalize import emit_llm_cost, finalize_run
from looplab.engine.orchestrator import Engine
from looplab.events.replay import fold
from looplab.events.types import EV_LLM_COST, EV_LLM_USAGE
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree


_ROOT = Path(__file__).resolve().parents[1]


def _client(accountant: CostAccountant):
    return SimpleNamespace(accountant=accountant)


def _role(accountant: CostAccountant, **attrs):
    return SimpleNamespace(client=_client(accountant), **attrs)


def _engine(run_dir, *, researcher=None, developer=None, **kwargs) -> Engine:
    from looplab.adapters.toytask import ToyTask

    task = ToyTask.load(_ROOT / "examples" / "toy_task.json")
    researcher = researcher or _role(CostAccountant())
    developer = developer or _role(CostAccountant())
    return Engine(
        run_dir, task=task, researcher=researcher, developer=developer,
        sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=3, max_nodes=4),
        max_parallel=1, **kwargs,
    )


def _usage(prompt: int, completion: int) -> dict:
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


def test_fold_uses_latest_legacy_summary_as_base_then_only_durable_deltas():
    events = [
        Event(seq=0, type=EV_LLM_COST,
              data={"cost": 1.0, "calls": 1, "prompt_tokens": 10,
                    "completion_tokens": 1, "total_tokens": 11}),
        Event(seq=1, type=EV_LLM_COST,
              data={"cost": 2.0, "calls": 2, "prompt_tokens": 20,
                    "completion_tokens": 2, "total_tokens": 22}),
        Event(seq=2, type=EV_LLM_USAGE,
              data={"cost": .25, "calls": 1, "prompt_tokens": 3,
                    "completion_tokens": 4, "total_tokens": 7}),
        # A derived summary after ledger activation cannot replace source-of-truth deltas.
        Event(seq=3, type=EV_LLM_COST,
              data={"cost": 999.0, "calls": 999, "prompt_tokens": 999,
                    "completion_tokens": 999, "total_tokens": 999}),
        # Corrupt values are bounded to zero and cannot poison replay.
        Event(seq=4, type=EV_LLM_USAGE,
              data={"cost": float("nan"), "calls": True, "prompt_tokens": -5,
                    "completion_tokens": "8", "total_tokens": 10**100}),
    ]

    total = fold(events).llm_cost
    assert total == {"cost": 2.25, "calls": 3, "prompt_tokens": 23,
                     "completion_tokens": 6, "total_tokens": 29}


def test_fold_treats_usage_id_as_first_write_wins():
    events = [
        Event(seq=0, type=EV_LLM_USAGE,
              data={"usage_id": "same-paid-call", "cost": .25, "calls": 1,
                    "prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}),
        # An ambiguous append retry may put the same logical delta in the log twice. The original
        # event is authoritative even if a later duplicate carries conflicting telemetry.
        Event(seq=1, type=EV_LLM_USAGE,
              data={"usage_id": "same-paid-call", "cost": 99.0, "calls": 99,
                    "prompt_tokens": 99, "completion_tokens": 99, "total_tokens": 198}),
        Event(seq=2, type=EV_LLM_USAGE,
              data={"usage_id": "another-paid-call", "cost": .50, "calls": 1,
                    "prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}),
    ]

    assert fold(events).llm_cost == {
        "cost": .75, "calls": 2, "prompt_tokens": 7,
        "completion_tokens": 3, "total_tokens": 10,
    }


def test_new_engine_and_client_continue_durable_total_after_resume(tmp_path):
    run_dir = tmp_path / "resume"
    first = CostAccountant()
    eng1 = _engine(run_dir, researcher=_role(first))
    first.add(.10, _usage(10, 2))
    assert [event.type for event in eng1.store.read_all()].count(EV_LLM_USAGE) == 1

    # A fresh process has fresh in-memory counters but sees phase one's durable event log.
    second = CostAccountant()
    eng2 = _engine(run_dir, researcher=_role(second))
    second.add(.20, _usage(20, 3))
    emit_llm_cost(eng2, finalize_scope="finish:resume")

    total = fold(eng2.store.read_all()).llm_cost
    assert total["cost"] == pytest.approx(.30)
    assert total["calls"] == 2
    assert total["prompt_tokens"] == 30
    assert total["completion_tokens"] == 5
    assert total["total_tokens"] == 35
    summaries = [event.data for event in eng2.store.read_all() if event.type == EV_LLM_COST]
    assert summaries[-1]["cost"] == pytest.approx(.30)
    assert summaries[-1]["calls"] == 2


def test_auxiliary_roles_wrappers_and_shared_accountants_are_bound_once(tmp_path):
    shared = CostAccountant()
    pilot = CostAccountant()
    deep = CostAccountant()
    report = CostAccountant()
    onboard = CostAccountant()

    # The same accounts are deliberately reachable through several roots/wrapper seams.
    researcher = _role(
        shared,
        stage_clients=[_client(shared), _client(pilot)],
        _pilot_client=_client(pilot),
        loop_opts={"summary_client": _client(pilot)},
    )
    developer = _role(shared)
    eng = _engine(
        tmp_path / "roles", researcher=researcher, developer=developer,
        strategist=_role(shared), deep_researcher=_role(deep),
        report_writer=_role(report), onboarder=_role(onboard),
    )

    for accountant, cost in ((shared, .1), (pilot, .2), (deep, .3),
                             (report, .4), (onboard, .5)):
        accountant.add(cost, _usage(1, 1))

    usage_events = [event for event in eng.store.read_all() if event.type == EV_LLM_USAGE]
    assert len(usage_events) == 5
    total = fold(eng.store.read_all()).llm_cost
    assert total["cost"] == pytest.approx(1.5)
    assert total["calls"] == 5
    assert total["total_tokens"] == 10


def test_finalize_summary_is_after_reflection_and_includes_its_call(tmp_path):
    accountant = CostAccountant()
    eng = _engine(tmp_path / "reflection", researcher=_role(accountant))
    eng.store.append("run_started", {
        "run_id": "reflection", "task_id": eng.task.id, "goal": eng.task.goal,
        "direction": eng.task.direction,
    })
    eng.store.append("run_finished", {"reason": "done"})
    eng._store_case = lambda _state: None

    def reflect(_state):
        accountant.add(.75, _usage(30, 5))

    eng._write_reflection_note = reflect
    final = finalize_run(eng, entry_finished=False, start_time=0.0)

    assert final.llm_cost["cost"] == pytest.approx(.75)
    assert final.llm_cost["calls"] == 1
    events = eng.store.read_all()
    usage_seq = next(event.seq for event in events if event.type == EV_LLM_USAGE)
    summary_seq = next(event.seq for event in events if event.type == EV_LLM_COST)
    reflection_step = next(event.seq for event in events
                           if event.type == "finalize_step"
                           and event.data.get("step") == "reflection")
    assert usage_seq < reflection_step < summary_seq


def test_dynamic_developer_swap_binds_replacement_before_first_call(tmp_path):
    replacement = CostAccountant()
    eng = _engine(
        tmp_path / "swap",
        developer_factory=lambda _name: _role(replacement),
    )

    eng._apply_strategy({"developer": "llm", "_pinned": ["developer"]})
    replacement.add(.4, _usage(4, 1))

    usage = [event.data for event in eng.store.read_all() if event.type == EV_LLM_USAGE]
    assert len(usage) == 1
    assert usage[0].pop("usage_id")
    assert usage == [{"cost": .4, "calls": 1, "prompt_tokens": 4,
                      "completion_tokens": 1, "total_tokens": 5}]


def test_failed_sink_is_caught_up_without_repeating_the_paid_call(tmp_path):
    accountant = CostAccountant()
    eng = _engine(tmp_path / "sink-failure", researcher=_role(accountant))
    real_append = eng.store.append
    fail_usage = True

    def flaky_append(event_type, data, *args, **kwargs):
        if event_type == EV_LLM_USAGE and fail_usage:
            raise OSError("temporary ledger outage")
        return real_append(event_type, data, *args, **kwargs)

    eng.store.append = flaky_append
    # This represents exactly one already-successful provider response. Sink failure is swallowed
    # by CostAccountant and cannot ask the provider for a second response.
    accountant.add(.6, _usage(6, 2))
    assert accountant.calls == 1
    assert "temporary ledger outage" in (accountant.last_sink_error or "")
    assert not eng.store.read_all()

    fail_usage = False
    emit_llm_cost(eng, finalize_scope="finish:catchup")
    usage = [event.data for event in eng.store.read_all() if event.type == EV_LLM_USAGE]
    assert len(usage) == 1
    assert usage[0].pop("usage_id")
    assert usage == [{"cost": .6, "calls": 1, "prompt_tokens": 6,
                      "completion_tokens": 2, "total_tokens": 8}]
    total = fold(eng.store.read_all()).llm_cost
    assert total["cost"] == pytest.approx(.6)
    assert total["calls"] == 1


def test_pending_usage_outbox_survives_engine_restart_and_flushes_exactly_once(tmp_path):
    run_dir = tmp_path / "durable-pending"
    paid_call = CostAccountant()
    first = _engine(run_dir, researcher=_role(paid_call))
    first.store.append("run_started", {
        "run_id": "durable-pending", "task_id": first.task.id, "goal": first.task.goal,
        "direction": first.task.direction,
    })
    first.store.append("run_finished", {"reason": "done"})
    first._store_case = lambda _state: None
    first._write_reflection_note = lambda _state: None
    real_first_append = first.store.append
    usage_attempt_ids = []

    def unavailable_ledger(event_type, data, *args, **kwargs):
        if event_type == EV_LLM_USAGE:
            usage_attempt_ids.append(data.get("usage_id"))
            raise OSError("events ledger unavailable through finalizer exit")
        return real_first_append(event_type, data, *args, **kwargs)

    first.store.append = unavailable_ledger
    # One successful provider response is already paid. Both the immediate sink and the normal
    # finalizer retry fail, but the exact same usage ID/delta remains atomic in the run outbox.
    paid_call.add(.61, _usage(6, 2))
    assert paid_call.calls == 1
    finalize_run(first, entry_finished=False, start_time=0.0)
    first_events = first.store.read_all()
    assert not [event for event in first_events if event.type == EV_LLM_USAGE]
    assert not [event for event in first_events if event.type == EV_LLM_COST]
    assert not [event for event in first_events
                if event.type == "finalize_step" and event.data.get("step") == "llm_cost"]
    # One immediate attempt plus one outbox-drain retry. The same in-memory binding must not append
    # the same ID again within this reconcile pass.
    assert len(usage_attempt_ids) == 2 and len(set(usage_attempt_ids)) == 1
    pending = list((run_dir / ".llm-usage-outbox").glob("*.json"))
    assert len(pending) == 1

    # A fresh Engine stands in for a new process: it has no old binding/pending dictionary and no
    # provider call to replay. Reconciliation must discover the run-local outbox before llm_cost.
    fresh_accountant = CostAccountant()
    resumed = _engine(run_dir, researcher=_role(fresh_accountant))
    resumed._store_case = lambda _state: None
    resumed._write_reflection_note = lambda _state: None
    finalize_run(resumed, entry_finished=False, start_time=0.0)

    events = resumed.store.read_all()
    usage = [event.data for event in events if event.type == EV_LLM_USAGE]
    summaries = [event.data for event in events if event.type == EV_LLM_COST]
    assert len(usage) == len(summaries) == 1
    assert usage[0]["usage_id"] == pending[0].stem
    assert {key: usage[0][key] for key in (
        "cost", "calls", "prompt_tokens", "completion_tokens", "total_tokens",
    )} == {
        "cost": .61, "calls": 1, "prompt_tokens": 6,
        "completion_tokens": 2, "total_tokens": 8,
    }
    assert summaries[0]["cost"] == pytest.approx(.61)
    assert summaries[0]["calls"] == 1
    assert paid_call.calls == 1 and fresh_accountant.calls == 0
    assert not list((run_dir / ".llm-usage-outbox").glob("*.json"))

    # Re-entry sees the first-write-wins event and cannot append a duplicate logical charge.
    finalize_run(resumed, entry_finished=False, start_time=0.0)
    usage_after = [event for event in resumed.store.read_all() if event.type == EV_LLM_USAGE]
    assert len(usage_after) == 1
    assert fold(resumed.store.read_all()).llm_cost["calls"] == 1


def test_successful_usage_append_leaves_no_pending_outbox_record(tmp_path):
    accountant = CostAccountant()
    eng = _engine(tmp_path / "outbox-success", researcher=_role(accountant))

    accountant.add(.12, _usage(2, 1))

    assert len([event for event in eng.store.read_all() if event.type == EV_LLM_USAGE]) == 1
    assert not list((eng.run_dir / ".llm-usage-outbox").glob("*.json"))


def test_store_only_outbox_gate_flushes_usage_before_destructive_boundary(tmp_path):
    run_dir = tmp_path / "outbox-gate"
    accountant = CostAccountant()
    eng = _engine(run_dir, researcher=_role(accountant))
    real_append = eng.store.append

    def reject_usage(event_type, data, *args, **kwargs):
        if event_type == EV_LLM_USAGE:
            raise OSError("event append unavailable")
        return real_append(event_type, data, *args, **kwargs)

    eng.store.append = reject_usage
    accountant.add(.15, _usage(5, 1))
    assert len(list((run_dir / ".llm-usage-outbox").glob("*.json"))) == 1

    eng.store.append = real_append
    assert reconcile_usage_outbox(eng.store) is True
    usage = [event.data for event in eng.store.read_all() if event.type == EV_LLM_USAGE]
    assert len(usage) == 1 and usage[0]["cost"] == pytest.approx(.15)
    assert not list((run_dir / ".llm-usage-outbox").glob("*.json"))


def test_exact_stale_outbox_after_committed_event_is_safely_acknowledged(
        tmp_path, monkeypatch):
    run_dir = tmp_path / "stale-outbox"
    accountant = CostAccountant()
    eng = _engine(run_dir, researcher=_role(accountant))
    real_unlink = Path.unlink
    refused = False

    def fail_first_outbox_unlink(path, *args, **kwargs):
        nonlocal refused
        if path.parent.name == ".llm-usage-outbox" and not refused:
            refused = True
            raise OSError("ack interrupted")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_first_outbox_unlink)
    accountant.add(.13, _usage(3, 1))
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert refused
    assert len(list((run_dir / ".llm-usage-outbox").glob("*.json"))) == 1
    assert len([event for event in eng.store.read_all() if event.type == EV_LLM_USAGE]) == 1

    resumed = _engine(run_dir)
    assert reconcile_cost_accountants(resumed) is True
    assert not list((run_dir / ".llm-usage-outbox").glob("*.json"))
    assert len([event for event in resumed.store.read_all() if event.type == EV_LLM_USAGE]) == 1


def test_conflicting_stale_outbox_is_retained_and_blocks_summary(tmp_path, monkeypatch):
    run_dir = tmp_path / "conflicting-outbox"
    accountant = CostAccountant()
    eng = _engine(run_dir, researcher=_role(accountant))
    real_unlink = Path.unlink

    def refuse_outbox_unlink(path, *args, **kwargs):
        if path.parent.name == ".llm-usage-outbox":
            raise OSError("retain committed acknowledgement")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_outbox_unlink)
    accountant.add(.14, _usage(4, 1))
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert len([event for event in eng.store.read_all() if event.type == EV_LLM_USAGE]) == 1
    pending = list((run_dir / ".llm-usage-outbox").glob("*.json"))
    assert len(pending) == 1
    record = orjson.loads(pending[0].read_bytes())
    record["delta"]["cost"] = .99
    pending[0].write_bytes(orjson.dumps(record))

    resumed = _engine(run_dir)
    assert emit_llm_cost(resumed, finalize_scope="finish:conflict") is False
    assert pending[0].exists()
    assert len([event for event in resumed.store.read_all() if event.type == EV_LLM_USAGE]) == 1
    assert not [event for event in resumed.store.read_all() if event.type == EV_LLM_COST]


def test_json_directory_in_outbox_is_malformed_evidence_and_blocks_summary(tmp_path):
    run_dir = tmp_path / "directory-evidence"
    eng = _engine(run_dir)
    malformed = run_dir / ".llm-usage-outbox" / ("a" * 32 + ".json")
    malformed.mkdir(parents=True)

    assert reconcile_usage_outbox(eng.store) is False
    assert emit_llm_cost(eng, finalize_scope="finish:directory-evidence") is False
    assert malformed.is_dir()
    assert not [event for event in eng.store.read_all() if event.type == EV_LLM_COST]


def test_broken_same_id_symlink_is_never_replaced_or_erased(tmp_path, monkeypatch):
    run_dir = tmp_path / "broken-symlink-evidence"
    usage_id = "b" * 32
    outbox = run_dir / ".llm-usage-outbox"
    outbox.mkdir(parents=True)
    evidence = outbox / f"{usage_id}.json"
    real_is_symlink = Path.is_symlink
    real_lexists = os.path.lexists
    monkeypatch.setattr(
        Path, "is_symlink",
        lambda path: True if path == evidence else real_is_symlink(path))
    monkeypatch.setattr(
        "looplab.engine.costs.os.path.lexists",
        lambda path: True if Path(path) == evidence else real_lexists(path))
    accountant = CostAccountant()
    monkeypatch.setattr("looplab.engine.costs.secrets.token_hex", lambda _size: usage_id)
    eng = _engine(run_dir, researcher=_role(accountant))

    accountant.add(.16, _usage(6, 1))

    assert not evidence.exists()
    assert "outbox" in (accountant.last_sink_error or "")
    assert not [event for event in eng.store.read_all() if event.type == EV_LLM_USAGE]
    assert emit_llm_cost(eng, finalize_scope="finish:broken-symlink") is False
    assert not evidence.exists()
    assert not [event for event in eng.store.read_all() if event.type == EV_LLM_COST]


def test_broken_outbox_directory_symlink_is_pending_evidence_not_absence(
        tmp_path, monkeypatch):
    run_dir = tmp_path / "broken-outbox-directory"
    eng = _engine(run_dir)
    outbox = run_dir / ".llm-usage-outbox"
    real_lstat = Path.lstat
    # Simulate a broken directory symlink deterministically on Windows, where creating a real
    # symlink may require an elevated developer-mode privilege.
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda path: os.stat_result((stat.S_IFLNK, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        if path == outbox else real_lstat(path),
    )

    assert not outbox.exists()
    assert reconcile_usage_outbox(eng.store) is False
    assert reconcile_cost_accountants(eng) is False
    assert emit_llm_cost(eng, finalize_scope="finish:broken-outbox-directory") is False
    assert not [event for event in eng.store.read_all() if event.type == EV_LLM_COST]


def test_append_that_commits_then_raises_is_not_retried_as_a_second_delta(tmp_path):
    accountant = CostAccountant()
    eng = _engine(tmp_path / "ambiguous-append", researcher=_role(accountant))
    real_append = eng.store.append
    raised_after_commit = False

    def ambiguous_append(event_type, data, *args, **kwargs):
        nonlocal raised_after_commit
        result = real_append(event_type, data, *args, **kwargs)
        if event_type == EV_LLM_USAGE and not raised_after_commit:
            raised_after_commit = True
            raise OSError("ack lost after durable write")
        return result

    eng.store.append = ambiguous_append
    accountant.add(.7, _usage(7, 3))

    assert raised_after_commit
    assert accountant.last_sink_error is None
    assert emit_llm_cost(eng, finalize_scope="finish:ambiguous") is True
    usage_events = [event for event in eng.store.read_all() if event.type == EV_LLM_USAGE]
    assert len(usage_events) == 1
    assert usage_events[0].data["usage_id"]
    total = fold(eng.store.read_all()).llm_cost
    assert total["cost"] == pytest.approx(.7)
    assert total["calls"] == 1
    assert total["total_tokens"] == 10


def test_reconcile_during_delayed_sink_does_not_infer_and_duplicate_snapshot(tmp_path):
    accountant = CostAccountant()
    eng = _engine(tmp_path / "delayed-sink", researcher=_role(accountant))
    durable_sink = accountant.on_delta
    assert callable(durable_sink)
    entered = threading.Event()
    release = threading.Event()

    def delayed_sink(delta):
        entered.set()
        assert release.wait(timeout=5), "test did not release delayed accounting sink"
        durable_sink(delta)

    accountant.set_sink(delayed_sink)
    worker = threading.Thread(target=accountant.add, args=(.3, _usage(3, 1)), daemon=True)
    worker.start()
    assert entered.wait(timeout=5), "accountant did not reach delayed sink"
    try:
        # Counters are already committed, but the durable callback has not queued its delta. A
        # snapshot-based catch-up here would append once and the delayed sink would append again.
        assert (accountant.calls, accountant.total_tokens) == (1, 4)
        assert reconcile_cost_accountants(eng) is True
        assert not [event for event in eng.store.read_all() if event.type == EV_LLM_USAGE]
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()

    assert emit_llm_cost(eng, finalize_scope="finish:delayed") is True
    usage_events = [event for event in eng.store.read_all() if event.type == EV_LLM_USAGE]
    assert len(usage_events) == 1
    total = fold(eng.store.read_all()).llm_cost
    assert total["cost"] == pytest.approx(.3)
    assert total["calls"] == 1


def test_reused_accountant_transfers_future_calls_to_new_engine_only(tmp_path):
    shared = CostAccountant()
    old = _engine(tmp_path / "old-owner", researcher=_role(shared))
    shared.add(.1, _usage(1, 1))

    new = _engine(tmp_path / "new-owner", researcher=_role(shared))
    shared.add(.2, _usage(2, 1))

    assert emit_llm_cost(old, finalize_scope="finish:old") is True
    assert emit_llm_cost(new, finalize_scope="finish:new") is True
    old_total = fold(old.store.read_all()).llm_cost
    new_total = fold(new.store.read_all()).llm_cost
    assert old_total["cost"] == pytest.approx(.1)
    assert old_total["calls"] == 1
    assert old_total["total_tokens"] == 2
    assert new_total["cost"] == pytest.approx(.2)
    assert new_total["calls"] == 1
    assert new_total["total_tokens"] == 3


def test_unresolved_pending_delta_prevents_summary_and_finalize_marker(tmp_path):
    accountant = CostAccountant()
    eng = _engine(tmp_path / "unresolved", researcher=_role(accountant))
    eng.store.append("run_started", {
        "run_id": "unresolved", "task_id": eng.task.id, "goal": eng.task.goal,
        "direction": eng.task.direction,
    })
    eng.store.append("run_finished", {"reason": "done"})
    eng._store_case = lambda _state: None
    eng._write_reflection_note = lambda _state: None
    real_append = eng.store.append

    def reject_usage(event_type, data, *args, **kwargs):
        if event_type == EV_LLM_USAGE:
            raise OSError("ledger remains unavailable")
        return real_append(event_type, data, *args, **kwargs)

    eng.store.append = reject_usage
    accountant.add(.9, _usage(9, 1))

    assert emit_llm_cost(eng, finalize_scope="finish:unresolved") is False
    finalize_run(eng, entry_finished=False, start_time=0.0)
    events = eng.store.read_all()
    assert not [event for event in events if event.type == EV_LLM_COST]
    assert not [event for event in events
                if event.type == "finalize_step" and event.data.get("step") == "llm_cost"]


def test_legacy_snapshot_retry_reserves_pending_delta_instead_of_duplicating_it(tmp_path):
    legacy = SimpleNamespace(
        spent=0.0,
        calls=0,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
    )
    eng = _engine(tmp_path / "legacy-pending", researcher=_role(legacy))
    legacy.spent = .5
    legacy.calls = 1
    legacy.prompt_tokens = 4
    legacy.completion_tokens = 1
    legacy.total_tokens = 5
    real_append = eng.store.append
    reject = True

    def flaky_append(event_type, data, *args, **kwargs):
        if reject and event_type == EV_LLM_USAGE:
            raise OSError("temporary ledger outage")
        return real_append(event_type, data, *args, **kwargs)

    eng.store.append = flaky_append
    assert reconcile_cost_accountants(eng) is False
    assert reconcile_cost_accountants(eng) is False
    reject = False
    assert reconcile_cost_accountants(eng) is True

    usage_events = [event for event in eng.store.read_all() if event.type == EV_LLM_USAGE]
    assert len(usage_events) == 1
    assert fold(eng.store.read_all()).llm_cost == {
        "cost": .5,
        "calls": 1,
        "prompt_tokens": 4,
        "completion_tokens": 1,
        "total_tokens": 5,
    }
