"""Shared plumbing for the tool subsystem (ADR-7 tool protocol).

Every toolset in `looplab/tools/` is a **tool provider**: a plain object the agent loop
(`looplab.agents.agent.drive_tool_loop` / `CompositeTools`) can interrogate for OpenAI-format
function schemas and dispatch tool calls to. There is no registry and no base class — the
contract is duck-typed (see `ToolProvider` below), so a provider is trivially unit-testable
and composable: `CompositeTools([...])` merges any number of providers into one.

This module holds the two pieces every provider shares:

- `fn_spec(...)` — the one place the OpenAI function/tool schema shape lives, so every
  provider's `specs()` builds identical JSON.
- `ToolProvider` — the Protocol documenting the provider contract itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Protocol

# The agent loop's hard per-result bound: `drive_tool_loop` (agents/agent.py) caps EVERY tool result
# at this many chars before it reaches the model, replacing the tail with an explicit truncation
# marker. Providers must derive their own page/tail budgets FROM this constant (cap minus their
# header/marker overhead) instead of hard-coding free-standing ~4000s — so the loop cap and every
# provider budget move together, and a provider's own honest truncation (not the loop's blunt cut)
# is what decides which content is dropped. Canonical home: core/context_budget.py (runtime/ sits
# BELOW tools/ in the layering and needs it too); re-exported here for the providers.
from looplab.core.context_budget import RESULT_CAP  # noqa: F401  (re-export, see comment above)
from looplab.core.jsonutil import canonical_json


# -------------------------------------------------------------------------------------- typed tools
#
# The public provider protocol predates MCP's structured tool results and deliberately remains
# backwards compatible: hundreds of small providers return a plain string.  These two immutable
# value objects are the additive compatibility layer.  A typed provider may return ``ToolResult``
# and declare ``ToolCapability``; the shared loop still gives the model ``str(result)`` while traces,
# policy code and future schedulers retain the machine-readable receipt.  Missing metadata is never
# guessed from a function name: UNKNOWN is a real, fail-closed value.

TOOL_EFFECTS = frozenset({"read", "write", "execute", "external", "control", "unknown"})
TOOL_RISKS = frozenset({"low", "medium", "high", "unknown"})
TOOL_IDEMPOTENCY = frozenset({"idempotent", "non_idempotent", "conditional", "unknown"})
TOOL_APPROVAL = frozenset({"never", "policy", "always", "task_pinned", "unknown"})


def _freeze(value):
    """Recursively copy JSON-like data into immutable containers.

    Tool results cross phase, trace and permission boundaries.  ``frozen=True`` on the dataclass is
    not enough when a caller can still mutate a nested dict after dispatch; freeze at construction
    so a receipt cannot change underneath the trace that is recording it.
    """
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, set):
        return tuple(sorted((_freeze(v) for v in value), key=repr))
    return value


def _thaw(value):
    """JSON-friendly inverse of :func:`_freeze`, used only at an egress boundary."""
    if isinstance(value, Mapping):
        return {str(k): _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


@dataclass(frozen=True)
class ToolCapability:
    """Machine-readable contract for one callable tool.

    ``effect`` and ``risk`` describe what the implementation can do, while ``approval`` describes
    how authority is obtained.  ``task_pinned`` means the operator recorded the exact operation in
    the immutable task snapshot; it does *not* mean arbitrary arguments are pre-approved.

    Defaults are deliberately conservative.  In particular, an undeclared legacy provider is not
    inferred to be read-only because its name starts with ``read_``.
    """

    name: str
    effect: str = "unknown"
    risk: str = "unknown"
    idempotency: str = "unknown"
    concurrency_safe: bool = False
    cancellable: bool = False
    approval: str = "unknown"
    input_schema: Optional[Mapping[str, Any]] = None
    output_schema: Optional[Mapping[str, Any]] = None
    source: str = "internal"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("tool capability needs a non-empty name")
        for value, allowed, label in (
                (self.effect, TOOL_EFFECTS, "effect"),
                (self.risk, TOOL_RISKS, "risk"),
                (self.idempotency, TOOL_IDEMPOTENCY, "idempotency"),
                (self.approval, TOOL_APPROVAL, "approval")):
            if value not in allowed:
                raise ValueError(f"unknown tool {label} {value!r}; expected one of {sorted(allowed)}")
        object.__setattr__(self, "input_schema", _freeze(self.input_schema)
                           if self.input_schema is not None else None)
        object.__setattr__(self, "output_schema", _freeze(self.output_schema)
                           if self.output_schema is not None else None)
        object.__setattr__(self, "source", str(self.source or "internal"))

    @classmethod
    def unknown(cls, name: str, *, input_schema=None, source: str = "legacy") -> "ToolCapability":
        return cls(name=name, input_schema=input_schema, source=source)

    def as_dict(self) -> dict:
        out = {
            "name": self.name,
            "effect": self.effect,
            "risk": self.risk,
            "idempotency": self.idempotency,
            "concurrency_safe": bool(self.concurrency_safe),
            "cancellable": bool(self.cancellable),
            "approval": self.approval,
            "source": self.source,
        }
        if self.input_schema is not None:
            out["input_schema"] = _thaw(self.input_schema)
        if self.output_schema is not None:
            out["output_schema"] = _thaw(self.output_schema)
        return out


@dataclass(frozen=True)
class ToolResult:
    """Typed result with a byte-compatible legacy string view.

    ``content`` is the bounded human/model-facing representation.  ``structured`` is data for
    trusted consumers and traces; ``receipt`` proves what was attempted; ``provenance`` says who
    declared it.  A tool-level failure is *data* (``is_error=True``), not an exception that tears
    down the agent loop.  ``retryable=None`` means the provider made no claim.
    """

    content: str
    structured: Any = None
    is_error: bool = False
    provenance: Mapping[str, Any] = field(default_factory=dict)
    receipt: Mapping[str, Any] = field(default_factory=dict)
    retryable: Optional[bool] = None
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", "" if self.content is None else str(self.content))
        object.__setattr__(self, "structured", _freeze(self.structured))
        object.__setattr__(self, "provenance", _freeze(self.provenance or {}))
        object.__setattr__(self, "receipt", _freeze(self.receipt or {}))
        object.__setattr__(self, "meta", _freeze(self.meta or {}))
        if self.retryable is not None:
            object.__setattr__(self, "retryable", bool(self.retryable))

    def __str__(self) -> str:
        return self.content

    @classmethod
    def coerce(cls, value) -> "ToolResult":
        return value if isinstance(value, cls) else cls(content=str(value))

    def trace_attributes(self) -> dict:
        out = {"is_error": bool(self.is_error)}
        if self.retryable is not None:
            out["retryable"] = bool(self.retryable)
        if self.structured is not None:
            out["structured"] = _thaw(self.structured)
        if self.provenance:
            out["provenance"] = _thaw(self.provenance)
        if self.receipt:
            out["receipt"] = _thaw(self.receipt)
        if self.meta:
            out["meta"] = _thaw(self.meta)
        return out


def capabilities_for_specs(specs, **contract) -> list[ToolCapability]:
    """Declare the same contract for every valid function spec in one provider."""
    out = []
    for spec in specs or ():
        fn = (spec or {}).get("function") or {}
        name = fn.get("name")
        if name:
            out.append(ToolCapability(name=name, input_schema=fn.get("parameters"), **contract))
    return out


def capability_manifest(specs, capabilities=()) -> tuple[dict, str]:
    """Canonical manifest + SHA-256 for the exact schemas and declared effects a model receives."""
    caps = {c.name: c for c in (capabilities or ()) if isinstance(c, ToolCapability)}
    rows = []
    for spec in specs or ():
        fn = (spec or {}).get("function") or {}
        name = str(fn.get("name") or "")
        if not name:
            continue
        cap = caps.get(name) or ToolCapability.unknown(
            name, input_schema=fn.get("parameters"), source="legacy")
        rows.append({"spec": spec, "capability": cap.as_dict()})
    rows.sort(key=lambda row: row["capability"]["name"])
    raw_manifest = {"schema": 1, "tools": rows}
    # THE ONE canonical-JSON contract (doc 25 SE-08), not a private re-spelling of its four strict
    # options: these bytes are a digest PREIMAGE — `agents/tool_loop.py` stamps their sha256 on every
    # tool observation as `capability_manifest_sha256` — and a second spelling of "canonical" is two
    # digests for one manifest with nothing to say why. It was byte-identical when this call replaced
    # it (2026-08-19); what moves with it now is the ANSWER FOR A SPEC THAT HAS NO CANONICAL FORM,
    # which is the reason to share rather than to copy: a bare `json.dumps` raised a `TypeError` out
    # of the router constructor, and `canonical_json` raises the contract's own `ValueError` naming
    # the value. Both are the same unhandled construction failure — no caller catches either — and
    # only one of them says what is wrong.
    encoded = canonical_json(raw_manifest)
    # Detach the retained manifest from provider-owned spec dicts. Without the round-trip a provider
    # could mutate a nested schema after construction, leaving ``manifest()`` describing bytes that
    # no longer match the digest stamped on tool observations.
    manifest = json.loads(encoded.decode("utf-8"))
    return manifest, hashlib.sha256(encoded).hexdigest()


class CancelSignal:
    """``threading.Event``-shaped adapter over the tool loop's callback cancellation seam."""

    def __init__(self, check: Optional[Callable[[], bool]] = None):
        self._check = check

    def is_set(self) -> bool:
        if self._check is None:
            return False
        try:
            return bool(self._check())
        except Exception:  # noqa: BLE001 - cancellation is a safety hint, never a new failure path
            return False


