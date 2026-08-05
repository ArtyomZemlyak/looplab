"""D3 tabular classification adapter (maximize CV accuracy)."""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.adapters.classification import ClassificationDeveloper, ClassificationTask, make_rings
from looplab.core.models import Idea
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.tasks import load_task

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "classification_task.json"


def _score(task, tmp_path, **params):
    """Run the code the Developer actually emits for `params` and return its metric."""
    X, Y = task._data()
    dev = ClassificationDeveloper(X, Y, k=task.cv_k, max_degree=task.max_degree)
    code = dev.implement(Idea(operator="draft", params=params, rationale="t"))
    res = SubprocessSandbox().run(code, str(tmp_path), timeout=60.0)
    assert res.exit_code == 0, res.stderr
    return res.metric


def test_make_rings_balanced():
    X, Y = make_rings(seed=0, n=200, gap=1.6, noise=0.6)
    assert len(X) == len(Y) == 200
    assert sum(Y) == 100          # exactly balanced 2-class (label alternates by construction)


def test_developer_runs_and_reports_accuracy(tmp_path):
    task = ClassificationTask()
    m = _score(task, tmp_path, degree=2.0, lr=0.1, l2=0.0, iters=100.0)
    assert m is not None and 0.0 <= m <= 1.0


def test_load_task_registers_classification():
    task = load_task(TASK)
    assert isinstance(task, ClassificationTask) and task.direction == "max"


def test_the_objective_is_not_flat(tmp_path):
    """THE anti-degeneracy guard — the property whose absence made this example useless.

    The shipped example used to generate two linearly-separable Gaussian blobs and template only
    (lr, l2, iters). Every setting scored the same: over a 175-point grid spanning the whole
    advertised space, 163 configurations returned the identical 0.925 and only two distinct values
    existed at all. A search over a constant function has nothing to find, so the champion was
    whichever node happened to be evaluated first — while the run still reported a champion, a
    metric and a confident rationale.

    Pinning "the metric moved in one run" would not hold this line: a run can move by luck. What
    must stay true is that the OBJECTIVE separates — that different points of the advertised space
    score measurably differently — so this drives four real evaluations through the real Developer
    and the real sandbox and asserts the spread."""
    task = load_task(TASK)
    linear = _score(task, tmp_path / "d1", degree=1.0, lr=0.1, l2=0.01, iters=200.0)
    quad = _score(task, tmp_path / "d2", degree=2.0, lr=0.1, l2=0.01, iters=200.0)
    slow = _score(task, tmp_path / "slow", degree=2.0, lr=0.001, l2=0.01, iters=10.0)
    ridged = _score(task, tmp_path / "ridge", degree=2.0, lr=0.1, l2=1.0, iters=200.0)

    # 1. The feature map is a REAL lever: the true boundary is a circle, so this learner's degree-1
    #    form tops out at 0.5550 across the WHOLE advertised lr/l2/iters grid (measured over 140
    #    points) while degree 2 reaches 0.905. That is the gradient the search exists to climb.
    #    (Note the honest bound: the best POSSIBLE line scores 0.71 — it is the log-loss fit, not
    #    the hypothesis class, that sits at 0.55. The search faces the fit.)
    assert linear < 0.65, f"degree-1 cannot represent a circular boundary, got {linear}"
    assert quad > 0.80, f"degree-2 should represent the circular boundary, got {quad}"
    assert quad - linear > 0.20

    # 2. The learner's own knobs still separate WITHIN a degree, so the search is not a single
    #    one-shot decision followed by a plateau of ties.
    assert slow < quad - 0.10, f"an under-trained learner should score worse ({slow} vs {quad})"
    assert len({linear, quad, slow, ridged}) >= 3, "the objective must not collapse onto one value"


def test_developer_implements_the_degree_it_was_given():
    """The other half of the same defect: a rationale must not describe work the code does not do.

    While `degree` was not a template parameter, the Researcher's repeated "expand to degree-2
    polynomial features" proposals were silently dropped — `implement()` emitted byte-identical
    plain-linear code under a rationale claiming the expansion. So the claim to pin is that the
    emitted PROGRAM changes with the proposed degree, not merely that `implement` was called."""
    task = ClassificationTask()
    X, Y = task._data()
    dev = ClassificationDeveloper(X, Y, k=task.cv_k, max_degree=task.max_degree)

    def code_for(degree):
        return dev.implement(Idea(operator="draft", params={"degree": float(degree), "lr": 0.1,
                                                            "l2": 0.0, "iters": 100.0},
                                  rationale="expand the feature map"))

    codes = {d: code_for(d) for d in (1, 2, 3, 4)}
    assert len(set(codes.values())) == 4, "each degree must produce a DIFFERENT program"
    for d, src in codes.items():
        assert f"DEGREE = {d}" in src

    # Out-of-range proposals are clamped into the family the rationale claims, never emitted raw:
    # degree 0 would build a feature-free model that always predicts one class.
    assert "DEGREE = 1" in code_for(0)
    assert f"DEGREE = {task.max_degree}" in code_for(task.max_degree + 5)


def test_classification_end_to_end_maximizes(tmp_path):
    task = load_task(TASK)
    r, d = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=3, max_nodes=8))
    state = anyio.run(eng.run)
    assert state.finished and state.best() is not None
    # A degree>=2 learner clears chance by a wide margin; a degree-1 one cannot. Eight nodes of the
    # blind random-walk researcher reliably find the feature map.
    assert state.best().metric >= 0.75
    # And the run must SEARCH, not tie: the old blob example produced identical metrics on every
    # node, so this asserts the scored nodes actually spread out.
    scored = [n.metric for n in state.nodes.values() if n.metric is not None]
    assert len(set(scored)) > 1, f"every node scored the same ({scored}) — the search is degenerate"
