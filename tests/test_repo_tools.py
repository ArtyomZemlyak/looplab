"""Item #3: the Researcher gets read-only grep/list/read over the editable repo(s) so it
proposes changes from the actual code. RepoTools is read-only and path-restricted (editing
stays the Developer's job — the role/trust boundary)."""
from __future__ import annotations

from looplab.tools.knowledge_tools import RepoTools


def _repo(tmp_path):
    (tmp_path / "model.py").write_text("def train(lr):\n    return lr * 2\n", encoding="utf-8")
    (tmp_path / "config.json").write_text('{"lr": 0.1}', encoding="utf-8")
    sub = tmp_path / "pkg"; sub.mkdir()
    (sub / "util.py").write_text("SECRET = 1\n", encoding="utf-8")
    return tmp_path


def test_grep_list_read_root_repo(tmp_path):
    t = RepoTools([{"name": ".", "path": str(_repo(tmp_path))}])
    assert "model.py:1" in t.execute("repo_grep", {"pattern": "def train"})
    listed = t.execute("repo_list", {"glob": "*.py"})
    assert "model.py" in listed and "pkg/util.py" in listed
    assert "lr * 2" in t.execute("repo_read", {"path": "model.py"})
    assert "SECRET" in t.execute("repo_read", {"path": "pkg/util.py"})


def test_repo_read_large_file_reports_more_not_eof(tmp_path):
    """M9 regression: repo_read used read_file's default 200KB cap, so a >200KB file was truncated
    and _paginate reported EOF (no '(more below)' marker) — telling the agent it read the whole file
    while everything past 200KB was silently missing. The full file must now be read + paginated."""
    repo = tmp_path / "repo"; repo.mkdir()
    # ~300KB file: 6000 lines of ~50 chars each, with a unique sentinel on the last line.
    lines = [f"line {i:05d}: " + "x" * 40 for i in range(6000)]
    lines[-1] = "FINAL_SENTINEL_LINE"
    (repo / "big.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    t = RepoTools([{"name": ".", "path": str(repo)}])
    # Windowing the first page must advertise more content (not falsely claim EOF).
    first = t.execute("repo_read", {"path": "big.py", "start_line": 1, "lines": 50})
    assert "more below" in first, first[-200:]
    # Windowing to the true end (past the old 200KB cut) must reach the sentinel and NOT say more below.
    last = t.execute("repo_read", {"path": "big.py", "start_line": 5995, "lines": 50})
    assert "FINAL_SENTINEL_LINE" in last
    assert "more below" not in last


def test_named_repos_namespacing(tmp_path):
    a = tmp_path / "a"; a.mkdir(); (a / "x.py").write_text("AAA = 1\n", encoding="utf-8")
    b = tmp_path / "b"; b.mkdir(); (b / "y.py").write_text("BBB = 2\n", encoding="utf-8")
    t = RepoTools([{"name": "a", "path": str(a)}, {"name": "b", "path": str(b)}])
    hits = t.execute("repo_grep", {"pattern": r"\w+ = \d"})
    assert "a/x.py:1" in hits and "b/y.py:1" in hits
    assert "AAA" in t.execute("repo_read", {"path": "a/x.py"})
    assert "BBB" in t.execute("repo_read", {"path": "b/y.py"})


def test_read_is_path_restricted(tmp_path):
    secret = tmp_path / "outside.txt"; secret.write_text("TOPSECRET", encoding="utf-8")
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "ok.py").write_text("x=1\n", encoding="utf-8")
    t = RepoTools([{"name": ".", "path": str(repo)}])
    assert "no such file" in t.execute("repo_read", {"path": "../outside.txt"}).lower()
    assert "no such file" in t.execute("repo_read", {"path": "/etc/passwd"}).lower()


def test_make_roles_wires_repo_tools_for_edit_mode(tmp_path):
    from pathlib import Path

    from looplab.agents.agent import ToolUsingResearcher
    from looplab.core.config import Settings
    from looplab.tools.knowledge_tools import RepoTools as RT
    from looplab.adapters.repo_task import EvalSpec, RepoTask
    from looplab.adapters.tasks import make_roles
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "m.py").write_text("x=1\n", encoding="utf-8")
    t = RepoTask(id="e", editable_path=str(repo), edit_surface=["*.py"],
                 eval=EvalSpec(command=["python", "m.py"]))
    s = Settings(); s.backend = "llm"; s.unified_agent = False   # default dev backend -> no editing agent
    researcher, _ = make_roles(t, s)
    assert isinstance(researcher, ToolUsingResearcher)
    provs = getattr(researcher.tools, "providers", [researcher.tools])
    assert any(isinstance(p, RT) for p in provs)


def test_make_roles_no_repo_tools_for_param_search(tmp_path):
    from looplab.core.config import Settings
    from looplab.tools.knowledge_tools import RepoTools as RT
    from looplab.adapters.repo_task import EvalSpec, RepoTask
    from looplab.adapters.tasks import make_roles
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "m.py").write_text("x=1\n", encoding="utf-8")
    t = RepoTask(id="p", editable_path=str(repo), params={"lr": (0.0, 1.0)},
                 eval=EvalSpec(command=["python", "m.py"], params_style="cli_overrides"))
    s = Settings(); s.backend = "llm"
    researcher, _ = make_roles(t, s)
    provs = getattr(getattr(researcher, "tools", None), "providers", [])
    assert not any(isinstance(p, RT) for p in provs)
