"""Novelty / dedup gate (E1/T5) for the engine — extracted from orchestrator.py as a MIXIN:
`class Engine(NoveltyGateMixin, …)` inherits these methods unchanged, so there is ZERO
call-site churn and `self` here IS the engine. The method bodies are verbatim moves and read
engine attributes freely (`_embedder` / `_idea_vecs` cache / `store` / `researcher` /
`_novelty_*` knobs / `_reflect_client`), exactly as they did inside the class.

Two layers, cheapest first, BEFORE any compute is spent on a proposal: a SEMANTIC/LLM
near-duplicate check (reject + one informed re-propose) and the E1 NUMERIC param-distance nudge.
The heavy tool/agent imports stay method-local (imported from their source modules on use), so a
test monkeypatching `looplab.tools.vectorstore._cosine` / `looplab.agents.agent.agentic_struct`
still intercepts them.

Layering: no runtime import of the orchestrator (TYPE_CHECKING only) and never serve — only
core, events, the pure action-governance helper and stdlib (search/agent/tool deps stay lazy)."""
from __future__ import annotations

import json
import logging
import math
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Optional

from looplab.core.llm import BudgetExceeded
from looplab.core.llm_broker import in_llm_lane
from looplab.core.models import (NODE_CONCEPT_PROVENANCE_CLASSIFIER,
                                  NODE_CONCEPT_PROVENANCE_OPERATOR, Idea, NodeStatus, RunState,
                                  idea_proposal_digest, idea_proposal_ref)
from looplab.engine.action_governance import effective_researcher_eval_timeout
from looplab.core.tracing import current_ids
from looplab.events.types import EV_CROSS_RUN_PRIOR, EV_NOVELTY_GRADED, EV_NOVELTY_REJECTED


_IDEA_IDENTITY_MAX_SOURCE_CHARS = 16_384
_IDEA_IDENTITY_MAX_NORMALIZED_CHARS = 32_768
_IDEA_IDENTITY_MAX_TOKENS = 2_048
_IDEA_IDENTITY_CACHE_MAX = 1_024
_IDEA_PROMPT_ATOM_CHARS = 80
_IDEA_PROMPT_MAPPING_CHARS = 1_200
_IDEA_PROMPT_PRIOR_CHARS = 24_000
_IDEA_PROMPT_PRIOR_COUNT = 25
_REEXAMINATION_BRIEF_CHARS = 1_500
_REEXAMINATION_MAX_ROOTS = 4
_REEXAMINATION_SAMPLES = 3
_LOG = logging.getLogger(__name__)
_PROPOSAL_EVENT_SINK: ContextVar[Optional[list[tuple[str, dict, Optional[str], Optional[str]]]]] = (
    ContextVar("looplab_proposal_event_sink", default=None)
)


def _canonical_idea_identity(text: str) -> tuple[tuple[str, ...], bool]:
    """Return a bounded, punctuation-insensitive identity and whether it is complete.

    Compatibility normalization and case-folding close cheap surface-form bypasses (for example full-
    width text or ``strasse``/``Straße``), while Unicode punctuation, symbols, controls and whitespace
    are separators. The completeness bit lets the admission gate fail closed instead of treating a common
    truncated prefix as proof that two arbitrarily large proposals differ.
    """
    raw = str(text or "")
    complete = len(raw) <= _IDEA_IDENTITY_MAX_SOURCE_CHARS
    normalized = unicodedata.normalize(
        "NFKC", raw[:_IDEA_IDENTITY_MAX_SOURCE_CHARS]
    ).casefold()
    if len(normalized) > _IDEA_IDENTITY_MAX_NORMALIZED_CHARS:
        normalized = normalized[:_IDEA_IDENTITY_MAX_NORMALIZED_CHARS]
        complete = False

    tokens: list[str] = []
    token: list[str] = []
    # Boundary care at exactly _IDEA_IDENTITY_MAX_TOKENS: a text whose 2048th token is flushed by a
    # trailing SEPARATOR must NOT be marked incomplete just for the separator — the byte-identical text
    # without it flushes token #2048 in the for/else and stays complete. Completeness carries real weight
    # (an "incomplete" prior permanently self-disables the level-4/5 graded-novelty short-circuit), so only
    # flip complete=False when token-worthy content GENUINELY remains past the cap.
    for i, char in enumerate(normalized):
        category = unicodedata.category(char)
        if category[0] in {"L", "N"} or (token and category[0] == "M"):
            token.append(char)
            continue
        if token:
            tokens.append("".join(token))
            token = []
            if len(tokens) >= _IDEA_IDENTITY_MAX_TOKENS:
                # A new token can only START on an L/N char, so genuine truncation requires one ahead.
                if any(unicodedata.category(c)[0] in {"L", "N"} for c in normalized[i + 1:]):
                    complete = False
                break
    else:
        if token:
            tokens.append("".join(token))
    return tuple(tokens), complete


def _same_canonical_idea_identity(
    left: tuple[tuple[str, ...], bool],
    right: tuple[tuple[str, ...], bool],
) -> bool:
    """Return whether two complete, non-empty identities are surface variants."""
    left_tokens, left_complete = left
    right_tokens, right_complete = right
    if not left_complete or not right_complete or not left_tokens or not right_tokens:
        return False
    return left_tokens == right_tokens or "".join(left_tokens) == "".join(right_tokens)


def _bounded_prompt_text(value, max_chars: int) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... <{len(text) - max_chars} chars omitted>"


