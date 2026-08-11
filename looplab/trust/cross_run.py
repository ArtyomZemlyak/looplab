"""Bounded, always-redacted projections for untrusted cross-run memory.

Cross-run rows outlive the run that produced them and are reused in agent prompts, HTTP responses and
operator audit views.  Treat every persisted string as untrusted at those read boundaries, including
legacy rows written before durable sanitizers existed.
"""
from __future__ import annotations

import math
from itertools import islice

from looplab.core.redact import (
    is_secret_key_name,
    redact_persisted_identity,
    redact_persisted_text,
)
from looplab.core.text import tokenize

_OPAQUE_IDENTITY_KEYS = frozenset({
    "action_id", "invocation_id", "claim_uid", "scope", "scope_task", "task_id", "run_id",
    "excluded_run", "metric", "key", "evidence_digest", "governance_digest", "corpus_digest",
    "retrieval_digest", "render_digest", "input_digest", "support", "oppose", "unverified",
    "runs", "scopes", "at",
})
_LIVE_DIRECTIONS = frozenset({"min", "max"})


def valid_live_direction(value) -> bool:
    """Whether ``value`` is an exact supported objective direction for agent-facing reuse."""
    return isinstance(value, str) and value in _LIVE_DIRECTIONS


def same_live_direction(current, persisted) -> bool:
    """Whether persisted evidence can influence a live run with ``current`` direction.

    Audit projections may deliberately show legacy rows without direction provenance, but an
    agent-facing prompt/tool must know that the objective polarity is identical before reusing them.
    """
    # direction is part of the semantic identity of live guidance. Missing, coerced or
    # garbled provenance fails closed; an exact task id must never manufacture polarity compatibility.
    return (valid_live_direction(current) and isinstance(persisted, str)
            and persisted == current)


def scope_terms(text) -> frozenset[str]:
    """The salient tokens of a goal or statement, for cross-run scope comparison (doc 25 TO-07).

    ONE tokenizer, because the same query used to match different lesson sets depending on which
    tool the model happened to call: `cross_run_tools` split on unicode word characters while
    `memory_tools` used an ASCII `[a-z0-9@._]+` class that kept `run_id` and `a.b` whole. Both read
    the same `lessons.jsonl`, so "what matches" has to be one answer.
    """
    return frozenset(w for w in tokenize(text) if len(w) > 2)


