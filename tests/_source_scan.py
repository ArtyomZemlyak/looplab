"""One walk of `looplab/`, for the guard tests that scan source (doc 25 XP-10).

Fifteen tests rglob the package and read every file. They exist because several seams are duck-typed
or registry-backed, so the only way to catch a rename is to look at the source — and that is worth
keeping. What was not worth keeping is fifteen copies of the walk, because the copies had already
diverged on the one detail that decides whether a scan WORKS: how a file is decoded.

Four spellings were in use — `utf-8`, `utf-8-sig`, and each with `errors="replace"`. The difference
is not cosmetic. At least one tracked file carries a BOM, and `ast.parse` on a plain-`utf-8` read of
it raises `SyntaxError: invalid non-printable character U+FEFF` — so a scanner written with the wrong
spelling does not miss a finding quietly, it dies on an unrelated file. Three tests still parsed with
plain `utf-8` and were one BOM away from that.

`utf-8-sig` with `errors="replace"` is the spelling that works for BOTH scan kinds: it strips a BOM
so `ast.parse` accepts the text, and it never raises on a stray byte so a regex scan keeps going.

Deliberately NOT here: `tests/test_signal_delivery.py` reads a handful of NAMED files rather than
walking the package, which is a different (and correct) shape — its point is that one specific
wiring line exists in one specific file.

Also here, and about the OTHER scan kind: `called_names`/`names_read`/`function_tree`, the
comment-proof replacement for a positive `"<literal>" in inspect.getsource(f)` pin. See the block
comment above them and CLAUDE.md's "Testing conventions". And `code_text`, the source minus its
comments and docstrings, which `tests/test_pin_budget.py` holds every positive pin to.
"""
from __future__ import annotations

import ast
import inspect
import io
import re
import textwrap
import tokenize
from pathlib import Path
from typing import Iterator

PKG = Path(__file__).resolve().parents[1] / "looplab"

# Directories that live INSIDE the package tree but are not the package. `.ipynb_checkpoints` is
# Jupyter's autosave sidecar: it is gitignored (`.gitignore`: `**/.ipynb_checkpoints/`) and untracked,
# so it holds a STALE copy of whatever a file looked like when someone last opened it in a notebook
# server. Scanning it makes every guard here report the PAST as a present violation — e.g.
# `serve/.ipynb_checkpoints/assistant-checkpoint.py` still carries an
# `from looplab.tools.knowledge_tools import _fn_spec` that the real `serve/assistant.py` dropped,
# which fails `test_cross_package_private_seams` on a tree whose production source is clean. That is
# not hypothetical here: LoopLab is developed on JupyterHub, where these dirs appear routinely (three
# exist under `looplab/` right now). A guard that fires on a gitignored autosave is unactionable —
# there is no source change that would make it pass.
EXCLUDED_DIRS = frozenset({".ipynb_checkpoints", "__pycache__"})


def _package_files(pkg: Path) -> Iterator[Path]:
    """Every `.py` under *pkg*, sorted, minus `EXCLUDED_DIRS` — the ONE walk both readers share.

    Sorted so a failure message lists offenders in a stable order — an unsorted `rglob` reports the
    same set in a different order per filesystem, which reads as a flapping test.
    """
    for path in sorted(pkg.rglob("*.py")):
        if EXCLUDED_DIRS.intersection(path.parts):
            continue
        yield path


