"""Developer memory — a SEPARATE cross-run store of IMPLEMENTATION lessons the Developer self-authors,
reads, and previews, plus the run-end engine distillation. Offline — tmp memory dir + fakes, no model.
"""
from __future__ import annotations

import types

import orjson

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.tools.dev_memory import (DevMemoryTools, DevMemoryWriteTools, append_dev_lessons,
                                      dev_lesson_preview, DEV_LESSONS_FILE, _load)


# --------------------------------------------------------------------------- store helpers
def test_append_consolidates_duplicates(tmp_path):
    mem = str(tmp_path)
    append_dev_lessons(mem, [{"task_id": "t", "statement": "reuse the repo's own dataset loader",
                              "outcome": "technique", "run_id": "r1", "evidence": []}])
    # a second run re-learns the SAME thing → consolidated into one row with bumped evidence_count
    append_dev_lessons(mem, [{"task_id": "t", "statement": "reuse the repo's own dataset loader",
                              "outcome": "technique", "run_id": "r2", "evidence": []}])
    rows = _load(tmp_path / DEV_LESSONS_FILE)
    assert len(rows) == 1
    assert int(rows[0].get("evidence_count") or 0) >= 2


# --------------------------------------------------------------------------- write tool
def test_write_tool_saves_and_refuses(tmp_path):
    w = DevMemoryWriteTools(str(tmp_path), task_id="t", run_id="r1", fingerprint=["kind:repo"], kind="repo")
    names = {s["function"]["name"] for s in w.specs()}
    assert names == {"remember_dev_lesson"}
    out = w.execute("remember_dev_lesson",
                    {"statement": "Lightning 1.5 wants precision=16, not '16-mixed'", "outcome": "pitfall"})
    assert "saved" in out
    rows = _load(tmp_path / DEV_LESSONS_FILE)
    assert rows and rows[0]["outcome"] == "pitfall" and rows[0]["source"] == "developer"
    assert rows[0]["task_id"] == "t" and rows[0]["run_id"] == "r1"
    # too-short → refused, nothing added
    assert "refused" in w.execute("remember_dev_lesson", {"statement": "hi"})
    assert len(_load(tmp_path / DEV_LESSONS_FILE)) == 1


# --------------------------------------------------------------------------- read tool
def test_read_tool_search_and_list(tmp_path):
    mem = str(tmp_path)
    append_dev_lessons(mem, [
        {"task_id": "t", "statement": "cast the argparse gpus flag to int", "outcome": "pitfall",
         "run_id": "r1", "evidence": []},
        {"task_id": "t", "statement": "orchestrate train.py via subprocess rather than reimplementing it",
         "outcome": "technique", "run_id": "r1", "evidence": []}])
    r = DevMemoryTools(mem)
    hit = r.execute("search_dev_lessons", {"query": "gpus flag argparse"})
    assert "argparse gpus" in hit and "pitfall" in hit
    listed = r.execute("list_dev_lessons", {})
    assert "subprocess" in listed and "argparse gpus" in listed
    assert DevMemoryTools(None).execute("list_dev_lessons", {}) == "(no developer memory configured)"


# --------------------------------------------------------------------------- preview
def test_preview_matches_task_and_truncates(tmp_path):
    mem = str(tmp_path)
    long = "always " + "x" * 300 + " end"
    append_dev_lessons(mem, [
        {"task_id": "mine", "fingerprint": ["kind:repo", "dir:max"], "statement": long,
         "outcome": "technique", "run_id": "r1", "evidence": []},
        {"task_id": "other", "fingerprint": ["kind:toy", "dir:min"], "statement": "unrelated toy note",
         "outcome": "technique", "run_id": "r1", "evidence": []}])
    prev = dev_lesson_preview(mem, task_id="mine", fingerprint=["kind:repo", "dir:max"])
    assert "DEVELOPER MEMORY" in prev
    assert "always" in prev                      # the exact-task lesson surfaced
    assert "unrelated toy note" not in prev       # the unrelated (different fingerprint) one did not
    # each preview line is truncated (width 100) — no 300-char dump
    assert max(len(ln) for ln in prev.splitlines()) < 140
    assert dev_lesson_preview(str(tmp_path / "empty")) == ""