class LessonScope:
    """Which persisted cross-run rows a BOUND agent may see (doc 25 TO-07).

    `CrossRunTools` invested heavily in this predicate — fail-closed on missing or malformed
    `direction`, the current run's own rows excluded as a replacement-generation fence, related-task
    visibility only through a strict goal-fingerprint overlap — and `MemoryTools.search_lessons` then
    read the SAME `lessons.jsonl` with none of it. `agents/factory.py` binds BOTH providers to a run
    whenever `memory_dir` and `cross_run_read_tools` are set, so every row one tool deliberately hid
    was retrievable one tool over, in the same agent's toolset.

    The predicate lives here rather than on either provider so the two surfaces cannot drift again,
    and so it is testable without constructing a tool provider.

    UNBOUND is portfolio-wide on purpose: a CLI or human audit reading the ledger directly has no
    live run to be scoped against, and narrowing it would hide evidence from the OPERATOR rather
    than from an agent.
    """

    __slots__ = ("bound", "run_uid", "run_id", "task_id", "direction", "goal_terms")

    def __init__(self, *, bound: bool = False, run_uid: str = "", run_id: str = "", task_id: str = "",
                 direction: str = "", goal_terms=frozenset()):
        self.bound = bound
        self.run_uid = run_uid
        self.run_id = run_id
        self.task_id = task_id
        self.direction = direction if valid_live_direction(direction) else ""
        self.goal_terms = frozenset(goal_terms)

    @classmethod
    def of(cls, state) -> "LessonScope":
        """Read one live run state into a scope. A `None` state stays unbound (portfolio-wide)."""
        if state is None:
            return cls()
        return cls(
            bound=True,
            run_uid=str(getattr(state, "run_uid", "") or ""),
            run_id=str(getattr(state, "run_id", "") or ""),
            task_id=str(getattr(state, "task_id", "") or getattr(state, "id", "") or ""),
            direction=str(getattr(state, "direction", "") or ""),
            goal_terms=scope_terms(getattr(state, "goal", "") or ""),
        )

    def is_current_run(self, row: dict) -> bool:
        """Whether a persisted cross-run row belongs to the bound live run."""
        # New rows use the globally unique incarnation id.  Fall back to the display/local run id
        # only when either side is legacy: two modern independent roots named ``run_local`` must not
        # exclude each other's evidence merely because their display labels collide.
        row_uid = str(row.get("run_uid") or "")
        if self.bound and self.run_uid and row_uid:
            return row_uid == self.run_uid
        return bool(self.bound and self.run_id and str(row.get("run_id") or "") == self.run_id)

    def exact_task(self, row: dict) -> bool:
        return bool(self.task_id and str(row.get("task_id") or "") == self.task_id)

    def related_goal(self, row: dict) -> bool:
        """Strict related-task overlap over the BARE fingerprint tokens.

        A single generic word ("model", "retrieval", "training") is not a security scope. Similar
        cross-task transfer requires at least two salient terms covering half of the smaller side;
        exact task ids remain authoritative. Rows carrying no fingerprint (including v3 D8) are
        exact-task-only. Agent-proposed facets affect neither this predicate nor retrieval order.
        """
        fp = row.get("fingerprint")
        if not (isinstance(fp, list) and self.goal_terms):
            return False
        row_terms = {t for t in fp if isinstance(t, str) and ":" not in t}
        shared = row_terms & self.goal_terms
        return (len(shared) >= 2
                and len(shared) / max(1, min(len(row_terms), len(self.goal_terms))) >= 0.5)

    def allows(self, row: dict) -> bool:
        """True for compatible direction plus exact task or strict related-goal fingerprint."""
        if not self.bound:
            return True                                        # unbound -> portfolio-wide
        # run_id is the replacement-generation fence. A stale durable row carrying the
        # live run's identity is never "prior" evidence, even when its task and direction still match.
        if self.is_current_run(row):
            return False
        # This fence is fail-closed on missing/malformed polarity, so every WRITER must persist
        # `direction` or its rows are invisible here. `lessons_distill.py` omitted it (only `store_case`
        # wrote it, which is why cases surfaced and lessons did not) — fixed at both lesson writers, and
        # `tests/test_cross_run.py::test_production_lessons_reach_a_bound_reader` pins the
        # writer→bound-reader path so the two can't drift apart again. Rows written before that fix
        # stay filtered, which is the intended fail-closed behaviour for unknown polarity.
        # A bound provider is agent-facing. Exact task identity cannot override missing or
        # malformed polarity provenance; only an explicitly unbound human/CLI audit stays portfolio-wide.
        if not same_live_direction(self.direction, row.get("direction")):
            return False
        return self.exact_task(row) or self.related_goal(row)


def cross_run_text(value, *, max_chars: int, single_line: bool = True,
                   entropy: bool = True) -> str:
    """Apply the repository's always-on durable redaction contract to one cross-run field."""
    text = redact_persisted_text(
        value, max_chars=max_chars, entropy=entropy, single_line=single_line)
    # ``redact_persisted_text``'s honest truncation receipt is multi-line even when its input was
    # collapsed. Cross-run fields are prompt/API labels, so enforce the single-line contract after the
    # marker is added as well.
    return " ".join(text.split()) if single_line else text


def cross_run_identity_text(value, *, max_chars: int) -> str:
    """Bound/redact an opaque cross-run identity without changing its Unicode equality key."""
    return redact_persisted_identity(value, max_chars=max_chars)


