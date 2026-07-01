"""LLM client + cost accounting (I2/I13, ADR-14/17/11).

`LiteLLMClient` wraps LiteLLM (lazy import — not required for the offline path or
tests). `CostAccountant` tallies per-call cost with warn/hard-stop thresholds and
raises `BudgetExceeded` at 100%. Secrets are never stored as values here — the
client takes a model name and reads the key from the environment via LiteLLM.
"""
from __future__ import annotations

import http.client
import json
import ssl
import time
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


def _is_transient(e: BaseException) -> bool:
    """True for a TRANSIENT network failure worth retrying — a socket timeout, a reset/aborted
    connection, or a mid-read TLS EOF (UNEXPECTED_EOF_WHILE_READING: the peer hung up mid-response,
    common over a busy hosted gateway like OpenRouter). FALSE for steady-state failures that should
    fail FAST: a refused connection (endpoint down), a DNS error (bad host), or a TLS CERT error
    (misconfig). urllib wraps the real cause in `URLError.reason`, so check that too. (`socket.timeout`
    is an alias of `TimeoutError` since Python 3.10.)"""
    for x in (e, getattr(e, "reason", None)):
        if x is None:
            continue
        if isinstance(x, (TimeoutError, ConnectionResetError, ConnectionAbortedError)):
            return True
        if isinstance(x, ssl.SSLError) and not isinstance(x, ssl.SSLCertVerificationError):
            return True   # dropped TLS read (e.g. UNEXPECTED_EOF), NOT a cert problem
    return isinstance(e, (http.client.IncompleteRead, http.client.RemoteDisconnected))


def _parse_chat_body(raw: Optional[str]) -> Optional[dict]:
    """Parse an OpenAI-compatible chat response body into its dict, or None when the body is
    UNRECOVERABLE. A hosted gateway can return HTTP 200 with a body that is empty / whitespace /
    truncated (it dropped the connection mid-response) or that interleaves SSE keep-alive COMMENT
    lines (': OPENROUTER PROCESSING' — sent to hold the socket open while a model is queued) around
    the JSON. A None return tells `_post` to retry the request like a transient network failure
    instead of crashing the run on a single gateway hiccup. A parsed dict is always returned as-is
    (including an `{"error": ...}` envelope), so the caller's no-`choices` check still fails fast on
    a genuine bad-request — only an unparseable body is retried."""
    if not raw or not raw.strip():
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Recover the keepalive-interleaved case: drop blank + ':'-comment lines, re-parse the rest.
        kept = "\n".join(ln for ln in raw.splitlines()
                         if ln.strip() and not ln.lstrip().startswith(":"))
        if not kept.strip():
            return None
        try:
            obj = json.loads(kept)
        except (json.JSONDecodeError, ValueError):
            return None
    return obj if isinstance(obj, dict) else None


def split_think(text: str) -> tuple[str, str]:
    """(thinking, answer) split — thin wrapper over `parse.split_think`, imported lazily to avoid
    the parse↔llm import cycle (parse imports LLMError from here)."""
    from .parse import split_think as _split
    return _split(text)


def _clean_thinking(content: str, reasoning: str = "") -> tuple[str, str]:
    """(thinking, answer) for an assistant turn. Reasoning models surface their chain-of-thought one
    of two ways: a DEDICATED field (`reasoning`/`reasoning_content` — newer Ollama/OpenAI) or INLINE
    <think>…</think> tags in `content`. Prefer the dedicated field (content is then already clean);
    otherwise split the inline tags. Either way the answer is the clean conclusion the UI surfaces."""
    if reasoning:
        _, answer = split_think(content or "")   # strip any stray inline tags too, for safety
        return str(reasoning), (answer or content or "")
    return split_think(content or "")


def reasoning_body(model: str, mode: str = "", style: str = "auto",
                   extra: Optional[dict] = None) -> dict:
    """The provider-specific request fields that TOGGLE a reasoning/thinking model — providers differ:
      - Qwen3 on vLLM/SGLang: `chat_template_kwargs.enable_thinking` (bool)
      - OpenAI / Ollama-v1 / DeepSeek: `reasoning_effort` (low|medium|high|none)
    `mode`: "" = inject nothing (use the server default — unchanged behavior); off|none = disable;
    on = enable at default depth; low|medium|high = enable at that effort. `style`: auto picks `qwen`
    for qwen* models else `effort`. `extra` is merged last (escape hatch, e.g. Anthropic
    `{"thinking": {"type": "enabled", "budget_tokens": N}}`)."""
    mode = (mode or "").strip().lower()
    body: dict = {}
    if mode:
        st = (style or "auto").lower()
        if st == "auto":
            st = "qwen" if "qwen" in (model or "").lower() else "effort"
        on = mode not in ("off", "none", "false", "0")
        if st == "qwen":
            body["chat_template_kwargs"] = {"enable_thinking": on}
        elif st == "effort":
            # OpenAI/OpenRouter accept only low|medium|high — "none" 400s. To DISABLE on an
            # effort-style provider, send nothing (server default); rely on `extra` for a provider
            # that has an explicit off switch (e.g. OpenRouter `{"reasoning": {"enabled": false}}`).
            if on:
                body["reasoning_effort"] = "medium" if mode == "on" else mode
        # st == "none": shape nothing (rely solely on `extra`)
    if extra:
        body = {**body, **extra}
    return body


