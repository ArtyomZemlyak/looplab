"""Did the path a candidate built actually RUN during its evaluation?

Measured 2026-09-23 on a MiniOneRec inference run. A node added a prefix-cached prefill behind a
self-check. The check died on an attribute transformers 5 removed, a fallback switched the new path
off, and the node scored 1.004 with every list byte-identical to its parent -- the unchanged tree
measured again, and recorded as an idea that does not help. Nothing in the engine could tell "this
change is worthless" from "this change never executed": both finish cleanly and print a number.

So a Developer may DECLARE, per node, lines its new code prints only when the new path is active
(`activation_markers` on its `done`, persisted as `looplab_activation.json` beside
`looplab_stages.json`). After an evaluation that otherwise succeeded, the engine checks that every
declared marker appears in what the evaluation printed. One that does not makes the attempt
`inert_path` (`core/models.py::FAILURE_REASONS`): the metric is withheld, because it measured the
path the node meant to replace, and the node goes to repair with the missing marker named.

WHY A DECLARATION AND NOT A GUESS. `engine/triage.py::_failure_reason` classifies structurally and
never from a failure's own text -- the rules that parsed messages were deleted on 2026-08-20. A
search of the log for "disabled" or "fallback" would be that rule again, with the candidate's own
choice of words deciding the verdict. A declared marker is a CONTRACT, like a stage's `expect`: the
node said what running looks like, and the engine only checks that it happened. A node that declares
nothing is judged exactly as before.

TYPED ENTRIES AND A GRADED VERDICT (minionerec-lora-v1 node 2, 2026-10-01). That node changed ONLY
`MiniOneRec/looplab/experiment.env` and declared `SFT_EVAL_SAMPLE=-2` and `SFT_RESUME_EVERY_MIN=0`
-- env ASSIGNMENTS, which no code prints. Its third attempt trained for 6.8 h, printed 0.1126388, and
the metric was withheld as `inert_path`; the repair directive made the Developer add `echo` lines and
a full 7 h re-run followed. A string is still a `log` entry (`{"kind": "log", "text": s}`), so every
manifest written before is read exactly as it was; three more kinds say what a config change CAN
show: `env` (a static check of the last `NAME=` assignment in a named file), `file` (an artefact
predicate, the stage `expect` shape) and `none` (a stated reason). WHICH declarations can be proven
and which merely could not be is a deterministic matrix (`check_activation`), keyed on where the
marker's emitter lives (`scan_emitters`) and on what the node changed (`change_class`): a missing
marker whose printer exists still withholds the metric (TP1/TP2), and one that nothing anywhere
could print withholds it on a CODE change (TP3) but only WARNS on a config-only one. No model
decides any of it; `Settings.activation_check` = "strict" is the historical rule byte for byte.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat as _stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from looplab.core.idea_report import IDEA_REPORT_NAME
from looplab.core.node_evidence import open_untrusted_regular, read_bounded_regular_file
from looplab.engine.eval_log_plan import _file_identity, _log_name_key, attempt_byte_floor

ACTIVATION_MANIFEST_NAME = "looplab_activation.json"
CHANGE_CAPABILITY = "capability"  # doc 72 hunk advice; activation.change_class semantics stay unchanged
MAX_MARKERS = 8
MAX_MARKER_CHARS = 200
# Per file, from its START and from its END: a marker is usually printed at warmup and a log is
# usually short, but a training log is not, and the check must stay bounded whatever the candidate
# printed. The end alone missed a warmup marker followed by 9 MiB of training output (crit_v55 A3,
# driven: a real metric withheld as `inert_path`), so a longer log is read at both ends and only
# its middle goes unread.
_MAX_LOG_BYTES = 8 * 1024 * 1024


def normalize_markers(raw) -> list:
    """The markers a Developer declared, as a clean bounded list; [] for anything unusable.

    Total: runs inside an emit that has already cost minutes. Each marker is stripped, must be a
    non-empty string, is cut to `MAX_MARKER_CHARS` (a marker is a line fragment, not a paragraph),
    and duplicates collapse. At most `MAX_MARKERS` survive."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list = []
    for item in raw:
        if not isinstance(item, str):
            continue
        marker = item.strip()[:MAX_MARKER_CHARS].strip()
        if marker and marker not in out:
            out.append(marker)
        if len(out) >= MAX_MARKERS:
            break
    return out


def manifest_text(markers: Iterable) -> str:
    """The manifest's bytes. A plain `log` text entry is written as the bare string it always was,
    so a manifest of strings is byte-identical to one written before typed entries existed."""
    return json.dumps({"markers": [_manifest_item(m) for m in markers]}, indent=1)


# The largest activation manifest read. A list of short marker strings; anything bigger is not one.
_MAX_MANIFEST_BYTES = 1 << 20


def read_markers(workdir) -> list:
    """The markers declared in a node's workdir, or [] -- a malformed file is no declaration.

    Read as the CANDIDATE's file it is (`core/node_evidence.py::read_bounded_regular_file`: no link,
    no FIFO, bounded) and parsed for everything `json.loads` raises: a FIFO under this name blocked
    the event loop and a manifest nested past ~1,000 levels raised `RecursionError` out of
    `_eval_settle_outcome` — the node's `engine_error`, the run paused (critic 2026-09-26, driven)."""
    raw = read_bounded_regular_file(Path(workdir) / ACTIVATION_MANIFEST_NAME, _MAX_MANIFEST_BYTES + 1)
    if raw is None or len(raw) > _MAX_MANIFEST_BYTES:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, RecursionError):
        return []
    return normalize_markers(data.get("markers") if isinstance(data, dict) else None)


