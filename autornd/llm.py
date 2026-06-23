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


class BudgetExceeded(Exception):
    pass


class OpenAICompatibleClient:
    """Dependency-free OpenAI-compatible chat client (stdlib urllib). Implements the
    `parse.LLMClient` Protocol, so it drops into the LLM roles like any other backend.

    Works against ANY OpenAI-compatible endpoint — Ollama (`/v1`), SGLang, vLLM, or
    the OpenAI API itself — so the serving backend is a base_url change, not code.
    Chosen for the live path because it has no install footprint and runs on the same
    Python 3.14 the engine uses (LiteLLM remains the documented production gateway)."""

    def __init__(self, model: str, base_url: str = "http://localhost:11434/v1",
                 api_key: str = "ollama", temperature: float = 0.7,
                 timeout: float = 180.0, accountant: Optional["CostAccountant"] = None):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or "x"
        self.temperature = temperature
        self.timeout = timeout
        self.accountant = accountant or CostAccountant()

    def _post(self, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        usage = body.get("usage") or {}
        # No price table for local models -> account tokens as 0 cost, but track them.
        self.accountant.add(0.0)
        self._last_usage = usage
        return body

    def complete_text(self, messages: list[dict]) -> str:
        body = self._post({"model": self.model, "messages": messages,
                           "temperature": self.temperature, "stream": False})
        return body["choices"][0]["message"].get("content") or ""

    def chat(self, messages: list[dict], tools: list[dict],
             tool_choice: str = "auto") -> dict:
        """General multi-turn tool-calling step. Returns the raw assistant message
        (content + optional tool_calls) so the caller can run an agent loop."""
        body = self._post({
            "model": self.model, "messages": messages, "tools": tools,
            "tool_choice": tool_choice, "temperature": self.temperature, "stream": False,
        })
        return body["choices"][0]["message"]

    def complete_tool(self, messages: list[dict], json_schema: dict) -> dict:
        tool = {"type": "function",
                "function": {"name": "emit", "description": "Emit the structured result.",
                             "parameters": json_schema}}
        body = self._post({
            "model": self.model, "messages": messages, "tools": [tool],
            "tool_choice": {"type": "function", "function": {"name": "emit"}},
            "temperature": self.temperature, "stream": False,
        })
        msg = body["choices"][0]["message"]
        calls = msg.get("tool_calls")
        if not calls:  # endpoint ignored tool_choice -> let parse.py fall back to text
            raise KeyError("no tool_calls in response")
        args = calls[0]["function"]["arguments"]
        return json.loads(args) if isinstance(args, str) else args


class CostAccountant:
    def __init__(self, limit: Optional[float] = None, warn_frac: float = 0.8):
        self.limit = limit
        self.warn_frac = warn_frac
        self.spent = 0.0
        self.warned = False

    def add(self, cost: Optional[float]) -> float:
        self.spent += max(0.0, float(cost or 0.0))
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

    def complete_text(self, messages: list[dict]) -> str:
        resp = self._litellm().completion(model=self.model, messages=messages, **self.kwargs)
        self._account(resp)
        return resp.choices[0].message.content or ""

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
        return json.loads(calls[0].function.arguments)
