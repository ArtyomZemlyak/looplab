"""Parent-aware implement (cumulative parent→child diff) + LLM stream stall-degrade.

The two cheapest high-leverage fixes from the modularity/robustness review:
- an IMPROVE hands the parent's actual solution to the Developer (implement_from), so it patches
  instead of regenerating from the pristine baseline;
- a stream that stalls degrades the next attempt to a plain blocking read (and streaming turns off
  for the client after two stalls) — a flaky proxied endpoint often answers the same request fine
  without SSE while its stream wedges mid-generation.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import looplab.llm as llm  # noqa: E402
from looplab.core.models import Idea, Node  # noqa: E402
from looplab.llm import OpenAICompatibleClient  # noqa: E402


# ---------------------------------------------------------------- stall-degrade
class _Ctx:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self.body

    def __exit__(self, *a):
        return False


def _nonstream_body(text="ok"):
    return io.BytesIO(json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": text}}],
         "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode())


def test_stream_stall_degrades_to_nonstream(monkeypatch):
    """Attempt 1 (stream) stalls -> attempt 2 goes NON-stream and succeeds; the stall is counted."""
    calls = []

    def fake_urlopen(req, timeout=None):
        payload = json.loads(req.data.decode())
        calls.append(bool(payload.get("stream")))
        if payload.get("stream"):
            raise TimeoutError("stream stalled")     # what the watchdog/idle check raises
        return _Ctx(_nonstream_body("degraded"))

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)   # skip the backoff wait
    c = OpenAICompatibleClient("m", base_url="http://x/v1", stream=True)
    out = c.complete_text([{"role": "user", "content": "hi"}])
    assert out == "degraded"
    assert calls == [True, False]                    # stream first, then degraded
    assert c._stream_stalls == 1


def test_two_stalls_disable_streaming_for_the_client(monkeypatch):
    def fake_urlopen(req, timeout=None):
        payload = json.loads(req.data.decode())
        if payload.get("stream"):
            raise TimeoutError("stream stalled")
        return _Ctx(_nonstream_body("ok"))

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    c = OpenAICompatibleClient("m", base_url="http://x/v1", stream=True)
    c._stream_stalls = 2                             # already stalled twice
    seen = []
    real = fake_urlopen

    def spy(req, timeout=None):
        seen.append(json.loads(req.data.decode()).get("stream"))
        return real(req, timeout=timeout)

    monkeypatch.setattr(llm.urllib.request, "urlopen", spy)
    assert c.complete_text([{"role": "user", "content": "hi"}]) == "ok"
    assert seen and not seen[0]                      # streaming never attempted again


# ---------------------------------------------------------------- parent-aware implement
class _FromScratchDev:
    def implement(self, idea):
        return "scratch"


class _ParentAwareDev(_FromScratchDev):
    def __init__(self):
        self.got = None

    def implement_from(self, idea, parent):
        self.got = parent
        return "patched"


def test_orchestrator_routes_to_implement_from():
    from looplab.engine.orchestrator import Engine
    parent = Node(id=1, operator="draft", idea=Idea(operator="draft", params={}), code="P")
    eng = Engine.__new__(Engine)                     # no full engine needed for the routing helper
    eng.developer = _ParentAwareDev()
    assert eng._implement(Idea(operator="improve", params={}), parent) == "patched"
    assert eng.developer.got is parent
    eng.developer = _FromScratchDev()
    assert eng._implement(Idea(operator="improve", params={}), parent) == "scratch"
    aware = _ParentAwareDev()
    eng.developer = aware
    assert eng._implement(Idea(operator="draft", params={}), None) == "scratch"  # no parent -> plain
    assert aware.got is None


def test_repo_developer_seeds_parent_files():
    """implement_from pre-loads the parent's files (they carry over verbatim) and shows them in the
    prompt with the amend instruction — the agent patches, never rebuilds from the pristine repo."""
    from looplab.adapters.repo_task import LLMRepoDeveloper

    class _DoneClient:
        """Scripted client: immediately calls done without writing anything."""
        def chat(self, messages, tools, tool_choice="auto"):
            # capture the prompt for assertions via attribute
            self.last_messages = messages
            return {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "done", "arguments": '{"summary":"ok"}'}}]}

    dev = LLMRepoDeveloper.__new__(LLMRepoDeveloper)
    dev.client = _DoneClient()
    dev.brief = "brief"
    dev.last_files, dev.last_deleted = {}, []
    dev.loop_opts = {}
    dev._surface = {"train.py": "print('train')"}
    dev._protected = set()
    dev._prefixes = ()
    dev._editables = []
    dev._recipes = lambda: "(none)"
    dev._results_context = lambda: ""
    dev._repo_context = lambda: "(repo)"
    dev._emit_spec = lambda: {"type": "function", "function": {
        "name": "done", "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}}}}

    parent = Node(id=7, operator="improve", idea=Idea(operator="improve", params={}),
                  code="", files={"solution.py": "BASE = 1"}, metric=0.81)
    dev.implement_from(Idea(operator="improve", params={}, rationale="add a new loss"), parent)
    # untouched parent file carried over verbatim into this node's working set
    assert dev.last_files.get("solution.py") == "BASE = 1"
    # and the prompt showed the parent solution with the amend instruction
    user = next(m["content"] for m in dev.client.last_messages if m["role"] == "user")
    assert "PARENT SOLUTION" in user and "BASE = 1" in user and "AMEND" in user


# ---------------------------------------------------------------- diff editing (edit_file patch-gate)
def test_edit_file_patch_gate(tmp_path):
    """SEARCH/REPLACE editing: exact-unique applies; staged overlay wins; ambiguous/no-match/protected
    are refused with actionable errors; whitespace-tolerant fallback catches trailing-space drift."""
    from looplab.adapters.repo_task import RepoWriteTools
    (tmp_path / "train.py").write_text("LR = 0.1\ndef f():\n    return 1\n")
    w = RepoWriteTools(["**/*.py"], {"grader.py"}, [], editables=[{"name": ".", "path": str(tmp_path)}])
    assert "1 hunk" in w.execute("edit_file", {"path": "train.py", "search": "LR = 0.1", "replace": "LR = 0.01"})
    assert "LR = 0.01" in w.files["train.py"]                     # disk original -> staged overlay
    assert "1 hunk" in w.execute("edit_file", {"path": "train.py", "search": "LR = 0.01", "replace": "LR = 0.003"})
    w.files["a.py"] = "x=1\nx=1\n"
    assert "ambiguous" in w.execute("edit_file", {"path": "a.py", "search": "x=1", "replace": "x=2"})
    assert "no match" in w.execute("edit_file", {"path": "train.py", "search": "NOPE", "replace": "y"})
    assert "protected" in w.execute("edit_file", {"path": "grader.py", "search": "a", "replace": "b"})
    assert "no such file" in w.execute("edit_file", {"path": "ghost.py", "search": "a", "replace": "b"})
    w.files["b.py"] = "def g():   \n    return 1\n"               # trailing spaces in the file
    assert "hunk applied" in w.execute(
        "edit_file", {"path": "b.py", "search": "def g():\n    return 1", "replace": "def g():\n    return 2"})
    assert "return 2" in w.files["b.py"]


def test_header_timeout_applies_only_to_stream_attempts(monkeypatch):
    """The short first-byte window is a STREAM-only bound (SSE headers arrive on admission). A
    non-stream attempt's headers arrive only after the WHOLE generation — bounding them at 45s would
    kill every legitimate >45s generation, self-triggering with the stall-degrade that switches a
    call to non-stream. Regression for exactly that: the degraded attempt must get the full timeout."""
    seen = []   # (stream?, urlopen timeout)

    def fake_urlopen(req, timeout=None):
        payload = json.loads(req.data.decode())
        seen.append((bool(payload.get("stream")), timeout))
        if payload.get("stream"):
            raise TimeoutError("stream stalled")
        return _Ctx(_nonstream_body("ok"))

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    c = OpenAICompatibleClient("m", base_url="http://x/v1", stream=True, timeout=180.0)
    assert c.complete_text([{"role": "user", "content": "hi"}]) == "ok"
    assert seen[0] == (True, 45.0)      # stream attempt: short header window
    assert seen[1] == (False, 180.0)    # degraded non-stream attempt: FULL timeout for the whole read


def test_fail_fast_errors_do_not_count_as_stream_stalls(monkeypatch):
    """A refused connection (endpoint down) says nothing about SSE health — it must not ratchet the
    permanent stream-off counter."""
    def fake_urlopen(req, timeout=None):
        raise ConnectionRefusedError("down")

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    c = OpenAICompatibleClient("m", base_url="http://x/v1", stream=True)
    try:
        c.complete_text([{"role": "user", "content": "hi"}])
    except Exception:
        pass
    assert c._stream_stalls == 0


def test_edit_file_fallback_is_line_anchored(tmp_path):
    """The whitespace-tolerant fallback swaps WHOLE lines, so its match must be line-anchored — a
    mid-line substring hit would silently eat the line's prefix/suffix (real corruption cases from
    review). Non-anchored hits are refused; anchored ones preserve EOF newlines, don't swallow the
    line after a trailing blank in `search`, delete cleanly on empty replace, and clear `deleted`."""
    from looplab.adapters.repo_task import RepoWriteTools
    w = RepoWriteTools(["**/*.py"], set(), [])
    w.files["a.py"] = "x = compute()  \ny = 2\n"      # mid-line prefix must not be eaten
    assert "no match" in w.execute("edit_file", {"path": "a.py", "search": "compute()\ny = 2", "replace": "R"})
    assert w.files["a.py"] == "x = compute()  \ny = 2\n"
    w.files["b.py"] = "a()   \nreturn foo(bar)\n"     # suffix must not be eaten
    assert "no match" in w.execute("edit_file", {"path": "b.py", "search": "a()\nreturn foo(", "replace": "R"})
    w.files["c.py"] = "foo1  \nbar\nbaz\n"            # trailing blank in search must not eat `baz`
    w.execute("edit_file", {"path": "c.py", "search": "foo1\nbar\n\n", "replace": "NEW"})
    assert "baz" in w.files["c.py"]
    w.files["d.py"] = "def g():   \n    return 1\n"   # EOF match keeps the trailing newline
    w.execute("edit_file", {"path": "d.py", "search": "def g():\n    return 1",
                            "replace": "def g():\n    return 2"})
    assert w.files["d.py"] == "def g():\n    return 2\n"
    w.files["e.py"] = "keep\ndrop  \nrest\n"          # empty replace = clean line deletion
    w.execute("edit_file", {"path": "e.py", "search": "drop\nrest", "replace": ""})
    assert w.files["e.py"] == "keep\n"
    w.deleted = ["f.py"]; w.files["f.py"] = "z = 1  \n"
    w.execute("edit_file", {"path": "f.py", "search": "z = 1", "replace": "z = 2"})
    assert "f.py" not in w.deleted                     # fallback edit resurrects a deleted file


def test_edit_file_resolves_named_editable_prefix(tmp_path):
    """Staged paths are prefixed with the editable's NAME (repo mounts at wd/<name>); _current must
    strip the owning prefix before joining that editable's root, or every unstaged file is 'missing'."""
    from looplab.adapters.repo_task import RepoWriteTools
    (tmp_path / "x.py").write_text("V = 1\n")
    w = RepoWriteTools(["**/*.py"], set(), [], editables=[{"name": "lib", "path": str(tmp_path)}])
    r = w.execute("edit_file", {"path": "lib/x.py", "search": "V = 1", "replace": "V = 2"})
    assert "hunk applied" in r and "V = 2" in w.files["lib/x.py"]