# ---------------------------------------------------------------- typed entries (manifest v2)
#
# minionerec-lora-v1 node 2, 2026-10-01: a node that changes only a config file has nothing to PRINT --
# its declared "markers" were `NAME=value` assignments, which no code echoes, and the metric of a
# 6.8 h run was withheld for it. A string is still a log line; the other kinds say what a config
# change can show. NO python probe kind, deliberately: a probe is candidate code run by the engine
# after the eval, which is a second evaluation the fence and the budget do not cover.
KIND_LOG = "log"
KIND_ENV = "env"
KIND_FILE = "file"
KIND_NONE = "none"
ACTIVATION_KINDS = (KIND_LOG, KIND_ENV, KIND_FILE, KIND_NONE)
# A regex is matched with `fullmatch` against ONE line at a time, so its length bounds what it can
# say and the line cap bounds what it is asked of (a pathological line is skipped, never matched).
MAX_REGEX_CHARS = 200
_MAX_REGEX_LINE_CHARS = 4096
_MAX_JSON_EQ_KEYS = 8
_MAX_REASON_CHARS = 300
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*\Z")


def _clean(value, cap: int = MAX_MARKER_CHARS) -> str:
    return value.strip()[:cap].strip() if isinstance(value, str) else ""


def _clean_rel_path(value) -> str:
    """A workdir-relative posix path, or "" -- absolute, drive-qualified and `..` names are refused
    here, lexically; the READ resolves it again (`core/pathsafe.py::contained_member`)."""
    text = _clean(value, 512).replace("\\", "/")
    if not text or text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        return ""
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return ""
    return "/".join(parts)


def _repeat_nested(parsed) -> bool:
    """Does a repeated group contain another repeat -- the shape of catastrophic backtracking?"""
    try:
        from re import _constants as _c   # type: ignore[attr-defined]   # 3.11+
    except ImportError:                    # pragma: no cover — older interpreters
        import sre_constants as _c        # type: ignore[no-redef]
    repeats = {_c.MAX_REPEAT, _c.MIN_REPEAT, getattr(_c, "POSSESSIVE_REPEAT", _c.MAX_REPEAT)}

    def walk(items, inside: bool) -> bool:
        for op, av in items:
            if op in repeats:
                lo, hi, sub = av
                if inside and hi != 1:
                    return True
                if walk(sub, inside or hi != 1):
                    return True
            elif op is _c.SUBPATTERN:
                if walk(av[-1], inside):
                    return True
            elif op is _c.BRANCH:
                if any(walk(b, inside) for b in av[1]):
                    return True
            elif op in (_c.ASSERT, _c.ASSERT_NOT):
                if walk(av[1], inside):
                    return True
        return False
    return walk(parsed, False)


def compile_marker_regex(pattern) -> Optional["re.Pattern"]:
    """The bounded regex a `log` entry may carry, or None: at most `MAX_REGEX_CHARS`, compiles, and
    no repeat nested inside a repeat (`(a+)+` backtracks exponentially, and the check runs on the
    event loop's worker over megabytes the candidate printed)."""
    if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_REGEX_CHARS:
        return None
    try:
        try:
            from re import _parser as _p   # type: ignore[attr-defined]
        except ImportError:                # pragma: no cover
            import sre_parse as _p         # type: ignore[no-redef]
        if _repeat_nested(_p.parse(pattern)):
            return None
        return re.compile(pattern)
    except (re.error, RecursionError, ValueError, TypeError, OverflowError):
        return None


def _json_scalar(value) -> bool:
    return value is None or isinstance(value, (str, bool, int, float))


def normalize_entry(item) -> Optional[dict]:
    """ONE declared entry in its canonical typed form, or None for anything unusable. Total.

    A bare string is `{"kind": "log", "text": s}` -- every manifest written before typed entries
    existed reads exactly as it did. `log` carries `text` (an exact, case-sensitive fragment) or a
    bounded `regex` (`compile_marker_regex`); `env` a NAME, the value it must equal and the FILE the
    assignment lives in (a static check, never the process environment); `file` a workdir-relative
    path, whether it must be this attempt's (`fresh`, default true) and optional `json_eq`; `none`
    the reason nothing observable exists."""
    if isinstance(item, str):
        text = _clean(item)
        return {"kind": KIND_LOG, "text": text} if text else None
    if not isinstance(item, dict):
        return None
    kind = item.get("kind", KIND_LOG)
    if kind == KIND_LOG:
        text = _clean(item.get("text"))
        regex = item.get("regex")
        if text and regex is None:
            return {"kind": KIND_LOG, "text": text}
        if not text and compile_marker_regex(regex) is not None:
            return {"kind": KIND_LOG, "regex": regex}
        return None
    if kind == KIND_ENV:
        name = _clean(item.get("name"), 128)
        raw = item.get("equals")
        equals = (_clean(raw) if isinstance(raw, str)
                  else str(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool)
                  else None)
        path = _clean_rel_path(item.get("file"))
        if not name or not _ENV_NAME.match(name) or equals is None or not path:
            return None
        return {"kind": KIND_ENV, "name": name, "equals": equals, "file": path}
    if kind == KIND_FILE:
        path = _clean_rel_path(item.get("path"))
        if not path:
            return None
        out: dict = {"kind": KIND_FILE, "path": path, "fresh": item.get("fresh", True) is not False}
        eq = item.get("json_eq")
        if eq is not None:
            if (not isinstance(eq, dict) or not eq or len(eq) > _MAX_JSON_EQ_KEYS
                    or not all(isinstance(k, str) and 0 < len(k) <= MAX_MARKER_CHARS
                               and _json_scalar(v) for k, v in eq.items())):
                return None
            out["json_eq"] = {k: eq[k] for k in sorted(eq)}
        return out
    if kind == KIND_NONE:
        why = _clean(item.get("why"), _MAX_REASON_CHARS)
        return {"kind": KIND_NONE, "why": why} if why else None
    return None


def normalize_entries(raw) -> list:
    """The declared entries as a clean bounded typed list; [] for anything unusable. Total.

    Same bounds as `normalize_markers` (at most `MAX_MARKERS`, duplicates collapse). A `none` entry
    beside an observable one says nothing the observable one does not, so it is dropped there."""
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list = []
    for item in raw:
        entry = normalize_entry(item)
        if entry is not None and entry not in out:
            out.append(entry)
        if len(out) >= MAX_MARKERS:
            break
    if any(e["kind"] != KIND_NONE for e in out):
        out = [e for e in out if e["kind"] != KIND_NONE]
    return out