def _decode(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def iter_sources(pkg: Path = PKG) -> Iterator[tuple[Path, str]]:
    """Every `.py` under *pkg*, sorted, with its decoded text."""
    for path in _package_files(pkg):
        yield path, _decode(path)


# Parsed trees, memoized per file for the life of the test process and revalidated on every call by
# the file's `(st_mtime_ns, st_size)`. `ast.parse` of the whole package is ~1.3 s and `iter_trees`
# has well over a hundred callers, so without this the guard family re-parsed the same unchanged
# tree on every call (review 2026-09-22, TST-04). The stat revalidation keeps an edit made between
# two calls visible; a mutation check runs in a throwaway COPY of the tree, i.e. at different paths,
# so it never meets a tree cached from the real one. CONTRACT: callers treat a yielded tree as
# READ-ONLY — it is shared with every later caller (none mutates one today: they `ast.walk`).
_TREE_CACHE: dict[Path, tuple[tuple[int, int], ast.AST]] = {}


def iter_trees(pkg: Path = PKG) -> Iterator[tuple[Path, ast.AST]]:
    """Every source under *pkg* parsed to an AST, with `filename` set so a SyntaxError names it."""
    for path in _package_files(pkg):
        st = path.stat()
        signature = (st.st_mtime_ns, st.st_size)
        hit = _TREE_CACHE.get(path)
        if hit is None or hit[0] != signature:
            hit = (signature, ast.parse(_decode(path), filename=str(path)))
            _TREE_CACHE[path] = hit
        yield path, hit[1]


# ------------------------------------------------------------------ the comment-proof call pin
#
# A positive source pin — `assert "self._header_join()" in inspect.getsource(f)` — is one comment
# away from vacuous, because the cheapest mutation is "delete the code, leave a comment carrying the
# pinned literal":
#
#     pass  # self._header_join()
#
# The suite holds ~62 such pins. Full call EXPRESSIONS beat bare names (a bare `_header_join` also
# matches the word in prose) but are NOT comment-proof either: the repo's own model pin,
# `test_card_speculation_engine.py:1731-1733`, pins three call expressions AND their order, and
# `pass  # self._record_eval_start_boundary(chosen)` satisfies all three `source.index()` lookups in
# the right order while the boundary event is never written — which CLAUDE.md records costing 17
# builds / 5 discards -> 12 / 0.
#
# Comments are not AST nodes. `called_names`/`names_read` resolve real `ast.Call` / `ast.Name(Load)`
# nodes, so no amount of commented-out text can satisfy them. They are the FALLBACK, not the goal:
# where the property can be driven (a real fallback with a counting accountant, a real socket that
# goes silent), drive it — a call that happens is not the same claim as an effect that lands.


def function_tree(func) -> ast.AST:
    """*func*'s own source parsed to an AST (dedented, so a method body parses standalone)."""
    return ast.parse(textwrap.dedent(inspect.getsource(func)))


def _dotted(node: ast.AST) -> str:
    """`self._header_join` / `openai.APITimeoutError` / `sock.shutdown` for a call target."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, (ast.Call, ast.Subscript)):
        inner = _dotted(node.func if isinstance(node, ast.Call) else node.value)
        return f"{inner}()" if isinstance(node, ast.Call) and inner else inner
    return ""


def called_names(func) -> list[str]:
    """Dotted target of every REAL call in *func*, in source order (nested defs included).

    Source order, not `ast.walk`'s breadth-first order, because "A is called before B" is a property
    several of these pins actually assert and BFS reports it wrong for two calls at different depths.
    """
    calls = [node for node in ast.walk(function_tree(func)) if isinstance(node, ast.Call)]
    calls.sort(key=lambda node: (node.lineno, node.col_offset))
    return [_dotted(node.func) for node in calls]


def called_or_offloaded_names(func) -> list[str]:
    """`called_names`, with a callable handed to `anyio.to_thread.run_sync` counted as its call.

    `await anyio.to_thread.run_sync(self._eval_prepare_workdir, a)` INVOKES the phase — on a worker
    thread since review 2026-09-22 (ENG2-11) — and a pin over the driver's phase order has to read it
    as such; `called_names` alone reports only `anyio.to_thread.run_sync`. The offloaded reference is
    reported in the offload call's own source position, so "A runs before B" pins keep their meaning.
    """
    calls = [node for node in ast.walk(function_tree(func)) if isinstance(node, ast.Call)]
    calls.sort(key=lambda node: (node.lineno, node.col_offset))
    out = []
    for node in calls:
        name = _dotted(node.func)
        if name == "anyio.to_thread.run_sync" and node.args:
            name = _dotted(node.args[0]) or name
        out.append(name)
    return out


def names_read(func) -> set[str]:
    """Every bare name LOADED in *func*. A name that survives only in a comment is not in here."""
    return {node.id for node in ast.walk(function_tree(func))
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}


def attributes_read(func) -> set[str]:
    """Every DOTTED attribute LOADED in *func* (`self._inline_repair_reasons`, `res.diverged`).

    `names_read`'s sibling for the case that matters most in this codebase: a gate that must consult
    a SETTING reads it as `self.<field>`, which is an `ast.Attribute` and therefore invisible to a
    bare-name scan. Same guarantee — a spelling that survives only in a comment is not an AST node
    and is not in here — so a positive pin over this cannot be satisfied by commenting the gate out
    and leaving its text behind."""
    return {dotted for dotted in (
        _dotted(node) for node in ast.walk(function_tree(func))
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)) if dotted}


# ------------------------------------------------------------------ the text a pin may match
# Review 2026-09-22, TST-05: 67 positive pins over production source were held ONLY by prose — the
# pinned literal occurred in a comment or a docstring of the source the test read, never in its
# code. `code_text` is what separates the two, and `tests/test_pin_budget.py` is the census that
# holds every positive pin to it. A STRING LITERAL IN CODE IS CODE: a pinned prompt sentence or an
# event name is a real pin, so only comments and string-only statements (docstrings, and a bare
# string used as a block comment) are removed.

_STRING_PREFIX = re.compile(r"[A-Za-z]*")


def _is_string_statement(tokens: list) -> bool:
    """A logical line that is nothing but string literals (wrapping parentheses allowed) — what
    `ast` calls an `Expr(Constant(str))`. An f-string statement is NOT one (`ast` reads it as a
    `JoinedStr`, so it is code there too); on 3.12 it is not even a STRING token."""
    body = [t for t in tokens if not (t.type == tokenize.OP and t.string in "()")]
    return bool(body) and all(
        t.type == tokenize.STRING and "f" not in _STRING_PREFIX.match(t.string).group().lower()
        for t in body)


def code_text(source: str) -> str:
    """*source* with every comment and every docstring removed — the text a positive pin may match.

    Kept: every other character, string literals included, and the LINE STRUCTURE — a removed span
    leaves the newlines it held, so line N of the result is line N of *source* minus its prose, and
    a slice of a file's code text is the code text of that slice. *source* may be a whole module or
    a function as `inspect.getsource` returns it, indentation and all: it is NOT dedented, because a
    pin that spells indentation must see the same indentation in both readings.

    Raises `ValueError` when *source* does not tokenize: a scanner must not guess which half of an
    unreadable text is code."""
    # Rows as `tokenize` counts them: `\n` only. `str.splitlines` also breaks on `\x0c`, `\x1c` and
    # ` `, which a docstring may hold, and every offset after one would then be wrong.
    starts = [0]
    at = source.find("\n")
    while at != -1:
        starts.append(at + 1)
        at = source.find("\n", at + 1)

    def offset(row_col: tuple) -> int:
        row, col = row_col
        return starts[row - 1] + col

    drop: list[tuple[int, int]] = []
    statement: list = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                drop.append((offset(tok.start), offset(tok.end)))
            elif tok.type in (tokenize.NEWLINE, tokenize.ENDMARKER):
                if statement and _is_string_statement(statement):
                    drop.append((offset(statement[0].start), offset(statement[-1].end)))
                statement = []
            elif tok.type not in (tokenize.NL, tokenize.INDENT, tokenize.DEDENT):
                statement.append(tok)
    except (tokenize.TokenError, SyntaxError) as exc:      # IndentationError is a SyntaxError
        raise ValueError(f"cannot separate code from prose: {exc}") from exc
    out: list[str] = []
    pos = 0
    for start, end in sorted(drop):
        if start > pos:
            out.append(source[pos:start])
        out.append("\n" * source.count("\n", max(start, pos), end))
        pos = max(pos, end)
    out.append(source[pos:])
    return "".join(out)


def scan(pattern: re.Pattern | str, *, pkg: Path = PKG) -> dict[str, set[str]]:
    """``{captured name: {relative file, …}}`` for every match of *pattern*.

    The file set is the point, not the count: a guard test's failure message has to say WHERE an
    unregistered name is used, or the person reading it has to re-run the grep by hand.
    """
    compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
    found: dict[str, set[str]] = {}
    for path, text in iter_sources(pkg):
        for name in compiled.findall(text):
            found.setdefault(name, set()).add(str(path.relative_to(pkg)))
    return found


# --------------------------------------------------------------------------- the eval attempt loop
# `_evaluate` is a DRIVER over `EvalAttempt` and nine (ten since 2026-09-25) phase methods since 2026-09-06 (doc 52 row 21).
# A guard that used to read "the attempt loop" off `inspect.getsource(EvaluateMixin._evaluate)` now
# reads the driver plus every phase, IN THE ORDER THE DRIVER RUNS THEM — so an index-order pin over
# the concatenation still says what it said about one method, and a "called exactly once" pin counts
# across the phases. Prefer naming the phase when the property belongs to one.
EVAL_PHASES = ("_eval_admit", "_eval_recover_settled", "_eval_prepare_workdir", "_eval_seed_ledgers", "_eval_run_attempt",
               "_eval_settle_outcome", "_eval_salvage", "_eval_decide_repair", "_eval_apply_repair",
               "_eval_write_terminal")


def eval_attempt_functions() -> list:
    """The driver, the ten phases in driver order, then the reset terminal the phases share."""
    from looplab.engine.evaluate import EvaluateMixin

    return [getattr(EvaluateMixin, name)
            for name in ("_evaluate",) + EVAL_PHASES + ("_eval_record_superseded",)]


def eval_attempt_source() -> str:
    """Every function of the attempt loop, as source, concatenated in driver order."""
    return "\n".join(inspect.getsource(f) for f in eval_attempt_functions())


def eval_attempt_dedented_source() -> str:
    """The same concatenation, dedented so `ast.parse` accepts it (a method's body on its own)."""
    return "\n".join(textwrap.dedent(inspect.getsource(f)) for f in eval_attempt_functions())


def eval_attempt_tree() -> ast.Module:
    """One module holding every function of the attempt loop, in driver order."""
    return ast.parse(eval_attempt_dedented_source())


def eval_attempt_called_names() -> list[str]:
    """`called_names` over the whole attempt loop, in driver order."""
    return [name for f in eval_attempt_functions() for name in called_names(f)]


def eval_attempt_attributes_read() -> set[str]:
    """`attributes_read` over the whole attempt loop."""
    return set().union(*(attributes_read(f) for f in eval_attempt_functions()))



# ------------------------------------------------------------------- the event payload contract
# `looplab/events/types.py::EVENT_PAYLOAD_KEYS` says what each event type's `data` dict CARRIES
# (doc 52 row 30). Nothing about a contract written by hand is trustworthy on its own, so the guard
# (`tests/test_event_payload_contract.py`) re-derives BOTH sides from source and joins them here:
# what the fold READS off a payload, and what the writers PUT there. One implementation, because two
# scanners that disagree would make the contract un-authorable rather than merely un-checked.

def _payload_dict_keys(node: ast.AST) -> tuple[set[str], set[str], bool]:
    """`(keys written unconditionally, keys written inside a spread, an opaque spread was seen)`.

    `{"a": 1, **({"b": 2} if cond else {})}` writes `a` always and `b` maybe; `{**other}` is opaque —
    the key set is decided somewhere else, so the site can prove nothing about REQUIRED keys.
    """
    always: set[str] = set()
    maybe: set[str] = set()
    opaque = False
    if not isinstance(node, ast.Dict):
        return always, maybe, True
    for key, value in zip(node.keys, node.values):
        if key is None:                                   # a `**` spread
            nested = [sub for sub in ast.walk(value) if isinstance(sub, ast.Dict)]
            if not nested:
                opaque = True
                continue
            for sub in nested:
                for k in sub.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        maybe.add(k.value)
        elif isinstance(key, ast.Constant) and isinstance(key.value, str):
            always.add(key.value)
    return always, maybe, opaque


def subscript_string_keys(scope, name: str, *, after: int, before: int) -> set[str]:
    """`{k}` for every `name["k"] = …` in `scope` BETWEEN two source lines.

    The window is the whole soundness of this: one function often assigns `data` several times and
    appends several different event types from it, so an unwindowed walk hands every append every
    other one's keys (measured: 13 types gained keys they never carry). `after` is the line of the
    dict literal this payload resolved to and `before` the append call, so only the writes that
    can actually reach THAT payload are collected.
    """
    def _subscripts(target):
        """Every `name["k"]` this assignment TARGET binds, unpacking included.

        `data["triage_action"], data["triage_rationale"] = (…)` is one `Assign` whose single
        target is a `Tuple` of two Subscripts, and a walk that only accepted a bare Subscript saw
        NEITHER — `node_failed.triage_action` reached the durable log with no contract row while
        this scan reported the type fully covered, which is the same class of miss the subscript
        hop itself was added for. Starred and nested targets unpack the same way.
        """
        if isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                yield from _subscripts(elt)
        elif isinstance(target, ast.Starred):
            yield from _subscripts(target.value)
        elif isinstance(target, ast.Subscript):
            yield target

    out: set[str] = set()
    for node in scope:
        if not (after < getattr(node, "lineno", 0) < before):
            continue
        for target in (node.targets if isinstance(node, ast.Assign) else
                       [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else []):
            for sub in _subscripts(target):
                if (isinstance(sub.value, ast.Name) and sub.value.id == name
                        and isinstance(sub.slice, ast.Constant)
                        and isinstance(sub.slice.value, str)):
                    out.add(sub.slice.value)
        # `payload.update({...})` / `payload.update(k=…)` / `payload.setdefault("k", …)` put keys in
        # the payload without ever being an assignment TARGET, so a target-only walk cannot see
        # them: `train_monitor_alert` reached the log with FIVE undeclared columns — two of them
        # read live by `serve/attention.py` — and `asha_rank`/`asha_verdict` with two each, while
        # `test_every_key_a_writer_writes_is_declared` reported all three types fully covered.
        # Only CONSTANT keys are collected; `update(<a name>)` is a spread this cannot resolve and
        # is reported as opaque by `event_payload_writers` instead, which is the honest answer.
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name) and call.func.value.id == name):
                continue
            if call.func.attr == "setdefault" and call.args:
                key = call.args[0]
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    out.add(key.value)
            elif call.func.attr == "update":
                for kw in call.keywords:
                    if kw.arg:
                        out.add(kw.arg)
                for arg in call.args:
                    if isinstance(arg, ast.Dict):
                        for k in arg.keys:
                            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                                out.add(k.value)
    return out


def _scope_bodies(tree: ast.AST) -> Iterator[list[ast.AST]]:
    """Every module/function scope's OWN nodes, cut at each nested `def`.

    `ast.walk(module)` reaches into every function, so a name-resolution pass run over the module
    sees every local of every function in the file at once — which is how one function's `payload`
    literal was read as another's. Yielding each scope's own nodes makes "the last assignment to
    this name" mean what it says."""
    stack: list[ast.AST] = [tree]
    while stack:
        scope = stack.pop()
        own: list[ast.AST] = []
        pending = list(ast.iter_child_nodes(scope))
        while pending:
            node = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                stack.append(node)          # its body belongs to ITS scope, not this one
                # …but the decorators and defaults are evaluated HERE, so keep them.
                pending.extend(node.decorator_list)
                continue
            own.append(node)
            pending.extend(ast.iter_child_nodes(node))
        yield own


def _event_type_names() -> dict[str, str]:
    """Every module-level name that IS an event type — the `EV_*` constants plus their aliases.

    `core/phase_events.py` calls `emit_phase_event(PHASE_STARTED, {...})`, so a scanner that knows
    only `EV_*` sees no writer for three of the diagnostic types and silently reports their payload
    as empty. The aliases are found the same way the constants are: by value.
    """
    from looplab.events import types as event_types

    from looplab.core import phase_events

    names = {n: getattr(event_types, n) for n in dir(event_types) if n.startswith("EV_")}
    alias = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=\s*(EV_[A-Z0-9_]+)\s*$", re.M)
    for _path, text in iter_sources():
        for local, const in alias.findall(text):
            if const in names and local not in names:
                names[local] = names[const]
    # `core/phase_events.py` re-SPELLS its four types as literals because `core` may not import
    # `events` (the layering rule), so no alias assignment above can find them. Read them off the
    # module itself rather than by regex: a name-and-value match over one known module cannot
    # mistake an unrelated constant that happens to equal an event name for an event type.
    for local in dir(phase_events):
        value = getattr(phase_events, local)
        if (local.isupper() and isinstance(value, str) and value in event_types.ALL_EVENT_TYPES
                and local not in names):
            names[local] = value
    return names


def event_payload_writers() -> dict[str, dict]:
    """`{event type: {"always": {key…}, "any": {key…}, "sites": [file:line…]}}`.

    A WRITER is any call whose first argument is an event type and whose payload argument is a dict
    literal — `store.append(EV_X, {...})`, `append_many([(EV_X, {...})])`, and the engine's own
    `_append_proposal_event(EV_X, {...})` / `emit_phase_event(PHASE_X, {...})` wrappers alike. A
    payload handed over as a local variable is followed to its last dict-literal assignment.
    `always` holds the keys EVERY site writes unconditionally, and is empty as soon as one site
    builds its payload opaquely — that is the set a `required` declaration is checked against.
    """
    from looplab.events.types import ALL_EVENT_TYPES

    names = _event_type_names()

    def event_type(node: ast.AST):
        if isinstance(node, ast.Constant) and node.value in ALL_EVENT_TYPES:
            return node.value
        if isinstance(node, ast.Name):
            return names.get(node.id)
        if isinstance(node, ast.Attribute):
            return names.get(node.attr)
        return None

    found: dict[str, dict] = {}
    for path, tree in iter_trees():
        # EACH SCOPE'S OWN NODES, never the module's view of every nested function. `ast.walk` on
        # the Module reaches every body, so a file where two functions each build a local
        # `payload` had the module pass hand each append the OTHER one's dict literal and
        # subscript writes — measured: three event types gained an `after_seq` only
        # `finalize_step` writes. `_own_nodes` cuts at every nested def, so a name resolves in the
        # scope that actually binds it.
        for scope in _scope_bodies(tree):
            assigned: dict[str, list[ast.AST]] = {}
            for node in scope:
                if (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)):
                    assigned.setdefault(node.targets[0].id, []).append(node.value)
            for node in scope:
                if not (isinstance(node, ast.Call) and node.args):
                    continue
                etype = event_type(node.args[0])
                if etype is None:
                    continue
                payload = (node.args[1] if len(node.args) >= 2 else
                           next((k.value for k in node.keywords if k.arg == "data"), None))
                if payload is None:
                    continue

                def _unreadable(_etype=etype, _path=path, _node=node):
                    """A PAYLOAD THIS WALK CANNOT READ MUST NOT READ AS AN ABSENT ONE.

                    The two `continue`s below used to leave the type with NO row at all, which
                    `test_the_writer_scan_says_which_types_it_cannot_verify` cannot name — it only
                    checks the types the scan DID reach. So 42 registered types were unverified and
                    silent, and four real defects lived there: `llm_usage` declared 2 of its 7
                    columns (the durable money ledger, and the fold reads all five missing ones),
                    `plan` omitted the `max_nodes` its own `replan` reads back, `trace_export_health`
                    omitted 15 keys including the two a human debugging a stalled exporter needs, and
                    `card_build_done`'s `required` named three columns NO writer has ever written —
                    a fabricated claim that could not fail, because `test_required_keys_are_written
                    _by_every_literal_writer` skips a type with no writer row. Marking them opaque
                    forces each into `OPAQUE_PAYLOAD_WRITERS` WITH a hand-written key list, which is
                    the discipline that registry already imposes.
                    """
                    row = found.setdefault(_etype, {"always": None, "any": set(), "sites": [],
                                                    "opaque": False})
                    row["opaque"] = True
                    row["sites"].append(f"{_path.relative_to(PKG.parent)}:{_node.lineno}")

                subscripts: set[str] = set()
                if isinstance(payload, ast.Name):
                    payload_name = payload.id
                    literals = [v for v in assigned.get(payload.id, []) if isinstance(v, ast.Dict)]
                    if not literals:
                        _unreadable()
                        continue
                    # `data["source"] = …` AFTER the literal is a payload key too, and one the
                    # dict walk cannot see: `memory_read.source` reached the log undeclared while
                    # this scan reported the type fully covered. A subscript write is `maybe` —
                    # every one found so far is conditional, and a scan that called it `always`
                    # would be claiming more than it checked.
                    # THE LITERAL NEAREST ABOVE THE CALL, not the scope's last one: a function
                    # that builds several payloads reuses the name, and `literals[-1]` then reads
                    # a LATER event's dict as this one's. It is also what makes the subscript
                    # window below sound — the keys collected are the ones assigned between this
                    # payload's literal and this append.
                    above = [v for v in literals if v.lineno <= node.lineno]
                    payload = above[-1] if above else literals[-1]
                    subscripts = subscript_string_keys(
                        scope, payload_name, after=payload.lineno, before=node.lineno)
                if not isinstance(payload, ast.Dict):
                    _unreadable()
                    continue
                always, maybe, opaque = _payload_dict_keys(payload)
                maybe |= subscripts
                row = found.setdefault(etype, {"always": None, "any": set(), "sites": [],
                                               "opaque": False})
                row["sites"].append(f"{path.relative_to(PKG.parent)}:{node.lineno}")
                row["opaque"] = row["opaque"] or opaque
                row["any"] |= always | maybe
                row["always"] = always if row["always"] is None else (row["always"] & always)
    for row in found.values():
        row["always"] = set() if row["opaque"] else (row["always"] or set())
    return found


