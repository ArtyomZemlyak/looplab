"""A positive source pin must be held by CODE, not by prose (review 2026-09-22, TST-05).

`assert "<literal>" in inspect.getsource(f)` asks whether production source SAYS something, and a
comment or a docstring says things as readily as code does. Measured on 2026-09-23 before this file
existed, two ways that agree: a pytest plugin that recorded the haystack of every `in`/`.index()` in
the 1,010 test functions a static nominator picked (1,082 run), mapped back to repository files,
found 555 positive pins over repository Python and 65 whose literal occurs there ONLY in comments
or docstrings; this census, which evaluates instead of running, found 67 (61 shared; the plugin's
four others read `__doc__` on purpose or slice by an `.index()` this census reports instead, and
the census found six the nominator never ran). The review's grep-level estimate was 41. Such a pin
guards nothing — delete the code, keep the comment, stay green — and breaks when someone edits the
comment. 17 of the 67 became checks of the behaviour or of the docstring they meant, or were
deleted; the 50 left are declared in `DOCUMENTATION_PINS`.

HOW. Every `assert` in `tests/test_*.py` that reads repository source (and every standalone
`.index()`/`.rindex()`, which raises when its literal is absent) is evaluated TWICE by a small
interpreter: in the FULL world a source read returns the text `inspect.getsource` / `read_text`
return, in the CODE world it returns `_source_scan.code_text` of it. Held in full and failed in code
means held by prose, and every positive term that fails alone in code is named. The interpreter
models only what a pin needs — a name is its latest binding at or before the line that reads it;
imports are real; `inspect`, `pathlib` and `open` are the only world-dependent readers; str/bytes/
list methods, slices, f-strings, `re`, comprehensions and helpers of the test module are evaluated;
enclosing `for` loops and `parametrize` rows are enumerated. Anything else is UNKNOWN, and UNKNOWN
never produces a finding: a budget that guessed would be a budget nobody trusts.

OUT OF SCOPE by construction: a pin that reads `__doc__` (or `ast.get_docstring`) reads the
docstring on purpose, so its haystack is not source; negative pins (`not in`) stay substrings —
what must not come back is the TEXT (CLAUDE.md); a pin over docs, data or test files.

WHAT IS KEPT ON PURPOSE is `DOCUMENTATION_PINS`: prose pins in tests whose OWN docstring names the
prose as the subject, each row quoting that sentence. The list is SHRINK-ONLY — a row whose pin is
gone must be deleted. A new prose pin either becomes a check of the behaviour, reads the docstring it
means, or declares itself here with a sentence of its own test's docstring.
"""
from __future__ import annotations

import ast
import builtins
import functools
import gc
import importlib
import inspect
import io
import os
import re
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path, PurePath

import _source_scan
from _source_scan import code_text

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"


# ------------------------------------------------------------------------------ the allow-list
# `test -> (a sentence of THAT test's docstring naming the prose as its subject, {pinned literals})`.
# A literal is the pinned string, or — when no single term fails alone in the code reading (an
# ORDER or a count that only prose makes true) — the assert's own text. 50 terms in 21 tests on
# 2026-09-23; every other prose pin the census found was converted or deleted (review 2026-09-22,
# TST-05). The count is the dict's, not this comment's.
DOCUMENTATION_PINS: dict[str, tuple[str, frozenset]] = {
    "tests/test_a_pinned_band_records_the_evidence_it_rests_on.py::"
    "test_the_reason_pagerank_waited_is_still_on_the_page": (
        "Записка о том, почему полосу нельзя было пришпилить при одной пробе, стоит рядом",
        frozenset({"0.99296 is outside 0.993-0.993 by rounding alone", "rounded\n    # OUTWARD"})),
    "tests/test_candidate_installs_stay_out_of_the_venv.py::"
    "test_the_reason_is_recorded_where_the_next_reader_will_look": (
        "The subject is the COMMENT at the env block",
        frozenset({"evaluate_results.py:266", "cutcounter", "156.4328"})),
    "tests/test_card_kind_divergence.py::test_the_field_comment_no_longer_asserts_ONE_call": (
        "The comment is the wire contract readers open",
        frozenset({"THEY ARE NOT ONE\n    # CALL"})),
    "tests/test_claim_key.py::test_the_one_identity_is_documented_where_a_reviewer_reads_them": (
        "the table is checked for the count it claims",
        frozenset({"THE ONE IDENTITY", "len(table) == 1"})),
    "tests/test_core_contracts.py::"
    "test_the_docstring_no_longer_claims_a_monkeypatch_seam_it_does_not_provide": (
        "The subject is `llm.py`'s re-export COMMENT",
        frozenset({"Patch the OWNING module", "silent no-op"})),
    "tests/test_dead_surface.py::test_the_deletion_says_why_so_it_is_not_reintroduced": (
        "The subject is the COMMENT in `perm_modes` citing why `decide` was deleted",
        frozenset({"doc 25 TO-10"})),
    "tests/test_dead_surface.py::test_the_forwarding_rule_is_written_down": (
        "The subject is the rule WRITTEN in `agent.py`'s comment",
        frozenset({"NOT auto-forwarded"})),
    "tests/test_digest_and_number_contracts.py::"
    "test_the_one_spelling_claim_is_scoped_to_what_it_actually_owns": (
        "The claim now names its own job — COERCING parse — and the map names the rest.",
        frozenset({"fitness.is_usable_metric", "comparison.finite_measurement",
                   "llm._safe_token_count", "tracing._token_int"})),
    "tests/test_digest_and_number_contracts.py::test_the_profiler_keeps_the_reasons_written_down": (
        "The shared predicate cannot carry that, so moving the code must not lose it.",
        frozenset({"OverflowError", "degrades to categorical"})),
    "tests/test_file_identity_tiers.py::"
    "test_each_declared_variant_names_the_canonical_it_departs_from": (
        "a narrower tuple whose reader cannot find what it narrowed is not.",
        frozenset({"atomicio.file_identity"})),
    "tests/test_kill_bar_is_not_a_ladder.py::"
    "test_the_comment_no_longer_claims_the_streak_is_confidence_gated": (
        "A comment that misdescribes a kill gate is a defect, not a typo",
        frozenset({"Phase 3 arming state.", "at ANY confidence"})),
    "tests/test_kill_bar_is_not_a_ladder.py::"
    "test_the_refusal_carries_the_two_metrics_that_refute_the_ladder": (
        "A marker that lost them would be an assertion, not a measurement",
        frozenset({"0.715142", "0.790898", "not_learning"})),
    "tests/test_kill_bar_is_not_a_ladder.py::test_the_refusal_is_recorded_where_the_kill_is_decided": (
        "What this asserts is that the marker sits at the DECISION, not in a docstring three files "
        "away.",
        frozenset({"broken-verdict-ladder", "DECLINED[", "measured:",
                   "docs/guide/llm-and-agents.md"})),
    "tests/test_mlebench.py::"
    "test_synthetic_held_out_labels_are_reconstructible_from_the_mounted_split": (
        "It exists so the caveat in `MLEBenchTask.host_graded` and `docs/guide/tasks.md` cannot rot "
        "into a guarantee",
        frozenset({"NOT a confidentiality boundary FOR THIS TASK"})),
    "tests/test_money_that_has_stopped_buying_readings.py::"
    "test_the_reason_is_where_it_was_missed_the_first_time": (
        "Если эта фраза оттуда исчезнет, следующий читатель придёт к тому же выводу заново.",
        frozenset({"collapses"})),
    "tests/test_options_divergence.py::test_curation_rationale_discloses_synchronous_finalize_latency": (
        "The subject is the curation RATIONALE COMMENTS in `config.py` and `finalize.py`",
        frozenset({"calls run synchronously during finalize", "calls run synchronously"})),
    "tests/test_options_divergence.py::"
    "test_part_iv_comments_distinguish_fold_storage_from_live_steering": (
        "The subject is the Part IV COMMENTS (and `tag_text_llm`'s docstring)",
        frozenset({"product `Settings` is ON", "can steer later proposals", "change admission",
                   "configured live-client call is synchronous"})),
    "tests/test_options_divergence.py::test_part_iv_v_default_rationale_discloses_behavior_and_cost": (
        "The subject is the RATIONALE COMMENT on the Part IV/V defaults in `config.py`",
        frozenset({"explicit experimental product choice", "change graded-novelty admission",
                   "paid LLM work"})),
    "tests/test_probe_summary_answers_the_standing_checklist.py::"
    "test_the_build_duration_comment_claims_no_gap_it_does_not_have": (
        "A guide that names a gap invites the next reader to treat a point inside it as a fault",
        frozenset({"first `plan_step` to first `node_evaluated`", "ROUGH, NOT A BAND"})),
    "tests/test_the_zero_band_is_measured_not_frozen.py::"
    "test_the_comment_no_longer_freezes_a_count_and_a_band": (
        "Проверять надо не упоминание, а то, что старое утверждение больше не ВЫСКАЗЫВАЕТСЯ",
        frozenset({"4.2 s to 60.9 s", "outgrown by the corpus", "All 12 corpus zeros are the",
                   "It read"})),
    "tests/test_watchdog_stage_scope.py::test_h4_neither_bit_claims_the_node_actually_stopped": (
        "so the registry note must describe a DECISION and a CLAIM, and point at the node's own "
        "terminal for the outcome.",
        frozenset({"stop_decided", "kill_superseded_by", "node's single terminal", "superseded",
                   "aborted"})),
}


