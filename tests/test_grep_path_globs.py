"""WP-TOOLS T1 (2026-09-29): a grep `glob` with a `/` in it matches a PATH, and a grep that searched
no file at all says so instead of `(no matches)`.

Measured on MiniOneRec inf13: 250 `repo_grep` calls carried a glob with a `/` (`service/
latency_engine.py`, `**/*.py`) and NOT ONE ever hit, because the glob was matched against the
file's basename, which has no `/`. Every one of them answered `(no matches)`, and the model read that
as "the symbol does not exist" — `def infer` reported absent from a file that defines it.

The rule these tests drive (`tools/reposcout.py::glob_admits`): a glob without a `/` keeps the
basename rule; a glob with one is matched segment by segment against the key a hit is SHOWN under
(`_disp` on disk, the overlay key when staged), right-anchored, `*` inside one segment and `**` over
any number of whole segments. The file count behind the no-file receipt is taken AFTER every gate,
so a secret's existence is never disclosed by it.
"""
from __future__ import annotations

from looplab.tools.knowledge_tools import RepoTools
from looplab.tools.reposcout import RepoScoutTools, glob_admits


def _tree(root):
    """A repo with the inf13 shape: the file the globs name, a namesake deeper down, a root file."""
    (root / "service").mkdir(parents=True)
    (root / "service" / "latency_engine.py").write_text("def infer(batch):\n    return batch\n",
                                                        encoding="utf-8")
    (root / "pkg" / "service").mkdir(parents=True)
    (root / "pkg" / "service" / "latency_engine.py").write_text("def infer(x):\n    pass\n",
                                                                encoding="utf-8")
    (root / "service" / "sub").mkdir()
    (root / "service" / "sub" / "deep.py").write_text("def infer_deep():\n    pass\n",
                                                      encoding="utf-8")
    (root / "main.py").write_text("def infer_main():\n    pass\n", encoding="utf-8")
    return root


def _scout(root, **kw):
    return RepoScoutTools(roots=[str(root)], default_root=str(root), **kw)


# ------------------------------------------------------------------------------ the matcher itself

def test_a_path_glob_is_right_anchored_segment_matching():
    assert glob_admits("service/latency_engine.py", "service/latency_engine.py")
    assert glob_admits("service/latency_engine.py", "pkg/service/latency_engine.py"), (
        "right-anchored: the glob names a suffix of the path, as a model copying a path means it")
    assert not glob_admits("service/latency_engine.py", "myservice/latency_engine.py"), (
        "segment-aware: `service` must be a whole directory name, not a string suffix")
    assert glob_admits("service/*.py", "service/latency_engine.py")
    assert not glob_admits("service/*.py", "service/sub/deep.py"), "`*` must not cross a `/`"
    assert glob_admits("service/**/*.py", "service/sub/deep.py")
    assert glob_admits("service/**/*.py", "service/latency_engine.py"), "`**` is zero or more dirs"
    assert glob_admits("**/*.py", "main.py"), "`**/` must not skip a root-level file"
    assert glob_admits("./service/*.py", "service/latency_engine.py"), "a leading ./ is noise"
    assert glob_admits("service/", "service/sub/deep.py"), "a trailing / is everything under it"
    assert glob_admits("/abs/repo/*.py", "/abs/repo/x.py") and not glob_admits(
        "/abs/repo/*.py", "/abs/repo/sub/x.py"), "a leading / anchors at the key's start"


def test_a_bare_file_name_glob_keeps_the_basename_rule():
    for key in ("x.py", "a/b/x.py"):
        assert glob_admits("*.py", key) and glob_admits("x.py", key)
    assert not glob_admits("*.txt", "a/b/x.py")


# ------------------------------------------------------------------------------ disk and overlay agree

