"""`RepoTools` is one walker over `RepoScoutTools`, not a second one (doc 25 TO-06).

Two read-only repo browsers evolved side by side — `RepoTools` (Researcher-facing: repo_grep /
repo_list / repo_read over named mounts) and `RepoScoutTools` (boss/Developer-facing: grep /
find_files / read_file over named roots) — and their guards drifted ONE way: every difference was a
protection `RepoTools` lacked. No file budget where `_grep` stops after 4 000 files and says so; no
skip-dirs; no per-file size skip; a silent 40-hit cut where `_grep` clamps and emits
`(capped at N hits)`, so an overflowing search read as an exhaustive one.

Three things had to survive the merge and are pinned below, because each is a place where "delegate
it" would have quietly changed what the Researcher can see:

* the `<repo>/<path>` mount prefix — `RepoScoutTools._resolve` knows `default_root` and CWD, not
  named mounts;
* recursive globbing — `repo_list` has always walked the whole tree, `_find_files` runs a pathlib
  glob where `*.py` is one level;
* `.git` — `_pathsafe.looks_secret` does not know it, so the scout's own gate would pass a
  credentialed clone's `.git/config`.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from looplab.tools.knowledge_tools import RepoTools, _recursive_glob
from looplab.tools.reposcout import RepoScoutTools


def _repo(tmp_path):
    (tmp_path / "model.py").write_text("def train(lr):\n    return lr * 2\n", encoding="utf-8")
    pkg = tmp_path / "pkg"; pkg.mkdir()
    (pkg / "util.py").write_text("HELPER = 1\n", encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------------------------
# One walker
# --------------------------------------------------------------------------------------------

def test_the_reader_owns_no_walk_of_its_own(tmp_path):
    """The finding's shape: `RepoTools` composes the scout instead of re-implementing it."""
    tools = RepoTools([{"name": ".", "path": str(_repo(tmp_path))}])
    assert isinstance(tools._scout, RepoScoutTools)

    source = textwrap.dedent(inspect.getsource(RepoTools.execute))
    calls = {ast.unparse(n.func) for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)}
    for own_walker in ("grep", "glob_files", "read_file"):
        assert own_walker not in calls, f"`execute` walks the tree itself again via {own_walker}"


def test_a_grep_that_overflows_says_so_instead_of_reading_as_exhaustive(tmp_path):
    """The concrete loss: 40 hits with no receipt is indistinguishable from "that is all there is"."""
    repo = tmp_path / "repo"; repo.mkdir()
    for i in range(60):
        (repo / f"f{i:03d}.py").write_text("NEEDLE = 1\n", encoding="utf-8")
    out = RepoTools([{"name": ".", "path": str(repo)}]).execute("repo_grep", {"pattern": "NEEDLE"})
    assert "capped at" in out, out[-200:]


def test_the_scan_budget_and_size_skip_now_apply(tmp_path):
    """Inherited from the scout — `RepoTools` had neither, so one huge mount stalled the grep."""
    source = inspect.getsource(RepoScoutTools._grep)
    assert "4000" in source and "2_000_000" in source


def test_noise_directories_are_pruned(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "src.py").write_text("NEEDLE = 1\n", encoding="utf-8")
    for noisy in ("node_modules", "__pycache__", ".git"):
        d = repo / noisy; d.mkdir()
        (d / "junk.py").write_text("NEEDLE = 2\n", encoding="utf-8")
    out = RepoTools([{"name": ".", "path": str(repo)}]).execute("repo_grep", {"pattern": "NEEDLE"})
    assert "src.py" in out
    for noisy in ("node_modules", "__pycache__", ".git"):
        assert noisy not in out, noisy


def test_dotted_source_directories_are_still_visible(tmp_path):
    """The scout prunes EVERY dotted dir because one of its roots is `~/`, where `.cache`/`.venv`
    dwarf the repo. A Researcher grepping ONE mounted repo wants `.github/workflows` — that is
    ordinary source, and losing it would be a silent capability regression from a refactor."""
    repo = tmp_path / "repo"; repo.mkdir()
    wf = repo / ".github" / "workflows"; wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("run: pytest NEEDLE\n", encoding="utf-8")
    out = RepoTools([{"name": ".", "path": str(repo)}]).execute(
        "repo_grep", {"pattern": "NEEDLE", "glob": "*.yml"})
    assert ".github/workflows/ci.yml" in out, out


def test_the_scout_itself_still_prunes_dotted_directories_by_default(tmp_path):
    """The divergence is a PARAMETER, not a loosening of the scout's own contract."""
    root = tmp_path / "home"; root.mkdir()
    cache = root / ".cache"; cache.mkdir()
    (cache / "junk.py").write_text("NEEDLE = 1\n", encoding="utf-8")
    (root / "ok.py").write_text("NEEDLE = 1\n", encoding="utf-8")
    scout = RepoScoutTools([root], default_root=root)
    assert ".cache" not in scout._grep("NEEDLE", str(root), "*", 40)
    assert ".cache" in scout._grep("NEEDLE", str(root), "*", 40, skip_hidden=False)