def entry_label(entry) -> str:
    """How an entry is NAMED to a reader -- a log entry by its own text, so a pre-typed manifest's
    `missing` list reads exactly as it did."""
    kind = entry.get("kind")
    if kind == KIND_LOG:
        return entry.get("text") or f"/{entry.get('regex')}/"
    if kind == KIND_ENV:
        return f"{entry['name']}={entry['equals']} in {entry['file']}"
    if kind == KIND_FILE:
        return f"file {entry['path']}"
    return f"none: {entry.get('why', '')}"


def _manifest_item(entry):
    if isinstance(entry, dict) and entry.get("kind") == KIND_LOG and set(entry) == {"kind", "text"}:
        return entry["text"]
    return entry


def read_manifest(workdir) -> list:
    """The TYPED entries declared in a node's workdir, or [] -- `read_markers`' reading rules."""
    raw = read_bounded_regular_file(Path(workdir) / ACTIVATION_MANIFEST_NAME, _MAX_MANIFEST_BYTES + 1)
    if raw is None or len(raw) > _MAX_MANIFEST_BYTES:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, RecursionError):
        return []
    return normalize_entries(data.get("markers") if isinstance(data, dict) else None)


def log_entries(entries) -> list:
    return [e for e in (entries or ()) if isinstance(e, dict) and e.get("kind") == KIND_LOG]


def log_entry_seen(entry, haystacks) -> bool:
    """Did the evaluation print this `log` entry? Exact substring for `text`, a per-line
    `fullmatch` for `regex` (lines past `_MAX_REGEX_LINE_CHARS` are not asked)."""
    text = entry.get("text")
    if text:
        return any(text in h for h in haystacks)
    pattern = compile_marker_regex(entry.get("regex"))
    if pattern is None:
        return False
    for h in haystacks:
        for line in h.splitlines():
            if len(line) <= _MAX_REGEX_LINE_CHARS and pattern.fullmatch(line):
                return True
    return False


# ---------------------------------------------------------------- static env / file predicates

_ENV_FILE_BYTES = 1 << 20
_FILE_JSON_BYTES = 4 << 20
# The freshness slack the stage `expect` check allows (`runtime/command_eval.py::_FRESH_EPS`): a file
# whose mtime is this far before the attempt's start still counts as written by it (coarse mtimes).
_FRESH_SLACK_S = 2.0


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def env_assignment(text: str, name: str) -> Optional[str]:
    """The value of the LAST `NAME=` (or `export NAME=`, or `NAME:`) assignment in `text`, or None.
    An unquoted value loses a trailing ` # comment`; a quoted one keeps everything inside its quotes.
    Static on purpose: what the file SAYS, not what a script that sources it ends up exporting --
    a later override is invisible here, which is why a satisfied env entry is graded `weak`."""
    pat = re.compile(r"^\s*(?:export\s+)?" + re.escape(name) + r"\s*[=:]\s*(.*?)\s*$")
    found = None
    for line in text.splitlines():
        m = pat.match(line)
        if not m:
            continue
        value = m.group(1)
        if value[:1] not in ("'", '"'):
            value = value.split(" #", 1)[0]
        found = _unquote(value)
    return found


def check_env_entry(entry, workdir) -> bool:
    from looplab.core.pathsafe import contained_member
    target = contained_member(workdir, entry["file"])
    if target is None:
        return False
    raw = read_bounded_regular_file(target, _ENV_FILE_BYTES)
    if raw is None:
        return False
    value = env_assignment(raw.decode("utf-8", "replace"), entry["name"])
    return value is not None and value == _unquote(str(entry["equals"]))


def _dotted(data, key: str):
    cur = data
    for part in key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


_MISSING = object()


def _json_equal(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b


def check_file_entry(entry, workdir, since: Optional[float]) -> bool:
    """The stage `expect.files` predicate on one path: exists inside the workdir, non-empty (a file)
    or non-empty (a directory), written by THIS attempt when `fresh`, and every `json_eq` key equal."""
    from looplab.core.pathsafe import contained_member
    target = contained_member(workdir, entry["path"])
    if target is None:
        return False
    try:
        st = target.stat()
        if target.is_dir():
            if not any(target.iterdir()):
                return False
        elif st.st_size <= 0:
            return False
    except OSError:
        return False
    if entry.get("fresh", True) and since is not None and st.st_mtime < float(since) - _FRESH_SLACK_S:
        return False
    eq = entry.get("json_eq")
    if eq:
        raw = read_bounded_regular_file(target, _FILE_JSON_BYTES)
        if raw is None:
            return False
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, TypeError, RecursionError):
            return False
        for key, want in eq.items():
            got = _dotted(data, key)
            if got is _MISSING or not _json_equal(got, want):
                return False
    return True


# ---------------------------------------------------------------- where a marker's printer lives
#
# A config file SETS values; it prints nothing. minionerec-lora-v1 node 2 (2026-10-01): the
# declaration-time check took `experiment.env` whole, found `SFT_EVAL_SAMPLE=-2` in it and passed a
# marker that no code could ever print. So an occurrence in a config file, or on an assignment line
# anywhere, is not an EMITTER.
CONFIG_SUFFIXES = (".env", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".json")
# Widened on the critic's pass: a printer in a Cython module, a CUDA header or a notebook the eval
# executes was simply unread, and "no printer anywhere" on a config-only change then WARNed where an
# existing printer should have blocked (TP2).
CODE_SUFFIXES = (".py", ".pyx", ".pxd", ".sh", ".bash", ".zsh", ".ksh", ".fish", ".js", ".mjs",
                 ".cjs", ".ts", ".rb", ".pl", ".lua", ".r", ".jl", ".go", ".rs", ".c", ".cc",
                 ".cpp", ".cxx", ".cu", ".cuh", ".h", ".hpp", ".hxx", ".java", ".scala", ".kt",
                 ".swift", ".php", ".ipynb", ".mk", ".cmake", ".ps1", ".bat", ".cmd")
