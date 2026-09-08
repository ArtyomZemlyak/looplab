"""The role-WRAPPER contracts: what a wrapper must forward, and the validating wrapper (doc 25 AG-02).

`WrapsResearcher` / `WrapsDeveloper` are the forwarding halves every wrapper in the tree inherits
(`search/panel.py`, `search/surrogate.py`, `search/foresight.py`, `search/best_of_n.py` and the
`UnifiedAgent` facade), `bind_state_on` is the arity rule two of them share, and
`ValidatingDeveloper` is the ADR-7 audit wrapper around an external coding agent. The finding names
this as the follow-up split, and the reason is the reading cost: the wrapper contract is what a
maintainer reaches for when adding a wrapper, and it sat 1,500 lines down a module whose first
half is prompt prose.

`roles.py` re-exports all four, so `from looplab.agents.roles import WrapsDeveloper` — spelled by
`agents/unified_agent.py`, `search/best_of_n.py`, `search/panel.py`, `search/surrogate.py`,
`search/foresight.py` and `agents/factory.py` — keeps naming the SAME objects.

THE ONE IMPORT BACK INTO `roles.py` IS DEFERRED, inside `ValidatingDeveloper._record`, and the
comment there says why: `roles.py` imports this module to re-export it, so a module-level import in
the other direction closes a cycle whose failure depends on which of the two a process imports
FIRST — the exact shape `agents/factory.py`'s docstring records for `agents` vs `search`.
"""
from __future__ import annotations

import inspect
from typing import Optional

from looplab.core.models import Idea
from looplab.core.validate import AgentReport, validate_agent_code


class WrapsResearcher:
    """Forwarding half of the Researcher-WRAPPER contract (`PanelResearcher`, `SurrogateResearcher`,
    `ForesightPanelResearcher`) — the parity sibling of `WrapsDeveloper` below (doc 25 SE-02).

    Only what all three genuinely share lives here: the delegate handle and `space_hint`. The three
    wrappers were each written with their own forwarding strategy and each carries a comment
    recording a bug that strategy once caused, so the temptation is to merge all of it. That would be
    wrong, and the divergences are load-bearing rather than accidental:

    * `client` is NOT forwarded here. `SurrogateResearcher` deliberately does not surface its
      fallback's client, because `cli/__init__.py` gates foresight wiring on
      ``getattr(researcher, "client", None) is not None`` — a bare surrogate wrapper must fall
      through to None or that gate flips on. `PanelResearcher` DOES forward it, because a missing
      attr there silently shadowed the run's configured client behind the defaults. Both are right
      for their wrapper; one shared rule cannot be.
    * `parser` / `prompts` are forwarded by `PanelResearcher` for that same shadowing reason and are
      left to the wrapper for the same reason `client` is.
    * `ForesightPanelResearcher` delegates everything else through a catch-all ``__getattr__`` so it
      can wrap a UNIFIED agent, where the researcher IS the developer and the whole developer surface
      must pass through the same object. A per-attr base cannot express that and must not fight it.

    Delegation target: `_delegate` (defaults to `base`). `SurrogateResearcher` overrides it — its
    wrapped researcher is `fallback`, and it may legitimately be None.
    """

    @property
    def _delegate(self):
        return getattr(self, "base", None)

    @property
    def space_hint(self) -> str:
        return getattr(self._delegate, "space_hint", "")