# --------------------------------------------------------------------------------------------
# The three things that had to survive
# --------------------------------------------------------------------------------------------

def test_a_bare_glob_still_walks_the_whole_tree(tmp_path):
    """`repo_list("*.py")` has always been recursive. `_find_files` runs a pathlib glob, where it is
    not — handing the pattern over unchanged would silently drop every subdirectory file."""
    listed = RepoTools([{"name": ".", "path": str(_repo(tmp_path))}]).execute(
        "repo_list", {"glob": "*.py"})
    assert "model.py" in listed and "pkg/util.py" in listed, listed


@pytest.mark.parametrize("given,expected", [
    ("*.py", "**/*.py"), ("*", "**/*"),
    ("**/*.py", "**/*.py"),                    # already recursive — do not double it
    ("pkg/*.py", "pkg/*.py"),                  # a path pattern is deliberate scoping
    ("", "**/*"),
])
def test_the_glob_translation_leaves_deliberate_patterns_alone(given, expected):
    assert _recursive_glob(given) == expected


def test_the_named_mount_prefix_still_resolves(tmp_path):
    """`RepoScoutTools._resolve` knows `default_root` and CWD, not named mounts — so `a/x.py` under
    mounts `a` and `b` only resolves through `RepoTools._resolve`, which the adapter keeps."""
    a = tmp_path / "a"; a.mkdir(); (a / "x.py").write_text("AAA = 1\n", encoding="utf-8")
    b = tmp_path / "b"; b.mkdir(); (b / "y.py").write_text("BBB = 2\n", encoding="utf-8")
    tools = RepoTools([{"name": "a", "path": str(a)}, {"name": "b", "path": str(b)}])

    assert "AAA" in tools.execute("repo_read", {"path": "a/x.py"})
    assert "BBB" in tools.execute("repo_read", {"path": "b/y.py"})
    hits = tools.execute("repo_grep", {"pattern": r"\w+ = \d"})
    assert "a/x.py:1" in hits and "b/y.py:1" in hits, hits


def test_every_mount_gets_its_own_receipt(tmp_path):
    """Merging both mounts' hits into ONE capped list is what let a partial answer read as complete —
    each mount's block carries the receipt for its own search."""
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    for i in range(60):
        (a / f"f{i:03d}.py").write_text("NEEDLE = 1\n", encoding="utf-8")
    (b / "one.py").write_text("NEEDLE = 1\n", encoding="utf-8")
    out = RepoTools([{"name": "a", "path": str(a)}, {"name": "b", "path": str(b)}]).execute(
        "repo_grep", {"pattern": "NEEDLE"})
    assert "capped at" in out
    assert "b/one.py" in out, "the capped mount swallowed the other mount's complete answer"


def test_git_internals_stay_refused_by_this_tools_own_gate(tmp_path):
    """`_pathsafe.looks_secret` does not know `.git`, so the scout's gate alone would hand back a
    credentialed clone's `.git/config`. The `.git`-parts filter is deliberately NOT delegated."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    token = "ghp_" + "z" * 32
    (repo / ".git" / "config").write_text(f"\turl = https://u:{token}@example.invalid\n",
                                          encoding="utf-8")
    tools = RepoTools([{"name": ".", "path": str(repo)}])
    body = tools.execute("repo_read", {"path": ".git/config"})
    assert token not in body and "refused" in body, body


def test_reading_outside_a_mount_is_still_refused(tmp_path):
    outside = tmp_path / "outside.txt"; outside.write_text("TOPSECRET", encoding="utf-8")
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "ok.py").write_text("x=1\n", encoding="utf-8")
    tools = RepoTools([{"name": ".", "path": str(repo)}])
    for escape in ("../outside.txt", "/etc/passwd"):
        assert "no such file" in tools.execute("repo_read", {"path": escape}).lower()


def test_the_page_marker_contract_comes_from_the_scout(tmp_path):
    """M9: a >200KB file must page, not report EOF. Delegating means this fix cannot diverge again."""
    repo = tmp_path / "repo"; repo.mkdir()
    lines = [f"line {i:05d}: " + "x" * 40 for i in range(6000)]
    lines[-1] = "FINAL_SENTINEL_LINE"
    (repo / "big.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    tools = RepoTools([{"name": ".", "path": str(repo)}])

    assert "more below" in tools.execute("repo_read", {"path": "big.py", "start_line": 1,
                                                       "lines": 50})
    last = tools.execute("repo_read", {"path": "big.py", "start_line": 5995, "lines": 50})
    assert "FINAL_SENTINEL_LINE" in last and "more below" not in last
