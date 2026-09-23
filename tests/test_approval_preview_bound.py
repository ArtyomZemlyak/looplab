"""An approval card never hides part of the change it asks about (review 2026-09-22, TAT-05).

The assistant's write tools ask a human before a mutation, and the card shows a PREVIEW of the change
— a unified diff for `write_file`/`edit_file`, the patch itself for `apply_patch`. Three places cut
that preview at 4,000 characters, all silently: `write_tools._diff` (`"".join(d)[:4000]`), the
`apply_patch` action (`diff[:4000]`), and the assistant router's public projection of every pending
action (`public_limits["preview"] = 4000`). A 14 KB rewrite therefore reached the approver as its
first 4,000 characters with nothing marking the rest — and "Approve once" applies the WHOLE change.
An injected instruction only had to put the edit that matters past character 4,000.

Now ONE bound, `perm_modes.APPROVAL_PREVIEW_CHARS`, applied by ONE function,
`perm_modes.clip_approval_preview`, at all three places: a preview that fits is byte-identical, one
that does not ends on a whole line and SAYS how much it leaves out and how to review it. The router
applying the same function to an already-clipped preview is a no-op, so nothing is cut twice.
"""
from __future__ import annotations

import difflib
import hashlib
import time

from looplab.tools.perm_modes import APPROVAL_PREVIEW_CHARS, clip_approval_preview
from looplab.tools.write_tools import WriteTools

_RECEIPT = "approval preview cut here"


def _asked(tmp_path, name: str, args: dict) -> dict:
    """Run one write tool in `default` mode and return the action the approver was shown."""
    seen: list = []
    tool = WriteTools([tmp_path], mode="default",
                      approver=lambda action: seen.append(action) or "deny")
    tool.execute(name, args)
    assert len(seen) == 1, "the tool did not ask"
    return seen[0]


def _full_diff(rel: str, old: str, new: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True),
                                        new.splitlines(keepends=True),
                                        fromfile=f"a/{rel}", tofile=f"b/{rel}"))


def _big_file(lines: int = 200) -> str:
    return "".join(f"line {i:04d}: " + "x" * 60 + "\n" for i in range(lines))


def _assert_honest_cut(preview: str, full: str) -> None:
    assert len(full) > APPROVAL_PREVIEW_CHARS, "the fixture must be over the bound"
    assert len(preview) <= APPROVAL_PREVIEW_CHARS
    head, _, receipt = preview.partition("\n…[" + _RECEIPT)
    assert receipt, f"the cut is silent: {preview[-200:]!r}"
    assert full.startswith(head) and head.endswith("\n"), "the part shown is the head, whole lines"
    omitted = full[len(head):]
    assert f"{len(omitted):,} of {len(full):,} characters" in receipt
    assert f"{len(omitted.splitlines()):,} more line" in receipt
    assert "reject" in receipt.lower()


def test_a_long_write_preview_says_what_it_leaves_out(tmp_path):
    """THE DEFECT: before, `preview == full_diff[:4000]` and nothing marked the rest."""
    content = _big_file()
    action = _asked(tmp_path, "write_file", {"path": str(tmp_path / "big.py"), "content": content})
    _assert_honest_cut(action["preview"], _full_diff("big.py", "", content))


def test_a_long_edit_preview_says_what_it_leaves_out(tmp_path):
    target = tmp_path / "big.py"
    original = _big_file()
    target.write_text(original, encoding="utf-8")
    old_block = "".join(f"line {i:04d}: " + "x" * 60 + "\n" for i in range(10, 130))
    new_block = "".join(f"edited {i:04d}: " + "y" * 60 + "\n" for i in range(120))
    action = _asked(tmp_path, "edit_file", {"path": str(target), "old_str": old_block,
                                            "new_str": new_block})
    _assert_honest_cut(action["preview"],
                       _full_diff("big.py", original, original.replace(old_block, new_block, 1)))


def test_a_long_patch_preview_says_what_it_leaves_out(tmp_path):
    body = "".join(f"+added {i:04d} " + "z" * 60 + "\n" for i in range(150))
    patch = ("diff --git a/new.txt b/new.txt\nnew file mode 100644\n--- /dev/null\n+++ b/new.txt\n"
             f"@@ -0,0 +1,150 @@\n{body}")
    action = _asked(tmp_path, "apply_patch", {"diff": patch})
    _assert_honest_cut(action["preview"], patch)
    # The card shows less; the approval never covers less — the scope binds the WHOLE patch.
    assert action["scope"]["diff_digest"] == hashlib.sha256(patch.encode("utf-8")).hexdigest()


def test_a_preview_that_fits_is_the_historical_bytes(tmp_path):
    content = "a = 1\nb = 2\n"
    action = _asked(tmp_path, "write_file", {"path": str(tmp_path / "s.py"), "content": content})
    assert action["preview"] == _full_diff("s.py", "", content)
    assert _RECEIPT not in action["preview"]


def test_the_clip_is_idempotent_so_the_router_never_cuts_twice():
    once = clip_approval_preview(_big_file(300))
    assert clip_approval_preview(once) == once
    assert clip_approval_preview("short") == "short"
    one_line = "y" * (APPROVAL_PREVIEW_CHARS * 2)          # no newline to end on: cut mid-line
    clipped = clip_approval_preview(one_line)
    assert len(clipped) <= APPROVAL_PREVIEW_CHARS and _RECEIPT in clipped


def test_the_router_shows_an_over_long_preview_with_its_receipt(tmp_path, monkeypatch):
    """The assistant router's public projection cut EVERY pending action's preview at 4,000
    characters, silently, on its way to the card — whichever tool produced it."""
    from fastapi.testclient import TestClient

    from looplab.serve.server import make_app

    long_preview = _big_file(300)          # a provider that did not bound its own preview

    def fake_run_turn(_client, _root, _history, _instruction, mode, **kwargs):
        verdict = kwargs["approver"]({
            "tool": "write_file", "tool_kind": "write", "label": "overwrite big.py",
            "verb": "write big.py", "preview": long_preview, "recovery_available": True,
            "scope": {"path": "big.py"}})
        return {"ok": True, "reply": verdict, "steps": [], "applied": [], "proposals": [],
                "todos": [], "refs": [], "mode": mode}

    monkeypatch.setenv("LOOPLAB_JOB_INLINE_WAIT", "0.01")
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda _s, **_kw: object())
    monkeypatch.setattr("looplab.serve.routers.assistant._assistant_run_turn", fake_run_turn)
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "default"}).json()["id"]
    client.post(f"/api/assistant/sessions/{sid}/message",
                json={"instruction": "rewrite it", "mode": "default"})
    req = None
    for _ in range(200):
        pending = client.get(f"/api/assistant/permissions?session={sid}").json()["pending"]
        if pending:
            req = pending[0]
            break
        time.sleep(0.02)
    assert req is not None, "the approver never asked"
    _assert_honest_cut(req["action"]["preview"], long_preview)
    assert client.post(f"/api/assistant/permissions/{req['id']}",
                       json={"decision": "deny"}).status_code == 200