# ------------------------------------------------------------------------------ values
class _Opaque:
    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return f"<{self.name}>"


UNKNOWN = _Opaque("unknown")        # not modelled: never produces a finding
RAISED = _Opaque("raised")          # the modelled expression raised (`.index()` of an absent literal)


def _opaque(value) -> bool:
    return value is UNKNOWN or value is RAISED


class Tainted(str):
    """A str read off repository source; `origins` names what it was read from."""
    origins: frozenset = frozenset()


class TaintedBytes(bytes):
    origins: frozenset = frozenset()


class TaintedList(list):
    """`splitlines()` of a source: ONE origin set for the list, each item tainted as it is taken out
    rather than ten thousand wrappers built up front."""
    origins: frozenset = frozenset()

    def __getitem__(self, index):
        got = list.__getitem__(self, index)
        if isinstance(index, slice):
            out = TaintedList(got)
            out.origins = self.origins
            return out
        return _taint(got, self.origins)

    def __iter__(self):
        for item in list.__iter__(self):
            yield _taint(item, self.origins)


def _origins(*values) -> frozenset:
    out: set = set()
    for value in values:
        if isinstance(value, (Tainted, TaintedBytes, TaintedList)):
            out |= value.origins
        elif isinstance(value, (list, tuple)) and len(value) <= 20000:
            out |= _origins(*value)
        elif isinstance(value, dict) and len(value) <= 20000:
            out |= _origins(*value.keys(), *value.values())
    return frozenset(out)


def _taint(value, origins):
    if not origins:
        return value
    if isinstance(value, str):
        out = Tainted(value)
    elif isinstance(value, bytes):
        out = TaintedBytes(value)
    elif isinstance(value, list):
        if all(isinstance(v, (str, bytes)) for v in value):
            out = TaintedList(_plain(v) for v in value)
        else:
            return [_taint(v, origins) for v in value]
    elif isinstance(value, tuple):
        return tuple(_taint(v, origins) for v in value)
    else:
        return value
    out.origins = frozenset(origins)
    return out


def _plain(value):
    """The same value with every tainted string turned back into a plain one."""
    if isinstance(value, Tainted):
        return str.__str__(value)
    if isinstance(value, TaintedBytes):
        return bytes(value)
    if isinstance(value, TaintedList):
        return list(list.__iter__(value))
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_plain(v) for v in value)
    return value


@dataclass(frozen=True)
class Handle:
    """`open(path)`; only `read()`/`readlines()` are modelled."""
    path: Path


@dataclass(frozen=True)
class Helper:
    """A function of a test module, called by inlining its return expressions."""
    node: ast.AST
    module: "TestModule"


@dataclass(frozen=True)
class BoundMethod:
    receiver: object
    name: str


# ------------------------------------------------------------------------------ the two worlds
class Reader:
    """Source reads in either world, for one repository root. Everything is cached for the life of
    ONE census and dropped with it."""

    def __init__(self, repo: Path):
        self.repo = repo.resolve()
        self._text: dict = {}
        self._code: dict = {}
        self._starts: dict = {}
        self._objects: dict = {}

    def label(self, path) -> str | None:
        """`looplab/core/x.py` for repository Python outside tests/, else None (not a pin target)."""
        try:
            rel = Path(path).resolve().relative_to(self.repo)
        except (ValueError, OSError, TypeError):
            return None
        if rel.suffix != ".py" or rel.parts[0] == "tests":
            return None
        return rel.as_posix()

    def text(self, path: Path):
        key = str(path)
        if key not in self._text:
            try:
                self._text[key] = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                self._text[key] = None
        return self._text[key]

    def code(self, text: str):
        if text not in self._code:
            try:
                self._code[text] = code_text(text)
            except ValueError:
                self._code[text] = None
        return self._code[text]

    def lines(self, text: str, first: int, count: int) -> str:
        starts = self._starts.get(text)
        if starts is None:
            starts = self._starts[text] = [0] + [m.end() for m in re.finditer("\n", text)]
        a = starts[first - 1] if first - 1 < len(starts) else len(text)
        b = starts[first - 1 + count] if first - 1 + count < len(starts) else len(text)
        return text[a:b]

    def in_world(self, text: str, label: str | None, world: str):
        if label is None:
            return text
        if world == "code":
            text = self.code(text)
            if text is None:
                return UNKNOWN
        return _taint(text, {label})

    def read_file(self, path, world: str):
        path = Path(path).resolve()
        text = self.text(path)
        if text is None:
            return RAISED
        return self.in_world(text, self.label(path), world)

    def _object_source(self, obj):
        key = id(obj)
        hit = self._objects.get(key)
        if hit is None or hit[0] is not obj:
            try:
                lines, first = inspect.getsourcelines(obj)
                got = (inspect.getsourcefile(obj), max(first, 1), "".join(lines))
            except (OSError, TypeError):
                got = None
            hit = self._objects[key] = (obj, got)
        return hit[1]

    def read_object(self, obj, world: str):
        """An object's source. The CODE world slices the FILE's code text — one tokenization per
        file however many of its functions are read — which `code_text` keeping line N on line N
        makes sound; an object whose source is not its file's slice is tokenized on its own."""
        got = self._object_source(obj)
        if got is None:
            return RAISED
        path, first, text = got
        label = self.label(path) if path else None
        if label is None:
            return text
        if not inspect.ismodule(obj):
            label = f"{label}::{getattr(obj, '__qualname__', getattr(obj, '__name__', '?'))}"
        if world == "full":
            return _taint(text, {label})
        whole = self.text(Path(path).resolve())
        count = text.count("\n") + (0 if text.endswith("\n") else 1)
        if whole is not None and self.lines(whole, first, count) == text:
            code = self.code(whole)
            return UNKNOWN if code is None else _taint(self.lines(code, first, count), {label})
        return self.in_world(text, label, world)

    def object_lines(self, obj):
        got = self._object_source(obj)
        return None if got is None else got[1]


# ------------------------------------------------------------------------------ test modules
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


