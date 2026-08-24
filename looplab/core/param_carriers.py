"""PARAM CARRIERS — what number a CONFIGURATION DOCUMENT assigns a declared dotted path.

THE RULE THIS MODULE EXISTS TO STATE. `Idea.params` is a proposal in dotted-path form
(`train.training.batch_size: 8192`). Somewhere in the working set is the artifact that DECIDES what
that path is worth when the node runs. `engine/repair_verify.py::declared_param_overrides` was
written for exactly that comparison and read only `.py` files — and on the task family this box
actually runs, the deciding artifact is a YAML document. The guard therefore returned EMPTY about
the champion of `e5small-dr-unified-v2` (RECALL@100 0.793426), whose own committed
`vectorsearch/configs/config.yaml` — one of the five files it was handed — reads
`batch_size: 512` / `gradient_accumulation_steps: 32` / `n_epochs: 3` against a declaration of
8192 / 2 / 15.

**THE `.py` TEST WAS NEVER THE RULE. IT WAS THE ONLY EXTRACTOR.** The rule is:

    a CARRIER is a file the engine holds that can be read, deterministically and without executing
    anything, as `dotted path -> numeric literal`.

Python source is one such format and `ast` is its extractor (`repair_verify._assigned_numeric_paths`,
which stays where it is). A structured document — YAML, JSON — is another, and there the path is not
something a reader has to reconstruct from a chain of attribute accesses: it IS the nesting. This
module is the extractor for that second family, plus the one rule for resolving a DECLARATION
against it.

WHY THE TWO FAMILIES ARE MATCHED BY DIFFERENT RULES, and it is not an inconsistency:

  * A structured document is a COMPLETE, ROOTED tree. Every leaf's full path from the document root
    is known, so "this declaration names two different leaves" is DECIDABLE, and it is exactly the
    `PARAM_OVERRIDE_MIN_PARTS` rule one level up — a name that resolves to two paths is a word, not a
    path. It is REFUSED, never guessed at. Measured on this box, a bare `batch_size` resolves to
    THREE leaves of `vectorsearch/configs/config.yaml` (`train.training`, `train.negatives.mining`,
    `test.retriever.model_settings`) and a four-leaf set once `adapter` is configured.
  * Python source is NOT a complete tree. A target's path is rooted at whatever local the code
    happened to bind (`config`, `cfg`, `self.conf`), so two assignments matching one declared suffix
    are two ASSIGNMENTS — not one ambiguous declaration — and reporting both is right. That path's
    behaviour is unchanged by this module.

WHAT A "NUMBER" IS IN A YAML DOCUMENT, and why the answer is not just "whatever PyYAML resolved".
PyYAML implements the YAML **1.1** float resolver, whose regex requires a `.` in the mantissa: the
scalar `5e-3` is left as the STRING `'5e-3'`, while YAML 1.2's core schema, `float()`, and every
pydantic model with a `float` field all read it as 0.005. Taking PyYAML's word for it would have
silently dropped two of the forty-one divergences measured over `runs/` — both on
`rubertlite-dr-unified-v8` node 12, a node that RECORDED a metric (0.761400): declared
`loss.temperature` 0.05 against a carrier holding `5e-3`, and declared
`train.training.learning_rate` 0.001 against `5e-4`. A ten-fold change in the temperature of the
loss, invisible because of a resolver's regex.

So a PLAIN scalar whose text `float()` accepts to a finite value is a number here, whatever tag the
resolver gave it. A QUOTED scalar is not, and that line is the whole guard against over-reading: an
author who wrote `"512"` wrote a string, and a rung that read it as 512 would be resolving on the
document's behalf. `.json` is not coerced at all — JSON's type system is exact and a JSON string is
a string in every reader there has ever been.

NOTHING HERE EXECUTES, IMPORTS OR RESOLVES ANYTHING. `yaml.compose` with `SafeLoader` builds the
node graph without constructing Python objects, which is also what makes the line numbers available;
the walk is bounded by `MAX_DOCUMENT_NODES` and by an explicit visited set, because an anchor
referenced from two places is ONE composed node and a naive walk of an alias-heavy document is
exponential in the number of anchors.
"""
from __future__ import annotations

import ast
import json
import math

# Files this module can read as a configuration document. The suffix is a statement about the
# FORMAT — i.e. about which parser applies — and never about whether a file is "a config": a
# document that names none of the declared paths simply produces no rows.
DOCUMENT_SUFFIXES = (".yaml", ".yml", ".json")