# Files whose CONTENTS the engine executes although their suffix reads as config: the stage manifest
# (`engine/eval_stages.py::STAGE_MANIFEST_NAME`, pinned equal by a test) is the pipeline's commands,
# so a change to it alone is a CODE change, and an `echo` in it is a printer.
EXECUTED_MANIFESTS = frozenset({"looplab_stages.json"})
# Paths the change class never counts: the engine's own declarations, not the node's change.
CHANGE_CLASS_IGNORED = frozenset({ACTIVATION_MANIFEST_NAME, IDEA_REPORT_NAME})
# The toy/dataset tasks' solution file: written from `node.code` by the sandbox, not from `files`.
SOLUTION_FILE = "solution.py"
CHANGE_CONFIG_ONLY = "config_only"
CHANGE_CODE = "code"
_ASSIGNMENT_LINE = re.compile(
    r"^\s*(?:export\s+|local\s+|readonly\s+|declare\s+(?:-\w+\s+)*)?[A-Za-z_][A-Za-z0-9_.\-]*\s*[=:]")
_PRINT_VERB = re.compile(r"\b(echo|printf|print|puts|logger|log|console\.log|warn|info|write)\b")
_COMMENT_PREFIXES = ("#", "//", "--", ";")


def is_test_path(path) -> bool:
    """A test file: the evaluation does not run it, so nothing it prints is the eval's output."""
    parts = str(path).replace("\\", "/").split("/")
    name = parts[-1]
    return (any(p in ("tests", "test") for p in parts[:-1])
            or name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py")


def is_config_path(path) -> bool:
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name in EXECUTED_MANIFESTS:
        return False
    return name.endswith(CONFIG_SUFFIXES) or name == ".env" or name.startswith(".env.")


def is_code_path(path, head: bytes = b"") -> bool:
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.endswith(CODE_SUFFIXES) or name in EXECUTED_MANIFESTS:
        return True
    return "." not in name and head.startswith(b"#!")


def printable_text(path, body: str) -> str:
    """What a staged file could PRINT: for Python, its string literals minus docstrings and bare
    string statements (comments are not in the tree at all); any other file, or Python that does not
    parse, is taken whole.

    Measured 2026-09-24: a node declared `FP8_DECODE_MLP_FALLBACK`, a name its module docstring used
    for the fallback, while the code printed `FP8_DECODE_MLP_ACTIVE`/`..._DISABLED`. A substring
    search of the file found the docstring, the declaration passed, and the node was filed
    `inert_path` although its new path had run."""
    if not str(path).endswith(".py"):
        return body
    try:
        tree = ast.parse(body)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return body
    prose: set = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            prose.add(id(node.value))
    return "\n".join(n.value for n in ast.walk(tree)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str)
                     and id(n) not in prose)


@dataclass(frozen=True)
class Emitter:
    """One place the evaluated tree could print a marker. `conditional` is False only for a print
    no branch guards (Python: no `if`/`try`/loop/boolean between it and its function or module;
    shell: outside every `if`/`case`/loop and with no `&&`/`||` on its line) -- an `echo` added
    FOR the check, which proves the script ran, never that a path did (69.8)."""

    path: str
    conditional: bool


_PY_CONDITIONAL = (ast.If, ast.IfExp, ast.Try, ast.ExceptHandler, ast.While, ast.For, ast.AsyncFor,
                   ast.BoolOp, ast.Match, ast.comprehension) + (
                       (ast.TryStar,) if hasattr(ast, "TryStar") else ())
_PY_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.Module, ast.ClassDef)


def _py_emitters(path: str, tree, needle: str) -> list:
    parents: dict = {}
    prose: set = set()
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            prose.add(id(node.value))
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in prose and needle in node.value):
            continue
        conditional = False
        cur = parents.get(id(node))
        while cur is not None and not isinstance(cur, _PY_SCOPES):
            if isinstance(cur, _PY_CONDITIONAL):
                conditional = True
                break
            cur = parents.get(id(cur))
        out.append(Emitter(path, conditional))
    return out


_SH_OPEN = re.compile(r"^\s*(if|case|while|until|for|select)\b")
_SH_CLOSE = re.compile(r"^\s*(fi|esac|done)\b")


def _text_emitters(path: str, body: str, needle: str) -> list:
    out = []
    depth = 0
    for line in body.splitlines():
        stripped = line.strip()
        if _SH_CLOSE.match(stripped):
            depth = max(0, depth - 1)
        opened = bool(_SH_OPEN.match(stripped)) and not re.search(r"\b(fi|esac|done)\s*;?\s*$", stripped)
        if needle in line and stripped and not stripped.startswith(_COMMENT_PREFIXES):
            if not _ASSIGNMENT_LINE.match(line) or _PRINT_VERB.search(line):
                conditional = (depth > 0 or opened or "&&" in line or "||" in line
                               or bool(re.match(r"^\s*(then|else|elif)\b", stripped)))
                out.append(Emitter(path, conditional))
        if opened:
            depth += 1
    return out


def emitters_in_file(path, body: str, needle: str) -> list:
    """Where `body` (the file at `path`) could print `needle`: [] for a test file, a config file,
    a docstring or comment, or an assignment line."""
    path = str(path).replace("\\", "/")
    if not needle or is_test_path(path) or is_config_path(path) or needle not in body:
        return []
    if path.endswith(".py"):
        try:
            return _py_emitters(path, ast.parse(body), needle)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            pass
    return _text_emitters(path, body, needle)


def config_occurrences(path, body: str, needle: str) -> bool:
    """Is `needle` SET in this file -- a config file holding it, or an assignment line carrying it?"""
    if not needle or needle not in body:
        return False
    if is_config_path(path):
        return True
    return any(needle in line and _ASSIGNMENT_LINE.match(line) for line in body.splitlines())