class TestModule:
    __test__ = False                     # not a pytest class

    def __init__(self, path: Path, census: "Census"):
        self.path = path
        self.census = census
        self.text = path.read_text(encoding="utf-8-sig", errors="replace")
        self.tree = ast.parse(self.text, filename=str(path))
        self._tables: dict = {}
        self._scope_parent: dict = {node: self.tree for node in self.tree.body
                                    if isinstance(node, _SCOPES)}
        self._walked: set = set()
        self._sys_path: list | None = None
        self._walrus = ":=" in self.text

    def parent_scope(self, scope):
        """The scope enclosing *scope*: the module for a top-level def, else mapped lazily one
        top-level statement at a time — a lookup climbs out of a handful of nested defs."""
        got = self._scope_parent.get(scope)
        if got is not None:
            return got
        for top in self.tree.body:
            if id(top) in self._walked or not (top.lineno <= scope.lineno <= top.end_lineno):
                continue
            self._walked.add(id(top))
            if isinstance(top, _SCOPES):
                self._scope_parent[top] = self.tree
            stack = [(top, top if isinstance(top, _SCOPES) else self.tree)]
            while stack:
                node, current = stack.pop()
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, _SCOPES):
                        self._scope_parent[child] = current
                        stack.append((child, child))
                    else:
                        stack.append((child, current))
        return self._scope_parent.get(scope, self.tree)

    def table(self, scope) -> dict:
        """`{name: [(line, kind, payload), …]}` for every binding made directly in *scope*."""
        got = self._tables.get(id(scope))
        if got is not None:
            return got
        table: dict = {}

        def add(name, line, kind, payload):
            table.setdefault(name, []).append((line, kind, payload))

        if isinstance(scope, _SCOPES):
            args = scope.args
            for p in args.posonlyargs + args.args + args.kwonlyargs + [
                    a for a in (args.vararg, args.kwarg) if a is not None]:
                add(p.arg, scope.lineno, "param", p)
            body = scope.body if isinstance(scope.body, list) else []
        else:
            body = scope.body
        stack = list(reversed(body))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                add(node.name, node.lineno, "def", node)
                continue
            # Bindings live in STATEMENTS, so only statements are descended into: walking every
            # expression node of every scope was a second of the census. A walrus is the one
            # binding an expression can make, and only a module that spells `:=` pays to find it.
            children = list(ast.iter_child_nodes(node))
            stack.extend(child for child in children
                         if isinstance(child, (ast.stmt, ast.excepthandler, ast.match_case)))
            if self._walrus:
                for child in children:
                    if isinstance(child, ast.expr):
                        for leaf in ast.walk(child):
                            if isinstance(leaf, ast.NamedExpr) and isinstance(leaf.target, ast.Name):
                                add(leaf.target.id, node.lineno, "assign", (leaf.target, leaf.value))
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    for leaf in ast.walk(target):
                        if isinstance(leaf, ast.Name):
                            add(leaf.id, node.lineno, "assign", (target, node.value))
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.value is not None:
                    add(node.target.id, node.lineno, "assign", (node.target, node.value))
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if isinstance(item.optional_vars, ast.Name):
                        add(item.optional_vars.id, node.lineno, "with", item.context_expr)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    add(alias.asname or alias.name.split(".")[0], node.lineno, "import", alias)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    add(alias.asname or alias.name, node.lineno, "from", (node, alias))
            else:
                # a loop target, an augmented assignment, an `except … as`, a global: a value this
                # interpreter does not follow, recorded so the name does not fall through to an
                # OUTER binding it is not.
                for name in _opaque_bindings(node):
                    add(name, node.lineno, "opaque", node)
        for rows in table.values():
            rows.sort(key=lambda row: row[0])
        self._tables[id(scope)] = table
        return table

    def sys_path(self) -> list[str]:
        """Directories this module puts on `sys.path` when imported (benchmarks/, tools/ …)."""
        if self._sys_path is None:
            self._sys_path = []
            for node in self.tree.body:
                call = node.value if isinstance(node, ast.Expr) else None
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr in ("insert", "append") and call.args
                        and ast.unparse(call.func.value) == "sys.path"):
                    value = Interp(self, "full").ev(call.args[-1], Frame(self, self.tree, BIG, {}))
                    if isinstance(value, (str, PurePath)):
                        self._sys_path.append(str(value))
        return self._sys_path


def _opaque_bindings(node) -> list[str]:
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return [leaf.id for leaf in ast.walk(node.target) if isinstance(leaf, ast.Name)]
    if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    if isinstance(node, ast.ExceptHandler) and node.name:
        return [node.name]
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return list(node.names)
    return []


@dataclass
class Frame:
    module: TestModule
    scope: ast.AST
    line: int
    overlay: dict
    hops: int = 0

    def at(self, line: int) -> "Frame":
        return Frame(self.module, self.scope, line, self.overlay, self.hops)

    def bind(self, names: dict) -> "Frame":
        return Frame(self.module, self.scope, self.line, {**self.overlay, **names}, self.hops)


BIG = 10 ** 9
MAX_HOPS = 30
MAX_ITEMS = 5000
FUEL = 20_000       # interpreter steps per site and world; a site that needs more is UNKNOWN


class OutOfFuel(Exception):
    """A site whose evaluation walks the whole test tree line by line: a scan, not a pin."""


def _import(name: str, extra: list[str]):
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 — a module this box cannot import is simply not modelled
        pass
    if not extra:
        return UNKNOWN
    saved = list(sys.path)
    try:
        sys.path[:0] = [p for p in extra if p not in sys.path]
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 — as above
        return UNKNOWN
    finally:
        sys.path[:] = saved


def _plain_object(value) -> bool:
    """Attributes are read only off modules, classes and functions — never off an instance, whose
    attribute can be a property with an effect."""
    return (inspect.ismodule(value) or inspect.isclass(value) or inspect.isroutine(value)
            or isinstance(value, (property, staticmethod, classmethod, functools.partial)))


_STR_METHODS = frozenset(n for n in dir(str) if not n.startswith("_"))
_BYTES_METHODS = frozenset(n for n in dir(bytes) if not n.startswith("_"))
_SEQ_METHODS = frozenset({"index", "count", "get", "keys", "values", "items", "copy"})
_PATH_ATTRS = frozenset({"parent", "parents", "name", "stem", "suffix", "parts", "anchor"})
_PATH_METHODS = frozenset({"joinpath", "with_name", "with_suffix", "with_stem", "resolve",
                           "absolute", "relative_to", "read_text", "read_bytes", "open", "exists",
                           "is_file", "is_dir", "glob", "rglob", "as_posix", "iterdir",
                           "is_relative_to", "expanduser", "match"})
_MATCH_METHODS = frozenset({"group", "groups", "start", "end", "span", "groupdict"})
_PATTERN_METHODS = frozenset({"search", "match", "fullmatch", "findall", "finditer", "sub", "subn",
                              "split"})
_PIN_METHODS = frozenset({"index", "rindex", "find", "rfind", "count"})
_PURE = frozenset({
    len, sorted, list, tuple, set, frozenset, min, max, sum, any, all, enumerate, zip, reversed,
    range, str, repr, int, float, bool, dict, abs, round, isinstance, hasattr, ord, chr,
    textwrap.dedent, textwrap.indent, inspect.cleandoc, os.path.join, os.path.dirname,
    os.path.basename, os.fspath, os.path.abspath, os.path.normpath, re.escape, re.search,
    re.match, re.fullmatch, re.findall, re.finditer, re.sub, re.subn, re.split, re.compile})
_OPS = {ast.Add: lambda x, y: x + y, ast.Sub: lambda x, y: x - y, ast.Mult: lambda x, y: x * y,
        ast.Div: lambda x, y: x / y, ast.FloorDiv: lambda x, y: x // y,
        ast.Mod: lambda x, y: x % y}
_CMP = {ast.Eq: lambda x, y: x == y, ast.NotEq: lambda x, y: x != y, ast.Lt: lambda x, y: x < y,
        ast.LtE: lambda x, y: x <= y, ast.Gt: lambda x, y: x > y, ast.GtE: lambda x, y: x >= y,
        ast.In: lambda x, y: x in y, ast.NotIn: lambda x, y: x not in y,
        ast.Is: lambda x, y: x is y, ast.IsNot: lambda x, y: x is not y}
_NATIVE_BUILTINS = {name: getattr(builtins, name) for name in (
    "len", "str", "int", "float", "bool", "sorted", "list", "tuple", "set", "min", "max", "sum",
    "any", "all", "repr", "enumerate", "zip", "range", "abs", "round", "reversed")}
_NATIVE_NODES = (ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.comprehension, ast.Name,
                 ast.Load, ast.Store, ast.Constant, ast.Compare, ast.BoolOp, ast.UnaryOp, ast.BinOp,
                 ast.IfExp, ast.Subscript, ast.Slice, ast.Tuple, ast.List, ast.JoinedStr,
                 ast.FormattedValue, ast.Call, ast.Attribute, ast.cmpop, ast.boolop, ast.unaryop,
                 ast.operator, ast.keyword)


@dataclass
class Term:
    """One pin-shaped read the evaluation made: `lit in src` or `src.<index|find|count>(lit)`."""
    op: str
    literal: object
    origins: frozenset
    node: ast.AST
    frame: Frame


