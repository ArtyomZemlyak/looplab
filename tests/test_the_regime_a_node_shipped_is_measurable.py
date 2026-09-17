"""The implementation regime a node shipped, and what it was worth — measured, never prescribed.

docs/56 §108 found that on `edge_expansion` a run's score is very nearly the answer to one binary
question: did the node write a compiled kernel (42 nodes, median 166.49, against 23 pure-Python at
22.86). §110 then corrected the reach of that sentence: it is true of `edge_expansion` and of
nothing else measured, and on `pde_heat1d` not one champion carries a kernel.

So the module under test computes a CONTRAST with its sample sizes and the task it came from, and
emits no recommendation at all. These tests hold the classifier's edges — the places where a
cheaper rule would read the corpus wrong — and the two refusals that keep the contrast honest.

Driven against the archive once (2026-09-17, 161 probes, 422 evaluated nodes) and it reproduces
§108's hand measurement, which is the falsifier that matters:

    edge_expansion   compiled 191 / median 188.43   jit 59 / 26.99   plain 102 / 22.50
    §108 by hand      compiled  42 / median 166.49   jit 14 / 27.52   plain  23 / 22.86

— the same three populations in the same order, on a corpus that has since grown fourfold, with
`plain`'s maximum identical to the hundredth (35.02).
"""
from __future__ import annotations

from looplab.engine.regime_contrast import (REGIME_COMPILED, REGIME_JIT, REGIME_PLAIN, REGIMES,
                                            contrast, known_regimes, node_regime, run_contrast)


def test_a_shipped_cython_source_is_a_compiled_kernel():
    """The EXTENSION is the signal. Reading the text for `cimport` would miss the common case on
    this bench: a `.pyx` written in pure-Python syntax purely to get it compiled."""
    assert node_regime({"kern.pyx": "def f(x):\n    return x + 1\n"}) == REGIME_COMPILED
    assert node_regime({"src/fast.c": "int f(int x){return x+1;}"}) == REGIME_COMPILED


def test_the_recipe_that_builds_one_counts_too():
    """A node that writes `setup.py` with `cythonize(...)` has committed to the regime even when
    the source it compiles arrived under a name this rule does not know."""
    assert node_regime({"setup.py": "from Cython.Build import cythonize\nsetup(ext_modules=cythonize('k.p'))"}) \
        == REGIME_COMPILED
    assert node_regime({"setup.py": "ext = Extension('k', ['k.c'])"}) == REGIME_COMPILED
    assert node_regime({"solver.py": "# cython: boundscheck=False\nimport numpy\n"}) == REGIME_COMPILED


def test_a_jit_decorator_is_its_own_regime_and_not_a_kernel():
    """§108 measured them as two populations and they are six-fold apart (27.52 against 166.49);
    folding them together would erase the finding the module exists to carry."""
    assert node_regime({"solver.py": "@njit\ndef f(x):\n    return x\n"}) == REGIME_JIT
    assert node_regime({"solver.py": "@numba.njit(cache=True)\ndef f(x): return x"}) == REGIME_JIT
    assert node_regime({"solver.py": "@torch.compile\ndef f(x): return x"}) == REGIME_JIT


def test_an_import_is_not_a_decoration():
    """`import numba` at the top of a file that never decorates anything compiles nothing, and a
    rule keyed on the import would put a plain node in the fast population."""
    assert node_regime({"solver.py": "import numba\n\ndef f(x):\n    return x + 1\n"}) == REGIME_PLAIN


def test_plain_is_plain():
    assert node_regime({"solver.py": "import numpy as np\n\ndef solve(p):\n    return np.sum(p)\n"}) \
        == REGIME_PLAIN
    assert node_regime({}) == REGIME_PLAIN
    assert node_regime(None) == REGIME_PLAIN


def test_a_node_that_ships_both_is_COMPILED_and_the_rule_is_written_down():
    """A `.pyx` beside a numba fallback is a common shape. The precedence is stated in the module
    rather than decided per call site, because two readings of one corpus that disagree about this
    disagree about the headline number."""
    files = {"kern.pyx": "def f(x): return x", "solver.py": "@njit\ndef g(x): return x"}
    assert node_regime(files) == REGIME_COMPILED