@dataclass
class EmitterScan:
    """Every emitter of every needle found in one bounded walk. `complete` is False when a bound
    stopped the walk: a needle with no emitter found is then UNKNOWN, never "nowhere"."""

    found: dict = field(default_factory=dict)
    configured: dict = field(default_factory=dict)
    complete: bool = True


# The walk is over the node's evaluated workdir, at settle, in a worker thread. A checkpoint
# directory can hold thousands of shards, so the bounds count ENTRIES as well as bytes read; a
# link is never followed (a data mount is a link and is not code).
_SCAN_MAX_ENTRIES = 50_000
_SCAN_MAX_FILES = 5_000
_SCAN_MAX_FILE_BYTES = 2 << 20
_SCAN_MAX_TOTAL_BYTES = 64 << 20
_SCAN_SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv", "venv",
                             "site-packages", ".mypy_cache", ".pytest_cache", ".tox"})


def _scan_file(scan: EmitterScan, rel: str, body: str, needles) -> None:
    for needle in needles:
        hits = emitters_in_file(rel, body, needle)
        if hits:
            scan.found.setdefault(needle, []).extend(hits)
        if config_occurrences(rel, body, needle):
            scan.configured.setdefault(needle, []).append(rel)


def tree_texts(root, prefix: str = "") -> tuple:
    """`({rel: text}, complete)`: every code or config file under `root` (`prefix/` before each
    name), within the `_SCAN_MAX_*` bounds -- `complete` is False past one. Links are never followed
    and dot/vendor directories are skipped. Never raises."""
    out: dict = {}
    root = str(root)
    entries = files = total = 0
    complete = True
    pre = f"{prefix.rstrip('/')}/" if prefix and prefix != "." else ""
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in _SCAN_SKIP_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                entries += 1
                if entries > _SCAN_MAX_ENTRIES:
                    return out, False
                full = os.path.join(dirpath, name)
                rel = pre + os.path.relpath(full, root).replace(os.sep, "/")
                lowered = name.lower()
                suffixed_code = lowered.endswith(CODE_SUFFIXES) or lowered in EXECUTED_MANIFESTS
                if not (suffixed_code or "." not in lowered or is_config_path(rel)):
                    continue
                try:
                    st = os.lstat(full)
                except OSError:
                    if suffixed_code:
                        complete = False
                    continue
                if not _stat.S_ISREG(st.st_mode) or st.st_size > _SCAN_MAX_FILE_BYTES:
                    # A CODE file this walk does not read -- a link, or one past the size bound --
                    # may hold the printer: the scan is then incomplete, and a marker with no printer
                    # found is UNKNOWN (blocked), never "nowhere" (critic: a 2 MB module skipped with
                    # complete=True turned a TP2 into a config-only WARN).
                    if suffixed_code:
                        complete = False
                    continue
                raw = read_bounded_regular_file(full, _SCAN_MAX_FILE_BYTES)
                if raw is None and suffixed_code:
                    complete = False
                if raw is None or not (is_code_path(rel, raw[:2]) or is_config_path(rel)):
                    continue
                files += 1
                total += len(raw)
                if files > _SCAN_MAX_FILES or total > _SCAN_MAX_TOTAL_BYTES:
                    return out, False
                out[rel] = raw.decode("utf-8", "replace")
    except OSError:
        return out, False
    return out, complete


def scan_emitters(workdir, needles) -> EmitterScan:
    """Every code file under `workdir` (tests excluded) that could print one of `needles`. Bounded
    (`tree_texts`): past a bound `complete` is False. Never raises."""
    needles = [n for n in dict.fromkeys(needles or ()) if n]
    if not needles:
        return EmitterScan()
    texts, complete = tree_texts(workdir)
    return scan_texts(texts, needles, complete=complete)


def scan_texts(files, needles, *, complete: bool = True) -> EmitterScan:
    """`scan_emitters` over an in-memory `{path: text}` (the declaration-time lint's tree)."""
    scan = EmitterScan(complete=complete)
    needles = [n for n in dict.fromkeys(needles or ()) if n]
    for rel, body in sorted((files or {}).items()):
        if isinstance(body, str) and (is_code_path(rel, body[:2].encode("utf-8", "replace"))
                                      or is_config_path(rel)):
            _scan_file(scan, str(rel).replace("\\", "/"), body, needles)
    return scan


# ---------------------------------------------------------------- an assignment the change itself made
#
# On a CONFIG-ONLY change, a marker spelled `NAME=value` whose value a config file THIS change
# touched assigns is ALWAYS the `env` entry `{"kind": "env", "name": NAME, "equals": value, "file": that config}` -- at the
# declaration lint and again at settle, whatever else in the tree contains the same text. The
# declared intent is "this value is set", and the static env check verifies exactly that. A code
# literal that merely contains the same text proves nothing more: minionerec-lora-v1 node 2,
# 2026-10-01, where `MiniOneRec/sft_resume.py` holds `why_off = "SFT_RESUME_EVERY_MIN=0"` -- a reason
# string stored in a dict, never printed on the run's path -- which the emitter scan reads as an
# EXISTING printer, so the node's config-only fix would have been blocked as TP2. TP2 (a flag that
# should enable an existing path with a silent fallback) is still caught by a log marker the PATH
# prints, which stays blocking. A value the touched config does NOT assign (or no touched config
# assigns the name at all) is left a `log` entry, and the emitter rules apply to it unchanged -- as
# they do to EVERY marker of a CODE change, where the same text is what the new code may print only
# when its path runs (critic probe: `USE_NEW=1` printed by a guarded new path behind a swallowed
# fallback, read as "the value is set", scored TP1). The callers gate on the change class.
_ASSIGNMENT_MARKER = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(\S.*)")


