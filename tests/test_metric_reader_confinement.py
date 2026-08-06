"""One workdir-confinement guard and one reader table for metric reading (doc 25 RA-05, RA-04).

`read_metric` was a ~120-line flat if-chain in which the containment idiom —
`if not _is_within(X.resolve(), Path(workdir).resolve()): return None`, wrapped in
`try/except (OSError, ValueError)` — appeared VERBATIM three times. That guard is not decoration:

* for `file_json` / `file_regex` and for `host_score`'s predictions it stops a read outside the
  attempt workspace — an answer key, or a planted result from another run that scores perfectly;
* for `adapter` it stops an arbitrary host `.py` being handed to `runpy`, which is code execution.

Three hand-copies meant a fourth reader could plausibly forget it, so both halves are pinned here:
`_confined` itself (including the refusal-not-crash rule), and the property that EVERY registered
reader that touches a path goes through it. The reader table additionally replaces the three parallel
enumerations of the reader kinds — this dict, `repo_task._valid_metric_kind`'s local set, and a
`METRIC_READERS` constant that claimed to be shared and had no consumers.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

from looplab.runtime.command_eval import (METRIC_READERS, READER_PATH_KEYS,
                                           READERS_REQUIRING_PATH, _confined, read_metric)


# ------------------------------------------------------------------ the guard itself

def test_a_plain_relative_path_is_confined_and_resolved(tmp_path):
    (tmp_path / "out.json").write_text("{}", encoding="utf-8")
    assert _confined(tmp_path, "out.json") == (tmp_path / "out.json").resolve()


def test_a_nested_relative_path_is_still_inside(tmp_path):
    (tmp_path / "deep").mkdir()
    assert _confined(tmp_path, "deep/out.json") is not None


@pytest.mark.parametrize("rel", ["../escape.json", "../../escape.json", "sub/../../escape.json"])
def test_a_traversal_out_of_the_workdir_is_refused(tmp_path, rel):
    workdir = tmp_path / "work"
    workdir.mkdir()
    (tmp_path / "escape.json").write_text('{"metric": 1.0}', encoding="utf-8")
    assert _confined(workdir, rel) is None


def test_an_absolute_path_is_refused_even_when_it_exists(tmp_path):
    """`Path(workdir) / "/etc/passwd"` is `/etc/passwd` — the join silently discards the workdir, so
    the guard is the ONLY thing standing between an agent-authored spec and any host file."""
    outside = tmp_path / "answer_key.json"
    outside.write_text('{"metric": 1.0}', encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()
    assert _confined(workdir, str(outside)) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_symlink_pointing_out_of_the_workdir_is_refused(tmp_path):
    """The reason the guard resolves rather than string-checks: a candidate can create the symlink
    itself, inside its own workspace, at eval time."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    (tmp_path / "answer_key.json").write_text('{"metric": 1.0}', encoding="utf-8")
    (workdir / "preds.json").symlink_to(tmp_path / "answer_key.json")
    assert _confined(workdir, "preds.json") is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_symlink_that_stays_inside_the_workdir_is_allowed(tmp_path):
    """Confinement is about WHERE it lands, not about symlinks being suspicious."""
    workdir = tmp_path / "work"
    (workdir / "runs").mkdir(parents=True)
    (workdir / "runs" / "real.json").write_text("{}", encoding="utf-8")
    (workdir / "latest.json").symlink_to(workdir / "runs" / "real.json")
    assert _confined(workdir, "latest.json") == (workdir / "runs" / "real.json").resolve()


def test_an_embedded_nul_is_a_refusal_not_a_crash(tmp_path):
    """A malformed spec must fail the NODE like every other malformed-spec branch, not raise out of
    the reader and take the whole run down with no terminal event."""
    assert _confined(tmp_path, "with\x00nul") is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_candidate_created_symlink_LOOP_is_a_refusal_not_a_crash(tmp_path):
    """A latent bug the extraction surfaced, present in all three hand-copies: they caught
    `(OSError, ValueError)`, but `Path.resolve()` raises `RuntimeError("Symlink loop from ...")` —
    and the candidate can create that loop inside its OWN workdir at eval time. Before the shared
    guard, a two-line `ln -s` plus a `file_json` spec naming the loop escaped `read_metric` and took
    the whole RUN down instead of failing one node.
    """
    (tmp_path / "a").symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(tmp_path / "a")
    assert _confined(tmp_path, "a") is None
    assert read_metric("", str(tmp_path), {"kind": "file_json", "path": "a"}) is None


def test_the_workdir_itself_counts_as_inside(tmp_path):
    assert _confined(tmp_path, ".") == tmp_path.resolve()


# ------------------------------------------------------------------ every path reader uses it

def _spec_variants(outside):
    """Every reader kind that takes a path, pointed at a file OUTSIDE the workdir."""
    return [
        {"kind": "file_json", "path": str(outside), "key": "metric"},
        {"kind": "file_regex", "path": str(outside), "pattern": r"([0-9.]+)"},
        {"kind": "host_score", "predictions": str(outside), "labels": str(outside)},
        {"kind": "adapter", "path": str(outside)},
    ]