def test_a_contrast_needs_two_populations():
    """One population summarised in a comparative's clothes is how a reader comes to believe
    something was compared."""
    assert contrast([(REGIME_PLAIN, 1.0), (REGIME_PLAIN, 2.0)]) is None
    assert contrast([]) is None
    got = contrast([(REGIME_PLAIN, 1.0), (REGIME_COMPILED, 6.0)])
    assert got is not None and got["best"] == REGIME_COMPILED and got["ratio"] == 6.0


def test_the_ratio_is_the_best_median_over_the_worst():
    """§108's "six-fold" is exactly this number, so the module computes that one and not a mean or
    a best-over-second-best that would read the same and differ."""
    got = contrast([(REGIME_COMPILED, 160.0), (REGIME_COMPILED, 172.0),
                    (REGIME_JIT, 27.0), (REGIME_JIT, 28.0),
                    (REGIME_PLAIN, 22.0), (REGIME_PLAIN, 24.0)])
    assert got["regimes"][REGIME_COMPILED]["median"] == 166.0
    assert got["regimes"][REGIME_COMPILED]["n"] == 2 and got["nodes"] == 6
    assert got["best"] == REGIME_COMPILED and got["worst"] == REGIME_PLAIN
    assert got["ratio"] == round(166.0 / 23.0, 4)


def test_a_zero_baseline_has_NO_ratio_rather_than_an_infinite_one():
    """A run whose worst regime scored zero has no ratio. Inventing one puts an unbounded number
    into a record other runs read -- and a zero on this bench is usually an evaluation that came
    back invalid, not a solver that is infinitely slow."""
    got = contrast([(REGIME_COMPILED, 5.0), (REGIME_PLAIN, 0.0)])
    assert got is not None and got["ratio"] is None, got
    assert got["regimes"][REGIME_PLAIN]["median"] == 0.0


def test_the_row_names_the_task_it_is_about():
    """§110 is the record of what this number does without its stamp: "compiled beats plain 6x" is
    a general law and false, while "on edge_expansion, over 65 nodes" is something another task can
    weigh. The stamp is the difference between the two."""
    class _N:
        def __init__(self, metric, files):
            self.metric, self.files = metric, files

    class _S:
        task_id, direction, run_id = "algotune_edge_expansion", "max", "r1"

        def feasible_nodes(self):
            return [_N(180.0, {"k.pyx": "x"}), _N(22.0, {"solver.py": "import numpy"}),
                    _N(None, {"solver.py": "x"})]          # unevaluated: in no population

    got = run_contrast(_S())
    assert got["task_id"] == "algotune_edge_expansion" and got["run_id"] == "r1"
    assert got["nodes"] == 2 and got["best"] == REGIME_COMPILED


# ------------------------------------------------------------------ the shared ledger's read side
def _row(task, regimes, **extra):
    return {"task_id": task, "regimes": regimes, **extra}


def test_what_this_task_knows_is_kept_apart_from_what_others_do():
    """A contrast from another task is evidence about that task. Merging the two is exactly how
    "a compiled kernel is worth 6x" became a general law it is not (§110)."""
    rows = [
        _row("edge_expansion", {REGIME_COMPILED: {"n": 40, "median": 180.0},
                                REGIME_PLAIN: {"n": 20, "median": 22.0}},
             best=REGIME_COMPILED, worst=REGIME_PLAIN, ratio=8.18, nodes=60),
        _row("pde_heat1d", {REGIME_JIT: {"n": 17, "median": 110.0}},
             best=REGIME_JIT, worst=REGIME_JIT, ratio=None, nodes=17),
    ]
    got = known_regimes(rows, "pde_heat1d")
    assert set(got["here"]) == {REGIME_JIT}
    assert got["here"][REGIME_JIT]["median_of_medians"] == 110.0
    assert [e["task_id"] for e in got["elsewhere"]] == ["edge_expansion"]


def test_the_regimes_nobody_tried_here_are_NAMED():
    """§419's point. `pde_heat1d` ran 17 of 18 nodes as jit and never compiled once; §108 read that
    as a failure to carry the kernel finding, and it is not one -- the task found a regime and
    stayed in it. What is missing there is a COMPARISON, and naming it invites a check instead of
    instructing a rewrite."""
    rows = [_row("pde_heat1d", {REGIME_JIT: {"n": 17, "median": 110.0}})]
    got = known_regimes(rows, "pde_heat1d")
    assert got["untried_here"] == [REGIME_COMPILED, REGIME_PLAIN], got
    assert known_regimes(rows, "brand_new_task")["untried_here"] == list(
        (REGIME_COMPILED, REGIME_JIT, REGIME_PLAIN))


