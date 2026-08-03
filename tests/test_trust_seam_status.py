"""The trust package's library surfaces, declared rather than accumulated (doc 25 CT-12).

Four surfaces in `looplab/trust/` have no production consumer. Each is a documented seam, so none is
deletion-on-sight — but docs/17 already listed the cv splitters as "tested, no live caller" three
weeks before doc 25 said the same thing, which is what silent accumulation looks like.

This file turns that into a DECLARED set. A surface listed here is a deliberate seam whose status
someone chose; a surface that gains a production caller fails the test and gets moved out of the
list, and a NEW unconsumed surface has to be added to it on purpose. That is the "decision point"
the finding asks for, and unlike a note in a review doc it cannot go quietly stale.

The one concrete DEFECT the finding names — `harden`'s rule naming using the per-interpreter
`hash()` — is fixed, and pinned below.
"""
from __future__ import annotations

import inspect
import re

from _source_scan import PKG, iter_sources

# name -> why it exists with no live caller. Every entry is a choice, not an oversight.
UNCONSUMED_TRUST_SEAMS = {
    "kfold_indices": "cv splitter; awaits a temporal TaskAdapter that needs purged/embargoed folds",
    "purged_walk_forward": "cv splitter; same — a leakage-safe split needs a task that has time",
    "consistent_cv": "cv splitter; same — it compares folds a temporal adapter would produce",
    "calibrate_detector": "operator harness for tuning reward-hack thresholds against a corpus",
    "SEED_CALIBRATION_CORPUS": "the fixture that harness calibrates against",
}


def _production_callers(name: str) -> set[str]:
    """Files under `looplab/` that CALL or import *name*, excluding where it is defined."""
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    hits = set()
    for path, source in iter_sources():
        rel = path.relative_to(PKG).as_posix()
        if not pattern.search(source):
            continue
        # A definition or a re-export in the owning module is not a consumer.
        if re.search(rf"^(def|class)\s+{re.escape(name)}\b|^{re.escape(name)}\s*[:=]",
                     source, re.M):
            continue
        hits.add(rel)
    return hits


def test_every_declared_seam_still_has_no_production_caller():
    """If one gained a caller, it stopped being a seam and the list is now lying about it."""
    wired = {name: sorted(_production_callers(name)) for name in UNCONSUMED_TRUST_SEAMS}
    wired = {name: files for name, files in wired.items() if files}
    assert wired == {}, (
        f"these are wired now and must leave UNCONSUMED_TRUST_SEAMS: {wired}")


def test_the_declared_seams_all_still_exist():
    """The other direction: a seam deleted without updating the list leaves a stale decision."""
    from looplab.trust import cv, reward_hack

    for name in ("kfold_indices", "purged_walk_forward", "consistent_cv"):
        assert hasattr(cv, name), f"{name} is listed as a seam but no longer exists"
    for name in ("calibrate_detector", "SEED_CALIBRATION_CORPUS"):
        assert hasattr(reward_hack, name), f"{name} is listed as a seam but no longer exists"


def test_each_declared_seam_says_why_it_is_kept():
    assert set(UNCONSUMED_TRUST_SEAMS) == {
        "kfold_indices", "purged_walk_forward", "consistent_cv",
        "calibrate_detector", "SEED_CALIBRATION_CORPUS"}
    for name, reason in UNCONSUMED_TRUST_SEAMS.items():
        assert len(reason) > 20, f"{name}'s reason is too thin to be a decision"


# ------------------------------------------------------------------ the concrete defect

def test_an_llm_found_exploit_gets_a_process_stable_name():
    """`hash()` is SALTED per interpreter. The name goes into a DURABLE rule suite that later runs
    read back, so a per-process value made the same exploit a new rule every time — the suite
    accumulated duplicates and `add`'s idempotence could never fire."""
    from looplab.trust.harden import _exploit_digest

    code = "import grader\nprint(grader.score(grader._Y))"
    assert _exploit_digest(code) == _exploit_digest(code)
    assert len(_exploit_digest(code)) == 12
    assert _exploit_digest(code) != _exploit_digest(code + " ")
    assert _exploit_digest("") == _exploit_digest(""), "an empty probe must not raise"


def test_the_digest_survives_a_subprocess_which_is_the_whole_point():
    import subprocess
    import sys

    probe = (
        "from looplab.trust.harden import _exploit_digest;"
        "print(_exploit_digest('import grader'))"
    )
    first = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": "1"})
    second = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": "2"})
    assert first.returncode == 0 and second.returncode == 0, (first.stderr, second.stderr)
    assert first.stdout.strip() == second.stdout.strip() != ""


def test_harden_no_longer_names_a_rule_from_a_salted_hash():
    from looplab.trust import harden

    source = inspect.getsource(harden)
    assert "abs(hash(" not in source
    assert "_exploit_digest(code)" in source


def test_a_repeated_llm_exploit_adds_exactly_one_rule():
    """End to end: the same exploit found twice must not become two durable rules."""
    from looplab.trust.harden import ExploitSuite, harden

    suite = ExploitSuite()
    exploit = "import grader_secret_module"
    first = harden(suite, hacker=lambda: [exploit], detector=lambda _c: [])
    second = harden(suite, hacker=lambda: [exploit], detector=lambda _c: [])
    assert len(first["added"]) == 1
    assert second["added"] == [], "the second sighting must be recognized as the same exploit"
    assert len(suite.patterns) == 1
    assert suite.patterns[0]["name"].startswith("exploit_")
