"""Read-only repo scouts for the repo Developer — now REUSING the shared RepoScoutTools
(read_file / grep / find_files / list_dir) instead of a bespoke copy. These close the gap that made
the developer GUESS a repo file's CLI flags (the embedded source is truncated) — the direct cause of
the --grad_clip crash on node 35. RepoScoutTools adds a content `grep` and repo-relative paths
(`default_root`) on top of its existing path-safety + secret-filtering; env_inspect fuzzy suggestion
is covered here too."""
from __future__ import annotations

from looplab.tools.env_inspect import EnvInspectTools, _suggest
from looplab.tools.reposcout import RepoScoutTools


def _scout(tmp_path):
    (tmp_path / "train.py").write_text(
        "import argparse\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--gradient_clip_val', type=float)\n"
        "p.add_argument('--cycle', action='store_true')\n", encoding="utf-8")
    (tmp_path / "model.py").write_text("class Net:\n    pass\n", encoding="utf-8")
    (tmp_path / "ckpt").mkdir()               # heavy dir must be pruned by grep
    (tmp_path / "ckpt" / "big.py").write_text("SHOULD_NOT_BE_GREPPED = 1\n", encoding="utf-8")
    # repo-relative paths (train.py) resolve against default_root — matching write_file's interface
    return RepoScoutTools(roots=[str(tmp_path)], default_root=str(tmp_path))


def test_read_file_repo_relative(tmp_path):
    s = _scout(tmp_path)
    out = s.execute("read_file", {"path": "train.py"})      # repo-relative, not absolute
    assert "--gradient_clip_val" in out and "--cycle" in out
    assert "not read" not in out


def test_grep_finds_the_flag(tmp_path):
    s = _scout(tmp_path)
    g = s.execute("grep", {"pattern": "add_argument", "glob": "*.py"})
    assert "train.py:3" in g and "gradient_clip_val" in g   # the info minimax lacked
    assert "not found" in s.execute("grep", {"pattern": "definitely_absent_zzz"})


def test_grep_prunes_heavy_dirs(tmp_path):
    s = _scout(tmp_path)
    # ckpt/ is a pruned dir -> its file is invisible to grep (a clean "not found", not the ckpt hit)
    assert "not found" in s.execute("grep", {"pattern": "SHOULD_NOT_BE_GREPPED"})


def test_grep_and_read_refuse_secrets(tmp_path):
    """The reuse win: RepoScoutTools hides credential files — a bespoke scout would have slurped a
    repo's .env into the (possibly remote) model context."""
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-secret-123\n", encoding="utf-8")
    (tmp_path / "train.py").write_text("x = 1\n", encoding="utf-8")   # no secret here
    s = RepoScoutTools(roots=[str(tmp_path)], default_root=str(tmp_path))
    # the secret lives ONLY in .env -> grep never surfaces it (the file is skipped, not walked)
    assert "sk-secret-123" not in s.execute("grep", {"pattern": "sk-secret"})
    assert "refused" in s.execute("read_file", {"path": ".env"}).lower()      # read is refused outright


def test_overlay_wins_over_disk(tmp_path):
    """The whole point for the Developer: read/grep see the code it is EDITING (staged) — not the
    pristine on-disk repo. The overlay is a live dict, so a later edit is visible immediately."""
    (tmp_path / "train.py").write_text("p.add_argument('--old_flag')\n", encoding="utf-8")
    staged: dict[str, str] = {}
    s = RepoScoutTools(roots=[str(tmp_path)], default_root=str(tmp_path), overlay=staged)
    # before any edit: reads the pristine disk file
    assert "--old_flag" in s.execute("read_file", {"path": "train.py"})
    # the Developer edits train.py + writes a new entrypoint (mutating the SAME dict)
    staged["train.py"] = "p.add_argument('--new_flag')\n"
    staged["entry.py"] = "MARKER = 1\n"
    assert "--new_flag" in s.execute("read_file", {"path": "train.py"})     # staged wins
    assert "--old_flag" not in s.execute("read_file", {"path": "train.py"})
    assert "MARKER" in s.execute("read_file", {"path": "entry.py"})         # a staged-only new file
    # grep: staged content wins, and the pristine train.py isn't double-counted
    g = s.execute("grep", {"pattern": "add_argument"})
    assert "train.py:1" in g and "--new_flag" in g and "--old_flag" not in g


