"""A bench tool names a probe from a path, and on Windows that path is spelled with "\\".

Windows CI, run 35785582444 (review 2026-09-22): 110 failures across ten of the benchmark tools'
test files had ONE cause. `glob.glob(f"{root}/model-probes/*/runs/...")` answers
`...\\bench/model-probes\\p1\\runs\\...` there -- the pattern's literal prefix as written, every
matched component joined with "\\" -- and every tool then split that string on "/model-probes/" or
"/runs/": an IndexError (pulse, resume, outlier_check, check_money, sweep_claims, read_loops) or,
where the split had a fallback, every probe silently named after its own run dir (probe_summary
printed one probe called `run`).

The fix puts each answer in POSIX form before the split. These tests drive four of the tools
through `_windows_emulation.windows_glob` (the Windows answer, still openable here); the last one
holds every OTHER such split in `benchmarks/` to the same rule, by AST.
"""
from __future__ import annotations

import ast
import glob
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BENCH = REPO / "benchmarks"
sys.path.insert(0, str(BENCH))

import check_money  # noqa: E402
import outlier_check  # noqa: E402
import probe_summary  # noqa: E402
import pulse  # noqa: E402
from _source_scan import iter_trees  # noqa: E402
from _windows_emulation import windows_glob  # noqa: E402


def _run_dir(bench: Path, probe: str, task: str = "t") -> Path:
    run = bench / "model-probes" / probe / "runs" / task / "run"
    run.mkdir(parents=True)
    return run


def _spelled_by_windows(handed: list) -> None:
    """The double really did hand out Windows-spelled answers -- else the test proves nothing."""
    assert handed and all("\\" in h for h in handed), handed


def test_check_money_sums_spans_under_the_probe_name(tmp_path, monkeypatch):
    run = _run_dir(tmp_path, "p1")
    (run / "spans.jsonl").write_text(
        json.dumps({"name": "generation", "start": 10.0, "attributes": {"cost": 0.25}}) + "\n",
        encoding="utf-8")
    handed = windows_glob(monkeypatch)
    cost, calls = check_money.spans_by_probe(str(tmp_path), 0.0)
    _spelled_by_windows(handed)
    assert dict(cost) == {"p1": 0.25} and dict(calls) == {"p1": 1}


def test_pulse_names_a_paused_probe(tmp_path, monkeypatch):
    run = _run_dir(tmp_path, "remDL13")
    (run / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in (
            {"type": "llm_usage", "data": {"cost": 0.11}},
            {"type": "pause", "data": {"reason": "auto-paused: the provider failed"}})),
        encoding="utf-8")
    handed = windows_glob(monkeypatch)
    got = pulse.paused_probes(str(tmp_path))
    _spelled_by_windows(handed)
    assert [r["probe"] for r in got] == ["remDL13"], got


def test_outlier_check_reads_the_task_from_the_probes_own_tree(tmp_path, monkeypatch):
    run = _run_dir(tmp_path, "live", task="pagerank")
    (run / "events.jsonl").write_text("", encoding="utf-8")
    handed = windows_glob(monkeypatch)
    got = outlier_check.probe_task(str(tmp_path / "model-probes"), "live")
    _spelled_by_windows(handed)
    assert got == "pagerank"


def test_probe_summary_names_the_probe_and_not_its_run_dir(tmp_path, monkeypatch):
    """probe_summary walks with `rglob`, so its run dir arrives as a Path; on Windows `str()` of it
    has "\\" separators and the "/runs/" split fell back to the run dir itself."""
    run = _run_dir(tmp_path, "p9")
    (run / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in (
            {"type": "run_started", "ts": 1000.0, "data": {}},
            {"type": "node_evaluated", "ts": 1001.0, "data": {"metric": 2.0}})),
        encoding="utf-8")
    (run / "spans.jsonl").write_text("", encoding="utf-8")
    handed = windows_glob(monkeypatch)
    # The run dir as Windows spells it: `...\\bench/model-probes\\p9\\runs\\t\\run`.
    spelled = Path(glob.glob(f"{tmp_path}/model-probes/*/runs/*/run")[0])
    _spelled_by_windows(handed)
    got = probe_summary.summarise(spelled)
    assert got is not None and got["probe"] == "p9", got


# The only split of this shape that does NOT parse a filesystem answer: `lanes.probes` reads the
# argv of a live process out of `/proc`, which exists on Linux alone and is never a glob result.
_NOT_A_GLOB_ANSWER = {"benchmarks/lanes.py"}


def _posix_form(node: ast.AST, bound: dict) -> bool:
    """Is this receiver spelled in POSIX form before it is split?"""
    if isinstance(node, ast.Name):
        return node.id in bound
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "as_posix":
            return True
        if node.func.attr == "replace" and len(node.args) == 2:
            old, new = node.args
            return (isinstance(old, ast.Attribute) and old.attr == "sep"
                    and isinstance(new, ast.Constant) and new.value == "/")
    return False


def _directory_marker(node: ast.Call) -> str | None:
    """`"/model-probes/"`, `"/runs/"` -- a directory named between two separators -- or None."""
    if not node.args or not isinstance(node.args[0], ast.Constant):
        return None
    lit = node.args[0].value
    if (isinstance(lit, str) and len(lit) > 2 and lit.startswith("/") and lit.endswith("/")
            and "/" not in lit[1:-1]):
        return lit
    return None


def test_every_directory_split_in_the_bench_tools_is_made_on_the_posix_form():
    """Tier 3 for the residue: this proves the TEXT of each split, not that it runs -- the drives
    above are what prove the rule works. A name counts as POSIX form only where the SAME function
    assigned it from one."""
    offenders, sites = set(), set()
    # The ONE walk (`tests/_source_scan.py::iter_trees`, sorted, BOM-safe) — a private `rglob` here
    # is what `test_source_scan_helper::test_no_guard_test_re_derives_the_walk` refuses.
    for path, tree in iter_trees(BENCH):
        rel = path.relative_to(REPO).as_posix()
        if rel in _NOT_A_GLOB_ANSWER:
            continue
        for scope in [n for n in ast.walk(tree)
                      if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef))]:
            bound = {t.id for n in ast.walk(scope) if isinstance(n, ast.Assign)
                     and _posix_form(n.value, {}) for t in n.targets if isinstance(t, ast.Name)}
            for n in ast.walk(scope):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr in {"split", "rsplit", "partition", "rpartition"}
                        and _directory_marker(n) is not None):
                    sites.add((rel, n.lineno, n.col_offset))
                    if not _posix_form(n.func.value, bound):
                        offenders.add(f"{rel}:{n.lineno} {ast.unparse(n)[:80]}")
    assert len(sites) >= 10, f"the scan found only {len(sites)} directory splits -- still looking?"
    assert not offenders, (
        "a bench tool splits a path on a directory marker without putting it in POSIX form first; "
        "a Windows glob answers with '\\\\' and the split finds nothing (WIN-SEPS):\n"
        + "\n".join(sorted(offenders)))
