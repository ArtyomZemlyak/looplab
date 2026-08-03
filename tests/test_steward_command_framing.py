"""The framing every paid steward command shares, and the four governed writes (doc 25 CT-04).

`concept-steward`, `task-facets` and `claim-steward` each wrote out the same four-step preamble, and
four deterministic governance writes each wrote out the same refusal block. Neither is decoration:

* the steward preamble is an ORDERED set of fail-closed boundaries. Reject `--apply` before anything
  costs money; preflight the audit history before a provider client exists; resolve the model
  override through `apply_llm_model_override` (a direct `settings.llm_model = …` lands a phantom
  attribute, because assignment validation is off, and the override silently does nothing); only
  then run the durable at-most-once transaction.
* the write refusal keeps two DIFFERENT failures apart. A ledger/lock failure is redacted, because an
  unreadable governance store must not leak filesystem shape into CLI output; a `ValueError` is the
  operator's own bad argument and its text is the whole diagnosis.

Every one of those fails quietly if a copy drifts: money spent on a refused command, a proposal run
against an unreadable ledger, a `--model` flag that does nothing, or a storage path printed to a
terminal.
"""
from __future__ import annotations

import pytest
import typer

from looplab.cli import inspect_cmds
from looplab.cli.inspect_cmds import _governed_write, _steward_command
from looplab.engine.governance_health import GovernanceLedgerUnavailable
from looplab.events.eventstore import EventStoreLockError


# ------------------------------------------------------------------ the steward preamble ORDER

# `preflight=None` is a MEANINGFUL argument to `_steward_command` ("this steward has no governance
# snapshot to validate"), so the helper cannot reuse it as its own "caller said nothing" sentinel —
# that collision is exactly what made an earlier draft forward `False` and get a TypeError instead of
# testing the no-preflight path.
_UNSET = object()


def _run(monkeypatch, *, apply=False, model=None, preflight=_UNSET, order=None):
    order = order if order is not None else []
    monkeypatch.setattr(inspect_cmds, "_make_llm_client",
                        lambda settings: order.append("client") or "the-client")
    def _transaction(*_a, prepare, **_k):
        # the durable transaction OWNS client construction: it calls `prepare` only once it has
        # decided this action id is not already settled. Calling it here is what makes the recorded
        # order reflect where the provider client really comes into existence.
        order.append("transaction")
        prepare()
        return {"proposals": {}}

    monkeypatch.setattr(inspect_cmds, "_run_cli_steward", _transaction)
    if preflight is _UNSET:
        preflight = lambda: order.append("preflight")  # noqa: E731 - order probe, not a name
    return _steward_command(
        "mem", "concept", "act-1", apply=apply, apply_refusal="error: --apply is gone", model=model,
        preflight=preflight,
        invoke=lambda client: {}, request={"surface": "cli"}), order


def test_a_deprecated_apply_is_refused_before_ANYTHING_costs_money(monkeypatch):
    """The command is proposal-only. An operator who passes `--apply` is asking for a mutation that
    will not happen; billing them for a proposal they did not want on the way to saying so is the
    failure this ordering prevents."""
    order = []
    with pytest.raises(typer.Exit) as exit_info:
        _run(monkeypatch, apply=True, order=order)
    assert exit_info.value.exit_code == 2
    assert order == [], "work happened after --apply was supposed to short-circuit"


def test_the_audit_preflight_runs_BEFORE_a_provider_client_exists(monkeypatch):
    """A paid proposal must not run against an invocation log that cannot be read — otherwise
    corrupting the ledger becomes a way to make the CLI spend money."""
    _out, order = _run(monkeypatch)
    assert order == ["preflight", "transaction", "client"]


def test_the_client_is_a_THUNK_the_transaction_calls_not_one_built_up_front(monkeypatch):
    """`prepare=lambda: _make_llm_client(settings)`, not `prepare=_make_llm_client(settings)`. The
    transaction decides whether this action id was already settled; building the provider client in
    the framing would construct one — and, for providers that validate credentials eagerly, fail —
    on every replay of an action that will never call the model."""
    order = []
    monkeypatch.setattr(inspect_cmds, "_make_llm_client",
                        lambda settings: order.append("client") or "the-client")
    monkeypatch.setattr(inspect_cmds, "_run_cli_steward",
                        lambda *a, **k: order.append("transaction") or {})
    _steward_command("mem", "concept", "act-1", apply=False, apply_refusal="no", model=None,
                     preflight=None, invoke=lambda client: {}, request={})
    assert order == ["transaction"], "the client was built before the transaction was entered"


def test_a_failing_preflight_stops_the_command_without_paying(monkeypatch):
    def _boom():
        raise GovernanceLedgerUnavailable("concept_governance", "storage_unreadable")

    order = []
    with pytest.raises(typer.Exit):
        _run(monkeypatch, preflight=_boom, order=order)
    assert "client" not in order and "transaction" not in order


def test_a_command_with_no_preflight_still_runs(monkeypatch):
    """Not every steward has a governance snapshot to validate; `preflight=None` must mean "nothing
    to check", not "skip the rest"."""
    _out, order = _run(monkeypatch, preflight=None)
    assert order == ["transaction", "client"]


