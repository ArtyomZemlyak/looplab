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
    assert contrast([(REGIME_PLAIN, 1.0), (REGIME_PLAIN, 2.0)], direction="max") is None
    assert contrast([], direction="max") is None
    got = contrast([(REGIME_PLAIN, 1.0), (REGIME_COMPILED, 6.0)], direction="max")
    assert got is not None and got["best"] == REGIME_COMPILED and got["ratio"] == 6.0


def test_the_ratio_is_the_best_median_over_the_worst():
    """§108's "six-fold" is exactly this number, so the module computes that one and not a mean or
    a best-over-second-best that would read the same and differ."""
    got = contrast([(REGIME_COMPILED, 160.0), (REGIME_COMPILED, 172.0),
                    (REGIME_JIT, 27.0), (REGIME_JIT, 28.0),
                    (REGIME_PLAIN, 22.0), (REGIME_PLAIN, 24.0)], direction="max")
    assert got["regimes"][REGIME_COMPILED]["median"] == 166.0
    assert got["regimes"][REGIME_COMPILED]["n"] == 2 and got["nodes"] == 6
    assert got["best"] == REGIME_COMPILED and got["worst"] == REGIME_PLAIN
    assert got["ratio"] == round(166.0 / 23.0, 4)


def test_a_zero_baseline_has_NO_ratio_rather_than_an_infinite_one():
    """A run whose worst regime scored zero has no ratio. Inventing one puts an unbounded number
    into a record other runs read -- and a zero on this bench is usually an evaluation that came
    back invalid, not a solver that is infinitely slow."""
    got = contrast([(REGIME_COMPILED, 5.0), (REGIME_PLAIN, 0.0)], direction="max")
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
    # Every ledger writer stamps the run's objective, and the reader now refuses a row without one
    # (review 2026-09-22, ENG3-04); the fixtures below were all maximized tasks.
    return {"task_id": task, "regimes": regimes, "direction": "max", **extra}


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
    got = known_regimes(rows, "pde_heat1d", direction="max")
    assert set(got["here"]) == {REGIME_JIT}
    assert got["here"][REGIME_JIT]["median_of_medians"] == 110.0
    assert [e["task_id"] for e in got["elsewhere"]] == ["edge_expansion"]


def test_the_regimes_nobody_tried_here_are_NAMED():
    """§419's point. `pde_heat1d` ran 17 of 18 nodes as jit and never compiled once; §108 read that
    as a failure to carry the kernel finding, and it is not one -- the task found a regime and
    stayed in it. What is missing there is a COMPARISON, and naming it invites a check instead of
    instructing a rewrite."""
    rows = [_row("pde_heat1d", {REGIME_JIT: {"n": 17, "median": 110.0}})]
    got = known_regimes(rows, "pde_heat1d", direction="max")
    assert got["untried_here"] == [REGIME_COMPILED, REGIME_PLAIN], got
    assert known_regimes(rows, "brand_new_task", direction="max")["untried_here"] == list(
        (REGIME_COMPILED, REGIME_JIT, REGIME_PLAIN))


def test_a_run_with_forty_nodes_does_not_outvote_one_with_two():
    """The ledger holds one row per RUN, and "does this regime work here" is asked once per run.
    Pooling the nodes would let a single long run answer it forty times."""
    rows = [_row("t", {REGIME_COMPILED: {"n": 40, "median": 10.0}}),
            _row("t", {REGIME_COMPILED: {"n": 2, "median": 200.0}}),
            _row("t", {REGIME_COMPILED: {"n": 2, "median": 300.0}})]
    got = known_regimes(rows, "t", direction="max")
    assert got["here"][REGIME_COMPILED]["runs"] == 3
    assert got["here"][REGIME_COMPILED]["median_of_medians"] == 200.0    # not the 40-node row
    assert got["here"][REGIME_COMPILED]["nodes"] == 44