def fit_rows(header, rows, *, receipt: str = "", cap: int = RESULT_CAP,
             omitted: str = "... ({receipt}{n} more omitted to fit the result cap)") -> str:
    """Assemble `header` + `rows` (+ a trailing `receipt`) so the whole result fits under `cap`.

    Drop whole ROWS from the end, and say in the receipt how many the cap itself removed. The agent
    loop cuts an over-cap tool result from the HEAD, which silently eats whatever is at the END — and
    for a listing the end is exactly the receipt that says the result is partial ("… (+K more)",
    "capped at N hits"). A long listing therefore arrived looking complete.

    `reposcout._fit_rows` and `memory_tools._bounded_result` were this function written twice with
    different marker wording and different header types (doc 25 TO-08). `header` accepts either a
    string (used verbatim — the caller owns its trailing newline) or a sequence of lines (joined, and
    the omission marker is appended as one more line). `omitted` keeps the per-site wording as a
    parameter: `{n}` is the dropped-row count and `{receipt}` the caller's own receipt with a
    separator, empty when there is none.
    """
    lines = header if isinstance(header, str) else "\n".join(header)
    joiner = "" if isinstance(header, str) else "\n"
    tail = f"\n{receipt}" if receipt else ""
    body = "\n".join(rows)
    if len(lines) + len(joiner if rows else "") + len(body) + len(tail) <= cap:
        return lines + (joiner if rows else "") + body + tail
    # Reserve room for the AMENDED marker before deciding how many rows survive: a marker added after
    # the fit decision is exactly what pushes the receipt back past the cap.
    dropped, kept = 0, list(rows)
    while kept:
        marker = "\n" + omitted.format(n=dropped, receipt=f"{receipt}; " if receipt else "")
        body = "\n".join(kept)
        if len(lines) + len(joiner) + len(body) + len(marker) <= cap:
            return lines + joiner + body + marker
        kept.pop()
        dropped += 1
    return lines + (f"\n({receipt})" if receipt else "\n(nothing fits the result cap)")