# ------------------------------------------------------------------------------ the fold's modules
# `events/replay.py` held the whole fold until review 2026-09-22 (EVT-12) began moving its handler
# FAMILIES into sibling modules, `events/replay_<family>.py`. Every guard that read "the fold" off
# `replay.py` alone would have narrowed SILENTLY with each move — a second hand-rolled queue purge
# in a family module is invisible to a count over one file, and a pause-lift that forgot its reason
# is invisible to an AST walk of the wrong tree. So those guards read the fold's source through the
# helpers below, which find the modules by ONE naming rule instead of naming a family, and
# `tests/test_replay_families.py` holds that rule to the table `fold` actually dispatches through:
# a handler defined anywhere else is a red test, not a module these scans skip.
FOLD_MODULE_GLOB = "replay*.py"


def fold_source_paths(pkg: Path = PKG) -> list[Path]:
    """`events/replay.py` first, then every `events/replay_<family>.py` it was split into, sorted."""
    events = pkg / "events"
    families = sorted(p for p in events.glob(FOLD_MODULE_GLOB) if p.name != "replay.py")
    return [events / "replay.py", *families]


def fold_sources(pkg: Path = PKG) -> Iterator[tuple[Path, str]]:
    """Every fold module with its decoded text — `iter_sources` restricted to the fold."""
    for path in fold_source_paths(pkg):
        yield path, _decode(path)