class Interp:
    """Evaluates test-module expressions in one world, "full" or "code"."""

    def __init__(self, module: TestModule, world: str, record: bool = False):
        self.module = module
        self.world = world
        self.record = record
        self.reader = module.census.reader
        self.terms: list[Term] = []
        self.touched: set = set()
        self.fuel = [FUEL]              # a list, so a helper's interpreter spends the SAME budget
        self.cache: dict = {}
        self.busy: set = set()

    def child(self, module: TestModule) -> "Interp":
        """An interpreter for a helper of another test module, sharing every ledger."""
        other = Interp(module, self.world, self.record)
        other.terms, other.touched, other.fuel = self.terms, self.touched, self.fuel
        other.cache, other.busy = self.cache, self.busy
        return other

    def reset(self) -> None:
        self.terms = []
        self.touched = set()
        self.fuel[0] = FUEL

    # --------------------------------------------------------------------------------- names
    def lookup(self, name: str, fr: Frame):
        if name in fr.overlay:
            return fr.overlay[name]
        if fr.hops > MAX_HOPS:
            return UNKNOWN
        mod, scope, line, overlay = fr.module, fr.scope, fr.line, fr.overlay
        while True:
            rows = mod.table(scope).get(name)
            if rows:
                # Module level binds before any test runs, so its LAST binding is the one read.
                before = rows if scope is mod.tree else [r for r in rows if r[0] <= line]
                if before:
                    row = before[-1]
                    return self.binding(row, name, Frame(mod, scope, row[0] - 1, overlay,
                                                         fr.hops + 1))
            if scope is mod.tree:
                break
            parent = mod.parent_scope(scope)
            line = BIG if parent is mod.tree else getattr(scope, "lineno", BIG)
            scope, overlay = parent, {}
        if name == "__file__":
            return str(mod.path)
        return getattr(builtins, name, UNKNOWN)

    def binding(self, row, name: str, fr: Frame):
        line, kind = row[0], row[1]
        key = ((id(fr.module), id(fr.scope), name, line, kind)
               if fr.scope is fr.module.tree or not fr.overlay else None)
        if key is not None:
            if key in self.cache:
                return self.cache[key]
            if key in self.busy:
                return UNKNOWN
            self.busy.add(key)
        try:
            value = self._binding(row, name, fr)
        finally:
            if key is not None:
                self.busy.discard(key)
        if key is not None:
            self.cache[key] = value
        return value

    def _binding(self, row, name: str, fr: Frame):
        _line, kind, payload = row
        if kind == "assign":
            target, value = payload
            return _unpack(target, self.ev(value, fr), name)
        if kind == "def":
            return Helper(payload, fr.module) if isinstance(payload, _SCOPES[:2]) else UNKNOWN
        if kind == "import":
            full = payload.name if payload.asname else payload.name.split(".")[0]
            return _import(full, fr.module.sys_path())
        if kind == "from":
            return self.import_from(*payload, fr)
        if kind == "with":
            return self.ev(payload, fr)
        if kind == "param":
            return self.fixture(name, fr)
        return UNKNOWN

    def import_from(self, node: ast.ImportFrom, alias: ast.alias, fr: Frame):
        if node.level:
            return UNKNOWN
        modname = node.module or ""
        leaf = modname.split(".")[-1]
        tests = fr.module.census.tests
        if (modname in (leaf, f"tests.{leaf}") and leaf != "_source_scan"
                and (tests / f"{leaf}.py").is_file()):
            other = fr.module.census.module(tests / f"{leaf}.py")
            rows = other.table(other.tree).get(alias.name)
            if not rows:
                return UNKNOWN
            return self.child(other).binding(rows[-1], alias.name,
                                             Frame(other, other.tree, rows[-1][0] - 1, {},
                                                   fr.hops + 1))
        module = _import(modname, fr.module.sys_path())
        if _opaque(module):
            return UNKNOWN
        if hasattr(module, alias.name):
            return getattr(module, alias.name)
        return _import(f"{modname}.{alias.name}", fr.module.sys_path())

    def fixture(self, name: str, fr: Frame):
        """A parameter nothing bound: the module-level pytest fixture of that name, inlined."""
        mod = fr.module
        for _line, kind, node in mod.table(mod.tree).get(name, []):
            if kind == "def" and isinstance(node, ast.FunctionDef) and any(
                    "fixture" in ast.unparse(d) for d in node.decorator_list):
                return self.call_helper(Helper(node, mod), [], {}, fr)
        return UNKNOWN

    # --------------------------------------------------------------------------------- values
    def ev(self, node: ast.AST, fr: Frame):
        self.fuel[0] -= 1
        if self.fuel[0] < 0:
            raise OutOfFuel
        try:
            value = self._ev(node, fr)
        except RecursionError:
            return UNKNOWN
        if isinstance(value, (Tainted, TaintedBytes, TaintedList)):
            self.touched |= value.origins
        elif isinstance(value, (list, tuple)) and value and isinstance(value[0],
                                                                       (Tainted, TaintedBytes)):
            self.touched |= value[0].origins
        return value

    def _ev(self, node: ast.AST, fr: Frame):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return self.lookup(node.id, fr)
        if isinstance(node, ast.Attribute):
            return self.attribute(node, fr)
        if isinstance(node, ast.Call):
            return self.call(node, fr)
        if isinstance(node, ast.Compare):
            return self.compare(node, fr)
        if isinstance(node, ast.BoolOp):
            return self.boolop(node, fr)
        if isinstance(node, ast.Subscript):
            return self.subscript(node, fr)
        if isinstance(node, ast.BinOp):
            return self.binop(node, fr)
        if isinstance(node, ast.JoinedStr):
            return self.fstring(node, fr)
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return self.display(node, fr)
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            return self.comprehension(node, fr)
        if isinstance(node, ast.UnaryOp):
            value = self.ev(node.operand, fr)
            if _opaque(value):
                return value
            try:
                if isinstance(node.op, ast.Not):
                    return not value
                if isinstance(node.op, ast.USub):
                    return -value
            except TypeError:
                return UNKNOWN
            return UNKNOWN
        if isinstance(node, ast.IfExp):
            test = self.ev(node.test, fr)
            return test if _opaque(test) else self.ev(node.body if test else node.orelse, fr)
        if isinstance(node, ast.Dict):
            out = {}
            for key, value in zip(node.keys, node.values):
                key = UNKNOWN if key is None else self.ev(key, fr)
                if _opaque(key):
                    return UNKNOWN
                try:
                    out[key] = self.ev(value, fr)
                except TypeError:
                    return UNKNOWN
            return out
        if isinstance(node, ast.Slice):
            parts = []
            for part in (node.lower, node.upper, node.step):
                value = None if part is None else self.ev(part, fr)
                if _opaque(value):
                    return value
                parts.append(value)
            try:
                return slice(*parts)
            except TypeError:
                return UNKNOWN
        return UNKNOWN

    def display(self, node, fr: Frame):
        values: list = []
        for elt in node.elts:
            if isinstance(elt, ast.Starred):
                value = self.ev(elt.value, fr)
                if _opaque(value) or not isinstance(value, (list, tuple, set, frozenset)):
                    return UNKNOWN
                values.extend(value)
            else:
                values.append(self.ev(elt, fr))
        if isinstance(node, ast.Tuple):
            return tuple(values)
        if isinstance(node, ast.List):
            return values
        try:
            return set(values)
        except TypeError:
            return UNKNOWN

    def fstring(self, node, fr: Frame):
        out, found = [], set()
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value))
                continue
            value = self.ev(part.value, fr)
            if _opaque(value):
                return value
            spec = "" if part.format_spec is None else self.ev(part.format_spec, fr)
            if not isinstance(spec, str):
                return UNKNOWN
            try:
                if part.conversion == ord("r"):
                    value = repr(_plain(value))
                elif part.conversion == ord("s"):
                    value = str(_plain(value))
                out.append(format(_plain(value), spec))
            except Exception:  # noqa: BLE001 — what the real f-string would choke on: not modelled
                return UNKNOWN
            found |= _origins(value)
        return _taint("".join(out), found)

    def binop(self, node, fr: Frame):
        left = self.ev(node.left, fr)
        if _opaque(left):
            return left
        right = self.ev(node.right, fr)
        if _opaque(right):
            return right
        op = _OPS.get(type(node.op))
        if op is None or (isinstance(node.op, ast.Mult) and isinstance(right, int)
                          and right > 10 ** 5):
            return UNKNOWN
        try:
            result = op(left, right)
        except Exception:  # noqa: BLE001 — an operation the real test would raise on: not modelled
            return UNKNOWN
        return _taint(result, _origins(left, right)) if isinstance(result, (str, bytes)) else result

    def boolop(self, node, fr: Frame):
        conjunction = isinstance(node.op, ast.And)
        unknown, last = False, None
        for value_node in node.values:
            value = self.ev(value_node, fr)
            if value is RAISED:
                return RAISED
            if value is UNKNOWN:
                unknown = True
                continue
            last = value
            if (conjunction and not value) or (not conjunction and value):
                return value
        return UNKNOWN if unknown else last

    def compare(self, node, fr: Frame):
        left = self.ev(node.left, fr)
        if _opaque(left):
            return left
        for op, comparator in zip(node.ops, node.comparators):
            right = self.ev(comparator, fr)
            if _opaque(right):
                return right
            if (self.record and isinstance(op, (ast.In, ast.NotIn))
                    and isinstance(right, (Tainted, TaintedBytes))
                    and isinstance(left, (str, bytes))):
                self.terms.append(Term("in" if isinstance(op, ast.In) else "not in", left,
                                       right.origins, node, fr))
            try:
                if not _CMP[type(op)](left, right):
                    return False
            except Exception:  # noqa: BLE001 — a comparison the real test would raise on
                return UNKNOWN
            left = right
        return True

    def attribute(self, node, fr: Frame):
        base = self.ev(node.value, fr)
        if _opaque(base):
            return base
        name = node.attr
        if isinstance(base, PurePath):
            if name in _PATH_ATTRS:
                return getattr(base, name)
            return BoundMethod(base, name) if name in _PATH_METHODS else UNKNOWN
        if isinstance(base, str):
            return BoundMethod(base, name) if name in _STR_METHODS else UNKNOWN
        if isinstance(base, bytes):
            return BoundMethod(base, name) if name in _BYTES_METHODS else UNKNOWN
        if isinstance(base, (list, tuple, dict)):
            return BoundMethod(base, name) if name in _SEQ_METHODS else UNKNOWN
        if isinstance(base, Handle):
            return BoundMethod(base, name) if name in ("read", "readlines") else UNKNOWN
        if isinstance(base, re.Match):
            if name == "string":
                return base.string
            return BoundMethod(base, name) if name in _MATCH_METHODS else UNKNOWN
        if isinstance(base, re.Pattern):
            return BoundMethod(base, name) if name in _PATTERN_METHODS else UNKNOWN
        if _plain_object(base):
            try:
                return getattr(base, name)
            except Exception:  # noqa: BLE001 — a missing attribute is simply not modelled
                return UNKNOWN
        return UNKNOWN

    def subscript(self, node, fr: Frame):
        base = self.ev(node.value, fr)
        if _opaque(base):
            return base
        index = self.ev(node.slice, fr)
        if _opaque(index):
            return index
        if not isinstance(base, (str, bytes, list, tuple, dict, re.Match)) \
                and type(base).__name__ != "_PathParents":
            return UNKNOWN
        try:
            result = base[index]
        except Exception:  # noqa: BLE001 — an index the real test would raise on: not modelled
            return UNKNOWN
        return _taint(result, _origins(base)) if isinstance(result, (str, bytes)) else result

    def comprehension(self, node, fr: Frame):
        native = self.native(node, fr)
        if native is not None:
            return native
        frames = [fr]
        for gen in node.generators:
            grown = []
            for frame in frames:
                items = self.ev(gen.iter, frame)
                if _opaque(items):
                    return items
                try:
                    items = list(items)
                except TypeError:
                    return UNKNOWN
                if len(items) > MAX_ITEMS:
                    return UNKNOWN
                for item in items:
                    bound = _bind(gen.target, item)
                    if bound is None:
                        return UNKNOWN
                    inner = frame.bind(bound)
                    keep = True
                    for cond in gen.ifs:
                        verdict = self.ev(cond, inner)
                        if _opaque(verdict):
                            return UNKNOWN
                        if not verdict:
                            keep = False
                            break
                    if keep:
                        grown.append(inner)
            frames = grown
            if len(frames) > MAX_ITEMS:
                return UNKNOWN
        values = [self.ev(node.elt, frame) for frame in frames]
        if isinstance(node, ast.SetComp):
            try:
                return set(values)
            except TypeError:
                return UNKNOWN
        return values

    def native(self, node, fr: Frame):
        """Run a PURE comprehension as real Python: a line filter over a 3,000-line file is
        otherwise a 3,000-step interpretation. Pure means names, constants, operators, slices,
        f-strings and calls of str/list methods or `_NATIVE_BUILTINS` only, over plain data, so
        nothing it reaches has an effect. One whose ELEMENT is pin-shaped over source is left to the
        interpreter, which records the pins; a result computed from source keeps every origin."""
        if not _native_ok(node):
            return None
        targets = {leaf.id for gen in node.generators for leaf in ast.walk(gen.target)
                   if isinstance(leaf, ast.Name)}
        free = {leaf.id for leaf in ast.walk(node)
                if isinstance(leaf, ast.Name) and isinstance(leaf.ctx, ast.Load)} - targets
        env = {}
        for name in free:
            if (name in _NATIVE_BUILTINS and name not in fr.overlay
                    and not fr.module.table(fr.module.tree).get(name)):
                continue
            value = self.lookup(name, fr)
            if _opaque(value):
                return value
            if not _data(value):
                return None
            env[name] = value
        found = _origins(*env.values())
        if self.record and found and _pin_shaped(node.elt):
            return None
        code = self.module.census.compiled(node)
        try:
            result = eval(code, {"__builtins__": _NATIVE_BUILTINS},  # noqa: S307 — pure, above
                          {name: _plain(value) for name, value in env.items()})
            if not isinstance(result, (list, set)):
                result = list(result)
        except Exception:  # noqa: BLE001 — what the real test would raise on is not modelled
            return UNKNOWN
        if len(result) > MAX_ITEMS * 4:
            return UNKNOWN
        return result if isinstance(result, set) else _taint(result, found)

    # --------------------------------------------------------------------------------- calls
    def call(self, node, fr: Frame):
        func = self.ev(node.func, fr)
        if _opaque(func):
            return func
        if not (isinstance(func, (Helper, BoundMethod)) or _known(func)):
            return UNKNOWN                # decided BEFORE the arguments are evaluated
        args: list = []
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                value = self.ev(arg.value, fr)
                if _opaque(value) or not isinstance(value, (list, tuple)):
                    return UNKNOWN
                args.extend(value)
            else:
                args.append(self.ev(arg, fr))
        kwargs = {}
        for keyword in node.keywords:
            if keyword.arg is None:
                return UNKNOWN
            kwargs[keyword.arg] = self.ev(keyword.value, fr)
        if isinstance(func, Helper):
            return self.call_helper(func, args, kwargs, fr)
        if func in (all, any) and args and isinstance(args[0], (list, tuple)):
            return _all_any(func, args[0])
        if any(_opaque(a) for a in args) or any(_opaque(v) for v in kwargs.values()):
            return RAISED if RAISED in args else UNKNOWN
        if isinstance(func, BoundMethod):
            return self.method(func, args, kwargs, node, fr)
        return self.call_real(func, args, kwargs)

    def call_helper(self, helper: Helper, args, kwargs, fr: Frame):
        if fr.hops > MAX_HOPS:
            return UNKNOWN
        node, spec = helper.node, helper.node.args
        names = [p.arg for p in spec.posonlyargs + spec.args]
        other = self.child(helper.module)
        home = Frame(helper.module, helper.module.parent_scope(node), node.lineno, {}, fr.hops + 1)
        overlay = {p: UNKNOWN for p in names + [k.arg for k in spec.kwonlyargs]}
        for p, default in zip(names[len(names) - len(spec.defaults):], spec.defaults):
            overlay[p] = other.ev(default, home)
        for p, default in zip(spec.kwonlyargs, spec.kw_defaults):
            if default is not None:
                overlay[p.arg] = other.ev(default, home)
        if len(args) > len(names) and spec.vararg is None:
            return UNKNOWN
        overlay.update(zip(names, args))
        if spec.vararg is not None:
            overlay[spec.vararg.arg] = tuple(args[len(names):])
        overlay.update(kwargs)
        best = UNKNOWN
        for ret in helper.module.census.returns(node):
            value = other.ev(ret.value, Frame(helper.module, node, ret.lineno, overlay, fr.hops + 1))
            if not _opaque(value):
                best = value
        return best

    def method(self, bound: BoundMethod, args, kwargs, node, fr: Frame):
        base, name = bound.receiver, bound.name
        if isinstance(base, PurePath):
            if name == "read_text":
                return self.reader.read_file(base, self.world)
            if name == "read_bytes":
                text = self.reader.read_file(base, self.world)
                return text if _opaque(text) else _taint(text.encode("utf-8"), _origins(text))
            if name == "open":
                return Handle(Path(base))
            if name in ("glob", "rglob", "iterdir"):
                try:
                    found = sorted(getattr(base, name)(*args))
                except Exception:  # noqa: BLE001 — an unreadable directory is simply not modelled
                    return UNKNOWN
                return found if len(found) <= MAX_ITEMS else UNKNOWN
            try:
                return getattr(base, name)(*args, **kwargs)
            except Exception:  # noqa: BLE001 — e.g. `relative_to` of an unrelated path
                return RAISED
        if isinstance(base, Handle):
            text = self.reader.read_file(base.path, self.world)
            if _opaque(text) or name == "read":
                return text
            return _taint(text.splitlines(keepends=True), _origins(text))
        if isinstance(base, (str, bytes)):
            found = _origins(base, *args, *kwargs.values())
            if (self.record and name in _PIN_METHODS and args and isinstance(args[0], (str, bytes))
                    and isinstance(base, (Tainted, TaintedBytes))):
                self.terms.append(Term(name, args[0], base.origins, node, fr))
            try:
                result = getattr(_plain(base), name)(*[_plain(a) for a in args],
                                                     **{k: _plain(v) for k, v in kwargs.items()})
            except ValueError:
                return RAISED
            except Exception:  # noqa: BLE001 — a call the real test would raise on: not modelled
                return UNKNOWN
            return _taint(result, found)
        if isinstance(base, (list, tuple, dict)):
            try:
                return getattr(base, name)(*args, **kwargs)
            except ValueError:
                return RAISED
            except Exception:  # noqa: BLE001 — as above
                return UNKNOWN
        if isinstance(base, re.Match):
            try:
                result = getattr(base, name)(*args)
            except Exception:  # noqa: BLE001 — as above
                return UNKNOWN
            if isinstance(result, (str, tuple)):
                return _taint(result, _origins(base.string))
            return result
        if isinstance(base, re.Pattern):
            return self.call_real(getattr(base, name), args, kwargs, pattern=True)
        return UNKNOWN

    def call_real(self, func, args, kwargs, pattern: bool = False):
        reader = None if pattern else _READERS.get(func)
        if reader is not None:
            return reader(self, args, kwargs)
        if func is getattr:
            if len(args) >= 2 and isinstance(args[1], str) and _plain_object(args[0]):
                if hasattr(args[0], args[1]):
                    return getattr(args[0], args[1])
                return args[2] if len(args) > 2 else RAISED
            return UNKNOWN
        found = _origins(*args, *kwargs.values())
        # `re` searching a tainted string keeps the taint on the match; everything else computes on
        # plain values and is re-tainted with every origin its inputs carried.
        keep = pattern or (found and func in (re.search, re.match, re.fullmatch, re.finditer))
        try:
            result = func(*[a if keep else _plain(a) for a in args],
                          **{k: _plain(v) for k, v in kwargs.items()})
        except ValueError:
            return RAISED
        except Exception:  # noqa: BLE001 — a call the real test would raise on: not modelled
            return UNKNOWN
        if inspect.isgenerator(result) or isinstance(result, (map, filter, zip, enumerate,
                                                              reversed, range)) \
                or type(result).__name__ == "callable_iterator":
            try:
                result = list(result)
            except Exception:  # noqa: BLE001 — as above
                return UNKNOWN
        return _taint(result, found) if isinstance(result, (str, bytes, list, tuple)) else result


