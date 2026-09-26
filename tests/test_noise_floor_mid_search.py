"""The eval noise floor, measured MID-SEARCH (doc 67 67.1a, `Settings.noise_floor_mid_search`).

The proposal board's SUPPORT token holds a single-run gain to the run's measured noise floor
(`events/card_ledger.py::verdict_support`), but the floor was written only by the empty-action
ladder at the END of the search, so the search itself never read one: a critic's three toy runs
with both instruments on printed 18 SUPPORT tokens, all 18 `single_run`. Under the flag the same
pass runs once, at the first creation boundary with a champion and no evaluation in flight.

Driven through the real CLI on real toy runs (Card-driven, the default) and read back off the log.
"""
from __future__ import annotations

from typer.testing import CliRunner

from looplab.cli import app
from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings, settings_from_snapshot
from looplab.engine.noise_floor import NoiseFloorMixin
from looplab.engine.options import EngineOptions
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from tests.factories import make_engine

_RUN = ["run", "--no-genesis", "--kind", "quadratic", "--goal", "min (x-3)^2", "--direction", "min",
        "--backend", "toy", "--max-nodes", "6"]


def _run(tmp_path, monkeypatch, *, mid_search: bool):
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("LOOPLAB_KNOWLEDGE_DIR", str(tmp_path / "kn"))
    monkeypatch.setenv("LOOPLAB_EVAL_NOISE_SEEDS", "3")
    monkeypatch.setenv("LOOPLAB_NOISE_FLOOR_MID_SEARCH", "1" if mid_search else "0")
    rd = tmp_path / "run"
    out = CliRunner().invoke(app, [*_RUN, "--out", str(rd)])
    assert out.exit_code == 0, out.output
    return EventStore(rd / "events.jsonl").read_all()


def test_on_the_floor_lands_mid_search_where_the_search_reads_it(tmp_path, monkeypatch):
    events = _run(tmp_path, monkeypatch, mid_search=True)
    floors = [e for e in events if e.type == "eval_noise_floor"]
    assert len(floors) == 1, "the end ladder did not measure a second time"
    floor = floors[0]
    assert floor.data.get("mid_search") is True and floor.data["n"] == 3
    # MID-search: nodes are still created after it, and the fold every later decision reads — the
    # prefix just before the next node is created — carries the floor.
    later = [e for e in events if e.type == "node_created" and e.seq > floor.seq]
    assert later, "the floor landed after the last node was created"
    prefix = fold([e for e in events if e.seq < later[0].seq])
    assert prefix.eval_noise_floor is not None and prefix.eval_noise_floor.get("mid_search") is True
    # It measured the champion of its moment, on that champion's own lifecycle.
    before = fold([e for e in events if e.seq < floor.seq])
    assert floor.data["node_id"] == before.best().id
    assert floor.data["generation"] == before.best().attempt


def test_off_the_floor_is_measured_at_the_end_as_before(tmp_path, monkeypatch):
    events = _run(tmp_path, monkeypatch, mid_search=False)
    floors = [e for e in events if e.type == "eval_noise_floor"]
    assert len(floors) == 1 and "mid_search" not in floors[0].data
    last_node = max(e.seq for e in events if e.type in ("node_created", "node_evaluated"))
    assert floors[0].seq > last_node, "OFF, the floor waits for the empty-action ladder"


def test_a_mid_search_pass_that_counted_nothing_leaves_the_end_pass_due(tmp_path, monkeypatch):
    """Every mid-search repeat abstains (the device was busy for its bounded minute): that pass
    records n=0, and the end ladder measures again rather than keeping an empty floor."""
    real = NoiseFloorMixin._run_noise_seed
    calls = []

    async def abstain_first_pass(self, nd, s, profile=None):
        calls.append(s)
        if len(calls) <= 3:
            return None                      # the bounded resource wait ran out: nothing recorded
        return await real(self, nd, s, profile)

    monkeypatch.setattr(NoiseFloorMixin, "_run_noise_seed", abstain_first_pass)
    events = _run(tmp_path, monkeypatch, mid_search=True)
    floors = [e.data for e in events if e.type == "eval_noise_floor"]
    assert [(f.get("mid_search", False), f["n"]) for f in floors] == [(True, 0), (False, 3)], floors
    assert fold(events).eval_noise_floor["n"] == 3, "the end pass's row is the one that stands"


