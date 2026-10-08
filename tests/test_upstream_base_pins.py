"""Which base a node's files are an overlay OF, while the live upstream lane moves the base (doc 73
§2.5), critic 2026-10-08.

A node's `node_created.base_selector` is what `upstream_workspace.py::materialization_plan` merges
FROM onto the run's current base. Three builds named the wrong one:

* a build seeded with a PARENT's files (an improve, an ensemble merge, a node-reset rebuild, an
  ablation's refine_block, an inject on a parent) was bound to — and stamped with — the CURRENT base,
  so the parent's old copy of a file the lane since promoted was taken as written on the new base:
  materialization merged nothing and the promotion was silently reverted. The Developer is now
  pinned to the parent's files base and the child names it;
* an operator inject shipping a FORKED node's files carried no base at all;
* a facade whose repair stage has its own Developer (`UnifiedAgent.repair_developer`) rebound only
  the implement Developer, so a repair ran on the launch base while its stamp said otherwise.
"""
from __future__ import annotations

from looplab.agents.unified_agent import UnifiedAgent
from looplab.core.models import Idea
from looplab.events.replay import fold
from factories import make_engine

_A = {"run_dir": "/r", "event_seq": 1, "digest": "a" * 64}     # the base the parent was built on
_B = {"run_dir": "/r", "event_seq": 9, "digest": "b" * 64}     # the base the live lane advanced to


class _Rebindable:
    """A Developer that owns its base, as `LLMRepoDeveloper` does, and records what it built on."""

    def __init__(self, base=None):
        self.authored_base, self.seen = dict(base or _A), []

    def rebind_base(self, selector=None):
        self.authored_base = dict(selector) if selector is not None else dict(_B)

    def implement(self, idea):
        self.seen.append(("implement", self.authored_base["digest"][:1]))
        return "code"

    def implement_from(self, idea, parent):
        self.seen.append(("implement_from", self.authored_base["digest"][:1]))
        return "code"

    def repair_from(self, idea, node, error):
        self.seen.append(("repair_from", self.authored_base["digest"][:1]))
        return "code"


def _armed(engine):
    engine._upstream_serve.armed = {"mode": "auto", "reason": "", "settings": None,
                                    "stamp": dict(_A), "current": dict(_B)}
    return engine


def _with_parent(engine, files):
    engine.store.append("run_started", {"run_id": "r", "task_id": "toy", "direction": "min"})
    engine.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft", "code": "print(0)",
        "idea": {"operator": "draft", "params": {"x": 1.0}}, "files": files,
        "base_selector": dict(_A)})
    return fold(engine.store.read_all())


def test_a_build_from_a_parents_files_is_pinned_to_the_parents_base(tmp_path):
    engine = _armed(make_engine(tmp_path / "run"))
    state = _with_parent(engine, {"train.py": "print('parent')\n"})
    dev = _Rebindable(_B)
    built = engine._implement_result(Idea(operator="improve", params={}), state.nodes[0],
                                     developer=dev, state=state)
    assert dev.seen == [("implement_from", "a")], "the Developer read the code the parent's files edit"
    assert built.authored_base == _A, "and the child names it, so the next lifecycle merges onto B"
    fresh = engine._implement_result(Idea(operator="draft", params={}), developer=dev, state=state)
    assert dev.seen[-1] == ("implement", "b") and fresh.authored_base == _B, (
        "a build from nothing authors on the engine's current base, as before")


def test_a_parent_with_no_files_pins_nothing(tmp_path):
    engine = _armed(make_engine(tmp_path / "run"))
    state = _with_parent(engine, {})
    dev = _Rebindable(_A)
    built = engine._implement_result(Idea(operator="improve", params={}), state.nodes[0],
                                     developer=dev, state=state)
    assert built.authored_base == _B, "implement_from regenerates from the base: the current one"


def test_an_inject_of_a_forked_nodes_files_names_that_nodes_base(tmp_path):
    engine = _armed(make_engine(tmp_path / "run"))
    _with_parent(engine, {"train.py": "print('parent')\n"})
    engine._create_injected_node({
        "idea": {"operator": "manual", "params": {"x": 0.5}, "rationale": "operator's fork"},
        "parent_id": 0, "code": "print(1)", "files": {"train.py": "print('forked')\n"},
        "forked_from": {"node_id": 0, "generation": 0, "observed_seq": 1}})
    created = [e.data for e in engine.store.read_all() if e.type == "node_created"]
    assert created[-1]["files"] == {"train.py": "print('forked')\n"}
    assert created[-1]["base_selector"] == _A, "the fork's files are an overlay of #0's base"
    engine._create_injected_node({
        "idea": {"operator": "manual", "params": {"x": 0.6}, "rationale": "no fork"},
        "parent_id": None, "code": "print(2)", "files": {"train.py": "print('mine')\n"}})
    assert "base_selector" not in [e.data for e in engine.store.read_all()
                                   if e.type == "node_created"][-1], (
        "supplied files of no stated origin keep the creation prefix, as before")


def test_a_repair_rebinds_the_facades_repair_developer_too(tmp_path):
    engine = _armed(make_engine(tmp_path / "run"))
    implement, repair = _Rebindable(_B), _Rebindable(_B)
    facade = UnifiedAgent(researcher=object(), developer=implement, repair_developer=repair)
    result = engine._run_developer(facade, facade.repair_from, Idea(operator="improve", params={}),
                                   object(), "boom", pinned_base=dict(_A))
    assert repair.seen == [("repair_from", "a")], "the member that REPAIRS read the lifecycle's base"
    assert result.authored_base == _A
    engine._run_developer(facade, facade.implement, Idea(operator="draft", params={}))
    assert implement.seen == [("implement", "b")] and repair.authored_base == _B, (
        "the next build moves every member back onto the engine's current base")