def fold_trees(pkg: Path = PKG) -> Iterator[tuple[Path, ast.AST]]:
    """Every fold module parsed, through the same memo `iter_trees` keeps."""
    wanted = set(fold_source_paths(pkg))
    for path, tree in iter_trees(pkg / "events"):
        if path in wanted:
            yield path, tree


def fold_source(pkg: Path = PKG) -> str:
    """The whole fold as ONE text, `replay.py` first — what `inspect.getsource(replay)` was."""
    return "\n".join(text for _path, text in fold_sources(pkg))


def _fold_handler_functions() -> tuple[dict[str, ast.AST], dict[str, str]]:
    """`(functions the fold can reach, {event type: handler name})`, off `_HANDLERS` itself.

    "Can reach" is the module-level functions of EVERY fold module (`fold_source_paths`) PLUS the
    ones each imports from elsewhere in the package under the local name it calls them by. That hop
    is load-bearing rather than thorough: `_on_promote` reads no key itself and calls
    `_coerce_node_id(d)`, which is `core/models.py::coerce_node_id`. Without the hop that handler
    reports an empty read set and, worse, `fold_stores_payload_whole` counts the call as opaque and
    declares the payload stored. The same hop is what follows a handler in one family module into a
    helper it imports from another.

    ONE namespace over all of them, which is sound only while a name means one function wherever
    the fold says it — so a name two modules bind to DIFFERENT functions is REFUSED, never resolved
    by whichever module was read first. The handler map is the RUNTIME `_HANDLERS`, the table `fold`
    dispatches through however it is assembled, each entry pinned to the definition it really is.
    """
    from looplab.events import replay

    trees: dict[Path, ast.Module] = {}

    def defs_of(path: Path) -> dict[str, ast.AST]:
        if path not in trees:
            trees[path] = ast.parse(_decode(path), filename=str(path))
        return {n.name: n for n in trees[path].body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    funcs: dict[str, ast.AST] = {}
    origin: dict[str, tuple[Path, str]] = {}

    def bind(local: str, path: Path, name: str, node: ast.AST) -> None:
        where = (path.resolve(), name)
        if origin.setdefault(local, where) != where:
            raise AssertionError(
                f"`{local}` names two different functions across the fold modules "
                f"({origin[local]} and {where}); this scan resolves a call by its name and will "
                "not guess which one a handler reaches")
        funcs[local] = node

    paths = fold_source_paths()
    for path in paths:
        for name, node in defs_of(path).items():
            bind(name, path, name, node)
    for path in paths:
        for node in trees[path].body:
            if not (isinstance(node, ast.ImportFrom)
                    and (node.module or "").startswith("looplab.")):
                continue
            source = PKG.parent / (node.module.replace(".", "/") + ".py")
            if not source.is_file():
                continue
            imported = defs_of(source)
            for alias in node.names:
                target = imported.get(alias.name)
                if target is not None:
                    bind(alias.asname or alias.name, source, alias.name, target)
    handlers: dict[str, str] = {}
    for etype, handler in replay._HANDLERS.items():
        where = (Path(inspect.getsourcefile(handler)).resolve(), handler.__name__)
        if origin.get(handler.__name__) != where:
            raise AssertionError(
                f"`{etype}` dispatches to {where}, which is not a module-level function of a fold "
                "module this scan reads (`fold_source_paths`)")
        handlers[etype] = handler.__name__
    return funcs, handlers


def _payload_params(fn: ast.AST) -> list[str]:
    args = fn.args
    return [p.arg for p in (args.posonlyargs + args.args + args.kwonlyargs)]


def _string_params(fn: ast.AST, call: "ast.Call | None") -> dict[str, str]:
    """`{parameter: the string it is bound to}` for one call into *fn*, defaults included.

    `core/models.py::coerce_node_id(d, key="node_id")` reads `d[key]` — a subscript whose key is a
    NAME, so a scan that only sees literals reports that every lifecycle handler reads nothing.
    Binding the constant arguments (and the parameter defaults the caller leaves alone) is what
    turns `_coerce_node_id(d)` back into "reads `node_id`" and `_coerce_node_id(d, "from_node_id")`
    into "reads `from_node_id`".
    """
    params = _payload_params(fn)
    bound: dict[str, str] = {}
    defaults = fn.args.defaults or []
    positional = fn.args.posonlyargs + fn.args.args
    for name, default in zip(positional[len(positional) - len(defaults):], defaults):
        if isinstance(default, ast.Constant) and isinstance(default.value, str):
            bound[name.arg] = default.value
    for name, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults or []):
        if isinstance(default, ast.Constant) and isinstance(default.value, str):
            bound[name.arg] = default.value
    if call is not None:
        for index, arg in enumerate(call.args):
            if (isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                    and index < len(params)):
                bound[params[index]] = arg.value
        for kw in call.keywords:
            if kw.arg and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                bound[kw.arg] = kw.value.value
    return bound


