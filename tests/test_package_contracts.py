"""Three modules that lived in the wrong package, and the math they dragged with them (doc 25 RA-08,
XP-12).

`looplab/runtime/`'s docstring said "process execution: sandboxes, command evaluation, environment
preparation" while three of its eight modules did none of that. The cost was not the wrong folder —
it was the import edge: `runtime/proxy.py` reached into `events` purely to call a k-NN function, and
that single edge was the whole reason the runtime tier depended on the projection layer at all. A
package whose stated contract is false stops being a boundary anyone can reason about.

The layering rule these restore (CLAUDE.md): `core` imports nothing above itself, and `runtime`
imports nothing above `core`.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "looplab"


def _module_imports(path: Path) -> set[str]:
    """Every `looplab.<pkg>` this module imports, at module scope OR function-local."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("looplab."):
                seen.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("looplab."):
                    seen.add(alias.name.split(".")[1])
    return seen


def _package_imports(package: str) -> dict[str, set[str]]:
    return {path.name: _module_imports(path) for path in sorted((ROOT / package).glob("*.py"))}


# ------------------------------------------------------------------ RA-08: the runtime contract

def test_runtime_holds_only_process_execution_modules():
    names = {path.stem for path in (ROOT / "runtime").glob("*.py")} - {"__init__"}
    assert names == {"sandbox", "command_eval", "deps", "bg_tasks", "read_fence",
                     # The three 2026-08-13 additions belong here for the same reason `read_fence`
                     # does: each is a property of HOW a child process is launched and what it may
                     # touch while it runs, and each must import nothing above `core` so the eval
                     # path stays a leaf. `metric_subject` is the identity the eval CAPTURES (a stat
                     # and a digest around the score stage); `read_allowlist` derives what the launch
                     # may read; `landlock` applies it in the child between fork and exec. The
                     # POLICY over all three — which rung, whether a violation is minted — lives in
                     # `engine/`, which is the split this package boundary exists to hold.
                     "metric_subject", "read_allowlist", "landlock",
                     # `stage_identity` (2026-08-17) is the same kind of fact one question over:
                     # WHAT a stage ran on and WHAT it produced, both derived from bytes at the
                     # instants the eval path already has them (before the command, and when the
                     # `expect.files` contract passes). It records onto the stage row and decides
                     # nothing — every POLICY that could read it (a reuse cache, a duplication
                     # report) lives above, which is the same split `metric_subject` is here for.
                     "stage_identity",
                     # `numeric_contract` (2026-09-06, doc 52 row 24) is the `expect.numeric` half of
                     # the stage contract: a declared relation held to the last value the stage
                     # PRINTED, evaluated by the eval path right where `expect.files` is, importing
                     # nothing above `core`. It decides one thing — did the stage satisfy its own
                     # declared bound — which is a property of the launch's output, not a policy.
                     "numeric_contract",
                     # `metric_inputs` (2026-08-20) is `metric_subject`'s MIRROR and belongs here by
                     # the same clause: it is a stat and a digest the eval path takes at the metric
                     # read, over the operator's declared `eval.inputs`, and it imports nothing above
                     # `core` — in fact nothing above `runtime`, since it is `metric_subject.bind_one`
                     # with two policies inverted (no confinement, no freshness floor, because an
                     # input is by definition foreign, shared and old). The POLICY over what it
                     # captures — the comparability KEY, its authorities, and which surfaces refuse
                     # a cross-key ranking — is `engine/comparability.py`, above this boundary,
                     # exactly as the `metric_subject` clause above describes for its own half.
                     "metric_inputs",
                     # `applied_params` (2026-08-20) is `metric_subject`'s third face and
                     # belongs here for the identical reason: it reads bytes standing in the
                     # eval's own workdir at the instant the number was read, through the SAME
                     # `bind_one` binder with the two policies inverted (no confinement rule of
                     # its own, freshness on the RESOLVED tier only), and it imports nothing
                     # above `core`. It records what the configuration that ran said and
                     # decides nothing — the POLICY that reads it (`engine/champion_caveats.py`
                     # `params_overridden`) lives above, exactly as the subject side's does.
                     "applied_params"}, (
        "a module that is not process execution landed in runtime again")


def test_runtime_imports_nothing_above_core():
    """The layering rule, now actually true. The one violation was `proxy.py` importing
    `events.digest` — and it was importing it for a math function, not for an event log."""
    above = {"events", "search", "engine", "agents", "trust", "adapters", "serve", "tools"}
    for module, imports in _package_imports("runtime").items():
        assert not (imports & above), f"runtime/{module} imports {sorted(imports & above)}"


@pytest.mark.parametrize("module,package,symbol", [
    ("proxy", "search", "ProxyScorer"),
    ("notebook", "events", "champion_notebook"),
    ("jupyter", "serve", "setup_looplab"),
])
def test_each_moved_module_lives_with_its_concern(module, package, symbol):
    assert not (ROOT / "runtime" / f"{module}.py").exists()
    assert hasattr(importlib.import_module(f"looplab.{package}.{module}"), symbol)


def test_the_flat_alias_follows_the_move():
    """`looplab/__init__.py`'s `_LAYOUT` finder resolves the pre-split flat paths; a move that does
    not update it leaves the alias pointing at a package that no longer holds the module."""
    from looplab import _LAYOUT

    assert _LAYOUT["proxy"] == "search"
    assert _LAYOUT["notebook"] == "events"
    assert _LAYOUT["jupyter"] == "serve"
    assert _LAYOUT["numeric"] == "core"
    import looplab.proxy
    import looplab.numeric
    assert looplab.proxy is importlib.import_module("looplab.search.proxy"), (
        "both names must resolve to the SAME module object, or monkeypatching one misses the other")
    assert looplab.numeric is importlib.import_module("looplab.core.numeric")


