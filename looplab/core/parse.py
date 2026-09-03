"""Structured-output parsing (I2, ADR-14). Native tool-calling is the DEFAULT;
on parse/validation failure it auto-falls back to a text+JSON-extraction path
(the BAML "Schema-Aligned Parsing" role). Callers are parser-agnostic.

`LLMClient` is the seam: any object with `complete_tool` + `complete_text` works,
so the shipped OpenAI-compatible client, the optional LiteLLM adapter and test fakes are
interchangeable.
"""
from __future__ import annotations

import ast
import json
import math
import re
import types
import typing
from typing import Protocol, Type, TypeVar, get_args, get_origin

from pydantic import BaseModel, ValidationError

from looplab.core.errors import LLMError
# Function-level in `parse_structured` would be per-call import overhead on a hot path;
# module-level is safe because `core.tracing` imports only `core` siblings and never `parse`.
from looplab.core.tracing import structured_parse as _structured_parse


# core carries several "is this a usable number" rules, and they are NOT interchangeable (doc 25
# CO-09). The map, so a reader picks by contract instead of by whichever import was nearest:
#
# * `to_float` / `to_int` (here) — COERCING. `float("3.5")` succeeds. For text from outside the
#   process (nvidia-smi CSV cells, env vars, CLI arguments) where a string IS the wire format.
# * `fitness.is_usable_metric` / `fitness.finite_metric` — STRICT on type, coercing to float only to
#   test finiteness. A JSON string is NOT a metric: it must not enter ordering. `core.profile` aliases
#   the predicate for column typing.
# * `comparison.finite_measurement` — strict on the EXACT type (`type(v) not in {int, float}`), so an
#   int/float SUBCLASS is refused too. Comparison contracts are durable published claims, and a
#   subclass can override `__eq__`/`__lt__`.
# * `llm._safe_token_count` — strict `type(v) is int` plus an int64 ceiling. Feeds the durable cost
#   ledger, where an integral float would be a provider bug rather than a value to round.
# * `tracing._token_int` — deliberately the LAX twin of that one: it coerces and clamps at 0 and never
#   raises, because tracing must not be able to perturb the operation it observes.
# * `cards._resource_int` / `models.safe_lesson_node_count` — bounded readers for untrusted persisted
#   payloads; the latter also accepts decimal STRINGS, for old logs that wrote them.


