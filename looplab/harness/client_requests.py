"""Immutable client-side request bodies, saved before HTTP, never a server verdict.

The stdio client owns these files on the client machine. Credentials are not part
of the envelope. Reads neither contact the server nor retry/resume anything.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import quote, urlsplit

from looplab.core.atomicio import strict_atomic_write_bytes
from looplab.core.jsonutil import DuplicateJSONKey, JSONTooDeep, canonical_json, strict_json_loads
from looplab.core.redact import secret_env_values
from looplab.core.node_evidence import read_bounded_regular_file
from looplab.events.eventstore import interprocess_lock

MAX_RECORD_BYTES = 1100 * 1024
MAX_RECORDS = 2000
# The server accepts `^[0-9a-fA-F]{64}$` and compares `.lower()`; the client stores the
# lower-case spelling so both spellings of one generation share one directory.
_GENERATION = r"[0-9a-fA-F]{64}"
# Routes whose server scopes `action_id` NARROWER than (route, run): the identity must carry
# the scoping body field too, or the client refuses a reuse the server accepts.
# `harness/checkpoints.py::respond` keys an answer on `checkpoint_id` alone (its `action_id` is a
# recorded field), so "answer" may be reused across checkpoints. Every other route keys on
# (run, action_id) or wider (upstream's one namespace), so its identity is unchanged and records
# already saved still read back.
ACTION_SCOPE_FIELDS = {"harness-checkpoints": ("checkpoint_id",)}
ACTION_ROUTES = frozenset({
    "harness-decisions", "harness-reviews", "harness-checkpoints", "harness-hypotheses",
    "harness-selection/verify", "harness-selection/values", "lessons", "skill-candidates",
    "result-notices", "upstream/proposals", "upstream/check", "upstream/advance", "upstream/recover",
})


# The persisted identity bytes (directory names, `request_sha256`, `act_` ids) ARE
# `core/jsonutil.py::canonical_json`: same four options, UTF-8. Only the refusal changed shape —
# an unencodable value is a `ValueError` (it could be a raw `TypeError` / `RecursionError`), which
# every caller already contains as not_sent / a damaged record.
_bytes = canonical_json


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _normal(path, *, directory=False):
    info = path.lstat()
    if ((not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode))
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise ValueError("client request source is not a regular directory/file")


class ClientRequestRefused(ValueError):
    """The AUTHORED request was refused before saving; storage itself did not fail.

    A `ValueError` so every existing handler still contains it as not_sent; `code` and
    `message` are fixed strings (never the caller's text) so `HarnessAPI.request` can say
    what happened instead of reporting a durability failure."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _generation(value):
    if not isinstance(value, str) or not re.fullmatch(_GENERATION, value):
        raise ValueError("invalid client request run/generation")
    return value.lower()


def _bounded_id(value, what):
    # One spelling for every authored ID that enters an identity hash.
    if (not isinstance(value, str) or not value or len(value) > 160
            or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ClientRequestRefused("client_request_invalid", f"The request has no valid {what}; "
                                   "nothing was saved and no HTTP request was made.")


class ClientRequests:
    identity_field = "command_id"
    identity_pattern = r"cmd_[0-9a-f]{32}"

    def __init__(self, root, server_url):
        # Resolve an operator-selected root once; subordinate entries cannot be links.
        self.root = Path(root).expanduser().resolve()
        self.server = server_url.rstrip("/") + "/"
        parsed = urlsplit(self.server)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("client request server must be a credential-free HTTP(S) URL")
        self.scope = self.root / _hash(self.server.encode())

    def _directory(self, run_id, generation):
        if (not isinstance(run_id, str) or not run_id or run_id in (".", "..")
                or any(c in run_id for c in "/\\")):
            raise ValueError("invalid client request run/generation")
        return self.scope / _hash(_bytes([run_id, _generation(generation)]))

    def _check_directory(self, directory, *, create=False):
        for path in (self.root, self.scope, directory):
            if create and not path.exists() and not path.is_symlink():
                # Let the strict publisher create/sync missing ancestors as well.
                # Plain mkdir before that would lose the Windows durability receipt.
                strict_atomic_write_bytes(path / ".directory", b"LoopLab client requests v1\n")
                if os.name != "nt":
                    path.chmod(0o700)
            _normal(path, directory=True)

    def _read(self, path, run_id, generation):
        raw = read_bounded_regular_file(path, MAX_RECORD_BYTES)
        if raw is None:
            raise ValueError("client request source unavailable")
        try:
            # `core/jsonutil.py::strict_json_loads`: bytes as before (its encoding detection kept),
            # member names unique at every depth, and now no bare NaN/Infinity either — `_bytes`
            # never writes one, so a record carrying one is damage.
            row = strict_json_loads(raw)
            expected = {"version", "server", "run_id", "generation", self.identity_field,
                        "request_sha256", "request"}
            request = row.get("request") if isinstance(row, dict) else None
            if (not isinstance(row, dict) or set(row) != expected
                    or type(row["version"]) is not int or row["version"] != 1
                    or row["server"] != self.server or row["run_id"] != run_id
                    or row["generation"] != generation or not isinstance(request, dict)
                    or set(request) != {"method", "path", "body", "idempotency_key"}
                    or request["method"] != "POST"
                    or not isinstance(request["body"], dict)
                    or not isinstance(request["body"].get("expected_generation"), str)
                    or request["body"]["expected_generation"].lower() != generation
                    or row[self.identity_field] not in self._read_identities(request, run_id)
                    or path.name != row[self.identity_field] + ".json"
                    or row["request_sha256"] != _hash(_bytes(request))):
                raise ValueError("client request identity/content invalid")
            return row
        except ClientRequestRefused as exc:
            # A SAVED record failing the authoring rules is damage, not the caller's request:
            # never let it read as "your request has no valid action_id".
            raise ValueError("client request identity/content invalid") from exc
        except DuplicateJSONKey as exc:
            raise ValueError("duplicate client request field") from exc
        except (TypeError, RecursionError, JSONTooDeep) as exc:
            raise ValueError("client request structure invalid") from exc

    @staticmethod
    def _key(key, *, required=True):
        if (not isinstance(key, str) or (required and not key) or len(key) > 512
                or any(ord(c) < 32 or ord(c) > 126 for c in key)):
            raise ClientRequestRefused("client_request_invalid",
                ("A command POST needs a non-empty Idempotency-Key" if required and not key
                 else "The Idempotency-Key must be printable ASCII of at most 512 characters")
                + "; nothing was saved and no HTTP request was made.")

    def _read_identities(self, request, run_id):
        """The identities a SAVED record may carry: today's, plus any earlier derivation records
        on disk still use (a store is never rewritten in place)."""
        return {self._identity(request, run_id)}

    def _identity(self, request, run_id):
        self._key(request["idempotency_key"])
        if request["path"] != f"/api/runs/{quote(run_id, safe='')}/commands":
            raise ValueError("invalid command path")
        # Deferred: `harness` reaches `serve` only inside a call (tests/test_package_layering.py).
        from looplab.serve.command_identity import command_identity
        return command_identity(request["idempotency_key"])[0]

    def save(self, run_id, generation, body, key, *, credential=""):
        request = {"method": "POST", "path": f"/api/runs/{quote(run_id, safe='')}/commands",
                   "body": body, "idempotency_key": key}
        return self._save_request(run_id, generation, request, credential=credential)

    def _save_request(self, run_id, generation, request, *, credential=""):
        directory = self._directory(run_id, generation)
        generation = _generation(generation)
        body = request["body"]
        if (not isinstance(body, dict) or not isinstance(body.get("expected_generation"), str)
                or body["expected_generation"].lower() != generation):
            raise ValueError("client request body generation invalid")
        request = json.loads(_bytes(request))  # Frozen, independent of the caller's mutable dict.
        identity = self._identity(request, run_id)
        row = {"version": 1, "server": self.server, "run_id": run_id,
               "generation": generation, self.identity_field: identity,
               "request_sha256": _hash(_bytes(request)), "request": request}
        raw = _bytes(row)
        if len(raw) > MAX_RECORD_BYTES:
            raise ValueError("client request exceeds storage limit")
        # Only a credential the redactor would treat as one is screened: the SAME public rule
        # (`core/redact.py::secret_env_values`, its value floor and `is_secret_env`) every write
        # boundary masks by. A short token such as "agent" is a substring of ordinary bodies,
        # and refusing on it made those requests permanently not_sent.
        screened = bool(credential) and bool(secret_env_values({"LOOPLAB_HARNESS_TOKEN": credential}))
        if screened and json.dumps(credential, ensure_ascii=False)[1:-1].encode() in raw:
            raise ClientRequestRefused("client_request_contains_credential",
                "The request body contains the transport credential, which is never saved to "
                "disk; remove it from the body. Nothing was saved and no HTTP request was made.")
        self._check_directory(directory, create=True)
        lock = directory / ".lock"
        if lock.exists() or lock.is_symlink():
            _normal(lock)
        with interprocess_lock(lock, required=True, blocking=False):
            path = directory / (identity + ".json")
            if path.exists() or path.is_symlink():
                old = self._read(path, run_id, generation)
                if old != row:
                    raise ClientRequestRefused("client_request_conflict",
                        "A different request body is already saved under this identity (the "
                        "Idempotency-Key, or the action_id in its server scope). Nothing was "
                        "saved or sent: read saved_commands/saved_actions and resend the original "
                        "exact body, or author a new key/action_id for a new intent.")
            else:
                if len(self._paths(directory)) >= MAX_RECORDS:
                    raise ValueError("client request store is full")
            # Reconfirm exact retries too: a prior strict sync may have failed AFTER replace.
            strict_atomic_write_bytes(path, raw)
        return row

    def _paths(self, directory):
        paths = []
        for path in directory.iterdir():
            if re.fullmatch(self.identity_pattern + r"\.json", path.name):
                paths.append(path)
                if len(paths) > MAX_RECORDS:
                    raise ValueError("client request store exceeds listing limit")
        return sorted(paths)

    def listing(self, run_id, generation, *, offset=0, limit=20):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid client request page")
        directory = self._directory(run_id, generation)
        generation = _generation(generation)
        exists = True
        try:
            self._check_directory(directory)
        except FileNotFoundError:
            exists = False
            paths = []
        else:
            paths = self._paths(directory)
        rows = [self._read(p, run_id, generation) for p in paths[offset:offset + limit]]
        end = min(len(paths), offset + limit)
        return {"version": 1, "source": "client", "server": self.server,
                "store_exists": exists,
                "generation": generation, "run_id": run_id, "total": len(paths),
                "offset": offset, "next_offset": end if end < len(paths) else None,
                "items": [self._summary(r) for r in rows]}

    def _summary(self, row):
        event_type = row["request"]["body"].get("type")
        return {self.identity_field: row[self.identity_field], "request_sha256": row["request_sha256"],
                "event_type": event_type if isinstance(event_type, str) and len(event_type) <= 128 else None,
                "server_effects": "unobserved"}

    def page(self, run_id, generation, command_id, *, offset=0, limit=2048,
             expected_request_hash=None):
        if (not isinstance(command_id, str) or not re.fullmatch(self.identity_pattern, command_id)
                or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= 16384
                or (offset > 0 and expected_request_hash is None)):
            raise ValueError("invalid client request page identity")
        directory = self._directory(run_id, generation)
        generation = _generation(generation)
        self._check_directory(directory)
        row = self._read(directory / (command_id + ".json"), run_id, generation)
        if expected_request_hash is not None and expected_request_hash != row["request_sha256"]:
            raise ValueError("client request content changed")
        raw = _bytes(row["request"])
        if offset > len(raw):
            raise ValueError("client request page outside content")
        chunk = raw[offset:offset + limit]
        end = offset + len(chunk)
        return {"version": 1, "source": "client", "server": self.server,
                "run_id": run_id, "generation": generation, self.identity_field: command_id,
                "request_sha256": row["request_sha256"], "server_effects": "unobserved",
                "total_bytes": len(raw), "offset": offset, "chunk": base64.b64encode(chunk).decode(),
                "chunk_sha256": _hash(chunk), "next_offset": end if end < len(raw) else None}


class ClientActions(ClientRequests):
    """Generation-bound semantic writes; action IDs and evidence remain exactly authored."""

    identity_field = "request_id"
    identity_pattern = r"act_[0-9a-f]{64}"

    def _identity(self, request, run_id):
        self._key(request["idempotency_key"], required=False)
        prefix = f"/api/runs/{quote(run_id, safe='')}/"
        path = request["path"]
        if not isinstance(path, str) or not path.startswith(prefix) or path[len(prefix):] not in ACTION_ROUTES:
            raise ValueError("invalid action path")
        action_id = request["body"].get("action_id")
        _bounded_id(action_id, "action_id")
        suffix = path[len(prefix):]
        # Upstream uses one server-side action namespace across all operations.
        namespace = "upstream" if suffix.startswith("upstream/") else suffix
        scope = ACTION_SCOPE_FIELDS.get(suffix, ())
        for field in scope:
            _bounded_id(request["body"].get(field), field)
        # An unscoped route keeps its original two-item identity byte for byte.
        return "act_" + _hash(_bytes([namespace, action_id,
                                      *(request["body"][field] for field in scope)]))

    def _read_identities(self, request, run_id):
        identities = super()._read_identities(request, run_id)
        prefix = f"/api/runs/{quote(run_id, safe='')}/"
        suffix = request["path"][len(prefix):]
        if ACTION_SCOPE_FIELDS.get(suffix):
            # A checkpoint answer saved before its scope joined the identity (2026-10-05..07)
            # carries the two-item one. Refusing it made `listing` fail for EVERY saved action of
            # that generation, and the record was never superseded (a new save lands elsewhere).
            namespace = "upstream" if suffix.startswith("upstream/") else suffix
            identities.add("act_" + _hash(_bytes([namespace, request["body"]["action_id"]])))
        return identities

    def save_action(self, run_id, generation, path, body, key="", *, credential=""):
        return self._save_request(run_id, generation,
                                  {"method": "POST", "path": path, "body": body, "idempotency_key": key},
                                  credential=credential)

    def _summary(self, row):
        return {"request_id": row["request_id"], "request_sha256": row["request_sha256"],
                "path": row["request"]["path"], "action_id": row["request"]["body"]["action_id"],
                "server_effects": "unobserved"}
