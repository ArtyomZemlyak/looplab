"""The card must name the reference module the model can already query.

MEASURED 2026-08-29. The arena's agent has `reference <input>` and `eval_input <input>` in its
system prompt and used them 119 and 97 times over arm A's twenty task-arms — its two most-used
diagnostics after `eval` itself (177). Ours has neither, and it cannot: `run_dev_command(name)`
takes a NAME and no arguments so the operator's argv cannot be forged, which makes an `--input`
flag on the checker a door the model has no way to open.

But the capability was always here. `reference_<task>.py` is staged in the workspace and
`run_probe` runs Python over that tree. What was missing is the affordance, and the corpus says so:
of 3,124 probes, 95 (3.0 %) import the reference at all and 72 (2.3 %) call `is_solution` or
`generate_problem`. Models wrote timing loops by hand beside a module that answers the question.

This test pins the three method names, because a clause that says "you can ask the reference"
without saying HOW is the same dead end in a friendlier voice.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks" / "algotune"))
import make_task  # noqa: E402


def _shipped_measure() -> str:
    """MEASURE AS THE CARD CARRIES IT, not as one constant happens to spell it today.

    These tests read `make_task.MEASURE` directly. On 2026-09-01 the affordance clause was split out
    into `REFERENCE_AFFORDANCE` so an arm could be built without it (§78's lost control), MEASURE
    kept a `{reference_affordance}` placeholder, and two of the three went red for a text that had
    moved rather than gone.

    The third went GREEN and that is the worse half: it did `MEASURE.find("ASK THE REFERENCE")`,
    got -1, sliced `[-1:899]` into a tail that of course contained neither forbidden string, and
    passed. A test whose subject has vanished must fail, so `_marker` asserts the anchor is there.
    """
    return make_task.MEASURE.format(cost="<cost>",
                                    reference_affordance=make_task.REFERENCE_AFFORDANCE)


def _marker(text: str) -> int:
    i = text.find("ASK THE REFERENCE")
    assert i > 0, "the affordance clause is not in the shipped MEASURE at all"
    return i


def test_the_clause_names_the_module_and_all_three_methods():
    clause = _shipped_measure()
    assert "reference_<task>.py" in clause
    for method in ("generate_problem", "solve(problem)", "is_solution(problem, answer)"):
        assert method in clause, f"the clause never tells the model about {method}"


def test_it_says_which_tool_runs_it():
    """Naming the module without naming `run_probe` leaves the Developer where it started."""
    text = _shipped_measure()
    i = _marker(text)
    assert "run_probe" in text[i:i + 900]


def test_it_does_not_promise_a_command_that_takes_an_argument():
    """The arena's spelling is `reference <input>`; ours cannot be, and must not claim to be."""
    text = _shipped_measure()
    tail = text[_marker(text):][:900]
    assert 'run_dev_command("reference' not in tail
    assert 'run_dev_command("eval_input' not in tail


def test_the_clause_is_still_REACHABLE_from_the_default_card():
    """The gate must not be able to remove it by default. `--reference-affordance` is ON, so the
    text these tests pin is what a probe actually receives; a flipped default would leave every
    assertion above true of a string nothing renders."""
    import argparse
    src = (ROOT / "benchmarks" / "algotune" / "make_task.py").read_text()
    i = src.index('"--reference-affordance"')
    assert "default=True" in src[i:i + 400], (
        "the affordance clause is no longer in the card by default, so these tests pin a string "
        "no probe is given"
    )


def test_the_card_still_builds_and_carries_the_clause(tmp_path):
    """A clause that only exists in the module constant would help nobody.

    Builds a REAL card off the arena checkout and reads the goal out of the written spec.
    """
    import json

    import pytest
    arena = Path("/var/tmp/looplab-bench/AlgoTune")
    if not arena.exists():
        pytest.skip("no arena checkout on this box")
    # AND its train split, which is a SEPARATE thing to have. `--full-context` on the command line
    # means "I require this" and `make_task` exits 1 rather than building a card that claims context
    # it does not have -- so on a box whose `.hf_datasets` did not survive (snapshot.sh deliberately
    # never copied it: 872 MB, re-downloadable) this test failed instead of skipping, which is a
    # report about the box wearing the clothes of a report about the card. The clause itself is
    # still checked without any dataset by the three tests above.
    if make_task.train_dataset(arena, "integer_factorization") is None:
        pytest.skip("no train split for integer_factorization on this box (.hf_datasets absent)")
    out = subprocess.run(
        [sys.executable, str(ROOT / "benchmarks" / "algotune" / "make_task.py"),
         "--algotune-root", str(arena), "--task", "integer_factorization",
         "--out-dir", str(tmp_path), "--full-context"],
        capture_output=True, text=True, cwd=str(ROOT))
    assert out.returncode == 0, out.stderr[-800:]
    spec = json.loads((tmp_path / "algotune_integer_factorization.json").read_text(encoding="utf-8"))
    goal = json.dumps(spec)
    assert "ASK THE REFERENCE" in goal
    assert "is_solution(problem, answer)" in goal


def test_a_card_without_full_context_does_not_offer_it():
    """`--no-full-context` is the deliberately bare arm; a new capability must not leak into it."""
    import json

    import pytest
    arena = Path("/var/tmp/looplab-bench/AlgoTune")
    if not arena.exists():
        pytest.skip("no arena checkout on this box")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = subprocess.run(
            [sys.executable, str(ROOT / "benchmarks" / "algotune" / "make_task.py"),
             "--algotune-root", str(arena), "--task", "integer_factorization",
             "--out-dir", d, "--no-full-context"],
            capture_output=True, text=True, cwd=str(ROOT))
        assert out.returncode == 0, out.stderr[-800:]
        spec = json.loads((Path(d) / "algotune_integer_factorization.json").read_text(encoding="utf-8"))
        assert "ASK THE REFERENCE" not in json.dumps(spec)
