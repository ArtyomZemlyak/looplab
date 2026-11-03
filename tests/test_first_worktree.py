from __future__ import annotations

import pytest

from looplab.engine.code_worktree import OverlayRefusal, add_worktree, worktree_guide
from tests.test_stage_reset_reuse import _evaluate, _run


def test_missing_repo_explains_the_first_real_candidate_path(tmp_path):
    with pytest.raises(OverlayRefusal) as caught:
        add_worktree(tmp_path, "first")
    assert caught.value.code == "code_repo_missing"
    message = str(caught.value)
    assert "first real candidate" in message
    assert "pre_stages" in message and "workspace_seeded" in message


def test_first_real_candidate_creates_the_base_for_a_worktree(tmp_path):
    engine, _calls = _run(tmp_path)
    _evaluate(engine)
    meta = add_worktree(engine.run_dir, "first")
    assert meta["branch"] == "agent/first"
    assert meta["start_commit"]
    assert worktree_guide(meta)["submit"].startswith("submit_experiment")