def test_implement_from_carries_parent_deletions():
    """A child of a parent that DELETED a repo file must inherit the deletion (deletions apply after
    writes at materialization) — else the child's workdir re-seeds the pristine repo with the file
    restored, measuring a different workspace than parent+patch. A delete-only parent still counts."""
    from looplab.adapters.repo_task import LLMRepoDeveloper

    class _DoneClient:
        def chat(self, messages, tools, tool_choice="auto"):
            return {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "done", "arguments": '{"summary":"ok"}'}}]}

    dev = LLMRepoDeveloper.__new__(LLMRepoDeveloper)
    dev.client = _DoneClient()
    dev.brief = "brief"; dev.last_files, dev.last_deleted = {}, []
    dev.loop_opts = {}; dev._surface = ["**/*.py"]; dev._protected = set()
    dev._prefixes = (); dev._editables = []
    dev._recipes = lambda: "(none)"; dev._results_context = lambda: ""
    dev._repo_context = lambda: "(repo)"
    dev._emit_spec = lambda: {"type": "function", "function": {
        "name": "done", "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}}}}
    parent = Node(id=7, operator="improve", idea=Idea(operator="improve", params={}),
                  files={}, deleted=["old_module.py"], metric=0.8)
    dev.implement_from(Idea(operator="improve", params={}, rationale="tweak"), parent)
    assert dev.last_deleted == ["old_module.py"]       # deletion carried into the child's working set
