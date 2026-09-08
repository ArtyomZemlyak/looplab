"""Prompt store (I18, ADR-8): role prompt bodies live as editable Markdown files and
are re-read on every use (hot-reload), so they can be tuned without code changes / a
restart. Templates use ``$var`` (string.Template) so JSON braces in prompts don't clash.
Missing file or no store -> the built-in default is used.

THE BUNDLE HAS AN IDENTITY NOW, AND HOT RELOAD IS KEPT (doc 27,
`prompt-bundle-unpinned-across-hot-reload`). Re-reading on every use is the feature — an operator
tunes a prompt and the next phase picks it up — but until this landed there was nothing to say
WHICH bytes a phase was handed: the only run-start pins are the settings in `run_started` plus the
two `core/setup_identity.py` digests (task payload + sorted config/workspace manifest), and none of
the three covers `<prompt_dir>/*.md`. So an operator who edited `researcher_system.md` at node 7
gave nodes 0-6 and 8-N different treatment of identical inputs, and no record anywhere could tell
the two halves apart.

`revision()` / `bundle_revision()` answer "which bytes", and `pin()` freezes that answer for the
caller that wants one. The pin DELIBERATELY DOES NOT LOCK THE TEXT: doc 27's own prescription is to
pin a bundle per run or phase "while retaining hot reload for future phases/runs", and refusing a
freshly edited override mid-run would change shipped behaviour for every operator who tunes a prompt
live. A pinned store therefore keeps serving the LIVE body and records the divergence
(`divergences()`, one WARNING per key per new revision), which is the half that was missing: the
run can say the bundle moved under it, and where.

THE REGISTRY IS TYPED (doc 27, `prompt-governance-has-no-typed-registry`). `PROMPT_KEYS` was a bare
tuple of strings, so a key carried no statement of what it governs or who renders it, and the
fragmentation the review measured — Genesis, assistants, reports, monitors and stewards each keeping
their own hard-coded prompt family outside this store — was prose in a document rather than anything
a guard could count. `PromptDefinition` carries the key, its FAMILY and one line about what it
governs; `PROMPT_KEYS` is derived from it so nothing re-spells the list; and
`UNGOVERNED_PROMPT_FAMILIES` names the families still outside the store beside the module that
hard-codes each one, which makes the gap shrink-only instead of anecdotal.
"""
from __future__ import annotations

import logging
import re
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from looplab.core.jsonutil import canonical_json_digest

_LOG = logging.getLogger(__name__)

# Anchored to the START of the string (\A), NOT ^…MULTILINE: a prompt body may use `---` as Markdown
# horizontal rules, and a MULTILINE `^---` matches between ANY two of them, silently deleting the
# section in between (Section A vanishes from a body like "intro\n---\nA\n---\nB"). Only a genuine
# leading YAML frontmatter block (the file's FIRST line is `---`) is stripped.
_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---[ \t]*\r?\n", re.DOTALL)

# The prefix every revision this module mints carries, so a revision is recognisable as one wherever
# it is written down. Minted through `core/jsonutil.py::canonical_json_digest`, the ONE minter of
# this repo's digest shape — never a second `hashlib` call here, or the shape drifts from the
# reader (`valid_digest_ref`) that already knows it.
REVISION_PREFIX = "sha256:"

# A key with no override file has NO revision, and this is deliberately not a digest of "".
# "the operator has not overridden this prompt" and "the operator's override happens to be empty"
# are different facts with different consequences under a pin — the first is the built-in default,
# whose identity is the commit, and the second is a live file that can change again. An empty
# string is the one value no digest can collide with.
NO_REVISION = ""


def _strip_frontmatter(text: str) -> str:
    return _FRONTMATTER.sub("", text, count=1)


