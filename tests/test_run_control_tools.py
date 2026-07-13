"""RunControlTools: the assistant's run-lifecycle verbs (finalize/stop/resume/reset/delete node/run).
Mode-gated (deny in plan, inline in auto) + destructive verbs refuse a live engine + delete_node takes
the whole subtree so no parent link is orphaned."""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

import pytest

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.tools.machine_runs_tools import RunControlTools


class _RecordingCommands:
    """Tiny service double: the tool submits; the service double alone owns the event append."""

    def __init__(self, root, status="succeeded", error=None, *, append=True):
        self.root = root
        self.status = status
        self.error = error
        self.append = append
        self.calls = []

    def submit(self, rd, idempotency_key, event_type, data):
        self.calls.append((rd.name, event_type, data, idempotency_key))
        if self.append and self.status in {"succeeded", "noop"}:
            EventStore(rd / "events.jsonl").append(event_type, data)
        return {"id": f"cmd-{len(self.calls)}", "status": self.status,
                "event_type": event_type, "error": self.error}


def _run(rd, nodes=(0, 1, 2)):
    rd.mkdir(parents=True, exist_ok=True)
    s = EventStore(rd / "events.jsonl")
    s.append("run_started", {"run_id": rd.name, "task_id": "t", "goal": "g", "direction": "min"})
    parent = []
    for nid in nodes:
        s.append("node_created", {"node_id": nid, "parent_ids": parent, "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"x": float(nid)}}, "code": "c"})
        s.append("node_evaluated", {"node_id": nid, "metric": float(nid)})
        parent = [nid]                       # a chain 0 <- 1 <- 2
    return s


def test_finalize_appends_run_abort_auto_mode(tmp_path):
    rd = tmp_path / "r1"
    _run(rd)
    commands = _RecordingCommands(tmp_path)
    t = RunControlTools(tmp_path, alive_fn=lambda _rd: False, mode="auto",
                        command_service=commands)   # auto = inline, no approver
    out = t.execute("finalize_run", {"run_id": "r1"})
    assert "completed" in out
    assert commands.calls[0][0:3] == ("r1", "run_abort", {"reason": "finalized"})
    assert uuid.UUID(commands.calls[0][3])
    types = [e.type for e in EventStore(rd / "events.jsonl").read_all()]
    assert "run_abort" in types


def test_assistant_turn_namespace_reconstructs_same_ordered_command_keys(tmp_path):
    _run(tmp_path / "stable")
    first = _RecordingCommands(tmp_path, append=False)
    second = _RecordingCommands(tmp_path, append=False)

    for commands in (first, second):
        tool = RunControlTools(
            tmp_path, alive_fn=lambda _rd: False, mode="auto", command_service=commands,
            command_key_namespace="session-a:turn-7")
        tool.execute("stop_run", {"run_id": "stable"})
        tool.execute("extend_budget", {"run_id": "stable", "add_nodes": 2})

    assert [call[3] for call in first.calls] == [call[3] for call in second.calls]
    assert first.calls[0][3] != first.calls[1][3]
    assert all(key.startswith("asst_") for key in (call[3] for call in first.calls))

    other = _RecordingCommands(tmp_path, append=False)
    RunControlTools(
        tmp_path, alive_fn=lambda _rd: False, mode="auto", command_service=other,
        command_key_namespace="session-a:turn-8").execute("stop_run", {"run_id": "stable"})
    assert other.calls[0][3] != first.calls[0][3]


def test_recovered_turn_reuses_journaled_intent_and_fences_changed_payload(tmp_path):
    """A dangling turn may observe its exact +10 again, but a nondeterministic +20 replay is inert."""
    _run(tmp_path / "stable")
    journal = tmp_path / "assistant-turn-mutations.json"
    namespace = "session-a:turn-crashed"

    first = _RecordingCommands(tmp_path, append=False)
    original = RunControlTools(
        tmp_path, alive_fn=lambda _rd: False, mode="auto", command_service=first,
        command_key_namespace=namespace, mutation_journal_path=journal)
    assert "completed" in original.execute(
        "extend_budget", {"run_id": "stable", "add_nodes": 10})
    assert len(first.calls) == 1 and journal.exists()

    exact = _RecordingCommands(tmp_path, append=False)
    exact_recovery = RunControlTools(
        tmp_path, alive_fn=lambda _rd: False, mode="auto", command_service=exact,
        command_key_namespace=namespace, mutation_journal_path=journal,
        mutation_recovery=True)
    assert "completed" in exact_recovery.execute(
        "extend_budget", {"run_id": "stable", "add_nodes": 10})
    assert len(exact.calls) == 1
    assert exact.calls[0][3] == first.calls[0][3]

    changed = _RecordingCommands(tmp_path, append=False)
    changed_recovery = RunControlTools(
        tmp_path, alive_fn=lambda _rd: False, mode="auto", command_service=changed,
        command_key_namespace=namespace, mutation_journal_path=journal,
        mutation_recovery=True)
    out = changed_recovery.execute(
        "extend_budget", {"run_id": "stable", "add_nodes": 20})
    assert "assistant_turn_recovery_conflict" in out
    assert changed.calls == []


@pytest.mark.parametrize("journal_text", [None, "{not-json"])
def test_recovered_turn_without_valid_mutation_journal_fails_closed(tmp_path, journal_text):
    _run(tmp_path / "stable")
    journal = tmp_path / "assistant-turn-mutations.json"
    if journal_text is not None:
        journal.write_text(journal_text, encoding="utf-8")
    commands = _RecordingCommands(tmp_path, append=False)
    recovery = RunControlTools(
        tmp_path, alive_fn=lambda _rd: False, mode="auto", command_service=commands,
        command_key_namespace="session-a:turn-missing", mutation_journal_path=journal,
        mutation_recovery=True)

    out = recovery.execute("extend_budget", {"run_id": "stable", "add_nodes": 10})

    expected = ("assistant_turn_recovery_fenced" if journal_text is None
                else "assistant_turn_journal_unavailable")
    assert expected in out
    assert commands.calls == []


def test_plan_mode_denies(tmp_path):
    _run(tmp_path / "r2")
    t = RunControlTools(tmp_path, mode="plan")
    assert "plan mode" in t.execute("finalize_run", {"run_id": "r2"})


def test_delete_node_takes_subtree_and_heals_best(tmp_path):
    rd = tmp_path / "r3"
    _run(rd, nodes=(0, 1, 2)).append("pause", {})   # settled → the fresh-write live backstop stands down
    t = RunControlTools(tmp_path, alive_fn=lambda _rd: False, mode="auto",
                        command_service=_RecordingCommands(tmp_path))
    out = t.execute("delete_node", {"run_id": "r3", "node_id": 1})   # deletes 1 AND its descendant 2
    assert "deleted node(s) [1, 2]" in out
    st = fold(EventStore(rd / "events.jsonl").read_all())
    assert set(st.nodes) == {0}                                     # only #0 remains
    assert not [p for n in st.nodes.values() for p in n.parent_ids if p not in st.nodes]  # no broken links
    assert (rd / "events.jsonl.bak-del1").exists()                  # recoverable backup


def test_reset_node_spec_accepts_any_stage_name(tmp_path):
    """F-reset-enum: prompts/executor/HTTP route accept ANY eval-pipeline stage name (train,
    data_prep, …), so the spec must not hard-code an enum — and the tool must actually queue a
    non-classic stage."""
    rd = tmp_path / "r5"
    _run(rd)
    t = RunControlTools(tmp_path, alive_fn=lambda _rd: False, mode="auto",
                        command_service=_RecordingCommands(tmp_path))
    spec = next(s for s in t.specs() if s["function"]["name"] == "reset_node")
    stage = spec["function"]["parameters"]["properties"]["stage"]
    assert "enum" not in stage                                  # no hard-coded stage list
    assert "eval-pipeline stage" in stage["description"]        # accepted values described instead
    out = t.execute("reset_node", {"run_id": "r5", "node_id": 1, "stage": "train"})
    assert "re-run from train" in out
    ev = [e for e in EventStore(rd / "events.jsonl").read_all() if e.type == "node_reset"]
    assert ev and ev[-1].data["from_stage"] == "train"


def test_destructive_refuses_live_engine(tmp_path):
    _run(tmp_path / "r4")
    t = RunControlTools(tmp_path, alive_fn=lambda _rd: True, mode="auto")   # engine "live"
    assert "LIVE" in t.execute("delete_run", {"run_id": "r4"})
    assert (tmp_path / "r4").exists()                               # not deleted


def test_traversal_and_unknown_run_rejected(tmp_path):
    t = RunControlTools(tmp_path, mode="auto", alive_fn=lambda _rd: False)
    assert "no such run" in t.execute("finalize_run", {"run_id": "../etc"})
    assert "no such run" in t.execute("finalize_run", {"run_id": "nope"})


def test_lifecycle_and_engine_controls_only_submit_commands(tmp_path):
    """Assistant control tools never append lifecycle intents themselves; the service is sole writer."""
    rd = tmp_path / "svc"
    _run(rd)
    before = (rd / "events.jsonl").read_bytes()
    commands = _RecordingCommands(tmp_path, append=False)
    t = RunControlTools(tmp_path, alive_fn=lambda _rd: False, mode="auto",
                        command_service=commands)

    assert "completed" in t.execute("stop_run", {"run_id": "svc"})
    assert "completed" in t.execute("finalize_run", {"run_id": "svc"})
    assert "completed" in t.execute("resume_run", {"run_id": "svc"})
    assert "completed" in t.execute("reset_node", {"run_id": "svc", "node_id": 1, "stage": "eval"})
    assert "completed" in t.execute("extend_budget", {"run_id": "svc", "add_nodes": 2})
    assert "completed" in t.execute("set_directive", {"run_id": "svc", "text": "prefer linear"})

    assert (rd / "events.jsonl").read_bytes() == before
    assert [call[1] for call in commands.calls] == [
        "pause", "run_abort", "resume", "node_reset", "budget_extend", "hint"]
    keys = [call[3] for call in commands.calls]
    assert len(set(keys)) == len(keys) and all(uuid.UUID(key) for key in keys)

    unavailable = RunControlTools(tmp_path, mode="auto")
    out = unavailable.execute("stop_run", {"run_id": "svc"})
    assert "command_service_unavailable" in out
    assert (rd / "events.jsonl").read_bytes() == before


def test_permission_card_precedes_command_submission(tmp_path):
    _run(tmp_path / "ask")
    commands = _RecordingCommands(tmp_path, append=False)
    denied = RunControlTools(tmp_path, mode="default", approver=lambda _a: "deny",
                             command_service=commands)
    assert "declined" in denied.execute("finalize_run", {"run_id": "ask"})
    assert commands.calls == []

    allowed = RunControlTools(tmp_path, mode="default", approver=lambda _a: "allow_once",
                              command_service=commands)
    assert "completed" in allowed.execute("finalize_run", {"run_id": "ask"})
    assert len(commands.calls) == 1


def test_pending_and_terminal_failure_are_reported_honestly(tmp_path):
    _run(tmp_path / "status")
    pending = _RecordingCommands(tmp_path, status="executing", append=False)
    ptool = RunControlTools(tmp_path, mode="auto", command_service=pending)
    pout = ptool.execute("resume_run", {"run_id": "status"})
    assert "requested/pending" in pout and "completed" not in pout

    failed = _RecordingCommands(tmp_path, status="rejected", append=False, error={
        "code": "invalid_state", "message": "Run is already finalized.", "retryable": False,
        "remediation": "Resume it before extending the budget.",
        "raw": "SECRET traceback must never escape",
    })
    ftool = RunControlTools(tmp_path, mode="auto", command_service=failed)
    fout = ftool.execute("extend_budget", {"run_id": "status", "add_nodes": 1})
    assert "command failed" in fout and "code=invalid_state" in fout
    assert "already finalized" in fout and "Resume it" in fout
    assert "SECRET" not in fout and "completed" not in fout


def test_command_adapter_uses_exact_service_signature_without_duplicate_retry(tmp_path):
    _run(tmp_path / "pos")

    class PositionalCommands:
        def __init__(self):
            self.calls = []

        def submit(self, rd, key, event_type, data, /):
            self.calls.append((rd.name, event_type, data, key))
            return {"id": "pos-1", "status": "succeeded", "event_type": event_type}

    positional = PositionalCommands()
    out = RunControlTools(tmp_path, mode="auto", command_service=positional).execute(
        "stop_run", {"run_id": "pos"})
    assert "completed" in out and len(positional.calls) == 1
    assert uuid.UUID(positional.calls[0][3])

    class BrokenCommands:
        def __init__(self):
            self.calls = 0

        def submit(self, rd, idempotency_key, event_type, data):
            self.calls += 1
            raise TypeError("SECRET internal bug")

    broken = BrokenCommands()
    out = RunControlTools(tmp_path, mode="auto", command_service=broken).execute(
        "stop_run", {"run_id": "pos"})
    assert broken.calls == 1
    assert "command_status_uncertain" in out and "SECRET" not in out
    assert "cmd_" in out and "no" in out.lower()  # explicit id + no blind retry


def test_pending_or_ambiguous_command_blocks_later_controls_in_same_turn(tmp_path):
    _run(tmp_path / "guarded")
    pending = _RecordingCommands(tmp_path, status="executing", append=False)
    tool = RunControlTools(tmp_path, mode="auto", command_service=pending)
    first = tool.execute("resume_run", {"run_id": "guarded"})
    second = tool.execute("stop_run", {"run_id": "guarded"})
    assert "requested/pending" in first
    assert "command_in_progress" in second and "cmd-1" in second
    assert len(pending.calls) == 1

    class BrokenCommands:
        def __init__(self):
            self.calls = 0

        def submit(self, *_args):
            self.calls += 1
            raise TimeoutError("SECRET maybe accepted")

    broken = BrokenCommands()
    uncertain = RunControlTools(tmp_path, mode="auto", command_service=broken)
    first = uncertain.execute("resume_run", {"run_id": "guarded"})
    second = uncertain.execute("stop_run", {"run_id": "guarded"})
    assert "command_status_uncertain" in first and "SECRET" not in first
    assert "command_in_progress" in second
    assert broken.calls == 1


def test_structured_existing_command_conflict_keeps_id_and_remediation(tmp_path):
    _run(tmp_path / "conflict")
    existing_id = "cmd_" + "b" * 32

    class Conflict(Exception):
        def __init__(self):
            self.detail = {"code": "retry_existing_command",
                           "existing_command_id": existing_id,
                           "remediation": f"POST /commands/{existing_id}/retry"}

    class Commands:
        def __init__(self):
            self.calls = 0

        def submit(self, *_args):
            self.calls += 1
            raise Conflict()

        def get(self, _rd, command_id):
            assert command_id == existing_id
            return {"id": existing_id, "status": "failed", "event_type": "pause", "error": {
                "code": "spawn_failed", "message": "engine did not start", "retryable": True,
                "remediation": f"POST /commands/{existing_id}/retry",
            }}

    commands = Commands()
    out = RunControlTools(tmp_path, mode="auto", command_service=commands).execute(
        "stop_run", {"run_id": "conflict"})
    assert existing_id in out and "spawn_failed" in out and "/retry" in out
    assert commands.calls == 1


def test_different_active_command_can_never_be_reported_as_requested_action_success(tmp_path):
    _run(tmp_path / "different")
    existing_id = "cmd_" + "d" * 32

    class Conflict(Exception):
        def __init__(self):
            self.detail = {"code": "command_in_progress", "existing_command_id": existing_id,
                           "message": "A resume command is active.",
                           "remediation": f"GET /commands/{existing_id}"}

    class Commands:
        def submit(self, *_args):
            raise Conflict()

        def get(self, _rd, command_id):
            assert command_id == existing_id
            # It terminalized between 409 and observation. This is success for the prior RESUME,
            # never success for the newly requested STOP.
            return {"id": existing_id, "status": "succeeded", "event_type": "resume", "error": None}

    tool = RunControlTools(tmp_path, mode="auto", command_service=Commands())
    record = tool._commands.submit(tmp_path / "different", "pause", {})
    assert record["status"] == "rejected" and "id" not in record
    assert record["error"]["retryable"] is False
    assert existing_id in record["error"]["remediation"]
    out = RunControlTools(tmp_path, mode="auto", command_service=Commands()).execute(
        "stop_run", {"run_id": "different"})
    assert "command_in_progress" in out and existing_id in out
    assert "completed" not in out and "A resume command is active" in out


@pytest.mark.parametrize("name,args", [
    ("delete_run", {"run_id": "ordered"}),
    ("delete_node", {"run_id": "ordered", "node_id": 1}),
])
def test_destructive_approval_then_guard_then_live_recheck(tmp_path, name, args):
    rd = tmp_path / "ordered"
    _run(rd).append("pause", {})
    order = []

    class Commands(_RecordingCommands):
        @contextmanager
        def destructive_guard(self, guarded_rd, operation):
            order.append(("guard-enter", operation, guarded_rd.name))
            try:
                yield guarded_rd
            finally:
                order.append(("guard-exit", operation, guarded_rd.name))

    def approve(_action):
        order.append(("approve",))
        return "allow_once"

    def alive(_rd):
        order.append(("live",))
        return True

    tool = RunControlTools(tmp_path, alive_fn=alive, mode="default", approver=approve,
                           command_service=Commands(tmp_path))
    out = tool.execute(name, args)
    assert "LIVE" in out and rd.exists()
    assert [item[0] for item in order] == ["approve", "guard-enter", "live", "guard-exit"]


def test_delete_node_requires_reapproval_if_descendant_scope_changes(tmp_path):
    rd = tmp_path / "scope"
    _run(rd, nodes=(0, 1)).append("pause", {})

    class Commands(_RecordingCommands):
        @contextmanager
        def destructive_guard(self, guarded_rd, _operation):
            # Deterministic stand-in for another actor adding a descendant while the confirm card is open.
            store = EventStore(guarded_rd / "events.jsonl")
            store.append("node_created", {"node_id": 2, "parent_ids": [1], "operator": "draft",
                                          "idea": {"operator": "draft", "params": {}}, "code": "c"})
            yield guarded_rd

    tool = RunControlTools(tmp_path, alive_fn=lambda _rd: False, mode="default",
                           approver=lambda _action: "allow_once",
                           command_service=Commands(tmp_path))
    out = tool.execute("delete_node", {"run_id": "scope", "node_id": 1})
    assert "scope changed" in out and "approve again" in out
    assert set(fold(EventStore(rd / "events.jsonl").read_all()).nodes) == {0, 1, 2}
    assert not (rd / "events.jsonl.bak-del1").exists()


def test_run_and_event_symlink_alias_is_rejected_even_for_direct_trust_write(tmp_path):
    outside = tmp_path / "outside"
    _run(outside)
    alias = tmp_path / "alias"
    try:
        os.symlink(outside, alias, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    before = (outside / "events.jsonl").read_bytes()
    tool = RunControlTools(tmp_path, alive_fn=lambda _rd: False, mode="auto",
                           command_service=_RecordingCommands(tmp_path))
    assert "no such run" in tool.execute(
        "set_trust_gate", {"run_id": "alias", "trust_gate": "block"})
    assert (outside / "events.jsonl").read_bytes() == before


def test_delete_refuses_fresh_write_even_when_flock_says_dead(tmp_path):
    # security backstop: on a FUSE mount flock (alive_fn) can wrongly say "dead"; a fresh events.jsonl
    # write on a non-settled run must still be treated as LIVE so the log isn't rewritten under it.
    _run(tmp_path / "r5", nodes=(0, 1))     # just written, not paused/finished
    t = RunControlTools(tmp_path, alive_fn=lambda _rd: False, mode="auto")   # flock lies: "dead"
    assert "LIVE" in t.execute("delete_node", {"run_id": "r5", "node_id": 1})
    assert not (tmp_path / "r5" / "events.jsonl.bak-del1").exists()          # never rewrote the log


# --- live settings tools (the assistant CAN change certain run settings) --------------------------

def _tools(tmp_path):
    return RunControlTools(tmp_path, alive_fn=lambda _rd: False, mode="auto",
                           command_service=_RecordingCommands(tmp_path))


def test_extend_budget_appends_budget_extend(tmp_path):
    rd = tmp_path / "b1"
    _run(rd)
    out = _tools(tmp_path).execute("extend_budget", {"run_id": "b1", "add_nodes": 5,
                                                     "max_eval_seconds": 1200})
    assert "budget extended" in out
    st = fold(EventStore(rd / "events.jsonl").read_all())
    assert st.budget_overrides.get("add_nodes") == 5
    assert st.budget_overrides.get("max_eval_seconds") == 1200.0


def test_extend_budget_uses_service_without_legacy_reopen(tmp_path):
    # The tool submits only budget_extend. Engine wake-up/postconditions belong to the real command
    # service; this recording double deliberately appends only the requested event.
    rd = tmp_path / "b2"
    _run(rd).append("run_finished", {})
    assert fold(EventStore(rd / "events.jsonl").read_all()).finished is True
    out = _tools(tmp_path).execute("extend_budget", {"run_id": "b2", "add_nodes": 3})
    assert "completed" in out
    types = [e.type for e in EventStore(rd / "events.jsonl").read_all()]
    assert "run_reopened" not in types and "budget_extend" in types
    st = fold(EventStore(rd / "events.jsonl").read_all())
    assert st.finished is True and st.budget_overrides.get("add_nodes") == 3   # stays finished


def test_extend_budget_rejects_nonfinite_negative_and_empty(tmp_path):
    _run(tmp_path / "b3")
    t = _tools(tmp_path)
    assert "finite" in t.execute("extend_budget", {"run_id": "b3", "max_seconds": float("inf")})
    assert "at least one" in t.execute("extend_budget", {"run_id": "b3"})
    # a negative add_nodes would SHRINK the budget (base + add_nodes) — reject it
    assert "positive" in t.execute("extend_budget", {"run_id": "b3", "add_nodes": -50})
    assert "positive" in t.execute("extend_budget", {"run_id": "b3", "add_nodes": 0})


def test_set_directive_appends_hint(tmp_path):
    rd = tmp_path / "d1"
    _run(rd)
    out = _tools(tmp_path).execute("set_directive", {"run_id": "d1", "text": "use only sklearn"})
    assert "directive recorded" in out
    st = fold(EventStore(rd / "events.jsonl").read_all())
    assert st.pending_hints and st.pending_hints[-1]["text"] == "use only sklearn"


def test_set_trust_gate_applies(tmp_path):
    rd = tmp_path / "g1"
    _run(rd)
    t = _tools(tmp_path)
    assert "must be audit" in t.execute("set_trust_gate", {"run_id": "g1", "trust_gate": "nonsense"})
    out = t.execute("set_trust_gate", {"run_id": "g1", "trust_gate": "block"})
    assert "trust_gate set to block" in out
    assert fold(EventStore(rd / "events.jsonl").read_all()).trust_gate == "block"


def test_set_trust_gate_rechecks_path_after_approval_and_cannot_recreate_deleted_run(tmp_path):
    import shutil
    pytest.importorskip("fastapi")
    from looplab.serve.server import make_app

    rd = tmp_path / "gone"
    _run(rd).append("pause", {})
    service = make_app(tmp_path).state.looplab.commands

    def approve(_action):
        shutil.rmtree(rd)
        return "allow_once"

    tool = RunControlTools(tmp_path, alive_fn=lambda _rd: False, mode="default",
                           approver=approve, command_service=service)
    out = tool.execute("set_trust_gate", {"run_id": "gone", "trust_gate": "block"})
    assert "tool error" in out.lower() or "no such run" in out.lower()
    assert not rd.exists()


def test_settings_tools_denied_in_plan_mode(tmp_path):
    _run(tmp_path / "p1")
    t = RunControlTools(tmp_path, mode="plan")
    for name, args in (("extend_budget", {"run_id": "p1", "add_nodes": 1}),
                       ("set_directive", {"run_id": "p1", "text": "x"}),
                       ("set_trust_gate", {"run_id": "p1", "trust_gate": "gate"})):
        assert "plan mode" in t.execute(name, args)