def _read_source(interp: Interp, args, kwargs):
    if not args or not _plain_object(args[0]):
        return UNKNOWN
    return interp.reader.read_object(args[0], interp.world)


def _read_source_lines(interp: Interp, args, kwargs):
    if not args or not _plain_object(args[0]):
        return UNKNOWN
    text = interp.reader.read_object(args[0], interp.world)
    first = interp.reader.object_lines(args[0])
    if _opaque(text) or first is None:
        return RAISED if first is None else text
    return (_taint(text.splitlines(keepends=True), _origins(text)), first)


def _read_eval_attempt(interp: Interp, args, kwargs, dedent: bool = False):
    parts = []
    for func in _source_scan.eval_attempt_functions():
        text = interp.reader.read_object(func, interp.world)
        if _opaque(text):
            return text
        parts.append(_taint(textwrap.dedent(text), _origins(text)) if dedent else text)
    return _taint("\n".join(parts), _origins(*parts))


def _open(interp: Interp, args, kwargs):
    if not args or not isinstance(args[0], (str, PurePath)):
        return UNKNOWN
    mode = args[1] if len(args) > 1 else kwargs.get("mode", "r")
    if not isinstance(mode, str) or any(c in mode for c in "wax+"):
        return UNKNOWN
    return Handle(Path(args[0]))


