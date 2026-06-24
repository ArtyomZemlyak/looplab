"""LLM client + cost accounting (I2/I13, ADR-14/17/11).

`LiteLLMClient` wraps LiteLLM (lazy import — not required for the offline path or
tests). `CostAccountant` tallies per-call cost with warn/hard-stop thresholds and
raises `BudgetExceeded` at 100%. Secrets are never stored as values here — the
client takes a model name and reads the key from the environment via LiteLLM.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

from .tracing import record_llm_call


class BudgetExceeded(Exception):
    pass


class LLMError(RuntimeError):
    """A reachable LLM transport/protocol failure (network down, HTTP error, non-JSON, no choices).
    Raised instead of leaking a raw urllib/JSON exception so the role layer's retry+fallback treats
    it like any other bad response and the run degrades to a safe default rather than crashing."""


def _assistant_text(msg: dict) -> str:
    """Display text for a tool-calling assistant turn: its content, plus a compact note of any
    tool calls it made (so the trace shows the model chose to call a tool, not an empty reply)."""
    content = msg.get("content") or ""
    calls = msg.get("tool_calls") or []
    if calls:
        names = ", ".join(c.get("function", {}).get("name", "?") for c in calls)
        note = f"[tool_calls: {names}]"
        return f"{content}\n{note}" if content else note
    return content


class OpenAICompatibleClient:
    """Dependency-free OpenAI-compatible chat client (stdlib urllib). Implements the
    `parse.LLMClient` Protocol, so it drops into the LLM roles like any other backend.

    Works against ANY OpenAI-compatible endpoint — Ollama (`/v1`), SGLang, vLLM, or
    the OpenAI API itself — so the serving backend is a base_url change, not code.
    Chosen for the live path because it has no install footprint and runs on the same
    Python 3.14 the engine uses (LiteLLM remains the documented production gateway)."""

    def __init__(self, model: str, base_url: str = "http://localhost:11434/v1",
                 api_key: str = "ollama", temperature: float = 0.7,
                 timeout: float = 180.0, accountant: Optional["CostAccountant"] = None,
                 guided_json: bool = False):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or "x"
        self.temperature = temperature
        self.timeout = timeout
        self.accountant = accountant or CostAccountant()
        # H1: when the endpoint supports constrained decoding (vLLM/SGLang), drive structured calls
        # from the Pydantic JSON schema — `response_format` json_schema (OpenAI-standard, vLLM+SGLang)
        # + `guided_json` (vLLM extra) — so a weak model can't emit invalid JSON. Off by default
        # (Ollama needs no constraint and some builds reject unknown fields).
        self.guided_json = guided_json

    def _post(self, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        # A network blip / HTTP error / non-JSON body must surface as a clean LLMError, not an
        # unhandled URLError/HTTPError/JSONDecodeError that aborts the whole run — the role layer
        # already retries + falls back on LLMError. (urllib.error was imported but unused before.)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            raise LLMError(f"LLM request to {self.base_url} failed: {e}") from e
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as e:
            raise LLMError(f"LLM returned non-JSON: {raw[:200]!r}") from e
        if not isinstance(body, dict) or "choices" not in body or not body["choices"]:
            # Ollama/vLLM emit {"error": ...} envelopes on a bad request — don't index [0] blind.
            raise LLMError(f"LLM response had no choices: {str(body)[:200]}")
        usage = body.get("usage") or {}
        # No price table for local models -> account tokens as 0 cost, but track them.
        self.accountant.add(0.0, usage=usage)
        self._last_usage = usage
        return body

    def complete_text(self, messages: list[dict]) -> str:
        body = self._post({"model": self.model, "messages": messages,
                           "temperature": self.temperature, "stream": False})
        out = body["choices"][0]["message"].get("content") or ""
        record_llm_call(op="complete_text", model=self.model, messages=messages,
                        completion=out, usage=body.get("usage"))
        return out

    def chat(self, messages: list[dict], tools: list[dict],
             tool_choice: str = "auto") -> dict:
        """General multi-turn tool-calling step. Returns the raw assistant message
        (content + optional tool_calls) so the caller can run an agent loop."""
        body = self._post({
            "model": self.model, "messages": messages, "tools": tools,
            "tool_choice": tool_choice, "temperature": self.temperature, "stream": False,
        })
        msg = body["choices"][0]["message"]
        record_llm_call(op="chat", model=self.model, messages=messages,
                        completion=_assistant_text(msg), usage=body.get("usage"))
        return msg

    def complete_tool(self, messages: list[dict], json_schema: dict) -> dict:
        tool = {"type": "function",
                "function": {"name": "emit", "description": "Emit the structured result.",
                             "parameters": json_schema}}
        payload = {
            "model": self.model, "messages": messages, "tools": [tool],
            "tool_choice": {"type": "function", "function": {"name": "emit"}},
            "temperature": self.temperature, "stream": False,
        }
        if self.guided_json:   # H1 constrained decoding (vLLM/SGLang); Ollama ignores when off
            payload["response_format"] = {"type": "json_schema",
                                          "json_schema": {"name": "emit", "schema": json_schema}}
            payload["guided_json"] = json_schema
        body = self._post(payload)
        msg = body["choices"][0]["message"]
        calls = msg.get("tool_calls")
        if not calls:  # endpoint ignored tool_choice -> let parse.py fall back to text
            raise KeyError("no tool_calls in response")
        args = calls[0]["function"]["arguments"]
        record_llm_call(op="complete_tool", model=self.model, messages=messages,
                        completion=args if isinstance(args, str) else json.dumps(args),
                        usage=body.get("usage"))
        return json.loads(args) if isinstance(args, str) else args


class CostAccountant:
    def __init__(self, limit: Optional[float] = None, warn_frac: float = 0.8):
        self.limit = limit
        self.warn_frac = warn_frac
        self.spent = 0.0
        self.warned = False
        # Token accounting (UI cost panel): local models have no $ price, but tokens are the
        # real signal of how much LLM work a run cost. Accumulated across all calls.
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0

    def add(self, cost: Optional[float], usage: Optional[dict] = None) -> float:
        self.spent += max(0.0, float(cost or 0.0))
        if usage:
            self.calls += 1
            pt = int(usage.get("prompt_tokens") or 0)
            ct = int(usage.get("completion_tokens") or 0)
            self.prompt_tokens += pt
            self.completion_tokens += ct
            self.total_tokens += int(usage.get("total_tokens") or (pt + ct))
        if self.limit is not None:
            if not self.warned and self.spent >= self.warn_frac * self.limit:
                self.warned = True  # caller may surface a warning event
            if self.spent >= self.limit:
                raise BudgetExceeded(f"spent {self.spent:.4f} >= budget {self.limit:.4f}")
        return self.spent

    def remaining(self) -> Optional[float]:
        return None if self.limit is None else max(0.0, self.limit - self.spent)


class LiteLLMClient:
    """Real backend. Implements the `parse.LLMClient` Protocol. Lazy-imports litellm
    so the package installs and tests run without it."""

    def __init__(self, model: str, accountant: Optional[CostAccountant] = None, **kwargs):
        self.model = model
        self.accountant = accountant or CostAccountant()
        self.kwargs = kwargs

    def _litellm(self):
        import litellm  # lazy
        return litellm

    def _account(self, resp) -> None:
        cost = None
        try:
            cost = resp._hidden_params.get("response_cost")  # type: ignore[attr-defined]
        except Exception:
            cost = None
        self.accountant.add(cost or 0.0)

    def _usage(self, resp) -> Optional[dict]:
        try:
            u = resp.usage
            return {"prompt_tokens": getattr(u, "prompt_tokens", 0),
                    "completion_tokens": getattr(u, "completion_tokens", 0),
                    "total_tokens": getattr(u, "total_tokens", 0)}
        except Exception:
            return None

    def complete_text(self, messages: list[dict]) -> str:
        resp = self._litellm().completion(model=self.model, messages=messages, **self.kwargs)
        self._account(resp)
        out = resp.choices[0].message.content or ""
        record_llm_call(op="complete_text", model=self.model, messages=messages,
                        completion=out, usage=self._usage(resp))
        return out

    def complete_tool(self, messages: list[dict], json_schema: dict) -> dict:
        tool = {
            "type": "function",
            "function": {"name": "emit", "description": "Emit the structured result.",
                         "parameters": json_schema},
        }
        resp = self._litellm().completion(
            model=self.model, messages=messages, tools=[tool],
            tool_choice={"type": "function", "function": {"name": "emit"}}, **self.kwargs,
        )
        self._account(resp)
        calls = resp.choices[0].message.tool_calls
        if not calls:  # endpoint ignored tool_choice -> KeyError so parse.py falls back
            raise KeyError("no tool_calls in response")
        args = calls[0].function.arguments
        record_llm_call(op="complete_tool", model=self.model, messages=messages,
                        completion=args if isinstance(args, str) else json.dumps(args),
                        usage=self._usage(resp))
        return json.loads(args)
