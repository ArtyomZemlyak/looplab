"""A `.pyx` with no build recipe must be REPORTED, not built.

Ground truth this test is written from, measured over the probe corpus on 2026-08-28: of the
thirty-nine nodes that shipped a `.pyx`, three (ds3 node_0, ds3 node_6, dsFB node_2) shipped it
with no `setup.py`. The bridge ran `python setup.py build_ext --inplace` over them anyway and the
only thing the model ever heard back was

    build_ext failed rc=2: .../python: can't open file '.../setup.py': [Errno 2] No such file

-- a complaint about a file it never wrote, which says nothing about the mistake it made. The three
runs were graded on the pure-Python fallback with a dead Cython source in the submission.

The refuter is `test_a_pyx_with_no_recipe_is_not_built`: restore the old predicate
(`if any(n.endswith((".pyx", ".pyi")) ...) or "setup.py" in submitted`) and it fails, because the
old code returns "run the build" for exactly this input.
"""
import importlib.util
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "looplab_eval.py"


@pytest.fixture(scope="module")
def bridge():
    spec = importlib.util.spec_from_file_location("looplab_eval_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_pyx_with_no_recipe_is_not_built(bridge):
    run, note = bridge.build_decision(["solver.py", "edge_cut.pyx"])
    assert run is False, "a build with no recipe to build from must not be attempted"
    assert note, "skipping the build silently would leave the model with no signal at all"


def test_the_note_names_the_file_the_model_wrote_and_the_one_it_did_not(bridge):
    _, note = bridge.build_decision(["solver.py", "edge_cut.pyx"])
    assert "edge_cut.pyx" in note, "the note must name the source that went unbuilt"
    assert "setup.py" in note, "the note must name what was missing"
    # The old message blamed a path; this one must describe the consequence, or the model has no
    # reason to add the recipe rather than delete the .pyx.
    assert "pure-Python" in note and "nothing was compiled" in note


def test_a_pyx_with_a_recipe_still_builds(bridge):
    for recipe in ("setup.py", "pyproject.toml"):
        run, note = bridge.build_decision(["solver.py", "edge_cut.pyx", recipe])
        assert run is True, f"{recipe} is a real recipe and the build must still run"
        assert note == ""


def test_a_recipe_with_no_pyx_still_builds(bridge):
    # `setup.py` alone is how a C extension or a compiled dependency arrives; unchanged behaviour.
    run, note = bridge.build_decision(["solver.py", "setup.py"])
    assert run is True and note == ""


def test_pure_python_neither_builds_nor_complains(bridge):
    run, note = bridge.build_decision(["solver.py"])
    assert run is False and note == "", "a plain Python submission must produce no build noise"