def _path(interp: Interp, args, kwargs):
    try:
        return Path(*[_plain(a) for a in args])
    except TypeError:
        return UNKNOWN


def _import_module(interp: Interp, args, kwargs):
    if args and isinstance(args[0], str):
        return _import(args[0], interp.module.sys_path())
    return UNKNOWN


def _source_file(interp: Interp, args, kwargs):
    if not args or not _plain_object(args[0]):
        return UNKNOWN
    try:
        return inspect.getsourcefile(args[0])
    except TypeError:
        return UNKNOWN


_READERS = {
    inspect.getsource: _read_source,
    inspect.getsourcelines: _read_source_lines,
    inspect.getsourcefile: _source_file,
    inspect.getfile: _source_file,
    _source_scan.eval_attempt_source: _read_eval_attempt,
    _source_scan.eval_attempt_dedented_source: functools.partial(_read_eval_attempt, dedent=True),
    open: _open,
    io.open: _open,
    Path: _path,
    PurePath: _path,
    type(Path(".")): _path,
    importlib.import_module: _import_module,
}


def _known(func) -> bool:
    """A real callable this interpreter will call: pure, a reader, or `getattr`."""
    try:
        return func in _PURE or func in _READERS or func is getattr
    except TypeError:                     # an unhashable callee is simply not one of them
        return False


def _all_any(func, items):
    known = [x for x in items if not _opaque(x)]
    if func is all:
        if any(not x for x in known):
            return False
        return True if len(known) == len(items) else UNKNOWN
    if any(x for x in known):
        return True
    return False if len(known) == len(items) else UNKNOWN


def _native_ok(node) -> bool:
    for sub in ast.walk(node):
        if not isinstance(sub, _NATIVE_NODES):
            return False
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                if func.id not in _NATIVE_BUILTINS:
                    return False
            elif not (isinstance(func, ast.Attribute)
                      and (func.attr in _STR_METHODS or func.attr in _SEQ_METHODS)):
                return False
        if isinstance(sub, ast.Attribute) and (sub.attr.startswith("_") or not (
                sub.attr in _STR_METHODS or sub.attr in _SEQ_METHODS)):
            return False
    return True


def _pin_shaped(node) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Compare) and any(isinstance(o, (ast.In, ast.NotIn)) for o in sub.ops):
            return True
        if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                and sub.func.attr in _PIN_METHODS):
            return True
    return False


def _data(value, depth: int = 0) -> bool:
    """What a native comprehension may see: plain data, never an object with behaviour."""
    if isinstance(value, (str, bytes, int, float, bool, type(None), TaintedList)):
        return True
    if depth < 3 and isinstance(value, (list, tuple, set, frozenset)) and len(value) <= 50000:
        return all(_data(v, depth + 1) for v in value)
    if depth < 3 and isinstance(value, dict) and len(value) <= 50000:
        return all(_data(k, depth + 1) and _data(v, depth + 1) for k, v in value.items())
    return False