def clip(text: str, cap: int, *, keep: str = "head", note: str = "", reserve: int = 0,
         line_boundary: bool = False) -> str:
    """Bound one STRING under `cap`, saying so when it actually cuts (doc 25 TO-08).

    Five providers wrote this separately, each with its own marker, so a model had to learn five
    receipts for one event. The differences that survive are parameters because they are real:

    * `keep` — `"tail"` for a log or command output (the end is where the error and the final metric
      line are; the marker then goes in FRONT), `"head"` for a reply or a listing.
    * `line_boundary` — cut back to the last newline so no half-line/half-hit shows.
    * `reserve` — whether the marker is charged AGAINST `cap`. Most callers pass a cap that already
      carries headroom and let the marker sit on top; a caller handed the loop's raw `RESULT_CAP` has
      no headroom, and a result landing EXACTLY on the cap is one the loop's own marker also skips —
      a cut answer byte-indistinguishable from a complete one.
    * `note` — the marker itself, formatted with `{n}` = characters dropped. Empty means no marker,
      which is only honest when the caller adds its own.
    """
    if len(text) <= cap:
        return text
    budget = max(0, cap - reserve)
    if keep == "tail":
        cut = text[len(text) - budget:]
        if line_boundary and "\n" in cut:
            cut = cut[cut.index("\n") + 1:]
        return note.format(n=len(text) - len(cut)) + cut
    cut = text[:budget]
    if line_boundary and "\n" in cut:
        cut = cut[:cut.rfind("\n")]
    return cut + note.format(n=len(text) - len(cut))