def config_assignment_entry(text, configs) -> Optional[dict]:
    """The `env` entry an assignment-shaped marker `text` normalizes to, or None. `configs` is
    `{path: text}` of the config files the node's change touched; the first (by path) whose
    effective LAST assignment of NAME equals the value -- `env_assignment`, the env check's own
    parsing -- names the file."""
    if not isinstance(text, str):
        return None
    m = _ASSIGNMENT_MARKER.fullmatch(text.strip())
    if not m:
        return None
    name, value = m.group(1), m.group(2).strip()
    for path in sorted(configs or {}):
        body = configs[path]
        if not isinstance(body, str) or not is_config_path(path):
            continue
        got = env_assignment(body, name)
        if got is not None and got == _unquote(value):
            entry = normalize_entry({"kind": KIND_ENV, "name": name, "equals": value, "file": path})
            if entry is not None:
                return entry
    return None


def normalize_config_assignments(entries, configs) -> tuple:
    """`(entries, rewritten)`: every `log` text entry `config_assignment_entry` recognizes replaced by
    its `env` entry (deduplicated, `normalize_entries` bounds), and the marker texts it replaced."""
    out, rewritten = [], []
    for entry in entries or ():
        env = (config_assignment_entry(entry.get("text"), configs)
               if entry.get("kind") == KIND_LOG else None)
        if env is not None:
            rewritten.append(entry["text"])
            out.append(env)
        else:
            out.append(entry)
    return normalize_entries(out), rewritten


def touched_configs(workdir, changed_paths) -> dict:
    """`{path: text}` of the changed paths that are config files, read from `workdir` (bounded,
    link-free, contained); an unreadable one is left out."""
    from looplab.core.pathsafe import contained_member
    out = {}
    for rel in sorted(changed_paths or ()):
        if not is_config_path(rel) or rel in CHANGE_CLASS_IGNORED:
            continue
        target = contained_member(workdir, rel)
        raw = read_bounded_regular_file(target, _ENV_FILE_BYTES) if target is not None else None
        if raw is not None:
            out[rel] = raw.decode("utf-8", "replace")
    return out


# ---------------------------------------------------------------- what the node changed

def node_change(node, parents=()) -> tuple:
    """`(changed_paths, code_changed)` of `node` against its parents -- or against the base tree
    when it has none (`files` is the node's overlay on that tree). A path is changed when no parent
    holds the same bytes under it; the activation manifest and the idea report are the engine's
    declarations and never count. `node.code` is the solution file on the toy/dataset tasks."""
    files = dict(getattr(node, "files", None) or {})
    deleted = set(getattr(node, "deleted", None) or [])
    code = getattr(node, "code", None) or ""
    parents = [p for p in (parents or ()) if p is not None]
    if not parents:
        changed = set(files) | deleted
        code_changed = bool(code.strip())
    else:
        changed = {p for p, b in files.items()
                   if all((getattr(par, "files", None) or {}).get(p) != b for par in parents)}
        changed |= {d for d in deleted
                    if all(d not in set(getattr(par, "deleted", None) or []) for par in parents)}
        code_changed = all(code != (getattr(par, "code", None) or "") for par in parents)
    changed -= CHANGE_CLASS_IGNORED
    if code_changed:
        changed.add(SOLUTION_FILE)
    return frozenset(str(p).replace("\\", "/") for p in changed), bool(code_changed)


def change_class(changed_paths, code_changed: bool = False) -> str:
    """`config_only` when every changed path is a config file (`CONFIG_SUFFIXES`) and the solution
    code did not move; `code` otherwise -- a `.sh` is code, and so is anything that is neither."""
    paths = [p for p in (changed_paths or ()) if p not in CHANGE_CLASS_IGNORED and p != SOLUTION_FILE]
    if code_changed:
        return CHANGE_CODE
    return CHANGE_CONFIG_ONLY if all(is_config_path(p) for p in paths) else CHANGE_CODE


def changed_code_digest(workdir, changed_paths) -> str:
    """A digest of the node's CHANGED files as they stand in `workdir` -- what re-check without
    re-run compares, so code the evaluation itself rewrote is never taken for the code that ran."""
    h = hashlib.sha256()
    for rel in sorted(p for p in (changed_paths or ()) if p not in CHANGE_CLASS_IGNORED):
        h.update(rel.encode("utf-8", "replace") + b"\0")
        raw = read_bounded_regular_file(Path(workdir) / rel, _SCAN_MAX_FILE_BYTES + 1)
        h.update(b"<absent>" if raw is None else hashlib.sha256(raw).digest())
    return h.hexdigest()


# ---------------------------------------------------------------- the verdict matrix

ACTIVATION_MODES = ("off", "strict", "graded")
ACTIVATION_GATES = ("audit", "gate")
VERDICT_OK, VERDICT_WARN, VERDICT_BLOCK = "ok", "warn", "block"
GRADES = ("strong", "medium", "weak")
# The causes a verdict names, per entry. `activation_unverifiable` is the WARN: a log marker nothing
# anywhere could print, on a config-only change.
CAUSE_TP1 = "emitter_in_changed_code_not_printed"
CAUSE_TP2 = "emitter_in_existing_code_not_printed"
CAUSE_TP3 = "no_emitter_on_code_change"
CAUSE_UNKNOWN = "emitter_unknown_not_printed"
CAUSE_UNVERIFIABLE = "activation_unverifiable"
CAUSE_NOT_PRINTED = "not_printed"
CAUSE_ENV = "env_not_satisfied"
CAUSE_FILE = "file_not_satisfied"
# The block causes that are DECLARATION errors -- the marker named text no code prints (TP3), or
# text whose printer nobody could establish -- and so the only ones re-check without re-run may
# answer (`engine/evaluate.py::_activation_recheck`). TP1/TP2 say the path did not RUN, and env/file
# that the value or artefact is not there: each of those needs a new evaluation.
RECHECKABLE_CAUSES = frozenset({CAUSE_TP3, CAUSE_UNKNOWN})


def _weakest(grades) -> str:
    grades = [g for g in grades if g in GRADES]
    return max(grades, key=GRADES.index) if grades else "strong"


