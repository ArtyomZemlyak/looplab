"""A stage row's `status` is a closed vocabulary, DERIVED from the one writer that mints it.

`_run_stages` is the only producer of these words; every other module in the tree reads them. They
were bare literals across thirteen files, with `RunResult.stages` documenting three of the eight and
`metric_salvage.VETO_STAGE_STATUSES` naming one.

A typo does not fail anywhere. It rides onto the durable `stage_finished` row and then reads as an
unknown status at every consumer: the salvage veto stops vetoing, the UI strip draws no glyph, and
the reuse predicate sees a stage that neither succeeded nor failed. `TRIAGE_ACTIONS` and
`CARD_BUILD_SKIP_REASONS` are the same seam shape and both carry a registry plus a two-way scan.

The scan below is TWO-WAY and derives from the writer's own AST, never from a copy of the list —
a hand-maintained registry drifts in the direction nobody checks, and the direction that matters
here is "the writer mints a word the registry does not have", which no consumer would ever notice.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from looplab.runtime import command_eval
from looplab.runtime.command_eval import (STAGE_FAILED_STATUSES, STAGE_STATUSES, _run_stages)


def _minted_by_the_writer() -> set[str]:
    """Every literal `_run_stages` can put in a row's `status`, read off its AST.

    Three shapes, all live in that function today: a `{"status": "x"}` dict entry, a
    `row["status"] = "x"` assignment, and a conditional bound to a local that is then used as the
    dict's value (`_status = "timeout" if ... else ("ok" if ... else "fail")`).
    """
    tree = ast.parse(inspect.getsource(_run_stages))
    found: set[str] = set()
    status_locals: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if not (isinstance(key, ast.Constant) and key.value == "status"):
                    continue
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found.add(value.value)
                elif isinstance(value, ast.Name):
                    status_locals.add(value.id)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if (isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                        and target.slice.value == "status"
                        and isinstance(node.value.value, str)):
                    found.add(node.value.value)

    # ...and whatever those locals were bound to, including through nested conditionals.
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in status_locals for t in node.targets):
            for inner in ast.walk(node.value):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    found.add(inner.value)
    return found


def test_the_scan_actually_reads_the_writer():
    minted = _minted_by_the_writer()

    assert minted, "the AST scan found no status literal — the guard would be vacuous"
    assert {"reused", "check_failed"} <= minted, (
        f"the scan cannot see statuses it should obviously find: {sorted(minted)}")


def test_the_registry_is_exactly_what_the_writer_mints():
    """MUTATION: mint a `"skipped"` row without registering it -> red, naming it. MUTATION: register
    a word nothing writes -> red the other way."""
    minted = _minted_by_the_writer()

    assert minted == set(STAGE_STATUSES), (
        f"minted-not-registered {sorted(minted - set(STAGE_STATUSES))}, "
        f"registered-not-minted {sorted(set(STAGE_STATUSES) - minted)}")


def test_the_registry_has_no_duplicates_and_is_ordered_by_lifecycle():
    assert len(STAGE_STATUSES) == len(set(STAGE_STATUSES))
    assert STAGE_STATUSES.index("reused") < STAGE_STATUSES.index("ok"), (
        "the order is the story a row tells: skipped, then ran, then its contract was checked")
    assert STAGE_STATUSES.index("ok") < STAGE_STATUSES.index("expect_failed")


def test_ok_and_reused_are_the_only_non_failures():
    """`reused` is a success: it is a previous run of this stage whose result still stands.

    MUTATION: put `reused` in the failed set -> every reused stage reads as a failure, and the
    engine's own reuse optimisation starts looking like a broken pipeline.
    """
    assert STAGE_FAILED_STATUSES == set(STAGE_STATUSES) - {"ok", "reused"}
    assert "reused" not in STAGE_FAILED_STATUSES and "ok" not in STAGE_FAILED_STATUSES


def test_the_salvage_veto_names_a_registered_status():
    """`VETO_STAGE_STATUSES` was the only named subset before this, and it is the reader whose
    silent failure is worst: an unrecognised status simply stops vetoing."""
    from looplab.engine.metric_salvage import VETO_STAGE_STATUSES

    unknown = set(VETO_STAGE_STATUSES) - set(STAGE_STATUSES)
    assert not unknown, (
        f"{sorted(unknown)} is vetoed on but is not a status any stage row can carry, so the veto "
        "can never fire")


def test_the_browsers_own_status_subset_names_only_statuses_the_engine_mints():
    """The cross-language half. `ui/src/stageAttribution.js::STAGE_OK_STATUSES` decides which stage
    rows the strip does NOT paint red, and it is a hand-written list of engine words in another
    language — nothing connects the two, so a renamed status leaves the browser matching on a word
    that can no longer arrive and quietly painting every such row as a failure.

    It asserts only membership, deliberately. WHICH statuses the strip paints red is a UI judgement
    the strip owns and states (`timeout` is amber rather than red — "working", not "broken"), and a
    test that pinned the partition here would be this repo asserting a colour from the wrong side.

    MUTATION: rename a status in the registry without touching the JS -> red, naming the orphan.
    """
    import pathlib
    import re

    strip = pathlib.Path(__file__).resolve().parents[1] / "ui" / "src" / "stageAttribution.js"
    if not strip.exists():
        pytest.skip("the UI strip model is not present in this checkout")
    match = re.search(r"STAGE_OK_STATUSES\s*=\s*\[([^\]]*)\]", strip.read_text(encoding="utf-8"))
    assert match, "STAGE_OK_STATUSES is gone or reshaped; re-point this guard at its replacement"
    js_statuses = set(re.findall(r"'([^']+)'", match.group(1)))

    assert js_statuses, "the JS subset parsed as empty — the guard would be vacuous"
    orphans = js_statuses - set(STAGE_STATUSES)
    assert not orphans, (
        f"{sorted(orphans)} is matched by the stage strip but is not a status the engine mints, so "
        "those rows can never take that branch")