# Per-STREAM tail budgets for a two-stream (stdout/stderr) command result. The agent loop caps the
# COMBINED result at RESULT_CAP (head-keep), so giving each stream ~RESULT_CAP alone let a verbose
# stdout push the whole stderr section — the traceback, i.e. the REASON the command failed — past the
# cap, where the loop silently dropped it. The MINIMUM below holds even when both streams are long;
# when one stream is short, its unused budget flows to the other (a stderr-only failure gets ~the
# whole cap for its traceback, not half — a fixed 50/50 split truncated exactly the frames the repair
# needed). Headroom (-400) covers the exit-code head + section labels + notes.
STDOUT_TAIL = RESULT_CAP // 2 - 200
# stderr's own guaranteed minimum is DERIVED in `stream_tails` as `avail - STDOUT_TAIL`,
# deliberately not a second constant that could drift away from it.


def stream_tails(out: str, err: str) -> tuple[int, int]:
    """Per-call tail budgets: each stream is guaranteed its minimum share, and whatever one stream
    leaves unused flows to the other (stderr first — the exception lives there). Sum always fits
    under RESULT_CAP with the -400 label/head headroom.

    Lives HERE, beside `clip`/`fit_rows`, rather than in `shell_tools`: the assistant's `run_command`
    and the Developer's `run_probe` are two surfaces reporting the SAME two-stream shape, and a
    second copy is how the two would come to disagree about which half of a failure survives
    (doc 25 TO-08 — five providers had written `clip` separately before it moved here)."""
    avail = RESULT_CAP - 400
    err_take = min(len(err), avail - min(len(out), STDOUT_TAIL))
    out_take = min(len(out), avail - err_take)
    return out_take, err_take


def fn_spec(name: str, description: str, props: dict, required: Optional[list] = None) -> dict:
    """Build one OpenAI-format function/tool schema. Shared by every tool provider so the
    schema shape lives in one place."""
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required or []}}}


# ------------------------------------------------------------------------------ tool inventory
#
# WHAT IT IS. The optional `inventory()` hook (see `ToolProvider`) lets a provider publish how much
# each of its tools can return right now, so the prompt states it and the model does not spend a
# call discovering that a store is empty. Measured 2026-08-19 over six cold-start runs, 138 of 227
# tool calls returned nothing at all, and the empty tail was dominated by tools whose emptiness was
# knowable before the call: `read_asset` 20/20, `cross_run_search` 12/12, `data_schema` 9/9.
#
# WHY A COUNT AND A REASON ARE DIFFERENT VALUES. "I looked and there are zero" and "I could not
# look" are not the same claim, and collapsing them is the failure this hook must not commit. A
# published `cross_run_search=0` tells the model the call is pointless; if the real situation was
# that the store could not be READ, that suppresses a call that had something to return. The
# concept readers in `tools/run_tools.py` already draw this line by hand -- `_concept_tree` answers
# "recorded fallback [] is NOT a known-empty taxonomy" when its projection is `unavailable` -- and
# this hook keeps it: an `int` is a count the provider stands behind, a `str` is the REASON it has
# no count, rendered as `UNKNOWN(<reason>)` and read by the model as "still worth a call".
#
# FAIL-CLOSED. A provider that raises, returns a wrong-shaped value, or simply omits the hook
# contributes NOTHING to the block rather than a zero. Absence of a row is silence, and silence
# costs at most the call that would have happened anyway.
def jsonl_row_count(path) -> int:
    """Non-blank rows in a JSONL store, splitting ONLY on a newline.

    `for line in open(path)` splits on a bare `\r` too, which `core/jsonlio.py` and
    `core/memory_window.py` both document as deliberately NOT a record boundary -- so a
    text-mode count disagrees with the reader the tool actually uses. Measured: a store whose
    middle record embeds a bare `\r` counted 4 rows here and 3 source lines there, and that
    count is what the prompt publishes and what `hide_empty_tools` withholds a tool on.

    Read as BYTES rather than through `read_jsonl_lenient_with_health`, whose `source_lines`
    is the same number: this runs on the synchronous prompt-assembly path and must not pay a
    full parse of every row to publish one integer.

    Raises `OSError` on an unreadable store -- the caller turns that into an UNKNOWN reason,
    which must never be collapsed into a zero.
    """
    with open(path, 'rb') as handle:
        # OPEN[jsonl-row-count-reads-the-whole-store] the whole file, plus a list of all its rows,
        # in memory — on the synchronous prompt-assembly path, per inventory sweep, over stores
        # that grow monotonically across runs.
        # proof:present:handle.read().split@looplab/tools/_base.py
        # REVIEW 2026-08-30 (robustness): the sibling reader this replaces (`core/memory_window`)
        # caps at 2 MiB for exactly this path, and `SiblingRunTools.inventory`'s own comment
        # refuses a ~2.5 s fold here — while this read is unbounded and runs 2-3x per prompt
        # (`collect_inventory` + the `hide_empty_tools` offer). Count lines in chunks (constant
        # memory), and above a byte ceiling answer the contract's UNKNOWN — "could not look" is a
        # value this vocabulary already has.
        return sum(1 for row in handle.read().split(b'\n') if row.strip())


