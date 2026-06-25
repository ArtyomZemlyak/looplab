"""Item #3: the Researcher gets read-only grep/list/read over the editable repo(s) so it
proposes changes from the actual code. RepoTools is read-only and path-restricted (editing
stays the Developer's job — the role/trust boundary)."""
from __future__ import annotations

from looplab.knowledge_tools import RepoTools


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

    from looplab.agent import ToolUsingResearcher
    from looplab.config import Settings
    from looplab.knowledge_tools import RepoTools as RT
    from looplab.repo_task import EvalSpec, RepoTask
    from looplab.tasks import make_roles
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
    from looplab.config import Settings
    from looplab.knowledge_tools import RepoTools as RT
    from looplab.repo_task import EvalSpec, RepoTask
    from looplab.tasks import make_roles
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "m.py").write_text("x=1\n", encoding="utf-8")
    t = RepoTask(id="p", editable_path=str(repo), params={"lr": (0.0, 1.0)},
                 eval=EvalSpec(command=["python", "m.py"], params_style="cli_overrides"))
    s = Settings(); s.backend = "llm"
    researcher, _ = make_roles(t, s)
    provs = getattr(getattr(researcher, "tools", None), "providers", [])
    assert not any(isinstance(p, RT) for p in provs)