def test_a_refinalized_run_is_ONE_run_in_the_ledger_read():
    """Review 2026-09-22, ENG3-03. The finalize APPENDS a row every time a run is finalized, and a
    reopened run finalizes again over its whole node set, so the reader counted its first segment
    once per finalize — `runs` and `nodes` inflated and its medians voted twice. The reader keeps
    the LATEST row per run (`run_ref`); the ledger itself stays append-only history. MUTATION: fold
    every row again -> `runs` reads 3 and `nodes` 10."""
    first = _row("t", {REGIME_COMPILED: {"n": 2, "median": 100.0}}, run_id="r", run_uid="U1")
    again = _row("t", {REGIME_COMPILED: {"n": 5, "median": 160.0}}, run_id="r", run_uid="U1")
    other = _row("t", {REGIME_COMPILED: {"n": 3, "median": 40.0}}, run_id="r", run_uid="U2")
    got = known_regimes([first, again, other], "t", direction="max")
    assert got["here"][REGIME_COMPILED] == {"runs": 2, "median_of_medians": 100.0, "nodes": 8}
    # ...and the same rule on the other-task clause: one run is one run there too.
    elsewhere = known_regimes(
        [_row("o", {REGIME_COMPILED: {"n": 1, "median": 9.0}, REGIME_PLAIN: {"n": 1, "median": 3.0}},
              run_uid="U9", nodes=2),
         _row("o", {REGIME_COMPILED: {"n": 2, "median": 9.0}, REGIME_PLAIN: {"n": 2, "median": 3.0}},
              run_uid="U9", nodes=4)], "t", direction="max")["elsewhere"]
    assert [(e["runs"], e["nodes"]) for e in elsewhere] == [(1, 4)]


def test_seeded_rows_are_one_run_per_SOURCE_LOG_not_per_probe_name():
    """`benchmarks/regime_table.py --seed-ledger` stamps the PROBE directory as `run_id` on every
    archived run under that probe, so keying on the name alone would merge distinct runs;
    `seeded_from` is the one log a seeded row came from, and re-seeding the same archive collapses
    onto it. A row that names no run at all is kept as it is — nothing to match it against."""
    a = _row("t", {REGIME_COMPILED: {"n": 1, "median": 100.0}}, run_id="probe1",
             seeded_from="/a/model-probes/probe1/runs/x/run/events.jsonl")
    b = _row("t", {REGIME_COMPILED: {"n": 1, "median": 200.0}}, run_id="probe1",
             seeded_from="/a/model-probes/probe1/runs/y/run/events.jsonl")
    anonymous = _row("t", {REGIME_COMPILED: {"n": 1, "median": 300.0}})
    got = known_regimes([a, b, dict(a), anonymous, dict(anonymous)], "t", direction="max")
    assert got["here"][REGIME_COMPILED]["runs"] == 4, got["here"]


def test_a_malformed_ledger_row_is_skipped_not_fatal():
    """Rows are data written by earlier runs; a reader that dies on one of them takes a run with
    it, and this read is only ever advisory."""
    rows = ["not a dict", {"task_id": "t"}, {"task_id": "t", "regimes": "nope"},
            _row("t", {REGIME_PLAIN: {"n": 1, "median": 1.0}})]
    got = known_regimes(rows, "t", direction="max")
    assert set(got["here"]) == {REGIME_PLAIN}
    assert known_regimes([], "t", direction="max")["here"] == {}
    assert known_regimes(None, "t", direction="max")["untried_here"] == list(REGIMES)


# ------------------------------------------------------------------ the propose prior's block
from looplab.engine.regime_contrast import REGIME_PRIOR_LABEL, regime_prior_line  # noqa: E402


def test_the_prior_states_the_sample_and_gives_no_instruction():
    """§342 and §410 both end on the same rule: the wording IS the fix. This block hands the
    proposer a measurement -- what was scored, over how many nodes, in how many runs -- and carries
    no recommendation clause and no superlative, because the recommendation would be false on a
    task in this very corpus."""
    rows = [_row("t", {REGIME_COMPILED: {"n": 40, "median": 180.0},
                       REGIME_PLAIN: {"n": 20, "median": 22.0}})]
    text, receipt = regime_prior_line(rows, "t", direction="max")
    assert REGIME_PRIOR_LABEL in text and "40 node(s)" in text and "1 run(s)" in text
    for word in ("should", "must", "try ", "recommend", "best practice"):
        assert word not in text.lower(), (word, text)
    assert receipt["here"][REGIME_COMPILED]["nodes"] == 40


