"""Engine notices for an EXTERNAL CLI coding agent, through the agent's OWN turn-boundary hook.

WHY (doc 73 §4.3, 2026-10-08). After a live base advance the engine tells the Developer sessions at
work (`engine/upstream_hints.py`): an in-house Developer hears the notice as a `user` turn at its
tool-loop boundary (`agents/tool_loop.py::interjection_scope`). An external coding agent
(`agents/cli_agent.py`) runs its loop in its own process, so that boundary is not ours — doc 73 §4.3
recorded the notice as impossible there. It is impossible for most presets, but not for `claude`:

* `claude --settings <file>` loads extra settings for ONE invocation, hooks included, and its
  `--print` mode runs them. A `PostToolUse` command hook that prints
  `{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "..."}}` on exit 0
  adds that text to the model's context after the tool call — a turn boundary of the agent's own
  loop, the same place the in-house loop interjects. Nothing is written into the agent's worktree
  (the patch gate diffs it) and nothing into the operator's `~/.claude`.
* The other presets have no per-invocation channel LoopLab can own: `aider` has no hooks; `codex`'s
  hooks are an experimental project file (`.codex/hooks.json`) that needs a trust step, `goose`'s live
  in plugin directories under the user's home, Continue reads `.claude/settings.json` from the project
  — each would mean writing into the agent's worktree or the operator's home; `opencode`'s plugin API
  is undocumented for context injection. For those (and for `claude` too) the NEXT call is told
  instead: its message states the run's promotions (`CliAgentDeveloper._upstream_note`).

THE CHANNEL. A private temporary directory (outside the worktree) holds `hook.py`, `settings.json`
and `notices.json`. The engine side (`HookNotices.pump`, called from the agent's wait loop every
`CliAgentDeveloper.CANCEL_POLL_S`) asks the session's notice channel for new notices — at most
`MAX_HINTS_PER_SESSION`, each once, the same cap as the in-house loop — and rewrites `notices.json`
atomically. The hook emits each notice it has not emitted yet and marks it under `delivered/` with an
exclusive create; the next pump turns each mark into the session's receipt
(`upstream_hint_delivered {channel: cli_hook}`), so a notice is recorded as delivered only once the
agent's own hook actually emitted it. A hook that never runs (hooks disabled by the operator's managed
settings, a `claude` without `--settings`) records nothing — the honest answer.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

# Presets whose CLI loads a per-invocation hook — the only ones `HookNotices` arms.
HOOK_PRESETS = frozenset({"claude"})
_HINT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_HOOK_TIMEOUT_S = 10

# The hook the agent runs after every tool call. Our own constant text, never model-authored; it reads
# only its own channel directory, which the engine writes.
_HOOK_SCRIPT = r'''import json, os, re, sys
d = sys.argv[1]
try:
    sys.stdin.read()
except Exception:
    pass
try:
    with open(os.path.join(d, "notices.json"), encoding="utf-8") as fh:
        notices = json.load(fh)
except Exception:
    notices = []
said = []
for n in notices if isinstance(notices, list) else []:
    if not isinstance(n, dict):
        continue
    hid, text = n.get("hint_id"), n.get("text")
    if not isinstance(hid, str) or not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", hid):
        continue
    try:
        os.close(os.open(os.path.join(d, "delivered", hid), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except OSError:
        continue
    said.append(text)
if said:
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                        "additionalContext": "\n\n".join(said)}}))
'''


def external_hint_setting(settings) -> bool:
    """THE ONE READER of `Settings.upstream_hint_external` (doc 73 §4.3) — for the agent's constructor
    (`agents/developer_backends.py`) and the engine's session (`engine/upstream_serve.py::
    external_hint_channel`). Absent or duck-typed reads OFF: an external agent's argv and message keep
    their historical bytes."""
    return getattr(settings, "upstream_hint_external", False) is True


def external_notice_kwargs(settings) -> dict:
    """`CliAgentDeveloper`'s notice keywords from the run's settings, for the composition root
    (`agents/developer_backends.py::external_cli_developer`): the switch, and the untrusted-evidence
    label its promotions paragraph is fenced with while the envelope is on (`core/evidence.py`)."""
    from looplab.core.evidence import EVIDENCE_LABEL, envelope_enabled
    return {"upstream_note": external_hint_setting(settings),
            "evidence_label": EVIDENCE_LABEL if envelope_enabled(settings) else ""}


def _command(argv: list[str]) -> str:
    """One shell command line for the hook (Claude Code runs a hook's `command` through a shell)."""
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return " ".join(shlex.quote(a) for a in argv)


class HookNotices:
    """One agent invocation's notice channel. `channel` is the session's notice source
    (`agents/tool_loop.py::notice_channel`): `offer() -> [{"hint_id", "text"}]` and
    `acknowledge(hint_id, channel=)`."""

    def __init__(self, channel, root: Path):
        self.channel = channel
        self.root = Path(root)
        self.offered: list[dict] = []
        self.acked: set[str] = set()
        (self.root / "delivered").mkdir(parents=True, exist_ok=True)
        (self.root / "hook.py").write_text(_HOOK_SCRIPT, encoding="utf-8")
        self._write_notices()
        command = _command([sys.executable, str(self.root / "hook.py"), str(self.root)])
        settings = {"hooks": {"PostToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": command, "timeout": _HOOK_TIMEOUT_S}]}]}}
        (self.root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    @classmethod
    def open(cls, channel, preset: str) -> Optional["HookNotices"]:
        """The channel for one invocation, or None: no session channel, or a preset with no hook."""
        if channel is None or preset not in HOOK_PRESETS:
            return None
        return cls(channel, Path(tempfile.mkdtemp(prefix="looplab-notices-")))

    def argv(self) -> list[str]:
        """The flags that load the hook for this invocation only."""
        return ["--settings", str(self.root / "settings.json")]

    def _write_notices(self) -> None:
        tmp = self.root / "notices.json.tmp"
        tmp.write_text(json.dumps(self.offered, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.root / "notices.json")

    def pump(self) -> None:
        """Publish newly offered notices; record the ones the hook emitted. Never raises."""
        try:
            new = [n for n in (self.channel.offer() or [])
                   if isinstance(n, dict) and isinstance(n.get("text"), str)
                   and isinstance(n.get("hint_id"), str) and _HINT_ID.fullmatch(n["hint_id"])]
            if new:
                self.offered.extend({"hint_id": n["hint_id"], "text": n["text"]} for n in new)
                self._write_notices()
        except Exception:  # noqa: BLE001 — a notice channel fault must never stop the agent it informs
            pass
        self._collect()

    def _collect(self) -> None:
        """Turn each mark the hook left into the session's receipt, once. Never raises."""
        try:
            for mark in sorted((self.root / "delivered").iterdir()):
                if mark.name not in self.acked and any(n["hint_id"] == mark.name for n in self.offered):
                    self.acked.add(mark.name)
                    self.channel.acknowledge(mark.name, channel="cli_hook")
        except Exception:  # noqa: BLE001 — a notice channel fault must never stop the agent it informs
            return

    def close(self) -> None:
        """Record what the hook emitted before the agent exited — offering nothing new, since no
        hook will run again — then remove the directory."""
        self._collect()
        shutil.rmtree(self.root, ignore_errors=True)