@dataclass(frozen=True)
class ActivationVerdict:
    """What `check_activation` decided: `verdict` ok|warn|block, the weakest `grade` among the
    entries, the labels `missing`, and per label the `causes` -- plus the inputs a reader needs to
    re-derive it (`kinds`, `mode`, `change_class`)."""

    verdict: str
    grade: str
    missing: tuple = ()
    kinds: tuple = ()
    mode: str = "graded"
    change_class: str = CHANGE_CODE
    causes: tuple = ()

    def record(self) -> dict:
        """The `activation` record a WARN or a graded success carries on `node_evaluated`."""
        return {"verdict": self.verdict, "grade": self.grade,
                "missing": [m[:MAX_MARKER_CHARS] for m in self.missing[:MAX_MARKERS]],
                "kinds": list(self.kinds), "mode": self.mode, "change_class": self.change_class,
                "causes": {k[:MAX_MARKER_CHARS]: v for k, v in self.causes[:MAX_MARKERS]}}


def check_activation(entries, *, printed, emitters, changed=frozenset(),
                     change_cls: str = CHANGE_CODE, satisfied=None,
                     mode: str = "graded") -> ActivationVerdict:
    """THE DETERMINISTIC MATRIX -- no model decides, and no text the candidate printed is read for
    anything but the declared entries. Pure.

    `printed[label]` -- did the evaluation print this log entry; `emitters[label]` -- the places the
    evaluated tree could print it (`Emitter`s), or None when that is UNKNOWN (a regex, a bounded
    walk that stopped); `satisfied[label]` -- an env/file entry's predicate. Per entry:

      log, printed .................. ok; grade strong (a guarded print in changed code), medium
                                      (an existing printer, or none found), weak (only unguarded)
      log, not printed, `strict` .... BLOCK (the historical rule)
      log, not printed, `graded`:
        printer in changed code ..... BLOCK -- TP1, a fallback swallowed the new path
        printer in existing code .... BLOCK -- TP2, a flag that should enable it did not
        printer unknown ............. BLOCK -- the refusing direction
        no printer, code change ..... BLOCK -- TP3, declared, never written
        no printer, config-only ..... WARN `activation_unverifiable` -- the metric stands, flagged
      env / file satisfied .......... ok; grade weak (env: a static reading) / medium (file)
      env / file not satisfied ...... BLOCK
      none .......................... ok; grade weak

    A BLOCK anywhere blocks; else a WARN warns."""
    satisfied = satisfied or {}
    kinds, missing, causes, grades = [], [], [], []
    verdict = VERDICT_OK
    for entry in entries or ():
        kind = entry.get("kind")
        label = entry_label(entry)
        if kind not in kinds:
            kinds.append(kind)
        if kind == KIND_LOG:
            found = emitters.get(label)
            if printed.get(label):
                if found is None or not found:
                    grades.append("medium")
                elif any(e.path in changed and e.conditional for e in found):
                    grades.append("strong")
                elif any(e.path in changed for e in found):
                    grades.append("weak")
                elif any(e.conditional for e in found):
                    grades.append("medium")
                else:
                    grades.append("weak")
                continue
            missing.append(label)
            if mode != "graded":
                cause = CAUSE_NOT_PRINTED
            elif found is None:
                cause = CAUSE_UNKNOWN
            elif any(e.path in changed for e in found):
                cause = CAUSE_TP1
            elif found:
                cause = CAUSE_TP2
            elif change_cls == CHANGE_CODE:
                cause = CAUSE_TP3
            else:
                cause = CAUSE_UNVERIFIABLE
            causes.append((label, cause))
            if cause == CAUSE_UNVERIFIABLE:
                grades.append("weak")
                if verdict == VERDICT_OK:
                    verdict = VERDICT_WARN
            else:
                verdict = VERDICT_BLOCK
        elif kind in (KIND_ENV, KIND_FILE):
            if satisfied.get(label):
                grades.append("weak" if kind == KIND_ENV else "medium")
                continue
            missing.append(label)
            causes.append((label, CAUSE_ENV if kind == KIND_ENV else CAUSE_FILE))
            verdict = VERDICT_BLOCK
        else:
            grades.append("weak")
    return ActivationVerdict(verdict=verdict, grade=_weakest(grades), missing=tuple(missing),
                             kinds=tuple(kinds), mode=mode, change_class=change_cls,
                             causes=tuple(causes))