def bind_state_on(target, state, parent=None) -> None:
    """Call `target.bind_state` with whichever arity it declares; a no-op when it has none.

    `tools/_base.py`'s contract is `bind_state(state, parent=None)`, but a developer is not a
    ToolProvider and nothing obliges it to take the second argument, so a one-argument
    implementation must not become a TypeError that kills the build. Decided from the SIGNATURE
    rather than by catching TypeError around the call: a `TypeError` raised from INSIDE the callee's
    own body is indistinguishable from an arity mismatch at the boundary, and retrying on it would
    run a state binding twice.

    A FREE FUNCTION because two callers need it and they bind different objects — the forwarder
    below binds whatever it wraps, and `UnifiedAgent` binds every per-stage backend it holds. A
    second spelling of the arity rule is how one of them comes to call a developer wrong.

    The signature is asked to BIND the call, never merely COUNTED. A count answers `>= 2` for
    `bind_state(self, state, **kw)` and for `bind_state(self, state, *, parent=None)` — the natural
    way to write "accepted and ignored" — and then makes the positional two-argument call that
    raises the exact `TypeError` this function exists to avoid, out of an unguarded forwarder and
    into `node_build._implement`, killing the build. It answers `1` for `bind_state(self, *args)`
    and silently drops `parent` on a callee that wanted it. `Signature.bind` decides by KIND, which
    is the property actually in question, and the three attempts are ordered widest-first so a
    callee that can take `parent` either way still gets it."""
    fn = getattr(target, "bind_state", None)
    if not callable(fn):
        return
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):     # a builtin/C callable exposes no signature
        fn(state)
        return
    for args, kwargs in (((state, parent), {}), ((state,), {"parent": parent}), ((state,), {})):
        try:
            sig.bind(*args, **kwargs)
        except TypeError:
            continue
        fn(*args, **kwargs)
        return


