"""Seven per-call receipts that described a DIFFERENT call than the one they were stamped on.

Each is the same shape — a fact recorded in one place whose truth lives in another — and each was
silent, because every one of these channels has a FALSY default that reads as an ordinary answer:
"the session was not cut off", "no rule was declared", "no prefix covers this URL". The tests below
drive the property rather than pinning the fix's own text, because a source pin here is one comment
away from vacuous (CLAUDE.md).
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

from looplab.agents.roles import DEVELOPER_OUTPUT_ATTRS
from looplab.core.errors import BudgetExceeded
from looplab.engine.node_build import NodeBuildMixin


# --------------------------------------------------------------- the read-only proxy's own dict

class _Inner:
    def __init__(self):
        self.last_footprint = None
        self.last_files = {}
        self.last_deleted = []

    def implement(self, idea):
        self.last_footprint = {"gpus": 2}
        return "code"


class _ReadOnlyProxy:
    """`search/foresight.py::ForesightPanelResearcher`'s shape: `__getattr__`, no `__setattr__`."""

    def __init__(self, base):
        object.__setattr__(self, "base", base)

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "base"), name)


def test_clearing_a_footprint_through_a_read_only_proxy_does_not_shadow_the_real_one():
    """Under the SHIPPED defaults the engine's `developer` IS such a proxy.

    `unified_agent` + `foresight` + `foresight_panel=2` make `cli/__init__.py` hand the engine one
    `ForesightPanelResearcher` as both roles. `_reset_developer_footprint` walked wrappers by
    `inner`/`developer`/`fallback` — `base` was not among them — and cleared through `hasattr`,
    which on a `__getattr__` proxy resolves to the INNER agent and then writes None into the
    PROXY's own `__dict__`. From the second build on, that shadow was permanent: the envelope read
    None every time, `_finalize_developer_footprint` fell back to the Researcher's proposal, and a
    build that RAISED its own resource estimate was scheduled at the old one.
    """
    inner = _ReadOnlyProxy(_Inner())

    NodeBuildMixin._reset_developer_footprint(inner)
    inner.implement(None)
    first = NodeBuildMixin._capture_developer_result(inner, "code")
    assert first.last_footprint == {"gpus": 2}

    # THE SECOND BUILD is where the shadow used to land — same clear, same call, same read.
    NodeBuildMixin._reset_developer_footprint(inner)
    assert "last_footprint" not in vars(inner), "the clear created the attribute on the PROXY"
    inner.implement(None)
    second = NodeBuildMixin._capture_developer_result(inner, "code")
    assert second.last_footprint == {"gpus": 2}, "the proxy shadowed the inner agent's footprint"

    # …and the clear still REACHES the inner agent, which is the whole point of the walk.
    NodeBuildMixin._reset_developer_footprint(inner)
    assert object.__getattribute__(inner, "base").last_footprint is None


# ------------------------------------------------------------------ the declared failure reason

def test_a_declared_failure_reason_travels_on_the_staged_path_too():
    """The bridge's own word, through the manifest every repo task actually runs.

    `benchmarks/algotune/looplab_eval.py` prints `{"looplab_failure_reason": "rules_violation"}`
    and exits 2, and `make_task.py` emits a `stages:` manifest — so the STAGED path is the shipped
    one. It returned at the failed stage with `declared_reason` unset (only the all-stages-passed
    tail ever read it), `triage._failure_reason` fell through to `exit_code != 0` and answered
    `crash`, which is REPAIRABLE: a paid triage judge plus `inline_repair_attempts` Developer
    repairs per node, spent trying to fix a candidate the arena refused before importing it.
    """
    from looplab.engine import triage
    from looplab.runtime.command_eval import run_command_eval

    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp)
        (wd / "bridge.py").write_text(
            "import json, sys\n"
            'print(json.dumps({"looplab_failure_reason": "rules_violation"}))\n'
            "sys.exit(2)\n", encoding="utf-8")
        cmd = [sys.executable, "bridge.py"]
        metric = {"source": "stdout_json", "key": "score"}
        staged = run_command_eval(cmd, str(wd), 60.0, metric,
                                  stages=[{"name": "score", "command": cmd}])
        single = run_command_eval(cmd, str(wd), 60.0, metric)

    # The two paths answer the SAME word — which is the property, not the word itself.
    assert staged.declared_reason == single.declared_reason == "rules_violation"
    assert triage._failure_reason(staged) == triage._failure_reason(single) == "rules_violation"
    from looplab.core.models import NON_REPAIRABLE_REASONS, REPAIRABLE_REASONS
    assert triage._failure_reason(staged) in NON_REPAIRABLE_REASONS
    assert triage._failure_reason(staged) not in REPAIRABLE_REASONS