def sanitize_cross_run_projection(value, *, max_chars: int = 128_000,
                                  max_items: int = 256, max_depth: int = 6,
                                  max_total_items: int = 8_192,
                                  max_text_chars: int = 4_000):
    """Return a bounded JSON-compatible copy of a cross-run API/audit payload.

    The aggregate limits are intentionally generous enough for the documented endpoint pages while
    still preventing a malformed legacy/model row from expanding without bound.  Callers that page rows
    sanitize each row independently so redaction never changes the public page length/count contract.
    """
    remaining = [max(0, int(max_chars))]
    total_items = [max(0, int(max_total_items))]
    item_cap = max(0, int(max_items))
    depth_cap = max(0, int(max_depth))
    text_cap = max(0, int(max_text_chars))

    def safe_text(item, *, cap: int | None = None, entropy: bool = True) -> str:
        allowed = min(remaining[0], text_cap if cap is None else max(0, int(cap)))
        text = cross_run_text(item, max_chars=allowed, single_line=True, entropy=entropy)
        remaining[0] = max(0, remaining[0] - len(text))
        return text

    def safe_identity(item, *, cap: int | None = None) -> str:
        allowed = min(remaining[0], text_cap if cap is None else max(0, int(cap)))
        text = cross_run_identity_text(item, max_chars=allowed)
        remaining[0] = max(0, remaining[0] - len(text))
        return text

    def walk(item, depth: int, *, entropy: bool = True, identity: bool = False):
        if remaining[0] <= 0:
            return ""
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, str):
            return safe_identity(item) if identity else safe_text(item, entropy=entropy)
        if type(item) is int:
            return (item if -(1 << 63) <= item <= (1 << 63) - 1
                    else safe_identity(item, cap=128) if identity else safe_text(item, cap=128))
        if type(item) is float:
            return (item if math.isfinite(item)
                    else safe_identity(item, cap=32) if identity else safe_text(item, cap=32))
        if depth >= depth_cap:
            return "<depth-limited>"
        if isinstance(item, dict):
            out = {}
            try:
                candidates = []
                groups: dict[str, list[tuple[int, object, object, str]]] = {}
                key_cap = 160
                candidate_cap = min(item_cap, total_items[0])
                for index, (key, child) in enumerate(islice(item.items(), candidate_cap)):
                    safe_key = cross_run_text(
                        key, max_chars=key_cap, single_line=True, entropy=True)
                    if not safe_key:
                        continue
                    candidate = (index, key, child, safe_key)
                    candidates.append(candidate)
                    groups.setdefault(safe_key, []).append(candidate)

                selected: dict[int, tuple[int, object, object, str]] = {}
                for safe_key, aliases in groups.items():
                    if len(aliases) == 1:
                        selected[aliases[0][0]] = aliases[0]
                        continue
                    # NFKC is useful for schema/display keys but is not injective.  A
                    # compatibility-spelled alias must never overwrite an exact governance field such as
                    # ``decision``. Prefer the unique exact spelling; if none exists, omit the ambiguous
                    # field entirely instead of making input order authoritative.
                    exact = [candidate for candidate in aliases
                             if isinstance(candidate[1], str) and candidate[1] == safe_key]
                    if len(exact) == 1:
                        selected[exact[0][0]] = exact[0]

                for index, key, child, safe_key in candidates:
                    if index not in selected:
                        continue
                    if total_items[0] <= 0 or remaining[0] <= 0:
                        break
                    total_items[0] -= 1
                    if len(safe_key) > remaining[0]:
                        break
                    remaining[0] -= len(safe_key)
                    if is_secret_key_name(key):
                        # redact by the original structured key before stringifying the
                        # child.  A nested ``api_key`` must never rely on its value looking secret-like.
                        # (Classifying the truncated/entropy-masked ``safe_key`` here would let a secret
                        # name that the 160-char cap or entropy mask rewrote slip past and leak its value.)
                        out[safe_key] = "***"
                        remaining[0] = max(0, remaining[0] - 3)
                    else:
                        # Classify the identity EXEMPTION (which disables the entropy mask for the child)
                        # on the ORIGINAL key too — same as the secret gate above (8d1bcda). safe_key is a
                        # truncated/NFKC-rewritten DISPLAY form, so keying the opt-out on it would let an
                        # alias spelling of e.g. run_id inherit the mask exemption; fail closed instead.
                        # `str(key)` because the key need not BE a str — int node-ids are ordinary
                        # keys in Python-side projections, and `key.casefold()` on one raised
                        # AttributeError into the broad except below, collapsing the WHOLE mapping to
                        # "<mapping unavailable>". Fail-closed, but one odd key erased every sibling
                        # field in the row.
                        child_identity = str(key).casefold() in _OPAQUE_IDENTITY_KEYS
                        out[safe_key] = walk(
                            child, depth + 1, entropy=not child_identity, identity=child_identity)
                return out
            except Exception:  # noqa: BLE001 - legacy projections must fail closed, never fail the API
                return "<mapping unavailable>"
        if isinstance(item, (list, tuple)):
            out = []
            for child in islice(item, item_cap):
                if total_items[0] <= 0 or remaining[0] <= 0:
                    break
                total_items[0] -= 1
                out.append(walk(child, depth + 1, entropy=entropy, identity=identity))
            return out
        return safe_identity(item) if identity else safe_text(item)

    return walk(value, 0)