def _keys_read(fn: ast.AST, payload: set[str], funcs: dict[str, ast.AST],
               seen: frozenset[str], bound: "dict[str, str] | None" = None) -> set[tuple[str, str]]:
    """`{(key, "get"|"sub"|"in")}` read off any name in *payload*, following calls that forward it.

    Following matters: eleven handlers read nothing directly and hand the payload to a module-level
    helper (`_coerce_node_id(d)`, `_control_generation_matches(n, d)`), so a scan that stops at the
    handler reports an empty contract for the events whose contract is the most load-bearing.
    """
    out: set[tuple[str, str]] = set()
    bound = bound or {}

    def literal(node: ast.AST) -> "str | None":
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return bound.get(node.id) if isinstance(node, ast.Name) else None

    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("get", "pop", "setdefault")
                and isinstance(node.func.value, ast.Name) and node.func.value.id in payload
                and node.args and literal(node.args[0]) is not None):
            out.add((literal(node.args[0]), "get"))
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id in payload and literal(node.slice) is not None):
            out.add((literal(node.slice), "sub"))
        if (isinstance(node, ast.Compare) and len(node.ops) == 1
                and isinstance(node.ops[0], ast.In)
                and isinstance(node.comparators[0], ast.Name)
                and node.comparators[0].id in payload
                and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)):
            out.add((node.left.value, "in"))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in funcs and node.func.id not in seen):
            callee = funcs[node.func.id]
            params = _payload_params(callee)
            forwarded = {params[i] for i, arg in enumerate(node.args)
                         if isinstance(arg, ast.Name) and arg.id in payload and i < len(params)}
            forwarded |= {kw.arg for kw in node.keywords if kw.arg
                          and isinstance(kw.value, ast.Name) and kw.value.id in payload}
            if forwarded:
                out |= _keys_read(callee, forwarded, funcs, seen | {node.func.id},
                                  _string_params(callee, node))
    return out