def test_a_reason_a_PASSING_stage_declared_is_not_stamped_on_a_later_failure():
    """The funnel may only read the FAILING stage's own words.

    `_EvalRun.out` is overwritten when a stage's command runs, and three early returns fire BEFORE
    that — the host-scorer subject expansion, the `needs` input contract, and the declared-environment
    refusal. At those, `out` still holds the stdout of the stage that PASSED, so a funnel keyed on
    `early.stdout` stamped that stage's declared reason onto a later stage's unrelated failure.

    That is worse than the missing reason it replaced. `triage._failure_reason` reads
    `declared_reason` ABOVE the exit code and above the stage row, so an engine-MEASURED,
    REPAIRABLE `needs_failed` was reclassified as a non-repairable `rules_violation`: the node was
    abandoned instead of repaired, and `failure_diagnosis` recorded the terminal as `declared` when
    the engine had measured it. It also widens the forgeable surface — the stdout in `out` there
    belongs to an AGENT-declared stage, where the pre-funnel read took the last stage's, which for a
    repo task is the engine's own host `score` stage.

    MUTATION: drop the `out_is_this_stage` test from the funnel -> this is red.
    """
    from looplab.engine import triage
    from looplab.runtime.command_eval import run_command_eval

    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp)
        (wd / "prep.py").write_text(
            "import json\n"
            'print("prep ok")\n'
            'print(json.dumps({"looplab_failure_reason": "rules_violation"}))\n', encoding="utf-8")
        (wd / "train.py").write_text('print("train ran")\n', encoding="utf-8")
        res = run_command_eval(
            [sys.executable, "prep.py"], str(wd), 60.0, {"source": "stdout_json", "key": "score"},
            stages=[{"name": "prep", "command": [sys.executable, "prep.py"]},
                    # fails its INPUT CONTRACT, so its command never runs and `out` is still prep's
                    {"name": "train", "command": [sys.executable, "train.py"],
                     "needs": ["data/ready.pt"]}])

    assert res.failed_stage == "train"
    assert "rules_violation" in res.stdout, (
        "fixture: the passing stage's declaration must still be in the carried stdout, or this "
        "test would pass for the wrong reason")
    assert res.declared_reason is None, "a passing stage's reason was stamped on another's failure"
    assert triage._failure_reason(res) == "needs_failed"
    from looplab.core.models import REPAIRABLE_REASONS
    assert triage._failure_reason(res) in REPAIRABLE_REASONS, (
        "an engine-measured input-contract failure must stay repairable")


# --------------------------------------------------------------------- the run's own crash text

def test_the_run_level_stop_detail_is_redacted_before_it_is_published():
    """`stop_detail` folds `run_finished.error`, which `cli/run_cmds.py` writes as a raw
    `str(exc)[:500]` — the one persisted tail that never passes `Engine._redact`.

    `state_payload` feeds the token-less `/state` GET, the headerless SSE stream and a `review`
    share link, and `_public_state_value` only DROPS the keys named in `_PUBLIC_STATE_RAW_KEYS`
    (`stop_detail` is not one) and gives every other string `entropy=False`. Its node-level twin
    two lines away gets the entropy pass and a 160-char cap; this one got neither.
    """
    from looplab.serve.appstate import AppState
    from looplab.serve.jobs import JobRegistry
    from looplab.serve.projects import ProjectStore
    from looplab.serve.settings_store import SettingsStore

    secret = "sk-" + "A7b9Qx2Lm4Zp8Rt6Vw1Ky3Nc5Hd0Jf" * 2
    with tempfile.TemporaryDirectory() as tmp:
        run = Path(tmp) / "run"
        run.mkdir()
        rows = [
            {"v": 1, "seq": 0, "ts": 1.0, "type": "run_started", "data": {"run_id": "r"}},
            {"v": 1, "seq": 1, "ts": 2.0, "type": "run_finished",
             "data": {"reason": "error", "error": f"RuntimeError: provider rejected {secret}"}},
        ]
        (run / "events.jsonl").write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
        root = Path(tmp)
        srv = AppState(root=root, projects=ProjectStore(root), settings=SettingsStore(root),
                       jobs=JobRegistry())
        payload = srv.state_payload(run)

    detail = (payload.get("state") or {}).get("stop_detail") or ""
    assert detail, "the stop detail must still be published — this is a redaction, not a removal"
    assert secret not in detail, detail
    assert len(detail) <= 160, len(detail)


# ------------------------------------------------------------------------- the hard budget stop