class WrapsDeveloper:
    """Forwarding half of the Developer-WRAPPER contract (`ValidatingDeveloper`,
    `BestOfNDeveloper`, `UnifiedAgent`). A wrapper composes an inner Developer and must stay
    transparent to every duck-typed probe the engine/factories make against `developer`:

    - `inner` (plain attribute, set by the wrapper's ``__init__``): the wrapped Developer.
      The engine's ablation probe reads ``getattr(developer, "inner", developer)`` to bypass
      wrapper retry/fallback/best-of-N machinery (``orchestrator._probe_developer``), so
      `inner` must always be the raw developer a probe should hit.
    - `brief` / `is_code_generating` / `honors_idea_space` / `answers_with_code` / `client` /
      `prompts` / `last_report`:
      read-through (and, for `client`/`prompts`, hasattr-guarded write-through) to the wrapped
      developer — `make_roles` pokes `prompts`, H3 per-role rewiring pokes `client`, T8/A0b
      merge_mode="auto" resolution reads `is_code_generating`, `make_roles`'s sweep gate reads
      `honors_idea_space`, and the orchestrator reads `last_report` for the `agent_validated`
      audit event.
    - `last_files` / `last_deleted` / `last_footprint`: per-call output attributes the orchestrator
      reads AFTER implement/repair. Wrappers own them as plain attributes: either mirrored from the
      wrapped developer via `_sync_audit()`, or set by the wrapper's own logic (e.g.
      best-of-N's chosen candidate, the validator's fell-back handling).
    - `audit_extra()`: wrapper-specific audit fields merged into the `agent_validated` event.

    Delegation target: `_wrapped` (defaults to `inner`). `UnifiedAgent` overrides it — its
    delegate is `self.developer` (possibly itself a wrapper) while its `inner` exposes the
    fully-unwrapped probe developer.

    A wrapper whose semantics for a member differ from these defaults keeps that member local
    (e.g. `ValidatingDeveloper`'s unconditional `prompts` setter, its agent-vs-fallback
    `is_code_generating`/`last_report`, and `UnifiedAgent`'s locally-held `prompts` handle).
    """

    @property
    def _wrapped(self):
        return self.inner

    # forward the hooks make_roles / the engine poke at, to the wrapped developer
    @property
    def brief(self) -> str:
        return getattr(self._wrapped, "brief", "")

    # T8/A0b: capability follows the wrapped developer (merge_mode="auto" resolution)
    @property
    def is_code_generating(self) -> bool:
        return bool(getattr(self._wrapped, "is_code_generating", False))

    # P6/P21: so does the sweep capability — `make_roles` reads it off the (possibly wrapped)
    # developer to decide whether to offer the Researcher an `idea.space` grid at all.
    @property
    def honors_idea_space(self) -> bool:
        return bool(getattr(self._wrapped, "honors_idea_space", False))

    # C5/C2: and so does "is this Developer's answer the thing best-of-N ranks?" — `make_roles`
    # reads it off the (possibly wrapped) developer to decide whether `best_of_n > 1` can be
    # honoured at all. Forwarded read-through so a wrapper never makes a repo Developer look
    # rankable.
    @property
    def answers_with_code(self) -> bool:
        return bool(getattr(self._wrapped, "answers_with_code", False))

    @property
    def client(self):
        return getattr(self._wrapped, "client", None)

    @client.setter
    def client(self, value) -> None:        # H3 per-role client rewiring reaches the inner developer
        if hasattr(self._wrapped, "client"):
            self._wrapped.client = value

    @property
    def prompts(self):
        return getattr(self._wrapped, "prompts", None)

    @prompts.setter
    def prompts(self, value) -> None:
        if hasattr(self._wrapped, "prompts"):
            self._wrapped.prompts = value

    @property
    def last_report(self):
        return getattr(self._wrapped, "last_report", None)

    def audit_extra(self) -> dict:
        fn = getattr(self._wrapped, "audit_extra", None)
        return fn() if callable(fn) else {}

    def bind_state(self, state, parent=None) -> None:
        """Forward the run-state binding to the wrapped developer.

        `engine/node_build.py` binds with `getattr(developer, "bind_state", None)` on the FACADE,
        and under the shipped default (`Settings.unified_agent`) the facade is a `UnifiedAgent` —
        a wrapper, which had no `bind_state`. So the `getattr` answered None, nothing was called,
        and `LLMRepoDeveloper._memory_state` stayed None for the whole run: the Developer's
        `QuestionBoardTools` answered "no run state bound" on every call and its `CrossRunTools`
        (`audience="run"`) answered nothing, both shipped INERT under the default config. The
        provider-side comment beside that wiring warns about binding a name that does not exist;
        this is the same failure one layer up, where the NAME was right and the object reading it
        was the wrapper.

        A no-op when the wrapped developer has none, which is most of them (draft, offline,
        template), so this only ever forwards a binding somebody asked for.
        """
        bind_state_on(self._wrapped, state, parent)

    def _sync_audit(self) -> None:
        """Mirror the wrapped developer's per-call outputs onto this wrapper.

        EVERY member of `DEVELOPER_OUTPUT_ATTRS` the engine reads off `self.developer` has to be
        here, and THREE were not: `last_rollback_stage`, `last_budget_exhausted` and
        `last_edit_calls` are set on the INNER developer by `adapters/repo_developer.py`, while
        `engine/evaluate.py` reads them off the FACADE — and under the shipped default
        (`Settings.unified_agent`) the engine's developer is a `UnifiedAgent`, i.e. a wrapper. Each
        has a FALSY default at its reader, so the omission did not fail: every `node_repaired` row
        recorded "no rollback was requested" and
        "the session finished on its own terms", which is precisely the reading each attribute was
        added to stop being the only one available. `tests/test_developer_output_forwarding.py`
        derives the required set from the engine's own `getattr` sites.

        `last_seed` / `last_run` / `last_patch` were deliberately NOT mirrored until 2026-09-06:
        `ValidatingDeveloper` reads them off `self.inner` directly, so a wrapper copy was a second
        spelling with no reader. The `DeveloperResult` envelope capture
        (`engine/node_build.py::_capture_developer_result`, doc 52 row 12) now reads EVERY registry
        member off the ACTIVE developer — which under the shipped default is this facade — so the
        three are mirrored too, or the envelope would record them as absent on every facade build.
        `last_report` is a read-through property above.
        """
        self.last_files = getattr(self._wrapped, "last_files", {}) or {}
        self.last_deleted = getattr(self._wrapped, "last_deleted", []) or []
        self.last_footprint = getattr(self._wrapped, "last_footprint", None)
        self.last_rollback_stage = getattr(self._wrapped, "last_rollback_stage", "") or ""
        self.last_budget_exhausted = getattr(self._wrapped, "last_budget_exhausted", "") or ""
        self.last_edit_calls = getattr(self._wrapped, "last_edit_calls", 0) or 0
        self.last_seed = getattr(self._wrapped, "last_seed", None)
        self.last_run = getattr(self._wrapped, "last_run", None)
        self.last_patch = getattr(self._wrapped, "last_patch", None)
        # …and `last_budget_facts`, the tenth member, which was NOT mirrored until 2026-09-07 —
        # the same omission this docstring records for the three before it, in the same shape. The
        # inner `LLMRepoDeveloper` writes it when a session is cut off by its money or time ceiling;
        # under the shipped `unified_agent` default the engine's developer is THIS facade, so the
        # envelope capture read None and every build recorded "the session was never cut" — the
        # falsy default, on the corpus meant to settle whether the ceiling ever fires.
        self.last_budget_facts = getattr(self._wrapped, "last_budget_facts", None)


