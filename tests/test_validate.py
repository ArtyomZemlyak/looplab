"""External-agent validator (ADR-7): static + process checks (validate.py) and the
ValidatingDeveloper wrapper (retry-with-feedback + fallback)."""
from __future__ import annotations

from looplab.models import Idea
from looplab.roles import ValidatingDeveloper
from looplab.validate import AgentRun, validate_agent_code

_SEED = 'import json\nprint(json.dumps({"metric": 0.0}))\n'
_GOOD = 'import json\nprint(json.dumps({"metric": 42.0}))\n'


# --------------------------- validate_agent_code -----------------------------

def test_good_code_passes():
    rep = validate_agent_code(_GOOD, seed=_SEED, run=AgentRun(exit_code=0))
    assert rep.ok
    assert not rep.failures()


def test_no_op_seed_is_rejected():
    rep = validate_agent_code(_SEED, seed=_SEED, run=AgentRun(exit_code=0))
    assert not rep.ok
    assert any(c.name == "modified_seed" for c in rep.failures())


def test_syntax_error_is_rejected():
    rep = validate_agent_code("def (:\n", seed=_SEED, run=AgentRun(exit_code=0))
    assert not rep.ok
    assert any(c.name == "parses" for c in rep.failures())


def test_missing_binary_is_rejected():
    rep = validate_agent_code(_SEED, seed=_SEED, run=AgentRun(launched=False))
    assert not rep.ok
    assert any(c.name == "agent_launched" for c in rep.failures())


def test_timeout_is_rejected():
    rep = validate_agent_code(_GOOD, seed=_SEED, run=AgentRun(timed_out=True))
    assert not rep.ok
    assert any(c.name == "agent_not_timed_out" for c in rep.failures())


def test_edit_in_surface_check():
    bad = validate_agent_code(_GOOD, seed=_SEED, run=AgentRun(exit_code=0),
                              patch={"ok": False, "paths": ["a.py", "../e.py"],
                                     "rejected": ["../e.py"]})
    assert not bad.ok and any(c.name == "edit_in_surface" for c in bad.failures())
    good = validate_agent_code(_GOOD, seed=_SEED, run=AgentRun(exit_code=0),
                               patch={"ok": True, "paths": ["solution.py"], "rejected": []})
    assert good.ok and any(c.name == "edit_in_surface" and c.ok for c in good.checks)


def test_nonzero_exit_is_only_a_warning():
    # exit!=0 is advisory: a valid edit can land even when the agent exits non-zero.
    rep = validate_agent_code(_GOOD, seed=_SEED, run=AgentRun(exit_code=1))
    assert rep.ok
    assert any(c.name == "agent_exit_ok" and not c.ok and c.severity == "warn"
               for c in rep.checks)


def test_summary_is_json_serializable():
    import json
    rep = validate_agent_code(_GOOD, seed=_SEED, run=AgentRun(exit_code=0))
    json.dumps(rep.summary())  # must not raise
    assert rep.summary()["ok"] is True


# --------------------------- ValidatingDeveloper -----------------------------

class _FakeAgent:
    """Stand-in for CliAgentDeveloper: returns a scripted sequence of outputs and
    exposes the last_run/last_seed signal the validator reads."""
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0
        self.last_seed = _SEED
        self.last_run = AgentRun(exit_code=0)
        self.brief = "do it"

    def implement(self, idea: Idea) -> str:
        out = self.outputs[min(self.calls, len(self.outputs) - 1)]
        self.calls += 1
        return out


class _FakeFallback:
    def __init__(self):
        self.called = False

    def implement(self, idea: Idea) -> str:
        self.called = True
        return _GOOD


def test_validator_forwards_patch_and_multifile_from_inner():
    agent = _FakeAgent([_GOOD])
    agent.last_patch = {"ok": True, "paths": ["solution.py", "helper.py"], "rejected": []}
    agent.last_files = {"solution.py": _GOOD, "helper.py": "x = 1\n"}
    dev = ValidatingDeveloper(agent, max_retries=0)
    assert dev.implement(Idea(operator="draft")) == _GOOD
    assert dev.last_files == {"solution.py": _GOOD, "helper.py": "x = 1\n"}   # forwarded
    assert any(c.name == "edit_in_surface" and c.ok for c in dev.last_report.checks)


def test_passes_through_valid_output_without_retry():
    agent = _FakeAgent([_GOOD])
    dev = ValidatingDeveloper(agent, max_retries=2)
    assert dev.implement(Idea(operator="draft")) == _GOOD
    assert agent.calls == 1
    assert dev.last_report.ok


def test_retries_with_feedback_then_succeeds():
    agent = _FakeAgent([_SEED, _GOOD])      # first no-op, then a real edit
    dev = ValidatingDeveloper(agent, max_retries=2)
    code = dev.implement(Idea(operator="draft", rationale="solve"))
    assert code == _GOOD
    assert agent.calls == 2
    assert dev.last_report.ok


