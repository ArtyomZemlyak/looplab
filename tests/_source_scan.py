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
comment above them and CLAUDE.md's "Testing conventions".
"""
from __future__ import annotations

import ast
import inspect
import re
import textwrap
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


def iter_sources(pkg: Path = PKG) -> Iterator[tuple[Path, str]]:
    """Every `.py` under *pkg*, sorted, with its decoded text.

    Sorted so a failure message lists offenders in a stable order — an unsorted `rglob` reports the
    same set in a different order per filesystem, which reads as a flapping test.
    """
    for path in sorted(pkg.rglob("*.py")):
        if EXCLUDED_DIRS.intersection(path.parts):
            continue
        yield path, path.read_text(encoding="utf-8-sig", errors="replace")


def iter_trees(pkg: Path = PKG) -> Iterator[tuple[Path, ast.AST]]:
    """Every source under *pkg* parsed to an AST, with `filename` set so a SyntaxError names it."""
    for path, text in iter_sources(pkg):
        yield path, ast.parse(text, filename=str(path))


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
# `_evaluate` is a DRIVER over `EvalAttempt` and nine phase methods since 2026-09-06 (doc 52 row 21).
# A guard that used to read "the attempt loop" off `inspect.getsource(EvaluateMixin._evaluate)` now
# reads the driver plus every phase, IN THE ORDER THE DRIVER RUNS THEM — so an index-order pin over
# the concatenation still says what it said about one method, and a "called exactly once" pin counts
# across the phases. Prefer naming the phase when the property belongs to one.
EVAL_PHASES = ("_eval_admit", "_eval_prepare_workdir", "_eval_seed_ledgers", "_eval_run_attempt",
               "_eval_settle_outcome", "_eval_salvage", "_eval_decide_repair", "_eval_apply_repair",
               "_eval_write_terminal")


def eval_attempt_functions() -> list:
    """The driver, the nine phases in driver order, then the reset terminal the phases share."""
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
        scopes = [n for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef))]
        for scope in scopes:
            assigned: dict[str, list[ast.AST]] = {}
            for node in ast.walk(scope):
                if (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)):
                    assigned.setdefault(node.targets[0].id, []).append(node.value)
            for node in ast.walk(scope):
                if not (isinstance(node, ast.Call) and node.args):
                    continue
                etype = event_type(node.args[0])
                if etype is None:
                    continue
                payload = (node.args[1] if len(node.args) >= 2 else
                           next((k.value for k in node.keywords if k.arg == "data"), None))
                if payload is None:
                    continue
                if isinstance(payload, ast.Name):
                    literals = [v for v in assigned.get(payload.id, []) if isinstance(v, ast.Dict)]
                    if not literals:
                        continue
                    payload = literals[-1]
                if not isinstance(payload, ast.Dict):
                    continue
                always, maybe, opaque = _payload_dict_keys(payload)
                row = found.setdefault(etype, {"always": None, "any": set(), "sites": [],
                                               "opaque": False})
                row["sites"].append(f"{path.relative_to(PKG.parent)}:{node.lineno}")
                row["opaque"] = row["opaque"] or opaque
                row["any"] |= always | maybe
                row["always"] = always if row["always"] is None else (row["always"] & always)
    for row in found.values():
        row["always"] = set() if row["opaque"] else (row["always"] or set())
    return found


def _fold_handler_functions() -> tuple[dict[str, ast.AST], dict[str, str]]:
    """`(functions the fold can reach, {event type: handler name})`, off `_HANDLERS` itself.

    "Can reach" is `replay.py`'s own module-level functions PLUS the ones it imports from elsewhere
    in the package under the local name it calls them by. That hop is load-bearing rather than
    thorough: `_on_promote` reads no key itself and calls `_coerce_node_id(d)`, which is
    `core/models.py::coerce_node_id`. Without the hop that handler reports an empty read set and,
    worse, `fold_stores_payload_whole` counts the call as opaque and declares the payload stored.
    """
    from looplab.events import types as event_types

    tree = ast.parse((PKG / "events" / "replay.py").read_text(encoding="utf-8-sig"))
    funcs = {n.name: n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for node in tree.body:
        if not (isinstance(node, ast.ImportFrom) and (node.module or "").startswith("looplab.")):
            continue
        source = PKG.parent / (node.module.replace(".", "/") + ".py")
        if not source.is_file():
            continue
        imported = {n.name: n for n in ast.parse(source.read_text(encoding="utf-8-sig")).body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for alias in node.names:
            target = imported.get(alias.name)
            local = alias.asname or alias.name
            if target is not None and local not in funcs:
                funcs[local] = target
    handlers: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_HANDLERS" for t in node.targets)):
            continue
        for key, value in zip(node.value.keys, node.value.values):
            if isinstance(key, ast.Name) and isinstance(value, ast.Name):
                etype = getattr(event_types, key.id, None)
                if isinstance(etype, str):
                    handlers[etype] = value.id
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
    """Event types whose handler keeps the payload OBJECT — assigned, appended, spread, or handed to
    something `replay.py` does not define. For those the fold has no key contract at all: whatever a
    writer puts in the dict reaches `RunState` and every projection over it."""
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
