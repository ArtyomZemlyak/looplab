"""T1 profile preset + T2 trust-gate enforcement (this session).

Covers:
- `profile=thorough` turns the built quality/trust machinery ON, and any explicit setting
  (init-kwarg or LOOPLAB_* env) beats the profile (config-first);
- an unknown profile / bad trust_gate is rejected;
- the pure fold applies the trust gate to best-selection (audit=legacy, gate=exclude-from-win,
  block=also-infeasible), gates ONLY hard cheating/leakage signals (critic stays advisory), is
  order-independent, and an old log with no trust_gate folds to legacy behavior;
- a real toy run records `trust_gate` in `run_started` so replay/resume enforce the same gate.
All offline (no LLM, no network)."""
from __future__ import annotations

import os
from pathlib import Path

import anyio
import orjson

from looplab.core.config import PROFILES, Settings
from looplab.events.eventstore import EventStore
from looplab.core.models import Event
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.events.replay import fold
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


# ---------- T1: profile preset (config-first) ----------

def test_default_profile_leaves_machinery_off():
    s = Settings()
    assert s.profile == "default"
    assert s.confirm_top_k == 0 and s.confirm_seeds == 0
    assert s.novelty_gate is False and s.reward_hack_detect is False
    assert s.trust_gate == "audit"


def test_thorough_profile_turns_machinery_on():
    s = Settings(profile="thorough")
    assert (s.confirm_top_k, s.confirm_seeds) == (3, 3)
    assert s.reward_hack_detect and s.code_leakage_detect and s.critic_check
    # novelty_gate stays OFF even in `thorough`: duplicate-detection is the agentic Researcher's
    # job (reads past experiments + find_analogous_across_runs), not an algorithmic auto-reject —
    # see the PROFILES["thorough"] comment in config.py for the live-node-61 misfire that motivated it.
    assert s.novelty_gate is False
    assert s.trust_gate == "gate"
    assert s.ablate_every == 3 and s.complexity_cue and s.budget_aware and s.failure_reflection
    # every value the profile sets must be a real, reachable field
    for k in PROFILES["thorough"]:
        assert k in Settings.model_fields


def test_explicit_setting_beats_profile():
    s = Settings(profile="thorough", confirm_top_k=1, trust_gate="block")
    assert s.confirm_top_k == 1          # explicit wins
    assert s.trust_gate == "block"       # explicit wins
    assert s.reward_hack_detect is True  # untouched profile field still applies


def test_env_beats_profile(monkeypatch):
    monkeypatch.setenv("LOOPLAB_CONFIRM_TOP_K", "5")
    s = Settings(profile="thorough")
    assert s.confirm_top_k == 5          # env source beats the profile default


def test_unknown_profile_and_bad_gate_rejected():
    import pytest
    with pytest.raises(Exception):
        Settings(profile="nope")
    with pytest.raises(Exception):
        Settings(trust_gate="nonsense")


# ---------- T2: trust-gate enforcement in the pure fold ----------

def _mk(events):
    return [Event(type=t, data=d) for t, d in events]


def _base(trust_gate):
    # node 2 has the better metric but is the "cheater"; node 1 is the clean runner-up.
    return [
        ("run_started", {"run_id": "r", "task_id": "t", "goal": "g",
                         "direction": "max", "trust_gate": trust_gate}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "rationale": "a"}, "code": "x"}),
        ("node_evaluated", {"node_id": 1, "metric": 0.90}),
        ("node_created", {"node_id": 2, "operator": "draft",
                          "idea": {"operator": "draft", "rationale": "b"}, "code": "y"}),
        ("node_evaluated", {"node_id": 2, "metric": 0.99}),
    ]


_HACK = ("reward_hack_suspected",
         {"node_id": 2, "signals": [{"signal": "grader_import", "detail": "imports grader"}]})
_CRITIC = ("reward_hack_suspected",
           {"node_id": 2, "signals": [{"signal": "critic:stub", "detail": "looks stubby"}]})


def test_audit_lets_flagged_node_win():
    s = fold(_mk(_base("audit") + [_HACK]))
    assert s.trust_gate == "audit" and s.best_node_id == 2


def test_gate_excludes_flagged_from_winning_but_keeps_it_breedable():
    s = fold(_mk(_base("gate") + [_HACK]))
    assert s.best_node_id == 1
    assert s.nodes[2].feasible is True          # still in the tree; may be repaired/improved


def test_block_also_marks_flagged_infeasible():
    s = fold(_mk(_base("block") + [_HACK]))
    assert s.best_node_id == 1
    assert s.nodes[2].feasible is False
    assert [n.id for n in s.feasible_nodes()] == [1]


def test_critic_only_signal_stays_advisory_even_under_gate():
    s = fold(_mk(_base("gate") + [_CRITIC]))
    assert s.best_node_id == 2                  # critic is a quality hint, not a gate


def test_gate_is_order_independent():
    reordered = ([("run_started", {"run_id": "r", "task_id": "t",
                                    "direction": "max", "trust_gate": "block"}), _HACK]
                 + _base("block")[1:])
    s = fold(_mk(reordered))
    assert s.best_node_id == 1 and s.nodes[2].feasible is False


def test_old_log_without_trust_gate_is_legacy():
    old = ([("run_started", {"run_id": "r", "task_id": "t", "direction": "max"})]
           + _base("audit")[1:] + [_HACK])
    s = fold(_mk(old))
    assert s.trust_gate == "audit" and s.best_node_id == 2


# ---------- T2: a real toy run records the gate for replay ----------

def test_toy_run_records_trust_gate_in_run_started(tmp_path):
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    rd = tmp_path / "run"
    eng = Engine(rd, task=task, researcher=r, developer=d, sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=2, max_nodes=4), trust_gate="block")
    anyio.run(eng.run)
    events = [orjson.loads(l) for l in (rd / "events.jsonl").read_bytes().splitlines()]
    started = next(e for e in events if e["type"] == "run_started")
    assert started["data"]["trust_gate"] == "block"
    # and the folded state reflects it (so a resume enforces the same gate)
    st = fold(EventStore(rd / "events.jsonl").read_all())
    assert st.trust_gate == "block"
