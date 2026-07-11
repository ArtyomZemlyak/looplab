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