# --------------------------------------------------------------------------- developer wiring
def _bare_dev(*, memory_dir=None, developer_memory=True):
    from looplab.adapters.repo_task import LLMRepoDeveloper
    dev = LLMRepoDeveloper.__new__(LLMRepoDeveloper)
    dev._editables = []
    dev._run_dir = None
    dev._run_tools = False
    dev._cross_run_tools = False
    dev._all_runs_tools = False
    dev._bound_state = None
    dev._bound_parent = None
    dev._memory_dir = memory_dir
    dev._developer_memory = developer_memory
    dev.task = types.SimpleNamespace(kind="repo", goal="g", direction="max", metric="acc")
    return dev


def test_developer_memory_tools_gating(tmp_path):
    dev = _bare_dev(memory_dir=str(tmp_path))
    dev._bound_state = RunState(run_id="rA", task_id="tX", goal="g", direction="max")
    read_names = {s["function"]["name"] for p in dev._dev_memory_tools(write=False) for s in p.specs()}
    assert read_names == {"search_dev_lessons", "list_dev_lessons"}       # no write in read-only phases
    write_names = {s["function"]["name"] for p in dev._dev_memory_tools(write=True) for s in p.specs()}
    assert "remember_dev_lesson" in write_names and "search_dev_lessons" in write_names
    # disabled / no memory_dir → nothing
    assert _bare_dev(memory_dir=str(tmp_path), developer_memory=False)._dev_memory_tools(write=True) == []
    assert _bare_dev(memory_dir=None)._dev_memory_tools(write=True) == []


def test_developer_prompt_previews_and_offers_write(tmp_path):
    mem = str(tmp_path)
    append_dev_lessons(mem, [{"task_id": "tX", "fingerprint": ["kind:repo"], "statement":
                              "reuse the repo's own pickled dataset loader", "outcome": "technique",
                              "run_id": "r0", "evidence": []}])

    class _DoneClient:
        def chat(self, messages, tools, tool_choice="auto"):
            self.offered = [t["function"]["name"] for t in tools]
            self.last_messages = messages
            return {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "done", "arguments": '{"summary":"ok"}'}}]}

    dev = _bare_dev(memory_dir=mem)
    dev.client = _DoneClient()
    dev.brief = "brief"
    dev.last_files, dev.last_deleted = {}, []
    dev.loop_opts = {}
    dev._surface, dev._protected, dev._prefixes = ["**/*.py"], set(), ()
    dev._recipes = lambda: "(none)"
    dev._results_context = lambda: ""
    dev._repo_context = lambda: "(repo)"
    dev.bind_state(RunState(run_id="rA", task_id="tX", goal="g", direction="max"), None)
    dev.implement(Idea(operator="draft", params={}, rationale="build it"))
    assert "remember_dev_lesson" in dev.client.offered and "search_dev_lessons" in dev.client.offered
    sys_prompt = dev.client.last_messages[0]["content"]
    assert "DEVELOPER MEMORY" in sys_prompt and "pickled dataset loader" in sys_prompt


# --------------------------------------------------------------------------- engine distillation
class _FakeStore:
    def __init__(self):
        self.events: list = []

    def read_all(self):
        return self.events

    def append(self, t, payload):
        self.events.append(types.SimpleNamespace(type=t, data=payload))


class _FakeEngine:
    def __init__(self, memory_dir, *, client=None):
        self._developer_memory = True
        self.memory_dir = memory_dir
        self.store = _FakeStore()
        self.task = types.SimpleNamespace(kind="repo", goal="g", direction="max", metric="acc")
        self._client = client

    def _task_fingerprint(self, final, best=None):
        return ["kind:repo", "dir:max"]

    def _reflect_client(self):
        return self._client


