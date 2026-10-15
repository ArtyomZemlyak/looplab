"""RUNTIME PARAMETERS — the configuration a node's attempt carried, as one flat table (doc 77 §2).

`Idea.params` is a PROPOSAL, and on an env-recipe task it is not even that: MiniOneRec's agent writes
descriptive labels there (`arm_index`, `users`) while the coordinates that decide the run live in
`MiniOneRec/looplab/experiment.env`, the one file its candidates author. Nothing showed that file's
values beside the metric, so an operator asking "what did this experiment run with?" had to open the
Code tab and read a diff. This module is the read-side answer and is deliberately small:

  * AUTHORED carriers — the configuration documents in the node's own `files` (its overlay, folded
    from `node_created`): exact, attempt-bound, no disk read;
  * WORKSPACE carriers — the task's LITERAL `edit_surface` entries that are configuration documents
    and that the node did not author, read from the attempt's workdir (what the attempt ran beside
    its overlay). A glob entry is not expanded: a pattern is a permission, not a list of files.

Formats: dotenv (`core/param_carriers.py::dotenv_assignments`), JSON (`bounded_json_loads`), YAML
(`yaml.safe_load`, bounded walk) and TOML. Every scalar leaf becomes one row `{key, value, source,
line?, origin}`; a nested document flattens to dotted keys. Nothing is executed or expanded.

WHAT IT DOES NOT CLAIM. It is a statement about DOCUMENTS, exactly the bound
`runtime/applied_params.py` states: a key the program never reads, a CLI flag or an environment
variable that wins over the file are invisible to any reader of bytes. The engine-side applied record
(`metric_provenance.applied_params`) stays the authority for declared dotted coordinates; this table
is the operator's view of the rest. A key the documents do not state is ABSENT, never zero.

SECRETS. A file whose NAME is secret-shaped (`.env`, `.env.local`, `secrets.yaml` —
`core/_pathsafe.py::looks_secret`, the artifact browser's own rule) is withheld whole. In the rest, a
value under a credential-shaped key (`core/redact.py::is_secret_key_name`, `core/envsafe.py::
is_secret_env`) or one the shape screen would rewrite (`redact_secrets`) is replaced by `***`.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

from looplab.core import param_carriers
from looplab.core._pathsafe import looks_secret
from looplab.core.envsafe import is_secret_env
from looplab.core.jsonutil import bounded_json_loads
from looplab.core.node_evidence import read_bounded_regular_file
from looplab.core.pathsafe import contained_member
from looplab.core.redact import is_secret_key_name, redact_secrets

MAX_CARRIERS = 8               # documents per node; the largest real working set names one
MAX_CARRIER_BYTES = 1 << 20    # a configuration larger than 1 MiB is data, not a configuration
MAX_ROWS = 500                 # rows per response; `truncated` says when the rest was cut
MAX_VALUE_CHARS = 300          # one value's display text
MAX_WALK_NODES = 20_000        # bounded walk of one parsed document (alias-heavy YAML included)
MAX_DEPTH = 24
MASK = "***"

ORIGIN_AUTHORED = "authored"
ORIGIN_WORKSPACE = "workspace"

# Carrier read outcomes. `withheld` is a refusal (secret-shaped file name), never "empty".
STATUS_READ = "read"
STATUS_ABSENT = "absent"
STATUS_UNPARSED = "unparsed"
STATUS_WITHHELD = "withheld"
STATUS_TOO_LARGE = "too_large"

# Declared-vs-document comparison outcomes.
MATCH_SAME = "same"
MATCH_DIFFERENT = "different"
MATCH_ABSENT = "absent"
MATCH_CONFLICT = "conflict"

_GLOB_CHARS = frozenset("*?[")


def carrier_format(path) -> Optional[str]:
    """`dotenv` / `json` / `yaml` / `toml` for a configuration document, else None."""
    name = str(path or "").lower()
    if param_carriers.is_dotenv_carrier(name):
        return "dotenv"
    if name.endswith(".json"):
        return "json"
    if name.endswith((".yaml", ".yml")):
        return "yaml"
    if name.endswith(".toml"):
        return "toml"
    return None


def _display(value) -> tuple[str, bool]:
    """`(text, numeric)` for one scalar leaf."""
    if isinstance(value, bool):
        return ("true" if value else "false"), False
    if value is None:
        return "null", False
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return str(value), False
        return str(value), True
    text = str(value)
    return (text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS - 1] + "…"), False


def _masked(key: str, text: str) -> bool:
    parts = [p for p in str(key).split(".") if p]
    if any(is_secret_key_name(p) for p in parts) or is_secret_env(parts[-1] if parts else key, text):
        return True
    return redact_secrets(text, entropy=False) != text


def _flatten(doc) -> list[tuple[str, object]]:
    """Scalar leaves of a parsed document as `(dotted key, value)`, bounded and cycle-safe."""
    out: list[tuple[str, object]] = []
    budget = [MAX_WALK_NODES]
    on_path: set[int] = set()

    def walk(node, parts):
        if budget[0] <= 0 or len(parts) > MAX_DEPTH:
            return
        budget[0] -= 1
        if isinstance(node, dict):
            if id(node) in on_path:
                return
            on_path.add(id(node))
            for name, child in node.items():
                walk(child, parts + (str(name),))
            on_path.discard(id(node))
            return
        if isinstance(node, (list, tuple)):
            if not parts:
                return
            # A list of scalars is one coordinate (`layers: [64, 32]`); a list of mappings is a
            # data structure, not a parameter, and an index is not a coordinate (as in
            # `param_carriers.yaml_numeric_paths`).
            if all(not isinstance(item, (dict, list, tuple)) for item in node[:64]):
                out.append((".".join(parts), "[" + ", ".join(_display(i)[0] for i in node[:64])
                            + (", …" if len(node) > 64 else "") + "]"))
            return
        if parts:
            out.append((".".join(parts), node))

    walk(doc, ())
    return out


def parse_carrier(path: str, text: str) -> tuple[Optional[list[dict]], str]:
    """`(rows, status)` for one document's text. Rows carry key/value/numeric/masked/line."""
    fmt = carrier_format(path)
    rows: list[dict] = []
    if fmt == "dotenv":
        for key, (value, line, quoted) in param_carriers.dotenv_assignments(text).items():
            shown, _ = _display(value)
            numeric = (not quoted) and param_carriers.plain_scalar_number(value) is not None
            rows.append({"key": key, "value": shown, "numeric": numeric, "line": line})
    else:
        import yaml
        try:
            if fmt == "json":
                doc = bounded_json_loads(text)
            elif fmt == "yaml":
                doc = yaml.safe_load(text)
            elif fmt == "toml":
                import tomllib
                doc = tomllib.loads(text)
            else:
                return None, STATUS_UNPARSED
        except (yaml.YAMLError, ValueError, TypeError, RecursionError, MemoryError):
            # A candidate may commit anything; a parse error is not evidence (tomllib's and the
            # JSON loader's errors are ValueErrors).
            return None, STATUS_UNPARSED
        if not isinstance(doc, dict):
            return None, STATUS_UNPARSED
        try:
            for key, value in _flatten(doc):
                shown, numeric = _display(value)
                rows.append({"key": key, "value": shown, "numeric": numeric, "line": None})
        except ValueError:
            # YAML and TOML both read a hex/octal/binary literal of ANY size (the int-to-str digit
            # limit binds only decimal parsing), and `str()` of one past 4 300 digits raises — as a
            # value or as a YAML mapping key. That escaped as a 500; it is a document this
            # projection cannot show, which the contract spells as a carrier status, never a raise.
            return None, STATUS_UNPARSED
    for row in rows:
        row["masked"] = _masked(row["key"], row["value"])
        if row["masked"]:
            row["value"], row["numeric"] = MASK, False
    return rows, STATUS_READ


