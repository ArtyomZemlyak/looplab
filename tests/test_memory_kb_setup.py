"""Memory + knowledge-base setup: enable flags, default-path resolution, and the agent's
write tools (kb_write / kb_append / remember). Offline, no model needed."""
from __future__ import annotations

from pathlib import Path

from looplab.config import Settings
from looplab.knowledge_tools import KnowledgeTools


# ---- Settings: enable flags + default-path resolution --------------------------------------------

def test_memory_kb_on_by_default_with_default_paths():
    s = Settings()
    assert s.memory_enabled is True and s.knowledge_enabled is True
    # No path needed: both resolve to <home_dir>/... so the user never has to wire one up.
    assert s.resolved_memory_dir() == str(Path(".looplab") / "memory")
    assert s.resolved_knowledge_dir() == str(Path(".looplab") / "knowledge")


def test_disable_flag_turns_store_off():
    s = Settings(memory_enabled=False, knowledge_enabled=False)
    assert s.resolved_memory_dir() is None
    assert s.resolved_knowledge_dir() is None


def test_explicit_dir_wins_but_disable_beats_it():
    s = Settings(memory_dir="/tmp/mem", knowledge_dir="/tmp/kb")
    assert s.resolved_memory_dir() == "/tmp/mem"
    assert s.resolved_knowledge_dir() == "/tmp/kb"
    # The disable flag is a hard off even when a path is set.
    off = Settings(memory_enabled=False, memory_dir="/tmp/mem")
    assert off.resolved_memory_dir() is None


def test_home_dir_drives_the_default_location():
    s = Settings(home_dir="/data/ll")
    assert s.resolved_memory_dir() == str(Path("/data/ll") / "memory")
    assert s.resolved_knowledge_dir() == str(Path("/data/ll") / "knowledge")


# ---- KnowledgeTools write tools (hierarchical markdown KB) ----------------------------------------

def test_kb_write_supports_folders_and_is_searchable(tmp_path):
    kt = KnowledgeTools(str(tmp_path))
    names = {f["function"]["name"] for f in kt.specs()}
    assert {"kb_write", "kb_append", "kb_edit", "kb_tree"} <= names

    # A relative path with sub-folders is created (hierarchy), .md implied.
    assert "wrote" in kt.execute("kb_write", {"name": "tabular/gbdt/xgboost",
                                              "content": "# XGBoost\nuse early stopping"})
    assert (tmp_path / "tabular" / "gbdt" / "xgboost.md").read_text(encoding="utf-8").startswith("# XGBoost")
    # The folder structure is visible to the agent, and the note is retrievable + readable by path.
    assert "tabular/" in kt.execute("kb_tree", {})
    assert "xgboost" in kt.execute("kb_search", {"query": "early stopping for boosting"}).lower()
    assert "early stopping" in kt.execute("read_note", {"name": "tabular/gbdt/xgboost.md"})

    kt.execute("kb_append", {"name": "tabular/gbdt/xgboost.md", "content": "also tune max_depth"})
    body = (tmp_path / "tabular" / "gbdt" / "xgboost.md").read_text(encoding="utf-8")
    assert "early stopping" in body and "max_depth" in body


def test_kb_edit_revises_in_place(tmp_path):
    kt = KnowledgeTools(str(tmp_path))
    kt.execute("kb_write", {"name": "ridge.md", "content": "lambda default is 1.0"})
    assert "edited" in kt.execute("kb_edit", {"name": "ridge.md", "old": "1.0", "new": "0.5"})
    assert "0.5" in (tmp_path / "ridge.md").read_text(encoding="utf-8")
    # A non-unique / missing target is reported, not silently applied.
    assert "not found" in kt.execute("kb_edit", {"name": "ridge.md", "old": "zzz", "new": "q"})


def test_kb_write_rejects_traversal(tmp_path):
    kt = KnowledgeTools(str(tmp_path))
    out = kt.execute("kb_write", {"name": "../escape", "content": "nope"})
    assert "bad" in out.lower()
    # Nothing is written inside OR outside the KB root.
    assert not (tmp_path.parent / "escape.md").exists()
    assert not (tmp_path / "escape.md").exists()


def test_remember_writes_markdown_memory_and_indexes_it(tmp_path):
    mem = tmp_path / "memory"
    kt = KnowledgeTools(str(tmp_path / "kb"), memory_dir=str(mem))
    names = {f["function"]["name"] for f in kt.specs()}
    assert {"remember", "memory_read", "memory_write", "memory_edit", "memory_list"} <= names

    assert "remembered" in kt.execute(
        "remember", {"text": "normalize features before the SVM or it diverges", "topic": "svm"})
    body = (mem / "svm.md").read_text(encoding="utf-8")
    assert body.startswith("- normalize features")          # a markdown bullet, not JSON
    # Memory folds into the unified kb_search index.
    assert "svm" in kt.execute("kb_search", {"query": "should I scale inputs for svm"}).lower()
    # And it's directly readable/listable as a markdown note.
    assert "svm.md" in kt.execute("memory_list", {})
    assert "normalize" in kt.execute("memory_read", {"name": "svm.md"})


def test_write_tools_absent_when_read_only_or_no_dir():
    # Read-only KB: read tools present, write tools gone.
    ro = KnowledgeTools("/some/kb", writable=False)
    ro_names = {f["function"]["name"] for f in ro.specs()}
    assert "kb_search" in ro_names
    assert not ({"kb_write", "kb_append", "kb_edit", "remember"} & ro_names)
    # No knowledge dir + no memory dir: no write tools at all, remember refuses cleanly.
    none = KnowledgeTools(None)
    assert not ({"kb_write", "remember", "memory_write"} & {f["function"]["name"] for f in none.specs()})
    assert "not configured" in none.execute("remember", {"text": "x"})


def test_remember_rejects_empty_text(tmp_path):
    kt = KnowledgeTools(None, memory_dir=str(tmp_path))
    assert "nothing to remember" in kt.execute("remember", {"text": "   "})


def test_remember_degenerate_topic_falls_back_to_lessons(tmp_path):
    # A degenerate topic ('.md', all-dots, empty) must not produce a '.md.md' file.
    kt = KnowledgeTools(None, memory_dir=str(tmp_path))
    kt.execute("remember", {"text": "guard against this", "topic": ".md"})
    assert (tmp_path / "lessons.md").exists()
    assert not (tmp_path / ".md.md").exists()


def test_cases_searchable_when_only_memory_configured(tmp_path):
    # No KB dir, but memory + cases: kb_search must still surface past cases (folded into memory's
    # index — not double-indexed in a separate store).
    cases = tmp_path / "mem" / "cases.jsonl"
    cases.parent.mkdir(parents=True)
    cases.write_text('{"task_id": "t1", "goal": "predict churn", "params": {"a": 1}, '
                     '"metric": 0.9, "rationale": "gbdt won"}\n', encoding="utf-8")
    kt = KnowledgeTools(None, cases_path=str(cases), memory_dir=str(tmp_path / "mem"))
    out = kt.execute("kb_search", {"query": "how to predict churn"})
    assert "PAST CASE" in out