def _failed_run():
    st = RunState(run_id="rA", task_id="tX", goal="g", direction="max")
    st.nodes = {
        0: Node(id=0, operator="draft", idea=Idea(operator="draft", params={}), metric=0.8,
                status=NodeStatus.evaluated, code="ok"),
        1: Node(id=1, operator="improve", idea=Idea(operator="improve", params={}),
                status=NodeStatus.failed, error_reason="oom", error="CUDA out of memory"),
        2: Node(id=2, operator="improve", idea=Idea(operator="improve", params={}),
                status=NodeStatus.failed, error_reason="oom", error="CUDA out of memory"),
    }
    return st


def test_engine_distillation_deterministic_fallback_and_gate(tmp_path):
    from looplab.engine.lessons import LessonMemory
    from looplab.events.types import EV_DEV_LESSONS_DISTILLED
    eng = _FakeEngine(str(tmp_path), client=None)     # no client → deterministic fallback
    lm = LessonMemory(eng)
    lm.write_dev_lessons(_failed_run())
    rows = _load(tmp_path / DEV_LESSONS_FILE)
    assert len(rows) == 1 and rows[0]["outcome"] == "pitfall" and "oom" in rows[0]["statement"]
    assert rows[0]["source"] == "distilled"
    assert any(e.type == EV_DEV_LESSONS_DISTILLED for e in eng.store.events)
    # gate: a second finalize (resume) does NOT re-append
    lm.write_dev_lessons(_failed_run())
    assert len(_load(tmp_path / DEV_LESSONS_FILE)) == 1


def test_engine_distillation_off_without_memory(tmp_path):
    from looplab.engine.lessons import LessonMemory
    eng = _FakeEngine(str(tmp_path))
    eng._developer_memory = False
    LessonMemory(eng).write_dev_lessons(_failed_run())
    assert not (tmp_path / DEV_LESSONS_FILE).exists()      # nothing written, no event
    assert eng.store.events == []


def test_engine_distillation_llm_path(tmp_path, monkeypatch):
    """The LLM branch: a wired client → agentic_text returns [GOOD]/[BAD] lines → they parse into
    technique/pitfall dev lessons (source=distilled). Monkeypatch agentic_text so the tool loop isn't
    actually driven — this tests the parse + outcome mapping, the error-prone part."""
    import looplab.agents.agent as agent_mod
    from looplab.engine.lessons import LessonMemory
    monkeypatch.setattr(agent_mod, "agentic_text",
                        lambda *a, **k: "[GOOD] orchestrate the repo's own train.py via subprocess\n"
                                        "[BAD] don't pass the --gpus flag as a string")
    eng = _FakeEngine(str(tmp_path), client=object())     # non-None client → LLM path
    LessonMemory(eng).write_dev_lessons(_failed_run())
    rows = _load(tmp_path / DEV_LESSONS_FILE)
    assert any(r["outcome"] == "technique" for r in rows)      # [GOOD] → technique
    assert any(r["outcome"] == "pitfall" for r in rows)        # [BAD]  → pitfall
    assert rows and all(r["source"] == "distilled" for r in rows)


def test_preview_matches_similar_fingerprint_not_dissimilar(tmp_path):
    """The cross-task branch: a DIFFERENT task_id but a SIMILAR fingerprint (Jaccard ≥ 0.34) surfaces;
    a dissimilar-fingerprint lesson does not — the reason each lesson carries a fingerprint."""
    mem = str(tmp_path)
    cur_fp = ["kind:repo", "dir:max", "goal:accuracy", "goal:image"]
    append_dev_lessons(mem, [
        {"task_id": "otherA", "fingerprint": ["kind:repo", "dir:max", "goal:accuracy", "goal:texture"],
         "statement": "similar-task lesson: patch the loss in model.py", "outcome": "technique",
         "run_id": "r1", "evidence": []},          # shares 3/union5 = 0.6 with cur_fp → surfaces
        {"task_id": "otherB", "fingerprint": ["kind:toy", "dir:min", "goal:speed"],
         "statement": "dissimilar-task lesson", "outcome": "technique", "run_id": "r1", "evidence": []}])
    prev = dev_lesson_preview(mem, task_id="mine", fingerprint=cur_fp)   # different task_id from both
    assert "similar-task lesson" in prev
    assert "dissimilar-task lesson" not in prev
