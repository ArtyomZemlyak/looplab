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

from . import tracing


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
                 guided_json: bool = False, reasoning: Optional[dict] = None,
                 stream: bool = True):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or "x"
        self.temperature = temperature
        self.timeout = timeout
        # Stream EVERY request (SSE) and reassemble it, so `timeout` acts as an INTER-TOKEN idle
        # timeout, NOT a whole-request deadline: a long-but-alive generation keeps streaming tokens
        # (each resets the timer, so it's never cut off), while a genuinely STALLED endpoint (no data
        # for `timeout` s) trips the socket read timeout and is retried on a fresh connection. This is
        # what stops the "27-minute hang" without ever capping a slow reasoning model. Opt out
        # (stream=False) to use one blocking read (subject to the old per-op timeout semantics).
        self.stream = stream
        self.accountant = accountant or CostAccountant()
        # Provider-specific reasoning toggle (from `reasoning_body`) merged into EVERY request, so the
        # whole agent loop (propose/chat/tool) runs with the same thinking setting. Empty = unchanged.
        self.reasoning = reasoning or {}
        # Flips to False permanently for this client the first time the endpoint rejects our reasoning
        # toggle with a 400 (e.g. litellm UnsupportedParamsError for reasoning_effort on glm-5.1), so
        # the request is retried without it and the model works. Deepseek keeps its reasoning; glm-5.1
        # silently drops it. Per-client (per-model), detected once and cached.
        self._reasoning_ok = True
        self._max_retries = 4               # 429/5xx backoff retries before surfacing an LLMError
        # H1: when the endpoint supports constrained decoding (vLLM/SGLang), drive structured calls
        # from the Pydantic JSON schema — `response_format` json_schema (OpenAI-standard, vLLM+SGLang)
        # + `guided_json` (vLLM extra) — so a weak model can't emit invalid JSON. Off by default
        # (Ollama needs no constraint and some builds reject unknown fields).
        self.guided_json = guided_json

    def _read_stream(self, resp) -> dict:
        """Reassemble an OpenAI SSE stream into the non-streaming response body shape
        ({"choices":[{"message":{content,reasoning?,tool_calls?}, "finish_reason"}], "usage"}). Each
        `for raw in resp` line is bounded by the socket `timeout`, so with streaming that timeout is an
        INTER-TOKEN idle timeout: a stall (no line for `timeout` s) raises socket.timeout here and
        propagates to `_post`'s transient-retry path; a steady stream is never cut off however long the
        generation runs. tool_call deltas are merged by `index` (partial name/arguments concatenated)."""
        content: list[str] = []
        reasoning: list[str] = []
        tcs: dict[int, dict] = {}
        finish = None
        usage: dict = {}
        raw_lines: list[str] = []
        got_sse = False
        # Idle timeout by PROGRESS, enforced two ways because a stall can hide from either alone:
        #  (a) the per-socket read timeout resets on ANY byte, so SSE keepalive heartbeats keep a
        #      STALLED generation's connection "alive" forever (observed: a ~70-min hang); and
        #  (b) a server that trickles bytes WITHOUT completing a line (huge-prompt prefill, partial
        #      chunks) blocks inside recv() so the in-loop check below never even runs (observed: a
        #      ~15-min hang on glm-5.1 during a big implement prompt).
        # So we track wall-clock since the last REAL token and (a) check it at the top of the loop AND
        # (b) run a watchdog thread that force-closes the response when it's exceeded — closing the fd
        # makes the blocked recv() raise, which _post catches and retries on a fresh connection. A
        # long-but-alive generation keeps emitting content, resetting the clock — no hard deadline.
        import threading
        idle_limit = self.timeout
        last_progress = [time.monotonic()]
        _stop = threading.Event()

        def _watchdog():
            while not _stop.wait(min(5.0, idle_limit / 4)):
                if time.monotonic() - last_progress[0] > idle_limit:
                    try:
                        resp.close()            # interrupt a recv() blocked on a stalled connection
                    except Exception:  # noqa: BLE001
                        pass
                    return

        _wd = threading.Thread(target=_watchdog, daemon=True)
        _wd.start()
        try:
            try:
                lines = iter(resp)              # SSE responses iterate line-by-line
            except TypeError:
                lines = iter(())                # a non-iterable body (e.g. a test mock) -> read() below
            for raw in lines:
                if time.monotonic() - last_progress[0] > idle_limit:
                    raise TimeoutError(f"LLM stream stalled — no new tokens for {idle_limit:.0f}s; "
                                       f"retrying on a fresh connection")
                s = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
                raw_lines.append(s)
                line = s.strip()
                if not line or not line.startswith("data:"):
                    continue
                got_sse = True
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except ValueError:
                    continue                    # keepalive / non-JSON heartbeat line
                if obj.get("usage"):
                    usage = obj["usage"]
                    last_progress[0] = time.monotonic()
                ch = (obj.get("choices") or [{}])[0] or {}
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    content.append(delta["content"])
                    last_progress[0] = time.monotonic()   # real token => progress
                r = delta.get("reasoning") or delta.get("reasoning_content")
                if r:
                    reasoning.append(r)
                    last_progress[0] = time.monotonic()
                for tc in (delta.get("tool_calls") or []):
                    last_progress[0] = time.monotonic()
                    slot = tcs.setdefault(tc.get("index", 0),
                                          {"id": None, "type": "function",
                                           "function": {"name": "", "arguments": []}})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"].append(fn["arguments"])
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
        finally:
            _stop.set()                         # stop the watchdog before we return / propagate
        # Not actually an SSE stream (a non-streaming endpoint, or a test mock that returns one JSON
        # body): parse the whole body as a normal chat completion so streaming stays transparent.
        if not got_sse and not content and not tcs:
            blob = "".join(raw_lines)
            if not blob and hasattr(resp, "read"):
                try:
                    blob = resp.read().decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    blob = ""
            whole = _parse_chat_body(blob)
            if whole is not None:               # any parsed JSON body (incl. an {"error":…} envelope,
                return whole                    # which the caller fails fast on at the no-choices check)
        msg: dict = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            msg["reasoning"] = "".join(reasoning)
        if tcs:
            msg["tool_calls"] = [
                {"id": s["id"] or f"call_{i}", "type": "function",
                 "function": {"name": s["function"]["name"], "arguments": "".join(s["function"]["arguments"])}}
                for i, s in sorted(tcs.items())]
        return {"choices": [{"message": msg, "finish_reason": finish}], "usage": usage}

    def _post(self, payload: dict) -> dict:
        # A network blip / HTTP error / non-JSON body must surface as a clean LLMError, not an
        # unhandled URLError/HTTPError/JSONDecodeError that aborts the whole run — the role layer
        # already retries + falls back on LLMError. (urllib.error was imported but unused before.)
        # Rate-limit/transient resilience: a 429 (or 5xx) is retried with backoff (honoring a
        # Retry-After header when given) BEFORE surfacing — free/shared endpoints (e.g. OpenRouter
        # free tier) rate-limit bursts, and a single 429 shouldn't crash the whole run.
        body = None
        for attempt in range(self._max_retries + 1):
            # Build the request per attempt so a param-compat retry (below) can drop the reasoning
            # toggle. `_reasoning_ok` starts True and flips off permanently for THIS client the first
            # time the endpoint rejects our reasoning param.
            p = dict(payload)
            if self.reasoning and self._reasoning_ok:   # inject the reasoning toggle
                p = {**p, **self.reasoning}
            if self.stream:                     # force streaming so `timeout` is an inter-token idle guard
                p = {**p, "stream": True, "stream_options": {"include_usage": True}}
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=json.dumps(p).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.api_key}"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    # Streaming: reassemble SSE lines (a stall raises socket.timeout HERE, inside the
                    # `with`, and lands in the transient-retry handler below). Non-streaming: one read.
                    parsed = self._read_stream(resp) if self.stream \
                        else _parse_chat_body(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                # A 400 that rejects our REASONING toggle — e.g. a litellm-proxied model like glm-5.1
                # returns UnsupportedParamsError for `reasoning_effort` — isn't a real bad request:
                # drop reasoning for this client and retry immediately, so the model still works
                # (deepseek accepts reasoning_effort, glm-5.1 doesn't; the client adapts per model).
                if e.code == 400 and self.reasoning and self._reasoning_ok:
                    try:
                        eb = e.read().decode("utf-8", "replace").lower()
                    except Exception:  # noqa: BLE001
                        eb = ""
                    if any(k in eb for k in ("reasoning", "unsupportedparams",
                                             "does not support parameters", "extra_forbidden",
                                             "unexpected keyword", "unrecognized")):
                        self._reasoning_ok = False
                        continue
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
                # JSON payload), or a stream that carried no content/tool-call. (A mid-read socket drop
                # is caught above as IncompleteRead.) Treat an empty/unparseable 200 like a transient
                # network failure — retry with backoff — rather than crash the run on a gateway hiccup.
                # `parsed` was computed in the try (streamed-and-reassembled, or _parse_chat_body).
                # A parsed dict is accepted (an `{"error": ...}` envelope has no `choices` and fails
                # fast at the post-loop check). Only two cases retry: an unparseable body (None), or a
                # STREAM that produced an empty message (keepalive-only heartbeats, no content/tool_call).
                m = ((parsed.get("choices") or [{}])[0] or {}).get("message") or {} if parsed else {}
                empty_stream = (self.stream and parsed is not None and parsed.get("choices")
                                and not m.get("content") and not m.get("tool_calls"))
                if parsed is not None and not empty_stream:
                    body = parsed
                    break
                if attempt < self._max_retries:
                    time.sleep(min(2.0 * (2 ** attempt), 30.0))
                    continue
                raise LLMError(f"LLM returned non-JSON/empty after {self._max_retries + 1} attempts")
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

    def _model_params(self) -> dict:
        """The generation's model_parameters (Langfuse generation metadata): sampling temperature +
        any provider reasoning toggle, so the trace shows HOW the model was called."""
        return {"temperature": self.temperature, **(self.reasoning or {})}

    def complete_text(self, messages: list[dict]) -> str:
        with tracing.generation(op="complete_text", model=self.model, messages=messages,
                                model_parameters=self._model_params()) as gen:
            body = self._post({"model": self.model, "messages": messages,
                               "temperature": self.temperature, "stream": False})
            msg = body["choices"][0]["message"]
            out = msg.get("content") or ""
            # Record the clean answer as the completion (the conclusion) and the raw reasoning
            # separately; return the original text so downstream parsing is unchanged.
            thinking, answer = _clean_thinking(out, msg.get("reasoning") or msg.get("reasoning_content") or "")
            gen.output(answer or out).thinking(thinking).usage(body.get("usage"))
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
        # The generation span stays open for the whole stream (its duration = time-to-full-answer).
        with tracing.generation(op="complete_text_stream", model=self.model, messages=messages,
                                model_parameters=self._model_params()) as gen:
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
                if not pieces:                 # never streamed -> fall back to a single blocking call
                    text = self.complete_text(messages)   # its own nested generation span
                    if text:
                        yield text
                    return
            if usage:                          # account the streamed answer's tokens like every other call
                self.accountant.add(0.0, usage=usage)
                self._last_usage = usage
            gen.output("".join(pieces)).usage(usage or None)

    def chat(self, messages: list[dict], tools: list[dict],
             tool_choice: str = "auto") -> dict:
        """General multi-turn tool-calling step. Returns the raw assistant message
        (content + optional tool_calls) so the caller can run an agent loop."""
        with tracing.generation(op="chat", model=self.model, messages=messages,
                                model_parameters=self._model_params()) as gen:
            body = self._post({
                "model": self.model, "messages": messages, "tools": tools,
                "tool_choice": tool_choice, "temperature": self.temperature, "stream": False,
            })
            msg = body["choices"][0]["message"]
            thinking, answer = _clean_thinking(msg.get("content") or "",
                                               msg.get("reasoning") or msg.get("reasoning_content") or "")
            # The output records BOTH the assistant text AND any tool_calls it decided to make, so the
            # trace shows what this generation produced (its content + the tool calls the loop will run).
            gen.output(_assistant_text({**msg, "content": answer})).thinking(thinking).usage(body.get("usage"))
            if msg.get("tool_calls"):
                gen.set("tool_calls", [{"name": (c.get("function") or {}).get("name"),
                                        "arguments": (c.get("function") or {}).get("arguments")}
                                       for c in msg["tool_calls"]])
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
        with tracing.generation(op="complete_tool", model=self.model, messages=messages,
                                model_parameters=self._model_params()) as gen:
            body = self._post(payload)
            msg = body["choices"][0]["message"]
            calls = msg.get("tool_calls")
            if not calls:  # endpoint ignored tool_choice -> let parse.py fall back to text
                gen.usage(body.get("usage")).error("no tool_calls in response")
                raise KeyError("no tool_calls in response")
            args = calls[0]["function"]["arguments"]
            # Reasoning models emit their chain-of-thought (a `reasoning` field, or inline <think> in
            # `content`) alongside the tool call; capture it (debug channel) instead of discarding it.
            # The completion stays the structured tool args — the clean conclusion the UI renders.
            thinking, _ = _clean_thinking(msg.get("content") or "",
                                          msg.get("reasoning") or msg.get("reasoning_content") or "")
            gen.output(args if isinstance(args, str) else json.dumps(args)).thinking(thinking).usage(body.get("usage"))
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
        with tracing.generation(op="complete_text", model=self.model, messages=messages) as gen:
            resp = self._litellm().completion(model=self.model, messages=messages, **self.kwargs)
            self._account(resp)
            m = resp.choices[0].message
            out = m.content or ""
            thinking, answer = _clean_thinking(
                out, getattr(m, "reasoning_content", None) or getattr(m, "reasoning", None) or "")
            u = self._usage(resp)
            gen.output(answer or out).thinking(thinking).usage(u).cost(self._cost(resp))
            return out

    def _cost(self, resp):
        try:
            return resp._hidden_params.get("response_cost")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return None

    def complete_tool(self, messages: list[dict], json_schema: dict) -> dict:
        tool = {
            "type": "function",
            "function": {"name": "emit", "description": "Emit the structured result.",
                         "parameters": json_schema},
        }
        with tracing.generation(op="complete_tool", model=self.model, messages=messages) as gen:
            resp = self._litellm().completion(
                model=self.model, messages=messages, tools=[tool],
                tool_choice={"type": "function", "function": {"name": "emit"}}, **self.kwargs,
            )
            self._account(resp)
            calls = resp.choices[0].message.tool_calls
            if not calls:  # endpoint ignored tool_choice -> KeyError so parse.py falls back
                gen.usage(self._usage(resp)).error("no tool_calls in response")
                raise KeyError("no tool_calls in response")
            args = calls[0].function.arguments
            m = resp.choices[0].message
            thinking, _ = _clean_thinking(
                getattr(m, "content", None) or "",
                getattr(m, "reasoning_content", None) or getattr(m, "reasoning", None) or "")
            gen.output(args if isinstance(args, str) else json.dumps(args)).thinking(thinking) \
               .usage(self._usage(resp)).cost(self._cost(resp))
            return json.loads(args) if isinstance(args, str) else (args or {})
