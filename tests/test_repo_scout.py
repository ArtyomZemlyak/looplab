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


def test_disk_hits_are_repo_relative_for_the_developer(tmp_path):
    """A grep/find hit in a PRISTINE on-disk file must come back REPO-RELATIVE ("train.py"), NOT
    absolute ("/abs/train.py") — the Developer feeds that exact path straight into edit_file, whose
    _safe_rel REJECTS absolutes. (The staged-overlay hits were already relative; disk hits must match
    so grep→edit round-trips. The old substring assertions missed this — "train.py:3" is a substring
    of the absolute path too.)"""
    (tmp_path / "train.py").write_text("p.add_argument('--lr')\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "m.py").write_text("p.add_argument('--wd')\n", encoding="utf-8")
    dev = RepoScoutTools(roots=[str(tmp_path)], default_root=str(tmp_path))
    g = dev.execute("grep", {"pattern": "add_argument"})
    assert "train.py:1:" in g and "sub/m.py:1:" in g
    assert str(tmp_path) not in g                 # NOT the absolute prefix
    f = dev.execute("find_files", {"root": str(tmp_path), "pattern": "**/*.py"})
    assert "train.py" in f.splitlines() and "sub/m.py" in f.splitlines()   # bare repo-relative lines
    # the boss (no default_root, unrelated roots) keeps ABSOLUTE paths — unambiguous across roots
    boss = RepoScoutTools(roots=[str(tmp_path)])
    assert str(tmp_path) in boss.execute("grep", {"pattern": "add_argument"})


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


def test_grep_refuses_a_symlink_out_of_root(tmp_path):
    """SECURITY (mega-review 07-06): `grep` walks with os.walk + open(), both of which follow symlinks.
    An innocuously-named link inside the repo (configs/data.json -> a secret OUTSIDE the roots) must be
    RE-VALIDATED on its RESOLVED target — else _looks_secret (which sees only the link's own name) is
    fooled and the off-sandbox file leaks into the hits fed to a (possibly remote) model. read_file and
    find_files already resolve-then-validate; grep must match them."""
    import os
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "train.py").write_text("p.add_argument('--lr')\n", encoding="utf-8")
    outside = tmp_path / "outside_secret.txt"                    # a sibling of repo/ — OUTSIDE the root
    outside.write_text("LEAKED_TOKEN = 'abc123'\n", encoding="utf-8")
    cfg = repo / "configs"
    cfg.mkdir()
    try:
        os.symlink(outside, cfg / "data.json")                  # innocuous name + readable extension
    except (OSError, NotImplementedError):
        import pytest
        pytest.skip("filesystem does not support symlinks")
    s = RepoScoutTools(roots=[str(repo)], default_root=str(repo))
    g = s.execute("grep", {"pattern": "LEAKED_TOKEN", "glob": "*"})
    # assert on the secret VALUE (not the pattern name, which the "not found" message echoes back):
    # a leak would surface the line `configs/data.json:1: LEAKED_TOKEN = 'abc123'`.
    assert "abc123" not in g                                    # the out-of-root secret never surfaces
    assert "not found" in g                                     # the symlink was skipped, not walked
    assert "--lr" in s.execute("grep", {"pattern": "add_argument"})   # legit in-root grep still works


def test_multi_editable_hits_are_name_prefixed_and_dedup(tmp_path):
    """MULTI-editable (mega-review 07-06): the write tools key staged files `<name>/rel`, so a scout
    grep/find hit must come back in the SAME shape or it can't round-trip into edit_file. Before the fix
    _disp rendered relative_to(roots[0]) — BARE for the first root, ABSOLUTE (relative_to raises) for a
    secondary root — and the dedup key never matched the overlay, so an already-edited file was
    re-grepped from PRISTINE disk."""
    a = tmp_path / "repoA"
    a.mkdir()
    (a / "train.py").write_text("p.add_argument('--old')\n", encoding="utf-8")
    b = tmp_path / "repoB"
    b.mkdir()
    (b / "util.py").write_text("HELPER = 1  # add_argument marker\n", encoding="utf-8")
    named = [("repoA", str(a)), ("repoB", str(b))]
    staged: dict[str, str] = {}
    s = RepoScoutTools(roots=[str(a), str(b)], default_root=str(a), overlay=staged, named_roots=named)
    g = s.execute("grep", {"pattern": "add_argument"})
    assert "repoA/train.py:1:" in g                              # first root — NAME-prefixed, round-trips
    f = s.execute("find_files", {"root": str(b), "pattern": "**/*.py"})
    assert "repoB/util.py" in f.splitlines()                     # secondary root — prefixed, not absolute
    assert str(tmp_path) not in f
    # edit repoA/train.py via the overlay using the WRITE-tool key shape -> pristine must NOT re-appear
    staged["repoA/train.py"] = "p.add_argument('--new')\n"
    g2 = s.execute("grep", {"pattern": "add_argument"})
    assert "--new" in g2 and "--old" not in g2                   # staged wins
    assert g2.count("repoA/train.py:1:") == 1                    # deduped — no pristine duplicate


def test_default_root_branch_still_refuses_traversal(tmp_path):
    """The repo-relative `default_root` branch (tried FIRST for a relative path) must not open an escape:
    a `..` climb or an out-of-root absolute path is still refused (mega-review 07-06 — the sibling
    traversal test uses NO default_root, so this branch was untested)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "train.py").write_text("x = 1\n", encoding="utf-8")
    secret = tmp_path / "secret.txt"                        # a sibling of repo/, outside the root
    secret.write_text("TOKEN = 'nope'\n", encoding="utf-8")
    s = RepoScoutTools(roots=[str(repo)], default_root=str(repo))
    climb = s.execute("read_file", {"path": "../secret.txt"})
    assert "not allowed" in climb.lower() and "TOKEN" not in climb     # `..` escape refused
    absolute = s.execute("read_file", {"path": str(secret)})
    assert "not allowed" in absolute.lower() and "TOKEN" not in absolute   # out-of-root absolute refused
    assert "x = 1" in s.execute("read_file", {"path": "train.py"})    # the legit repo-relative read works


def test_grep_max_hits_is_clamped(tmp_path):
    """A model-supplied max_hits can't disable the cap (mega-review 07-06): it's clamped to 200."""
    (tmp_path / "big.py").write_text(
        "\n".join(f"x{i} = 1  # add_argument" for i in range(500)), encoding="utf-8")
    s = RepoScoutTools(roots=[str(tmp_path)], default_root=str(tmp_path))
    g = s.execute("grep", {"pattern": "add_argument", "max_hits": 100000})
    assert "capped at 200 hits" in g


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
