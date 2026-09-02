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
import logging
import os
import sys

import pytest

from looplab.runtime import command_eval
from looplab.runtime.command_eval import (METRIC_READERS, READER_PATH_KEYS,
                                           READERS_REQUIRING_PATH, _confined,
                                           metric_spec_path_error, read_metric)


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
    assert module_source.count("_is_within(") == 4, (
        "expected exactly four: the definition, `_confined`'s use, and the TWO halves of host_score's "
        "labels-must-be-OUTSIDE check — the score-time one (which scores nothing and logs) and the "
        "submit-time `host_score_labels_error` (which refuses before the run starts). Both are the "
        "inverse assertion and deliberately not `_confined`; they are two because the audiences "
        "differ, and a reader inside an eval worker can only ever return None")


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

    `kind` is excluded here because it is not a path slot — it selects the reader rather than being
    read by one, so it belongs to the dispatch. It is covered by the test below instead.
    """
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "ok.json").write_text(json.dumps({"metric": 1.0}), encoding="utf-8")
    for field in sorted(_spec_string_keys(METRIC_READERS[kind]) - {"kind"}):
        for value in _NON_STRINGS:
            read_metric("", str(workdir), {"kind": kind, field: value})   # must not raise


@pytest.mark.parametrize("kind", [["file_json"], {"a": 1}, {"file_json"}, 42, 1.5, b"file_json",
                                  None, True])
def test_a_kind_that_is_not_a_string_fails_the_node_not_the_run(tmp_path, kind):
    """The same defect one layer UP from the path slots, and it was worse in one respect.

    `METRIC_READERS.get(spec["kind"])` is not total over operator- or model-authored JSON: an
    UNHASHABLE kind (`{"kind": ["file_json"]}`) raised `TypeError: unhashable type: 'list'` straight
    out of the dict lookup. That escaped `read_metric` into the eval worker exactly as a non-string
    path slot did — no node terminal, and a run that re-dies on every resume. And unlike the path
    slots it also escaped `metric_spec_path_error`, so the submit-time refusal whose whole job is to
    name a malformed spec crashed on this one instead.

    Both entry points are driven, because the refusal cannot reach a spec reloaded from a run's own
    `task.snapshot.json` (invariant #6) and the runtime abstention cannot name the field for an
    operator who is still authoring one.

    `None` and `True` are in the list for a reason: `None` must NOT resolve to the absent-kind
    default (a JSON `"kind": null` is a mistake, not a request for `stdout_json`), and `True` is a
    hashable non-string that would otherwise sail through an `isinstance(kind, Hashable)` guard.
    """
    workdir = tmp_path / "work"
    workdir.mkdir()
    assert read_metric("", str(workdir), {"kind": kind, "path": "m.json"}) is None
    assert metric_spec_path_error({"kind": kind, "path": "m.json"}) is None
    # Not silently scored by the absent-kind default either: a real metrics.json next door must not
    # turn a malformed kind into a plausible-looking number.
    (workdir / "m.json").write_text(json.dumps({"metric": 1.0}), encoding="utf-8")
    assert read_metric('{"metric": 2.0}', str(workdir), {"kind": kind, "path": "m.json"}) is None


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
        spec = {"kind": kind, **({"path": "m.json"} if kind in READERS_REQUIRING_PATH else {})}
        # `host_score`'s held-out `labels` is the SECOND such requirement, and it is asked the same
        # way: from the rule's own authority, not from a literal here.
        if command_eval.host_score_labels_error(spec):
            spec["labels"] = os.path.join(os.sep, "held-out", "labels.json")
        EvalSpec.model_validate({"command": ["python", "main.py"], "metric": spec})
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
    """The table only works if every entry takes the same arguments; a reader with a different
    signature would raise TypeError on the FIRST real eval that used it, not at import.

    `env` joined the shape on 2026-09-02 and only ONE reader uses it — `_read_adapter`, which EXECs
    a subprocess and used to pass `env=None`, so the one reader that runs candidate-lineage code ran
    OUTSIDE the eval's own environment: no fence marker, no GPU pin, and none of the operator's
    declared `EvalSpec.env`. It is on every reader rather than on that one because the dispatch is
    a single call shape; a per-reader exception would be a second registry answering "does this one
    take an env", which is exactly the shape `READER_PATH_KEYS` exists as a warning about.
    """
    import inspect

    assert list(inspect.signature(METRIC_READERS[kind]).parameters) == [
        "stdout", "workdir", "spec", "wrap", "since", "env"]


def test_only_the_reader_that_EXECS_actually_reads_the_env():
    """`env` is on every signature and used by one. A second reader consulting it would be a new
    way for a metric read to depend on the environment, which is worth noticing.

    AST, so a mention in a comment or docstring does not count as a read.
    """
    import ast
    import inspect
    import textwrap

    users = set()
    for kind, reader in METRIC_READERS.items():
        tree = ast.parse(textwrap.dedent(inspect.getsource(reader)))
        body = [node for node in ast.walk(tree)
                if isinstance(node, ast.Name) and node.id == "env"
                and isinstance(node.ctx, ast.Load)]
        if body:
            users.add(kind)
    assert users == {"adapter"}, f"the env is read by {sorted(users)}"


def test_stdout_readers_never_touch_the_filesystem(tmp_path):
    """Pins why only some readers need the guard: the stdout family reads the eval's own output."""
    assert read_metric("METRIC: 0.25", str(tmp_path),
                       {"kind": "stdout_regex", "pattern": r"METRIC: ([0-9.]+)"}) == pytest.approx(
        0.25)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX path semantics")
def test_host_score_still_refuses_labels_inside_the_candidate_workspace(tmp_path, caplog):
    """The one place this module asserts the INVERSE of confinement — and the one place the module's
    own rule about WHERE to be loud bites the check itself.

    Labels inside the workspace are mounted and writable by the candidate, which defeats held-out
    grading entirely, so this must never become a silent None. It also must never RAISE here: this
    function runs inside the eval worker, whose only handler is `except GpuPinUnenforceable` while
    the dispatchers wrap `_evaluate` in try/FINALLY — measured, the raise reached the top and left
    the node with no terminal event and the run re-dying on every resume. That is precisely the
    failure class the `READER_PATH_KEYS` registry above exists to prevent, and this reader was
    committing it deliberately.

    So the refusal is SPLIT, and both halves are asserted: score time scores nothing and says so at
    ERROR (a node fails, a run does not), and `host_score_labels_error` refuses the same spec before
    the run starts, where an operator can still move the file. Neither alone is the property.
    """
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "predictions.json").write_text("[1.0]", encoding="utf-8")
    (workdir / "labels.json").write_text("[1.0]", encoding="utf-8")
    spec = {"kind": "host_score", "labels": str(workdir / "labels.json")}
    with caplog.at_level(logging.ERROR, logger="looplab.runtime.command_eval"):
        assert read_metric("", str(workdir), spec) is None
    assert "inside the candidate workspace" in caplog.text
    assert "INSIDE the candidate workspace" in command_eval.host_score_labels_error(
        spec, workspace_root=workdir)
    # The whole run root, not just the eval cwd: the untrusted tier bind-mounts it, so a labels file
    # anywhere under it is reachable by the candidate. This is the shape the launch check refuses.
    assert command_eval.host_score_labels_error(spec, workspace_root=tmp_path)