def test_the_model_override_goes_through_the_validated_setter(monkeypatch):
    """`settings.llm_model = model` looks right and does NOTHING: assignment validation is disabled on
    `Settings`, so the write lands a phantom attribute and the paid call goes to the default model.
    The override has to run through `apply_llm_model_override`."""
    seen = []
    monkeypatch.setattr(inspect_cmds, "apply_llm_model_override",
                        lambda settings, model: seen.append(model))
    _run(monkeypatch, model="a-specific-model")
    assert seen == ["a-specific-model"]


def test_no_model_flag_means_no_override_call(monkeypatch):
    seen = []
    monkeypatch.setattr(inspect_cmds, "apply_llm_model_override",
                        lambda settings, model: seen.append(model))
    _run(monkeypatch, model=None)
    assert seen == []


def test_the_durable_transaction_receives_the_kind_and_action_id(monkeypatch):
    """The action id is what fences the paid call across crash/retry; losing it in the framing would
    turn at-most-once into at-least-once, silently."""
    captured = {}
    monkeypatch.setattr(inspect_cmds, "_make_llm_client", lambda settings: "client")
    monkeypatch.setattr(inspect_cmds, "_run_cli_steward",
                        lambda memory_dir, kind, action_id, **kw: captured.update(
                            memory_dir=memory_dir, kind=kind, action_id=action_id, **kw) or {})
    _steward_command("mem", "claim", "act-42", apply=False, apply_refusal="no", model=None,
                     preflight=None, invoke="the-invoke", request={"surface": "cli"})
    assert captured["kind"] == "claim" and captured["action_id"] == "act-42"
    assert captured["invoke"] == "the-invoke" and captured["request"] == {"surface": "cli"}


# ------------------------------------------------------------------ the governed-write refusal

@pytest.mark.parametrize("exc", [
    GovernanceLedgerUnavailable("concept_governance", "storage_unreadable"),
    EventStoreLockError("events.jsonl", OSError("locked")),
])
def test_a_ledger_or_lock_failure_is_REDACTED_not_echoed(exc, monkeypatch, capsys):
    """`_governance_cli_error` withholds the OS path and the platform's parser text. Echoing the
    exception here instead would print storage shape the redaction boundary exists to keep out."""
    seen = []
    monkeypatch.setattr(inspect_cmds, "_governance_cli_error",
                        lambda e: seen.append(type(e).__name__))
    with _governed_write():
        raise exc
    assert seen == [type(exc).__name__]
    assert "error:" not in capsys.readouterr().out


def test_a_ValueError_is_echoed_VERBATIM_because_it_is_the_diagnosis(capsys):
    """A self-link, a cycle-closing edge, an unknown axis — the message is what tells the operator
    what to change. Routing it through the redacting path would hide the only useful part."""
    with pytest.raises(typer.Exit) as exit_info:
        with _governed_write():
            raise ValueError("cannot merge a concept into itself")
    assert exit_info.value.exit_code == 2
    assert "cannot merge a concept into itself" in capsys.readouterr().out


def test_a_successful_write_passes_through_untouched():
    done = []
    with _governed_write():
        done.append("wrote")
    assert done == ["wrote"]


def test_an_unexpected_exception_is_not_swallowed():
    """The block names two failures it knows how to explain. Anything else is a bug, and a bug that
    prints "error: …" and exits 2 looks like an operator mistake."""
    with pytest.raises(RuntimeError):
        with _governed_write():
            raise RuntimeError("something nobody anticipated")


# ------------------------------------------------------------------ every call site uses them

@pytest.mark.parametrize("command", ["concept_steward_cmd", "task_facets_cmd", "claim_steward_cmd"])
def test_every_paid_steward_goes_through_the_shared_framing(command):
    import inspect

    source = inspect.getsource(getattr(inspect_cmds, command))
    assert "_steward_command(" in source, f"{command} re-derives the steward preamble"
    assert "_run_cli_steward(" not in source, f"{command} bypasses the framing"
    assert "apply_llm_model_override(" not in source, f"{command} resolves the model itself"


@pytest.mark.parametrize("command", ["concept_merge_cmd", "concept_split_cmd",
                                     "claim_decide_cmd", "task_facets_set_cmd"])
def test_every_deterministic_write_goes_through_the_shared_refusal(command):
    import inspect

    source = inspect.getsource(getattr(inspect_cmds, command))
    assert "with _governed_write():" in source, f"{command} re-derives the refusal block"
    assert "GovernanceLedgerUnavailable" not in source, f"{command} still catches it by hand"


def test_the_refusal_block_exists_in_exactly_one_place():
    """A grep guard: the `(GovernanceLedgerUnavailable, EventStoreLockError)` pair outside the two
    shared helpers is a fifth copy, and a copy is where the two arms get collapsed."""
    import inspect

    source = inspect.getsource(inspect_cmds)
    assert source.count("(GovernanceLedgerUnavailable, EventStoreLockError)") == 2, (
        "expected exactly two: `_governance_cli_read` and `_governed_write`")
