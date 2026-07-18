"""UI-side settings + secret persistence for the server (BACKLOG §4 extraction — bodies verbatim
from `serve/server.py`). The engine has no settings server (ADR-18); these are UI-chosen DEFAULTS
for new runs, persisted at <run-root>/ui_settings.json and applied to a spawned run as LOOPLAB_*
env. One `SettingsStore` per app (per run-root), constructed by `make_app`."""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from looplab.core.atomicio import atomic_write_text
from looplab.core.config import Settings

_SECRET_FIELDS = {"llm_api_key"}
_ALLOWED_FIELDS = set(Settings.model_fields)

# Keep each resource's opaque CAS token in the SAME atomically-replaced JSON object as its data.
# A sidecar token could lag the data after a crash between two renames and let an old request pass
# CAS. These reserved keys are ignored by the allow-listed data loaders below. The secret token is
# random and independent of the credential -- it is not a value-derived hash or other verifier.
_REVISION_KEY = "__looplab_revision__"
_INITIAL_UI_REVISION = "gUoF2YQlVSLWCEg3hWJOCxJ2YFDKfZ2D"
_INITIAL_SECRET_REVISION = "p5SDQn8FLhx0O8vFbKbBzix42RdCUGyN"


class SettingsRevisionConflict(RuntimeError):
    """A settings resource moved after the caller read it."""

    def __init__(self, resource: str, expected: str, current: str):
        self.resource = resource
        self.expected = expected
        self.current = current
        super().__init__(f"stale {resource} revision")


def _new_revision() -> str:
    return secrets.token_urlsafe(24)

# --- Secret store (B3/ADR-11): secrets (the LLM API key) are NEVER written to ui_settings.json
# or a run's config.snapshot.json (which only ever record `***`). They live in a separate,
# owner-only file and are applied to this server process's env + every spawned engine via env —
# so the value transits as a credential, not as a persisted, reportable setting. The HTTP API
# only ever echoes the masked `***`, never the value.
# Derive the secret -> env-var map from the SAME _SECRET_FIELDS set the rest of the server uses to
# strip secrets, via the standard env_prefix convention (the LOOPLAB_{KEY} rule _settings_env also
# uses). A NEW SecretStr field is then covered by editing ONE place (_SECRET_FIELDS), not three.
_secret_prefix = Settings.model_config.get("env_prefix", "LOOPLAB_")
_SECRET_ENV = {k: f"{_secret_prefix}{k.upper()}" for k in _SECRET_FIELDS}   # UI key -> LOOPLAB_* env


