"""Slash commands for the assistant (Claude-Code style): `/name args` expands into a full instruction
prompt before the model sees it. Built-in, prompt-template only — no code execution here; the assistant
then uses its tools to carry the instruction out. `$ARGS` is replaced by whatever follows the command.
"""
from __future__ import annotations

COMMANDS = {
    "init": {
        "desc": "Analyze the repo and write/update CLAUDE.md",
        "template": ("Analyze this repository (read the README, pyproject, top-level packages and a few "
                     "key modules) and write or update a concise CLAUDE.md at the repo root: what the "
                     "project does, how to build/test/run it, the main modules, and any conventions a "
                     "new contributor needs. Propose the file for approval."),
    },
    "review": {
        "desc": "Review the current git diff",
        "template": ("Review the current uncommitted changes (use git_diff). Look for correctness bugs, "
                     "risky edits, and simple improvements. Summarize findings as a short list; do not "
                     "change anything unless I ask."),
    },
    "commit": {
        "desc": "Stage and commit the current changes",
        "template": ("Stage the current changes (git_add) and create a git commit (git_commit) with a "
                     "clear, conventional message summarizing them. Show me git_status first. $ARGS"),
    },
    "test": {
        "desc": "Run the tests (optionally a path) and report",
        "template": ("Run the tests ($ARGS if given, else the whole suite) with run_tests / run_command "
                     "and report the result. If anything fails, read the failure and propose a fix."),
    },
    "explain": {
        "desc": "Explain a file or part of the code",
        "template": "Read $ARGS and explain what it does, its key pieces, and how it fits the codebase.",
    },
    "fix": {
        "desc": "Diagnose and fix an issue",
        "template": ("Diagnose and fix: $ARGS. Inspect the relevant code first, propose the change, and "
                     "(once approved / in an auto mode) apply it and run the tests to confirm."),
    },
}


def expand_command(text: str) -> str:
    """If `text` is `/name args`, return the expanded instruction; otherwise return `text` unchanged."""
    if not text or not text.startswith("/"):
        return text
    parts = text[1:].split(None, 1)
    name = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""
    cmd = COMMANDS.get(name)
    if not cmd:
        return text
    return cmd["template"].replace("$ARGS", args).strip()


def list_commands() -> list[dict]:
    return [{"name": k, "desc": v["desc"]} for k, v in COMMANDS.items()]
