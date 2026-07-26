"""H3 per-role model presets: Researcher and Developer on different models/endpoints."""
from __future__ import annotations

import json
from pathlib import Path

from looplab.core.config import Settings
from looplab.adapters.tasks import load_task, make_roles

ROOT = Path(__file__).resolve().parents[1]


def _client_model(role):
    c = getattr(role, "client", None)
    return getattr(c, "model", None)


def _client_temp(role):
    c = getattr(role, "client", None)
    return getattr(c, "temperature", None)


def test_per_role_models_point_at_distinct_endpoints():
    task = load_task(ROOT / "examples" / "code_regression_task.json")
    s = Settings(backend="llm", llm_model="shared-model", unified_agent=False,
                 researcher_model="fast-researcher", developer_model="coder-30b")
    researcher, developer = make_roles(task, s)
    assert _client_model(researcher) == "fast-researcher"
    assert _client_model(developer) == "coder-30b"


def test_per_role_unset_falls_back_to_shared():
    task = load_task(ROOT / "examples" / "code_regression_task.json")
    s = Settings(backend="llm", llm_model="shared-model", unified_agent=False)
    researcher, developer = make_roles(task, s)
    assert _client_model(researcher) == "shared-model"
    assert _client_model(developer) == "shared-model"


def test_settings_accepts_per_role_base_urls():
    s = Settings(researcher_base_url="http://a/v1", developer_base_url="http://b/v1")
    assert s.researcher_base_url == "http://a/v1" and s.developer_base_url == "http://b/v1"


def test_per_role_temperature_overrides_shared(  # §4.1
):
    task = load_task(ROOT / "examples" / "code_regression_task.json")
    s = Settings(backend="llm", llm_model="shared-model", llm_temperature=0.6, unified_agent=False,
                 researcher_temperature=0.9, developer_temperature=0.1)
    researcher, developer = make_roles(task, s)
    assert _client_temp(researcher) == 0.9        # breadth
    assert _client_temp(developer) == 0.1         # determinism
    # the model stays SHARED (a temperature-only override must not lose the shared model/endpoint)
    assert _client_model(researcher) == "shared-model" and _client_model(developer) == "shared-model"


def test_per_role_temperature_unset_falls_back_to_shared():
    task = load_task(ROOT / "examples" / "code_regression_task.json")
    s = Settings(backend="llm", llm_model="shared-model", llm_temperature=0.55, unified_agent=False)
    researcher, developer = make_roles(task, s)
    assert _client_temp(researcher) == 0.55 and _client_temp(developer) == 0.55


def test_temperature_only_override_with_a_per_role_model():
    # a role can combine a distinct model AND a distinct temperature
    task = load_task(ROOT / "examples" / "code_regression_task.json")
    s = Settings(backend="llm", llm_model="shared", llm_temperature=0.6, unified_agent=False,
                 developer_model="coder", developer_temperature=0.0)
    _r, developer = make_roles(task, s)
    assert _client_model(developer) == "coder" and _client_temp(developer) == 0.0


def test_strategist_temperature_threaded_in_unified_mode():
    # mega-review (2 votes): the DEFAULT unified path built its strategy client via client_for(), which
    # ignored strategist_temperature — the knob was a silent no-op. It must be honored now.
    from looplab.adapters.tasks import build_unified_agent
    task = load_task(ROOT / "examples" / "code_regression_task.json")
    s = Settings(backend="llm", llm_temperature=0.6, unified_agent=True,
                 strategist_backend="llm", strategist_temperature=0.15)
    agent = build_unified_agent(task, s)
    assert getattr(agent.strategist, "client", None) is not None
    assert agent.strategist.client.temperature == 0.15


# --------------------------------------------------------------------------- #
# Stage rebinding in unified mode: implement and repair are DIFFERENT stages
# --------------------------------------------------------------------------- #
# `agent_stage_models` promises a model per stage, but implement and repair used to be pointed at
# one mutable Developer object, so the second rebind overwrote the first. Both stages ended up on
# whatever `repair` said — including the case where the operator only set `repair` and the untouched
# implement stage silently moved with it. The rebind also rebuilt the client without a temperature,
# resetting the stage to `llm_temperature` even with `developer_temperature` set.