def test_a_path_glob_hits_the_same_file_on_disk_and_staged(tmp_path):
    """Matched against the DISPLAY key on both sides, so staging a file cannot change what a glob
    admits. MUTATION: match the disk walk against a path relative to the walk base and the overlay
    against its key -> a `root`-scoped walk and the overlay disagree."""
    root = _tree(tmp_path / "repo")
    on_disk = _scout(root).execute("grep", {"pattern": "def infer", "glob": "service/*.py"})
    assert "service/latency_engine.py:1:" in on_disk
    assert "pkg/service/latency_engine.py:1:" in on_disk, "the right-anchored suffix match"
    assert "deep.py" not in on_disk and "main.py" not in on_disk

    staged = {"service/latency_engine.py": "def infer(batch, staged=True):\n",
              "service/new_path.py": "def infer_new():\n"}
    both = _scout(root, overlay=staged).execute("grep", {"pattern": "def infer",
                                                        "glob": "service/*.py"})
    assert "service/latency_engine.py:1: def infer(batch, staged=True):" in both
    assert "service/new_path.py:1:" in both, "a staged-only file answers the same glob"
    assert both.count("service/latency_engine.py:1:") == 2, (
        "the staged copy and pkg/'s namesake — the pristine disk copy is deduped away")


def test_a_path_glob_scoped_by_root_matches_the_shown_key_not_the_walk_relative_path(tmp_path):
    """A glob copied off a hit label must still work when the walk starts BELOW the repo root."""
    root = _tree(tmp_path / "repo")
    out = _scout(root).execute("grep", {"pattern": "def infer", "root": "service",
                                        "glob": "service/*.py"})
    assert "service/latency_engine.py:1:" in out and "pkg/" not in out


def test_repo_grep_answers_a_path_glob_across_named_mounts(tmp_path):
    """Globs in the display form (`<mount>/<path>`) work across several mounts."""
    a = _tree(tmp_path / "a")
    b = tmp_path / "b"
    (b / "service").mkdir(parents=True)
    (b / "service" / "latency_engine.py").write_text("def infer_b():\n", encoding="utf-8")
    tools = RepoTools([{"name": "a", "path": str(a)}, {"name": "b", "path": str(b)}])
    out = tools.execute("repo_grep", {"pattern": "def infer", "glob": "b/service/*.py"})
    assert "b/service/latency_engine.py:1:" in out
    assert "a/service" not in out, "the mount segment is part of the shown key"
    out = tools.execute("repo_grep", {"pattern": "def infer", "glob": "service/latency_engine.py"})
    assert "a/service/latency_engine.py:1:" in out and "b/service/latency_engine.py:1:" in out
    assert "a/pkg/service/latency_engine.py:1:" in out


def test_repo_grep_with_the_inf13_glob_now_finds_the_definition(tmp_path):
    """The defect's own shape, end to end through the Researcher's tool."""
    root = _tree(tmp_path / "repo")
    tools = RepoTools([{"name": ".", "path": str(root)}])
    out = tools.execute("repo_grep", {"pattern": "def infer", "glob": "service/latency_engine.py"})
    assert "service/latency_engine.py:1: def infer(batch):" in out, out
    assert "(no matches)" not in out
    assert "main.py:1:" in tools.execute("repo_grep", {"pattern": "def infer_main",
                                                       "glob": "**/*.py"})


# ------------------------------------------------------------------------------ the no-file receipt

def test_a_glob_no_file_matches_says_so_and_names_the_mount_not_the_absolute_root(tmp_path):
    root = _tree(tmp_path / "repo")
    tools = RepoTools([{"name": ".", "path": str(root)}])
    out = tools.execute("repo_grep", {"pattern": "def infer", "glob": "service/nope/*.py"})
    assert out.startswith("(grep: no searchable file under the repo matches glob "), out
    assert "'service/nope/*.py'" in out and "path pattern like service/*.py" in out
    assert str(tmp_path) not in out, "the receipt must not leak the mount's absolute path"
    assert "(no matches)" not in out

    scout = _scout(root)
    out = scout.execute("grep", {"pattern": "x", "root": "service", "glob": "*.md"})
    assert out == ("(grep: no searchable file under service matches glob '*.md' — a glob is a "
                   "file-name pattern like *.py or a repo-relative path pattern like service/*.py)")


def test_a_real_miss_is_still_no_matches(tmp_path):
    root = _tree(tmp_path / "repo")
    tools = RepoTools([{"name": ".", "path": str(root)}])
    assert tools.execute("repo_grep", {"pattern": "absent_zzz", "glob": "*.py"}) == "(no matches)"
    assert "not found" in _scout(root).execute("grep", {"pattern": "absent_zzz"})


