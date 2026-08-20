"""The env inspector may not read the package that GRADES the experiment.

`tools/env_inspect.py` answers "what does this installed library look like". An evaluation harness
pip-installed into the same venv is, to it, just another library — so `read_installed` /
`grep_installed` reach the checker, the timer and the scorer exactly as they reach numpy.

Measured 2026-08-20 on an AlgoTune run: a Developer whose task goal told it not to use the execution
probe to CHOOSE an algorithm did not stop needing the answer. It went and read the harness instead —
213 of that node's 216 env-inspection calls named `AlgoTuner`/`AlgoTuneTasks`, among them
`grep_installed(is_solution)`, `grep_installed(def run_isolated_benchmark)`,
`grep_installed(mean_speedup)` and `read_installed(AlgoTuner.utils.isolated_benchmark)`. The two
control runs beside it made 20 and 16 env reads and touched the harness ZERO times.

That asymmetry is the reason this is a fence and not a prompt line: the route opens under pressure,
so a rule that is merely stated is a rule that holds right up until it matters. It is also why
closing one channel is not enough on its own — `runtime/read_fence.py` already covered file reads and
`tools/dev_probe.py` the probe, and the search simply moved to the third.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from looplab.tools.env_inspect import EnvInspectTools

ROOT = Path(__file__).resolve().parents[1]

# Every tool that names a package or module, with the argument slot it names it in. A tool added
# with a new slot and no row here is a hole, which `test_every_naming_tool_is_covered` re-derives.
NAMING_TOOLS = (
    ("pkg_info", {"name": "AlgoTuner"}),
    ("py_api", {"target": "AlgoTuner.utils.isolated_benchmark"}),
    ("read_installed", {"module": "AlgoTuner.utils.isolated_benchmark"}),
    ("grep_installed", {"query": "is_solution", "package": "AlgoTuner"}),
)


@pytest.mark.parametrize("tool,args", NAMING_TOOLS)
def test_the_fence_refuses_every_tool_that_names_the_grader(tool, args):
    out = EnvInspectTools(deny_packages=["AlgoTuner"]).execute(tool, args)
    assert "refused" in out and "AlgoTuner" in out
    # NOT "(not installed)". That answer would send the Developer hunting for a dependency it
    # actually has — the silent-skip shape `dev_probe`'s non-OSError refusal exists to avoid.
    assert "not installed" not in out


def test_a_dotted_spelling_cannot_walk_around_the_top_level_name():
    """`AlgoTuner` fences `AlgoTuner.utils.isolated_benchmark` — one rule, not two spellings."""
    tools = EnvInspectTools(deny_packages=["AlgoTuner"])
    assert "refused" in tools.execute("read_installed", {"module": "AlgoTuner.utils.timing_core"})
    assert "refused" in tools.execute("py_api", {"target": "AlgoTuner.utils.evaluator.main"})


def test_an_undeclared_package_is_untouched_and_no_fence_is_the_default():
    """The fence must cost nothing when nobody declared one — a run that declares no grader keeps
    the historical behaviour byte for byte, and a fence that guessed would refuse real work."""
    fenced = EnvInspectTools(deny_packages=["AlgoTuner"])
    assert "refused" not in fenced.execute("pkg_info", {"name": "json"})
    assert EnvInspectTools().execute("pkg_info", {"name": "json"}) == fenced.execute(
        "pkg_info", {"name": "json"})


def test_every_naming_tool_is_covered_rather_than_the_four_we_remembered():
    """Re-derived from the provider's OWN specs, not from the table above: a tool added later that
    names a package gets no fence unless its slot is one the dispatch checks, and the table would
    not know. Tier 3 of CLAUDE.md's ladder — AST over the real spec list, never a substring."""
    slots = {"name", "target", "module", "package"}
    for spec in EnvInspectTools().specs():
        fn = spec.get("function", spec)
        props = ((fn.get("parameters") or {}).get("properties") or {})
        named = slots & set(props)
        if not named:
            continue                      # gpu_info names no package — nothing to fence
        for slot in named:
            out = EnvInspectTools(deny_packages=["AlgoTuner"]).execute(
                fn.get("name", ""), {slot: "AlgoTuner"})
            assert "refused" in out, (
                f"{fn.get('name')} names a package in `{slot}` and is NOT fenced")


def test_no_developer_site_builds_the_inspector_without_the_fence():
    """The hole is a MISSED CONSTRUCTION SITE, not a missing rule — there are four, and one bare
    call re-opens the whole route for that phase. AST so a commented-out example cannot satisfy it.
    """
    src = (ROOT / "looplab" / "adapters" / "repo_developer.py").read_text(encoding="utf-8")
    bare = [node.lineno for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "EnvInspectTools" and not node.args and not node.keywords]
    assert bare == [], f"EnvInspectTools built with no grader fence at line(s) {bare}"


def test_the_developer_takes_the_fence_from_the_operators_declaration():
    """Declared, never derived: only the operator, who wrote `eval.command`, knows which installed
    distribution is the grader. Driven through the real accessor rather than pinned as text."""
    from looplab.adapters.repo_developer import LLMRepoDeveloper

    class _Task:
        def __init__(self, spec):
            self._spec = spec

        def eval_spec(self):
            return self._spec

    dev = LLMRepoDeveloper.__new__(LLMRepoDeveloper)
    dev.task = _Task({"command": ["python", "x.py"],
                      "protect_packages": ["AlgoTuner", "AlgoTuneTasks"]})
    assert dev._grader_packages() == ("AlgoTuner", "AlgoTuneTasks")

    dev.task = _Task({"command": ["python", "x.py"]})
    assert dev._grader_packages() == (), "an undeclared task must fence nothing"

    class _Raises:
        def eval_spec(self):
            raise RuntimeError("no adapter")

    dev.task = _Raises()
    assert dev._grader_packages() == (), "an unreadable spec fences nothing, like _cmd_context"


def test_the_algotune_bridge_declares_the_harness():
    """The benchmark's own task template must carry the declaration — the fence is only as good as
    the operator remembering to state it, and this is the operator we control."""
    src = (ROOT / "benchmarks" / "algotune" / "make_task.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = [node for node in ast.walk(tree)
             if isinstance(node, ast.Constant) and node.value == "protect_packages"]
    assert found, "make_task.py no longer declares protect_packages"
    assert "\"AlgoTuner\", \"AlgoTuneTasks\"" in src or "'AlgoTuner', 'AlgoTuneTasks'" in src