# Total composed nodes one document may contribute. A bound on work, not a rule: over the ceiling the
# rest of the document is unread, which can only ever UNDER-report — the direction every rung built
# on this one already fails in. The largest real carrier on this box composes 411 nodes.
MAX_DOCUMENT_NODES = 20_000

# Nesting a path may reach. Deeper leaves are skipped; a declaration is at most a handful of parts and
# a path this long is a data structure, not a configuration coordinate.
MAX_DOCUMENT_DEPTH = 24

# How the declaration was matched against the document, recorded so a reader can tell "the document
# says this at exactly the path you named" from "the document says this at ONE longer path".
MATCH_EXACT = "exact"
MATCH_SUFFIX = "suffix"

# Why a declaration got no answer from a document. `ambiguous` is a REFUSAL and is deliberately not
# the same word as `absent`: one means the carrier is silent about this coordinate, the other means
# the carrier answers it in two places and the declaration does not say which.
UNRESOLVED_ABSENT = "absent"
UNRESOLVED_AMBIGUOUS = "ambiguous"
# Deliberately only TWO members. "the carrier names this path and its value is not a number" would
# be a third useful fact, and it is not recorded because the extractors keep numeric leaves only —
# a slug no input can produce is a dead branch, and this repo has paid for those.
UNRESOLVED_REASONS = (UNRESOLVED_ABSENT, UNRESOLVED_AMBIGUOUS)


def is_document_carrier(path) -> bool:
    """Whether `path`'s suffix names a structured format this module can read."""
    text = str(path or "").lower()
    return any(text.endswith(suffix) for suffix in DOCUMENT_SUFFIXES)


