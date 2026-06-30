"""Memory + knowledge-base setup: enable flags, default-path resolution, and the agent's
write tools (kb_write / kb_append / remember). Offline, no model needed."""
from __future__ import annotations

import json
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


# ---- KnowledgeTools write tools ------------------------------------------------------------------

def test_kb_write_and_append_then_searchable(tmp_path):
    kt = KnowledgeTools(str(tmp_path))
    names = {f["function"]["name"] for f in kt.specs()}
    assert {"kb_write", "kb_append"} <= names

    assert "wrote" in kt.execute("kb_write", {"name": "xgboost", "content": "# XGBoost\nuse early stopping"})
    # A bare name gets a .md suffix and lands in the KB dir.
    assert (tmp_path / "xgboost.md").read_text(encoding="utf-8").startswith("# XGBoost")
    # The new note is immediately retrievable (index rebuilt on write).
    assert "xgboost" in kt.execute("kb_search", {"query": "early stopping for boosting"}).lower()

    kt.execute("kb_append", {"name": "xgboost.md", "content": "also tune max_depth"})
    body = (tmp_path / "xgboost.md").read_text(encoding="utf-8")
    assert "early stopping" in body and "max_depth" in body


def test_kb_write_is_path_restricted(tmp_path):
    kt = KnowledgeTools(str(tmp_path))
    kt.execute("kb_write", {"name": "../escape", "content": "nope"})
    # Traversal is stripped to a bare filename inside the KB dir; nothing escapes.
    assert not (tmp_path.parent / "escape.md").exists()
    assert (tmp_path / "escape.md").exists()


def test_remember_writes_memory_note_and_indexes_it(tmp_path):
    mem = tmp_path / "memory"
    kt = KnowledgeTools(str(tmp_path / "kb"), memory_dir=str(mem))
    assert "remember" in {f["function"]["name"] for f in kt.specs()}

    assert "remembered" in kt.execute(
        "remember", {"text": "normalize features before the SVM or it diverges", "tags": ["svm"]})
    rec = json.loads((mem / "notes.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert rec["text"].startswith("normalize features") and rec["tags"] == ["svm"]
    # Memory notes fold into the same searchable index.
    assert "svm" in kt.execute("kb_search", {"query": "should I scale inputs for svm"}).lower()


def test_write_tools_absent_when_read_only_or_no_dir():
    # Read-only KB: no write tools exposed.
    ro = KnowledgeTools("/some/kb", writable=False)
    assert not ({"kb_write", "kb_append", "remember"} & {f["function"]["name"] for f in ro.specs()})
    # No knowledge dir + no memory dir: no write tools at all.
    none = KnowledgeTools(None)
    assert not ({"kb_write", "kb_append", "remember"} & {f["function"]["name"] for f in none.specs()})
    # remember refuses cleanly when memory isn't configured.
    assert "not configured" in none.execute("remember", {"text": "x"})


def test_remember_rejects_empty_text(tmp_path):
    kt = KnowledgeTools(None, memory_dir=str(tmp_path))
    assert "nothing to remember" in kt.execute("remember", {"text": "   "})