def test_the_litellm_backend_lets_the_run_budget_stop_through():
    """`BudgetExceeded` is raised from INSIDE the permit (`llm_broker.borrow()` reserves against
    `RunBudget` before queueing), and `_completion`'s blind handler normalised it to `LLMError`.

    The role layer's documented `except LLMError` retry+fallback then treats an exhausted ceiling
    as a bad response and DEGRADES around it, and `tool_loop.resilient`'s `except BudgetExceeded:
    raise` funnel never sees it — because it is no longer one. CLAUDE.md states the rule for every
    blind handler around a paid call in the run path.
    """
    from looplab.core.llm import LLMError, LiteLLMClient

    client = LiteLLMClient.__new__(LiteLLMClient)
    client.model = "gpt-x"

    class _Boom:
        @staticmethod
        def completion(**kwargs):
            raise BudgetExceeded("run budget exhausted")

    client._litellm = lambda: _Boom
    client._model_for_call = lambda: "gpt-x"

    with pytest.raises(BudgetExceeded):
        client._completion(messages=[])

    # …and an ordinary provider fault is still normalised, which is what the handler is for.
    class _Bad:
        @staticmethod
        def completion(**kwargs):
            raise ValueError("malformed response")

    client._litellm = lambda: _Bad
    with pytest.raises(LLMError):
        client._completion(messages=[])


# ------------------------------------------------------------------ the facade's propose receipt

def test_a_propose_that_raises_does_not_leave_the_previous_call_s_receipt_on_the_facade():
    """The four code stages were moved into `try/finally` for exactly this reason and `propose` was
    not: a delegate that RAISES must not leave the facade mirroring the call before it.

    Without the `finally`, a transport failure that escapes the inner `resilient` kept node N-1's
    cutoff, and the orchestrator logged node N's proposal as "cut short by its budget … treat it as
    TRUNCATED" for a proposal that had no cutoff — the cross-call misattribution the role-scoped
    name was introduced to end, one call later.
    """
    from looplab.agents.unified_agent import UnifiedAgent

    facade = UnifiedAgent.__new__(UnifiedAgent)

    class _Inner:
        last_budget_exhausted = "turns"

        def propose(self, state, parent):
            return "idea-1"

    inner = _Inner()
    facade.researcher = inner
    facade.last_propose_budget_exhausted = ""
    facade.propose(None, None)
    assert facade.last_propose_budget_exhausted == "turns"

    inner.last_budget_exhausted = ""            # the NEXT call was not cut off…
    inner.propose = lambda state, parent: (_ for _ in ()).throw(RuntimeError("provider down"))
    with pytest.raises(RuntimeError):
        facade.propose(None, None)
    assert facade.last_propose_budget_exhausted == "", "the facade kept the PREVIOUS call's receipt"


# --------------------------------------------------------------- every registered channel, at all

def test_a_best_of_n_pick_carries_the_whole_registry_of_the_call_it_chose():
    """`BestOfNDeveloper`'s N>1 path set three of the ten registered channels and called
    `_sync_audit` on neither of its shipping branches, so `_capture_developer_result` — which reads
    every member off the ACTIVE developer, and under the shipped default that is this wrapper —
    recorded the other seven as their FALSY defaults on every best-of-N node.

    And a plain `_sync_audit()` here would be wrong in the other direction: N inner calls each
    overwrite the inner's channels, so a read AFTER the loop describes the LAST candidate. The
    receipt has to be snapshotted PER CANDIDATE and applied for the one that was chosen.
    """
    from looplab.search.best_of_n import BestOfNDeveloper

    class _Varying:
        """Two candidates: the second scores worse and is cut off; the first ships."""

        def __init__(self):
            self.calls = 0
            self.last_files, self.last_deleted, self.last_footprint = {}, [], None
            self.last_budget_exhausted = ""
            self.last_edit_calls = 0
            self.last_rollback_stage = ""

        def implement(self, idea):
            self.calls += 1
            if self.calls == 1:
                self.last_budget_exhausted, self.last_edit_calls = "", 7
                self.last_rollback_stage = ""
                return "def solve():\n    return 1\n"
            self.last_budget_exhausted, self.last_edit_calls = "time", 1
            self.last_rollback_stage = "train"
            return "x"                       # scores worse: not the pick

    dev = BestOfNDeveloper(_Varying(), n=2)
    dev.implement(object())

    got = NodeBuildMixin._capture_developer_result(dev, "code")
    assert got.last_edit_calls == 7, "the envelope took the LAST candidate's edit count"
    assert got.last_budget_exhausted == "", "a candidate that was not chosen supplied the receipt"
    assert got.last_rollback_stage == ""
    # Every registered channel is present on the wrapper, not just the three it owns explicitly.
    for attr in DEVELOPER_OUTPUT_ATTRS:
        assert hasattr(dev, attr), attr