class SettingsStore:
    """The per-run-root settings/secrets files + their read/write/render helpers. Method bodies are
    the former `make_app` closures, unchanged."""

    def __init__(self, root):
        self._ui_settings_path = root / "ui_settings.json"
        self._secrets_path = root / "secrets.json"
        # Atomic rename keeps one write whole, but a PUT is a larger read/merge/validate/write
        # transaction. Serialize that complete transaction within this server process too.
        self._ui_settings_lock = threading.Lock()
        self._secrets_lock = threading.Lock()

    @contextmanager
    def ui_settings_transaction(self):
        """Serialize one complete UI-settings read/modify/write transaction.

        The sibling file lock extends the local mutex guarantee to multiple server processes.
        Locking is required: a filesystem without advisory-lock support fails closed instead of
        silently reintroducing lost updates.
        """
        from looplab.events.eventstore import _interprocess_lock

        lock_path = Path(str(self._ui_settings_path) + ".lock")
        with self._ui_settings_lock, _interprocess_lock(lock_path, required=True):
            yield

    @contextmanager
    def secret_transaction(self):
        """Serialize one secret read/CAS/write transaction locally and across server processes."""
        from looplab.events.eventstore import _interprocess_lock

        lock_path = Path(str(self._secrets_path) + ".lock")
        with self._secrets_lock, _interprocess_lock(lock_path, required=True):
            yield

    @staticmethod
    def _stored_revision(path: Path, initial: str) -> str:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return initial
        if not isinstance(payload, dict):
            return initial
        revision = payload.get(_REVISION_KEY)
        if not isinstance(revision, str) or not revision or len(revision) > 256:
            return initial
        return revision

    def ui_settings_revision(self) -> str:
        """Return the opaque revision. Call under ``ui_settings_transaction`` for a CAS snapshot."""
        return self._stored_revision(self._ui_settings_path, _INITIAL_UI_REVISION)

    def secret_revision(self) -> str:
        """Return the opaque revision. Call under ``secret_transaction`` for a CAS snapshot."""
        return self._stored_revision(self._secrets_path, _INITIAL_SECRET_REVISION)

    def load_ui_settings(self) -> dict:
        try:
            d = json.loads(self._ui_settings_path.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                return {}
            return {k: v for k, v in d.items() if k in _ALLOWED_FIELDS and k not in _SECRET_FIELDS}
        except (OSError, json.JSONDecodeError):
            return {}

    def load_secrets(self) -> dict:
        try:
            d = json.loads(self._secrets_path.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                return {}
            return {k: v for k, v in d.items() if k in _SECRET_ENV and isinstance(v, str) and v}
        except (OSError, json.JSONDecodeError):
            return {}

    def store_secret(self, key: str, value: str, *, expected_revision: Optional[str] = None) -> str:
        """CAS-write one credential and return its new opaque revision.

        The interprocess lock covers revision read, comparison, credential merge, and atomic rename.
        Callers that omit ``expected_revision`` retain the legacy last-writer-wins behavior, but are
        still serialized so two processes cannot produce torn JSON or race a read/merge/write cycle.
        """
        with self.secret_transaction():
            current_revision = self.secret_revision()
            if expected_revision is not None and expected_revision != current_revision:
                raise SettingsRevisionConflict("secret", expected_revision, current_revision)

            d = self.load_secrets()
            if value:
                d[key] = value
            else:
                d.pop(key, None)
            revision = _new_revision()
            d[_REVISION_KEY] = revision
            # Write through a temp file that is owner-only FROM CREATION (mkstemp creates 0600), then
            # atomically rename. This closes the window where atomic_write_text + a later chmod would leave
            # the plaintext key world-readable at the default umask between the rename and the chmod.
            fd, tmp = tempfile.mkstemp(
                dir=str(self._secrets_path.parent), prefix=".secrets-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(d))
                os.replace(tmp, self._secrets_path)    # the 0600 mode rides along from the temp inode
            finally:
                try:
                    os.unlink(tmp)                # no-op once the rename consumed it; cleans up on write error
                except OSError:
                    pass
            try:                                  # belt-and-suspenders (no-op on Windows)
                os.chmod(self._secrets_path, 0o600)
            except OSError:
                pass
            env_name = _SECRET_ENV[key]           # live-apply: in-process LLM + future spawns see it now
            if value:
                os.environ[env_name] = value
            else:
                os.environ.pop(env_name, None)
            return revision

    def prime_env(self) -> None:
        # Prime this process's env from the stored secrets at startup, WITHOUT clobbering a value the
        # operator set explicitly — via an EXPORTED env var OR a `.env` file. Subtlety this fixes: a
        # `.env` value is NOT in os.environ yet here, so a bare setdefault would prime the secret into
        # os.environ and then WIN over `.env` (pydantic ranks os.environ above the .env file) — silently
        # overriding a key the operator just edited in `.env`. So we skip any key the local `.env`
        # already provides, keeping the documented ".env wins over the saved store" contract true.
        dotenv_keys: set = set()
        try:
            from dotenv import dotenv_values
            # UPPERCASE every key present (pydantic-settings matches env vars case-INsensitively, so a
            # lower/mixed-case .env key still feeds Settings) and keep keys with an EMPTY value too (an
            # explicit `KEY=` is the operator deliberately clearing it — that must still win over the
            # stored secret). Both were gaps that let the secret store override `.env`.
            dotenv_keys = {str(k).upper() for k in dotenv_values(".env")}
        except Exception:  # noqa: BLE001 — no/unreadable .env just means nothing to defer to
            dotenv_keys = set()
        for _k, _v in self.load_secrets().items():
            env_name = _SECRET_ENV[_k]
            if env_name.upper() in dotenv_keys:    # .env provides it → let .env win, don't prime
                continue
            os.environ.setdefault(env_name, _v)

    def resolved_settings(self, s: Optional["Settings"] = None) -> dict:
        """Engine defaults (Settings(): defaults+env) overlaid with the saved UI overrides — i.e.
        exactly what a new run gets if the launch dialog changes nothing. Secret masked. Pass an
        already-built Settings to avoid constructing one (and re-reading .env from disk) a 2nd time.

        Built by RE-RESOLVING `Settings(**overrides)` (not a shallow overlay) so a `profile` override
        is EXPANDED into its bundle here too — the UI then shows the real values `thorough` turns on,
        matching what the spawned run computes. Falls back to the shallow overlay if that ever raises."""
        overrides = self.load_ui_settings()
        try:
            return Settings(**overrides).masked_snapshot()
        except Exception:
            base = (s or Settings()).masked_snapshot()
            base.update(overrides)
            # Keep llm_api_key but ONLY as the mask masked_snapshot already applied ("***" when set,
            # else None) — the UI needs the set/unset state to render the secret field; value never leaks.
            return base

    def settings_env(self, settings: dict) -> dict:
        """Render UI settings into LOOPLAB_* env strings pydantic-settings can parse back."""
        env = {}
        for k, v in settings.items():
            if k not in _ALLOWED_FIELDS or k in _SECRET_FIELDS or v is None:
                continue
            if isinstance(v, bool):
                s = "true" if v else "false"
            elif isinstance(v, (list, dict)):
                s = json.dumps(v)            # pydantic reads complex env values as JSON
            else:
                s = str(v)
            env[f"LOOPLAB_{k.upper()}"] = s
        return env

    def write_ui_settings(self, overrides: dict) -> str:
        """Atomically publish overrides plus a fresh opaque CAS revision; caller holds UI lock."""
        revision = _new_revision()
        payload = dict(overrides)
        payload[_REVISION_KEY] = revision
        atomic_write_text(self._ui_settings_path, json.dumps(payload, indent=2))  # unique temp + safe fsync
        return revision