# --------------------------------------------------------------------------- #
# Validating wrapper (ADR-7): audit how an external coding agent performed
# --------------------------------------------------------------------------- #


def audit_extra_of(developer) -> Optional[dict]:
    """`developer.audit_extra()` as a plain dict, or None — total over anything a stub can do.

    Here rather than in the engine because it is part of the Developer contract this module owns,
    and because BOTH sides need it: `engine/node_build.py::_capture_developer_result` calls it
    inside the capture, under `developer_call_lock` (the wrapper builds the dict from its own
    instance state, and that state is only this call's while the lock is held), and
    `engine/audit.py::_emit_agent_report` calls it on the fallback path for a node whose build
    made no fresh Developer call. Total over junk like every read in the capture: a stub whose
    `audit_extra` raises, or returns a string, must read as "no annotation", never break a build.
    """
    fn = getattr(developer, "audit_extra", None)
    if not callable(fn):
        return None
    try:
        extra = fn()
    except Exception:  # noqa: BLE001 — an optional audit annotation must never break a build
        return None
    return dict(extra) if isinstance(extra, dict) else None


class ValidatingDeveloper(WrapsDeveloper):
    """Wrap a Developer and validate how it performed before the orchestrator spends a
    sandbox evaluation on its output (see `validate.py`).

    On an invalid result it re-prompts the *inner* developer with the failure folded
    into the Idea's rationale (a cheap correction loop), up to `max_retries` times. If it
    still can't produce valid code it falls back to the task's original in-process Developer:
    an LLM writer, deterministic/template Developer, or repo baseline. A flaky external agent
    therefore degrades to the adapter-owned known-good path instead of poisoning the search with a
    no-op/broken node.

    `last_report` holds the `AgentReport` for the most recent call; the orchestrator logs
    it as an `agent_validated` event, giving a per-node audit trail of the agent.

    Tool-agnostic: any Developer works as `inner`. If `inner` exposes `last_run` /
    `last_seed` (as `CliAgentDeveloper` does), the report also includes process-level
    checks (launched / not-timed-out / exit) and the no-op (`modified_seed`) check.
    """

    # `last_report` is genuinely LOCAL state — it always describes the EXTERNAL AGENT (even
    # when we fall back) — so shadow the mixin's live forwarder with a plain attribute.
    last_report: Optional[AgentReport] = None

    def __init__(self, inner, *, fallback=None, max_retries: int = 1,
                 metric_key: str = "metric", repo_mode: bool = False):
        self.inner = inner
        self.fallback = fallback
        self.max_retries = max_retries
        self.metric_key = metric_key
        # repo_mode: validate the agent's changed-FILE set (RepoTask), and treat the
        # fallback (a baseline / no-op developer) as always shippable — running the
        # unmodified repo is a valid result, not a failure.
        self.repo_mode = repo_mode
        # Audit of the most recent call. `last_report` always describes the EXTERNAL
        # AGENT (even when we fall back) — that's what we're auditing; the fallback's
        # validity is recorded separately in `last_shipped_ok`.
        self.last_report: Optional[AgentReport] = None
        self.last_attempts: int = 0
        self.last_fell_back: bool = False
        self.last_shipped_ok: bool = False
        self.last_files: dict[str, str] = {}   # multi-file output of the shipped attempt
        self.last_deleted: list[str] = []      # accepted in-surface deletions of the shipped attempt
        self.last_footprint: Optional[dict] = None  # resource estimate of the attempt that shipped

    # T8/A0b: code-generation capability combines the inner and task-owned fallback Developers —
    # kept local because, unlike the mixin's forwarder, both capabilities count here.
    @property
    def is_code_generating(self) -> bool:
        return bool(getattr(self.inner, "is_code_generating", False)
                    or getattr(self.fallback, "is_code_generating", False))

    # forward the prompt hook make_roles pokes at, to the wrapped developer — kept local:
    # the setter is UNCONDITIONAL (it must create the attribute on an inner that lacks one),
    # unlike the mixin's hasattr-guarded write-through.
    @property
    def prompts(self):
        return getattr(self.inner, "prompts", None)

    @prompts.setter
    def prompts(self, value) -> None:
        self.inner.prompts = value
        # An invalid external result eventually crosses this wrapper into the in-process fallback.
        # Prompt governance must follow that reachable leaf just like client rebinding does;
        # otherwise a developer_system/repair override disappears only on the recovery path.
        if self.fallback is not None and hasattr(self.fallback, "prompts"):
            self.fallback.prompts = value

    def _report(self, code: str, *, agent: bool) -> AgentReport:
        """Validate `code`. `agent=True` pulls the inner agent's process signal + seed
        (no-op detection) + patch-gate verdict; `agent=False` (fallback output) does
        static checks only."""
        return validate_agent_code(
            code,
            seed=getattr(self.inner, "last_seed", None) if agent else None,
            run=getattr(self.inner, "last_run", None) if agent else None,
            patch=getattr(self.inner, "last_patch", None) if agent else None,
            files=(getattr(self.inner, "last_files", {}) or {}) if (agent and self.repo_mode)
                  else None,
            metric_key=self.metric_key,
        )

    def _record(self, report: AgentReport, *, attempts: int, fell_back: bool,
                shipped_ok: bool) -> None:
        self.last_report = report
        self.last_attempts = attempts
        self.last_fell_back = fell_back
        self.last_shipped_ok = shipped_ok
        # Multi-file output only when the external agent itself shipped. A task-owned fallback
        # returns node.code (LLM/template) or the unchanged repo baseline, never that agent's patch.
        self.last_files = ({} if fell_back
                           else dict(getattr(self.inner, "last_files", {}) or {}))
        self.last_deleted = ([] if fell_back
                             else list(getattr(self.inner, "last_deleted", []) or []))
        # This output must describe the implementation that actually ships. In particular, a
        # rejected agent attempt must not leak its resource estimate onto fallback code.
        shipped = self.fallback if fell_back else self.inner
        self.last_footprint = getattr(shipped, "last_footprint", None)
        # …AND EVERY OTHER REGISTERED CHANNEL, off the SAME shipped developer. This wrapper set
        # three of the ten and never called `_sync_audit`, so on its shipping path the engine's
        # envelope read `last_rollback_stage`, `last_budget_exhausted`, `last_edit_calls`,
        # `last_seed`, `last_run`, `last_patch` and `last_budget_facts` as their FALSY defaults —
        # "no rollback was requested", "the session finished on its own terms", "zero edits" — which
        # is the reading `DEVELOPER_OUTPUT_ATTRS` exists to stop being the only one available.
        # `last_report` is excluded because this wrapper owns it: it describes the external AGENT
        # even when the FALLBACK shipped, which is the whole point of the `agent=` split above.
        #
        # THE REGISTRY IS IMPORTED INSIDE THE CALL (doc 25 AG-02), and it is the only edge from this
        # module back into `roles.py`: `roles.py` imports THIS module to re-export the wrapper
        # contract, so a module-level import in the other direction closes a cycle whose failure
        # depends on which of the two a process imports FIRST — green from `import roles`, an
        # ImportError from `import role_wrappers`. Deferring the one read keeps the edge one-way.
        from looplab.agents.roles import DEVELOPER_OUTPUT_ATTRS
        for _attr in DEVELOPER_OUTPUT_ATTRS:
            if _attr in ("last_files", "last_deleted", "last_footprint", "last_report"):
                continue
            try:
                setattr(self, _attr, getattr(shipped, _attr, None))
            except Exception:  # noqa: BLE001 - an optional audit channel must never block a build
                pass

    def _attempt_loop(self, idea: Idea, call, fallback_call=None) -> str:
        """Run `call(idea)` (implement or repair), validate, retry-with-feedback up to
        `max_retries`, then fall back via `fallback_call` (defaults to the fallback's
        implement). Records the agent audit on every path."""
        code, report = "", AgentReport()
        attempt = idea
        attempts = 0
        for _ in range(self.max_retries + 1):
            attempts += 1
            code = call(attempt)
            report = self._report(code, agent=True)
            if report.ok:
                self._record(report, attempts=attempts, fell_back=False, shipped_ok=True)
                return code
            attempt = idea.model_copy(deep=True)   # re-prompt with the failure as a hint
            attempt.rationale = (
                idea.rationale +
                f"\n[validator] the previous attempt was rejected: {report.feedback()}. "
                "Fix this in your changed files (the solution script / the repo files you edited).").strip()
        if self.fallback is not None:              # exhausted retries -> known-good path
            fb = (fallback_call or (lambda: self.fallback.implement(idea)))()
            # In repo mode the fallback is the baseline (no-op) developer — running the
            # unmodified repo is always a valid shippable result.
            fb_ok = True if self.repo_mode else self._report(fb, agent=False).ok
            # last_report still describes the external AGENT (it failed); the shipped result came
            # from the task-owned fallback (LLM writer, deterministic/template Developer, or repo
            # baseline according to the adapter).
            self._record(report, attempts=attempts, fell_back=True, shipped_ok=fb_ok)
            return fb
        self._record(report, attempts=attempts, fell_back=False, shipped_ok=report.ok)
        return code

    def audit_extra(self) -> dict:
        """Wrapper-specific audit fields merged into the `agent_validated` event so the
        log shows whether the agent succeeded, how many tries it took, and whether the task's
        original Developer fallback ran."""
        return {"attempts": self.last_attempts, "fell_back": self.last_fell_back,
                "shipped_ok": self.last_shipped_ok}

    def implement(self, idea: Idea) -> str:
        return self._attempt_loop(idea, self.inner.implement)

    def repair(self, idea: Idea, code: str, error: str) -> str:
        inner_repair = getattr(self.inner, "repair", None)
        if not callable(inner_repair):
            return self.implement(idea)
        # Fall back to the fallback's repair (preserving the error-feedback) when it has
        # one, else its implement — never lose the debug context on the fallback path.
        fb_repair = getattr(self.fallback, "repair", None) if self.fallback else None
        fb_call = (lambda: fb_repair(idea, code, error)) if callable(fb_repair) else None
        return self._attempt_loop(idea, lambda i: inner_repair(i, code, error), fb_call)

    def implement_from(self, idea: Idea, parent, *, co_parents=()) -> str:
        """Parent-aware implement, forwarded through the validation retry loop (arch-review §4 P1-9):
        without exposing this, the engine's `getattr(developer, 'implement_from')` capability probe
        saw only the validator's plain `implement` and regenerated the child from the pristine baseline
        (losing the parent's accumulated edits). Degrades to `implement` when the inner has no
        parent-aware path."""
        impl_from = getattr(self.inner, "implement_from", None)
        if not callable(impl_from):
            return self.implement(idea)
        if co_parents:
            from looplab.engine.node_build import accepts_co_parents
            if accepts_co_parents(impl_from):
                return self._attempt_loop(idea, lambda i: impl_from(i, parent, co_parents=co_parents))
        return self._attempt_loop(idea, lambda i: impl_from(i, parent))

    def repair_from(self, idea: Idea, node, error: str) -> str:
        """Node-aware repair, forwarded like `implement_from` — seed the fix from the FAILING node's own
        files. Falls back to the fallback's repair_from/repair, preserving the error feedback."""
        rf = getattr(self.inner, "repair_from", None)
        if not callable(rf):
            return self.repair(idea, getattr(node, "code", ""), error)
        fb_rf = getattr(self.fallback, "repair_from", None) if self.fallback else None
        fb_call = (lambda: fb_rf(idea, node, error)) if callable(fb_rf) else None
        return self._attempt_loop(idea, lambda i: rf(i, node, error), fb_call)
