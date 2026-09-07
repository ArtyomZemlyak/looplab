"""UI settings and source-aware credential persistence for the owner server."""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from pydantic import SecretStr

from looplab.core.atomicio import atomic_write_text, best_effort_fsync
from looplab.core.config import Settings

# Both fields are runtime-only and must never enter UI settings or run snapshots. Only the key is an
# HTTP-writeable secret; the server derives and persists its endpoint binding in the same JSON file.
_SECRET_FIELDS = {"llm_api_key", "llm_api_key_base_url"}
_SECRET_API_FIELDS = {"llm_api_key"}
_ALLOWED_FIELDS = set(Settings.model_fields)

_REVISION_KEY = "__looplab_revision__"
_INITIAL_UI_REVISION = "gUoF2YQlVSLWCEg3hWJOCxJ2YFDKfZ2D"
_INITIAL_SECRET_REVISION = "p5SDQn8FLhx0O8vFbKbBzix42RdCUGyN"

_secret_prefix = Settings.model_config.get("env_prefix", "LOOPLAB_")
_SECRET_ENV = {field: f"{_secret_prefix}{field.upper()}" for field in _SECRET_FIELDS}
_SECRET_ENV_LOCK = threading.RLock()


class SettingsRevisionConflict(RuntimeError):
    """A settings resource moved after the caller read it."""

    def __init__(self, resource: str, expected: str, current: str):
        self.resource = resource
        self.expected = expected
        self.current = current
        super().__init__(f"stale {resource} revision")


def _new_revision() -> str:
    return secrets.token_urlsafe(24)