def test_the_prior_names_the_regimes_nobody_tried_here():
    """§419: `pde_heat1d` has no comparison at all, and saying so invites a check. Saying "a
    compiled kernel is worth 6x" instructs, and is false there."""
    rows = [_row("pde_heat1d", {REGIME_JIT: {"n": 17, "median": 110.61}})]
    text, receipt = regime_prior_line(rows, "pde_heat1d", direction="max")
    assert "Never tried here: compiled, plain" in text, text
    assert receipt["untried_here"] == [REGIME_COMPILED, REGIME_PLAIN]


def test_another_task_is_quoted_WITH_its_task_name():
    """A number without its task is how §110 happened. The other-task clause always names the task
    and its sample, so it can never be read as a law about this one."""
    rows = [_row("pde_heat1d", {REGIME_JIT: {"n": 17, "median": 110.0}}),
            _row("edge_expansion", {REGIME_COMPILED: {"n": 191, "median": 188.43},
                                    REGIME_PLAIN: {"n": 102, "median": 22.5}},
                 best=REGIME_COMPILED, worst=REGIME_PLAIN, ratio=8.38, nodes=352)]
    text, _r = regime_prior_line(rows, "pde_heat1d", direction="max")
    # 8.37x and not the row's own stored `ratio` of 8.38: the line RE-DERIVES it from the same
    # per-regime medians `here` uses (188.43 / 22.5), so a row whose stored ratio disagrees with its
    # own medians -- an older writer, a different aggregation -- cannot put the disagreement in
    # front of a model. The two numbers differing in the last digit is that property visible.
    assert "On edge_expansion: compiled over plain 8.37x (352 nodes)" in text, text


def test_an_empty_ledger_renders_NOTHING():
    """A header over "no data" spends a prompt slot to say nothing, and the five-slot budget one
    module over is the record of what that costs."""
    assert regime_prior_line([], "t", direction="max") == ("", {})
    assert regime_prior_line(None, "t", direction="max") == ("", {})


def test_the_block_is_off_unless_an_operator_asked(tmp_path):
    """`regime_prior` defaults False at every layer, so today's prompt is byte-identical."""
    from looplab.core.config import Settings
    from looplab.engine.options import EngineOptions
    assert Settings().regime_prior is False
    assert EngineOptions.from_settings(Settings()).regime_prior is False


# ------------------------------------------------- what a ledger row carries, and what it costs
def test_a_one_regime_run_still_writes_what_it_measured():
    """`contrast` refuses to call one population a comparison, and that refusal stands -- the row
    carries no `best`, `worst` or `ratio`. But the ROW is the evidence B2 exists to carry.

    Measured on a ledger seeded from the archive: with one-regime runs dropped, `pde_heat1d` kept
    ONE row of twelve and its prior read "jit, median 37.47 over 1 node" where the corpus says 17
    nodes at 110.61 -- and on `discrete_log` the surviving two-regime subset REVERSED the ranking
    (1.49x for jit against the corpus's 1.28x for compiled). A prior built from that subset would
    have told the next run the opposite of what was measured.
    """
    class _N:
        def __init__(self, metric, files):
            self.metric, self.files = metric, files

    class _S:
        task_id, direction, run_id = "algotune_pde_heat1d", "max", "r7"

        def feasible_nodes(self):
            return [_N(110.0, {"solver.py": "@njit\ndef f(x): return x"}),
                    _N(89.0, {"solver.py": "@njit\ndef g(x): return x"})]

    row = run_contrast(_S())
    assert row is not None and set(row["regimes"]) == {REGIME_JIT}
    assert row["regimes"][REGIME_JIT]["n"] == 2 and row["nodes"] == 2
    for absent in ("best", "worst", "ratio"):
        assert absent not in row, (absent, row)
    assert row["task_id"] == "algotune_pde_heat1d" and row["run_id"] == "r7"