def literal_surface_carriers(edit_surface) -> list[str]:
    """The task's `edit_surface` entries that NAME one configuration document (no glob)."""
    out = []
    for entry in edit_surface or ():
        if not isinstance(entry, str) or not entry or _GLOB_CHARS & set(entry):
            continue
        rel = entry.replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        if rel and carrier_format(rel) and rel not in out:
            out.append(rel)
    return out


def _number(text) -> Optional[float]:
    return param_carriers.plain_scalar_number(text)


def compare_declared(declared: dict, rows: list[dict]) -> list[dict]:
    """Each declared `Idea.params` key against the documents' rows.

    A row answers a declaration when its key equals it (case-insensitively — env keys are upper-case
    by convention, Idea keys are not), or, when no row does, when its dotted key ENDS in the
    declaration's parts. Two rows that answer with different values are a `conflict`, never a
    tie-break."""
    out = []
    for name, want in sorted((declared or {}).items()):
        parts = [p.lower() for p in str(name).split(".") if p]
        if not parts:
            continue
        exact, suffix = [], []
        for row in rows:
            if row.get("masked"):
                continue
            key_parts = [p.lower() for p in str(row["key"]).split(".") if p]
            if key_parts == parts:
                exact.append(row)
            elif len(key_parts) > len(parts) and key_parts[-len(parts):] == parts:
                suffix.append(row)
        # A row naming the WHOLE declared key wins outright over longer keys ending in it — the
        # first rung of `param_carriers.resolve_declaration`, for the same reason.
        hits = exact or suffix
        values = {r["value"] for r in hits}
        entry = {"param": name, "declared": want,
                 "sources": [{"source": r["source"], "key": r["key"], "value": r["value"],
                              "line": r.get("line")} for r in hits[:4]]}
        if not hits:
            entry["match"] = MATCH_ABSENT
        elif len(values) > 1:
            entry["match"] = MATCH_CONFLICT
        else:
            got = next(iter(values))
            want_num = param_carriers.finite_number(want)
            got_num = _number(got)
            same = (want_num is not None and got_num is not None and math.isclose(
                want_num, got_num, rel_tol=1e-9, abs_tol=0.0)) or str(want) == got
            entry["match"] = MATCH_SAME if same else MATCH_DIFFERENT
        out.append(entry)
    return out