class SettingsStore:
    """Per-run-root settings and owner-only key+endpoint-binding storage."""

    def __init__(self, root):
        self._ui_settings_path = root / "ui_settings.json"
        self._secrets_path = root / "secrets.json"
        self._ui_settings_lock = threading.Lock()
        self._secrets_lock = threading.Lock()
        self._launch_lock_path = root / ".settings-launch.lock"
        self._launch_lock = threading.Lock()

    @contextmanager
    def ui_settings_transaction(self):
        """Serialize one UI settings transaction locally and across server workers."""
        from looplab.events.eventstore import _interprocess_lock

        lock_path = Path(str(self._ui_settings_path) + ".lock")
        with self._ui_settings_lock, _interprocess_lock(lock_path, required=True):
            yield

    @contextmanager
    def secret_transaction(self):
        """Serialize one credential transaction locally and across server workers."""
        from looplab.events.eventstore import _interprocess_lock

        lock_path = Path(str(self._secrets_path) + ".lock")
        with self._secrets_lock, _interprocess_lock(lock_path, required=True):
            yield

    @contextmanager
    def launch_transaction(self):
        """Fence settings publication against the credential snapshot-to-Popen boundary."""
        from looplab.events.eventstore import _interprocess_lock

        with self._launch_lock, _interprocess_lock(self._launch_lock_path, required=True):
            yield

    @contextmanager
    def settings_write_transaction(self):
        """Serialize a UI/credential publication against every fenced child launch.

        This is the sole mutation order: UI -> secret -> launch. A writer may wait for a short
        Popen while it owns UI/secret, but a launched child can never receive a credential that a
        completed clear/rotation already revoked.
        """
        with (self.ui_settings_transaction(), self.secret_transaction(),
              self.launch_transaction()):
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
        return self._stored_revision(self._ui_settings_path, _INITIAL_UI_REVISION)

    def secret_revision(self) -> str:
        return self._stored_revision(self._secrets_path, _INITIAL_SECRET_REVISION)

    def load_ui_settings(self) -> dict:
        try:
            payload = json.loads(self._ui_settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {key: value for key, value in payload.items()
                if key in _ALLOWED_FIELDS and key not in _SECRET_FIELDS}

    def load_secrets(self) -> dict:
        """Read one atomically-published pair. A legacy key has no binding and stays unusable."""
        try:
            payload = json.loads(self._secrets_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {key: value for key, value in payload.items()
                if key in _SECRET_FIELDS and isinstance(value, str) and value}

    @staticmethod
    def _dotenv_values() -> dict[str, Optional[str]]:
        try:
            from dotenv import dotenv_values
            return {str(key).upper(): (None if value is None else str(value))
                    for key, value in dotenv_values(".env").items()}
        except Exception:  # noqa: BLE001 - absent/unreadable .env is simply not a source
            return {}

    @staticmethod
    def _process_secret_values() -> tuple[dict[str, str], dict[str, str]]:
        """Select one whole operator tier: process env pair, else dotenv pair."""
        process = {str(key).upper(): str(value) for key, value in os.environ.items()}
        dotenv = SettingsStore._dotenv_values()
        values: dict[str, str] = {}
        sources: dict[str, str] = {}
        names = {field: env_name.upper() for field, env_name in _SECRET_ENV.items()}
        if any(name in process for name in names.values()):
            for field, name in names.items():
                values[field] = process.get(name, "")
                sources[field] = "environment"
        elif any(name in dotenv for name in names.values()):
            for field, name in names.items():
                values[field] = dotenv.get(name) or ""
                sources[field] = "dotenv"
        return values, sources

    def _effective_secret_values(self) -> tuple[dict[str, str], str, dict[str, str]]:
        """Resolve a whole pair; any operator-owned component excludes the stored pair."""
        ambient, sources = self._process_secret_values()
        stored = self.load_secrets()
        if sources:
            source = sources.get("llm_api_key") or sources.get("llm_api_key_base_url") or "none"
            return ambient, source, stored
        return stored, ("stored" if any(stored.get(field) for field in _SECRET_FIELDS)
                        else "none"), stored

    @staticmethod
    def _with_secret_pair(settings: Settings, values: dict[str, str]) -> Settings:
        """Apply one already-snapshotted credential pair to resolved ordinary settings."""
        resolved = settings.model_copy(update={
            "llm_api_key": (SecretStr(values["llm_api_key"])
                            if values.get("llm_api_key") else None),
            "llm_api_key_base_url": values.get("llm_api_key_base_url") or None,
        })
        resolved._llm_credential_pair_trusted = True
        return resolved

    def _resolve_bundle(self, overrides: Optional[dict] = None) -> tuple[Settings, dict]:
        clean = {key: value for key, value in (overrides or {}).items()
                 if key in _ALLOWED_FIELDS and key not in _SECRET_FIELDS and value is not None}
        with _SECRET_ENV_LOCK:
            values, source, stored_values = self._effective_secret_values()
            # Pydantic validates all ordinary Settings sources. Replace both credential components
            # from our one source snapshot so env/.env cannot be combined with the stored pair.
            settings = self._with_secret_pair(Settings(**clean), values)
            key = values.get("llm_api_key") or ""
            binding = values.get("llm_api_key_base_url") or ""
            stored = any(stored_values.get(field) for field in _SECRET_FIELDS)
            effective = bool(key)
            active = False
            status = "missing"
            if binding and not effective:
                status = "incomplete"
            elif effective and not binding:
                status = "unbound"
            elif effective:
                try:
                    from looplab.core.llm import normalize_llm_base_url
                    active = (normalize_llm_base_url(binding)
                              == normalize_llm_base_url(settings.llm_base_url))
                    status = ("ambient_override" if active and source in {"environment", "dotenv"}
                              and stored else
                              "active" if active else "endpoint_mismatch")
                except Exception:  # noqa: BLE001 - never expose parser details or either URL
                    status = "unbound"
            credential = {
                "source": source,
                "stored": stored,
                "effective": effective,
                "active": active,
                "clearable": stored,
                "status": status,
            }
            return settings, credential

    def resolve_settings(self, overrides: Optional[dict] = None) -> Settings:
        """Typed settings with one source-aware key+binding pair applied explicitly."""
        return self._resolve_bundle(overrides)[0]

    def resolve_snapshot_settings(self, snapshot: dict) -> Settings:
        """Resolve a complete historical run snapshot with only today's credential pair.

        Profile and role routing are part of the run contract, so they must come from the snapshot
        as a whole rather than being overlaid onto current UI defaults. Runtime credentials are the
        sole exception: snapshots intentionally omit them and revocation/rotation must take effect
        immediately for owner-side chat/report calls.
        """
        from looplab.core.config import settings_from_snapshot

        resolved = settings_from_snapshot(snapshot)
        with _SECRET_ENV_LOCK:
            values, _source, _stored = self._effective_secret_values()
            return self._with_secret_pair(resolved, values)

    def settings_and_credential(self, overrides: Optional[dict] = None) -> tuple[Settings, dict]:
        """Return effective Settings and status from the same credential/source snapshot."""
        return self._resolve_bundle(overrides)

    def credential_status(self, overrides: Optional[dict] = None) -> dict:
        """Content-free status of the shared fallback pair against the base LLM endpoint.

        Named profile and role/stage credentials are intentionally not collapsed into this object;
        target-aware health and launch preflight validate every consumer separately.
        """
        return self._resolve_bundle(overrides)[1]

    @staticmethod
    def _runtime_credential_env(
            settings: Settings, *,
            consumer_roles: Optional[set[str | None] | frozenset[str | None]] = None,
            shared_active: Optional[bool] = None) -> dict[str, str]:
        """Authorize credentials whose binding matches this launch's resolved consumers.

        With no explicit consumer set this preserves the historical Settings-only superset used by
        public compatibility callers.  Task-aware spawn boundaries pass the exact plan: otherwise a
        custom task-owned consumer (for example an onboarding ``pilot`` while the ordinary pilot is
        inactive) can pass parent-side validation but lose its dedicated key when the scrubbed child
        environment is assembled.
        """
        from looplab.core.llm import (
            bound_api_key_for, client_kwargs_for, llm_credential_consumers,
            llm_optional_credential_consumers, resolve_llm_target, role_profile,
        )

        shared_key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""
        shared_binding = settings.llm_api_key_base_url or ""
        env = {
            _SECRET_ENV["llm_api_key"]: "",
            _SECRET_ENV["llm_api_key_base_url"]: "",
        }
        configured_shared_active, configured_role_keys = llm_credential_consumers(settings)
        if consumer_roles is None:
            role_keys = set(configured_role_keys)
            effective_shared_active = configured_shared_active
        else:
            role_keys = set(consumer_roles)
            effective_shared_active = bool(shared_active)
        # Requiredness is supplied by the caller: production spawn boundaries pass the exact
        # task-aware `llm_consumer_plan`, while the public no-task compatibility path passes the
        # conservative Settings-derived set. This exporter adds only valid bound pairs from that
        # required set plus the documented optional/latent candidates below. Invalid/missing pairs
        # are omitted here only after the matching preflight boundary has decided whether they are
        # required and, when required, rejected them before Popen.
        #
        # `developer` is a potential role even in unified settings: RepoTask.make_onboarder resolves
        # it, and a custom/historical split Strategy may later request the in-process default. That
        # late target is NOT required or probed at startup; make_developer_factory gates it only if
        # requested. Best-effort export lets that child gate consume a valid configured pair while
        # CliAgentDeveloper still strips every secret-looking variable from its own subprocess.
        candidate_roles = set(role_keys) | set(llm_optional_credential_consumers(settings))
        if getattr(settings, "backend", "toy") == "llm":
            candidate_roles.add("developer")
        for role in sorted(candidate_roles, key=lambda item: item or ""):
            try:
                target = resolve_llm_target(settings, role=role)
                configured_profile_env = role_profile(settings, role).get("api_key_env")
                if target.credential_mode == "none" and configured_profile_env:
                    continue
                if target.credential_mode == "shared":
                    bound_api_key_for(settings, target.base_url)
                    effective_shared_active = True
                    continue
                if not target.api_key_env:
                    continue
                kwargs = client_kwargs_for(target, role=role)
                env[target.api_key_env] = kwargs["api_key"]
                env[f"{target.api_key_env}_BASE_URL"] = kwargs["api_key_base_url"]
            except Exception:  # noqa: BLE001 - caller-side preflight owns requiredness/diagnosis
                continue

        if effective_shared_active and shared_key:
            try:
                bound_api_key_for(settings, settings.llm_base_url)
            except Exception:  # noqa: BLE001 — caller-side preflight rejects when its plan really needs this pair
                pass
            else:
                env[_SECRET_ENV["llm_api_key"]] = shared_key
                env[_SECRET_ENV["llm_api_key_base_url"]] = shared_binding
        return env

    @staticmethod
    def _validate_task_consumers(settings: Settings, task):
        """Validate and return the child's exact task-aware plan before a parent-side Popen."""
        from looplab.agents.reachability import llm_consumer_plan
        from looplab.core.llm import validate_bound_profiles

        plan = llm_consumer_plan(task, settings)
        validate_bound_profiles(
            settings, consumer_roles=plan.roles,
            external_fallback_roles=plan.external_fallback_roles,
            trusted_in_process_roles=plan.trusted_in_process_roles)
        return plan

    def validate_launch_consumers(self, settings: Settings, task) -> None:
        """Validate current credentials for frozen ordinary settings before a destructive phase.

        This is a snapshot check, not the Popen fence. Reset uses it before archiving, then
        ``launch_env(..., task=...)`` repeats the same check under the credential publication fence
        immediately before process creation so a later rotation/revocation cannot slip through.
        """
        with self.ui_settings_transaction(), self.secret_transaction():
            with _SECRET_ENV_LOCK:
                values, _source, _stored = self._effective_secret_values()
                resolved = self._with_secret_pair(settings, values)
            self._validate_task_consumers(resolved, task)

    def refresh_env_secrets(self, rd: Optional[Path] = None) -> dict[str, str]:
        """Return a per-spawn pair overlay; never mutate process-global ``os.environ``.

        Empty tombstones outrank a child process's `.env`, so revocation takes effect at its very
        next spawn. Callers that only need a refresh may continue to ignore the return value.
        """
        if rd is None:
            return {env_name: "" for env_name in _SECRET_ENV.values()}
        overrides: dict = {}
        found_snapshot = False
        try:
            payload = json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                overrides = payload
                found_snapshot = True
        except (OSError, json.JSONDecodeError):
            overrides = {}
        if not found_snapshot:
            return {env_name: "" for env_name in _SECRET_ENV.values()}
        settings = self.resolve_settings(overrides)
        return self._runtime_credential_env(settings)

    @contextmanager
    def launch_env_for_run(self, rd: Path):
        """Yield a fenced, credential-only overlay for a resume/reconciler child.

        Resume loads ordinary settings from its own immutable snapshot. Supplying them again through
        the environment would change precedence, so this context authorizes only the current secret
        pair (or revocation tombstones when the snapshot is absent). The shared run-config lock binds
        that exact endpoint/profile snapshot to credential authorization and is retained, together
        with the credential publication fence, through Popen.
        """
        from looplab.adapters.tasks import load_task
        from looplab.serve.engine_proc import _resolve_task_file
        from looplab.serve.run_files import run_config_write_lock
        from looplab.core.config import settings_from_snapshot

        snapshot = rd / "config.snapshot.json"
        with run_config_write_lock(snapshot):
            overrides: dict = {}
            resolved_snapshot: Optional[Settings] = None
            found_snapshot = False
            try:
                raw_snapshot = snapshot.read_text(encoding="utf-8")
            except FileNotFoundError:
                pass
            else:
                payload = json.loads(raw_snapshot)
                if not isinstance(payload, dict):
                    raise ValueError("config.snapshot.json must contain an object")
                overrides = payload
                resolved_snapshot = settings_from_snapshot(payload)
                found_snapshot = True
            task_file = _resolve_task_file(rd) if found_snapshot else None
            if found_snapshot and not task_file:
                raise ValueError("run snapshot has no loadable task for launch credential validation")
            task = load_task(task_file, existing_run=True) if task_file else None
            with self.launch_env(
                overrides, include_ordinary=False,
                authorize_credentials=found_snapshot,
                resolved_settings=resolved_snapshot, task=task) as env:
                yield env

    def prime_env(self) -> None:
        """Compatibility hook: stored pairs are intentionally never installed process-globally."""
        return None

    def _store_secret_locked(self, key: str, value: str, *, binding: Optional[str]) -> str:
        """Write one bound shared credential; caller holds ``secret_transaction``."""
        if key not in _SECRET_API_FIELDS:
            raise ValueError(f"unsupported secret field: {key}")
        payload = self.load_secrets()
        if value:
            if not binding:
                raise ValueError("a nonempty shared credential requires an endpoint binding")
            from looplab.core.llm import normalize_llm_base_url
            payload[key] = value
            payload["llm_api_key_base_url"] = normalize_llm_base_url(binding)
        else:
            payload.pop(key, None)
            payload.pop("llm_api_key_base_url", None)
        revision = _new_revision()
        payload[_REVISION_KEY] = revision
        fd, tmp = tempfile.mkstemp(
            dir=str(self._secrets_path.parent), prefix=".secrets-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload))
                handle.flush()
                best_effort_fsync(handle.fileno())
            os.replace(tmp, self._secrets_path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        try:
            os.chmod(self._secrets_path, 0o600)
        except OSError:
            pass
        return revision

    def store_secret(self, key: str, value: str, *, binding: Optional[str] = None,
                     expected_revision: Optional[str] = None) -> str:
        """CAS-write one bound credential and return its new opaque revision."""
        with self.settings_write_transaction():
            current = self.secret_revision()
            if expected_revision is not None and expected_revision != current:
                raise SettingsRevisionConflict("secret", expected_revision, current)
            return self._store_secret_locked(key, value, binding=binding)

    def resolved_settings(self, s: Optional["Settings"] = None) -> dict:
        """Effective launch defaults with the credential masked and binding omitted."""
        overrides = self.load_ui_settings()
        try:
            return self.resolve_settings(overrides).masked_snapshot()
        except Exception:  # noqa: BLE001 — unresolvable overrides fall back to the schema defaults merged with what was saved
            base = (s or Settings()).masked_snapshot()
            base.update(overrides)
            base.pop("llm_api_key_base_url", None)
            return base

    @staticmethod
    def ordinary_settings_env(settings: dict) -> dict[str, str]:
        """Render non-secret settings only; safe to retain across a long launch preparation."""
        env: dict[str, str] = {}
        for key, value in settings.items():
            if key not in _ALLOWED_FIELDS or key in _SECRET_FIELDS or value is None:
                continue
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, (list, dict)):
                rendered = json.dumps(value)
            else:
                rendered = str(value)
            env[f"LOOPLAB_{key.upper()}"] = rendered
        return env

    @contextmanager
    def launch_env(
            self, settings: dict, *, expected_settings_revision: Optional[str] = None,
            include_ordinary: bool = True, authorize_credentials: bool = True,
            resolved_settings: Optional[Settings] = None, task=None):
        """Yield a just-in-time child environment while its publication fence remains held.

        Acquire in the same UI -> secret -> launch order as writers. Once the environment is built,
        release the UI and secret filesystem locks but deliberately retain ``launch`` through the
        caller's Popen. Writers also acquire ``launch`` before publishing, so clear/rotate and spawn
        are linearly ordered without holding either settings file lock across process creation.

        The caller must do only Popen inside this context; settings reads there would invert the
        repository lock order.
        """
        launch = self.launch_transaction()
        launch_entered = False
        try:
            with self.ui_settings_transaction(), self.secret_transaction():
                launch.__enter__()
                launch_entered = True
                settings_revision = self.ui_settings_revision()
                if (expected_settings_revision is not None
                        and expected_settings_revision != settings_revision):
                    raise SettingsRevisionConflict(
                        "settings", expected_settings_revision, settings_revision)
                env = self.ordinary_settings_env(settings) if include_ordinary else {}
                if authorize_credentials:
                    if resolved_settings is None:
                        resolved = self.resolve_settings(settings)
                    else:
                        with _SECRET_ENV_LOCK:
                            values, _source, _stored = self._effective_secret_values()
                            resolved = self._with_secret_pair(resolved_settings, values)
                    consumer_plan = None
                    if task is not None:
                        consumer_plan = self._validate_task_consumers(resolved, task)
                    else:
                        # Public no-task callers predate task-aware reachability. Preserve their
                        # conservative settings-level gate; production spawn boundaries now pass
                        # a TaskAdapter and therefore use the exact consumer plan above.
                        from looplab.core.llm import validate_bound_profiles
                        validate_bound_profiles(resolved)
                    env.update(self._runtime_credential_env(
                        resolved,
                        consumer_roles=(consumer_plan.roles if consumer_plan is not None else None),
                        shared_active=(consumer_plan.shared_active
                                       if consumer_plan is not None else None)))
                else:
                    env.update({env_name: "" for env_name in _SECRET_ENV.values()})
            yield env
        finally:
            if launch_entered:
                # The launch context never suppresses an exception. Passing a clean exit here keeps
                # lock release independent from a Popen failure while preserving the original error.
                launch.__exit__(None, None, None)

    def settings_env(self, settings: dict) -> dict:
        """Resolve an environment immediately; direct spawners must use ``launch_env`` instead."""
        with self.launch_env(settings) as env:
            return env

    def write_ui_settings(self, overrides: dict) -> str:
        """Atomically publish overrides plus a fresh opaque CAS revision; caller holds UI lock."""
        revision = _new_revision()
        payload = dict(overrides)
        payload[_REVISION_KEY] = revision
        atomic_write_text(self._ui_settings_path, json.dumps(payload, indent=2))
        return revision