def test_another_task_is_ONE_line_however_many_runs_it_has():
    """The ledger holds a row per RUN, so quoting rows verbatim quotes a task as often as it ran.
    Rendered from a real 105-row seed that read: "On edge_expansion: compiled over plain 5.62x
    (4 nodes). On edge_expansion: compiled over plain 13.7x (4 nodes)" -- two four-node "facts" in
    place of one task's 352-node picture, and a reader counting sentences would have counted
    evidence."""
    rows = [_row("other", {REGIME_COMPILED: {"n": 2, "median": 180.0},
                           REGIME_PLAIN: {"n": 2, "median": 20.0}}, nodes=4),
            _row("other", {REGIME_COMPILED: {"n": 2, "median": 200.0},
                           REGIME_PLAIN: {"n": 2, "median": 25.0}}, nodes=4),
            _row("mine", {REGIME_JIT: {"n": 1, "median": 5.0}}, nodes=1)]
    got = known_regimes(rows, "mine", direction="max")
    assert len(got["elsewhere"]) == 1, got["elsewhere"]
    only = got["elsewhere"][0]
    assert only["task_id"] == "other" and only["runs"] == 2 and only["nodes"] == 8
    text, _r = regime_prior_line(rows, "mine", direction="max")
    assert text.count("On other:") == 1, text


def test_the_other_task_line_uses_THE_SAME_statistic_as_this_one():
    """Both halves of one sentence have to be reconcilable. The first version took the median of the
    per-run RATIOS for other tasks while `here` takes the median of the per-run MEDIANS, and the two
    disagreed in print: `discrete_log: jit over compiled 1.49x` beside a published table reading
    `compiled over jit 1.28x`. Same aggregation now, so the direction cannot flip."""
    rows = [_row("other", {REGIME_COMPILED: {"n": 1, "median": 10.0},
                           REGIME_JIT: {"n": 3, "median": 2.0}}, nodes=4),
            _row("other", {REGIME_COMPILED: {"n": 1, "median": 12.0},
                           REGIME_JIT: {"n": 3, "median": 3.0}}, nodes=4),
            _row("mine", {REGIME_PLAIN: {"n": 1, "median": 1.0}}, nodes=1)]
    elsewhere = known_regimes(rows, "mine", direction="max")["elsewhere"][0]
    # medians of run medians: compiled 11.0, jit 2.5 -> 4.4x, compiled ahead.
    assert elsewhere["best"] == REGIME_COMPILED and elsewhere["worst"] == REGIME_JIT
    assert elsewhere["ratio"] == 4.4, elsewhere
    here = known_regimes(rows, "other", direction="max")["here"]
    assert here[REGIME_COMPILED]["median_of_medians"] == 11.0
    assert here[REGIME_JIT]["median_of_medians"] == 2.5


# ------------------------------------------ the objective's DIRECTION (review 2026-09-22, ENG3-04)
# Every ranking here assumed HIGHER IS BETTER. On a minimized task (a runtime, a loss) the regime
# that scored worst was written to the shared ledger as `best`, and the prior told the next run
# "compiled over plain 5.48x" where plain had won. Driven before the fix: a `min` run whose plain
# nodes scored 2.0/2.2 and compiled nodes 12.0/11.0 wrote `best: compiled, worst: plain`.
import pytest  # noqa: E402

from looplab.engine.regime_contrast import _rank  # noqa: E402