def runtime_params(node, *, workdir: Optional[Path], edit_surface=()) -> dict:
    """The projection for one node attempt. Total: every failure is a carrier status, never a raise."""
    files = getattr(node, "files", None) or {}
    authored = sorted(p for p in files if isinstance(p, str) and carrier_format(p))
    surface = [p for p in literal_surface_carriers(edit_surface) if p not in files]
    carriers: list[dict] = []
    rows: list[dict] = []
    truncated = False
    planned = [(p, ORIGIN_AUTHORED) for p in authored] + [(p, ORIGIN_WORKSPACE) for p in surface]
    if len(planned) > MAX_CARRIERS:
        planned, truncated = planned[:MAX_CARRIERS], True
    for path, origin in planned:
        entry = {"path": path, "origin": origin, "format": carrier_format(path)}
        carriers.append(entry)
        if looks_secret(Path(path)):
            entry["status"] = STATUS_WITHHELD
            continue
        if origin == ORIGIN_AUTHORED:
            text = files.get(path)
            if not isinstance(text, str):
                entry["status"] = STATUS_UNPARSED
                continue
            if len(text.encode("utf-8", "replace")) > MAX_CARRIER_BYTES:
                entry["status"] = STATUS_TOO_LARGE
                continue
        else:
            target = contained_member(workdir, path) if workdir is not None else None
            raw = (read_bounded_regular_file(target, MAX_CARRIER_BYTES + 1)
                   if target is not None else None)
            if raw is None:
                entry["status"] = STATUS_ABSENT
                continue
            if len(raw) > MAX_CARRIER_BYTES:
                entry["status"] = STATUS_TOO_LARGE
                continue
            text = raw.decode("utf-8", "replace")
        parsed, status = parse_carrier(path, text)
        entry["status"] = status
        if parsed is None:
            continue
        entry["n_keys"] = len(parsed)
        for row in parsed:
            if len(rows) >= MAX_ROWS:
                truncated = True
                break
            rows.append({**row, "source": path, "origin": origin})
    idea = getattr(node, "idea", None)
    declared = dict(getattr(idea, "params", None) or {})
    return {
        "node_id": getattr(node, "id", None),
        "attempt": getattr(node, "attempt", None),
        "declared": declared,
        "carriers": carriers,
        "params": rows,
        "comparisons": compare_declared(declared, rows),
        "truncated": truncated,
    }
