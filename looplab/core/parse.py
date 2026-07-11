"""Structured-output parsing (I2, ADR-14). Native tool-calling is the DEFAULT;
on parse/validation failure it auto-falls back to a text+JSON-extraction path
(the BAML "Schema-Aligned Parsing" role). Callers are parser-agnostic.

`LLMClient` is the seam: any object with `complete_tool` + `complete_text` works,
so the real LiteLLM client and the test fake are interchangeable.
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


def to_float(v, *, finite: bool = False):
    """`float(v)` or None when unparseable. `finite=True` additionally rejects NaN/inf — the
    metric-reading rule (a diverged run must read as "no metric", never enter best-selection).
    The one spelling of scalar coercion previously re-implemented per module."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (finite and not math.isfinite(f)) else f


def to_int(v):
    """`int(float(v))` or None when unparseable (nvidia-smi CSV cells and similar)."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
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


def _extract_json(text: str) -> dict:
    # Reasoning models (e.g. Qwen3) wrap chain-of-thought in <think>…</think> that
    # can itself contain braces — strip it before locating the JSON object.
    text = _THINK.sub("", text)
    decoder = json.JSONDecoder()
    # Decode the first complete JSON object, ignoring any trailing prose (which may
    # itself contain braces — so a naive find('{')..rfind('}') span is unsafe).
    i = text.find("{")
    while i != -1:
        try:
            obj, _ = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            return obj
        i = text.find("{", i + 1)
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


_ORDER = {
    "tool_call": ["tool_call", "baml"],
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
    """Return a validated `model` instance, trying parsers in fallback order."""
    schema = model.model_json_schema()
    last_err: Exception | None = None
    for p in _ORDER.get(parser, ["tool_call", "baml"]):
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
                obj = _extract_json(client.complete_text([*messages, hint]))
            try:
                return model.model_validate(obj)
            except ValidationError:
                # H2 schema-aligned repair: coerce common type/format drift, then re-validate. Only
                # if THAT fails do we fall through to the next parser — so a weak model's near-miss
                # (e.g. {"degree":"3"} or single-quoted keys) parses instead of crashing the run.
                return model.model_validate(_coerce_to_model(obj, model))
        except (ValidationError, ParseError, json.JSONDecodeError, KeyError, AttributeError,
                ArithmeticError, TypeError, LLMError) as e:
            # ArithmeticError/TypeError: belt-and-suspenders for a coercion path that raises on
            # pathological model output (e.g. an int field fed an infinite float) — fall over to the
            # next parser rather than crash, honoring the "returns validated or raises ParseError"
            # contract. LLMError (a transient endpoint/transport failure) is treated like an unparseable
            # response: try the next parser, then let the caller fall back — never crash the run.
            last_err = e
            continue
    raise ParseError(f"all parsers failed (last: {last_err})")