def _revision(body: str) -> str:
    """The identity of one resolved prompt BODY.

    Taken over the body a role is handed (frontmatter stripped, `$var` NOT yet substituted), and not
    over the file's raw bytes: frontmatter is metadata the model never sees, so an edit there is not
    a change of treatment and must not read as one, while the substituted vars are per-CALL data
    rather than bundle identity — digesting those would make every call its own revision and the pin
    would say nothing.

    `canonical_json_digest` fails closed (None) rather than raising; a body is a `str` and therefore
    always has a canonical form, so the `or NO_REVISION` is the belt this module never expects to
    need and would rather have than an exception inside a prompt render.
    """
    return canonical_json_digest(body, prefix=REVISION_PREFIX) or NO_REVISION


@dataclass(frozen=True)
class PromptDefinition:
    """One overridable prompt: the key its file is named after, the FAMILY that renders it, and one
    line saying what it governs.

    Frozen and additive. The `key` alone is what the two-way source scan
    (`tests/test_prompt_keys.py`) has always checked; `family` is what makes "prompt governance is
    fragmented" countable rather than a sentence in a review — every governed family appears here
    with its rows, and every family that is NOT here is either a rename nobody registered or one of
    the `UNGOVERNED_PROMPT_FAMILIES` below, which names where its text is hard-coded instead.

    `what` is for the operator writing the override file, and it is the one field with no guard: a
    wrong sentence here misleads a human, which is why it states the JOB rather than the wording (a
    prompt's wording is a contract and lives in the module that owns the default).
    """

    key: str
    family: str
    what: str


# Every family a definition may claim, derived below from the rows themselves rather than declared
# beside them: a hand-kept vocabulary and a hand-kept row list disagree exactly the way
# `docs/BACKLOG.md` §0.8 measured for the claim/verdict join, and the rows are the half that has to
# be right. A typo'd family therefore shows up as a family of one, which
# `tests/test_prompt_keys.py` reports.
#
# The overridable prompt-key REGISTRY (docs/15 §P4.7): every `render(prompts, "<key>", …)` call
# site must use a key listed here — `tests/test_prompt_keys.py` source-scans both directions
# (the same discipline as event types / hints / signals). Why: an override lands as
# `<prompt_dir>/<key>.md`, so a typo'd KEY at a call site (or a renamed key with a stale
# override file) silently falls back to the built-in default — no error, the operator's tuned
# prompt just stops applying.
PROMPT_REGISTRY: tuple[PromptDefinition, ...] = (
    PromptDefinition("researcher_system", "researcher",
                     "the single-shot Researcher's system prompt (no tool surface)"),
    PromptDefinition("tool_researcher_system", "researcher",
                     "the agentic Researcher's system prompt (reads the run before proposing)"),
    PromptDefinition("developer_system", "developer",
                     "the script Developer's system prompt — one fenced program per node"),
    PromptDefinition("developer_repair_prefix", "developer",
                     "the prefix that turns a Developer call into a repair of a failed node"),
    PromptDefinition("repo_developer_system_intro", "developer",
                     "the repo Developer's opening — who it is and what tree it is editing"),
    PromptDefinition("repo_developer_system_body", "developer",
                     "the repo Developer's working rules (phases, writes, the manifest)"),
    PromptDefinition("repo_onboarder_system", "onboarding",
                     "the run-start agent that proposes a ratifiable eval adapter/spec"),
    PromptDefinition("strategist_system", "strategist",
                     "the single-shot Strategist's system prompt (meta-control knobs)"),
    PromptDefinition("tool_strategist_system", "strategist",
                     "the agentic Strategist's system prompt (reads the run before deciding)"),
    PromptDefinition("pilot_system", "pilot",
                     "the pilot's system prompt — the facade's own bounded session"),
    PromptDefinition("triage_system", "triage",
                     "the crash diagnostician: what did this failed evaluation fail of"),
    # F8's repair CRITIC, a separate key from `triage_system` for the same reason
    # `concept_consolidate_system` is separate from `merge_system`: it is a different job (is this
    # chain circling? vs what do I change next?) and folding it into the triage prompt would change
    # the shipped text of a paid agent whose verdict is the loop's primary stop.
    PromptDefinition("repair_critic_system", "triage",
                     "the repair critic: is this repair chain circling rather than converging"),
    # The triage judge's LOOK invitation — the sentence that tells it the stderr tail may be about a
    # different phase than the one it is diagnosing, spliced only when `repair_log_tools` actually
    # wired the log tools. A SEPARATE key from `triage_system` because it is CONDITIONAL text: an
    # operator override of the system prompt must not lose it, and (the direction that matters more)
    # `repair_log_tools=false` must keep reproducing the historical message byte for byte, which it
    # cannot if the sentence lives inside a prompt that is always rendered.
    PromptDefinition("triage_look_invitation", "triage",
                     "the conditional invitation to READ a named stage log, not only the tail"),
    # The other half of that look: the sentence that asks the triage judge to write down WHAT IT
    # FOUND AND WHERE, as `findings`. A separate key for both of `triage_look_invitation`'s reasons
    # and for one of its own — it is the ask that fills a durable column, so an operator who wants
    # the record shaped differently (fewer entries, a house citation format) must be able to reword
    # it without touching the diagnosis prompt the verdict itself comes from.
    PromptDefinition("triage_findings_invitation", "triage",
                     "the ask that fills the durable `findings` column: what was found, and where"),
    PromptDefinition("deep_research_system", "research",
                     "the Deep-Research stage's system prompt — the memo that steers the batch"),
    PromptDefinition("foresight_system", "judge",
                     "the foresight ranker over candidate ideas"),
    PromptDefinition("merge_system", "consolidation",
                     "the generic near-duplicate item merge (hypothesis board consolidation)"),
    PromptDefinition("bestofn_judge_system", "judge",
                     "the best-of-N judge that ranks one batch of candidate programs"),
    # The concept-vocabulary consolidation prompt (doc 25 SE-10). A SEPARATE key from
    # `merge_system`: consolidating an axis/slug vocabulary is a different job from the generic
    # item merge, and collapsing the two would have changed the shipped text for a paid agent.
    PromptDefinition("concept_consolidate_system", "consolidation",
                     "the concept axis/slug vocabulary consolidator"),
)