def finite_number(value):
    """`float(value)` when that is a finite number, else None. Bools are NOT numbers.

    `True` is `isinstance(int)` and comparing it against a declared `1.0` would report an agreement
    nobody wrote — the same exclusion `repair_verify._numeric_literal` makes on the Python side, for
    the same reason. Non-finite is dropped on both sides too: a value that rides onto a durable event
    as a bare `NaN`/`Infinity` is not JSON, and `nan != anything` would convict every declaration.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def _plain_scalar_number(text):
    """The number a PLAIN YAML scalar's own text denotes, or None.

    This is the YAML-1.1-resolver repair the module docstring names, and it is bounded to what
    `float()` itself accepts so that no second numeric grammar is invented here. `nan`/`inf` are
    accepted by `float()` and rejected by `finite_number`, which is the intended composition.
    """
    body = str(text or "").strip()
    if not body:
        return None
    try:
        return finite_number(float(body))
    except (TypeError, ValueError):
        return None


def yaml_numeric_paths(source) -> dict:
    """`(dotted path) -> (value, line)` for every numeric leaf of one YAML document.

    A 1-based line, so the row reads the way an editor does. Duplicate keys in one mapping are
    LAST-WINS, matching what `yaml.safe_load` itself would build, so this map and the document a
    loader would produce cannot disagree about which of two spellings a config sees.

    A document that does not compose answers `{}` — an agent may commit anything, and a parse error
    is not evidence about a parameter. Same rule, same reason, as the `SyntaxError` branch on the
    Python side.
    """
    try:
        import yaml
    except Exception:                                    # pragma: no cover — pyyaml is a hard dep
        return {}
    try:
        root = yaml.compose(source or "", Loader=yaml.SafeLoader)
    except Exception:                                    # noqa: BLE001 — a parse error is not evidence
        return {}
    if root is None:
        return {}
    out: dict = {}
    budget = [MAX_DOCUMENT_NODES]
    seen: set = set()

    def _walk(node, parts) -> None:
        if budget[0] <= 0 or len(parts) > MAX_DOCUMENT_DEPTH:
            return
        budget[0] -= 1
        # An ANCHOR referenced twice composes to ONE node object reached by two paths. Visiting it
        # once per path is correct and terminating for a tree; the visited set is what keeps a
        # RECURSIVE anchor (`&a {k: *a}`, which composes into a cycle) from running forever.
        key = (id(node), parts)
        if key in seen:
            return
        seen.add(key)
        if getattr(node, "value", None) is None:
            return
        tag = str(getattr(node, "tag", ""))
        if tag.endswith(":map") or tag.endswith(":omap"):
            for child_key, child_val in node.value:
                name = getattr(child_key, "value", None)
                if not isinstance(name, str) or not name:
                    continue                             # a non-string key names no coordinate
                _walk(child_val, parts + (name,))
            return
        if tag.endswith(":seq") or tag.endswith(":set"):
            # A SEQUENCE INDEX IS NOT A CONFIGURATION COORDINATE. `Idea.params` keys are dotted
            # names; nothing declares `layers.3.width`, and minting `layers.0` paths would put
            # positional noise in front of the suffix rule for no declaration that can ever reach it.
            return
        if not parts:
            return
        value = None
        if tag.endswith(":int") or tag.endswith(":float"):
            # The resolver already said this is a number; take its own construction of the text so
            # `0x10`, `1_000` and YAML 1.1's sexagesimals mean here exactly what a loader means.
            try:
                value = finite_number(yaml.safe_load(node.value))
            except Exception:                            # noqa: BLE001 — fall through to the text
                value = None
        if value is None and tag.endswith(":str") and not getattr(node, "style", None):
            value = _plain_scalar_number(node.value)      # the 1.1-resolver repair; PLAIN only
        if value is None:
            return
        line = int(getattr(getattr(node, "start_mark", None), "line", 0)) + 1
        out[parts] = (value, line)

    _walk(root, ())
    return out


def json_numeric_paths(source) -> dict:
    """`(dotted path) -> (value, 0)` for every numeric leaf of one JSON document.

    LINE 0 AND IT MEANS "no line", not line zero: `json.loads` does not carry marks, and inventing a
    line by re-scanning the text would put a number in a durable row that nothing derived it from.
    Readers already treat `line` as advisory (`ParamOverride.as_row` ships it beside `file`).

    A JSON string is NEVER coerced — see the module docstring. JSON's type system is exact and both
    ends of this comparison already know what a number is.
    """
    try:
        doc = json.loads(source or "")
    except Exception:                                    # noqa: BLE001 — a parse error is not evidence
        return {}
    out: dict = {}
    budget = [MAX_DOCUMENT_NODES]

    def _walk(node, parts) -> None:
        if budget[0] <= 0 or len(parts) > MAX_DOCUMENT_DEPTH:
            return
        budget[0] -= 1
        if isinstance(node, dict):
            for name, child in node.items():
                if isinstance(name, str) and name:
                    _walk(child, parts + (name,))
            return
        if isinstance(node, (list, tuple)):
            return                                       # see `yaml_numeric_paths`: an index is not a coordinate
        if not parts:
            return
        value = finite_number(node)
        if value is not None:
            out[parts] = (value, 0)

    _walk(doc, ())
    return out


def document_numeric_paths(path, source) -> dict:
    """The numeric leaf map for one carrier, dispatched on its format. `{}` for a non-document."""
    name = str(path or "").lower()
    if name.endswith(".json"):
        return json_numeric_paths(source)
    if name.endswith(".yaml") or name.endswith(".yml"):
        return yaml_numeric_paths(source)
    return {}


def resolve_declaration(paths: dict, parts):
    """What one declared dotted path is worth in one document: `(value, line, how)` or
    `(None, 0, <reason>)`.

    THE THREE RUNGS, strongest first, and the order is the statement:

      1. **The declaration IS a full path.** `train.training.batch_size` naming the document's own
         `train.training.batch_size` is not a suffix question at all, and it wins outright even when
         longer paths also end in those parts. A declaration that names the whole path has said which
         leaf it means.
      2. **Exactly one longer path ENDS in the declaration.** `loss.temperature` against a document
         holding only `train.loss.temperature`. Measured over `runs/`, 238 of 593 resolvable
         declarations land here — the carrier nests one level deeper than the coordinate the
         Researcher writes — so refusing this rung would blind the rung to 40 % of the corpus.
      3. **Two or more do.** REFUSED as `ambiguous`. This is where the ceiling on guessing is: the
         declaration is a word, not a path, and no tie-break is admissible — not "the shortest", not
         "the first", not "the one under `train`". Measured over `runs/`, 5 declarations land here.

    Returns the reason rather than raising, because every caller wants to RECORD "I could not say"
    and none of them may treat it as agreement.
    """
    if not isinstance(paths, dict) or not paths:
        return None, 0, UNRESOLVED_ABSENT
    want = tuple(parts or ())
    if not want:
        return None, 0, UNRESOLVED_ABSENT
    hit = paths.get(want)
    if hit is not None:
        return float(hit[0]), int(hit[1]), MATCH_EXACT
    matches = [p for p in paths if len(p) > len(want) and tuple(p[-len(want):]) == want]
    if len(matches) == 1:
        value, line = paths[matches[0]]
        return float(value), int(line), MATCH_SUFFIX
    if matches:
        return None, 0, UNRESOLVED_AMBIGUOUS
    return None, 0, UNRESOLVED_ABSENT


# ================================================================================================
# THE PYTHON CARRIER. Moved here from `engine/repair_verify.py` on 2026-08-20 so that the guard and
# the applied-configuration record read Python through ONE body — see the re-export shim there for
# the measurement that forced it (14 coordinates whose Python and document carriers state different
# numbers, 9 on scored nodes, one of them a champion).
#
# Its matching rule is TARGET-first and stays that way: a Python target's path is rooted at whatever
# local the code bound, so the tree is INCOMPLETE and two assignments matching one declared suffix
# are two assignments rather than one ambiguous declaration. `resolve_declaration` above is the
# document rule and must not be used here.
# ================================================================================================


def numeric_literal(node):
    """The float value of a numeric literal AST node (`4096`, `-1`, `0.5`), else None.

    `ast.UnaryOp(USub)` is spelled out because a negative literal is not one node in Python's
    grammar. Anything with a NAME or a CALL in it is not a literal and is not resolved — see the
    docstring's fourth bound. Bools are excluded: `True` is `isinstance(int)` and comparing it to a
    declared `1.0` would report agreement nobody wrote."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = numeric_literal(node.operand)
        return None if inner is None else -inner
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        val = float(node.value)
        # `1e400` parses to `inf` and a huge int overflows the conversion; either would ride onto a
        # durable event as a bare `Infinity`, which is not JSON. Same rule as the declared side.
        return val if math.isfinite(val) else None
    return None