def _prompt_scalar(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return value
    return _bounded_prompt_text(value, _IDEA_PROMPT_ATOM_CHARS)


def _bounded_action_mapping(raw, *, sequence_values: bool) -> dict:
    """Return a deterministic, receipt-bearing mapping small enough for an LLM admission prompt."""
    values = raw if isinstance(raw, dict) else {}
    rows = sorted(((str(key), value) for key, value in values.items()), key=lambda item: item[0])
    kept: list = []
    for key, value in rows:
        bounded_key = _bounded_prompt_text(key, _IDEA_PROMPT_ATOM_CHARS)
        if sequence_values:
            source = list(value) if isinstance(value, (list, tuple)) else [value]
            entry = {
                "key": bounded_key,
                "values": [_prompt_scalar(item) for item in source[:16]],
                "values_omitted": max(0, len(source) - 16),
            }
        else:
            entry = [bounded_key, _prompt_scalar(value)]
        candidate = {"entries": [*kept, entry], "entries_omitted": len(rows) - len(kept) - 1}
        encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > _IDEA_PROMPT_MAPPING_CHARS:
            break
        kept.append(entry)
    return {"entries": kept, "entries_omitted": len(rows) - len(kept)}


def _canonical_action_number(value) -> tuple[str, float | str]:
    """Stable equality token for the numeric fields accepted by ``Idea``."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return "invalid", type(value).__name__
    if math.isnan(number):
        return "nonfinite", "nan"
    if math.isinf(number):
        return "nonfinite", "inf" if number > 0 else "-inf"
    return "number", 0.0 if number == 0.0 else number


def _canonical_action_identity(idea, *, operator: str,
                               eval_timeout: Optional[float]) -> tuple[tuple, bool]:
    """Canonical governed action identity and whether it has a concrete executable axis."""
    params = getattr(idea, "params", None)
    params = params if isinstance(params, dict) else {}
    space = getattr(idea, "space", None)
    space = space if isinstance(space, dict) else {}
    profile = getattr(idea, "eval_profile", None)
    identity = (
        unicodedata.normalize("NFKC", str(operator or "")).strip(),
        tuple(sorted((str(key), _canonical_action_number(value)) for key, value in params.items())),
        tuple(sorted(
            (str(key), tuple(_canonical_action_number(value) for value in values))
            for key, values in space.items()
            if isinstance(values, (list, tuple))
        )),
        (unicodedata.normalize("NFKC", str(profile)).strip() if profile is not None else None),
        (_canonical_action_number(eval_timeout) if eval_timeout is not None else None),
    )
    # Operator alone is too coarse: repo drafts commonly have no structured knobs and are distinguished
    # by their implementation claim. Once any executable axis exists, structural inequality is authoritative.
    return identity, bool(params or space or profile is not None or eval_timeout is not None)


class NoveltyGateMixin:
    """The engine's novelty/dedup gate cluster. See the module docstring for the mixin convention
    (`self` is the Engine)."""

    @contextmanager
    def _capture_proposal_events(self):
        """Buffer proposal audit events so a worker never writes the folded log.

        Legacy/main-task proposal paths keep appending immediately.  Layer 5 installs this context in
        its isolated Researcher worker, then publishes the bounded intents from the main task only if
        the prepared Card still passes its lifecycle/cue fence.
        """

        intents: list[tuple[str, dict, Optional[str], Optional[str]]] = []
        token = _PROPOSAL_EVENT_SINK.set(intents)
        try:
            yield intents
        finally:
            _PROPOSAL_EVENT_SINK.reset(token)

    def _append_proposal_event(self, event_type: str, data: dict):
        sink = _PROPOSAL_EVENT_SINK.get()
        if sink is None:
            return self.store.append(event_type, data)
        trace_id, span_id = current_ids()
        sink.append((event_type, deepcopy(data), trace_id, span_id))
        return None

    # -------------------------------------------------------- novelty gate (E1/T5)
    @staticmethod
    def _idea_text(idea) -> str:
        """The semantic identity of a proposal: what it claims to try + why."""
        return " ".join(filter(None, [getattr(idea, "rationale", "") or "",
                                      getattr(idea, "hypothesis", "") or ""])).strip()

    def _prospective_node_id(self, state: RunState, requested=None) -> int:
        """Resolve the audit identity of the node slot this proposal is trying to fill."""
        if isinstance(requested, int) and not isinstance(requested, bool) and requested >= 0:
            return requested
        ceiling = getattr(self, "_node_id_ceiling", None)
        if callable(ceiling):
            try:
                return ceiling(self.store.read_all(), state)
            except Exception:  # noqa: BLE001 - audit identity falls back; admission must stay live
                pass
        return max(state.nodes, default=-1) + 1

    @staticmethod
    def _canonicalize_idea_operator(idea, operator: str):
        """Apply a policy-owned operator before novelty admission and implementation."""
        if idea is None or getattr(idea, "operator", None) == operator:
            return idea
        governed = idea.model_copy()
        governed.operator = operator
        return governed

    @classmethod
    def _canonicalize_draft_idea(cls, idea):
        """Apply the draft policy's operator before novelty admission and implementation."""
        return cls._canonicalize_idea_operator(idea, "draft")

    def _effective_researcher_eval_timeout(self, idea) -> Optional[float]:
        """Return the finite per-node timeout override that the evaluator will actually honor."""
        return effective_researcher_eval_timeout(self, idea)

    def _proposal_binding(self, state: RunState, idea: Idea, prospective_node_id=None) -> dict:
        """Exact card-sidecar subject: reserved slot, lifecycle, and normalized durable Idea."""
        node_id = self._prospective_node_id(state, prospective_node_id)
        current = state.nodes.get(node_id)
        binding = {"node_id": node_id, "generation": current.attempt if current is not None else 0}
        proposal_ref = idea_proposal_ref(idea)
        if proposal_ref is not None:
            binding["proposal_ref"] = proposal_ref
        return binding

    @staticmethod
    def _near_binding(state: RunState, node_id) -> dict:
        node = state.nodes.get(node_id) if isinstance(node_id, int) and not isinstance(node_id, bool) else None
        if node is None:
            return {"near_node": node_id}
        return {"near_node": node.id, "near_generation": node.attempt}

    def _idea_prompt_identity(self, idea, *, prose_chars: int) -> str:
        """A bounded claim + action identity for the live LLM novelty adjudicator."""
        # rationale/hypothesis are not the experiment action. Keep every structural axis
        # independently bounded and receipt-bearing so a huge params map cannot hide space/eval_profile.
        action = {
            "operator": _bounded_prompt_text(getattr(idea, "operator", ""), 160),
            "params": _bounded_action_mapping(getattr(idea, "params", None), sequence_values=False),
            "space": _bounded_action_mapping(getattr(idea, "space", None), sequence_values=True),
            "eval_profile": (
                _bounded_prompt_text(getattr(idea, "eval_profile", ""), 160)
                if getattr(idea, "eval_profile", None) is not None else None
            ),
            "eval_timeout": self._effective_researcher_eval_timeout(idea),
        }
        return (
            f"claim={json.dumps(_bounded_prompt_text(self._idea_text(idea), prose_chars), ensure_ascii=False)}; "
            f"action={json.dumps(action, ensure_ascii=False, separators=(',', ':'))}"
        )

    def _idea_vec(self, text: str):
        # Key on the TEXT (not a node_id): the embedding is a pure function of the text, and a
        # `node_reset` re-creates the SAME id with a NEW idea — a node_id-keyed cache then returned the
        # OLD vector and the semantic-novelty gate compared future proposals against a stale idea. The
        # cache is in-memory only (never persisted/replayed), so a per-process `hash(text)` key is safe.
        key = hash(text)
        v = self._idea_vecs.get(key)
        if v is None:
            v = self._embedder(text)
            self._idea_vecs[key] = v
        return v

    def _cached_prior_idea_identity(self, text: str) -> tuple[tuple[str, ...], bool]:
        """Memoize immutable prior-node identities with a bounded, collision-free content key."""
        raw = str(text or "")
        # The canonicalizer never reads beyond its source cap. Length + cap+1 exact characters therefore
        # distinguish every complete input and every materially different truncated result without retaining
        # an unbounded model-supplied rationale in the cache key.
        key = (len(raw), raw[:_IDEA_IDENTITY_MAX_SOURCE_CHARS + 1])
        cache = getattr(self, "_idea_identity_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._idea_identity_cache = cache
        if key not in cache:
            if len(cache) >= _IDEA_IDENTITY_CACHE_MAX:
                cache.clear()
            cache[key] = _canonical_idea_identity(raw)
        return cache[key]

    def _warn_incomplete_prior_identity(self, state: RunState, node) -> None:
        """Expose a fail-closed oversized-prior cliff once per run/node lifecycle."""
        seen = getattr(self, "_idea_identity_warnings", None)
        if not isinstance(seen, set):
            seen = set()
            self._idea_identity_warnings = seen
        key = (state.run_id, node.id, getattr(node, "attempt", 0))
        if key in seen:
            return
        if len(seen) >= _IDEA_IDENTITY_CACHE_MAX:
            seen.clear()
        seen.add(key)
        _LOG.warning(
            "graded novelty bypass deferred: prior node %s has an incomplete bounded idea identity",
            node.id,
        )

    def _semantic_duplicate(self, state: RunState, idea: Idea):
        """T5: nearest existing node by idea-TEXT embedding similarity, or None. Only meaningful
        for proposals with real text (LLM ideas); short/empty rationales (toy backends) skip."""
        text = self._idea_text(idea)
        if len(text) < 20:
            return None, 0.0
        from looplab.tools.vectorstore import _cosine
        v = self._embedder(text)
        best_n, best_s = None, 0.0
        for n in state.nodes.values():
            nt = self._idea_text(n.idea)
            if len(nt) < 20:
                continue
            try:
                s = _cosine(v, self._idea_vec(nt))
            except Exception:  # noqa: BLE001 — an embedder hiccup must never block proposing
                continue
            if s > best_s:
                best_n, best_s = n, s
        if best_n is not None and best_s >= self._novelty_semantic_threshold:
            return best_n, best_s
        return None, best_s

    def _llm_novelty_gate(self, state: RunState, idea: Idea, repropose=None, researcher=None,
                          prospective_node_id=None) -> Idea:
        """novelty_mode="llm": an LLM (not an embedding/param-distance heuristic) judges whether the
        proposed idea near-duplicates an already-tried experiment — READING the real experiments via
        tools when unsure — and, if it does and a `repropose` callable is given, asks the Researcher once
        more for a meaningfully different idea (surfacing the duplicate's outcome). Loop-safe + best-
        effort: any failure just returns the original idea. Emits the same `novelty_rejected` audit
        event (kind="llm") the algorithmic gate does."""
        if not state.nodes:
            return idea
        try:
            client = self._reflect_client()
        except Exception:  # noqa: BLE001
            client = None
        if client is None:
            return idea
        from pydantic import BaseModel
        from looplab.agents.agent import agentic_struct, CompositeTools
        from looplab.tools.run_tools import RunTools

        class _NoveltyVerdict(BaseModel):
            is_duplicate: bool = False
            near_node_id: Optional[int] = None
            reason: str = ""

        prior_nodes = list(state.nodes.values())[-_IDEA_PROMPT_PRIOR_COUNT:]
        prior_rows: list[str] = []
        used = 0
        omitted = 0
        # Newest experiments carry the most current action space. Fit whole rows newest-first, then restore
        # chronological presentation; never cut a row halfway through one of its structural dimensions.
        for node in reversed(prior_nodes):
            node_operator = json.dumps(
                _bounded_prompt_text(getattr(node, "operator", ""), 160), ensure_ascii=False)
            row = (
                f"#{node.id} node_operator={node_operator}: "
                f"{self._idea_prompt_identity(node.idea, prose_chars=240)}"
            )
            if used + len(row) + (1 if prior_rows else 0) > _IDEA_PROMPT_PRIOR_CHARS:
                omitted += 1
                continue
            prior_rows.append(row)
            used += len(row) + (1 if len(prior_rows) > 1 else 0)
        prior_rows.reverse()
        brief = "\n".join(prior_rows)
        if omitted:
            brief += f"\n[{omitted} older bounded action row(s) omitted by the prompt budget]"
        msgs = [{"role": "system",
                  "content": "You judge experiment NOVELTY for an ML research loop. Decide if a PROPOSED "
                             "idea is a near-duplicate of an experiment already tried in THIS run. Read the "
                             "actual experiments (read_experiment / read_code) when unsure. A rewording or a "
                             "trivially-close variant of a tried idea is a DUPLICATE; a genuinely different "
                             "approach, component, loss, data or direction is NOVEL. Compare both the claim "
                             "and the bounded action identity: operator, params, search space, eval profile "
                             "and the governed evaluation-timeout override are part of what was tried. "
                             "Prefer NOVEL unless clearly a repeat."},
                 {"role": "user",
                  "content": f"PROPOSED idea: {self._idea_prompt_identity(idea, prose_chars=800)}"
                             f"\n\nAlready tried:\n{brief}\n\n"
                             "Emit is_duplicate, near_node_id (the tried experiment it duplicates, or null), "
                             "and a one-line reason."}]
        try:
            rt = RunTools()
            rt.bind_state(state, None)
            v = agentic_struct(client, CompositeTools([rt]), msgs, _NoveltyVerdict,
                               loop_opts={"max_turns": 12})
        except Exception:  # noqa: BLE001
            return idea
        if not (v and getattr(v, "is_duplicate", False)
                and isinstance(v.near_node_id, int) and v.near_node_id in state.nodes):
            return idea
        dup = state.nodes[v.near_node_id]
        outcome = (f"it FAILED ({dup.error_reason})" if dup.status is NodeStatus.failed
                   else f"it scored {dup.metric}")
        original = idea
        original_digest = idea_proposal_digest(original)
        if callable(repropose):
            hint = (f"\nNOVELTY GATE (LLM): your proposal near-duplicates experiment #{dup.id} — "
                    f"{outcome} ({str(v.reason)[:160]}). Propose something MEANINGFULLY DIFFERENT "
                    "(another approach, component or direction), not a rewording.")
            try:
                idea = self._repropose_with_feedback(repropose, hint, idea, researcher=researcher)
            except BudgetExceeded:
                self._append_proposal_event(EV_NOVELTY_REJECTED, {
                    **self._proposal_binding(state, original, prospective_node_id),
                    **self._near_binding(state, dup.id), "kind": "llm",
                    "reason": str(v.reason)[:200], "stance": self._novelty_stance,
                    "action": "budget_exceeded"})
                raise
        final_digest = idea_proposal_digest(idea)
        action = ("reproposed" if original_digest is not None and final_digest is not None
                  and original_digest != final_digest else "kept")
        self._append_proposal_event(EV_NOVELTY_REJECTED, {
            **self._proposal_binding(state, original, prospective_node_id),
            **self._near_binding(state, dup.id), "kind": "llm",
            "reason": str(v.reason)[:200], "stance": self._novelty_stance,
            "action": action})
        return idea

    def _repropose_with_feedback(self, repropose, hint: str, idea: Idea, researcher=None) -> Idea:
        """One informed re-propose with the duplicate surfaced as a TRANSIENT `_novelty_feedback`
        directive (shared by the LLM and semantic gates — the set/try/finally-restore discipline
        must stay identical in both). `BudgetExceeded` re-raises (the hard budget stop must end the
        run, not be swallowed); any other repropose failure keeps the original idea. The `finally`
        ALWAYS restores the previous feedback, even if repropose() raised: otherwise this transient
        "you are duplicating #N" directive leaks into EVERY later proposal in the run — including
        drafts in unrelated regions — permanently mis-steering the researcher away from a direction
        the operator never banned."""
        _r = researcher if researcher is not None else self.researcher   # Variant-1: this build's researcher
        prev = getattr(_r, "_novelty_feedback", "")
        setattr(_r, "_novelty_feedback", hint)
        try:
            idea2 = repropose()
            if idea2 is not None:
                idea = idea2
        except BudgetExceeded:
            raise
        except Exception:  # noqa: BLE001 — a repropose failure keeps the original idea
            pass
        finally:
            setattr(_r, "_novelty_feedback", prev)
        return idea

    def _intra_batch_dup(self, idea, chosen: list) -> bool:
        """Variant-1 Phase 2: is `idea` a near-duplicate of one already chosen in THIS proposal batch,
        first by canonical action, then (only when neither action has concrete structure) by idea text.
        This preserves distinct numeric/sweep/profile trials even when their prose is identical while a
        short rationale can no longer hide an identical executable action."""
        action, has_action_axis = _canonical_action_identity(
            idea,
            operator="draft",
            eval_timeout=self._effective_researcher_eval_timeout(idea),
        )
        raw = self._idea_text(idea)
        t = raw.strip().lower()
        # Semantic parity with the serial draft path (review finding #7): when a run actually uses the
        # novelty gate, two batch siblings that are embedding-near but textually DISTINCT are still
        # duplicates (the serial path would catch the second against the first, already in history).
        # Guarded on the novelty mode so toy/off runs stay text-only + byte-identical; best-effort, so an
        # embedder hiccup silently degrades to text dedup. The new idea's vector is embedded at most once.
        _semantic = getattr(self, "_novelty_mode", "off") not in (None, "off")
        _vec = None
        for other in chosen:
            other_action, other_has_action_axis = _canonical_action_identity(
                other,
                operator="draft",
                eval_timeout=self._effective_researcher_eval_timeout(other),
            )
            # policy owns the batch operator (every accepted sibling executes as draft),
            # while a governed finite timeout is a real budget axis. Compare that one canonical action
            # before prose so model-authored labels cannot admit duplicate drafts or erase long trials.
            if has_action_axis or other_has_action_axis:
                if action == other_action:
                    return True
                continue
            ot_raw = self._idea_text(other)
            ot = ot_raw.strip().lower()
            if t and t == ot:
                return True
            if len(t) < 20 or len(ot) < 20:
                continue
            # A pure-substring test would wrongly merge a short idea into a much LONGER distinct one
            # ("use a deeper net" vs "use a deeper net WITH cross-attention"). Only treat containment as
            # a near-duplicate when the two texts are also close in LENGTH (the longer isn't materially
            # extending the shorter with new content).
            if (t in ot or ot in t):
                lo, hi = sorted((len(t), len(ot)))
                if hi <= lo * 1.25:
                    return True
            if _semantic:
                try:
                    from looplab.tools.vectorstore import _cosine
                    if _vec is None:
                        _vec = self._embedder(raw)
                    if _cosine(_vec, self._idea_vec(ot_raw)) >= self._novelty_semantic_threshold:
                        return True
                except Exception:  # noqa: BLE001 — an embedder hiccup must never block proposing
                    pass
        return False

    @in_llm_lane("build")
    def _propose_batch(self, state: RunState, n: int) -> list:
        """Variant-1 Phase 2 — the ONE shared-researcher pass that yields up to N DISTINCT seed
        hypotheses for a concurrent draft batch ("one researcher -> N ideas -> N developers"), so a
        `parallel_build>1` fan-out doesn't pay N independent research rolls that collide on the same
        direction. A backend MAY expose a true one-call `propose_batch(state, n)`; otherwise we roll
        the researcher's own `propose` up to a bounded number of times, each time surfacing the
        directions already taken THIS batch as a transient avoidance directive (reusing the
        `_novelty_feedback` channel the researcher already reads), applying the normal vs-history
        novelty gate, and DROPPING an intra-batch near-duplicate. Distinct-by-construction and
        backend-agnostic; returns 1..N ideas (fewer only if the researcher can't diversify). Runs in
        the MAIN task before the build fan-out, so it uses `self.researcher` (no pool race)."""
        n = max(1, int(n))
        self._pending_batch_dropped = []
        # Keep the exact returned objects as a one-shot capability for the rare unreserved
        # ``_create_node(..., preproposed=...)`` compatibility path.  The normal parallel path reserves
        # every Idea before fan-out and never consults this list.  Identity (rather than a digest/card id)
        # matters: an unrelated caller that later supplies an equal proposal must still pass through the
        # ordinary novelty gate once.
        self._pending_batch_novelty_gated = []
        native = getattr(self.researcher, "propose_batch", None)
        ideas: list = []
        dropped: list[dict] = []
        # admission receipts belong to the slot that will actually be reserved. Advancing
        # by accepted ideas (not raw attempts) keeps retries on one slot and gives batch siblings unique ids.
        prospective_base = self._prospective_node_id(state)
        proposal_events = self.store.read_all()
        used_card_ids: set[str] = set()

        def _link_card(candidate, slot: int):
            if candidate is None:
                return None
            linked = (candidate if isinstance(candidate, Idea)
                      else Idea.model_validate(candidate)).model_copy(deep=True)
            linked.card_id = None
            # The immutable Card action records the effective governed timeout, not an uncapped
            # model request. This also makes batch dedupe and later execution share one identity.
            linked.eval_timeout = self._effective_researcher_eval_timeout(linked)
            plan = self._plan_native_card(
                proposal_events, state, linked, parents=[], parent_generations={},
                scored_against=state.best_node_id, source="researcher",
                at_node=prospective_base + slot, excluded=used_card_ids,
                steering_context=getattr(self.researcher, "_steering_context", []),
            )
            if plan.disposition == "invalid":
                self._append_proposal_event(EV_NOVELTY_REJECTED, {
                    "node_id": prospective_base + slot, "generation": 0,
                    "kind": "card_contract",
                    "reason": "proposal cannot form a bounded native Card action",
                    "action": "dropped",
                })
            return plan.idea if plan.disposition in {"mint", "reuse"} else None

        if callable(native):
            try:
                from itertools import islice

                self._set_complexity_hint(state, None)
                # native batching is only a latency/diversity optimization. It must not
                # bypass the same history/graded-novelty admission boundary as sequential proposals,
                # and a broken backend must not make us materialize an unbounded iterable.
                produced = list(islice(native(state, n) or (), n))

                def _native_repropose():
                    replacement = list(islice(native(state, 1) or (), 1))
                    return (_link_card(self._canonicalize_draft_idea(replacement[0]), len(ideas))
                            if replacement else None)

                for idea in produced:
                    if idea is None:
                        continue
                    idea = _link_card(self._canonicalize_draft_idea(idea), len(ideas))
                    if idea is None:
                        continue
                    idea = self._apply_novelty_gate(
                        state,
                        idea,
                        repropose=_native_repropose,
                        researcher=self.researcher,
                        prospective_node_id=prospective_base + len(ideas),
                    )
                    idea = _link_card(idea, len(ideas))
                    if idea is None:
                        continue
                    if self._intra_batch_dup(idea, ideas):
                        dropped.append({
                            "idea": idea.model_copy(deep=True, update={"card_id": None}),
                            "reason": "intra_batch_duplicate",
                            "steering_context": list(
                                getattr(self.researcher, "_steering_context", []) or []),
                        })
                        continue
                    ideas.append(idea)
                    used_card_ids.add(idea.card_id)
                if ideas:
                    # A native batch backend proposes all N at once, so per-idea FOREAGENT telemetry
                    # isn't separable here. The structured cue snapshot is common to the one batch call;
                    # preserve it per reservation while leaving rank telemetry to the backend.
                    _steering = list(getattr(self.researcher, "_steering_context", []) or [])
                    self._pending_batch_telemetry = [
                        {"_steering_context": list(_steering)} for _ in ideas[:n]
                    ]
                    self._pending_batch_dropped = dropped
                    accepted = ideas[:n]
                    self._pending_batch_novelty_gated = list(accepted)
                    return accepted
            except Exception:  # noqa: BLE001 — a batch-backend hiccup falls back to sequential rolls
                ideas = []
                dropped = []
        prev_feedback = getattr(self.researcher, "_novelty_feedback", "")
        # Capture EACH accepted roll's FOREAGENT telemetry (hypothesis ranking + foresight pick the
        # researcher set during propose) BEFORE the next roll overwrites it on the shared researcher, so
        # each pooled build can later emit hypothesis_ranked/foresight_selected for ITS OWN idea instead
        # of the last roll's (mega-review MEDIUM). Aligned 1:1 with the returned ideas.
        telem: list = []
        attempts, max_attempts = 0, n * 2 + 2
        try:
            while len(ideas) < n and attempts < max_attempts:
                attempts += 1
                if ideas:
                    taken = "; ".join(filter(None, (self._idea_text(x)[:200] for x in ideas)))
                    if taken:
                        setattr(self.researcher, "_novelty_feedback",
                                ("BATCH DIVERSITY: other drafts building CONCURRENTLY in this batch already "
                                 "take these directions — propose a MEANINGFULLY DIFFERENT axis / theme / "
                                 f"component, not a variation of: {taken}"))
                self._set_complexity_hint(state, None)          # A0d cues on the shared researcher
                idea = self.researcher.propose(state, None)
                if idea is None:
                    continue
                idea = _link_card(self._canonicalize_draft_idea(idea), len(ideas))
                if idea is None:
                    continue
                # Normal vs-history novelty gate (one informed re-propose on a semantic hit), exactly as
                # the serial draft path runs it — then drop an intra-batch near-duplicate.
                idea = self._apply_novelty_gate(
                    state, idea,
                    repropose=lambda: _link_card(self._canonicalize_draft_idea(
                        self.researcher.propose(state, None)), len(ideas)),
                    prospective_node_id=prospective_base + len(ideas))
                idea = _link_card(idea, len(ideas))
                if idea is None:
                    continue
                if self._intra_batch_dup(idea, ideas):
                    dropped.append({
                        "idea": idea.model_copy(deep=True, update={"card_id": None}),
                        "reason": "intra_batch_duplicate",
                        "steering_context": list(
                            getattr(self.researcher, "_steering_context", []) or []),
                    })
                    continue
                ideas.append(idea)
                used_card_ids.add(idea.card_id)
                telem.append({
                    "last_hyp_priority": self._snapshot_role_telemetry("last_hyp_priority"),
                    "last_foresight": self._snapshot_role_telemetry("last_foresight"),
                    "_steering_context": list(
                        getattr(self.researcher, "_steering_context", []) or []),
                    # THIS roll's cross-run advisory receipt (set on self by _set_complexity_hint above)
                    # so each pooled build stamps ITS OWN node_created provenance, not the last roll's.
                    "_cross_run_advisory_receipt": dict(getattr(self, "_cross_run_advisory_receipt", {}) or {}),
                })
        finally:
            setattr(self.researcher, "_novelty_feedback", prev_feedback)
            # Clear the shared researcher's per-roll telemetry: build 0 reuses self.researcher as its
            # pooled role, so a leftover last-roll ranking would otherwise be emitted against build 0's
            # node. Each build restamps its OWN captured snapshot in _create_node.
            for _a in ("last_hyp_priority", "last_foresight"):
                try:
                    setattr(self.researcher, _a, None)
                except Exception:  # noqa: BLE001
                    pass
        self._pending_batch_telemetry = telem
        self._pending_batch_dropped = dropped
        self._pending_batch_novelty_gated = list(ideas)
        return ideas

    def _snapshot_role_telemetry(self, attr: str):
        """A shallow copy of a researcher predictive-telemetry attr (a dict set during propose), or None.
        Copied so a later consume (setattr None) on the shared researcher can't blank the captured value."""
        val = getattr(self.researcher, attr, None)
        return dict(val) if isinstance(val, dict) else None

    def _failed_direction_asset_brief(self, state: RunState) -> str:
        """Load a bounded D1 brief from the actual editable repositories, or return no proof."""
        try:
            from pathlib import Path

            roots = tuple(sorted({
                str(Path(str(editable.get("path"))).resolve())
                for editable in (getattr(self, "_repo_spec", None) or {}).get("editables", [])
                if isinstance(editable, dict) and editable.get("path")
                and Path(str(editable.get("path"))).is_dir()
            }))
        except (OSError, TypeError, ValueError):
            return ""
        # A partial repository sample cannot justify overriding the ordinary novelty gate.
        if not roots or len(roots) > _REEXAMINATION_MAX_ROOTS:
            return ""
        key = (state.task_id or "", roots)
        cache = getattr(self, "_failed_direction_brief_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._failed_direction_brief_cache = cache
        if key in cache:
            return cache[key]
        try:
            from looplab.tools.asset_brief import asset_brief

            per_root = max(1, _REEXAMINATION_BRIEF_CHARS // len(roots))
            parts = [
                f"EDITABLE {index}:\n{asset_brief(root, client=None, task_type=state.task_id)[:per_root]}"
                for index, root in enumerate(roots, start=1)
            ]
            brief = "\n\n".join(parts)[:_REEXAMINATION_BRIEF_CHARS].strip()
        except Exception:  # noqa: BLE001 -- unavailable grounding must defer, never block proposing
            brief = ""
        cache[key] = brief
        return brief

    def _verified_failed_direction_reopen(self, state: RunState, graph, node_id, client) -> bool:
        """Require grounded, repeated and stable evidence before a level-5 admission override."""
        if client is None or not isinstance(node_id, int):
            return False
        brief = self._failed_direction_asset_brief(state)
        if not brief:
            return False
        try:
            from looplab.search.graded_novelty import reexamine_failed_direction

            researcher = getattr(self, "researcher", None)
            parser = next((value for owner in (
                researcher,
                getattr(researcher, "inner", None),
                getattr(researcher, "fallback", None),
                getattr(self, "developer", None),
            ) if (value := getattr(owner, "parser", None))), "tool_call")
            verdict = reexamine_failed_direction(
                state,
                node_id,
                graph,
                client=client,
                asset_brief=brief,
                samples=_REEXAMINATION_SAMPLES,
                parser=parser,
            )
        except Exception:  # noqa: BLE001 -- verifier failures preserve the ordinary flat gate
            return False
        if not isinstance(verdict, dict):
            return False
        agreement = verdict.get("agreement")
        n_samples = verdict.get("n_samples")
        requested = verdict.get("requested_samples")
        # L5 changes admission, so one lucky parse or an unstable split verdict is not
        # "repeated verification". Require a strict parsed majority and a strict modal majority; every
        # unavailable, closed, malformed or low-agreement result falls through to the ordinary flat gate.
        return bool(
            verdict.get("available") is True
            and verdict.get("recommendation") == "reexamine"
            and requested == _REEXAMINATION_SAMPLES
            and isinstance(n_samples, int) and not isinstance(n_samples, bool)
            and n_samples <= _REEXAMINATION_SAMPLES
            and n_samples * 2 > _REEXAMINATION_SAMPLES
            and isinstance(agreement, (int, float)) and not isinstance(agreement, bool)
            and 0.5 < float(agreement) <= 1.0
        )

    def _graded_novelty_precheck(self, state: RunState, idea: Idea, prospective_node_id=None):
        """PART IV D3 (§21.4, Phase 2b): a concept-graph-aware PRE-gate. When `graded_novelty` is on and
        the task has a curated concept skeleton, grade the proposal over the concept graph BEFORE the flat
        dedup gate runs. Returns the idea UNCHANGED (an allow decision that SHORT-CIRCUITS the flat gate)
        for the two grades the flat LLM/semantic gate gets WRONG:
          * level 4 `same_direction_new_impl` — shares a concept BRANCH with a tried node but is a
            materially different implementation. The flat gate can't tell "this DCL tweak" from "the whole
            DCL branch" and would wrongly reject/repropose a legitimate variant.
          * level 5 `wrongly_abandoned` — re-opens a FAILED direction only after the grounded, repeated
            verifier confirms a stable implementation-bound failure. The flat gate has no re-open path.
        Returns None (defer to the flat gate, UNCHANGED behavior) for every other grade: level 0 (novel,
        the flat gate passes it anyway) and levels 1/2/3 (identical / near-dup / prior-run, which the flat
        gate legitimately dedups). Audit-only: the allow is recorded as a `novelty_graded` event, never a
        selection change. No-op (returns None) with the flag off, an empty run, or no vocabulary — so the
        default path is byte-identical.

        AGENTIC-FIRST (§21.4 F2): when independently classified node tags are cached as `node_concepts`,
        the grade uses only entries carrying an exact classifier provenance receipt (reconstructed
        deterministically — no re-tag). Researcher-authored memberships and unknown provenance never enter
        this bypass. It degrades to the skeleton + deterministic heuristic when the trusted cache is empty."""
        if not getattr(self, "_graded_novelty", False) or not state.nodes:
            return None
        from looplab.search.concept_graph import _experiment_nodes, graph_from_node_concepts, skeleton_for
        from looplab.search.graded_novelty import grade_novelty, tag_idea_llm
        seed = skeleton_for(state.task_id or "")
        seed = seed if seed.concepts() else None
        all_node_concepts = getattr(state, "node_concepts", None) or {}
        concept_provenance = getattr(state, "node_concept_provenance", None) or {}
        receipts = getattr(state, "node_concept_materialization_receipts", None) or {}
        # an Idea's concepts are authored by the same proposer whose admission is being
        # decided, so they cannot certify their own graded-novelty bypass. Only replay-proven
        # `node_concepts` classifier events enter the agentic path; missing/unknown provenance fails
        # closed to the curated heuristic path (or no-op when no curated vocabulary exists).
        experiment_ids = {nd.id for nd in _experiment_nodes(state)}
        if any(
            nid in receipts
            and concept_provenance.get(nid) in {
                NODE_CONCEPT_PROVENANCE_CLASSIFIER, NODE_CONCEPT_PROVENANCE_OPERATOR}
            for nid in experiment_ids
        ):
            # A retained subset cannot certify complete experiment coverage or an L4/L5 bypass. This
            # also prevents an emptied partial cache from silently falling back to the heuristic path.
            return None
        classifier_ids = {
            nid for nid in experiment_ids
            if nid in all_node_concepts
            and concept_provenance.get(nid) == NODE_CONCEPT_PROVENANCE_CLASSIFIER
            and nid not in receipts
        }
        # An operator-edited node has an authoritative human/assistant-asserted tag set, so it counts as
        # COVERED for the completeness check below — but its tags never enter `node_concepts` (the graded
        # channel stays classifier-only). Without this, a single operator edit leaves that node forever
        # outside `classifier_ids`, so the coverage gate below trips on every subsequent cadence and
        # disables the agentic graded-novelty path for the WHOLE run.
        operator_ids = {
            nid for nid in experiment_ids
            if concept_provenance.get(nid) == NODE_CONCEPT_PROVENANCE_OPERATOR
            and nid not in receipts
        }
        # a partial cadence is UNKNOWN coverage, not evidence that the remaining nodes
        # touch no concepts. Once any classifier receipt exists, every experiment must be covered (a
        # classifier receipt — an explicit empty list is valid — or an operator assertion) before this
        # precheck may issue an L4/L5 admission override. Otherwise a post-cadence near-repeat or win
        # could disappear from the grade; defer to the ordinary flat gate without mixing
        # proposer-authored labels into the trusted channel.
        if classifier_ids and (classifier_ids | operator_ids) != experiment_ids:
            return None
        node_concepts = {
            nid: all_node_concepts[nid]
            for nid in classifier_ids
        }
        # Everything below is guarded: a reconstruction / tagger / grader hiccup must NEVER block proposing.
        try:
            if node_concepts:                     # AGENTIC: reuse the cached LLM node tags (no re-tag)
                graph, tags = graph_from_node_concepts(node_concepts, seed_graph=seed)
            elif seed is not None:                # FALLBACK: curated skeleton + heuristic node tags
                graph, tags = seed, None
            else:
                return None                       # no vocabulary at all -> nothing to grade
            if not graph.concepts():
                return None
            # Tag the PROPOSED idea CONSISTENTLY with the node tags: only go agentic when the node tags are
            # agentic (the cache is present) so idea+node tags share one production rule; without the cache,
            # node tags are heuristic, so tag the idea heuristically too (idea_tags=None -> tag_idea inside
            # grade_novelty). This keeps the no-cache path fully deterministic (byte-identical to pre-F2) and
            # avoids a per-proposal LLM call that would have nothing agentic to be consistent with.
            _rc = getattr(self, "_reflect_client", None)
            client = _rc() if callable(_rc) else None
            idea_tags = (tag_idea_llm(idea, graph, client)
                         if (node_concepts and client is not None) else None)
            # §21.20 Step 2: the gating grade is computed WITHOUT cross-run priors, so enabling the flag is
            # byte-identical to cross-run-off for SELECTION (grade_novelty checks its level 3 before the
            # same-run level 4, so feeding priors here would flip an L4 allow into a defer — not audit-only).
            grade = grade_novelty(state, idea, graph, tags=tags, idea_tags=idea_tags)
        except Exception:  # noqa: BLE001 — a grader/tagger/reconstruction hiccup must never block proposing
            return None
        # §21.20 Step 2: cross-run priors are AUDIT-ONLY and computed SEPARATELY from the grade above — we
        # SURFACE an earlier run's outcome (a `cross_run_prior` event) only when the idea's OWN concepts
        # overlap a prior run's, and it NEVER changes the selection decision. Best-effort throughout.
        prior_set, prior_caps, aliases, splits = self._cross_run_prior(state)
        if prior_set:
            try:
                from looplab.engine.concept_registry import canonicalize_concepts
                from looplab.search.graded_novelty import tag_idea
                raw_concepts = list(idea_tags) if idea_tags is not None else list(tag_idea(idea, graph))
                idea_concepts = set(canonicalize_concepts(raw_concepts, aliases=aliases, splits=splits))
            except Exception:  # noqa: BLE001 — a tagger hiccup just means no surfacing
                idea_concepts = set()
            matched = idea_concepts & prior_set
            if matched:
                self._record_cross_run_prior(
                    state, idea, matched, prior_caps, prospective_node_id=prospective_node_id)
        # Level 4 is an ALLOW override; level 5 is only a candidate override until the grounded repeated
        # verifier below ratifies reopening it. Level 0 (novel) and 1/2/3 (dedup) defer.
        if grade.level not in (4, 5):
            return None
        # grade_novelty's own dedup (levels 1/2) is PARAM-based, so empty/key-disjoint params can skip it
        # and make a textual repeat reach a level-4/5 ALLOW. Compare against EVERY tried node, not merely
        # `grade.near_node`: the concept-sharing node selected by the grader need not be the duplicated one.
        # a graded score is never sufficient evidence of a new implementation. A level-4/5
        # short-circuit is allowed only when its bounded canonical prose is complete, non-empty, and differs
        # from every prior proposal after NFKC, Unicode case-folding and punctuation/whitespace separation.
        # Oversize/empty identities defer to the flat gate because we cannot prove a concrete difference.
        candidate_identity = _canonical_idea_identity(self._idea_text(idea))
        identity, complete = candidate_identity
        if not complete or not identity:
            return None
        for nd in state.nodes.values():
            if getattr(nd, "idea", None) is None:
                continue
            prior_canonical = self._cached_prior_idea_identity(self._idea_text(nd.idea))
            prior_identity, prior_complete = prior_canonical
            if not prior_complete:
                self._warn_incomplete_prior_identity(state, nd)
                return None                     # ambiguous prior -> visible, fail-closed flat-gate defer
            if _same_canonical_idea_identity(candidate_identity, prior_canonical):
                return None                      # duplicate/ambiguous identity -> defer to the flat gate
        near = state.nodes.get(grade.near_node)
        if near is None or getattr(near, "idea", None) is None:
            return None
        candidate_change = (dict(idea.params or {}), dict(idea.space or {}))
        prior_change = (dict(near.idea.params or {}), dict(near.idea.space or {}))
        # Different prose is a model assertion, not evidence of a different implementation. Until an
        # implementation/patch identity exists, only an explicit param or search-space delta can justify
        # skipping the ordinary semantic/LLM duplicate gate; prose-only variants must still pass it.
        if not any(candidate_change) or candidate_change == prior_change:
            return None
        if grade.level == 5 and not self._verified_failed_direction_reopen(
                state, graph, grade.near_node, client):
            return None
        self._append_proposal_event(EV_NOVELTY_GRADED, {
            **self._proposal_binding(state, idea, prospective_node_id),
            "level": grade.level, "grade": grade.name,
            "recommendation": grade.recommendation,
            **self._near_binding(state, grade.near_node),
            "shared_concepts": list(grade.shared_concepts), "stance": self._novelty_stance,
            "rationale": str(grade.rationale)[:200]})
        return idea

    _CROSS_RUN_MIN_SIM = 0.3   # task-fingerprint Jaccard floor for a prior run to count as "similar"

    def _cross_run_prior(self, state: RunState):
        """Return canonical prior concepts/capsules plus the taxonomy snapshot used for proposal tags.
        for tasks SIMILAR to this run's fingerprint. (set(), []) when `cross_run_concepts` is off / no
        memory dir / store empty. Best-effort — any hiccup yields no priors so proposing is never blocked."""
        if not getattr(self, "_cross_run_concepts", False) or not getattr(self, "memory_dir", ""):
            return set(), [], {}, {}
        try:
            from pathlib import Path
            from looplab.engine.concept_registry import (canonicalize_concept, canonicalize_concepts,
                                                         load_concept_aliases, load_concept_splits)
            from looplab.engine.memory import (
                ConceptCapsuleStore,
                _capsule_completeness,
                _capsule_concept_evidence_completeness,
            )
            # NOTE (full-CR TODO, §21.20.13 CR2a): this reloads+scans the whole capsule JSONL per proposal;
            # a bounded, versioned, scope-keyed query index replaces it once the retrieval planner lands.
            store = ConceptCapsuleStore(Path(self.memory_dir) / "concept_capsules.jsonl")
            fp = self.lessons.task_fingerprint(state, state.best())
            # HARD direction gate: a min/rmse task and a max/recall task can share enough goal
            # tokens to clear the fuzzy Jaccard floor, but their outcomes are not comparable — require the
            # same optimization direction before a prior counts (a lean stand-in for the full contract).
            my_dir = str(getattr(state, "direction", "") or "min")
            caps = [(s, c) for s, c in store.prior_capsules(
                        fp, min_sim=self._CROSS_RUN_MIN_SIM,
                        exclude_run_id=getattr(state, "run_id", "") or "",
                        task_id=getattr(state, "task_id", "") or "")
                    if str(c.get("direction") or "min") == my_dir]
            aliases = load_concept_aliases(self.memory_dir)
            splits = load_concept_splits(self.memory_dir)
            prior: set[str] = set()
            canonical_caps = []
            for similarity, capsule in caps:
                raw = [str(x) for x in (capsule.get("concepts") or []) if str(x)]
                evidence_meta = _capsule_concept_evidence_completeness(capsule)
                concept_meta = _capsule_completeness(capsule, "concepts", len(raw))
                raw_outcomes = capsule.get("concept_outcomes") or {}
                outcome_meta = _capsule_completeness(
                    capsule, "concept_outcomes",
                    len(raw_outcomes) if isinstance(raw_outcomes, dict) else 0,
                )
                if evidence_meta is None or concept_meta is None or outcome_meta is None:
                    continue
                concepts = canonicalize_concepts(raw, aliases=aliases, splits=splits)
                outcomes = {}
                if isinstance(raw_outcomes, dict):
                    for source in sorted(raw_outcomes, key=str):
                        target = canonicalize_concept(source, sibling_concepts=raw,
                                                      aliases=aliases, splits=splits)
                        if not target or target not in concepts:
                            continue
                        candidate, current = raw_outcomes[source], outcomes.get(target)
                        # several raw aliases can collapse to one canonical concept. Mirror
                        # portfolio_concept_overview: retain the best observation in this run's direction,
                        # never the value of whichever raw spelling sorts first.
                        better = (current is None and candidate is not None) or (
                            current is not None and candidate is not None
                            and ((candidate < current) if my_dir == "min" else (candidate > current))
                        )
                        if target not in outcomes or better:
                            outcomes[target] = candidate
                # canonicalization can merge/purge retained labels, so completeness cannot be
                # recomputed from the transformed list. Freeze the already-validated raw capsule receipt
                # before replacing membership with its governed projection.
                source_receipt = {
                    "concept_evidence_nodes_total": evidence_meta[0],
                    "concept_evidence_nodes_incomplete": evidence_meta[1],
                    "concept_evidence_complete": evidence_meta[2],
                    "concepts_total": concept_meta[0],
                    "concepts_omitted": concept_meta[1],
                    "concepts_complete": concept_meta[2],
                    "concept_outcomes_total": outcome_meta[0],
                    "concept_outcomes_omitted": outcome_meta[1],
                    "concept_outcomes_complete": outcome_meta[2],
                }
                normalized = {
                    **capsule, "concepts": concepts, "concept_outcomes": outcomes,
                    "_source_receipt": source_receipt,
                    # a valid matching capsule does not make unreadable sibling rows vanish.
                    # Carry one snapshot-level health receipt into the durable v2 prior event.
                    "_store_source_health": dict(store.source_health),
                }
                canonical_caps.append((similarity, normalized))
                prior.update(concepts)
            return prior, canonical_caps, aliases, splits
        except Exception:  # noqa: BLE001 — cross-run read is advisory; a hiccup just yields no priors
            return set(), [], {}, {}

    def _record_cross_run_prior(self, state: RunState, idea: Idea, matched: set, prior_caps, *,
                                prospective_node_id=None) -> None:
        """SURFACE (never gate) the cross-run prior: which of the idea's OWN concepts were tried before, in
        which runs, with each matched concept's retained outcome and the explicitly run-level best metric —
        folds into `RunState.cross_run_priors` for the trace/UI. Best-effort; audit-only, so a failure never
        blocks proposing. Every v2 event carries the source-capsule completeness receipt; legacy event fields
        remain additive aliases so historical consumers keep folding.
        `matched` is the idea∩prior overlap already computed by the caller (never the gating grade)."""
        try:
            matched = sorted(matched)
            if not matched:
                return
            # Filter capsules to those actually contributing a matched concept FIRST, THEN cap — so a
            # matching capsule past the top-N is never silently dropped from the receipt.
            runs = []
            source_receipts = []
            store_health = None
            for sim, c in prior_caps:
                source = c.get("_source_receipt")
                if not isinstance(source, dict):
                    source = {
                        "concept_evidence_nodes_total": None,
                        "concept_evidence_nodes_incomplete": None,
                        "concept_evidence_complete": False,
                        "concepts_total": None, "concepts_omitted": None,
                        "concepts_complete": False,
                        "concept_outcomes_total": None, "concept_outcomes_omitted": None,
                        "concept_outcomes_complete": False,
                    }
                # source completeness is a denominator over ALL eligible prior capsules. A
                # partial row where the target is not retained may have omitted that target; filtering to
                # matching rows would repeat the false-completeness bug fixed in concept_card.
                source_receipts.append(source)
                if store_health is None and isinstance(c.get("_store_source_health"), dict):
                    store_health = c["_store_source_health"]
                shared = sorted(set(str(x) for x in (c.get("concepts") or [])) & set(matched))
                if not shared:
                    continue
                oc = c.get("concept_outcomes") or {}
                retained_outcomes = {k: oc[k] for k in shared if k in oc}
                run_best = c.get("best_metric")
                runs.append({
                    "run_id": c.get("run_id"),
                    # Legacy aliases remain through the additive v2 transition. New consumers must label
                    # run_best_metric as RUN-level and use matched_concept_outcomes for concept evidence.
                    "best_metric": run_best,
                    "run_best_metric": run_best,
                    "similarity": round(float(sim), 4),
                    "concepts": shared,
                    "matched_concepts": shared,
                    "outcomes": retained_outcomes,
                    "matched_concept_outcomes": [
                        {"concept": concept, "outcome_retained": concept in oc,
                         "outcome": oc.get(concept)}
                        for concept in shared
                    ],
                    "source_receipt": source,
                })
            if not runs:
                return
            runs.sort(key=lambda r: (-r["similarity"], str(r["run_id"])))   # deterministic, most-similar first
            returned_runs = runs[:8]
            partial = sum(
                not receipt.get("concept_evidence_complete")
                or not receipt.get("concepts_complete")
                or not receipt.get("concept_outcomes_complete")
                for receipt in source_receipts
            )
            unknown = sum(
                receipt.get("concept_evidence_nodes_total") is None
                or receipt.get("concept_evidence_nodes_incomplete") is None
                or receipt.get("concepts_total") is None
                or receipt.get("concept_outcomes_total") is None
                for receipt in source_receipts
            )
            store_health = store_health if isinstance(store_health, dict) else {}
            quarantined = store_health.get("source_rows_quarantined")
            quarantined = (quarantined if isinstance(quarantined, int)
                           and not isinstance(quarantined, bool) and quarantined >= 0 else 0)
            concept_source = {
                "source_complete": partial == 0 and quarantined == 0,
                "partial_capsules": partial,
                "source_unknown_capsules": unknown,
                "source_concepts_omitted": sum(
                    receipt.get("concepts_omitted") or 0 for receipt in source_receipts),
                "source_outcomes_omitted": sum(
                    receipt.get("concept_outcomes_omitted") or 0 for receipt in source_receipts),
                "source_store_complete": quarantined == 0,
                "source_rows_total": int(store_health.get("source_rows_total", 0) or 0),
                "source_rows_quarantined": quarantined,
                "source_malformed_rows": int(store_health.get("source_malformed_rows", 0) or 0),
                "source_invalid_capsule_rows": int(
                    store_health.get("source_invalid_capsule_rows", 0) or 0),
                "source_duplicate_run_rows": int(
                    store_health.get("source_duplicate_run_rows", 0) or 0),
            }
            self._append_proposal_event(EV_CROSS_RUN_PRIOR, {
                "v": 2,
                **self._proposal_binding(state, idea, prospective_node_id),
                "matched_concepts": matched, "prior_runs": returned_runs,
                "prior_runs_total": len(runs),
                "prior_runs_omitted": len(runs) - len(returned_runs),
                "prior_runs_complete": len(runs) == len(returned_runs),
                "concept_source": concept_source,
                "stance": getattr(self, "_novelty_stance", None)})
        except Exception:  # noqa: BLE001 — audit only, never block proposing
            return

    @in_llm_lane("novelty_dedup")
    def _apply_novelty_gate(self, state: RunState, idea: Idea, repropose=None, researcher=None,
                            prospective_node_id=None) -> Idea:
        """E1+T5: novelty/dedup gate over fresh proposals, BEFORE any compute is spent.
        Two layers:
        (1) SEMANTIC (T5, ShinkaEvolve `novelty rejection before evaluation`): if the idea TEXT is a
            near-duplicate of an existing node's, reject it — and when a `repropose` callable is
            given, ask the Researcher ONCE more with the duplicate (and its outcome, especially a
            FAILURE) surfaced, so the search learns "you already tried X, it scored Y because Z"
            instead of paying another eval for the same idea.
        (2) NUMERIC (E1 legacy): params within `novelty_epsilon` (normalized L2) of an existing
            node are deterministically nudged off the duplicate.
        Loop-safe (always returns a usable idea) and replay-safe (the final idea lands in
        node_created; the gate is not re-run on replay). Runs when `novelty_gate` is on OR the
        Strategist's novelty stance is "explore" (slice 5): the stance can engage a soft dedup +
        one informed re-propose even when the static gate is off, so novelty pressure follows the
        meta-controller. "balanced"/"exploit" (and gate off) leave this a no-op — exactly as before."""
        # PART IV D3 (Phase 2b): the concept-graph pre-gate runs FIRST. When it recognizes a legitimate
        # same-direction-new-implementation (level 4), or RATIFIES a re-open of a wrongly-abandoned failed
        # direction (level 5) with grounded repeated evidence, it SHORT-CIRCUITS. Every other grade (and the
        # flag being off) falls through UNCHANGED.
        graded = self._graded_novelty_precheck(
            state, idea, prospective_node_id=prospective_node_id)
        if graded is not None:
            # The graded pre-gate is a DELIBERATE short-circuit (Phase-2b, behind `_novelty_mode`): only
            # levels 4/5 return here, and only to ADMIT a proposal the flat gate would wrongly reject (a
            # legitimate same-direction-new-implementation, or a verifier-ratified re-open of a
            # wrongly-abandoned failed direction). It never ADMITS a true duplicate:
            # `_graded_novelty_precheck` runs a verbatim-dup
            # guard scanning ALL prior nodes (B1 fix), so a punctuation-only paraphrase can't reach here —
            # it falls through to the stronger `_llm_novelty_gate` below like every other grade.
            return graded
        mode = getattr(self, "_novelty_mode", "llm")
        # "llm" -> an LLM adjudicates duplication by READING the real experiments (not an embedding/
        # distance heuristic), then re-proposes if it's a dup.
        if mode == "llm":
            return self._llm_novelty_gate(
                state, idea, repropose, researcher=researcher,
                prospective_node_id=prospective_node_id)
        # The deterministic "algo" gate below runs when mode is "algo" OR the Strategist's novelty stance
        # is "explore" (the stance can engage a cheap soft dedup + one informed re-propose even when the
        # mode is otherwise off). "off" without explore leaves this a no-op — the Researcher's own
        # read-the-history judgment stands.
        if not (mode == "algo" or self._novelty_stance == "explore"):
            return idea
        import random as _random

        from looplab.events.digest import numeric_params, param_distance

        if self._novelty_semantic:
            dup, sim = self._semantic_duplicate(state, idea)
            if dup is not None:
                outcome = (f"it FAILED ({dup.error_reason}: {(dup.error or '')[:80]})"
                           if dup.status is NodeStatus.failed
                           else f"it scored {dup.metric}")
                original = idea
                original_digest = idea_proposal_digest(original)
                if callable(repropose):
                    hint = (f"\nNOVELTY GATE: your proposal is a near-duplicate of experiment "
                            f"#{dup.id} ('{self._idea_text(dup.idea)[:160]}') — {outcome}. "
                            "Propose something MEANINGFULLY DIFFERENT (another approach, "
                            "component or direction), not a rewording.")
                    try:
                        idea = self._repropose_with_feedback(
                            repropose, hint, idea, researcher=researcher)
                    except BudgetExceeded:
                        self._append_proposal_event(EV_NOVELTY_REJECTED, {
                            **self._proposal_binding(state, original, prospective_node_id),
                            **self._near_binding(state, dup.id), "kind": "semantic",
                            "similarity": round(sim, 4), "stance": self._novelty_stance,
                            "action": "budget_exceeded"})
                        raise
                final_digest = idea_proposal_digest(idea)
                action = ("reproposed" if original_digest is not None and final_digest is not None
                          and original_digest != final_digest else "kept")
                self._append_proposal_event(EV_NOVELTY_REJECTED, {
                    **self._proposal_binding(state, original, prospective_node_id),
                    **self._near_binding(state, dup.id), "kind": "semantic",
                    "similarity": round(sim, 4), "stance": self._novelty_stance,
                    "action": action})

        params = numeric_params(idea.params)
        if not params:
            return idea

        nearest, mind = None, float("inf")
        for n in state.nodes.values():
            d = param_distance(params, n.idea.params)
            if d < mind:
                mind, nearest = d, n.id
        if mind >= self._novelty_epsilon:
            return idea
        nid = self._prospective_node_id(state, prospective_node_id)
        rng = _random.Random(nid * 1009 + 7)        # deterministic per node-slot
        nudged = dict(idea.params)
        for k in params:
            scale = max(abs(params[k]), 1.0) * 0.1
            nudged[k] = round(params[k] + rng.uniform(-1.0, 1.0) * scale, 4)
        out = idea.model_copy()
        out.params = nudged
        out.rationale = (idea.rationale + " [novelty-gate: nudged off a near-duplicate]").strip()
        self._append_proposal_event(EV_NOVELTY_REJECTED, {
            **self._proposal_binding(state, out, prospective_node_id),
            **self._near_binding(state, nearest), "distance": round(mind, 4),
            "stance": self._novelty_stance, "action": "nudged",
            "original": idea.params, "nudged": nudged})
        return out
