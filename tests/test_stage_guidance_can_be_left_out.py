"""The Developer's stage-pipeline advice is 5,001 characters nobody used, and now it can be dropped.

MEASURED 2026-08-28 over six probes on the repaired stack: `declare_stages` was called **zero
times**, while the block that teaches it sat in every `plan` / `plan_step` / `card_build` system
prompt -- 167, 143, 176 and 233 generations on dsFix1, dsFix2, dsPyx and dsN3b, costing $0.057,
$0.049, $0.060 and $0.080, i.e. 4.8-6.0 % of each run and about a sixth of a node at the measured
$0.35/node. It is advice about GPU training, checkpoints, shards and `train.py`, addressed to a
role whose task declares one stage called `score`.

DEFAULT ON, and that is a contract rather than caution: `_system_body` must reproduce the
historical prompt BYTE FOR BYTE when `developer_probe=False`, pinned by
`LEGACY_CONFIG_SNAPSHOT_DEFAULTS`, so a resumed pre-2026-08-13 run keeps the prompt its first half
ran under.

The cut is a slice between two sentinels rather than a hoist into a separate constant: three
attempts at the hoist broke the adjacent-string literal that builds the body. A slice cannot, and
when a sentinel stops matching the body is returned UNCHANGED -- an operator gets the historical
prompt, never a mangled one.
"""
from __future__ import annotations

import sys
from pathlib import Path

from looplab.adapters.repo_developer import LLMRepoDeveloper
from looplab.adapters.repo_task import EvalSpec, RepoTask


def _task(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text("print('x')\n", encoding="utf-8")
    return RepoTask(id="r", goal="g", direction="max", editable_path=str(root),
                    edit_surface=["*.py"], protect=[], eval=EvalSpec(command=[sys.executable, "main.py"]))


def _body(root: Path, **kw) -> str:
    dev = LLMRepoDeveloper(object(), _task(root), **kw)
    return dev._system_body(lambda _store, _key, default: default)


def _developer_from_settings(**fields) -> LLMRepoDeveloper:
    """The in-house repo Developer as `make_roles` builds it, from real `Settings`.

    `in_house_repo_developer` is the ONE gate that turns a setting into this role's behaviour
    (`agents/developer_backends.py`), and it is what `agents/factory.py::make_roles` calls, so
    driving it covers the whole path from the operator's field to the constructor keyword.
    """
    from looplab.adapters.tasks import load_task
    from looplab.agents.developer_backends import in_house_repo_developer
    from looplab.core.config import Settings

    task = load_task(Path(__file__).resolve().parents[1] / "examples" / "repo_task.json")
    settings = Settings(backend="llm", unified_agent=False, **fields)
    return in_house_repo_developer(task, settings, client=None, param_search=False, established=None)


def test_the_block_is_present_by_default(tmp_path):
    body = _body(tmp_path / "a")
    assert "TRAIN-THEN-SCORE PIPELINE" in body
    assert "For a ROUTINE hyperparameter experiment" in body


def test_turning_it_off_removes_the_block_and_keeps_the_rest(tmp_path):
    on = _body(tmp_path / "b")
    off = _body(tmp_path / "c", stage_guidance=False)
    assert "TRAIN-THEN-SCORE PIPELINE" not in off
    assert "For a ROUTINE hyperparameter experiment" in off, "only the pipeline block goes"
    assert "ALWAYS use REPO-RELATIVE paths" in off, "the text before it must survive"
    assert len(on) - len(off) > 4000, f"expected ~5k characters removed, got {len(on) - len(off)}"


def test_the_default_body_is_byte_identical_to_the_historical_one(tmp_path):
    """The contract `_system_body` states: `developer_probe=False` reproduces the old prompt."""
    explicit_on = _body(tmp_path / "d", stage_guidance=True)
    implicit = _body(tmp_path / "e")
    assert explicit_on == implicit


def test_a_missing_sentinel_returns_the_body_untouched(tmp_path):
    """Fail toward the historical prompt, never toward a mangled one."""
    dev = LLMRepoDeveloper(object(), _task(tmp_path / "f"), stage_guidance=False)
    text = "a body that mentions neither marker"
    assert dev._drop_stage_guidance(text) == text
    only_open = "... TRAIN-THEN-SCORE PIPELINE and then nothing else"
    assert dev._drop_stage_guidance(only_open) == only_open


def test_the_setting_reaches_the_developer_through_the_factory():
    """The operator's setting must actually reach the constructor.

    This was a source pin on `agents/factory.py` until 2026-09-08, when doc 25 RA-01's second half
    moved the in-house repo Developer's wiring into `agents/developer_backends.py` and the pin went
    looking for a line that had legitimately moved one module over. A pin that follows a rename is
    a pin that can be satisfied by a comment, so drive the property instead (CLAUDE.md's tier 1):
    call the real gate `make_roles` calls, with real `Settings`, and read the setting back off the
    role it built. Delete the keyword at ANY of those hops and this goes red.
    """
    off = _developer_from_settings(developer_stage_guidance=False)
    assert off._stage_guidance is False, "the operator's setting must actually reach the constructor"
    assert _developer_from_settings()._stage_guidance is True, "and the default must stay ON"