def assignment_target_parts(node):
    """The dotted path an assignment TARGET names, outermost-last, or None if it names no path.

    `config.train.training.batch_size` -> `["config", "train", "training", "batch_size"]`, and
    `cfg["train"]["training"]["batch_size"]` -> the same tail, because a config object reached by
    attribute and one reached by key are the same declaration to the reader this serves. A subscript
    whose index is not a plain string constant (`row[i]`) makes the whole target unreadable and
    answers None — a partial path would silently match on its suffix, which is the one thing the
    suffix rule below cannot survive."""
    parts: list = []
    cur = node
    while True:
        if isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        elif isinstance(cur, ast.Subscript):
            key = cur.slice
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                return None
            parts.append(key.value)
            cur = cur.value
        elif isinstance(cur, ast.Name):
            parts.append(cur.id)
            break
        else:
            return None                       # a call, a literal, a tuple — names no stable path
    parts.reverse()
    return parts


def python_numeric_paths(source: str) -> dict:
    """`(dotted path) -> (value, line)` for every numeric-literal assignment in one Python source.

    LAST WRITE WINS on a repeated path, matching what the interpreter would do if both ran in order —
    and if they are in exclusive branches the rung is over-reading either way, which is the residual
    the docstring states rather than guesses at. Unparseable source answers `{}`: an agent may commit
    anything, and a `SyntaxError` is not evidence about a parameter.

    **SOURCE ORDER IS RESOLVED EXPLICITLY, and it is not a tidy-up.** `ast.walk` is BREADTH-FIRST, so
    it yields every module-level statement before anything nested inside one, and "last write" under
    it means DEEPEST-then-latest rather than last-in-the-file. That inverts the rule this docstring
    states, in the direction the whole rung is not allowed to fail in: a node whose module-level
    `cfg.train.training.batch_size = 8192` AGREES with its declaration is convicted anyway when a
    helper `def` earlier in the file carries a different default, because the nested assignment is
    visited last and overwrites the agreeing one. That row reaches `champion_caveats` as
    `params_overridden` on the run's best number. It also hands an adversarial candidate both
    directions for free — a one-line decoy `def _unused(): cfg.a.b = <the declared value>`, nested
    ANYWHERE in the file, outranks a real module-level divergence and answers "agrees" — and it
    breaks `declared_param_overrides`' baseline attribution, which acquits only on an EQUAL prior
    value: a repair that merely DELETES a dead helper carrying `1024`, over a module body that said
    `4096` before and after, was charged with introducing the 4096 (driven; `tests/
    test_repair_verification.py` keeps all three). So the nodes are sorted by `(lineno, col_offset)`
    before the dict is written, and the dict then means what it says. (Textual order still is not execution
    order — a nested `def` may run after the module body — which is exactly why the module docstring
    says this is a statement about two artifacts and never "this is what ran".)

    Walks `Assign` and `AnnAssign` (`config.train.batch_size: int = 4096`) and deliberately NOT
    `AugAssign`: `x += 1` carries no absolute value to compare a declaration against.
    """
    try:
        tree = ast.parse(source or "")
    except (SyntaxError, ValueError, RecursionError, MemoryError):  # noqa: BLE001 — not evidence
        return {}
    found: list = []
    # SCOPE DEPTH per statement: 0 for the module body, +1 inside every `def`/`class`/`lambda`.
    # Computed here rather than inferred from `col_offset`, which a continuation line or a
    # module-level `if` would both get wrong.
    depth_of: dict = {}

    def _mark(node, depth: int) -> None:
        for child in ast.iter_child_nodes(node):
            deeper = depth + isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                ast.ClassDef, ast.Lambda))
            depth_of[id(child)] = deeper
            _mark(child, deeper)

    depth_of[id(tree)] = 0
    _mark(tree, 0)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        val = numeric_literal(value)
        if val is None:
            continue
        found.append((depth_of.get(id(node), 0), getattr(node, "lineno", 0),
                      getattr(node, "col_offset", 0), targets, val))
    out: dict = {}
    # SHALLOWEST WINS, then latest. `out[key] = ...` is last-write-wins, so the sort runs DEEPEST
    # first and the module body last: an assignment in the module body certainly executes, one
    # inside a `def` only if something calls it, and letting the second outrank the first is what
    # made a one-line `def _unused(): cfg.a.b = <declared value>` — placed ANYWHERE in the file —
    # a free acquittal, and a nested default a free conviction of an agreeing module body. Within
    # ONE depth the rule is unchanged and is textual: later overwrites earlier. (Textual order still
    # is not execution order, which is exactly why the module docstring says this compares two
    # artifacts and never claims "this is what ran".)
    for _depth, lineno, _col, targets, val in sorted(
            found, key=lambda f: (-f[0], f[1], f[2])):
        for tgt in targets:
            parts = assignment_target_parts(tgt)
            if parts:
                out[tuple(parts)] = (val, lineno)
    return out