def _retry_after_seconds(ra) -> Optional[float]:
    """Parse a Retry-After header into seconds. It may be a number (int/float seconds) OR an
    HTTP-date; returns the delay in seconds (clamped ≥0) or None when absent/unparseable (caller
    then falls back to exponential backoff)."""
    if not ra:
        return None
    s = str(ra).strip()
    try:
        return max(0.0, float(s))
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


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
                 guided_json: bool = False, reasoning: Optional[dict] = None):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or "x"
        self.temperature = temperature
        self.timeout = timeout
        self.accountant = accountant or CostAccountant()
        # Provider-specific reasoning toggle (from `reasoning_body`) merged into EVERY request, so the
        # whole agent loop (propose/chat/tool) runs with the same thinking setting. Empty = unchanged.
        self.reasoning = reasoning or {}
        self._max_retries = 4               # 429/5xx backoff retries before surfacing an LLMError
        # H1: when the endpoint supports constrained decoding (vLLM/SGLang), drive structured calls
        # from the Pydantic JSON schema — `response_format` json_schema (OpenAI-standard, vLLM+SGLang)
        # + `guided_json` (vLLM extra) — so a weak model can't emit invalid JSON. Off by default
        # (Ollama needs no constraint and some builds reject unknown fields).
        self.guided_json = guided_json

    def _post(self, payload: dict) -> dict:
        if self.reasoning:                  # inject the reasoning toggle into every request
            payload = {**payload, **self.reasoning}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        # A network blip / HTTP error / non-JSON body must surface as a clean LLMError, not an
        # unhandled URLError/HTTPError/JSONDecodeError that aborts the whole run — the role layer
        # already retries + falls back on LLMError. (urllib.error was imported but unused before.)
        # Rate-limit/transient resilience: a 429 (or 5xx) is retried with backoff (honoring a
        # Retry-After header when given) BEFORE surfacing — free/shared endpoints (e.g. OpenRouter
        # free tier) rate-limit bursts, and a single 429 shouldn't crash the whole run.
        body = None
        for attempt in range(self._max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < self._max_retries:
                    ra = _retry_after_seconds(e.headers.get("Retry-After") if e.headers else None)
                    delay = ra if ra is not None else 2.0 * (2 ** attempt)
                    time.sleep(min(delay, 30.0))
                    continue
                hint = (" — check the API key (LOOPLAB_LLM_API_KEY)" if e.code == 401 else "")
                raise LLMError(f"LLM request to {self.base_url} failed: {e}{hint}") from e
            except (urllib.error.URLError, TimeoutError, OSError, http.client.IncompleteRead) as e:
                # Retry TRANSIENT network failures (timeout, reset, TLS EOF / IncompleteRead mid-read)
                # — the modes that
                # otherwise abort a long run, since the tool-using proposer / pilot call client.chat
                # directly and don't wrap LLMError. A refused connection / DNS / TLS CERT error is a
                # steady-state "endpoint down or misconfigured" signal: fail FAST so /api/llm/health
                # stays instant and a wrong base_url surfaces on the first call, not after backoff.
                if _is_transient(e) and attempt < self._max_retries:
                    time.sleep(min(2.0 * (2 ** attempt), 30.0))
                    continue
                raise LLMError(f"LLM request to {self.base_url} failed: {e}") from e
            else:
                # HTTP 200 read cleanly, but the body can still be unusable: a hosted gateway
                # sometimes returns an empty / whitespace / SSE-keepalive-only body (OpenRouter sends
                # ': OPENROUTER PROCESSING' heartbeats while a model is queued and can finish with no
                # JSON payload). (A mid-read socket drop is caught above as IncompleteRead.) Treat an
                # unparseable 200 like a transient network failure — retry with backoff — rather than
                # crash the run on a single gateway hiccup. `_parse_chat_body`
                # returns the dict for any valid JSON object (so an `{"error": ...}` envelope still
                # fails fast at the no-`choices` check below), and None only when truly unrecoverable.
                parsed = _parse_chat_body(raw)
                if parsed is not None:
                    body = parsed
                    break
                if attempt < self._max_retries:
                    time.sleep(min(2.0 * (2 ** attempt), 30.0))
                    continue
                raise LLMError(f"LLM returned non-JSON after {self._max_retries + 1} attempts: {raw[:200]!r}")
        if body is None:  # loop exhausted retries on a transient code without ever succeeding
            raise LLMError(f"LLM request to {self.base_url} failed: no response after retries")
        if "choices" not in body or not body["choices"]:
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
        msg = body["choices"][0]["message"]
        out = msg.get("content") or ""
        # Record the clean answer as the completion (the conclusion) and the raw reasoning
        # separately; return the original text so downstream parsing is unchanged.
        thinking, answer = _clean_thinking(out, msg.get("reasoning") or msg.get("reasoning_content") or "")
        record_llm_call(op="complete_text", model=self.model, messages=messages,
                        completion=answer or out, thinking=thinking or None,
                        usage=body.get("usage"))
        return out

    def complete_text_stream(self, messages: list[dict]):
        """Stream a plain-text completion token-by-token (an OpenAI `stream:true` SSE call). Yields
        content deltas as they arrive; used by the assistant to stream its final answer live. Falls
        back to a single yield of the whole text if the endpoint doesn't stream. Best-effort — any
        transport error mid-stream ends the generator (the caller keeps what it got)."""
        payload = {"model": self.model, "messages": messages, "temperature": self.temperature,
                   "stream": True, "stream_options": {"include_usage": True}}
        if self.reasoning:
            payload = {**payload, **self.reasoning}
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(payload).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json",
                                    "Authorization": f"Bearer {self.api_key}"})
        pieces: list[str] = []
        usage: dict = {}
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        obj = json.loads(chunk)
                    except ValueError:
                        continue
                    if obj.get("usage"):                       # final usage chunk (include_usage)
                        usage = obj["usage"]
                    delta = ((obj.get("choices") or [{}])[0] or {}).get("delta") or {}
                    piece = delta.get("content") or ""
                    if piece:
                        pieces.append(piece)
                        yield piece
        except (urllib.error.URLError, TimeoutError, OSError, http.client.IncompleteRead):
            if not pieces:                     # never streamed -> fall back to a single blocking call
                text = self.complete_text(messages)
                if text:
                    yield text
                return
        if usage:                              # account the streamed answer's tokens like every other call
            self.accountant.add(0.0, usage=usage)
            self._last_usage = usage
        record_llm_call(op="complete_text_stream", model=self.model, messages=messages,
                        completion="".join(pieces), usage=usage or None)

    def chat(self, messages: list[dict], tools: list[dict],
             tool_choice: str = "auto") -> dict:
        """General multi-turn tool-calling step. Returns the raw assistant message
        (content + optional tool_calls) so the caller can run an agent loop."""
        body = self._post({
            "model": self.model, "messages": messages, "tools": tools,
            "tool_choice": tool_choice, "temperature": self.temperature, "stream": False,
        })
        msg = body["choices"][0]["message"]
        thinking, answer = _clean_thinking(msg.get("content") or "",
                                           msg.get("reasoning") or msg.get("reasoning_content") or "")
        record_llm_call(op="chat", model=self.model, messages=messages,
                        completion=_assistant_text({**msg, "content": answer}),
                        thinking=thinking or None, usage=body.get("usage"))
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
        # Reasoning models emit their chain-of-thought (a `reasoning` field, or inline <think> in
        # `content`) alongside the tool call; capture it (debug channel) instead of discarding it.
        # The completion stays the structured tool args — the clean conclusion the UI renders.
        thinking, _ = _clean_thinking(msg.get("content") or "",
                                      msg.get("reasoning") or msg.get("reasoning_content") or "")
        record_llm_call(op="complete_tool", model=self.model, messages=messages,
                        completion=args if isinstance(args, str) else json.dumps(args),
                        thinking=thinking or None, usage=body.get("usage"))
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
        self.accountant.add(cost or 0.0, usage=self._usage(resp))

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
        m = resp.choices[0].message
        out = m.content or ""
        thinking, answer = _clean_thinking(
            out, getattr(m, "reasoning_content", None) or getattr(m, "reasoning", None) or "")
        record_llm_call(op="complete_text", model=self.model, messages=messages,
                        completion=answer or out, thinking=thinking or None,
                        usage=self._usage(resp))
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
        m = resp.choices[0].message
        thinking, _ = _clean_thinking(
            getattr(m, "content", None) or "",
            getattr(m, "reasoning_content", None) or getattr(m, "reasoning", None) or "")
        record_llm_call(op="complete_tool", model=self.model, messages=messages,
                        completion=args if isinstance(args, str) else json.dumps(args),
                        thinking=thinking or None, usage=self._usage(resp))
        return json.loads(args) if isinstance(args, str) else (args or {})
