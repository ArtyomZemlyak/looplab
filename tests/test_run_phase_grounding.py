"""`grounding` must mean "setup is running", not "nothing has happened yet".

The operator's report: forty minutes after starting a run, the UI still read *"The run is in
grounding. Setting up task and data…"*. Setup had finished in 6.5 seconds. In between, the
Researcher spent 13 minutes on a proposal and the Developer 23 minutes on a build.

The cause was a phase derived from an ABSENCE:

    if not st.nodes and st.data_profile is None and st.run_id: return PHASE_GROUNDING

`data_profile` is set by `data_profiled`, which `_setup_phase` emits only when the task has a
`columns()` hook. A `RepoTask` has none — so for repo runs the condition was really "no nodes yet",
and it stayed true through every stage between setup and the first node.

A phase that names a stage must be folded from that stage's own terminal. `setup_done` is exactly
that (`setup_finished`), so these tests pin the phase against the fold rather than against a proxy.
"""
from __future__ import annotations

from looplab.core.models import RunState
from looplab.serve.appstate import AppState
from looplab.serve.protocol import PHASE_GROUNDING, PHASE_SEARCH


def _phase(**fields) -> str:
    """`phase` reads nothing off `self`, so the rule can be driven without an app."""
    return AppState.phase(None, RunState(run_id="r", **fields))


def test_a_run_whose_setup_has_not_finished_is_grounding():
    assert _phase(setup_done=False) == PHASE_GROUNDING


def test_a_repo_run_leaves_grounding_when_SETUP_finishes_not_when_a_node_appears():
    """The defect, stated as a test. A repo task profiles no data and has no nodes yet — the exact
    state the run spent forty minutes in — and it must no longer read as grounding."""
    assert _phase(setup_done=True, data_profile=None) == PHASE_SEARCH


def test_a_profiled_task_is_unaffected():
    """The task kinds that DO emit `data_profiled` behaved correctly before and must still."""
    assert _phase(setup_done=True, data_profile={"rows": 10}) == PHASE_SEARCH
    assert _phase(setup_done=False, data_profile={"rows": 10}) == PHASE_GROUNDING


def test_the_absence_of_a_data_profile_no_longer_decides_anything():
    """Pinned as source, because the property is a NEGATIVE: the old derivation must not come back
    under a different spelling of the same proxy."""
    import inspect

    source = inspect.getsource(AppState.phase)
    assert "st.data_profile is None" not in source
    assert "not st.setup_done" in source
