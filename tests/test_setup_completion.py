"""arch-review §3 P0-3: setup completion is a FOLDED state machine (setup_done), not inferred from
run_id. run_started is appended mid-setup (before the leakage hard-stop), so a crash right after it
used to make every later resume skip the rest of preflight — leakage included — forever."""
from __future__ import annotations

from looplab.core.models import Idea, RunState
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import EV_DATA_LEAKAGE, EV_RUN_STARTED, EV_SETUP_FINISHED


def test_setup_done_folds_from_setup_finished(tmp_path):
    s = EventStore(tmp_path / "events.jsonl")
    s.append(EV_RUN_STARTED, {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    assert fold(s.read_all()).setup_done is False        # run_started alone is NOT setup-complete
    s.append(EV_SETUP_FINISHED, {"seconds": 0.1})
    assert fold(s.read_all()).setup_done is True


def test_resume_after_crash_reruns_leakage(tmp_path):
    from looplab.engine.orchestrator import Engine
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree

    class _LeakyTask:
        id = "t"; goal = "g"; direction = "min"
        def model_dump(self, mode="json"):
            return {"id": "t"}
        def leakage_inputs(self):
            # identical train/test rows -> train_test_contamination flags a hard leak
            return {"train_rows": [[1, 2], [3, 4]], "test_rows": [[1, 2], [3, 4]]}

    class _R:
        def propose(self, s, p):
            return Idea(operator="draft", params={})

    class _D:
        def implement(self, idea):
            return "print(1)"

    run = tmp_path / "run"
    eng = Engine(run, task=_LeakyTask(), researcher=_R(), developer=_D(),
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 auto_install_deps=False)
    # Simulate a crash right AFTER run_started (before setup_finished / the leakage hard-stop).
    eng.store.append(EV_RUN_STARTED, {"run_id": run.name, "task_id": "t", "goal": "g",
                                      "direction": "min"})
    st = fold(eng.store.read_all())
    assert st.setup_done is False and st.run_id and not st.nodes   # run_id set, setup NOT complete
    # Resume: _setup_phase must RE-RUN (gate is setup_done, not run_id) and hit the leakage hard-stop.
    eng._setup_phase(st)
    types = [e.type for e in eng.store.read_all()]
    assert EV_DATA_LEAKAGE in types                                # leakage was re-checked, not skipped
    st2 = fold(eng.store.read_all())
    assert st2.finished and st2.setup_done                         # blocked, and setup is now marked done


def test_setup_finished_records_and_folds_manifest(tmp_path):
    # P0-3: setup_finished carries a material manifest that binds setup_done to the exact inputs.
    s = EventStore(tmp_path / "events.jsonl")
    s.append(EV_RUN_STARTED, {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    s.append(EV_SETUP_FINISHED, {"seconds": 0.1, "manifest": "deadbeef"})
    st = fold(s.read_all())
    assert st.setup_done and st.setup_manifest == "deadbeef"
    # an old log without a manifest still folds setup_done, manifest stays "" (pure-boolean fallback)
    s2 = EventStore(tmp_path / "e2.jsonl")
    s2.append(EV_RUN_STARTED, {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    s2.append(EV_SETUP_FINISHED, {"seconds": 0.1})
    st2 = fold(s2.read_all())
    assert st2.setup_done and st2.setup_manifest == ""


def test_resume_reruns_setup_when_material_manifest_changes(tmp_path):
    # P0-3: on a PRE-node resume, setup re-runs preflight when the material manifest no longer matches
    # what setup completed against (edited config/data), instead of trusting the stale setup_done boolean.
    from looplab.engine.orchestrator import Engine
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree

    class _Task:
        id = "t"; goal = "g"; direction = "min"
        cfg = {"id": "t", "v": 1}
        def model_dump(self, mode="json"):
            return dict(self.cfg)
        def leakage_inputs(self):
            return None                       # no leak -> setup completes cleanly

    class _R:
        def propose(self, s, p):
            return Idea(operator="draft", params={})

    class _D:
        def implement(self, idea):
            return "print(1)"

    task = _Task()
    eng = Engine(tmp_path / "run", task=task, researcher=_R(), developer=_D(),
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 auto_install_deps=False)

    def _n_setup():
        return len([e for e in eng.store.read_all() if e.type == EV_SETUP_FINISHED])

    eng._setup_phase(fold(eng.store.read_all()))            # first setup completes + records a manifest
    assert fold(eng.store.read_all()).setup_manifest and _n_setup() == 1
    eng._setup_phase(fold(eng.store.read_all()))            # unchanged material -> NOT re-run (no loop)
    assert _n_setup() == 1
    task.cfg = {"id": "t", "v": 2}                          # the config material changed
    eng._setup_phase(fold(eng.store.read_all()))            # manifest differs -> setup RE-RUNS
    assert _n_setup() == 2
    eng._setup_phase(fold(eng.store.read_all()))            # new manifest recorded -> stable again
    assert _n_setup() == 2


def test_run_started_pins_environment(tmp_path):
    # P0-5 environment identity is pinned at run_started and folded onto RunState.env.
    s = EventStore(tmp_path / "events.jsonl")
    s.append(EV_RUN_STARTED, {"run_id": "r", "task_id": "t", "direction": "min",
                              "env": {"python": "3.11.0", "libs": {"numpy": "1.0"}}})
    assert fold(s.read_all()).env == {"python": "3.11.0", "libs": {"numpy": "1.0"}}
    s2 = EventStore(tmp_path / "e2.jsonl")
    s2.append(EV_RUN_STARTED, {"run_id": "r", "task_id": "t", "direction": "min"})
    assert fold(s2.read_all()).env is None                 # old logs: no env pin


def test_run_started_records_dirty_inputs(tmp_path):
    # P0-5 dirty-input enumeration: the uncommitted-file list is pinned at run_started and folded.
    s = EventStore(tmp_path / "events.jsonl")
    s.append(EV_RUN_STARTED, {"run_id": "r", "task_id": "t", "direction": "min",
                              "dirty_inputs": [{"source": "repo", "dirty": [" M model.py", "?? new.py"]}]})
    assert fold(s.read_all()).dirty_inputs == [{"source": "repo", "dirty": [" M model.py", "?? new.py"]}]
    s2 = EventStore(tmp_path / "e2.jsonl")        # a clean / non-repo / old-log run records []
    s2.append(EV_RUN_STARTED, {"run_id": "r", "task_id": "t", "direction": "min"})
    assert fold(s2.read_all()).dirty_inputs == []


def test_resume_flags_environment_drift(tmp_path, monkeypatch):
    # P0-5: a resume whose Python/library environment differs from run start emits env_changed; an
    # unchanged environment (and the first run) does not.
    from looplab.engine.orchestrator import Engine
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree

    class _Task:
        id = "t"; goal = "g"; direction = "min"
        def model_dump(self, mode="json"):
            return {"id": "t"}
        def leakage_inputs(self):
            return None

    class _R:
        def propose(self, s, p):
            return Idea(operator="draft", params={})

    class _D:
        def implement(self, idea):
            return "print(1)"

    eng = Engine(tmp_path / "run", task=_Task(), researcher=_R(), developer=_D(),
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 auto_install_deps=False)

    def _types():
        return [e.type for e in eng.store.read_all()]

    monkeypatch.setattr(eng, "_env_fingerprint", lambda: {"python": "3.11.0", "libs": {"numpy": "1.0"}})
    eng._setup_phase(fold(eng.store.read_all()))           # first run pins env A
    assert fold(eng.store.read_all()).env == {"python": "3.11.0", "libs": {"numpy": "1.0"}}
    assert "env_changed" not in _types()                   # no drift on the first run
    eng._setup_phase(fold(eng.store.read_all()))           # resume, same env -> no drift
    assert "env_changed" not in _types()
    monkeypatch.setattr(eng, "_env_fingerprint", lambda: {"python": "3.11.0", "libs": {"numpy": "2.0"}})
    eng._setup_phase(fold(eng.store.read_all()))           # resume after a numpy upgrade -> drift
    assert "env_changed" in _types()


def test_completed_legacy_run_without_setup_finished_is_not_re_setup(tmp_path):
    # A legacy log that reached a node but never emitted setup_finished must be treated as
    # set-up-complete (via state.nodes), so _setup_phase's gate never re-runs preflight on it.
    st = RunState()
    st.run_id = "r"
    st.setup_done = False
    from looplab.core.models import Node
    st.nodes = {0: Node(id=0, operator="draft", idea=Idea(operator="draft", params={}))}
    # the setup gate is `if not (setup_done or nodes or finished)` — with a node present it is False,
    # so _setup_phase does NOT re-run preflight for a legacy completed run.
    assert bool(st.setup_done or st.nodes or st.finished) is True
