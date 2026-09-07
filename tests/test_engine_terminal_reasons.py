"""The words the ENGINE mints on a terminal are a registry, and a registered word must be minted.

`FAILURE_REASONS` says how an EVALUATION can fail. A node can also end without its evaluation having
failed at all — no GPU on the box, the operator aborted it, the Card behind it was dropped — and
those eleven words were bare literals at every write and read site across roughly eighteen modules,
with no registry and no scan. `TRIAGE_ACTIONS` is the same seam shape and has both.

WHAT THE ABSENCE COST, exactly: two readers spell the benign subset independently — `events/replay.py`
excludes it from the failure-spike count, `serve/attention.py` from the owner alert — and both listed
`cancelled`, which NO terminal writer mints. Each carried a word that could never match, in a set
whose whole job is matching, and neither could tell. `cancelled` is minted in this tree, which is why
it looked plausible: `serve/jobs.py` for a launch and `serve/assistant_watch.py` for a watch. Neither
is a node terminal.

The registry is only half the fix. The other half is that the two readers DERIVE from one shared
subset rather than spelling their own, because a registry both of them ignore is a fourth spelling.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from _source_scan import iter_sources

from looplab.core.models import (BENIGN_TERMINAL_REASONS, ENGINE_TERMINAL_REASONS,
                                 FAILURE_REASONS)

LOOPLAB = pathlib.Path(__file__).resolve().parents[1] / "looplab"
_REGISTRY_FILE = LOOPLAB / "core" / "models.py"


def _minted_literals() -> set[str]:
    """Every string a call site writes AS A TERMINAL `reason`, anywhere in `looplab/`.

    NOT "every string constant in the package", which is what this was and which made the guard
    vacuous for the exact word it was written about. That scan found 20,227 strings, `"cancelled"`
    among them — `serve/jobs.py`'s launch state and `assistant_watch.py`'s watch status, neither of
    them a node terminal — so the mutation this file documents ("add `cancelled` back to
    `ENGINE_TERMINAL_REASONS` -> red, naming it") was GREEN, and `test_the_scan_is_not_vacuous`'s
    `len(minted) > 1000` bar guaranteed any short word would pass.

    So the scan reads the WRITE, the shape `CARD_BUILD_SKIP_REASONS`' guard uses:

      * `{... "reason": "<word>" ...}` — a dict literal carrying the key, which is how every
        `store.append(EV_NODE_FAILED, {...})` names it;
      * `<expr>["reason"] = "<word>"` — the same fact assigned after the dict was built;
      * `reason="<word>"` / `terminal_reason="<word>"` — the keyword forms;
      * `reason = "<word>"` — a local whose NAME ends in `reason`, because a real writer routinely
        computes the word first and appends it a hundred lines later (`evaluate.py`'s
        `_kreason = str(... or "monitor_broken")`), or the same write on the attempt record the
        `_eval_*` phases share (`a.reason = "idea_rejected"`).

    A ternary, an `or`, or a `str(...)` wrapper in any of those positions contributes every branch,
    because every branch is a reachable write. Everything else in the package is invisible to it,
    which is the point: `serve/jobs.py`'s `launch_state = "cancelled"` and `assistant_watch.py`'s
    `status="cancelled"` are neither a `reason` key nor a `*reason` name, so the word they share
    with a retired terminal no longer stands in as proof that something mints it.
    """
    registry_lines = set()
    src = _REGISTRY_FILE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and node.target.id in ("ENGINE_TERMINAL_REASONS", "BENIGN_TERMINAL_REASONS")):
            registry_lines.update(range(node.lineno, node.end_lineno + 1))

    def _strings(value) -> set[str]:
        """The string constants this expression can evaluate to — both arms of a choice."""
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return {value.value}
        if isinstance(value, ast.IfExp):
            return _strings(value.body) | _strings(value.orelse)
        if isinstance(value, ast.BoolOp):
            return set().union(*(_strings(v) for v in value.values))
        if (isinstance(value, ast.Call) and getattr(value.func, "id", None) == "str"
                and value.args):
            return _strings(value.args[0])       # `str(x or "monitor_broken")`
        return set()

    def _is_reason_name(node) -> bool:
        # A bare local, or an attribute of the attempt record the `_eval_*` phases share since the
        # EvalAttempt split (`a.reason = "idea_rejected"`, doc 52 row 21): the same writer, boxed.
        name = (node.id if isinstance(node, ast.Name)
                else node.attr if isinstance(node, ast.Attribute) else "")
        return name.rstrip("_").lower().endswith("reason")

    found: set[str] = set()
    for path, source in iter_sources(LOOPLAB):
        try:
            file_tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(file_tree):
            if path == _REGISTRY_FILE and getattr(node, "lineno", -1) in registry_lines:
                continue
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "reason":
                        found |= _strings(value)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if (isinstance(target, ast.Subscript)
                            and isinstance(target.slice, ast.Constant)
                            and str(target.slice.value).endswith("reason")):
                        found |= _strings(node.value)
                    elif _is_reason_name(target) and node.value is not None:
                        found |= _strings(node.value)
            elif isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg and kw.arg.endswith("reason"):
                        found |= _strings(kw.value)
    return found


def test_the_scan_is_not_vacuous():
    """Two bars, and the SECOND one is what the first could not give.

    `len(minted) > 1000` over every string in the package was not a non-vacuity check at all: it
    guaranteed that any short word would be found, which is precisely how the documented mutation
    below came to be green. A scan of the WRITES is small by construction, so what has to be shown
    is that it sees real writers and does NOT see a word used elsewhere for something else.
    """
    minted = _minted_literals()

    assert "gpu_unavailable" in minted, "the scan cannot see a reason it should obviously find"
    assert "frozen" in minted and "superseded" in minted, (
        "nor the pair `_fail_reserved_build` chooses between — a ternary is two writes")
    assert "crash" in minted, "and it must reach the FAILURE_REASONS writers too"
    assert "cancelled" not in minted, (
        "THE VACUITY: `serve/jobs.py`'s launch state and `assistant_watch.py`'s watch status are "
        "both the string `cancelled`, and neither is a node terminal. A scan of every constant in "
        "the package found them and made this file's own documented mutation pass.")


@pytest.mark.parametrize("reason", ENGINE_TERMINAL_REASONS)
def test_every_registered_reason_is_actually_minted(reason):
    """A registered word nobody writes is exactly the `cancelled` defect, pre-registered.

    MUTATION: add "cancelled" back to `ENGINE_TERMINAL_REASONS` -> red, naming it.
    """
    assert reason in _minted_literals(), (
        f"{reason!r} is registered as a terminal reason but appears nowhere in looplab/ outside the "
        "registry — either a writer was removed or the word never existed")


def test_the_two_vocabularies_stay_separate():
    """`FAILURE_REASONS` gates `Settings.inline_repair_reasons` and the repair loop. A node the
    engine SUPERSEDED is not a node whose code can be repaired, so merging them would offer repair
    for an outcome no repair can address.

    MUTATION: fold one into the other -> red.
    """
    overlap = set(ENGINE_TERMINAL_REASONS) & set(FAILURE_REASONS)

    assert not overlap, f"a reason is in both vocabularies: {sorted(overlap)}"


def test_benign_is_a_subset_of_the_registry():
    assert BENIGN_TERMINAL_REASONS <= set(ENGINE_TERMINAL_REASONS), (
        f"{sorted(BENIGN_TERMINAL_REASONS - set(ENGINE_TERMINAL_REASONS))} is filtered as benign but "
        "is not a terminal reason at all")


def test_both_readers_derive_the_benign_set_rather_than_spelling_it():
    """THE ACTUAL DEFECT. Two hand-written copies is how one dead word survived in both.

    MUTATION: re-inline either set -> they drift again, and nothing notices until someone counts.
    """
    from looplab.events import replay
    from looplab.serve import attention

    assert set(replay._FAILURE_SPIKE_IGNORED_REASONS) == set(BENIGN_TERMINAL_REASONS)
    assert set(attention._IGNORED_FAILURE_REASONS) == set(BENIGN_TERMINAL_REASONS)
    assert "cancelled" not in replay._FAILURE_SPIKE_IGNORED_REASONS, (
        "no terminal writer mints `cancelled`; it can never match and must not read as if it could")


def test_a_frozen_build_is_benign_to_BOTH_readers():
    """The one word the two sets disagreed about, and the disagreement was the bug.

    `attention.py` already excluded `frozen` with its reason written down — a speculative build
    frozen by a transient pause/stop/budget crossing keeps its Card for a later resume — while the
    failure-spike filter counted it as a failure. One judgement, so one answer.
    """
    from looplab.events import replay
    from looplab.serve import attention

    assert "frozen" in replay._FAILURE_SPIKE_IGNORED_REASONS
    assert "frozen" in attention._IGNORED_FAILURE_REASONS
