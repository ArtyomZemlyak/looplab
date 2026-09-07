"""Every score names the WIDTH it was evaluated at and the BASELINE it was divided by.

docs/58 §58.3, two findings about the record rather than the arms:

* **Width.** "Width moves speedup by about 1.6× on this box: `ab2-summary.txt` scores the same
  `discrete_log` solver at 1.0007 / 0.9973 on `workers=1` and 1.6318 / 1.6054 on `workers=24`" —
  and eight of arm B's twenty campaign numbers were RE-SCORED (`logs/rescore.log`) with the width
  recorded nowhere. `eval_regime.key` has carried the regime since 2026-08-30; the resolved count
  itself was still only derivable from it by a reader who knew the key's grammar.
* **Baseline.** "All twenty arm-B files say `baseline_source: in-harness`, which records no baseline
  to compare." The denominator is ONE per-instance cache entry, `<task>__<subset><regime>.json`
  (named by `patch_baseline_cache.py`, globbed by `_regime_mismatch`), and nothing recorded which
  bytes it held when the number was taken.

So `looplab_eval.py::_emit` now stamps, top-level and non-numeric, `eval_workers` (the resolved
count as a string), `baseline_cache_file` and `baseline_cache_sha256` (the entry's digest, or null
with `baseline_cache_missing` saying why), and `campaign.sh::record_done` stamps the same identity
into every `.done` marker through `ruler_fields` — arm A's number passes through no `final.json`,
so the marker is the only place it can carry one. `compare_arms.py` reads both and refuses to pair
rows whose identities differ or are missing.

The bridge is driven by path and by subprocess; the campaign's functions are extracted and run,
as `test_campaign_marker_evidence.py` does. The one branch not driven is `--enforce-rules`, whose
refusal line prints outside `_emit` and needs AlgoTune's own validator: it is checked by AST for the
splat of `_ruler_fields`, which proves the call is in the text and not that it executes.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / "benchmarks" / "algotune" / "looplab_eval.py"
CAMPAIGN = REPO / "benchmarks" / "algotune" / "campaign.sh"


def _by_path(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LE = _by_path(BRIDGE, "_bridge_under_test_ruler_identity")


def _width() -> int:
    return len(os.sched_getaffinity(0))


def _lane() -> str:
    """This process's own affinity as a `taskset -c` list: `ruler_fields` pins the bridge to the
    lane's cpus so the regime key carries the LANE width, and the fixture entry has to be named
    for the width that call will actually see."""
    return ",".join(str(c) for c in sorted(os.sched_getaffinity(0)))


def _entry(times: Path, task: str, subset: str, key: str, body: str = '{"i0": 12.5}') -> Path:
    times.mkdir(parents=True, exist_ok=True)
    path = times / f"{task}__{subset}{key}.json"
    path.write_text(body, encoding="utf-8")
    return path


def _emit(monkeypatch, capsys, row: dict, *, workers: str = "auto") -> dict:
    monkeypatch.setenv("ALGOTUNE_EVAL_WORKERS", workers)
    LE._emit(dict(row))
    return json.loads(capsys.readouterr().out.strip())


# ------------------------------------------------------------------------------------------------
# the result line
# ------------------------------------------------------------------------------------------------

def test_a_scored_line_names_its_width_and_its_baseline(monkeypatch, capsys, tmp_path):
    w = _width()
    entry = _entry(tmp_path / "times", "demo", "test", f"__w{w}x1r3")
    LE.bind_ruler("demo", "test", tmp_path / "times")
    printed = _emit(monkeypatch, capsys, {"speedup": 1.5, "subset": "test",
                                          "baseline_source": "in-harness (record exposes no "
                                                             "baseline_time_ms to cache)"})
    assert printed["eval_workers"] == str(w), printed
    assert printed["eval_regime"]["workers"] == w
    assert printed["baseline_cache_file"] == entry.name
    assert printed["baseline_cache_sha256"] == hashlib.sha256(entry.read_bytes()).hexdigest()
    assert "baseline_cache_missing" not in printed
    # the scored path's own sentence is kept; identity ADDS to it
    assert printed["baseline_source"].startswith("in-harness")


def test_the_digest_is_of_the_bytes_and_changes_when_the_cache_is_retimed(monkeypatch, capsys,
                                                                            tmp_path):
    """The key alone cannot see a re-timed cache: same name, different bytes, different
    denominator. That is why the digest is recorded beside the key rather than instead of it."""
    w = _width()
    entry = _entry(tmp_path / "times", "demo", "test", f"__w{w}x1r3", '{"i0": 12.5}')
    LE.bind_ruler("demo", "test", tmp_path / "times")
    first = _emit(monkeypatch, capsys, {"speedup": 1.5, "subset": "test"})
    entry.write_text('{"i0": 13.1}', encoding="utf-8")
    second = _emit(monkeypatch, capsys, {"speedup": 1.5, "subset": "test"})
    assert first["eval_regime"]["key"] == second["eval_regime"]["key"]
    assert first["baseline_cache_sha256"] != second["baseline_cache_sha256"]


def test_a_missing_entry_is_null_with_a_reason_and_never_a_guess(monkeypatch, capsys, tmp_path):
    w = _width()
    LE.bind_ruler("demo", "test", tmp_path / "times-that-do-not-exist")
    printed = _emit(monkeypatch, capsys, {"speedup": None, "no_speedup": {"reason": "no_solver"}})
    assert printed["baseline_cache_sha256"] is None
    assert printed["baseline_cache_file"] == f"demo__test__w{w}x1r3.json"
    assert "times-that-do-not-exist" in printed["baseline_cache_missing"]
    # nothing above set `baseline_source`, so the reason is the source
    assert printed["baseline_source"] == printed["baseline_cache_missing"]


def test_an_unbound_emit_says_so_rather_than_digesting_another_tasks_cache(monkeypatch, capsys):
    LE.bind_ruler(None, None, None)
    printed = _emit(monkeypatch, capsys, {"speedup": 2.0, "subset": "train"})
    assert printed["baseline_cache_sha256"] is None
    assert "no task/subset bound" in printed["baseline_cache_missing"]
    assert printed["eval_workers"] == str(_width())      # the width needs no binding


def test_the_row_s_own_subset_outranks_the_bound_one(monkeypatch, capsys, tmp_path):
    """`subset` is reassigned after the run when the evaluator says it scored the other half
    (`subset_from_stderr`); the entry named must be the one that DIVIDED, not the one asked for."""
    w = _width()
    _entry(tmp_path / "times", "demo", "train", f"__w{w}x1r3")
    tested = _entry(tmp_path / "times", "demo", "test", f"__w{w}x1r3", '{"i0": 99}')
    LE.bind_ruler("demo", "train", tmp_path / "times")
    printed = _emit(monkeypatch, capsys, {"speedup": 1.5, "subset": "test"})
    assert printed["baseline_cache_file"] == tested.name
    assert printed["baseline_cache_sha256"] == hashlib.sha256(tested.read_bytes()).hexdigest()


def test_the_new_fields_do_not_reach_the_node_s_extra_metrics(monkeypatch, capsys, tmp_path):
    """The falsifier for `eval_workers: 22` as an int: `json_line_extras` sweeps every top-level
    numeric key into `extra_metrics` as an undeclared `auto` measurement."""
    from looplab.runtime.sandbox import json_line_extras, json_line_metric

    w = _width()
    _entry(tmp_path / "times", "demo", "test", f"__w{w}x1r3")
    LE.bind_ruler("demo", "test", tmp_path / "times")
    monkeypatch.setenv("ALGOTUNE_EVAL_WORKERS", "auto")
    LE._emit({"speedup": 1.5, "eval_seconds": 12.0, "subset": "test"})
    line = capsys.readouterr().out.strip()
    assert json_line_metric(line, "speedup") == 1.5
    assert json_line_extras(line, "speedup") == {"eval_seconds": 12.0}, json.loads(line)


def test_the_entry_name_is_the_one_the_patch_writes_and_the_guard_globs():
    """Three spellings of one file name, pinned to each other: `patch_baseline_cache.py` writes
    `<task>__<subset><regime>.json`, `_regime_mismatch` globs `<task>__<subset>__*.json`, and this
    names the same file -- or the digest is of a file nothing divides by."""
    patch = (REPO / "benchmarks" / "algotune" / "patch_baseline_cache.py").read_text("utf-8")
    assert '{{_ll_task}}__{{subset}}{{_ll_regime}}.json' in patch, "the patch renamed its cache"
    src = BRIDGE.read_text("utf-8")
    assert 'glob(f"{args.task}__{args.subset}__*.json")' in src, "the guard's glob moved"
    ident = LE.ruler_identity("demo", "test", None)
    assert ident["baseline_cache_file"] == f"demo__test{LE.eval_regime()['key']}.json"
    assert ident["baseline_cache_file"].startswith("demo__test__")


def test_the_rules_violation_line_carries_the_identity_too():
    """AST, not substring: the refusal prints outside `_emit` (see `_NO_SPEEDUP_REASONS`), so it has
    to splat the same fields itself. This proves the splat is in the call, not that it runs."""
    tree = ast.parse(BRIDGE.read_text("utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print"
                and node.args and isinstance(node.args[0], ast.Call)
                and isinstance(node.args[0].func, ast.Attribute) and node.args[0].func.attr == "dumps"
                and node.args[0].args and isinstance(node.args[0].args[0], ast.Dict)):
            literal = node.args[0].args[0]
            keys = {k.value for k in literal.keys if isinstance(k, ast.Constant)}
            if "rules_violation" in keys:
                splats = [v for k, v in zip(literal.keys, literal.values) if k is None]
                assert any(isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                           and v.func.id == "_ruler_fields" for v in splats), (
                    "the --enforce-rules refusal line no longer splats _ruler_fields")
                return
    raise AssertionError("no rules_violation print found in the bridge")


# ------------------------------------------------------------------------------------------------
# --print-ruler: the same identity for a caller that scores nothing
# ------------------------------------------------------------------------------------------------

def _print_ruler(tmp: Path, *extra: str, workers: str = "auto") -> str:
    got = subprocess.run([sys.executable, str(BRIDGE), "--print-ruler", "--task", "demo",
                          "--subset", "test", "--baseline-times-dir", str(tmp / "times"), *extra],
                         capture_output=True, text=True, timeout=60,
                         env=dict(os.environ, ALGOTUNE_EVAL_WORKERS=workers))
    assert got.returncode == 0, got.stderr
    return got.stdout.strip()


def test_print_ruler_needs_no_arena(tmp_path):
    w = _width()
    entry = _entry(tmp_path / "times", "demo", "test", f"__w{w}x1r3")
    ident = json.loads(_print_ruler(tmp_path))
    assert ident["eval_workers"] == str(w)
    assert ident["regime"] == f"__w{w}x1r3"
    assert ident["baseline_cache_sha256"] == hashlib.sha256(entry.read_bytes()).hexdigest()


def test_print_ruler_marker_format_is_the_marker_grammar(tmp_path):
    w = _width()
    entry = _entry(tmp_path / "times", "demo", "test", f"__w{w}x1r3")
    sha = hashlib.sha256(entry.read_bytes()).hexdigest()
    line = _print_ruler(tmp_path, "--ruler-format", "marker")
    assert line == f"eval_workers={w} regime=__w{w}x1r3 baseline_sha256={sha}", line
    # a cold cache is `none` -- derived, and absent -- which is not the `?` of "could not derive"
    entry.unlink()
    line = _print_ruler(tmp_path, "--ruler-format", "marker", workers="1")
    assert line == f"eval_workers=1 regime=__lane{w}r3 baseline_sha256=none", line


# ------------------------------------------------------------------------------------------------
# the campaign's markers carry the same identity, for BOTH arms
# ------------------------------------------------------------------------------------------------

_FUNCTIONS = ("run_started_evidence", "successful_calls", "ruler_fields", "marker_is_harness_cut",
              "marker_is_operator_skip", "marker_is_immediate_exit", "already_measured",
              "record_done", "refuse_to_start")


def _harness(here: Path) -> str:
    src = CAMPAIGN.read_text(encoding="utf-8")
    parts = ["set -u", "LANE_COUNT=4", "CORES_PER_LANE=22", 'LANE_LAYOUT="whole_cores"',
             'IMMEDIATE_EXIT_S="${IMMEDIATE_EXIT_S:-60}"', f'HERE="{here}"',
             'ARM="${ARM:-A}"', 'T="${T:-demo}"']
    for name in _FUNCTIONS:
        found = re.search(rf"^{name}\(\) \{{.*?^\}}$", src, re.M | re.S)
        assert found, f"campaign.sh no longer defines {name}()"
        parts.append(found.group(0))
    return "\n".join(parts) + "\n"


def _bash(script: str, cwd: Path, here: Path = CAMPAIGN.parent, **env) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", _harness(here) + script], cwd=str(cwd),
                          capture_output=True, text=True, timeout=120,
                          env={**os.environ, "ALGOTUNE_EVAL_WORKERS": "auto", **env})


def test_an_arm_a_marker_carries_the_ruler_identity(tmp_path):
    """Arm A's number goes through no `final.json`; the marker is where its identity has to be."""
    w = _width()
    entry = _entry(tmp_path / "times", "demo", "test", f"__w{w}x1r3")
    done = tmp_path / "A-demo.done"
    # start epoch 0 -> a wall of the whole Unix era, i.e. `ran_to_completion`; no LoopLab run dir
    got = _bash(f'record_done "{done}" 0 0 "{_lane()}" ""', tmp_path,
                ALGOTUNE_BASELINE_CACHE_DIR=str(tmp_path / "times"))
    assert got.returncode == 0, got.stderr
    marker = done.read_text()
    assert "state=ran_to_completion" in marker, marker
    assert re.search(r"\beval_workers=\d+\b", marker), marker
    assert re.search(r"\bregime=__(w\d+x\d+|lane\d+)r3\b", marker), marker
    assert f"baseline_sha256={hashlib.sha256(entry.read_bytes()).hexdigest()}" in marker, marker