def to_float(v, *, finite: bool = False):
    """`float(v)` or None when unparseable. `finite=True` additionally rejects NaN/inf — the
    metric-reading rule (a diverged run must read as "no metric", never enter best-selection).
    The one spelling of COERCING scalar parsing (see the contract map above — the strict readers are
    deliberately separate, because accepting `"3.5"` where a durable number is required is a bug)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (finite and not math.isfinite(f)) else f


def to_int(v):
    """`int(float(v))` or None when unparseable (nvidia-smi CSV cells and similar)."""
    try:
        return int(float(v))
    except (TypeError, ValueError, OverflowError):
        # OverflowError: int(float('inf'/'1e400')) — a non-finite value is "unparseable" per the
        # contract above, not a crash (float() itself accepts 'inf'/'Infinity', int() then rejects it).
        return None

T = TypeVar("T", bound=BaseModel)

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_INNER = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
_FENCE_PY = re.compile(r"```(?:python|py)\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_FENCE_ANY = re.compile(r"```\s*(.*?)```", re.DOTALL)
# An UNCLOSED opening fence (the reply was truncated at max tokens, finish_reason="length"): salvage
# from the opening fence to the end so a truncated Developer reply doesn't return the literal
# "```python" header as "code" (a guaranteed SyntaxError node + a wasted sandbox eval + repair cycle).
_FENCE_OPEN = re.compile(r"```(?:python|py)?[^\S\n]*\n(.*)\Z", re.DOTALL | re.IGNORECASE)


def strip_think(text: str) -> str:
    return _THINK.sub("", text)


def split_think(text: str) -> tuple[str, str]:
    """Split a reasoning-model reply into (thinking, answer): the concatenated
    <think>…</think> chain-of-thought and the clean post-reasoning answer. Either may be
    empty. Lets callers surface the model's *conclusion* (the answer) as the primary output
    while keeping the raw reasoning as a debug-only channel — never discarding it silently."""
    if not text:
        return "", ""
    thinking = "\n\n".join(m.strip() for m in _THINK_INNER.findall(text) if m.strip())
    return thinking, strip_think(text).strip()


def extract_code(text: str) -> str:
    """Pull a runnable script out of an LLM reply: drop <think>, prefer a python-tagged
    fenced block (so a leading output/example fence doesn't win), else the first bare
    fence, else the stripped remainder."""
    text = strip_think(text)
    m = _FENCE_PY.search(text) or _FENCE_ANY.search(text)
    if m:
        return m.group(1).strip()
    # No CLOSED fence — salvage an UNCLOSED one (truncated reply) rather than returning the whole
    # reply incl. the "```python" header line (which fails to compile). Only fires when a closed fence
    # didn't match, so it never overrides a real block.
    mo = _FENCE_OPEN.search(text)
    return (mo.group(1) if mo else text).strip()


class LLMClient(Protocol):
    def complete_tool(self, messages: list[dict], json_schema: dict) -> dict: ...
    def complete_text(self, messages: list[dict]) -> str: ...


class ParseError(Exception):
    pass


# How many top-level JSON objects one reply is scanned for. A reply that opens more than this many
# decodable objects is prose about JSON, not an answer; the bound is on the WORK, and the historical
# first-object behaviour is what it degrades to.
_JSON_CANDIDATE_CAP = 16


def _schema_key_sets(schema) -> tuple[frozenset[str], frozenset[str]]:
    """(required, declared) top-level property names of a JSON schema, or two empty sets.

    Total on purpose: this is handed whatever `model_json_schema()` produced, and a schema shape it
    cannot read must degrade to "no opinion" — which is exactly the historical first-object rule —
    rather than raise inside a parser whose whole job is tolerating malformed input.
    """
    if not isinstance(schema, dict):
        return frozenset(), frozenset()
    props = schema.get("properties")
    declared = frozenset(k for k in props if isinstance(k, str)) if isinstance(props, dict) else frozenset()
    req = schema.get("required")
    required = frozenset(k for k in req if isinstance(k, str)) if isinstance(req, list) else frozenset()
    return required & declared if declared else required, declared


def _schema_fit(obj: dict, required: frozenset[str], declared: frozenset[str]) -> tuple[int, int]:
    """How well a decoded object answers the schema: (required keys present, declared keys present).

    Compared as a TUPLE, so a candidate carrying every required field beats one that merely mentions
    more optional names. Both halves are counted because a schema with no `required` block — several
    of this repo's models are entirely optional-with-defaults — would otherwise score everything 0.
    """
    keys = frozenset(k for k in obj if isinstance(k, str))
    return len(keys & required), len(keys & declared)


def _extract_json(text: str, schema=None) -> dict:
    """Pull the model's ANSWER out of a text reply.

    THE OBJECT THE MODEL MEANT, not the first one it typed. This returned the first complete
    top-level JSON object, and the text path's own hint message ends by pasting the whole JSON
    schema — so a model that echoes or restates that schema before answering had its ECHO parsed as
    the answer. `{"type": "object", "properties": {...}}` decodes cleanly, is a `dict`, and carries
    none of the fields the caller asked for; it then either fails validation (a wasted provider call
    and a fall-through to the next parser) or, for a model whose fields are all optional with
    defaults, VALIDATES — returning an object of entirely default values as though the model had
    chosen them. A worked example in the reply, or a restated few-shot, does the same.

    The rule is conservative by construction: candidates are scored against the schema and the FIRST
    one wins every tie, so this changes an answer only when a LATER object matches the schema
    STRICTLY better. With no schema (`schema=None`, which is every direct caller and every test that
    predates this) it is byte-identical to the first-object walk it replaces.
    """
    # Reasoning models (e.g. Qwen3) wrap chain-of-thought in <think>…</think> that
    # can itself contain braces — strip it before locating the JSON object.
    text = _THINK.sub("", text)
    decoder = json.JSONDecoder()
    # Decode top-level JSON objects, ignoring any trailing prose (which may itself contain braces —
    # so a naive find('{')..rfind('}') span is unsafe). Resume the scan AFTER a decoded object rather
    # than one character in: a nested `{` inside an object already decoded is not a second candidate,
    # and re-decoding from inside it is quadratic on a large reply.
    required, declared = _schema_key_sets(schema)
    perfect = (len(required), len(declared))
    best: dict | None = None
    best_fit = (-1, -1)
    seen = 0
    i = text.find("{")
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            if not declared and not required:
                return obj                      # no schema to judge by: the historical behaviour
            fit = _schema_fit(obj, required, declared)
            if fit > best_fit:                  # strictly better only — the first candidate wins ties
                best, best_fit = obj, fit
            if best_fit >= perfect:
                return best                     # every declared field present; nothing can beat it
            seen += 1
            if seen >= _JSON_CANDIDATE_CAP:
                break
        # Resume AFTER the object just decoded: a nested `{` inside it is not a second candidate.
        i = text.find("{", max(end, i + 1))
    if best is not None:
        return best
    # H2 schema-aligned lenient fallback: small models emit near-JSON (single quotes, trailing
    # commas, Python True/None). Try a Python-literal eval of the outermost {...} span before failing.
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e > s:
        try:
            obj = ast.literal_eval(text[s:e + 1])
            if isinstance(obj, dict):
                return obj
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            pass
    raise ParseError("no JSON object found in text")


def _coerce_value(val, ann):
    """Best-effort coerce a raw value to the field annotation (H2 schema-aligned repair): unwrap
    Optional, cast string/number/bool drift, and recurse into dict/list element types. Never raises —
    returns the original value if it can't coerce, so validation makes the final decision."""
    origin = get_origin(ann)
    # `typing.Union` covers Optional[X]/Union[...]; `types.UnionType` covers the PEP 604 `X | None`
    # spelling the codebase uses pervasively (get_origin(int | None) is types.UnionType on 3.11, NOT
    # typing.Union) — without it the schema-aligned coercion was silently skipped for `| None` fields.
    if origin is typing.Union or origin is types.UnionType:   # Optional[X] / Union[...] / X | None
        if val is None:
            return None
        non_none = [a for a in get_args(ann) if a is not type(None)]
        if non_none:
            ann, origin = non_none[0], get_origin(non_none[0])
    try:
        if ann is bool:
            if isinstance(val, str):
                s = val.strip().lower()
                if s in ("true", "yes", "1", "y", "on"):
                    return True
                if s in ("false", "no", "0", "n", "off"):
                    return False
                return val          # unrecognized string: don't silently coerce to False — return it
            return bool(val)        # so model validation rejects it rather than flipping a flag off
        if ann is int:
            if isinstance(val, bool):       # a JSON bool for an int field is a type error — don't flip
                return val                  # it to 1/0; let model validation reject it
            # round, don't truncate: a weak model emitting 3.9 for an int field means ~4, not 3
            return int(round(float(val))) if isinstance(val, (str, float)) else int(val)
        if ann is float:
            return float(val)
        if ann is str:
            return val if isinstance(val, str) else (json.dumps(val) if isinstance(val, (dict, list)) else str(val))
    except (ValueError, TypeError, OverflowError):
        # OverflowError: `int(round(float("1e400")))` -> round(inf) raises it; without this catch it
        # escapes both here AND parse_structured (which lists only ValueError/ParseError/…), crashing
        # the run instead of failing over to the next parser. Return the raw value so model validation
        # makes the final decision (it rejects an out-of-range int cleanly).
        return val
    if origin is dict and isinstance(val, dict):
        args = get_args(ann)
        vt = args[1] if len(args) == 2 else typing.Any
        return {str(k): _coerce_value(v, vt) for k, v in val.items()}
    if origin is list and isinstance(val, list):
        args = get_args(ann)
        it = args[0] if args else typing.Any
        return [_coerce_value(x, it) for x in val]
    return val


def _coerce_to_model(obj: dict, model: Type[T]) -> dict:
    """Map a loosely-typed dict onto a model's fields with per-field coercion: case-insensitive key
    match + type repair, dropping extras. The BAML 'Schema-Aligned Parsing' step that lets a weak
    local model's near-miss output validate instead of throwing."""
    out: dict = {}
    lower = {str(k).lower(): k for k in obj}
    for name, field in model.model_fields.items():
        key = name if name in obj else lower.get(name.lower())
        if key is None:
            continue
        out[name] = _coerce_value(obj[key], field.annotation)
    return out


# The parser vocabulary. `Settings.llm_parser` is validated against these keys at construction
# (config.py::_check_enum_fields), so a typo fails loudly instead of silently resolving to the default
# order below — which is otherwise indistinguishable from asking for the default. Adding a key here
# widens what the setting accepts; `tests/test_parse_llm.py` pins the two together.
_ORDER = {
    "tool_call": ["tool_call", "baml"],
    # Paid durable jobs must not hide a second provider call behind parser fallback. Their caller has
    # already claimed one invocation and must be able to record an unambiguous failure for that invocation.
    "tool_call_once": ["tool_call"],
    "baml": ["baml"],
    # "outlines" is an alias for the text (baml) path until constrained decoding lands here —
    # `parse_structured` treats any non-"tool_call" entry as the text+JSON-extraction path. For
    # endpoint-side constrained decoding today, see the `llm_guided_json` setting instead.
    "outlines": ["outlines", "baml"],
}


def parse_structured(
    client: LLMClient,
    messages: list[dict],
    model: Type[T],
    parser: str = "tool_call",
) -> T:
    """Return a validated `model` instance, trying parsers in fallback order.

    THE WALK IS RECORDED (2026-08-19). Which parser answered, and how many failed before it, used to
    be invisible: the caller gets a validated object either way, so a native function-call collapse
    that a second provider call rescued left no trace anywhere — no span, no counter, no event. That
    is what made `docs/BACKLOG.md` H2 ("make the schema-aligned parser the default") unanswerable on
    this box: the row's ~20% vs ~92-94% is a different deployment's benchmark, taken before H1's
    `guided_json` shipped, and `guided_json` repairs exactly the weakness the row is about. The span
    is what lets the default be decided from OUR endpoints instead of someone else's.
    """
    schema = model.model_json_schema()
    order = _ORDER.get(parser, ["tool_call", "baml"])
    with _structured_parse(parser) as _obs:
        return _walk_parsers(client, messages, model, schema, order, _obs)


def _walk_parsers(client, messages, model, schema, order, obs) -> T:
    """The fallback walk itself. Split out so the observation above wraps ONE expression and the
    walk keeps its original shape — every `return`/`continue` below is where it always was."""
    last_err: Exception | None = None
    attempts = 0
    for p in order:
        attempts += 1
        try:
            if p == "tool_call":
                obj = client.complete_tool(messages, schema)
            else:  # baml / outlines text path: ask for JSON, extract, validate
                # A trailing USER message, not a trailing `system`: several strict OpenAI-compatible
                # chat templates (some llama.cpp / Mistral servers) require the system role to come
                # FIRST and 400 on a mid-conversation system turn — which would make the very fallback
                # path fail on the endpoints most likely to need it. A final user instruction is
                # universally accepted.
                hint = {"role": "user",
                        "content": f"Respond with ONLY a JSON object matching this schema: {json.dumps(schema)}"}
                # The SCHEMA reaches the extractor: this hint pastes it into the prompt, so a model
                # that echoes it back emits a decodable object carrying none of the asked-for fields.
                obj = _extract_json(client.complete_text([*messages, hint]), schema)
            try:
                answer = model.model_validate(obj)
                obs.set("parser_used", p).set("attempts", attempts).set("repaired", False)
                return answer
            except ValidationError:
                # H2 schema-aligned repair: coerce common type/format drift, then re-validate. Only
                # if THAT fails do we fall through to the next parser — so a weak model's near-miss
                # (e.g. {"degree":"3"} or single-quoted keys) parses instead of crashing the run.
                answer = model.model_validate(_coerce_to_model(obj, model))
                # `repaired` is the OTHER half of the H2 question and is not the same fact as which
                # parser won: a `tool_call` that only validated after coercion is a native FC that
                # nearly collapsed, and counting it as a clean win would hide precisely the signal
                # the default flip needs.
                obs.set("parser_used", p).set("attempts", attempts).set("repaired", True)
                return answer
        except (ValidationError, ParseError, json.JSONDecodeError, KeyError, AttributeError,
                ArithmeticError, TypeError, LLMError) as e:
            # ArithmeticError/TypeError: belt-and-suspenders for a coercion path that raises on
            # pathological model output (e.g. an int field fed an infinite float) — fall over to the
            # next parser rather than crash, honoring the "returns validated or raises ParseError"
            # contract. LLMError (a transient endpoint/transport failure) is treated like an unparseable
            # response: try the next parser, then let the caller fall back — never crash the run.
            last_err = e
            obs.set(f"failed_{p}", type(e).__name__)
            continue
    obs.set("attempts", attempts).set("failed", True)
    raise ParseError(f"all parsers failed (last: {last_err})")


def forced_structured(client: LLMClient, messages: list[dict], model: Type[T], parser: str,
                      *, nudge: str | None = None, then=None, on_fail):
    """One structured parse whose failure DEGRADES to `on_fail` instead of crashing the run.

    The salvage shape four agent roles had each written out (doc 25 AG-05): the agentic Researcher's
    forced emit, the deep Researcher's forced memo, and the Strategist's parse-or-rule decision (the
    fourth, `LLMResearcher.propose`, is a two-attempt retry LOOP with error feedback folded back into
    the prompt — a genuinely different shape, and it keeps its own).

    `nudge` is appended as a trailing USER turn when given. It stays a caller argument because prompt
    text is a contract: each site's wording is its own and must not drift into a shared default.
    `then` runs INSIDE the guarded region, because two callers transform the parsed model there
    (`.to_idea()`, `_assemble(...)`) and a transform that raises must degrade with everything else
    rather than escape past the salvage.

    The exception posture is the part worth spelling ONCE, because it depends on a fact that is not
    visible at any call site: `BudgetExceeded` is deliberately NOT an `LLMError`, so unlike a
    transport failure it passes straight through `parse_structured` rather than arriving as a
    `ParseError`. A hard budget stop must therefore END the run here, while everything else — an
    unparseable answer, a dead endpoint, a coercion that blew up — degrades. Two of the three sites
    re-stated that re-raise and one relied on a narrower catch to get the same effect by accident.
    """
    from looplab.core.errors import BudgetExceeded

    turns = messages + ([{"role": "user", "content": nudge}] if nudge else [])
    try:
        out = parse_structured(client, turns, model, parser)
        return then(out) if then is not None else out
    except BudgetExceeded:      # a hard budget stop ends the run; it is not a degradable failure
        raise
    except Exception as exc:  # noqa: BLE001 — every other failure is what the salvage exists for
        return on_fail(exc)