def _fresh_logs(workdir, since: Optional[float], snapshot=None, engine_logs=None,
                spans: Optional[list] = None) -> list:
    """What THIS attempt wrote to the `*.log` files in `workdir` — both ends of each — newest first.

    `snapshot` is the attempt-start cursor set (`engine/eval_log_plan.py::snapshot_training_logs`) and
    `attempt_byte_floor` the one boundary the watchdogs and the repair judge already read at: a stage
    that RE-RAN appends to its log (`runtime/sandbox.py::_tee_drain`), so an earlier attempt's marker
    line sits in a file this attempt also wrote, fresh by mtime, and it vouched for a path the scored
    run never took (critic crit_v51 F1b / crit_v52 F2, driven: the scored checkpoint written by a
    fallback that printed no marker). A log whose boundary cannot be established is not read — no
    credit, the refusing direction. `since` (the attempt's start) still drops a whole log last written
    before it; with no snapshot it is the only floor, and the appended case is then credited.
    A stage this attempt REUSED wrote nothing, so its earlier markers are not credited either way —
    doc 69, 69.10b.

    `engine_logs` names the logs the ENGINE appends to — the case-folded basenames of this attempt's
    `EvalLogPlan.roles`: `setup.log`, one `<stage>.log` per resolved stage, or `eval.log` — and only
    those are read past the cursor. The engine never truncates one, so a boundary that still matches
    is taken as an append — which a CANDIDATE writing a file of the same name defeats: a Python
    `logging` handler on `train.log` in mode "w" under a stage named `train` shares the engine's log, its byte-identical rewrite matches the old boundary, and the marker it printed
    before the old end is not credited (crit_v56 F3, driven end to end: `inert_path` where the
    candidate's own `own.log` scores). No size, identity or probe tells that rewrite from an append;
    it stays open in doc 69 69.10b. A log the CANDIDATE writes is read whole from the `since` floor: rewritten in place
    ('w' mode, same inode) with deterministic output, its bytes at the old boundary match, the probe
    reads the rewrite as an append, and the marker this attempt really printed was dropped — a real
    metric withheld as `inert_path` (crit_v55 A1, driven). The cost is the old one for that file: a
    candidate that APPENDS to its own log across attempts gets its earlier line credited, and the
    marker contract is the stage's stdout and stderr, which the engine's own logs carry. None =
    every log is taken as the engine's.

    `spans`, when given, receives one `LogSpan` per log read: WHICH bytes were this attempt's
    (identity, floor, end). Re-check without re-run (`read_log_spans`) re-reads exactly those bytes
    after a manifest-only repair, so a later append can never vouch for the attempt that failed."""
    out = []

    def _mtime(path) -> float:
        # PER ENTRY (critic 2026-09-30, crit_v51 F7): the key stat-ed every entry inside the
        # listing's one `try`, so a single dangling `latest.log` link emptied the whole listing and
        # every declared marker read as missing. An entry that cannot be stat-ed sorts last and is
        # skipped by the loop's own `stat` below.
        try:
            return path.stat().st_mtime
        except OSError:
            return float("-inf")

    try:
        entries = sorted(Path(workdir).glob("*.log"), key=_mtime, reverse=True)
    except OSError:
        return out
    for path in entries:
        try:
            st = path.stat()
            if since is not None and st.st_mtime + 1.0 < float(since):
                continue
            # The candidate's own file: no link, no FIFO — `x.log` as a FIFO blocked this read on
            # the event loop (critic 2026-09-26, driven) — and the size bound read off the SAME entry.
            with open_untrusted_regular(path) as fh:
                size = os.fstat(fh.fileno()).st_size
                cursor = snapshot is not None and (engine_logs is None
                                                   or _log_name_key(path.name) in engine_logs)
                floor = attempt_byte_floor(fh, path, snapshot) if cursor else 0
                if floor is None:
                    continue
                fh.seek(floor)
                if spans is not None:
                    spans.append(LogSpan(str(path), _identity(os.fstat(fh.fileno())), floor, size))
                if size - floor <= 2 * _MAX_LOG_BYTES:
                    out.append(fh.read(2 * _MAX_LOG_BYTES).decode("utf-8", "replace"))
                    continue
                head = fh.read(_MAX_LOG_BYTES)
                fh.seek(size - _MAX_LOG_BYTES)
                out.append(head.decode("utf-8", "replace"))
                out.append(fh.read(_MAX_LOG_BYTES).decode("utf-8", "replace"))
        except OSError:
            continue
    return out


def missing_markers(markers: Iterable, *, texts: Iterable[str] = (), workdir=None,
                    since: Optional[float] = None, snapshot=None, engine_logs=None,
                    spans: Optional[list] = None) -> list:
    """The declared markers that appear NOWHERE the evaluation printed: the captured streams in
    `texts`, then what this attempt wrote to the logs in `workdir` (`_fresh_logs`, which says what
    `snapshot` and `engine_logs` bound). Exact substring, case-sensitive -- the node wrote the line
    and named it, so there is nothing to interpret. A typed `log` entry is accepted beside a bare
    string (a `regex` one matched per line, `log_entry_seen`); the answer names each missing one as
    it was passed in."""
    markers = [m for m in markers if m]
    if not markers:
        return []

    def _entry(m):
        return m if isinstance(m, dict) else {"kind": KIND_LOG, "text": m}

    haystacks = [t for t in texts if isinstance(t, str) and t]
    missing = [m for m in markers if not log_entry_seen(_entry(m), haystacks)]
    if missing and workdir is not None and os.path.isdir(str(workdir)):
        logs = _fresh_logs(workdir, since, snapshot, engine_logs, spans)
        missing = [m for m in missing if not log_entry_seen(_entry(m), logs)]
    return missing


@dataclass(frozen=True)
class LogSpan:
    """The bytes of ONE log one attempt wrote: `[floor, end)` of the file whose identity was
    `identity` when the attempt settled."""

    path: str
    identity: Optional[tuple]
    floor: int
    end: int


def _identity(st) -> Optional[tuple]:
    # The cursor's own "same file?" rule (`eval_log_plan._file_identity`), never a second spelling.
    return _file_identity(st)


def attempt_log_texts(workdir, since: Optional[float], snapshot=None, engine_logs=None) -> tuple:
    """`(texts, spans)`: what this attempt wrote to the logs in `workdir` (`_fresh_logs`) and the
    `LogSpan`s it was read from."""
    spans: list = []
    if workdir is None or not os.path.isdir(str(workdir)):
        return [], spans
    return _fresh_logs(workdir, since, snapshot, engine_logs, spans), spans


def read_log_spans(spans) -> Optional[list]:
    """The texts of `spans` read AGAIN, with the windows `_fresh_logs` reads -- or None when any one
    of them can no longer be established: the file was replaced (another identity), truncated below
    the span's end, or cannot be opened. None means "no re-check": the refusing direction."""
    out = []
    for span in spans or ():
        try:
            with open_untrusted_regular(span.path) as fh:
                st = os.fstat(fh.fileno())
                if span.identity is not None and _identity(st) != span.identity:
                    return None
                if st.st_size < span.end or span.floor > span.end:
                    return None
                fh.seek(span.floor)
                if span.end - span.floor <= 2 * _MAX_LOG_BYTES:
                    out.append(fh.read(span.end - span.floor).decode("utf-8", "replace"))
                    continue
                out.append(fh.read(_MAX_LOG_BYTES).decode("utf-8", "replace"))
                fh.seek(span.end - _MAX_LOG_BYTES)
                out.append(fh.read(_MAX_LOG_BYTES).decode("utf-8", "replace"))
        except OSError:
            return None
    return out