INVENTORY_CONTRACT = "int = a count the provider stands behind; str = the reason it has none"


def coerce_inventory(raw: object) -> dict[str, int | str]:
    """Normalize one provider's `inventory()` return, dropping anything ill-formed.

    Total on purpose: this runs while building a prompt, and a provider with a broken hook must
    cost the run a missing row, never a raised exception. A bool is rejected because `True` would
    render as the count `1`; a negative count is rejected because no tool can hold fewer than none.
    """
    if not isinstance(raw, Mapping):
        return {}
    rows: dict[str, int | str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name:
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            if value >= 0:
                rows[name] = value
        elif isinstance(value, str) and value.strip():
            rows[name] = value.strip()
    return rows


def collect_inventory(provider: object) -> dict[str, int | str]:
    """Ask one provider (or a `CompositeTools`) for its inventory; `{}` if it has none to give."""
    hook = getattr(provider, "inventory", None)
    if not callable(hook):
        return {}
    try:
        return coerce_inventory(hook())
    except Exception:  # noqa: BLE001 - a prompt must never fail on an optional receipt
        return {}


def render_inventory(rows: Mapping[str, int | str]) -> str:
    """Render inventory rows as the `name=value` pairs the briefs publish.

    Bare pairs on purpose: the tool DESCRIPTIONS are already in the request, so the name is the
    whole link back to the tool surface and any prose around it is duplicated context.
    """
    return " ".join(
        f"{name}={value}" if isinstance(value, int) else f"{name}=UNKNOWN({value})"
        for name, value in sorted(rows.items()))


class ToolProvider(Protocol):
    """The duck-typed tool-provider contract (structural — no provider inherits this).

    A provider exposes:

    - `specs() -> list[dict]` — the OpenAI function/tool schemas it offers (built with
      `fn_spec`). May be empty (e.g. a provider whose backing directory is unconfigured);
      an empty provider simply contributes no tools.
    - `execute(name, args) -> str` — legacy-compatible dispatch returning a STRING.
      Soft-fail rule: `execute` returns an error message string, it never raises — a junk
      tool call from the model must not crash the run. Long output is additionally
      truncated by the agent layer (~4000 chars), so providers should tail/clip smartly.
    - `bind_state(state, parent=None)` (optional) — run-aware providers (e.g. `RunTools`)
      implement this so the agent loop can point them at the current `RunState` (and the
      node's parent, when the loop knows one) each turn. The loop CALLS it with BOTH
      arguments — `bind_state(state, parent)` (`agents/agent.py`) — so a provider must
      accept the second one (default it to None), or it raises TypeError at dispatch.
      Providers that don't need run state simply omit the hook (`CompositeTools` forwards
      it only where present), hence the no-op default here.

    Additive typed extensions (optional; CompositeTools supplies conservative adapters):

    - `capabilities() -> list[ToolCapability]` declares effect/risk/idempotency/cancellation/
      approval plus input/output schemas. An omitted declaration is explicitly ``unknown``; a name
      such as ``read_file`` is never treated as proof of safety.
    - `execute_result(name, args, cancel_check=None) -> ToolResult` retains structured content,
      tool-level errors, provenance and execution receipts. The shared loop still sends
      ``result.content`` to legacy model transports and propagates its live cancellation callback.
    - `inventory() -> dict[str, int | str]` (optional) — HOW MUCH each of this provider's tools
      has to return RIGHT NOW, so the prompt can publish it and the model need not spend a call
      to find out. An `int` is a count the provider stands behind; a `str` is an UNKNOWN reason.
      See `INVENTORY_CONTRACT` below for why the two are not interchangeable.

    Inventory is a claim about SIZE, never about CONTENT: `cross_run_search=41` says the store
    holds 41 rows, not that any of them answers the query. Only a zero is decisive, and it is
    decisive in exactly one direction — a tool with nothing to read cannot return anything.
    """

    def specs(self) -> list[dict]: ...

    def execute(self, name: str, args: dict) -> str: ...

    def bind_state(self, state, parent=None) -> None:  # optional hook — default is a no-op
        return None