def test_falls_back_when_agent_keeps_failing():
    agent = _FakeAgent([_SEED])             # always no-op
    fb = _FakeFallback()
    dev = ValidatingDeveloper(agent, fallback=fb, max_retries=1)
    code = dev.implement(Idea(operator="draft"))
    assert code == _GOOD and fb.called
    assert agent.calls == 2                 # initial + 1 retry, then fallback
    # last_report audits the AGENT (it failed); the shipped fallback code is valid.
    assert not dev.last_report.ok
    assert dev.last_fell_back and dev.last_shipped_ok
    assert dev.audit_extra() == {"attempts": 2, "fell_back": True, "shipped_ok": True}


def test_records_failure_when_no_fallback():
    agent = _FakeAgent([_SEED])
    dev = ValidatingDeveloper(agent, max_retries=0)
    dev.implement(Idea(operator="draft"))
    assert dev.last_report is not None and not dev.last_report.ok
    assert not dev.last_fell_back and not dev.last_shipped_ok


class _FakeRepairAgent(_FakeAgent):
    def repair(self, idea, code, error):
        return self.implement(idea)          # scripted outputs, same as implement


class _FakeRepairFallback:
    def __init__(self):
        self.implement_called = self.repair_called = False

    def implement(self, idea):
        self.implement_called = True
        return _GOOD

    def repair(self, idea, code, error):
        self.repair_called = True
        return _GOOD


def test_repair_fallback_uses_fallback_repair_not_implement():
    # On a repair that exhausts the agent, the fallback's REPAIR must run (preserving the
    # error-feedback), not its implement (review finding: lost debug context).
    agent = _FakeRepairAgent([_SEED])        # repair always no-ops
    fb = _FakeRepairFallback()
    dev = ValidatingDeveloper(agent, fallback=fb, max_retries=0)
    code = dev.repair(Idea(operator="debug"), "broken", "Boom: traceback")
    assert code == _GOOD
    assert fb.repair_called and not fb.implement_called
    assert dev.last_fell_back


def test_brief_and_prompts_forward_to_inner():
    agent = _FakeAgent([_GOOD])
    dev = ValidatingDeveloper(agent)
    assert dev.brief == "do it"
    dev.prompts = "PSTORE"
    assert agent.prompts == "PSTORE"


# --------------------- engine integration: audit trail -----------------------

def test_engine_emits_agent_validated_audit_trail(tmp_path):
    """A ValidatingDeveloper in the loop produces an `agent_validated` event per node,
    and the fold attaches the verdict to each node (audit only — selection unaffected)."""
    from pathlib import Path

    import anyio

    from looplab.eventstore import EventStore
    from looplab.orchestrator import Engine
    from looplab.policy import GreedyTree
    from looplab.replay import fold
    from looplab.sandbox import SubprocessSandbox
    from looplab.toytask import ToyTask

    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    researcher, developer = task.build_roles()
    engine = Engine(
        tmp_path / "run", task=task, researcher=researcher,
        developer=ValidatingDeveloper(developer),       # wrap the toy developer
        sandbox=SubprocessSandbox(),
        policy=GreedyTree(n_seeds=3, max_nodes=6), max_parallel=4)
    state = anyio.run(engine.run)

    assert state.finished and len(state.nodes) == 6
    # One agent_validated event per created node.
    events = list(EventStore(tmp_path / "run" / "events.jsonl").read_all())
    validated = [e for e in events if e.type == "agent_validated"]
    assert len(validated) == 6
    # The fold attaches the verdict to every node and search still worked.
    refolded = fold(events)
    assert all(n.agent_report is not None and n.agent_report["ok"]
               for n in refolded.nodes.values())
    assert refolded.best() is not None


def test_ablation_probes_bypass_the_validator(tmp_path):
    """Ablation probes (I7) must use the RAW inner developer, not the ValidatingDeveloper
    — otherwise each probe runs the retry/fallback loop, corrupting impact measurement
    and multiplying expensive agent calls (review finding B1)."""
    from pathlib import Path

    from looplab.orchestrator import Engine
    from looplab.policy import GreedyTree
    from looplab.sandbox import SubprocessSandbox
    from looplab.toytask import ToyTask

    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    researcher, inner = task.build_roles()
    wrapped = ValidatingDeveloper(inner)
    engine = Engine(tmp_path / "run", task=task, researcher=researcher,
                    developer=wrapped, sandbox=SubprocessSandbox(),
                    policy=GreedyTree(n_seeds=3, max_nodes=6))
    assert engine._probe_developer is inner          # bypasses the wrapper

    engine_plain = Engine(tmp_path / "run2", task=task, researcher=researcher,
                          developer=inner, sandbox=SubprocessSandbox(),
                          policy=GreedyTree(n_seeds=3, max_nodes=6))
    assert engine_plain._probe_developer is inner     # plain developer -> itself
