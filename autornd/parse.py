"""Structured-output parsing (I2, ADR-14). Native tool-calling is the DEFAULT;
on parse/validation failure it auto-falls back to a text+JSON-extraction path
(the BAML "Schema-Aligned Parsing" role). Callers are parser-agnostic.

`LLMClient` is the seam: any object with `complete_tool` + `complete_text` works,
so the real LiteLLM client and the test fake are interchangeable.
"""
from __future__ import annotations

import json
import re
from typing import Protocol, Type, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_PY = re.compile(r"```(?:python|py)\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_FENCE_ANY = re.compile(r"```\s*(.*?)```", re.DOTALL)


def strip_think(text: str) -> str:
    return _THINK.sub("", text)


def extract_code(text: str) -> str:
    """Pull a runnable script out of an LLM reply: drop <think>, prefer a python-tagged
    fenced block (so a leading output/example fence doesn't win), else the first bare
    fence, else the stripped remainder."""
    text = strip_think(text)
    m = _FENCE_PY.search(text) or _FENCE_ANY.search(text)
    return (m.group(1) if m else text).strip()


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
    raise ParseError("no JSON object found in text")


_ORDER = {
    "tool_call": ["tool_call", "baml"],
    "baml": ["baml"],
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
                hint = {"role": "system",
                        "content": f"Respond with ONLY a JSON object matching this schema: {json.dumps(schema)}"}
                obj = _extract_json(client.complete_text([*messages, hint]))
            return model.model_validate(obj)
        except (ValidationError, ParseError, json.JSONDecodeError, KeyError, AttributeError) as e:
            last_err = e
            continue
    raise ParseError(f"all parsers failed (last: {last_err})")
