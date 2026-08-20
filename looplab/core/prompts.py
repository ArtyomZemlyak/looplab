"""Prompt store (I18, ADR-8): role prompt bodies live as editable Markdown files and
are re-read on every use (hot-reload), so they can be tuned without code changes / a
restart. Templates use ``$var`` (string.Template) so JSON braces in prompts don't clash.
Missing file or no store -> the built-in default is used.
"""
from __future__ import annotations

import re
import string
from pathlib import Path
from typing import Optional

# Anchored to the START of the string (\A), NOT ^…MULTILINE: a prompt body may use `---` as Markdown
# horizontal rules, and a MULTILINE `^---` matches between ANY two of them, silently deleting the
# section in between (Section A vanishes from a body like "intro\n---\nA\n---\nB"). Only a genuine
# leading YAML frontmatter block (the file's FIRST line is `---`) is stripped.
_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---[ \t]*\r?\n", re.DOTALL)


def _strip_frontmatter(text: str) -> str:
    return _FRONTMATTER.sub("", text, count=1)


class PromptStore:
    def __init__(self, directory: Optional[str] = None):
        self.dir = Path(directory) if directory else None

    def get(self, name: str, default: str = "", /, **vars) -> str:
        # `name`/`default` are positional-only (the `/`): otherwise a template variable named `name`
        # or `default` passed through **vars collides with these params and raises TypeError instead
        # of substituting `$name`/`$default`. Positional-only frees the whole **vars namespace.
        text = default
        if self.dir is not None:
            f = self.dir / f"{name}.md"
            # Read-then-tolerate, not exists()-then-read: the override is re-read on EVERY call for
            # hot reload, which invites live editing, so the file can vanish between the check and the
            # open — and the read itself can fail (permissions, a transient FUSE error). Either one
            # used to crash the calling ROLE, where the documented behaviour for a missing override is
            # simply the built-in default.
            try:
                # utf-8-sig strips a BOM so a Windows-edited prompt's frontmatter still matches ^---.
                raw = f.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                raw = None
            if raw is not None:
                text = _strip_frontmatter(raw).strip()
        return string.Template(text).safe_substitute(vars)


# The overridable prompt-key REGISTRY (docs/15 §P4.7): every `render(prompts, "<key>", …)` call
# site must use a key listed here — `tests/test_prompt_keys.py` source-scans both directions
# (the same discipline as event types / hints / signals). Why: an override lands as
# `<prompt_dir>/<key>.md`, so a typo'd KEY at a call site (or a renamed key with a stale
# override file) silently falls back to the built-in default — no error, the operator's tuned
# prompt just stops applying.
PROMPT_KEYS: tuple[str, ...] = (
    "researcher_system", "tool_researcher_system",
    "developer_system", "developer_repair_prefix",
    "repo_developer_system_intro", "repo_developer_system_body", "repo_onboarder_system",
    "strategist_system", "tool_strategist_system",
    "pilot_system", "triage_system",
    # F8's repair CRITIC, a separate key from `triage_system` for the same reason
    # `concept_consolidate_system` is separate from `merge_system`: it is a different job (is this
    # chain circling? vs what do I change next?) and folding it into the triage prompt would change
    # the shipped text of a paid agent whose verdict is the loop's primary stop.
    "repair_critic_system",
    # The triage judge's LOOK invitation — the sentence that tells it the stderr tail may be about a
    # different phase than the one it is diagnosing, spliced only when `repair_log_tools` actually
    # wired the log tools. A SEPARATE key from `triage_system` because it is CONDITIONAL text: an
    # operator override of the system prompt must not lose it, and (the direction that matters more)
    # `repair_log_tools=false` must keep reproducing the historical message byte for byte, which it
    # cannot if the sentence lives inside a prompt that is always rendered.
    "triage_look_invitation",
    # The other half of that look: the sentence that asks the triage judge to write down WHAT IT
    # FOUND AND WHERE, as `findings`. A separate key for both of `triage_look_invitation`'s reasons
    # and for one of its own — it is the ask that fills a durable column, so an operator who wants
    # the record shaped differently (fewer entries, a house citation format) must be able to reword
    # it without touching the diagnosis prompt the verdict itself comes from.
    "triage_findings_invitation",
    "deep_research_system", "foresight_system", "merge_system", "bestofn_judge_system",
    # The concept-vocabulary consolidation prompt (doc 25 SE-10). A SEPARATE key from
    # `merge_system`: consolidating an axis/slug vocabulary is a different job from the generic
    # item merge, and collapsing the two would have changed the shipped text for a paid agent.
    "concept_consolidate_system",
)


def render(store: Optional[PromptStore], name: str, default: str, /, **vars) -> str:
    """Resolve a prompt via the store (if any) or the inline default; render $vars."""
    # `store`/`name`/`default` positional-only (the `/`) for the same reason as PromptStore.get:
    # a `$name`/`$default`/`$store` template variable must be free to pass through **vars.
    if store is not None:
        return store.get(name, default, **vars)          # positional: default is positional-only now
    return string.Template(default).safe_substitute(vars)