# DERIVED, never re-spelled. This is the name every existing reader imports
# (`tests/test_prompt_keys.py`, `ui/src/panels.jsx`'s operator note, `tests/test_concept_graph.py`),
# and its contract is unchanged: a tuple of override-file keys in registry order.
PROMPT_KEYS: tuple[str, ...] = tuple(d.key for d in PROMPT_REGISTRY)

# Likewise derived. Sorted so the vocabulary reads as a set rather than as an accident of row order.
PROMPT_FAMILIES: tuple[str, ...] = tuple(sorted({d.family for d in PROMPT_REGISTRY}))

# THE RESIDUE, named so it can shrink. Doc 27's row 134 ("prompt governance covers a bounded
# registry while Genesis, assistants, reports, monitors and stewards keep separate prompt families")
# was true and uncountable: nothing in the tree said which families those were or where their text
# lived, so "canonical prompt store" stayed partially true forever with no way to measure the part.
#
# Each row is a family name that MUST NOT appear in `PROMPT_FAMILIES` above, beside the module whose
# module-level constant is the hard-coded text. Migrating one means adding its `PromptDefinition`
# rows, wiring a `render(prompts, "<key>", <the existing constant>)` at the call site with the
# shipped text as the byte-for-byte default, and deleting the row here — the same one-family-at-a-
# time, byte-identical migration `repo_onboarder_system` already went through.
#
# NOT A CENSUS of every hard-coded prompt in the tree, and it does not claim to be: it is the five
# families doc 27 named, which is the population the open item was written about. A sixth
# discovered later is a row to add, not a reason to distrust these five.
UNGOVERNED_PROMPT_FAMILIES: tuple[tuple[str, str], ...] = (
    ("genesis", "serve/serve_prompts.py::genesis_system + RESEARCH_BRIEF_SYSTEM"),
    ("assistant", "serve/serve_prompts.py::COMMAND_SYSTEM / CHAT_SYSTEM / COMPACT_SYSTEM"),
    ("report", "serve/report.py::_SYSTEM"),
    ("monitor", "engine/train_monitor.py::_MONITOR_SYSTEM, "
                "engine/asha_monitor.py::_ASHA_JUDGE_SYSTEM"),
    ("steward", "engine/claim_steward.py (inline system turn at the steward's one call site)"),
)