def test_every_marker_state_carries_it(tmp_path):
    """The identity rides in REGIME, so a state added later cannot forget it. Driven over the
    states that write a marker with no evidence prerequisites."""
    w = _width()
    _entry(tmp_path / "times", "demo", "test", f"__w{w}x1r3")
    for rc, state in ((124, "wall_cut"),):
        done = tmp_path / f"B-demo-{rc}.done"
        _bash(f'record_done "{done}" {rc} 0 "{_lane()}" ""', tmp_path,
              ALGOTUNE_BASELINE_CACHE_DIR=str(tmp_path / "times"))
        marker = done.read_text()
        assert f"state={state}" in marker and "baseline_sha256=" in marker, marker
    done = tmp_path / "B-demo-now.done"
    _bash(f'record_done "{done}" 0 "$(date +%s)" "{_lane()}" ""', tmp_path,
          ALGOTUNE_BASELINE_CACHE_DIR=str(tmp_path / "times"))
    marker = done.read_text()
    assert "state=exited_immediately" in marker and "baseline_sha256=" in marker, marker


def test_a_cold_cache_is_none_and_an_unreachable_bridge_is_a_question_mark(tmp_path):
    """Two different absences, told apart in the marker -- and neither withholds the marker: the
    identity is a record, not a gate, and a task-arm that was measured stays measured."""
    done = tmp_path / "A-demo.done"
    _bash(f'record_done "{done}" 0 0 "{_lane()}" ""', tmp_path,
          ALGOTUNE_BASELINE_CACHE_DIR=str(tmp_path / "empty-times"))
    assert "baseline_sha256=none" in done.read_text(), done.read_text()
    gone = tmp_path / "A-demo-2.done"
    _bash(f'record_done "{gone}" 0 0 "{_lane()}" ""', tmp_path, here=tmp_path / "no-bridge-here")
    marker = gone.read_text()
    assert "eval_workers=? regime=? baseline_sha256=?" in marker, marker
    assert "state=ran_to_completion" in marker