def resolve_declaration_python(paths: dict, parts) -> dict:
    """`{value: (file-relative line)}` for every Python assignment whose path ENDS in `parts`.

    A dict and not a single value, because the Python tree is incomplete: several assignments may
    reach one declared coordinate and the caller — not this function — decides what a disagreement
    means. `declared_param_overrides` reports each of them as its own row; `runtime/applied_params.py`
    treats two different values as a CONFLICT it refuses to settle.
    """
    out: dict = {}
    want = tuple(parts or ())
    if not want or not isinstance(paths, dict):
        return out
    for target, entry in paths.items():
        if len(target) >= len(want) and tuple(target[-len(want):]) == want:
            value, line = entry
            out.setdefault(float(value), int(line))
    return out


def node_params_brief(node, *, cap: int = 12) -> str:
    """What this node's coordinates WERE, with what was proposed in brackets where the two differ.

    THE ORDER IS THE POINT. `Idea.params` is a PROPOSAL — with `params_style: "none"` the engine
    applies nothing and the Developer realises it by editing the repo, so a repair that fits a run
    into memory silently moves the coordinates and the proposal stays frozen at what was asked for.
    Every reader that printed `idea.params` was therefore printing a wish.

    Measured on `runs/e5small-dr-unified-v4` node 3, the run's own long-standing champion: proposed
    `batch_size 8192 / accum 2 / n_epochs 15`, APPLIED `4096 / 4 / 3` after six repairs — a quarter
    of the effective batch and a fifth of the schedule. `agents/roles.py::_state_brief` fed the
    proposal to the Researcher on every single proposal cycle as "Best so far: … params=…", so every
    later idea was sized against a recipe that never ran. (The same reading cost the author of this
    function an hour and a wrong "the experiment is confounded" claim about node 8, which is in fact
    a clean one-knob delta from node 3's APPLIED coordinates.)

    Falls back to the declaration, unmarked, when no applied record exists — a pre-2026-08-20 node,
    or one whose metric was never bound. Absent evidence is not evidence of agreement, so nothing is
    bracketed in that case: the reader sees exactly what it saw before.
    """
    idea = getattr(node, "idea", None)
    declared = dict(getattr(idea, "params", None) or {})
    provenance = getattr(node, "metric_provenance", None)
    record = provenance.get("applied_params") if isinstance(provenance, dict) else None
    applied = record.get("applied") if isinstance(record, dict) else None
    if not isinstance(applied, dict) or not applied:
        return repr(declared) if declared else "(none recorded)"
    diverged = record.get("diverged") if isinstance(record, dict) else None
    moved = {}
    if isinstance(diverged, list):
        for row in diverged:
            if isinstance(row, dict) and isinstance(row.get("param"), str):
                moved[row["param"]] = row.get("declared")
    parts = []
    for name in sorted(applied)[:max(0, cap)]:
        value = applied[name]
        if name in moved:
            parts.append(f"{name}={value} (proposed {moved[name]})")
        else:
            parts.append(f"{name}={value}")
    omitted = max(0, len(applied) - cap)
    tail = f", +{omitted} more" if omitted else ""
    # Name the divergence COUNT even when the diverged entries fall outside the cap: "these are the
    # numbers that ran" is only trustworthy if the reader is also told how many of them moved.
    note = f" [{len(moved)} of {len(applied)} moved from the proposal]" if moved else ""
    return ", ".join(parts) + tail + note