@pytest.mark.parametrize("direction,medians,expected", [
    ("max", {"compiled": 12.0, "plain": 2.0}, ("compiled", "plain", 6.0)),
    ("min", {"compiled": 12.0, "plain": 2.0}, ("plain", "compiled", 6.0)),
    # the ratio is the fold the BEST beats the worst by, so >= 1 under either objective
    ("min", {"compiled": 1.0, "jit": 4.0, "plain": 2.0}, ("compiled", "jit", 4.0)),
    ("max", {"compiled": 1.0, "jit": 4.0, "plain": 2.0}, ("jit", "compiled", 4.0)),
    # its denominator is the SMALLER median either way: zero or negative there means no ratio
    ("max", {"compiled": 5.0, "plain": 0.0}, ("compiled", "plain", None)),
    ("min", {"compiled": 5.0, "plain": 0.0}, ("plain", "compiled", None)),
    ("min", {"compiled": -1.0, "plain": 3.0}, ("compiled", "plain", None)),
    # ties keep the FIRST regime, exactly as `max`/`min` did on a maximized task
    ("max", {"compiled": 3.0, "plain": 3.0}, ("compiled", "compiled", 1.0)),
    ("min", {"compiled": 3.0, "plain": 3.0}, ("compiled", "compiled", 1.0)),
])
def test_best_and_worst_are_decided_by_the_objective(direction, medians, expected):
    """THE TRUTH TABLE. MUTATION: rank with `max`/`min` on the raw median again -> every `min`
    row with two different medians flips its best and worst."""
    assert _rank(medians, direction) == expected


def test_a_minimized_run_writes_its_WINNING_regime_as_best():
    class _N:
        def __init__(self, metric, files):
            self.metric, self.files = metric, files

    class _S:
        task_id, direction, run_id = "runtime_task", "min", "r1"

        def feasible_nodes(self):
            return [_N(2.0, {"solver.py": "x"}), _N(2.2, {"solver.py": "y"}),
                    _N(12.0, {"k.pyx": "x"}), _N(11.0, {"k.pyx": "y"})]

    row = run_contrast(_S())
    assert (row["best"], row["worst"], row["direction"]) == (REGIME_PLAIN, REGIME_COMPILED, "min")
    assert row["ratio"] == round(11.5 / 2.1, 4)


def test_the_direction_is_required_and_an_unknown_one_ranks_nothing():
    """A default would silently re-assume the maximized objective this module used to hard-code."""
    with pytest.raises(TypeError):
        contrast([(REGIME_PLAIN, 1.0), (REGIME_COMPILED, 6.0)])
    with pytest.raises(TypeError):
        known_regimes([], "t")
    assert contrast([(REGIME_PLAIN, 1.0), (REGIME_COMPILED, 6.0)], direction="") is None
    rows = [_row("t", {REGIME_PLAIN: {"n": 1, "median": 1.0}})]
    assert regime_prior_line(rows, "t", direction="sideways") == ("", {})


def test_an_inverted_row_ALREADY_in_the_ledger_now_reads_correctly():
    """Rows written for minimized tasks before the fix carry a stored `best` that is the loser.
    The reader recomputes from `regimes` under the row's own direction and never trusts the stored
    ranking, so the existing shared ledger heals without a migration."""
    written_before_the_fix = _row(
        "runtime_task", {REGIME_COMPILED: {"n": 2, "median": 11.5}, REGIME_PLAIN: {"n": 2, "median": 2.1}},
        direction="min", best=REGIME_COMPILED, worst=REGIME_PLAIN, ratio=5.4762, nodes=4)
    [entry] = known_regimes([written_before_the_fix], "another_task", direction="max")["elsewhere"]
    assert (entry["best"], entry["worst"], entry["ratio"]) == (REGIME_PLAIN, REGIME_COMPILED, 5.4762)
    text, _r = regime_prior_line([written_before_the_fix], "another_task", direction="max")
    assert "On runtime_task: plain over compiled 5.48x (4 nodes)." in text, text


def test_a_minimized_task_lists_its_own_regimes_best_first():
    rows = [_row("runtime_task", {REGIME_COMPILED: {"n": 2, "median": 11.5},
                                  REGIME_PLAIN: {"n": 2, "median": 2.1}}, direction="min")]
    text, _r = regime_prior_line(rows, "runtime_task", direction="min")
    assert text.index("plain (median 2.1") < text.index("compiled (median 11.5"), text
    # ...and a maximized task still reads highest first, byte for byte as before.
    rows_max = [_row("t", {REGIME_COMPILED: {"n": 2, "median": 11.5},
                           REGIME_PLAIN: {"n": 2, "median": 2.1}})]
    assert regime_prior_line(rows_max, "t", direction="max")[0] == (
        "Implementation regimes measured on this task: compiled (median 11.5 over 2 node(s) in "
        "1 run(s)); plain (median 2.1 over 2 node(s) in 1 run(s)). Never tried here: jit.")


