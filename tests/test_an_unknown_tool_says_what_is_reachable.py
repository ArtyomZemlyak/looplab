"""A refusal the model can act on, instead of a dead end.

MEASURED over the 46-probe corpus, 2026-09-02: 36 tool calls named something the toolset does not
route, and every one was answered with the bare `(unknown tool: <name>)`. The distribution is the
finding: **31 of the 36 are `write_file` called during `plan`, in 20 of the 46 probes** -- and
`write_file` is a real tool that works 391 times in `plan_step` and 14 in `card_build`. The
Developer, while PLANNING, reaches for the tool it will have while EXECUTING. Nothing in the old
message says the name was right and the moment was wrong, so the model repeats it or invents
another name (`read_memo`, `python`: four more calls, same answer).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from looplab.agents.tool_loop import CompositeTools  # noqa: E402


class _Provider:
    def __init__(self, names):
        self._names = list(names)

    def specs(self):
        return [{"type": "function", "function": {"name": n, "description": n, "parameters": {}}}
                for n in self._names]

    def execute(self, name, args):
        return f"ran {name}"


def _tools(names=("write_file_step", "repo_read", "run_probe", "read_code", "eval_train", "diff_nodes")):
    return CompositeTools([_Provider(names)])


def test_a_near_miss_names_the_tool_that_exists():
    out = _tools().execute("write_file", {})
    assert "unknown tool: write_file" in out
    assert "write_file_step" in out, out


def test_a_name_from_nowhere_still_gets_something_to_call():
    """`read_memo` and `python` matched nothing at all, three and one calls. A refusal that lists
    NOTHING leaves the model to guess a second time."""
    out = _tools().execute("read_memo", {})
    assert "unknown tool: read_memo" in out
    assert "available here:" in out, out
    assert any(n in out for n in ("repo_read", "read_code")), out


def test_the_refusal_stays_short():
    """It is pasted into a context window this repo budgets carefully: suggestions are capped and
    the rest is a count."""
    many = [f"tool_{i:02d}" for i in range(40)]
    out = _tools(many).execute("nothing_like_this", {})
    assert len(out) < 200, f"{len(out)} chars: {out}"
    assert "more)" in out, out


def test_the_typed_path_says_the_same_thing():
    r = _tools().execute_result("write_file", {})
    assert r.is_error and not r.retryable
    assert "write_file_step" in r.content, r.content


def test_an_empty_toolset_does_not_invent_a_suggestion():
    empty = CompositeTools([_Provider([])])
    assert empty.execute("anything", {}) == "(unknown tool: anything)"


def test_a_known_tool_is_untouched():
    """MUTATION GUARD: the suggestion path must not swallow a call that routes."""
    assert _tools().execute("repo_read", {}) == "ran repo_read"