def definition(key: str) -> Optional[PromptDefinition]:
    """The registry row for `key`, or None for a key nothing registers.

    None rather than a raise: every consumer of this is a reporter (the settings UI, a guard, an
    operator listing), and a prompt key that fell out of the registry is exactly what such a
    reporter exists to SHOW rather than to crash on.
    """
    for d in PROMPT_REGISTRY:
        if d.key == key:
            return d
    return None


class PromptStore:
    def __init__(self, directory: Optional[str] = None):
        self.dir = Path(directory) if directory else None
        # UNPINNED BY DEFAULT, so every existing construction is byte-identical and pays nothing:
        # `get()` only digests what it read when a pin exists to compare against. `pin()` is the
        # single writer of both, and `_diverged` is what `divergences()` reports.
        self._pinned: Optional[dict] = None
        self._diverged: dict = {}

    def _read(self, name: str) -> Optional[str]:
        """The override BODY for `name`, or None when there is no usable override file.

        Hoisted out of `get()` so `revision()` resolves a key exactly the way a render does — a
        second reader of "what would this key resolve to" is a second answer, and the pin would then
        be taken over bytes no role was ever handed.
        """
        if self.dir is None:
            return None
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
            return None
        return _strip_frontmatter(raw).strip()

    def get(self, name: str, default: str = "", /, **vars) -> str:
        # `name`/`default` are positional-only (the `/`): otherwise a template variable named `name`
        # or `default` passed through **vars collides with these params and raises TypeError instead
        # of substituting `$name`/`$default`. Positional-only frees the whole **vars namespace.
        text = default
        body = self._read(name)
        if body is not None:
            text = body
        if self._pinned is not None:
            # ONE read, then the comparison — never a second `_read` here. A pin check that re-reads
            # doubles the file I/O of every render AND can compare a key against a version of itself
            # it never served, because the two reads straddle exactly the live edit this is watching
            # for.
            self._note_divergence(name, NO_REVISION if body is None else _revision(body))
        return string.Template(text).safe_substitute(vars)

    # ------------------------------------------------------------------ bundle identity (doc 27)

    def revision(self, name: str, /) -> str:
        """The identity of the override bytes this store would resolve `name` from RIGHT NOW.

        `NO_REVISION` ("") when there is no store, no directory or no readable override — the
        built-in default, whose identity is the commit and not this file.
        """
        body = self._read(name)
        return NO_REVISION if body is None else _revision(body)

    def bundle_revision(self, keys: "tuple[str, ...] | None" = None) -> str:
        """ONE digest over the whole override bundle — the run/phase-pinnable prompt identity.

        Over the {key: revision} MAP and not over a concatenation of bodies: a concatenation lets
        content move between two keys without changing the hash, which is the same forgery
        `research_cadence.research_memo_sig` uses a record separator to refuse. The map is keyed, so
        a body that moves from `researcher_system.md` to `developer_system.md` is a different bundle.

        Keys with no override are INCLUDED with `NO_REVISION`, because "this key is not overridden"
        is part of what the bundle is: a run whose `developer_system.md` appears at node 7 has a
        different bundle from the one that started, and a map holding only the files that exist
        would hash the same before and after a DELETION plus an unrelated addition.
        """
        names = PROMPT_KEYS if keys is None else tuple(keys)
        return canonical_json_digest({k: self.revision(k) for k in names},
                                     prefix=REVISION_PREFIX) or NO_REVISION

    def pin(self, keys: "tuple[str, ...] | None" = None) -> dict:
        """Freeze what this store resolves each key to now, and start reporting divergence.

        Returns the manifest ({key: revision}) so the caller can record it — a run stamping it into
        its own start receipt is the shape doc 27 prescribes ("stamp a content hash per rendered
        prompt family"), and it is a caller's decision rather than this module's because only the
        caller knows what a phase is.

        THE PIN DOES NOT LOCK THE TEXT, deliberately. Hot reload is the store's whole purpose and
        doc 27's own prescription keeps it ("while retaining hot reload for future phases/runs"); a
        pin that refused a freshly edited override would change shipped behaviour for every operator
        who tunes a prompt live, and would do it silently in the middle of a paid phase. What was
        missing is not a lock but a RECORD, so a pinned store keeps serving the live body and says —
        once per key per new revision — that the bundle moved under it.

        Re-pinning is legal and is how a caller opens a new phase: it replaces the manifest and
        clears what has been reported, so the next divergence is measured against the new baseline.
        """
        names = PROMPT_KEYS if keys is None else tuple(keys)
        self._pinned = {k: self.revision(k) for k in names}
        self._diverged = {}
        return dict(self._pinned)

    @property
    def pinned(self) -> Optional[dict]:
        """The pinned manifest, or None for an unpinned store. A COPY: the manifest is the baseline
        every divergence is measured against, and a caller that could mutate it in place would move
        the baseline under the comparison rather than re-pin."""
        return None if self._pinned is None else dict(self._pinned)

    def divergences(self) -> dict:
        """{key: (pinned_revision, live_revision)} for every pinned key whose bytes have since
        moved. Empty for an unpinned store and for a bundle nobody has edited.

        A COPY, for the same reason `pinned` is: this is a report, and a reader that can edit the
        report can erase the fact that a phase ran on text nobody pinned.
        """
        return dict(self._diverged)

    def _note_divergence(self, name: str, live: str) -> None:
        """Record (and log once) that `name` no longer resolves to what was pinned.

        A key that is not in the manifest is not a divergence at all: the caller pinned a bundle,
        and a render of something outside it — an unregistered key, or a key a later commit added —
        has no baseline to have moved from. Reporting one would make every pin noisy in exactly the
        case where it knows nothing.

        ONE WARNING PER KEY PER REVISION, keyed on the live revision rather than on the key: an
        operator who edits a prompt back to the pinned bytes has ENDED the divergence (the entry is
        dropped), and one who edits it twice has caused two, which is two facts and two lines. A
        per-key-once rule would report the first and hide every later one, and the store is re-read
        on every render, so a per-call rule would print one line per paid call.
        """
        pinned = self._pinned.get(name) if self._pinned is not None else None
        if pinned is None or pinned == live:
            self._diverged.pop(name, None)
            return
        if self._diverged.get(name) != (pinned, live):
            self._diverged[name] = (pinned, live)
            _LOG.warning(
                "prompt override %r changed under a pinned bundle (pinned %s, live %s) — this "
                "phase is running on text the run did not pin; hot reload is kept on purpose, so "
                "this is a record and not a refusal", name, pinned, live)


def render(store: Optional[PromptStore], name: str, default: str, /, **vars) -> str:
    """Resolve a prompt via the store (if any) or the inline default; render $vars."""
    # `store`/`name`/`default` positional-only (the `/`) for the same reason as PromptStore.get:
    # a `$name`/`$default`/`$store` template variable must be free to pass through **vars.
    if store is not None:
        return store.get(name, default, **vars)          # positional: default is positional-only now
    return string.Template(default).safe_substitute(vars)
