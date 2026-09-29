"""Stdio MCP adapter over LoopLab's authenticated, durable HTTP API.

One API, one validation boundary: MCP never writes events or reads workspaces directly.
It forwards to the same routes and command service the UI uses. The external agent can
discover every current operation from OpenAPI, even when new endpoints are added later.
"""
from __future__ import annotations

import json
import os
from urllib.parse import unquote, urlsplit

import httpx

from looplab.harness.manifest import harness_manifest
from looplab.harness.phases import phase_catalog, phase_detail


MAX_RESPONSE_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 1024 * 1024


class HarnessAPI:
    def __init__(self, url: str, token: str = "", *, transport=None):
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("LOOPLAB_HARNESS_URL must be an HTTP(S) server URL without credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("LOOPLAB_HARNESS_URL cannot contain query or fragment")
        self.client = httpx.Client(base_url=url.rstrip("/") + "/", timeout=30,
                                   follow_redirects=False, trust_env=False, transport=transport,
                                   headers={"X-LoopLab-Token": token} if token else {})

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
        response = self.client.request(verb, self._path(path), json=body, headers=headers)
        return self._result(response)


def build_server(api: HarnessAPI):
    try:
        from mcp.server.mcpserver import MCPServer as FastMCP
    except ImportError:
        from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("looplab")

    @mcp.tool()
    def capabilities() -> dict:
        """Discover LoopLab phases, backend support, limits and control interfaces."""
        return harness_manifest()

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

    @mcp.tool()
    def api_request(method: str, path: str, body: dict | None = None,
                    idempotency_key: str = "") -> dict:
        """Call a live /api route using UI authorization and validation. Commands require
        a unique Idempotency-Key and the run's expected_generation in the JSON body;
        reuse the same key only for an exact lost-response retry. HTTP errors are returned
        with their status and body so the agent can handle stale state explicitly."""
        return api.request(method, path, body, idempotency_key)

    return mcp


def run_stdio(url: str | None = None, token: str | None = None) -> None:
    api = HarnessAPI(url or os.environ.get("LOOPLAB_HARNESS_URL", "http://127.0.0.1:8765"),
                     token if token is not None else (os.environ.get("LOOPLAB_HARNESS_TOKEN")
                                                     or os.environ.get("LOOPLAB_UI_TOKEN", "")))
    build_server(api).run(transport="stdio")
