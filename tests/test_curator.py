"""MarkdownStore (hierarchical markdown CRUD) + the goal-driven Curator agent session.
Offline — the curator runs against a scripted fake chat client, no model needed."""
from __future__ import annotations

import json

from looplab.curator import Curator, make_curator
from looplab.knowledge_tools import KnowledgeTools
from looplab.mdstore import MarkdownStore


# ---- MarkdownStore -------------------------------------------------------------------------------

def test_store_write_read_tree_hierarchy(tmp_path):
    s = MarkdownStore(tmp_path)
    assert s.write("cv/augmentation/mixup", "# mixup\nblend two images") == "cv/augmentation/mixup.md"
    assert s.write("tabular/xgboost.md", "# xgb") == "tabular/xgboost.md"
    # list is recursive + relative; tree shows the folders.
    assert set(s.list()) == {"cv/augmentation/mixup.md", "tabular/xgboost.md"}
    tree = s.tree()
    assert "cv/" in tree and "augmentation/" in tree and "mixup.md" in tree
    # read tolerates the missing .md suffix.
    assert "blend two images" in s.read("cv/augmentation/mixup")


def test_store_edit_requires_unique_match(tmp_path):
    s = MarkdownStore(tmp_path)
    s.write("n.md", "alpha and alpha")
    assert "appears 2" in s.edit("n.md", "alpha", "beta")        # ambiguous → refused
    assert "edited" in s.edit("n.md", "alpha and alpha", "beta") # unique → applied
    assert s.read("n.md").strip() == "beta"
    assert "not found" in s.edit("n.md", "zzz", "q")
    assert "no such note" in s.edit("missing.md", "a", "b")


def test_store_blocks_traversal(tmp_path):
    s = MarkdownStore(tmp_path / "root")
    assert s.resolve("../secrets") is None
    assert s.write("../escape", "x") is None
    assert not (tmp_path / "escape.md").exists()
    # A leading-slash path is treated as relative-to-root (contained), never an absolute escape.
    contained = s.resolve("/etc/passwd")
    assert contained is not None and (s.root in contained.parents)


def test_store_search_indexes_extra(tmp_path):
    # Extra (non-file) content — e.g. a past case — is searchable alongside the notes.
    s = MarkdownStore(tmp_path, extra=[("case:poly", "PAST CASE polynomial degree 2 worked best")])
    s.write("ridge.md", "ridge shrinks coefficients")
    labels = {label for label, _ in s.search("which polynomial degree", k=2)}
    assert "case:poly" in labels


def test_store_edit_updates_index_not_stale(tmp_path):
    # Incremental indexing must not leave a stale snapshot: after an edit, search returns the new
    # text (and the extra survives the per-write upsert, never dropped by a partial reindex).
    s = MarkdownStore(tmp_path, extra=[("case:x", "PAST CASE about boosting")])
    s.write("note.md", "the answer is alpha")
    s.edit("note.md", "alpha", "omega")
    hits = dict(s.search("the answer", k=3))
    assert "omega" in hits.get("note.md", "") and "alpha" not in hits.get("note.md", "")
    assert "case:x" in dict(s.search("boosting", k=3))      # extra still indexed after the write


# ---- Curator agent session -----------------------------------------------------------------------

class _FakeChatClient:
    """Scripts assistant turns (tool calls then a final emit)."""
    def __init__(self, scripted):
        self.scripted = list(scripted)

    def chat(self, messages, tools, tool_choice="auto"):
        return self.scripted.pop(0)


def _call(name, args):
    return {"content": "", "tool_calls": [
        {"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_curator_surveys_then_writes(tmp_path):
    kb, mem = tmp_path / "kb", tmp_path / "mem"
    tools = KnowledgeTools(str(kb), memory_dir=str(mem), writable=True)
    client = _FakeChatClient([
        _call("kb_tree", {}),                                          # 1: survey existing structure
        _call("kb_write", {"name": "cv/augmentation/mixup.md",         # 2: file the new knowledge
                           "content": "# mixup\nblend images + labels, alpha~0.2"}),
        _call("emit", {"summary": "added mixup note",                  # 3: report
                       "changes": ["cv/augmentation/mixup.md"]}),
    ])
    res = Curator(client, tools).run("research mixup augmentation and add it to the KB")
    assert res.ok
    assert res.changes == ["cv/augmentation/mixup.md"]
    # The edit actually landed on disk (a real agentic session, not just a reply).
    assert "blend images" in (kb / "cv" / "augmentation" / "mixup.md").read_text(encoding="utf-8")


def test_curator_records_a_memory_lesson(tmp_path):
    tools = KnowledgeTools(str(tmp_path / "kb"), memory_dir=str(tmp_path / "mem"), writable=True)
    client = _FakeChatClient([
        _call("memory_list", {}),
        _call("remember", {"text": "scale features before SVM", "topic": "svm"}),
        _call("emit", {"summary": "noted SVM scaling lesson", "changes": ["svm.md"]}),
    ])
    res = Curator(client, tools).run("you keep forgetting to scale before the SVM — remember it")
    assert res.ok and "svm.md" in res.changes
    assert "scale features" in (tmp_path / "mem" / "svm.md").read_text(encoding="utf-8")


def test_curator_soft_fails_without_client():
    res = Curator(None, None).run("anything")
    assert not res.ok and "no LLM client" in res.error


def test_make_curator_none_when_stores_disabled():
    from looplab.config import Settings
    s = Settings(memory_enabled=False, knowledge_enabled=False)
    assert make_curator(s, client=object()) is None
    # No client → None regardless.
    assert make_curator(Settings(), client=None) is None