def effective_params(node) -> dict:
    """The coordinates a node actually ran at: the applied record where it exists, the declaration
    where it does not. ONE spelling, so every reader answers "what were this node's numbers?" the
    same way instead of half of them reading the proposal."""
    provenance = getattr(node, "metric_provenance", None)
    record = provenance.get("applied_params") if isinstance(provenance, dict) else None
    applied = record.get("applied") if isinstance(record, dict) else None
    declared = dict(getattr(getattr(node, "idea", None), "params", None) or {})
    if isinstance(applied, dict) and applied:
        merged = dict(declared)
        merged.update(applied)          # applied WINS; a declared-only path survives as context
        return merged
    return declared


def resolved_params(node, nodes_by_id, *, _depth: int = 0) -> dict:
    """A node's coordinates RESOLVED up its lineage — its own record layered over its parent's.

    A node inherits its parent's workspace (`repo_developer.implement_from` hands the parent's files
    to the build), so a path ABSENT from a node's own record means "inherited", never "differs".
    Comparing bare records reads absence as change and produces nonsense: on
    `runs/e5small-dr-unified-v4`, node 8 — a card that claims ONE knob and is one — came out as a
    fifteen-knob delta from node 3, purely because node 3's record names twelve paths that node 8's
    one-line declaration does not repeat.

    Cycle- and depth-guarded: `parent_ids` is persisted data, and a malformed chain must cost a
    shallower answer, never a hang."""
    if node is None or _depth > 64:
        return {}
    parents = list(getattr(node, "parent_ids", None) or [])
    base: dict = {}
    for pid in parents[:1]:             # the PRIMARY parent is the one whose workspace was seeded
        base = resolved_params(nodes_by_id.get(pid), nodes_by_id, _depth=_depth + 1)
    merged = dict(base)
    merged.update(effective_params(node))
    return merged


def node_knob_delta(node, parent, nodes_by_id=None) -> list[str]:
    """Which coordinates this node moved relative to its parent — the arbiter of "one hypothesis,
    minimal change".

    Compared on EFFECTIVE values (see `effective_params`), because the question is what the two
    experiments differed by when they RAN, not what was asked for. On
    `runs/e5small-dr-unified-v4` node 8 vs node 3 that is exactly one path
    (`train.training.max_grad_norm`) — the card claims a one-knob delta and the delta is one. Read
    off the DECLARATIONS instead it looks like three, because node 3's proposal says 8192/2/15 while
    it ran 4096/4/3; the author of this function made that mistake and called a clean experiment
    confounded.

    It is also the deterministic test for whether a repair changed the HYPOTHESIS or merely fitted
    it to the machine: intersect this list with the paths the hypothesis names. Empty intersection
    means the same card still describes the experiment; non-empty means it does not.

    Returns sorted paths, both directions (added, removed and changed). Empty when there is no
    parent, or when nothing moved."""
    if parent is None:
        return []
    if nodes_by_id:
        a = resolved_params(node, nodes_by_id)
        b = resolved_params(parent, nodes_by_id)
    else:
        # No lineage to resolve against: compare the child's OWN paths only. Absence on either side
        # is "inherited", so a path the child never mentions cannot be a change it made.
        a, b = effective_params(node), effective_params(parent)
        return sorted({k for k in a if a.get(k) != b.get(k)})
    return sorted({k for k in set(a) | set(b) if a.get(k) != b.get(k)})