def _unified(**kw):
    from looplab.adapters.tasks import build_unified_agent
    task = load_task(ROOT / "examples" / "code_regression_task.json")
    return build_unified_agent(task, Settings(backend="llm", unified_agent=True, **kw))


def test_implement_and_repair_stage_models_do_not_overwrite_each_other():
    agent = _unified(llm_model="shared",
                     agent_stage_models={"implement": "coder-A", "repair": "fixer-B"})
    assert _client_model(agent.developer) == "coder-A"          # was "fixer-B" (clobbered)
    assert _client_model(agent.repair_developer) == "fixer-B"
    assert agent.repair_developer is not agent.developer        # genuinely separate objects


def test_repair_only_stage_model_leaves_the_implement_stage_alone():
    agent = _unified(llm_model="shared", developer_model="coder-30b",
                     agent_stage_models={"repair": "fixer-B"})
    assert _client_model(agent.developer) == "coder-30b"        # was dragged to "fixer-B"
    assert _client_model(agent.repair_developer) == "fixer-B"


def test_repair_runs_on_its_own_developer_and_reports_that_developers_files():
    """The orchestrator reads last_files off the facade right after the call and writes them to the
    node — so a repair that ran on the repair Developer must not report the implement one's files."""
    from looplab.agents.unified_agent import UnifiedAgent

    class _Dev:
        def __init__(self, tag):
            self.tag, self.inner, self.last_deleted, self.last_footprint = tag, None, [], None
            self.last_files: dict = {}
        def implement(self, idea):
            self.last_files = {"main.py": f"# {self.tag} implement"}
            return self.last_files["main.py"]
        def repair(self, idea, code, error):
            self.last_files = {"main.py": f"# {self.tag} repair"}
            return self.last_files["main.py"]

    impl, rep = _Dev("impl"), _Dev("rep")
    agent = UnifiedAgent(researcher=object(), developer=impl, repair_developer=rep)
    assert agent.implement(object()) == "# impl implement"
    assert agent.last_files == {"main.py": "# impl implement"}
    assert agent.repair(object(), "", "boom") == "# rep repair"
    assert agent.last_files == {"main.py": "# rep repair"}      # was the implement developer's


def test_equal_stage_targets_keep_the_single_developer_path():
    # Same model for both stages -> no second Developer at all (the historical object graph).
    agent = _unified(llm_model="shared",
                     agent_stage_models={"implement": "same", "repair": "same"})
    assert agent.repair_developer is None
    assert _client_model(agent.developer) == "same"
    # ...and so does the default posture with no stage maps at all.
    assert _unified(llm_model="shared").repair_developer is None


def test_stage_rebind_keeps_the_per_role_temperature_and_model():
    # Rebinding a stage used to rebuild the client from scratch: the per-role temperature was lost,
    # and a url-only override also threw away `developer_model`.
    agent = _unified(llm_model="shared", llm_temperature=0.6, developer_model="coder-30b",
                     developer_temperature=0.05,
                     agent_stage_base_urls={"implement": "http://coder/v1"})
    assert _client_temp(agent.developer) == 0.05               # was 0.6 (the shared default)
    assert _client_model(agent.developer) == "coder-30b"       # was "shared"
    assert agent.developer.client.base_url == "http://coder/v1"


def test_repair_stage_client_is_billed():
    """A second Developer means a second CostAccountant — the roll-up walk must reach it, or every
    repair call is spent and never appears in the run's cost ledger."""
    from looplab.engine.costs import _CHILD_ATTRS
    assert "repair_developer" in _CHILD_ATTRS
    agent = _unified(llm_model="shared",
                     agent_stage_models={"implement": "coder-A", "repair": "fixer-B"})
    assert getattr(agent.repair_developer.client, "accountant", None) is not None