def _bind(target, item) -> dict | None:
    if isinstance(target, ast.Name):
        return {target.id: item}
    if isinstance(target, (ast.Tuple, ast.List)):
        if _opaque(item) or not isinstance(item, (tuple, list)) or len(item) != len(target.elts):
            return None
        out: dict = {}
        for sub, value in zip(target.elts, item):
            bound = _bind(sub, value)
            if bound is None:
                return None
            out.update(bound)
        return out
    return None


def _unpack(target, value, name: str):
    if isinstance(target, ast.Name):
        return value if target.id == name else UNKNOWN
    if isinstance(target, (ast.Tuple, ast.List)):
        if _opaque(value) or not isinstance(value, (tuple, list)) or len(value) != len(target.elts):
            return UNKNOWN
        for sub, item in zip(target.elts, value):
            if any(isinstance(leaf, ast.Name) and leaf.id == name for leaf in ast.walk(sub)):
                return _unpack(sub, item, name)
    return UNKNOWN


# ------------------------------------------------------------------------------ the census
READ_SPELLINGS = ("getsource", "read_text", "read_bytes", "open(", "eval_attempt_source",
                  "eval_attempt_dedented_source")


@dataclass(frozen=True)
class Finding:
    test: str             # "tests/test_x.py::test_name"
    line: int
    literal: str          # the pinned literal, or the assert's text when no single term is at fault
    origins: tuple


class Census:
    """One pass over a tests directory, reading the repository it sits in."""

    def __init__(self, repo: Path, tests: Path):
        self.repo = repo
        self.tests = tests
        self.reader = Reader(repo)
        self._modules: dict = {}
        self._compiled: dict = {}
        self._returns: dict = {}
        self.pins: set = set()

    def module(self, path: Path) -> TestModule:
        path = path.resolve()
        if path not in self._modules:
            self._modules[path] = TestModule(path, self)
        return self._modules[path]

    def compiled(self, node):
        hit = self._compiled.get(id(node))
        if hit is None or hit[0] is not node:
            hit = self._compiled[id(node)] = (node, compile(ast.Expression(node), "<pin>", "eval"))
        return hit[1]

    def returns(self, node) -> list:
        hit = self._returns.get(id(node))
        if hit is None or hit[0] is not node:
            found = []
            stack = list(node.body) if isinstance(node.body, list) else [node.body]
            while stack:
                sub = stack.pop()
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    continue
                if isinstance(sub, (ast.Return, ast.Yield)) and sub.value is not None:
                    found.append(sub)
                stack.extend(ast.iter_child_nodes(sub))
            found.sort(key=lambda n: (n.lineno, n.col_offset))
            hit = self._returns[id(node)] = (node, found)
        return hit[1]

    def run(self, paths) -> list[Finding]:
        findings: set = set()
        for path in paths:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            if not _reads_repository(text):
                continue
            self._modules.clear()                # one module's ASTs alive at a time
            mod = self.module(path)
            rel = path.resolve().relative_to(self.repo).as_posix()
            full = Interp(mod, "full", record=True)
            code = Interp(mod, "code")
            for node, fn, loops, kind in _sites(mod):
                expr = node.test if kind == "assert" else node
                try:
                    full.reset()
                    frames = _frames(node, fn, loops, mod, full)
                except OutOfFuel:
                    continue
                for fr in frames:
                    try:
                        findings.update(self.judge(node, expr, kind, fn, fr, rel, full, code))
                    except OutOfFuel:
                        continue
        self._modules.clear()
        return sorted(findings, key=lambda f: (f.test, f.line, f.literal))

    def judge(self, node, expr, kind, fn, fr, rel, full: Interp, code: Interp) -> list[Finding]:
        """[] unless the site holds in the FULL reading and fails in the CODE one."""
        full.reset()
        held = full.ev(expr, fr)
        if not full.touched:
            return []
        positive = [t for t in full.terms if t.op != "not in"]
        self.pins.update((rel, t.node.lineno, str(t.literal)) for t in positive)
        code.reset()
        if kind == "index":
            if _opaque(held) or code.ev(expr, fr) is not RAISED:
                return []
        else:
            if held is RAISED or (not _opaque(held) and not held):
                return []
            got = code.ev(expr, fr)
            if not (got is RAISED or (not _opaque(got) and not got)):
                return []
        name = f"{rel}::{fn.name if fn else '<module>'}"
        culprits, seen = [], set()
        for term in positive:
            key = (id(term.node), str(term.literal), id(term.frame))
            if key in seen:
                continue
            seen.add(key)
            code.fuel[0] = FUEL
            value = code.ev(term.node, term.frame)
            if value is RAISED or value is False or (term.op == "count" and value == 0) or (
                    term.op in ("find", "rfind") and value == -1):
                culprits.append(Finding(name, term.node.lineno, str(term.literal),
                                        tuple(sorted(term.origins))))
        return culprits or [Finding(name, node.lineno, ast.unparse(expr),
                                    tuple(sorted(full.touched)))]


def _reads_repository(text: str) -> bool:
    """The module prefilter: a source READ is `getsource`/an eval-attempt helper, or a file read in
    a module that spells a repository path at all."""
    if "getsource" in text or "eval_attempt" in text:
        return True
    if not any(r in text for r in ("read_text", "read_bytes", "open(")):
        return False
    return any(p in text for p in ("__file__", "_source_scan", "looplab/", '"looplab"', "'looplab'",
                                   "benchmarks", "PKG"))


def _mentions(names: set):
    if not names:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
                      + r")\b")


def _source_names(mod: TestModule) -> set:
    """Module-level names whose definition reads source, directly or through another such name."""
    lines = mod.text.splitlines()
    segments: dict = {}
    for node in mod.tree.body:
        text = "\n".join(lines[node.lineno - 1:node.end_lineno])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            segments.setdefault(node.name, []).append(text)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                for leaf in ast.walk(target):
                    if isinstance(leaf, ast.Name):
                        segments.setdefault(leaf.id, []).append(text)
        elif isinstance(node, ast.ImportFrom) and node.module and (
                node.module.split(".")[-1].startswith("test_")
                or node.module.endswith("_source_scan")):
            for alias in node.names:
                segments.setdefault(alias.asname or alias.name, []).append("getsource")
    names = {k for k, texts in segments.items()
             if any(s in t for t in texts for s in READ_SPELLINGS)}
    for _ in range(6):
        pattern = _mentions(names)
        grown = {k for k, texts in segments.items()
                 if k not in names and pattern is not None and any(pattern.search(t) for t in texts)}
        if not grown:
            break
        names |= grown
    return names


def _sites(mod: TestModule) -> list:
    """`(node, function, loops, kind)` for every assert and every standalone `.index()`/`.rindex()`
    in a top-level statement that can read source (by the text prefilter)."""
    lines = mod.text.splitlines()
    mentions = _mentions(_source_names(mod))
    out: list = []

    def visit(nodes, fn, loops):
        for child in nodes:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child.body, child, [])
            elif isinstance(child, ast.ClassDef):
                visit(child.body, fn, loops)
            elif isinstance(child, ast.Lambda):
                continue
            elif isinstance(child, ast.Assert):
                out.append((child, fn, loops, "assert"))
            elif isinstance(child, (ast.For, ast.AsyncFor)):
                visit([child.target, child.iter, *child.orelse], fn, loops)
                visit(child.body, fn, loops + [child])
            else:
                if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                        and child.func.attr in ("index", "rindex") and child.args):
                    out.append((child, fn, loops, "index"))
                visit(ast.iter_child_nodes(child), fn, loops)

    for top in mod.tree.body:
        text = "\n".join(lines[top.lineno - 1:top.end_lineno])
        if any(s in text for s in READ_SPELLINGS) or (mentions and mentions.search(text)):
            visit([top], None, [])
    return out