def fold_payload_reads() -> dict[str, set[tuple[str, str]]]:
    """`{event type: {(key, how)}}` for every key `replay.fold` reads off that type's payload."""
    funcs, handlers = _fold_handler_functions()
    return {etype: _keys_read(funcs[name], {_payload_params(funcs[name])[2]}, funcs,
                              frozenset({name}))
            for etype, name in handlers.items()}


def fold_stores_payload_whole() -> set[str]:
    """Event types whose handler MAY keep the payload OBJECT — assigned, appended, spread, or handed
    to something `replay.py` does not define. For those the fold has no key contract at all:
    whatever a writer puts in the dict reaches `RunState` and every projection over it.

    AN OVER-APPROXIMATION, and used as one since review 2026-09-22 (EVT-05): a payload handed to
    `set(d)`, a local `dict(d)` copy or a bounded receipt builder counts as stored, which declared
    four types whole that keep nothing of an unknown key. `tests/test_event_payload_contract.py`
    now DECIDES by folding a marker; this only nominates the types that need a valid probe row."""
    funcs, handlers = _fold_handler_functions()

    def stores(fn: ast.AST, payload: set[str], seen: frozenset[str]) -> bool:
        for node in ast.walk(fn):
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Name)
                    and node.value.id in payload):
                return True
            if isinstance(node, ast.Dict) and any(
                    k is None and isinstance(v, ast.Name) and v.id in payload
                    for k, v in zip(node.keys, node.values)):
                return True
            if isinstance(node, ast.Call):
                forwarded = [a for a in list(node.args) + [k.value for k in node.keywords]
                             if isinstance(a, ast.Name) and a.id in payload]
                local = isinstance(node.func, ast.Name) and node.func.id in funcs
                if forwarded and not local:
                    return True                       # imported or method call: opaque to this scan
                if local and node.func.id not in seen:
                    callee = funcs[node.func.id]
                    params = _payload_params(callee)
                    names = {params[i] for i, arg in enumerate(node.args)
                             if isinstance(arg, ast.Name) and arg.id in payload and i < len(params)}
                    names |= {kw.arg for kw in node.keywords if kw.arg
                              and isinstance(kw.value, ast.Name) and kw.value.id in payload}
                    if names and stores(callee, names, seen | {node.func.id}):
                        return True
        return False

    return {etype for etype, name in handlers.items()
            if stores(funcs[name], {_payload_params(funcs[name])[2]}, frozenset({name}))}