def test_the_calibration_probe_moved_down_and_every_old_spelling_is_the_same_object():
    """The probe the engine has to ask about extra-metric provenance (2026-08-14).

    It moved because `runtime/sandbox.py` needed `engine_declared_extra_metric_keys` and `runtime`
    may import nothing above `core`; the GRANT has since moved up to `engine/eval_dispatch.py`
    (the byte-prefix check authenticated nothing, since the bytes are the candidate's), and `core`
    remains right for a module `engine` and `search` both read — but the answer was a MOVE and never a
    copy: the probe source is an input to a calibration DIGEST, so a second copy of its key tuple is
    how the two silently drift apart. Both halves are asserted here, because either one alone is the
    bug: the module lives in `core`, AND every spelling of it names ONE object.
    """
    assert not (ROOT / "agents" / "calibration.py").exists()
    canonical = importlib.import_module("looplab.core.calibration")
    # The retired dotted path routes through `_RENAMED`, not through a re-exporting shim: the
    # grant site resolves the classifier through this module on every call, and a second module object
    # would make the monkeypatch in `tests/test_auto_extra_metrics.py` a silent no-op.
    assert importlib.import_module("looplab.agents.calibration") is canonical
    assert importlib.import_module("looplab.calibration") is canonical

    from looplab import _LAYOUT
    from looplab.agents import roles
    from looplab.search import speculation_quality

    assert _LAYOUT["calibration"] == "core"
    # `agents/roles.py` re-exported these before the move and must keep doing it — the same objects,
    # not a re-derivation that can drift from the digested source.
    for name in ("SPECULATION_CUDA_PROBE_CODE_PREFIX", "SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS",
                 "SPECULATION_CUDA_PROBE_STATIC_EXTRA_METRICS"):
        assert getattr(roles, name) is getattr(canonical, name)
        assert getattr(speculation_quality, name) is getattr(canonical, name)
    assert _module_imports(ROOT / "core" / "calibration.py") == set(), (
        "the probe must keep importing nothing, or `core` inherits whatever it grows")


def test_the_packaging_entry_point_follows_jupyter():
    """The launch spec is reached by NAME from installed metadata, not by import, so a move that
    leaves the entry point behind fails only at deploy time — in a JupyterHub, silently, as a
    missing Launcher tile."""
    pyproject = (ROOT.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert 'looplab = "looplab.serve.jupyter:setup_looplab"' in pyproject
    assert "looplab.runtime.jupyter" not in pyproject


# ------------------------------------------------------------------ XP-12: the numeric primitives

def test_the_estimator_lives_in_core_and_reads_no_event_log():
    from looplab.core import numeric

    assert _module_imports(ROOT / "core" / "numeric.py") == set(), (
        "a pure numeric primitive must not import anything from looplab")
    assert callable(numeric.knn_idw) and callable(numeric.numeric_params)


def test_the_digest_still_exports_the_same_objects():
    """Several modules and tests import these from `events.digest`; the re-export must be the SAME
    object, not a re-implementation that can drift."""
    from looplab.core import numeric
    from looplab.events import digest

    assert digest.knn_idw is numeric.knn_idw
    assert digest.numeric_params is numeric.numeric_params


def test_every_predictor_reaches_the_shared_core():
    from looplab.search import panel, proxy, surrogate
    from looplab.core.numeric import knn_idw

    assert panel.knn_idw is knn_idw
    assert surrogate.knn_idw is knn_idw
    assert proxy.knn_idw is knn_idw


def test_param_distance_stays_a_projection_concern():
    """Not moved: it is the exact metric the novelty gate uses to say two experiments are "near",
    which is a statement about a run, not arithmetic."""
    from looplab.events import digest

    assert hasattr(digest, "param_distance")
    assert not hasattr(importlib.import_module("looplab.core.numeric"), "param_distance")


def test_the_proxy_keeps_its_own_deliberately_different_coercion():
    """It accepts numeric STRINGS, which `numeric_params` must never start doing — generated
    solutions sometimes emit params as text and the proxy should still rank them."""
    from looplab.core.numeric import numeric_params
    from looplab.search.proxy import ProxyScorer

    assert ProxyScorer._numeric({"a": "0.1", "b": 2}) == {"a": 0.1, "b": 2.0}
    assert numeric_params({"a": "0.1", "b": 2}) == {"b": 2.0}


def test_the_idw_core_behaves_identically_after_the_move():
    from looplab.core.numeric import knn_idw

    assert knn_idw([], 3) is None, "an empty history is the caller's abstain path"
    assert knn_idw([(0.0, 5.0), (1.0, 9.0)], 3) == (5.0, 0.0), "exact match short-circuits"
    # The zero is found even when a NaN distance sorts ahead of it — the property the whole-top-k
    # scan exists for, and the one a naive `nn[0]` check would lose.
    assert knn_idw([(float("nan"), 1.0), (0.0, 5.0)], 3) == (5.0, 0.0)
    pred, nearest = knn_idw([(1.0, 0.0), (1.0, 10.0)], 2)
    assert pred == 5.0 and nearest == 1.0
