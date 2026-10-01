"""Stdio MCP adapter over LoopLab's authenticated, durable HTTP API.

One API, one validation boundary: MCP never writes events or reads workspaces directly.
It forwards to the same routes and command service the UI uses. The external agent can
discover every current operation from OpenAPI, even when new endpoints are added later.
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import quote, unquote, urlsplit

import httpx

from looplab.harness.manifest import harness_manifest
from looplab.harness.phases import phase_catalog, phase_detail


MAX_RESPONSE_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
MCP_INSTRUCTIONS = (
    "LoopLab evaluates ready-made experiments; the external agent proposes candidates. "
    "Start with capabilities and connection_check, then read /state?observe_only=true, task, config and harness-contract. "
    "Call run_progress with the current generation; inspect source_health and checkpoints. "
    "Search phases and read phase_info before decisions. On reconnect, read command_receipt "
    "before retrying; preserve the exact payload and original key. Explicitly pause or finalize. "
    "Quiet logs do not prove agent or engine liveness. "
    "MCP Connected proves stdio only; use connection_check for live run reads. "
    "Client tool approval may still be required. Inspect isError/is_error, permission_denials and HTTP status; exit 0 is not an applied command receipt. "
    "Transport loss returns status=null; a write outcome is unknown. Inspect original receipts before any exact retry, never recover by inventing a new key. "
    "Follow enabled admission/finish obligations. A trainer exit is not terminal evaluation. "
    "After each terminal node and finalized run, read generation-fenced result-notices and POST "
    "a brief interpretation in the user's language with receipt_id, evidence_token and a stable "
    "action_id; retry a lost response with the exact body. Scores come from LoopLab, not prose. "
    "Commentary executes no actions and never replaces checkpoints or report obligations. "
    "Use only the scoped harness credential; owner-only workflows require the operator."
)


class HarnessAPI:
    def __init__(self, url: str, token: str = "", *, transport=None):
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise ValueError("LOOPLAB_HARNESS_URL must be a valid HTTP(S) server URL") from None
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("LOOPLAB_HARNESS_URL must be an HTTP(S) server URL without credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("LOOPLAB_HARNESS_URL cannot contain query or fragment")
        try:
            self.client = httpx.Client(base_url=url.rstrip("/") + "/", timeout=30,
                                      follow_redirects=False, trust_env=False, transport=transport,
                                      headers={"X-LoopLab-Token": token} if token else {})
        except httpx.InvalidURL:
            raise ValueError("LOOPLAB_HARNESS_URL must be a valid HTTP(S) server URL") from None

    @staticmethod
    def _path(path: str) -> str:
        parsed = urlsplit(path)
        decoded = unquote(parsed.path)
        if (parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/api/")
                or "\\" in decoded or "//" in decoded
                or any(part in (".", "..") for part in decoded.split("/"))):
            raise ValueError("path must be a relative /api/... route without traversal or fragment")
        return path.lstrip("/")

    def _result(self, response: httpx.Response) -> dict:
        if len(response.content) > MAX_RESPONSE_BYTES:
            return {"status": response.status_code, "truncated": True,
                    "bytes": len(response.content), "message": "Use a narrower API query."}
        try:
            body = response.json()
        except ValueError:
            body = response.text
        return {"status": response.status_code, "body": body}

    def operations(self, query: str = "", limit: int = 50) -> dict:
        response = self.client.get("openapi.json")
        response.raise_for_status()
        schema = response.json()
        matches = []
        needle = query.casefold().strip()
        for path, methods in (schema.get("paths") or {}).items():
            for method, spec in methods.items():
                if method not in ("get", "post", "put", "patch", "delete"):
                    continue
                row = {"method": method.upper(), "path": path,
                       "summary": spec.get("summary", ""), "operation_id": spec.get("operationId", "")}
                if needle and needle not in json.dumps(row, ensure_ascii=False).casefold():
                    continue
                matches.append(row)
        return {"matches": matches[:max(1, min(limit, 100))], "total": len(matches)}

    def schema(self, path: str) -> dict:
        route = "/" + self._path(path).split("?", 1)[0]
        response = self.client.get("openapi.json")
        response.raise_for_status()
        spec = response.json()
        operations = (spec.get("paths") or {}).get(route)
        definitions = spec.get("components", {}).get("schemas", {})
        needed: dict = {}

        def collect(value):
            if isinstance(value, list):
                for item in value:
                    collect(item)
            elif isinstance(value, dict):
                ref = value.get("$ref", "")
                prefix = "#/components/schemas/"
                if isinstance(ref, str) and ref.startswith(prefix):
                    name = ref[len(prefix):]
                    if name in definitions and name not in needed:
                        needed[name] = definitions[name]
                        collect(needed[name])
                for item in value.values():
                    collect(item)

        collect(operations)
        return {"path": route, "operations": operations, "components": needed}

    def request(self, method: str, path: str, body: dict | None = None,
                idempotency_key: str = "") -> dict:
        verb = method.upper()
        if verb not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise ValueError("unsupported HTTP method")
        if body is not None and (not isinstance(body, dict)
                                 or len(json.dumps(body).encode("utf-8")) > MAX_REQUEST_BYTES):
            raise ValueError("body must be a JSON object of at most 1 MiB")
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        route = self._path(path)
        try:
            response = self.client.request(verb, route, json=body, headers=headers)
        except httpx.TransportError:
            # Never echo transport exception text: it may contain sensitive URLs.
            # Even a timeout after sending a write can hide an accepted command.
            if verb == "GET":
                return {"status": None, "code": "api_unreachable", "outcome": "unavailable",
                        "message": "API read response unavailable. Check the UI/server and read again explicitly; missing evidence does not prove no action occurred."}
            return {"status": None, "code": "request_outcome_unknown", "outcome": "unknown",
                    "message": "API write response unavailable; the server may already have applied it. Read original receipts and current state/checkpoints before retrying. For commands, use command_receipt with the original Idempotency-Key and generation; preserve the exact body/key. For other actions preserve their original action_id and exact body. Do not create a new key or action_id to recover a lost response. No automatic retry was made."}
        return self._result(response)

    def run_progress(self, run_id: str, expected_generation: str) -> dict:
        self._run_identity(run_id, expected_generation)
        return self.request("GET", f"/api/runs/{quote(run_id, safe='')}/harness-progress"
                            f"?expected_generation={expected_generation}&brief=true")

    def connection_check(self, run_id: str, expected_generation: str = "") -> dict:
        """Explicit, read-only bootstrap check; never resumes a worker or probes a model.

        Successful MCP initialization alone does not test this API or credential. The
        state read discovers the generation; handoff/progress fence subsequent reads.
        An operator-supplied generation must match before reading the new incarnation.
        Errors use fixed messages rather than HTTP exception text, URLs or raw bodies.
        Success proves these reads, not agent liveness, credential scope or admission.
        """
        self._run_identity(run_id, expected_generation or "0" * 64)
        base = f"/api/runs/{quote(run_id, safe='')}"

        def read(suffix, phase):
            try:
                result = self.request("GET", base + suffix)
            except (httpx.HTTPError, httpx.InvalidURL):
                return {"ok": False, "status": None, "code": "api_unreachable", "at": phase,
                        "message": "UI/API request failed. Check the server URL, network and running UI."}
            status = result["status"]
            if status != 200:
                code, message = {
                    None: ("api_unreachable", "UI/API request failed. Check the server URL, network and running UI."),
                    401: ("credential_refused", "API rejected the credential. Ask the operator for the scoped token."),
                    403: ("access_refused", "API refused this read. Check the credential and launched run mode."),
                    404: ("run_not_found", "Run not found on this server. Check the URL, run root and literal run ID."),
                    409: ("run_context_changed", "Run context changed or is unavailable. Read state and request fresh handoff."),
                    503: ("source_unavailable", "Run sources are unavailable. Inspect source health before acting."),
                }.get(status, ("api_read_failed", "API read failed. Inspect the server before acting."))
                return {"ok": False, "status": status, "code": code, "at": phase, "message": message}
            if result.get("truncated") or not isinstance(result.get("body"), dict):
                return {"ok": False, "status": status, "code": "invalid_response", "at": phase,
                        "message": "API returned incomplete connection evidence. Do not act on this check."}
            return {"ok": True, "body": result["body"]}

        state = read("/state?observe_only=true", "state")
        if state.get("ok") is False:
            return state
        state = state["body"]
        generation = state.get("generation")
        if not isinstance(generation, str) or re.fullmatch(r"[a-fA-F0-9]{64}", generation) is None:
            return {"ok": False, "status": 200, "code": "invalid_response", "at": "state",
                    "message": "State did not supply a valid generation. Request fresh connection context."}
        if expected_generation and expected_generation != generation:
            return {"ok": False, "status": 409, "code": "generation_mismatch", "at": "state",
                    "message": "This handoff belongs to another run generation. Request fresh context."}
        handoff = read(f"/harness-handoff?expected_generation={generation}", "handoff")
        if handoff.get("ok") is False:
            return handoff
        handoff = handoff["body"]
        if handoff.get("credential_configured") is False:
            return {"ok": False, "status": 200, "code": "harness_credential_missing", "at": "handoff",
                    "message": "This server has no harness credential configured. Ask the operator to configure it and restart the UI."}
        progress = read(f"/harness-progress?expected_generation={generation}&brief=true", "progress")
        if progress.get("ok") is False:
            return progress
        progress = progress["body"]
        paths = handoff.get("server_paths")
        if (handoff.get("run_id") != run_id or handoff.get("generation") != generation
                or handoff.get("mode") != "external_harness" or progress.get("generation") != generation
                or handoff.get("credential_configured") is not True
                or not isinstance(progress.get("source_health"), dict)
                or not isinstance(progress.get("next_step"), dict)
                or type(progress.get("complete")) is not bool
                or not isinstance(paths, dict)
                or any(not isinstance(paths.get(key), str) or not paths[key]
                       for key in ("run_root", "run_dir"))
                or (handoff.get("engine_running") is not None and type(handoff["engine_running"]) is not bool)):
            return {"ok": False, "status": 200, "code": "invalid_response", "at": "context",
                    "message": "Connection evidence does not match this external run. Request fresh context."}
        return {"ok": True, "status": 200, "code": "run_reads_succeeded", "run_id": run_id,
                "generation": generation, "server_paths": {key: paths[key] for key in ("run_root", "run_dir")},
                "engine_running": handoff.get("engine_running"), "agent_connection": "not_measured",
                "evidence_complete": progress["complete"],
                "source_health": progress.get("source_health"), "next_step": progress.get("next_step"),
                "message": "Live run reads succeeded. Check source health, obligations and engine status before decisions; this check starts no work."}

    @staticmethod
    def _run_identity(run_id: str, expected_generation: str):
        if (not run_id or run_id in (".", "..")
                or any(char in run_id for char in "/\\")):
            raise ValueError("run_id must be one literal run identifier")
        if re.fullmatch(r"[a-fA-F0-9]{64}", expected_generation) is None:
            raise ValueError("expected_generation must be the SHA-256 token from /state")

    def command_receipt(self, run_id: str, expected_generation: str,
                        command_id: str = "", idempotency_key: str = "") -> dict:
        self._run_identity(run_id, expected_generation)
        if bool(command_id) == bool(idempotency_key):
            raise ValueError("Supply exactly one command_id or idempotency_key")
        if command_id and re.fullmatch(r"cmd_[0-9a-f]{32}", command_id) is None:
            raise ValueError("Invalid durable command ID")
        if idempotency_key and (len(idempotency_key) > 512
                                or any(ord(ch) < 32 or ord(ch) == 127 for ch in idempotency_key)):
            raise ValueError("Invalid idempotency key")
        path = f"/api/runs/{quote(run_id, safe='')}/command-receipt?expected_generation={expected_generation}"
        if command_id:
            path += f"&command_id={command_id}"
        return self.request("GET", path, idempotency_key=idempotency_key)


def build_server(api: HarnessAPI):
    try:
        from mcp.server.mcpserver import MCPServer as FastMCP
    except ImportError:
        from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    mcp = FastMCP("looplab", instructions=MCP_INSTRUCTIONS)

    @mcp.tool()
    def capabilities() -> dict:
        """Discover LoopLab phases, backend support, limits and control interfaces."""
        return harness_manifest()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True,
                                         destructiveHint=False, openWorldHint=True))
    def connection_check(run_id: str, expected_generation: str = "") -> dict:
        """Check UI/API access to one launched external run, beyond MCP Connected status.

        Performs GET state, generation-fenced handoff and compact progress only. Pass
        the copied handoff generation to refuse a replaced run; otherwise discover it.
        Reports fixed diagnostics for refused access, missing run, stale context or
        unavailable API. Does not resume, submit, probe a model or certify agent liveness.
        """
        return api.connection_check(run_id, expected_generation)

    @mcp.tool()
    def phases(query: str = "") -> list[dict]:
        """Find standard/external decision phases by name, entity or purpose.
        Each phase names the built-in owner and durable read/write surfaces.
        write_access marks owner-only setup actions. A command:TYPE write goes through the
        generation-fenced /api/runs/{run_id}/commands endpoint."""
        return phase_catalog(query)

    @mcp.tool()
    def phase_info(phase_id: str) -> dict:
        """Read a phase's entity, evidence, output actions and command field contracts."""
        phase = phase_detail(phase_id)
        if phase is None:
            return {"error": "unknown phase", "phase_id": phase_id}
        from looplab.serve.control_validation import (CONTROL_DATA_FIELDS,
                                                      CONTROL_SERVER_DERIVED_FIELDS)
        phase["commands"] = {
            name: {"request_fields": sorted(CONTROL_DATA_FIELDS[name]),
                   "server_derived": sorted(CONTROL_SERVER_DERIVED_FIELDS.get(name, ())) }
            for ref in phase["writes"] if ref.startswith("command:")
            for name in (ref.removeprefix("command:"),)
        }
        if phase_id in ("research", "proposal", "novelty", "implementation"):
            from looplab.core.models import Idea, ResearchMemo
            phase["entity_schema"] = (ResearchMemo if phase_id == "research"
                                      else Idea).model_json_schema()
        return phase

    @mcp.tool()
    def settings_keys(query: str = "") -> dict:
        """Search all LoopLab Settings names, including advanced settings absent from the UI form."""
        from looplab.core.config import Settings
        names = sorted(name for name in Settings.model_fields
                       if query.casefold() in name.casefold())
        return {"matches": names[:100], "total": len(names)}

    @mcp.tool()
    def setting_info(name: str) -> dict:
        """Read one setting's actual JSON Schema, default and curated operator help."""
        manifest = harness_manifest(include_settings=True)
        schema = manifest["settings_schema"]
        field = schema.get("properties", {}).get(name)
        if field is None:
            return {"error": "unknown setting", "name": name}
        definitions = schema.get("$defs", {})
        needed = {}

        def collect(value):
            if isinstance(value, list):
                for item in value:
                    collect(item)
            elif isinstance(value, dict):
                ref = value.get("$ref", "")
                prefix = "#/$defs/"
                if isinstance(ref, str) and ref.startswith(prefix):
                    key = ref[len(prefix):]
                    if key in definitions and key not in needed:
                        needed[key] = definitions[key]
                        collect(needed[key])
                for item in value.values():
                    collect(item)

        collect(field)
        curated = next((row for group in manifest["settings_help"].get("groups", [])
                        for row in group.get("fields", []) if row.get("key") == name), None)
        return {"name": name, "schema": field, "definitions": needed,
                "operator_help": curated}

    @mcp.tool()
    def operations(query: str = "", limit: int = 50) -> dict:
        """Search the live LoopLab OpenAPI catalog for reads, settings and controls."""
        return api.operations(query, limit)

    @mcp.tool()
    def operation_schema(path: str) -> dict:
        """Get a route's input/output schema before calling it."""
        return api.schema(path)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                        idempotentHint=True, openWorldHint=False))
    def run_progress(run_id: str, expected_generation: str) -> dict:
        """Read the compact next step, continue/finish gates and source health.
        execution separates recorded node activity from the last-read engine lock
        probe; agent connection is unmeasured. Refresh even without new events.
        Use the current generation from /state. Read detail references and phase_info
        before deciding; refresh after events or answers. This performs one GET only,
        returns HTTP failures unchanged, and never retries or submits a candidate."""
        return api.run_progress(run_id, expected_generation)

    @mcp.tool()
    def api_request(method: str, path: str, body: dict | None = None,
                    idempotency_key: str = "") -> dict:
        """Call a live /api route using UI authorization and validation. Commands require
        a unique Idempotency-Key and the run's expected_generation in the JSON body;
        reuse the same key only for an exact lost-response retry. HTTP errors are returned
        with their status and body so the agent can handle stale state explicitly.
        Transport loss returns status=null: a write has outcome=unknown, not failed.
        Inspect saved receipts before an exact retry; this tool never retries itself."""
        return api.request(method, path, body, idempotency_key)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                        idempotentHint=True, openWorldHint=False))
    def command_receipt(run_id: str, expected_generation: str,
                        command_id: str = "", idempotency_key: str = "") -> dict:
        """Observe a saved command after disconnect or a lost POST response.
        Supply exactly one known command_id or the ORIGINAL Idempotency-Key.
        This performs one generation-fenced GET without reconciliation, worker
        restart, retry or resume. A missing/stale receipt does not prove no action
        occurred; read state, events and checkpoints before choosing recovery.
        """
        return api.command_receipt(run_id, expected_generation, command_id, idempotency_key)

    return mcp


def run_stdio(url: str | None = None, token: str | None = None) -> None:
    credential = token if token is not None else os.environ.get("LOOPLAB_HARNESS_TOKEN", "")
    if not credential or not credential.strip() or credential == "${LOOPLAB_HARNESS_TOKEN}":
        raise ValueError("Set LOOPLAB_HARNESS_TOKEN in the MCP process environment. "
                         "LOOPLAB_UI_TOKEN is never used by harness-mcp; ask the operator "
                         "for a distinct scoped credential.")
    if any(ord(char) < 32 or ord(char) > 126 for char in credential):
        raise ValueError("LOOPLAB_HARNESS_TOKEN must be printable ASCII without control characters.")
    if credential == os.environ.get("LOOPLAB_UI_TOKEN", ""):
        raise ValueError("The harness credential must differ from LOOPLAB_UI_TOKEN.")
    api = HarnessAPI(url or os.environ.get("LOOPLAB_HARNESS_URL", "http://127.0.0.1:8765"),
                     credential)
    try:
        build_server(api).run(transport="stdio")
    finally:
        api.client.close()