def test_rows_are_pooled_and_ranked_only_under_one_direction():
    """A task id reused for the OPPOSITE objective is not evidence about this one, a row with no
    direction cannot be ranked at all, and another task is ranked under its own direction — two
    groups when its rows disagree, never one pooled ranking."""
    rows = [
        _row("t", {REGIME_PLAIN: {"n": 3, "median": 5.0}}),                       # max: this one
        _row("t", {REGIME_JIT: {"n": 3, "median": 9.0}}, direction="min"),        # opposite
        {"task_id": "t", "regimes": {REGIME_COMPILED: {"n": 3, "median": 7.0}}},   # unknown
        _row("o", {REGIME_COMPILED: {"n": 1, "median": 4.0}, REGIME_PLAIN: {"n": 1, "median": 2.0}},
             direction="min", nodes=2),
        _row("o", {REGIME_COMPILED: {"n": 1, "median": 4.0}, REGIME_PLAIN: {"n": 1, "median": 2.0}},
             nodes=2),
    ]
    got = known_regimes(rows, "t", direction="max")
    assert set(got["here"]) == {REGIME_PLAIN}
    assert sorted((e["direction"], e["best"]) for e in got["elsewhere"]) == [
        ("max", REGIME_COMPILED), ("min", REGIME_PLAIN)]


def _archive(tmp_path, probes: dict):
    """A `model-probes/<probe>/runs/<run>/run/events.jsonl` tree the benchmark globs, one run each:
    `{probe: (direction, [(files, metric), ...])}`."""
    import json

    root = tmp_path / "model-probes"
    for probe, (direction, nodes) in probes.items():
        run = root / probe / "runs" / "r" / "run"
        run.mkdir(parents=True)
        events = [{"type": "run_started", "data": {"task_id": f"task_{probe}",
                                                   "direction": direction}}]
        for nid, (files, metric) in enumerate(nodes):
            events.append({"type": "node_created", "data": {"node_id": nid, "files": files}})
            events.append({"type": "node_evaluated", "data": {"node_id": nid, "metric": metric}})
        (run / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events),
                                          encoding="utf-8")
    return str(root)


def test_the_archive_table_and_its_seed_rank_each_probe_under_its_OWN_direction(tmp_path):
    """`benchmarks/regime_table.py` imports `contrast`, so it moved in the same change: it read no
    direction at all and `--seed-ledger` stamped every row `max`, so a minimized probe was published
    — and seeded into the shared ledger — with its losing regime as `best`. It now reads the
    direction the way the run's own fold does."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
    import regime_table

    root = _archive(tmp_path, {
        "fast": ("min", [({"solver.py": "x"}, 2.0), ({"solver.py": "y"}, 2.2),
                         ({"k.pyx": "x"}, 12.0), ({"k.pyx": "y"}, 11.0)]),
        "speed": ("max", [({"solver.py": "x"}, 2.0), ({"k.pyx": "x"}, 12.0)]),
        "legacy": ("Maximize", [({"solver.py": "x"}, 2.0), ({"k.pyx": "x"}, 12.0)]),
    })
    seeded = {row["task_id"]: row for row in regime_table.seed_rows(root)}
    assert (seeded["task_fast"]["direction"], seeded["task_fast"]["best"]) == ("min", REGIME_PLAIN)
    assert (seeded["task_speed"]["direction"], seeded["task_speed"]["best"]) == (
        "max", REGIME_COMPILED)
    # The fold's own rule: anything but min/max is "min" — the objective that run actually used.
    assert seeded["task_legacy"]["direction"] == "min"

    probes, by_task, directions = regime_table.collect(root)
    table = regime_table.render(probes, by_task, 1, directions)
    assert "best plain over worst compiled: 5.48x" in table, table
    assert "best compiled over worst plain: 6.00x" in table, table