def test_developer_wires_reused_scout_with_overlay(tmp_path):
    """The repo Developer exposes RepoScoutTools bound to its editable roots, with the write tools'
    live `files` dict as the STAGED overlay — verifying the reuse (not a re-implementation) AND that a
    session reads the code it is currently writing."""
    from looplab.adapters.repo_developer import LLMRepoDeveloper, RepoWriteTools
    (tmp_path / "train.py").write_text("p.add_argument('--lr')\n", encoding="utf-8")
    dev = LLMRepoDeveloper.__new__(LLMRepoDeveloper)      # bypass __init__/LLM
    dev._editables = [{"name": ".", "path": str(tmp_path)}]
    write = RepoWriteTools(surface=["**/*.py"], protected=[],
                           editables=[{"name": ".", "path": str(tmp_path)}])
    scouts = dev._scout_tools(write)
    assert len(scouts) == 1 and isinstance(scouts[0], RepoScoutTools)
    names = {sp["function"]["name"] for sp in scouts[0].specs()}
    assert {"read_file", "grep", "find_files", "list_dir"} <= names
    assert "--lr" in scouts[0].execute("grep", {"pattern": "add_argument"})   # reads the disk repo
    # a write flows through the SHARED files dict into the scout's overlay
    write.execute("write_file", {"path": "entry.py", "content": "STAGED = 2\n"})
    assert "STAGED" in scouts[0].execute("read_file", {"path": "entry.py"})
    # no editables -> no scout (a bare/legacy developer degrades gracefully)
    dev._editables = []
    assert dev._scout_tools(write) == []


def test_staged_deletion_hidden_from_scouts(tmp_path):
    # a file deleted THIS session (but still on the editable-root disk) must read/grep/list as gone —
    # the scouts reflect the STAGED tree, not the pristine repo. `deleted` is a live list, so a later
    # delete takes effect at once. (Reused equivalent of the old bespoke deletion handling.)
    (tmp_path / "train.py").write_text("p.add_argument('--gone')\n", encoding="utf-8")
    (tmp_path / "model.py").write_text("KEEP = 1\n", encoding="utf-8")
    deleted: list[str] = []
    s = RepoScoutTools(roots=[str(tmp_path)], default_root=str(tmp_path), deleted=deleted)
    assert "--gone" in s.execute("grep", {"pattern": "add_argument"})       # present before the delete
    deleted.append("train.py")
    assert "deleted this session" in s.execute("read_file", {"path": "train.py"})   # read reports gone
    assert "not found" in s.execute("grep", {"pattern": "add_argument"})    # grep can't resurface it
    assert "KEEP" in s.execute("grep", {"pattern": "KEEP"})                 # siblings unaffected
    assert "train.py" not in s.execute("find_files", {"root": str(tmp_path), "pattern": "*.py"})


def test_grep_installed_honors_a_submodule_scope():
    # scoping to a SUBMODULE actually narrows the walk: `def detect_encoding` lives in json/__init__.py,
    # so a search scoped to json.decoder must NOT find it (the old _top-stripping searched all of json).
    t = EnvInspectTools()
    assert "detect_encoding" in t.execute("grep_installed",
                                          {"query": "def detect_encoding", "package": "json"})
    scoped = t.execute("grep_installed", {"query": "def detect_encoding", "package": "json.decoder"})
    assert "not found" in scoped and "json/__init__" not in scoped


def test_env_suggest_points_at_the_real_name():
    # a near-miss of an installed package suggests it ('pytes' -> 'pytest', always installed in dev)
    hint = _suggest("pytes")
    assert "pytest" in hint
    out = EnvInspectTools().execute("pkg_info", {"name": "pytes"})
    assert "not installed" in out and "pytest" in out
