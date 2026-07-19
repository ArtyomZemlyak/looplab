"""Versioned, immutable metadata for the Settings and per-run Config editors.

The field catalogue is display data, not executable UI code.  Keeping it beside the server makes
the browser fetch it only when a settings surface opens and keeps the JavaScript bundle focused on
coercion, validation and interaction logic.  The form is intentionally curated rather than a raw
mirror of every structural/expert Settings field, but both the visible keyset and the complete
Settings-field count are pinned below.  A malformed catalogue, an accidental omission, or an
unreviewed Settings addition must fail the build/server instead of silently drifting the editor.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import get_args
from pathlib import Path

from looplab.core.config import Settings


# The packaged catalogue format remains v1. HTTP contract v2 adds bounds derived from the live
# Pydantic model so the browser never maintains a second, drifting copy of validation truth.
SETTINGS_UI_SCHEMA_CATALOGUE_VERSION = 1
SETTINGS_UI_SCHEMA_VERSION = 2
SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT = 148
SETTINGS_UI_SCHEMA_SETTINGS_FIELD_COUNT = 175
SETTINGS_UI_SCHEMA_KEYSET_REVISION = "35da0fffccaca0f3467891f0ce2df039b36deefae0b6fc09f4af63e88fe5d05a"
_SCHEMA_PATH = Path(__file__).with_name("settings_ui_schema.json")
_FIELD_TYPES = frozenset({"bool", "enum", "secret", "int", "float", "list", "text"})
_OPTIONAL_TEXT = ("help", "placeholder", "warning", "warningTitle", "warningTone")
_MODEL_BOUND_KEYS = (("ge", "minimum"), ("gt", "exclusiveMinimum"),
                     ("le", "maximum"), ("lt", "exclusiveMaximum"))


def _text(value, label: str, *, maximum: int = 16_000, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value) or len(value) > maximum:
        raise RuntimeError(f"settings UI schema {label} must be a bounded string")
    return value


def _numeric_bounds(key: str, kind: str) -> dict[str, int | float]:
    """Project Pydantic numeric constraints into inert, JSON-safe display metadata."""
    if kind not in {"int", "float"}:
        return {}
    bounds: dict[str, int | float] = {}
    for constraint in Settings.model_fields[key].metadata:
        for model_name, ui_name in _MODEL_BOUND_KEYS:
            value = getattr(constraint, model_name, None)
            if value is None:
                continue
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)):
                raise RuntimeError(f"Settings field {key!r} has a non-JSON numeric bound")
            if ui_name in bounds and bounds[ui_name] != value:
                raise RuntimeError(f"Settings field {key!r} repeats numeric bound {ui_name}")
            bounds[ui_name] = value
    return bounds


def _load_schema() -> tuple[dict, str]:
    try:
        raw = _SCHEMA_PATH.read_bytes()
    except OSError as exc:  # packaging omission must never become an empty editor
        raise RuntimeError(f"cannot read packaged settings UI schema: {exc}") from exc
    if len(raw) > 512 * 1024:
        raise RuntimeError("settings UI schema exceeds the 512 KiB package bound")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid packaged settings UI schema: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != SETTINGS_UI_SCHEMA_CATALOGUE_VERSION:
        raise RuntimeError("settings UI catalogue version mismatch")

    groups = value.get("groups")
    roles = value.get("agent_role_pills")
    if not isinstance(groups, list) or not 1 <= len(groups) <= 32:
        raise RuntimeError("settings UI schema groups must be a bounded non-empty list")
    if not isinstance(roles, dict) or not 1 <= len(roles) <= 16:
        raise RuntimeError("settings UI schema agent roles must be a bounded non-empty object")

    role_names: set[str] = set()
    for name, role in roles.items():
        _text(name, "role key", maximum=80)
        if name in role_names or not isinstance(role, dict):
            raise RuntimeError("settings UI schema has a duplicate or malformed role")
        role_names.add(name)
        _text(role.get("short"), f"role {name}.short", maximum=12)
        _text(role.get("title"), f"role {name}.title", maximum=500)

    known_fields = set(Settings.model_fields)
    if len(known_fields) != SETTINGS_UI_SCHEMA_SETTINGS_FIELD_COUNT:
        raise RuntimeError(
            "Settings field count changed; review the curated settings UI catalogue contract")
    seen_groups: set[str] = set()
    seen_fields: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            raise RuntimeError("settings UI schema group must be an object")
        title = _text(group.get("title"), "group title", maximum=200)
        _text(group.get("sub"), f"group {title}.sub", maximum=1000, empty=True)
        if title in seen_groups:
            raise RuntimeError(f"settings UI schema repeats group {title!r}")
        seen_groups.add(title)
        fields = group.get("fields")
        if not isinstance(fields, list) or not fields or len(fields) > 256:
            raise RuntimeError(f"settings UI schema group {title!r} has invalid fields")
        for field in fields:
            if not isinstance(field, dict):
                raise RuntimeError(f"settings UI schema group {title!r} has a malformed field")
            key = _text(field.get("key"), "field key", maximum=120)
            if key in seen_fields or key not in known_fields:
                raise RuntimeError(f"settings UI schema field {key!r} is duplicate or unknown")
            seen_fields.add(key)
            _text(field.get("label"), f"field {key}.label", maximum=500)
            kind = field.get("type")
            if kind not in _FIELD_TYPES:
                raise RuntimeError(f"settings UI schema field {key!r} has invalid type")
            for bound_name in {ui_name for _model_name, ui_name in _MODEL_BOUND_KEYS}:
                if bound_name in field:
                    raise RuntimeError(
                        f"settings UI catalogue must not duplicate model bound {key}.{bound_name}")
            if "nullable" in field:
                raise RuntimeError(
                    f"settings UI catalogue must not duplicate model nullability {key}.nullable")
            field.update(_numeric_bounds(key, kind))
            field["nullable"] = type(None) in get_args(Settings.model_fields[key].annotation)
            for attribute in _OPTIONAL_TEXT:
                if attribute in field:
                    _text(field[attribute], f"field {key}.{attribute}", empty=True)
            if "essential" in field and not isinstance(field["essential"], bool):
                raise RuntimeError(f"settings UI schema field {key!r} has invalid essential flag")
            options = field.get("options")
            if kind == "enum":
                if not isinstance(options, list) or not options or len(options) > 64:
                    raise RuntimeError(f"settings UI schema enum {key!r} has invalid options")
                for option in options:
                    _text(option, f"field {key}.option", maximum=500, empty=True)
            elif options is not None:
                raise RuntimeError(f"settings UI schema non-enum {key!r} declares options")
            agents = field.get("agents", [])
            if not isinstance(agents, list) or len(agents) > len(role_names):
                raise RuntimeError(f"settings UI schema field {key!r} has invalid agents")
            if len(set(agents)) != len(agents) or any(agent not in role_names for agent in agents):
                raise RuntimeError(f"settings UI schema field {key!r} references an unknown role")

    keyset_revision = hashlib.sha256("\0".join(sorted(seen_fields)).encode("utf-8")).hexdigest()
    if (len(seen_fields) != SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT
            or keyset_revision != SETTINGS_UI_SCHEMA_KEYSET_REVISION):
        raise RuntimeError(
            "settings UI catalogue keyset changed; review and pin the curated field contract")

    # The digest covers the canonical semantic value, not JSONResponse's exact transfer bytes. It is
    # therefore a semantic revision and weak ETag suffix for the revalidated endpoint.
    value = {**value, "schema": SETTINGS_UI_SCHEMA_VERSION}
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    revision = hashlib.sha256(canonical).hexdigest()
    return {**value, "revision": revision}, revision


SETTINGS_UI_SCHEMA, SETTINGS_UI_SCHEMA_REVISION = _load_schema()
SETTINGS_UI_SCHEMA_ETAG = f'W/"settings-ui-v{SETTINGS_UI_SCHEMA_VERSION}-{SETTINGS_UI_SCHEMA_REVISION}"'
