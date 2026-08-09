"""MCP client tool provider: expose tools from configured Model Context Protocol servers to the
assistant as ordinary OpenAI functions (ordinary names retain ``mcp__<server>__<tool>``; ambiguous,
unsafe or long origin pairs get a deterministic hashed spelling), so the shared tool loop can call
them with no special-casing — provider-neutral by construction.

Config (first found wins): env ``LOOPLAB_MCP_CONFIG`` (path to JSON), env ``LOOPLAB_MCP_SERVERS``
(inline JSON), or ``<repo>/.mcp.json``. Shape mirrors the common ``.mcp.json``::

    {"mcpServers": {"name": {"command": "npx", "args": ["-y", "pkg"]},        # stdio
                    "web":  {"url": "https://host/mcp"}}}                      # streamable HTTP

Degrades gracefully: no config, no ``mcp`` SDK, or a server that won't connect → that server simply
contributes no tools (never raises into the loop). The spec-conversion and call-routing are separated
from the live transport so they are unit-testable with a fake server handle.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional

# The LoopLab repo root (…/looplab, two levels above this file) — where the default `.mcp.json`
# lives. Computed locally instead of importing `looplab.serve.assistant.REPO_ROOT` (same value):
# the tools layer must not depend on the serve layer.
REPO_ROOT = Path(__file__).resolve().parents[2]
_LOG = logging.getLogger(__name__)


_FUNCTION_NAME_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")
_FUNCTION_NAME_MAX = 64
_MCP_SCHEMA_MAX_BYTES = 64 * 1024


def _prefixed(server: str, tool: str) -> str:
    """Map one MCP origin pair to a provider-safe, collision-resistant function name.

    Preserve the established readable spelling for ordinary unambiguous ASCII components. The
    ``__`` delimiter is not injective when either component itself contains ``__``; unsafe/long or
    ambiguous pairs therefore use a short readable label plus the complete SHA-256 encoded in
    URL-safe base64 (43 chars). The result always fits the common OpenAI 64-character contract.
    """
    if not isinstance(server, str) or not server or not isinstance(tool, str) or not tool:
        raise ValueError("MCP server and tool names must be non-empty strings")
    readable = f"mcp__{server}__{tool}"
    if (len(readable) <= _FUNCTION_NAME_MAX
            and _FUNCTION_NAME_RE.fullmatch(server)
            and _FUNCTION_NAME_RE.fullmatch(tool)
            and "__" not in server and "__" not in tool):
        return readable
    material = json.dumps([server, tool], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = base64.urlsafe_b64encode(hashlib.sha256(material).digest()).decode("ascii").rstrip("=")
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", tool).strip("-_")[:8] or "tool"
    encoded = f"mcp__{label}__{digest}"
    assert len(encoded) <= _FUNCTION_NAME_MAX and _FUNCTION_NAME_RE.fullmatch(encoded)
    return encoded


def _advertised_mcp_spec(server, tool: object) -> tuple[str, str, dict]:
    """Validate one untrusted MCP declaration before either route index is mutated."""
    if not isinstance(tool, dict):
        raise ValueError("tool declaration is not an object")
    original_name = tool.get("name")
    full = _prefixed(getattr(server, "name", None), original_name)
    description = tool.get("description", "")
    if not isinstance(description, str):
        raise ValueError("tool description is not a string")
    schema = tool.get("input_schema")
    if schema is None:
        parameters = {"type": "object", "properties": {}}
    else:
        if not isinstance(schema, dict):
            raise ValueError("tool input_schema is not a JSON Schema object")
        parameters = dict(schema)
        parameters.setdefault("type", "object")
        if parameters.get("type") != "object":
            raise ValueError("tool input_schema must describe an object")
        parameters.setdefault("properties", {})
        if not isinstance(parameters.get("properties"), dict):
            raise ValueError("tool input_schema properties must be an object")
    try:
        encoded_schema = json.dumps(
            parameters, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("tool input_schema is not finite JSON") from exc
    if len(encoded_schema) > _MCP_SCHEMA_MAX_BYTES:
        raise ValueError("tool input_schema exceeds the bounded schema budget")
    advertised = {"type": "function", "function": {
        "name": full, "description": description[:400], "parameters": parameters,
    }}
    return full, original_name, advertised


# Honest truncation, the ToolProvider convention (env_inspect._clamp, reposcout._paginate). `{n}` =
# exact number of characters cut, matching tool_loop._TRUNC_NOTE's shape so a model that has learned
# one marker reads the other. An MCP reply is opaque (JSON, prose, a diff), so unlike env_inspect
# there is no line boundary worth preserving — say how much went missing and let the caller narrow.
_TRUNC_NOTE = "\n…[mcp reply truncated — {n} chars omitted; re-request a narrower query]"
# Headroom reserved for that marker, comfortably above its longest realistic rendering (~80 chars),
# so the note itself never pushes the reply back over the cap it is reporting.
_TRUNC_HEADROOM = 160


def _clip(reply: str, cap: int) -> str:
    """Bound one MCP reply to `cap` chars, saying so when it actually cuts.

    A bare `reply[:cap]` appended no marker AND landed EXACTLY on the cap, so the loop's own
    `_cap_tool_result` — which only marks results LONGER than the cap — added none either: a cut
    reply was byte-indistinguishable from a complete one, and the model acted on a silently
    amputated answer."""
    from looplab.tools._base import clip
    # `reserve` (unlike the log/stream clippers) because `cap` here is the loop's RAW RESULT_CAP with
    # no headroom of its own — see `_base.clip` (doc 25 TO-08).
    return clip(reply, cap, keep="head", note=_TRUNC_NOTE, reserve=_TRUNC_HEADROOM)


def load_config() -> dict:
    """Return {server_name: config} from the first configured source, else {}."""
    raw = None
    p = os.environ.get("LOOPLAB_MCP_CONFIG")
    if p and Path(p).is_file():
        raw = Path(p).read_text(encoding="utf-8")
    elif os.environ.get("LOOPLAB_MCP_SERVERS"):
        raw = os.environ["LOOPLAB_MCP_SERVERS"]
    else:
        default = REPO_ROOT / ".mcp.json"
        if default.is_file():
            raw = default.read_text(encoding="utf-8")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    servers = data.get("mcpServers") or data.get("servers") or {}
    return servers if isinstance(servers, dict) else {}


class McpTools:
    """Aggregate provider over one-or-more connected MCP servers. `servers` is a list of handles, each
    exposing `.name`, `.tools()` -> [{name, description, input_schema}], and `.call(tool, args) -> str`.
    Use `from_config()` for the live path; inject fakes in tests."""

    def __init__(self, servers: Optional[list] = None):
        self.servers = servers or []
        self._route: dict = {}       # prefixed tool name -> (server, tool_name)
        self._specs: list[dict] = []
        self.collisions: list[tuple[str, str, str]] = []
        self.rejections: list[tuple[str, str]] = []
        for s in self.servers:
            try:
                declarations = s.tools()
            except Exception:  # noqa: BLE001 - a flaky server contributes no tools
                continue
            try:
                iterator = iter(declarations)
            except TypeError:
                self.rejections.append(
                    (repr(getattr(s, "name", None)), "tools result is not iterable"))
                _LOG.warning(
                    "MCP server %r returned a non-iterable tool inventory",
                    getattr(s, "name", None),
                )
                continue
            while True:
                try:
                    t = next(iterator)
                except StopIteration:
                    break
                except Exception as exc:  # noqa: BLE001 - one broken inventory stops only its server
                    origin = repr(getattr(s, "name", None))
                    reason = str(exc) or type(exc).__name__
                    self.rejections.append((origin, reason))
                    _LOG.warning("MCP server %s tool inventory failed: %s", origin, reason)
                    break
                try:
                    full, original_name, advertised = _advertised_mcp_spec(s, t)
                except Exception as exc:  # noqa: BLE001 - reject one malformed declaration only
                    tool_name = getattr(t, "get", lambda *_: None)("name")
                    origin = f"{getattr(s, 'name', None)!r}:{tool_name!r}"
                    reason = str(exc) or type(exc).__name__
                    self.rejections.append((origin, reason))
                    _LOG.warning("rejecting malformed MCP tool %s: %s", origin, reason)
                    continue
                try:
                    if full in self._route:
                        first_server, first_tool = self._route[full]
                        first = f"{first_server.name}:{first_tool}"
                        shadowed = f"{s.name}:{original_name}"
                        self.collisions.append((full, first, shadowed))
                        _LOG.warning(
                            "duplicate MCP tool name %r: keeping first target %r, shadowing %r",
                            full, first, shadowed,
                        )
                        # Route and advertised schema are one contract.  Keeping the first schema
                        # while overwriting its route would let an approval for one server/tool
                        # execute a different effect whose prefixed spelling happens to collide.
                        continue
                    self._route[full] = (s, original_name)
                    self._specs.append(advertised)
                except Exception as exc:  # noqa: BLE001 - keep malformed origin data isolated
                    origin = f"{getattr(s, 'name', None)!r}:{original_name!r}"
                    reason = str(exc) or type(exc).__name__
                    self.rejections.append((origin, reason))
                    _LOG.warning("rejecting malformed MCP tool %s: %s", origin, reason)

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        return list(self._specs)

    def execute(self, name: str, args: dict) -> str:
        target = self._route.get(name)
        if not target:
            return f"(unknown tool: {name})"
        server, tool = target
        try:
            # Cap at the loop's RESULT_CAP (the ToolProvider convention: derive budgets FROM it, not a
            # free-standing 8000 that the loop's own 4000 tail-cut always dominates anyway).
            from looplab.tools._base import RESULT_CAP
            return _clip(str(server.call(tool, args or {})), RESULT_CAP)
        except Exception as e:  # noqa: BLE001 - a tool error is data for the model, never a crash
            return f"(mcp error calling {name}: {e})"

    @classmethod
    def from_config(cls) -> "McpTools":
        cfg = load_config()
        if not cfg:
            return cls([])
        try:
            from looplab.tools._mcp_transport import connect_server   # live SDK path (optional dependency)
        except Exception:  # noqa: BLE001 - no mcp SDK installed -> inert
            return cls([])
        servers = []
        for name, spec in cfg.items():
            try:
                servers.append(connect_server(name, spec))
            except Exception:  # noqa: BLE001 - a server that won't connect is skipped
                continue
        return cls([s for s in servers if s is not None])

    @classmethod
    def cached(cls) -> "McpTools":
        """Process-global instance: connect to each MCP server ONCE (a live server owns a background
        thread + event loop + subprocess), not on every assistant turn. build_tools calls this.

        Double-checked under a lock: two concurrent first turns (two tabs/sessions — the workers are
        plain threads) would otherwise both see `_CACHED is None`, both `from_config()`, and each spawn
        a full set of server handles (thread + loop + subprocess); the loser's set orphans and leaks
        for the process lifetime."""
        global _CACHED
        if _CACHED is None:
            with _CACHE_LOCK:
                if _CACHED is None:
                    _CACHED = cls.from_config()
        return _CACHED


class GatedMcpTools:
    """Wrap `McpTools` so every MCP call passes the assistant's permission policy. An MCP tool is an
    arbitrary EXTERNAL side effect, so CompositeTools dispatching it with NO gate was a bypass
    (arch-review §3 P0-6): a `default`-mode session could fire an MCP mutation with no confirm-card.
    Here each call is treated as an UNKNOWN external effect — ASK in
    `default`/`acceptEdits`/**and `auto`** because MCP metadata is not yet trusted or typed well enough
    to prove a call read-only. Read tool definitions (specs) pass through unchanged; plan mode never
    even builds this wrapper (build_tools drops MCP there, so no stdio server is started in a
    read-only session)."""

    def __init__(self, inner: "McpTools", mode: str, approver=None):
        self._inner = inner
        self._mode = mode
        from looplab.tools.perm_modes import default_approver
        self._approver = approver or default_approver

    def specs(self) -> list[dict]:
        return self._inner.specs()

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def execute(self, name: str, args: dict) -> str:
        from looplab.tools.perm_modes import authorize
        try:
            args_json = json.dumps(args or {}, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            args_json = "<invalid arguments>"
        from looplab.core.redact import redact_secrets
        args_digest = hashlib.sha256(args_json.encode("utf-8")).hexdigest()
        action = {"tool": name, "tool_kind": "mcp", "label": f"MCP tool {name}",
                  "verb": f"call MCP tool `{name}`",
                  "preview": redact_secrets(args_json)[:2000], "cwd": "",
                  "scope": {"tool": name, "arguments_digest": args_digest}}
        refusal = authorize(
            self._mode, self._approver, action,
            denied=f"(MCP tool {name} is disabled in plan mode. Switch to default/acceptEdits/auto.)",
            declined=f"MCP tool {name}")
        if refusal:
            return refusal
        return self._inner.execute(name, args)


_CACHED: Optional["McpTools"] = None
_CACHE_LOCK = threading.Lock()