def test_a_glob_matching_only_a_secret_answers_exactly_like_one_matching_nothing(tmp_path):
    """Counting files that PASSED THE GLOB would disclose that `.env` exists; the count is taken
    after every gate. MUTATION: count before the secret gate -> the two answers differ."""
    root = _tree(tmp_path / "repo")
    (root / ".env").write_text("API_KEY=sk-live-123\n", encoding="utf-8")
    scout = _scout(root)
    secret = scout.execute("grep", {"pattern": "API", "glob": ".env"})
    nothing = scout.execute("grep", {"pattern": "API", "glob": ".nothing"})
    assert "sk-live" not in secret
    assert secret.replace("'.env'", "G") == nothing.replace("'.nothing'", "G"), (secret, nothing)
    tools = RepoTools([{"name": ".", "path": str(root)}])
    assert (tools.execute("repo_grep", {"pattern": "API", "glob": ".env"}).replace("'.env'", "G")
            == tools.execute("repo_grep", {"pattern": "API", "glob": ".none"}).replace("'.none'",
                                                                                       "G"))


def test_a_walk_that_hit_its_file_budget_says_so_rather_than_no_file_matched(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for i in range(4005):
        (root / f"f{i:05d}.py").write_text("x = 1\n", encoding="utf-8")
    out = _scout(root).execute("grep", {"pattern": "absent_zzz", "glob": "*.py"})
    assert "stopped after 4000 files" in out and "no searchable file" not in out, out


def test_an_empty_pattern_is_surfaced_not_folded_into_no_matches(tmp_path):
    """`repo_grep` dropped every block starting `(grep:`, so the scout's refusal of an empty pattern
    reached the model as `(no matches)` — a claim about a pattern it never searched for."""
    root = _tree(tmp_path / "repo")
    tools = RepoTools([{"name": "a", "path": str(root)}, {"name": "b", "path": str(root)}])
    out = tools.execute("repo_grep", {"pattern": "   "})
    assert out == "(grep: give a (short) pattern to search for)", out


def test_the_receipt_is_emitted_once_and_only_when_no_mount_matched_a_file(tmp_path):
    a = _tree(tmp_path / "a")
    b = tmp_path / "b"
    b.mkdir()
    (b / "notes.md").write_text("def infer in prose\n", encoding="utf-8")
    tools = RepoTools([{"name": "a", "path": str(a)}, {"name": "b", "path": str(b)}])
    # `*.md` matches a file in b only: b searched it, so this is a pattern answer, not a glob one.
    assert tools.execute("repo_grep", {"pattern": "absent_zzz", "glob": "*.md"}) == "(no matches)"
    hit = tools.execute("repo_grep", {"pattern": "def infer", "glob": "*.md"})
    assert "b/notes.md:1:" in hit and "no searchable file" not in hit
    none = tools.execute("repo_grep", {"pattern": "def infer", "glob": "*.rst"})
    assert none.count("no searchable file") == 1 and "under a, b matches" in none, none


def test_a_grep_result_survives_copy_deepcopy_and_pickle():
    """D4: `GrepResult.__new__` needs the kind as well as the text, so `copy.copy`, `deepcopy` and
    `pickle` — which rebuild a str subclass through `__new__` — raised TypeError on every result.
    MUTATION: drop `__getnewargs__` -> TypeError."""
    import copy
    import pickle

    from looplab.tools.reposcout import GrepResult
    original = GrepResult("a.py:1: x", "hits")
    for twin in (copy.copy(original), copy.deepcopy(original),
                 pickle.loads(pickle.dumps(original))):
        assert type(twin) is GrepResult and twin == "a.py:1: x" and twin.kind == "hits"


def test_a_dot_segment_in_a_path_glob_is_noise(tmp_path):
    root = _tree(tmp_path / "repo")
    out = _scout(root).execute("grep", {"pattern": "def infer", "glob": "service/./*.py"})
    assert "service/latency_engine.py:1:" in out, out