@pytest.mark.parametrize("index", range(4))
def test_no_reader_reads_a_metric_source_outside_the_workdir(tmp_path, index):
    workdir = tmp_path / "work"
    workdir.mkdir()
    outside = tmp_path / "answer_key.json"
    outside.write_text(json.dumps({"metric": 999.0}), encoding="utf-8")
    spec = _spec_variants(outside)[index]
    assert read_metric("", str(workdir), spec) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_the_adapter_reader_refuses_a_symlinked_host_module_before_EXECing_it(tmp_path):
    """The sharpest case: this branch does not read the file, it `runpy`s it. A missed guard here is
    arbitrary host code execution, not a wrong number."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    marker = tmp_path / "pwned.txt"
    (tmp_path / "evil.py").write_text(
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\n"
        "def read_metric(workdir):\n    return 1.0\n", encoding="utf-8")
    (workdir / "LOOPLAB_adapter.py").symlink_to(tmp_path / "evil.py")
    assert read_metric("", str(workdir), {"kind": "adapter"}) is None
    assert not marker.exists(), "the adapter module was EXECed from outside the workdir"


def test_a_confined_adapter_still_runs(tmp_path):
    """The guard must not have made the feature unusable — the positive case is what proves the
    refusals above are about location, not about the reader being broken."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "LOOPLAB_adapter.py").write_text(
        "def read_metric(workdir):\n    return 0.75\n", encoding="utf-8")
    assert read_metric("", str(workdir), {"kind": "adapter",
                                          "timeout": 60}) == pytest.approx(0.75)


def test_every_path_touching_reader_is_registered_and_routes_through_the_guard():
    """A source scan on the finding itself: a reader that builds `Path(workdir) / …` on its own has
    re-created the copy. The guard is the only sanctioned way in."""
    import inspect

    from looplab.runtime import command_eval

    for kind, reader in METRIC_READERS.items():
        source = inspect.getsource(reader)
        if "workdir" not in source or "Path(workdir)" not in source:
            continue                                    # a stdout reader touches no path at all
        assert "_confined(" in source, f"the {kind!r} reader builds a path without `_confined`"
    # ...and `_confined` is the only place the idiom is spelled out.
    module_source = inspect.getsource(command_eval)
    assert module_source.count("_is_within(") == 3, (
        "expected exactly three: the definition, `_confined`'s use, and host_score's labels-must-be-"
        "OUTSIDE check, which is the inverse assertion and deliberately not `_confined`")


# -------------------------------------------- a non-string path slot: a refusal, not a dead RUN

_NON_STRINGS = [123, 1.5, True, ["metrics.json"], {"path": "metrics.json"}, b"metrics.json"]
_PATH_SLOTS = sorted((kind, slot) for kind, slots in READER_PATH_KEYS.items() for slot in slots)


@pytest.mark.parametrize("kind,slot", _PATH_SLOTS, ids=[f"{k}.{s}" for k, s in _PATH_SLOTS])
@pytest.mark.parametrize("value", _NON_STRINGS, ids=[type(v).__name__ for v in _NON_STRINGS])
def test_a_nonstring_path_slot_fails_the_node_not_the_run(tmp_path, kind, slot, value):
    """`Path(workdir) / 123` raises TypeError, which `_confined` does NOT catch (it catches
    OSError/ValueError/RuntimeError), and neither does `host_score`'s `Path(<labels>).resolve()`.
    Above `read_metric` the eval worker's only handler is `except GpuPinUnenforceable`
    (`engine/evaluate.py`), so the escape gave the node NO terminal event and killed the whole RUN —
    which then re-died on every resume. A malformed spec must fail the NODE, like every other
    malformed-spec branch in this module.

    Parametrized off `READER_PATH_KEYS` so the guard is asked of every slot that reaches a path
    constructor, not just the two that `metric_spec_path_error` used to cover: the check lived INSIDE
    that function's `kind not in READERS_REQUIRING_PATH` early exit, so `adapter`'s `path` and
    `host_score`'s `predictions`/`labels` were unprotected and all three were measured to raise.

    The submit-time refusal is not enough on its own, which is why this drives `read_metric` rather
    than the validator: `adapters/repo_task.py::_grandfathered` reloads a run's recorded
    `task.snapshot.json` WITHOUT re-validating it, so a spec authored before the refusal existed
    still arrives here.
    """
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "ok.json").write_text(json.dumps({"metric": 1.0}), encoding="utf-8")
    # Every OTHER path slot of this reader gets a usable string, so the reader really would have run
    # on to the bad one rather than abstaining earlier for an unrelated reason.
    spec = {"kind": kind, **{s: "ok.json" for s in READER_PATH_KEYS[kind]}, slot: value}
    assert read_metric("", str(workdir), spec) is None