def _champion_state(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft", "files": {},
                                  "idea": {"operator": "draft", "params": {"x": 1.0},
                                           "rationale": "r"}, "code": "pass\n"})
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 4.0, "violations": []})
    return store


def test_the_mid_search_question_and_the_end_question(tmp_path):
    """`_noise_floor_mid_search_due` and `_noise_floor_due` over the REAL fold, row by row."""
    store = _champion_state(tmp_path)
    on = make_engine(tmp_path / "on", eval_noise_seeds=3, noise_floor_mid_search=True)
    state = fold(store.read_all())
    assert on._noise_floor_mid_search_due(state) and on._noise_floor_due(state)
    on._eval_inflight = {(0, 0)}
    assert not on._noise_floor_mid_search_due(state), "an evaluation is in flight: wait for it"
    on._eval_inflight = set()
    # …nor while a Card build is in flight: the pass's rows would move the log its receipt is fenced
    # on (critic 2026-09-26, driven with speculation on: a build request closed stale).
    building = fold([*store.read_all(), *EventStore(tmp_path / "b.jsonl").read_all()])
    building.buildings = {7: {"parent_ids": [0]}}
    assert not on._noise_floor_mid_search_due(building), "a build is in flight: wait for it"
    assert not make_engine(tmp_path / "off", eval_noise_seeds=3)._noise_floor_mid_search_due(state)
    assert not make_engine(tmp_path / "noseeds",
                           noise_floor_mid_search=True)._noise_floor_mid_search_due(state)
    floor = {"node_id": 0, "generation": 0, "seeds": [0, 1, 2], "metrics": [None, None, None],
             "n": 0, "mean": None, "std": None, "sem": None, "spread": None, "search_metric": 4.0,
             "profile": None}
    store.append("eval_noise_floor", {**floor, "mid_search": True})
    state = fold(store.read_all())
    assert not on._noise_floor_mid_search_due(state), "mid-search is asked once, never retried"
    assert on._noise_floor_due(state), "…and the end ladder owns the retry of an empty pass"
    # …but only while a seed is left to RUN: repeats that ran and failed are recorded (paid for,
    # and charged once per seed by the fold), and the end pass would skip every one of them
    # (critic 2026-09-26, driven: a second empty row, no second measurement).
    for seed in (0, 1, 2):
        store.append("eval_noise_seed", {"node_id": 0, "generation": 0, "seed": seed,
                                         "metric": None, "eval_seconds": 1.0})
    assert not on._noise_floor_due(fold(store.read_all())), "nothing left to run: not due"
    store.append("eval_noise_floor", {**floor, "n": 3, "metrics": [4.0, 4.0, 4.0], "mean": 4.0,
                                      "std": 0.0, "sem": 0.0, "spread": 0.0, "mid_search": True})
    assert not on._noise_floor_due(fold(store.read_all())), "a mid-search pass that counted stands"
    store.append("eval_noise_floor", floor)
    assert not on._noise_floor_due(fold(store.read_all())), "the end pass is the last, whatever n"


def test_the_flag_ships_off_resumes_off_and_is_off_at_every_constructor():
    assert Settings().noise_floor_mid_search is False
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["noise_floor_mid_search"] is False
    legacy = {k: v for k, v in Settings().masked_snapshot().items() if k != "noise_floor_mid_search"}
    assert settings_from_snapshot(legacy).noise_floor_mid_search is False
    assert EngineOptions().noise_floor_mid_search is False
    on = Settings(noise_floor_mid_search=True)
    assert EngineOptions.from_settings(on).noise_floor_mid_search is True