def test_a_run_with_forty_nodes_does_not_outvote_one_with_two():
    """The ledger holds one row per RUN, and "does this regime work here" is asked once per run.
    Pooling the nodes would let a single long run answer it forty times."""
    rows = [_row("t", {REGIME_COMPILED: {"n": 40, "median": 10.0}}),
            _row("t", {REGIME_COMPILED: {"n": 2, "median": 200.0}}),
            _row("t", {REGIME_COMPILED: {"n": 2, "median": 300.0}})]
    got = known_regimes(rows, "t")
    assert got["here"][REGIME_COMPILED]["runs"] == 3
    assert got["here"][REGIME_COMPILED]["median_of_medians"] == 200.0    # not the 40-node row
    assert got["here"][REGIME_COMPILED]["nodes"] == 44


def test_a_malformed_ledger_row_is_skipped_not_fatal():
    """Rows are data written by earlier runs; a reader that dies on one of them takes a run with
    it, and this read is only ever advisory."""
    rows = ["not a dict", {"task_id": "t"}, {"task_id": "t", "regimes": "nope"},
            _row("t", {REGIME_PLAIN: {"n": 1, "median": 1.0}})]
    got = known_regimes(rows, "t")
    assert set(got["here"]) == {REGIME_PLAIN}
    assert known_regimes([], "t")["here"] == {}
    assert known_regimes(None, "t")["untried_here"] == list(REGIMES)


# ------------------------------------------------------------------ the propose prior's block
from looplab.engine.regime_contrast import REGIME_PRIOR_LABEL, regime_prior_line  # noqa: E402


def test_the_prior_states_the_sample_and_gives_no_instruction():
    """§342 and §410 both end on the same rule: the wording IS the fix. This block hands the
    proposer a measurement -- what was scored, over how many nodes, in how many runs -- and carries
    no recommendation clause and no superlative, because the recommendation would be false on a
    task in this very corpus."""
    rows = [_row("t", {REGIME_COMPILED: {"n": 40, "median": 180.0},
                       REGIME_PLAIN: {"n": 20, "median": 22.0}})]
    text, receipt = regime_prior_line(rows, "t")
    assert REGIME_PRIOR_LABEL in text and "40 node(s)" in text and "1 run(s)" in text
    for word in ("should", "must", "try ", "recommend", "best practice"):
        assert word not in text.lower(), (word, text)
    assert receipt["here"][REGIME_COMPILED]["nodes"] == 40


def test_the_prior_names_the_regimes_nobody_tried_here():
    """§419: `pde_heat1d` has no comparison at all, and saying so invites a check. Saying "a
    compiled kernel is worth 6x" instructs, and is false there."""
    rows = [_row("pde_heat1d", {REGIME_JIT: {"n": 17, "median": 110.61}})]
    text, receipt = regime_prior_line(rows, "pde_heat1d")
    assert "Never tried here: compiled, plain" in text, text
    assert receipt["untried_here"] == [REGIME_COMPILED, REGIME_PLAIN]


def test_another_task_is_quoted_WITH_its_task_name():
    """A number without its task is how §110 happened. The other-task clause always names the task
    and its sample, so it can never be read as a law about this one."""
    rows = [_row("pde_heat1d", {REGIME_JIT: {"n": 17, "median": 110.0}}),
            _row("edge_expansion", {REGIME_COMPILED: {"n": 191, "median": 188.43},
                                    REGIME_PLAIN: {"n": 102, "median": 22.5}},
                 best=REGIME_COMPILED, worst=REGIME_PLAIN, ratio=8.38, nodes=352)]
    text, _r = regime_prior_line(rows, "pde_heat1d")
    assert "On edge_expansion: compiled over plain 8.38x (352 nodes)" in text, text


def test_an_empty_ledger_renders_NOTHING():
    """A header over "no data" spends a prompt slot to say nothing, and the five-slot budget one
    module over is the record of what that costs."""
    assert regime_prior_line([], "t") == ("", {})
    assert regime_prior_line(None, "t") == ("", {})


def test_the_block_is_off_unless_an_operator_asked(tmp_path):
    """`regime_prior` defaults False at every layer, so today's prompt is byte-identical."""
    from looplab.core.config import Settings
    from looplab.engine.options import EngineOptions
    assert Settings().regime_prior is False
    assert EngineOptions.from_settings(Settings()).regime_prior is False
