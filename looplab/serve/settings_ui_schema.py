"""Versioned, immutable metadata for the Settings and per-run Config editors.

The field catalogue is display data, not executable UI code.  Keeping it beside the server makes
the browser fetch it only when a settings surface opens and keeps the JavaScript bundle focused on
coercion, validation and interaction logic.  The form is intentionally curated rather than a raw
mirror of every structural/expert Settings field, so the review gate is a RECONCILIATION against
`Settings.model_fields`, not a count: every field is either a row here or listed in
`SETTINGS_UI_SCHEMA_UNCURATED_FIELDS` with the reason the form omits it, and every row pins the
DEFAULT its copy was written against.  A malformed catalogue, an accidental omission, an unreviewed
Settings addition, or a flipped default whose row still describes the old one must fail the
build/server instead of silently drifting the editor.
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
SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT = 178
# DERIVED, and deliberately no longer a hand-pinned review gate: a bare integer is satisfied by
# bumping the integer. That is exactly how `asha_live_kill_confidence` — the threshold that now
# decides every ASHA early stop — shipped with no row and no review (15b7822f took this constant
# 193 -> 194 and added zero rows to the catalogue). The real gate is the two-way reconciliation in
# `_reconcile_settings_fields` plus the per-row `default` pin in `_check_pinned_default`, both
# checked against the live model at load. This value only feeds the docs-count assertion in
# tests/test_config_docs_sync.py, which keeps configuration.md's "N of the M direct Settings
# fields" sentence honest.
SETTINGS_UI_SCHEMA_SETTINGS_FIELD_COUNT = len(Settings.model_fields)
# 169 rows since the ASSISTANT's own wall clock joined the agentic tool-loop group, beside
# `agent_time_budget_s`. It is a row rather than an uncurated omission for the same reason the
# fence is: it is the operator-visible knob that decides whether a long chat turn gets cut off,
# and until it existed the chat silently ignored the neighbouring row's documented "0 = no cap"
# and applied a five-minute ceiling nothing could name.
# 170 rows since the DEVELOPER PROBE joined Agent loop & models, beside the Developer session
# budgets — `developer_probe` and `developer_probe_timeout_s` (F2). They are rows rather than an
# uncurated omission for the same reason `read_fence` is: the probe is an EXECUTION surface inside
# the engine process, so an operator must be able to see that it exists and close it, and its
# timeout is the bound between "a question" and "a job that belongs in an eval stage".
# 169 rows since F8's `repair_critic_after` joined Resilience & efficiency, directly under
# `inline_repair_attempts`. It is a row rather than an uncurated omission because the operator is
# the one who has to understand that the repair bound stopped being a count: the cap above it now
# defaults to 0, and this is the knob that says when the second model gets its veto. Note what it is
# NOT — it cannot extend a repair loop, only end one — and the help text says so, because a reader
# who mistakes it for a budget will set it wrong in the expensive direction.
# 170 rows since METRIC PROVENANCE joined Safety & trust beside the read fence: `metric_subject`
# (off|audit|require, default audit) — what a recorded number is a claim ABOUT — and `landlock`
# (off|enforce, default OFF) — the kernel read allow-list. Both are rows rather than uncurated
# omissions for the same reason the fence is: each can REFUSE a running eval or change which
# nodes are selectable, and each has a default the operator is expected to move only on stated
# evidence (one repo run where every node binds; one real GPU eval under `enforce`).
# (Previously 168, since the SOURCE-TREE READ FENCE joined Safety & trust, beside `seed_mode`: the two are
# the same question from both ends — seeding decides what a node's own copy CONTAINS, the fence
# decides that the copy is the only place the node may read from. It is a row rather than an
# uncurated omission because it is an operator-visible policy that can REFUSE a running eval, and
# because the one legitimate reason to lower it (a large untracked in-tree input `seed_mode="auto"`
# does not copy) is a decision the operator makes about their own repo.
# (Previously 167, since METRIC SALVAGE joined the Resilience group — `metric_salvage`
# (off|audit|select, default audit) and `metric_salvage_repair`. They belong beside the inline-repair
# rows because they answer the same question from the other end: inline repair asks "can this node be
# made to work", salvage asks "did it already, and did we throw the answer away".)
# (173 since five curated rows landed across five branches on 2026-08-13. Four spend an operator's
# money or machine when switched on — `eval_deadline_grace_s` (GPU time), `developer_probe` and
# `developer_probe_timeout_s` (provider time), `repair_critic_after` (an extra call per repair from
# the Nth on) — and one, `assistant_time_budget_s`, gave the chat the wall clock it used to borrow
# from every engine role. Each branch pinned this digest against a tree without the others, so it is
# re-derived over the merged keyset rather than taken from any side.)
# (176 since docs/29 F1 added `proposal_width`: whether the run's WIDTH is decided by the research's
# own declared footprints or by the box alone. A curated row rather than an uncurated one because it
# changes how much hardware the run uses concurrently, which is the same class of question every
# other concurrency row on this form answers.)
# (177 since `train_monitor_tools`: whether the two live-eval watchdog JUDGES may query the stage
# logs themselves instead of being handed a fixed slice of them. Curated, beside the kill switch
# and its confidence bar, because it changes what the evidence for an LLM-judged kill IS — the
# measured slice was ~30 seconds of a five-hour run — and because it buys extra paid round trips
# per check, which is the same class of question every other watchdog row here answers.)
# (178 since `auto_extra_metrics`: may an UNDECLARED numeric key on an experiment's own stdout be
# RECORDED as one of that node's metrics? A curated row beside `metric_subject` because it is the
# same question one rung down — what a recorded number is, and who said so. The measurement is the
# argument for showing it: 12 of the 12 secondary metrics in this box's preserved runs came through
# that undeclared channel, one of them a schema VERSION number, and all of them were rendered beside
# the protected objective with nothing marking the difference.)
SETTINGS_UI_SCHEMA_KEYSET_REVISION = "36f5eb275a4bd27c918945673ab6cd9fd1a94ea787493aba590c5f0b2578bf03"
_SCHEMA_PATH = Path(__file__).with_name("settings_ui_schema.json")
_FIELD_TYPES = frozenset({"bool", "enum", "secret", "int", "float", "list", "text"})
_OPTIONAL_TEXT = ("help", "placeholder", "warning", "warningTitle", "warningTone")
_MODEL_BOUND_KEYS = (("ge", "minimum"), ("gt", "exclusiveMinimum"),
                     ("le", "maximum"), ("lt", "exclusiveMaximum"))
# The two defaults derived from the HOST (`~/.looplab/...`): a literal pin would be one developer's
# home directory and would fail the load everywhere else. They are pinned — and served — in
# home-relative form instead, so the row still carries a checkable, portable, operator-readable
# default rather than dropping out of the drift check.
_HOME_RELATIVE_DEFAULT_FIELDS = frozenset({"memory_dir", "knowledge_dir"})

# ---------------------------------------------------------------------------------------------
# The other half of the reconciliation: Settings fields the form deliberately does NOT carry.
#
# The curation rule this catalogue is kept by, written down because the 2026-08-04 default flips
# broke it in both directions: every switch that lets an LLM JUDGE end an experiment, and the
# confidence bar that decides it, is a row (`asha_live_kill` + `asha_live_kill_confidence`,
# `train_monitor_kill` + `train_monitor_kill_confidence`) — together with the observer each kill
# depends on, because a kill switch whose parent is invisible cannot be reasoned about. Cadences,
# sampling temperatures and defensive caps stay out.
#
# RESIDUAL RISK, stated rather than papered over: only CURATED rows carry a default pin, so flipping
# the default of a field on this list changes nothing here — correctly, because the form says nothing
# about it that could become false. What such a flip CAN falsify is its row in
# docs/guide/configuration.md, which has its own field-by-field guard (tests/test_config_docs_sync.py)
# but no default check at all. A flipped default on an uncurated field is therefore still reviewable
# only by a human reading that table.
_UNCURATED_OPEN_KEYED = frozenset({
    "agent_control", "agent_stage_base_urls", "agent_stage_models", "llm_profile", "llm_profiles",
    "llm_reasoning_extra", "role_profiles",
})
_UNCURATED_LEGACY_ALIAS = frozenset({"max_parallel", "parallel_build"})
# The RUN-LEVEL DECLARED ENVIRONMENT (F1d). Open-keyed like the group above — the keys are arbitrary
# variable NAMES, so the control would be a JSON blob editor — but it is its own row here because the
# reason to keep it out of a browser form is stronger than "no good widget": the field REFUSES a
# secret-shaped name or value on purpose (`core/envsafe.py::validate_env_map`), and a text box
# labelled "environment variables" inside the settings UI is precisely the affordance that invites
# someone to paste a token into a value this server then writes to `config.snapshot.json` and to
# `run_started`. It belongs in the config file / `-s eval_env=NAME=VALUE`, where the refusal reaches
# the operator at launch rather than after a save.
_UNCURATED_OPERATOR_DECLARED_ENV = frozenset({"eval_env"})
_UNCURATED_NOT_TYPED_BY_AN_OPERATOR = frozenset({
    "llm_api_key_base_url", "speculation_gate_receipt",
})
_UNCURATED_SECOND_ORDER = frozenset({
    "coverage_context", "developer_temperature", "eval_stall_timeout_s", "foresight_min_confidence",
    "foresight_verify", "foresight_verify_samples", "inline_repair_retrain_cap",
    "memora_anchors", "memora_cache",
    "memora_consolidate_threshold", "memora_llm", "phase_handoff_summary",
    "researcher_temperature", "sandbox_cpus", "sandbox_fsize_local", "sandbox_memory",
    "sandbox_memory_local", "sandbox_readonly_rootfs",
    "strategist_temperature", "train_monitor_interval_s",
    "workdir_audit",
})
SETTINGS_UI_SCHEMA_UNCURATED_FIELDS: dict[str, str] = {
    **dict.fromkeys(
        _UNCURATED_OPEN_KEYED,
        "open key set (stage / role / profile names, cross-referenced against each other) — a form "
        "row would have to be a JSON blob editor; edited in the config file or through /api"),
    **dict.fromkeys(
        _UNCURATED_LEGACY_ALIAS,
        "legacy alias superseded by a canonical row (eval_parallel / llm_parallel); it still parses "
        "from old env/config/snapshots, but two operator controls for one axis is how an operator "
        "sets the one the engine ignores"),
    **dict.fromkeys(
        _UNCURATED_NOT_TYPED_BY_AN_OPERATOR,
        "not an operator-typed value: a binding resolved at config load and popped from the "
        "snapshot, or a path to a receipt another command writes"),
    **dict.fromkeys(
        _UNCURATED_SECOND_ORDER,
        "second-order tuning (cadence, sampling temperature, retry cap, sandbox limit) whose parent "
        "feature already has a row and whose default is not a behaviour the form describes"),
    **dict.fromkeys(
        _UNCURATED_OPERATOR_DECLARED_ENV,
        "open key set AND a value the form should not invite: it refuses secret-shaped names on "
        "purpose, and a browser field labelled 'environment variables' is how one gets pasted into "
        "a run's durable snapshot — declared in the config file or `-s eval_env=NAME=VALUE`"),
}


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


def _shipped_default(key: str):
    """What `Settings()` actually gives this field today, factory defaults included."""
    model_field = Settings.model_fields[key]
    value = (model_field.default_factory() if model_field.default_factory is not None
             else model_field.default)
    home = str(Path.home())
    if (key in _HOME_RELATIVE_DEFAULT_FIELDS and isinstance(value, str)
            and home not in ("", "/") and value.startswith(home)):
        # `/home/dev/.looplab/memory` -> `~/.looplab/memory`, separators normalized so the pinned
        # value is the same string on every host and platform. Rendering, not re-derivation: the
        # location itself still comes from the field's own factory, so moving it fails the pin.
        return ("~" + value[len(home):]).replace("\\", "/")
    return value


def _same_default(pinned, shipped) -> bool:
    """Exact-enough equality for a pinned JSON default. `True == 1` in Python, so bools compare by
    identity: a boolean knob that became an int (or the reverse) is a review-worthy change, while a
    JSON `0` pinning a float `0.0` is the same default written two ways."""
    if isinstance(pinned, bool) or isinstance(shipped, bool):
        return isinstance(pinned, bool) and isinstance(shipped, bool) and pinned is shipped
    if isinstance(pinned, (int, float)) and isinstance(shipped, (int, float)):
        return float(pinned) == float(shipped)
    if isinstance(shipped, tuple):
        shipped = list(shipped)         # JSON has no tuple; an immutable default is still a list here
    return type(pinned) is type(shipped) and pinned == shipped


def _check_pinned_default(key: str, field: dict) -> None:
    """Fail when a row's copy was written against a DIFFERENT default than the one that ships.

    A default flip adds no field and removes none, so it moves no count and no keyset — the flip on
    2026-08-04 left `card_driven_selection` telling the operator to "opt in" to something already on
    and `asha_live_kill` describing a quantile that no longer kills, and nothing here noticed. The
    row's help text is a claim ABOUT the default; pinning the default beside it is what makes that
    claim checkable.
    """
    if "default" not in field:
        raise RuntimeError(
            f"settings UI catalogue field {key!r} does not pin the default its copy was reviewed "
            'against; add "default": <the value Settings ships>')
    shipped = _shipped_default(key)
    try:
        json.dumps(shipped)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Settings field {key!r} has a non-JSON default: {exc}") from exc
    if not _same_default(field["default"], shipped):
        raise RuntimeError(
            f"Settings field {key!r} now defaults to {shipped!r}, but its settings-form row was "
            f"reviewed against {field['default']!r}. Re-read that row's label/help against the code "
            "and re-pin the default in settings_ui_schema.json — a flipped default usually inverts "
            "copy that tells the operator to switch on what is already on.")


def _reconcile_settings_fields(curated: set[str]) -> None:
    """Every Settings field is a curated row or a written-down omission. No third option."""
    known = set(Settings.model_fields)
    uncurated = set(SETTINGS_UI_SCHEMA_UNCURATED_FIELDS)
    both = sorted(curated & uncurated)
    if both:
        raise RuntimeError(
            f"settings UI schema field(s) {both} are both a form row and listed as uncurated; "
            "delete the SETTINGS_UI_SCHEMA_UNCURATED_FIELDS entry")
    ghosts = sorted(uncurated - known)
    if ghosts:
        raise RuntimeError(
            f"SETTINGS_UI_SCHEMA_UNCURATED_FIELDS names removed Settings field(s) {ghosts}; "
            "a deleted knob must take its exemption with it")
    unreviewed = sorted(known - curated - uncurated)
    if unreviewed:
        raise RuntimeError(
            f"Settings field(s) {unreviewed} are neither a row in settings_ui_schema.json nor "
            "listed in SETTINGS_UI_SCHEMA_UNCURATED_FIELDS. Give each one a form row (with its "
            "`default` pinned) or record WHY the form omits it — an LLM-judged kill switch and the "
            "confidence bar that decides it are always rows.")


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
            _check_pinned_default(key, field)
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

    _reconcile_settings_fields(seen_fields)
    # Kept alongside the reconciliation because it catches the one move the reconciliation cannot:
    # DROPPING a row and adding the same key to the uncurated list still balances, and a knob that
    # quietly leaves the form has to be as reviewable as one that quietly joins it.
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