def _frames(node, fn, loops, mod: TestModule, interp: Interp) -> list:
    """Every overlay the site runs under: each `parametrize` row, then each enclosing loop's item."""
    frames = [Frame(mod, fn or mod.tree, node.lineno, {})]
    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for deco in fn.decorator_list:
            if not (isinstance(deco, ast.Call) and ast.unparse(deco.func).endswith("parametrize")
                    and len(deco.args) >= 2):
                continue
            home = Frame(mod, mod.tree, BIG, {})
            keys, rows = interp.ev(deco.args[0], home), interp.ev(deco.args[1], home)
            if isinstance(keys, (list, tuple)):
                keys = ",".join(keys)
            if not isinstance(keys, str) or not isinstance(rows, (list, tuple)):
                continue
            keys = [k.strip() for k in keys.split(",") if k.strip()]
            grown = []
            for frame in frames:
                for row in rows:
                    if type(row).__name__ == "ParameterSet":
                        row = row.values if len(keys) > 1 else row.values[0]
                    if len(keys) == 1:
                        grown.append(frame.bind({keys[0]: row}))
                    elif isinstance(row, (tuple, list)) and len(row) == len(keys):
                        grown.append(frame.bind(dict(zip(keys, row))))
            frames = grown or frames
    for loop in loops:
        grown = []
        for frame in frames:
            items = interp.ev(loop.iter, frame.at(loop.lineno))
            try:
                items = None if _opaque(items) else list(items)
            except TypeError:
                items = None
            if items is None or len(items) > MAX_ITEMS:
                grown.append(frame)              # an opaque loop: its names stay unknown
                continue
            for item in items:
                bound = _bind(loop.target, item)
                if bound is not None:
                    grown.append(frame.bind(bound))
        frames = grown
    return frames


def census(repo: Path = REPO, tests: Path = TESTS, paths=None) -> tuple[list[Finding], set]:
    """`(findings, positive pins)` for every `test_*.py` under *tests*.

    The cyclic GC is paused for the pass and restored after it: the census allocates millions of
    short-lived nodes while holding a large import graph, and full collections tripled its wall
    clock on this box (13.3 s -> 8.6 s)."""
    run = Census(repo, tests)
    paths = sorted(tests.glob("test_*.py")) if paths is None else paths
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        return run.run(paths), run.pins
    finally:
        if was_enabled:
            gc.enable()


@functools.lru_cache(maxsize=1)
def _this_suite() -> tuple:
    findings, pins = census()
    return tuple(findings), frozenset(pins)


def _allowed() -> set:
    return {(test, literal) for test, (_why, literals) in DOCUMENTATION_PINS.items()
            for literal in literals}


def _normalized(text: str) -> str:
    return " ".join(text.split())


# ------------------------------------------------------------------------------ the budget
def test_no_positive_pin_is_held_by_prose():
    """The budget. Every finding is either a check that needs to become one of the behaviour, or a
    documentation pin its own test declares."""
    findings, _pins = _this_suite()
    offenders = [f for f in findings if (f.test, f.literal) not in _allowed()]
    assert not offenders, (
        "positive pins that only a comment or a docstring of the source they read satisfies — "
        "delete the code, keep the comment, and they stay green:\n"
        + "\n".join(f"  {f.test}:{f.line}  {f.literal!r}  (prose of {', '.join(f.origins)})"
                    for f in offenders)
        + "\nCheck the BEHAVIOUR instead (drive it; else `_source_scan.called_names`/`names_read`/"
          "`attributes_read`), read the docstring you mean (`obj.__doc__`), or — only when the "
          "test's own docstring names the prose as its subject — add a DOCUMENTATION_PINS row "
          "quoting that sentence.")


def test_the_documentation_pins_only_shrink():
    """SHRINK-ONLY: a row whose pin no longer exists — converted, deleted, or now held by code — is
    deleted, never kept as a standing permission for the next prose pin under that name."""
    findings, _pins = _this_suite()
    live = {(f.test, f.literal) for f in findings}
    stale = sorted(_allowed() - live)
    assert not stale, (
        "DOCUMENTATION_PINS rows whose prose pin is gone; delete them:\n"
        + "\n".join(f"  {test}  {literal!r}" for test, literal in stale))


def test_each_documentation_pin_is_declared_by_its_own_test():
    """The condition for keeping a prose pin, made checkable: the row's reason is a sentence of the
    test's OWN docstring saying the prose is its subject, quoted verbatim (whitespace aside)."""
    undeclared = []
    for test, (why, literals) in DOCUMENTATION_PINS.items():
        rel, name = test.split("::")
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8-sig"))
        docs = [ast.get_docstring(node) or "" for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
        if not literals or not why.strip() or not any(
                _normalized(why) in _normalized(doc) for doc in docs):
            undeclared.append(test)
    assert not undeclared, (
        f"rows whose reason is not a sentence of their own test's docstring: {undeclared}")


def test_the_census_reads_the_pins_it_polices():
    """A census whose readers silently resolved nothing would report a clean budget. Measured on
    2026-09-23: 592 positive pin terms over repository Python. The floor sits well under that, so
    converting pins does not trip it; losing the readers does."""
    _findings, pins = _this_suite()
    assert len(pins) >= 400, f"the census resolved only {len(pins)} positive pins"


# ------------------------------------------------------------------------------ the census, driven
_FIXTURE_MOD = '''\
"""A module whose docstring says MODULE_DOC_ONLY."""

PROMPT = "say PROMPT_WORDS"   # COMMENT_ONLY lives in this comment


def run(x):
    """RUN_DOC_ONLY lives in this docstring."""
    # RUN_COMMENT_ONLY
    return real_call(x)


def real_call(x):
    return x
'''

_FIXTURE_TEST = '''\
import inspect
from pathlib import Path

from pinbudget_fixture_pkg import mod

SRC = (Path(__file__).resolve().parents[1] / "pinbudget_fixture_pkg" / "mod.py").read_text()


def test_code():
    assert "return real_call(x)" in SRC


def test_prompt_string():
    assert "PROMPT_WORDS" in SRC


def test_comment():
    assert "COMMENT_ONLY" in SRC


def test_module_docstring():
    assert "MODULE_DOC_ONLY" in SRC


def test_function_docstring():
    assert "RUN_DOC_ONLY" in inspect.getsource(mod.run)


def test_function_code():
    assert "return real_call(x)" in inspect.getsource(mod.run)


def test_loop():
    for literal in ("real_call(x)", "RUN_COMMENT_ONLY"):
        assert literal in inspect.getsource(mod.run)


def test_order():
    src = inspect.getsource(mod.run)
    assert src.index("RUN_COMMENT_ONLY") < src.index("real_call(x)")


def test_either():
    assert "COMMENT_ONLY" in SRC or "real_call" in SRC


def test_docstring_on_purpose():
    assert "RUN_DOC_ONLY" in mod.run.__doc__


def test_negative():
    assert "GONE" not in SRC


def test_anchor():
    at = SRC.index("# COMMENT_ONLY")
    assert at > 0
'''


def test_the_census_tells_prose_from_code(tmp_path, monkeypatch):
    """The census's own truth table, over a repository it has never seen: every shape it claims to
    read, each on one side of the line. A pin on a comment, a module or function docstring, a loop
    item, an ORDER and a standalone `.index()` anchor are prose-held; code, a prompt string in code,
    an `or` with a code branch, a deliberate `__doc__` read and a negative pin are not."""
    package = tmp_path / "pinbudget_fixture_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "mod.py").write_text(_FIXTURE_MOD, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    fake = tmp_path / "tests" / "test_fake.py"
    fake.write_text(_FIXTURE_TEST, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        findings, pins = census(tmp_path, tmp_path / "tests", [fake])
    finally:
        for name in ("pinbudget_fixture_pkg.mod", "pinbudget_fixture_pkg"):
            sys.modules.pop(name, None)
    assert {(f.test.split("::")[1], f.literal) for f in findings} == {
        ("test_comment", "COMMENT_ONLY"),
        ("test_module_docstring", "MODULE_DOC_ONLY"),
        ("test_function_docstring", "RUN_DOC_ONLY"),
        ("test_loop", "RUN_COMMENT_ONLY"),
        ("test_order", "RUN_COMMENT_ONLY"),
        ("test_anchor", "# COMMENT_ONLY"),
    }
    assert {f.origins for f in findings} <= {("pinbudget_fixture_pkg/mod.py",),
                                             ("pinbudget_fixture_pkg/mod.py::run",)}
    # ...and the pins held by code were READ, not skipped: the census saw them and let them be.
    # (`test_either`'s code branch is not among them: the full reading short-circuits on the prose
    # branch exactly as the real test does, and only the CODE reading needs the second one.)
    literals = {literal for _rel, _line, literal in pins}
    assert {"return real_call(x)", "PROMPT_WORDS", "real_call(x)"} <= literals