def test_the_nonstring_guard_refuses_the_type_not_the_reader(tmp_path):
    """The other half: a guard that made every reader abstain would satisfy the test above while
    silently disabling metric reading. The same readers with STRING paths still produce values."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "ok.json").write_text(json.dumps({"metric": 1.0}), encoding="utf-8")
    (workdir / "LOOPLAB_adapter.py").write_text(
        "def read_metric(workdir):\n    return 0.5\n", encoding="utf-8")
    assert read_metric("", str(workdir), {"kind": "file_json", "path": "ok.json"}) == 1.0
    assert read_metric("", str(workdir), {"kind": "adapter", "timeout": 60}) == pytest.approx(0.5)


def _spec_string_keys(reader) -> set:
    """Every `spec.get("<literal>")` key a reader reads, from its real AST.

    Used to ENUMERATE inputs for the behavioural sweep below — the assertions are all made by
    calling `read_metric` — so a reader that grows a new path slot is exercised without anyone
    remembering to list it here or in `READER_PATH_KEYS`.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(reader).lstrip())
    return {node.args[0].value for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get" and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "spec" and node.args
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)}


@pytest.mark.parametrize("kind", sorted(METRIC_READERS))
def test_no_spec_field_of_any_reader_can_raise_out_of_read_metric(tmp_path, kind):
    """The generalization, and the part that survives a NEW reader: every field a registered reader
    reads out of the spec, given a value of the wrong type, must fail the node rather than the run.

    `READER_PATH_KEYS` is a registry, and a registry nobody re-derives rots — a reader added with a
    `checkpoint` path slot would pass the parametrized test above (which reads the registry) while
    crashing exactly as `adapter` did. This one reads the READER, so it goes red instead.

    `kind` is excluded and is the one genuine gap: an unhashable `kind` raises TypeError out of
    `METRIC_READERS.get(...)` itself, which predates this guard and belongs to the dispatch, not to
    the path slots.
    """
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "ok.json").write_text(json.dumps({"metric": 1.0}), encoding="utf-8")
    for field in sorted(_spec_string_keys(METRIC_READERS[kind]) - {"kind"}):
        for value in _NON_STRINGS:
            read_metric("", str(workdir), {"kind": kind, field: value})   # must not raise


# ------------------------------------------------------------------ the reader table is the registry

def test_the_submit_validator_reads_the_reader_table_not_a_local_copy():
    """RA-04's real complaint: the kinds were enumerated three times, and the copy that CLAIMED to be
    the registry had zero consumers. A reader that exists but is unlisted is unconfigurable; a listed
    kind with no reader is worse — it validates at submit and then returns no metric forever."""
    from looplab.adapters.repo_task import EvalSpec

    for kind in METRIC_READERS:
        # A registered kind must be ACCEPTED — with its own required fields supplied. The `path`
        # requirement is table-driven from the same module for the same reason the kind set is: a
        # literal here would be a fourth parallel enumeration, which is the finding this file pins.
        # (A `file_json` spec with no `path` reads no metric at all and is refused at submit; see
        # tests/test_silent_misconfiguration.py.)
        EvalSpec.model_validate(
            {"command": ["python", "main.py"],
             "metric": {"kind": kind,
                        **({"path": "m.json"} if kind in READERS_REQUIRING_PATH else {})}})
    with pytest.raises(Exception) as exc:
        EvalSpec.model_validate({"command": ["python", "main.py"],
                                 "metric": {"kind": "max"}})
    assert "not a metric reader" in str(exc.value)


def test_an_unknown_kind_still_falls_through_to_no_metric_at_runtime():
    """The dispatch replaced an if-chain whose fall-through was `return None`. A KeyError here would
    turn a bad spec that used to fail ONE node into a crash with no terminal event."""
    assert read_metric("", ".", {"kind": "no_such_reader"}) is None


def test_the_default_kind_is_still_stdout_json():
    assert read_metric(json.dumps({"metric": 0.5}), ".", {}) == pytest.approx(0.5)


@pytest.mark.parametrize("kind", sorted(METRIC_READERS))
def test_every_registered_reader_accepts_the_uniform_call_shape(kind):
    """The table only works if every entry takes the same five arguments; a reader with a different
    signature would raise TypeError on the FIRST real eval that used it, not at import."""
    import inspect

    assert list(inspect.signature(METRIC_READERS[kind]).parameters) == [
        "stdout", "workdir", "spec", "wrap", "since"]


def test_stdout_readers_never_touch_the_filesystem(tmp_path):
    """Pins why only some readers need the guard: the stdout family reads the eval's own output."""
    assert read_metric("METRIC: 0.25", str(tmp_path),
                       {"kind": "stdout_regex", "pattern": r"METRIC: ([0-9.]+)"}) == pytest.approx(
        0.25)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX path semantics")
def test_host_score_still_refuses_labels_inside_the_candidate_workspace(tmp_path):
    """The one place this module asserts the INVERSE of confinement, and it must stay loud rather
    than becoming another silent None: labels inside the workspace are mounted and writable by the
    candidate, which defeats held-out grading entirely."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "predictions.json").write_text("[1.0]", encoding="utf-8")
    (workdir / "labels.json").write_text("[1.0]", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        read_metric("", str(workdir), {"kind": "host_score",
                                       "labels": str(workdir / "labels.json")})
    assert "inside the candidate workspace" in str(exc.value)
