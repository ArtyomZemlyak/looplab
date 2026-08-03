"""One owner-liveness decision, two claim carriers (doc 25 SC-12).

A spawn claim arrives as an already-parsed dict; an execution claim arrives as a file that must be
read (and may be a legacy bare-PID line). That was the only real difference, but the pid-state +
identity-reuse DECISION had been written twice — and it gates the same thing both times: whether a
second worker may take over durable command state. Two copies of that rule is two chances to answer
"the owner is gone" about a process that is merely suspended.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from looplab.serve.run_commands import RunCommandService  # noqa: E402


class _Service(RunCommandService):
    def __init__(self, *, alive=None, identity=None):
        self._alive, self._identity = alive, identity

    def process_alive(self, pid):
        if callable(self._alive):
            return self._alive(pid)
        return self._alive

    def process_identity(self, pid):
        if callable(self._identity):
            return self._identity(pid)
        return self._identity


# Source-TAGGED identities: `_process_identity_proves_reuse` only calls a mismatch
# conclusive when both tokens share a scheme, so an untagged fixture would prove nothing.
def _row(pid=4321, identity="psutil:1"):
    return {"pid": pid, "process_identity": identity}


# ------------------------------------------------------------------ definitely-gone (destructive)

def test_a_dead_pid_is_definitely_gone():
    assert _Service(alive=False)._owner_definitely_gone(_row()) is True


def test_a_reused_pid_is_definitely_gone():
    """The owner exited and the OS handed its number to something else — the identity token is the
    only thing that can tell that from a still-running owner."""
    service = _Service(alive=True, identity="psutil:2")
    assert service._owner_definitely_gone(_row(identity="psutil:1")) is True


@pytest.mark.parametrize("service,row", [
    (_Service(alive=True, identity="psutil:1"), _row()),
    (_Service(alive=None, identity="psutil:1"), _row()),          # unknown liveness
    (_Service(alive=True, identity=None), _row()),              # unavailable identity
    (_Service(alive=True), _row(identity=None)),                # legacy claim, no token
])
def test_everything_ambiguous_leaves_the_claim_alone(service, row):
    """This is the DESTRUCTIVE direction, so a suspended worker that missed every heartbeat must not
    read as gone — reclaiming its file would let two workers write terminal command state."""
    assert service._owner_definitely_gone(row) is False


@pytest.mark.parametrize("pid", [None, "4321", 0, -1, True, 1.5])
def test_an_unusable_pid_is_never_definitely_gone(pid):
    """The claim-dict path used to skip this shape check and hand the raw value to `process_alive`.
    Sharing the decision tightened it in the fail-CLOSED direction."""
    probed = []
    # `alive=False` on purpose: if the shape check were dropped, the probe would answer "dead" and
    # the claim would read as definitely gone. Raising here would be swallowed by the ambiguity
    # `except`, so the breakage has to be observable as a WRONG ANSWER, not as an exception.
    service = _Service(alive=lambda seen: probed.append(seen) or False)
    assert service._owner_definitely_gone({"pid": pid}) is False
    assert probed == [], "a malformed pid must not reach the liveness probe at all"


def test_a_probe_that_raises_is_ambiguous_not_gone():
    def _boom(_pid):
        raise OSError("permission denied")

    assert _Service(alive=_boom)._owner_definitely_gone(_row()) is False


# ------------------------------------------------------------------ exactly-alive (protective)

def test_the_exact_live_generation_is_recognized():
    assert _Service(alive=True, identity="psutil:1")._owner_exactly_alive(_row()) is True


@pytest.mark.parametrize("service", [
    _Service(alive=False, identity="psutil:1"),
    _Service(alive=None, identity="psutil:1"),
    _Service(alive=True, identity="psutil:2"),
])
def test_anything_short_of_an_exact_match_is_not_proof_of_life(service):
    assert service._owner_exactly_alive(_row()) is False


def test_own_process_counts_is_the_one_rule_that_differs():
    """A SPAWN claim with no comparable identity is not proof of an exact live generation. An
    EXECUTION claim written by THIS server process is, even where creation identity is unavailable:
    its worker or activity context may still be running, and clearing it is the same double-writer
    hazard the identity check exists to prevent."""
    service = _Service(alive=True, identity=None)
    mine = {"pid": os.getpid()}
    assert service._owner_exactly_alive(mine) is False
    assert service._owner_exactly_alive(mine, own_process_counts=True) is True
    # ...and it is scoped to THIS process, not to "any pid without a token".
    assert service._owner_exactly_alive({"pid": 4321}, own_process_counts=True) is False


# ------------------------------------------------------------------ both carriers, one decision

def test_the_spawn_carrier_delegates(monkeypatch):
    service = _Service(alive=True, identity="psutil:1")
    seen = []
    monkeypatch.setattr(_Service, "_owner_definitely_gone",
                        lambda self, row: seen.append(row) or True)
    assert service._claim_child_definitely_gone(_row()) is True
    assert seen == [_row()]


def test_the_execution_carrier_reads_the_file_then_delegates(tmp_path):
    path = tmp_path / ".cmd.executing"
    path.write_text(json.dumps({"pid": 4321, "process_identity": "psutil:1"}), encoding="utf-8")
    assert _Service(alive=False)._execution_owner_definitely_gone(path) is True
    assert _Service(alive=True, identity="psutil:1")._execution_owner_definitely_gone(path) is False


def test_a_legacy_bare_pid_claim_is_adapted_not_decided_again(tmp_path):
    """Older claims are a bare integer with no identity token. They stay READABLE — the adapter just
    gives them the same row shape — and the same ambiguity rules then apply."""
    path = tmp_path / ".cmd.executing"
    path.write_text("4321", encoding="utf-8")
    assert _Service(alive=False)._execution_owner_definitely_gone(path) is True
    assert _Service(alive=True)._execution_owner_definitely_gone(path) is False


@pytest.mark.parametrize("body", ["", "not-a-pid", "{}", "[1,2]", "{\"pid\": \"x\"}"])
def test_a_malformed_execution_claim_fails_closed(tmp_path, body):
    path = tmp_path / ".cmd.executing"
    path.write_text(body, encoding="utf-8")
    assert _Service(alive=False)._execution_owner_definitely_gone(path) is False


def test_an_unreadable_execution_claim_fails_closed(tmp_path):
    assert _Service(alive=False)._execution_owner_definitely_gone(
        tmp_path / "never-written") is False


def test_neither_carrier_re_derives_the_decision():
    import inspect

    for name in ("_claim_child_definitely_gone", "_claim_child_exactly_alive",
                 "_execution_owner_definitely_gone", "_execution_owner_exactly_alive"):
        body = inspect.getsource(getattr(RunCommandService, name))
        assert "_owner_definitely_gone" in body or "_owner_exactly_alive" in body, name
        assert "_process_identity_proves_reuse" not in body, f"{name} re-derives the reuse check"
    source = inspect.getsource(RunCommandService)
    assert source.count("_process_identity_proves_reuse(") == 1
