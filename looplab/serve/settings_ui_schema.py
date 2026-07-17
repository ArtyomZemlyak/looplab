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
from pathlib import Path

from looplab.core.config import Settings


SETTINGS_UI_SCHEMA_VERSION = 1
SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT = 141
SETTINGS_UI_SCHEMA_SETTINGS_FIELD_COUNT = 164
SETTINGS_UI_SCHEMA_KEYSET_REVISION = "a5871bc85e10eeb6289dd6b4bba51de601320785719ca00e1db58fb2e9914878"
_SCHEMA_PATH = Path(__file__).with_name("settings_ui_schema.json")
_FIELD_TYPES = frozenset({"bool", "enum", "secret", "int", "float", "list", "text"})
_OPTIONAL_TEXT = ("help", "placeholder", "warning", "warningTitle", "warningTone")


def _text(value, label: str, *, maximum: int = 16_000, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value) or len(value) > maximum:
        raise RuntimeError(f"settings UI schema {label} must be a bounded string")
    return value


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
    if not isinstance(value, dict) or value.get("schema") != SETTINGS_UI_SCHEMA_VERSION:
        raise RuntimeError("settings UI schema version mismatch")

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

    # The digest covers the canonical semantic value, not whitespace in the package file.  It is
    # both the response revision and the strong ETag suffix for the versioned immutable endpoint.
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    revision = hashlib.sha256(canonical).hexdigest()
    return {**value, "revision": revision}, revision


SETTINGS_UI_SCHEMA, SETTINGS_UI_SCHEMA_REVISION = _load_schema()
SETTINGS_UI_SCHEMA_ETAG = f'"settings-ui-v{SETTINGS_UI_SCHEMA_VERSION}-{SETTINGS_UI_SCHEMA_REVISION}"'
