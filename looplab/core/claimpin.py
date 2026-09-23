r"""A recorded claim, pinned to the site that decides it — and re-derived from the tree.

WHY THIS EXISTS, measured. Seven claims failed in the two days to 2026-08-20 and every one has the
same shape: a fact recorded in ONE place whose truth lives in ANOTHER, with nothing connecting them.
The most expensive was aimed at an agent — `runs/e5small-dr-unified-v3`'s goal stated the manual
e5-small recipe as "16k overall = 8k x 2 GPUs" and labelled it VERIFIED. That row belongs to
`rubert-tiny-lite`; the e5-small baseline block names batch 1750 on 4 devices. All three nodes of
that run died chasing a per-device 8192 that needs ~530 GiB on a 139.8 GiB card.

THE PART OF THE SHAPE THAT DECIDES THE MECHANISM. Four of the seven were **false on the day they
were written**, not decayed: two BACKLOG rows whose subject had landed hours earlier, doc 27's
eval-corpus banner (both tests it says are missing predate the document), and the goal above. So an
EXPIRY — a claim that goes red after N days — would have caught none of them: an expiry that has
not elapsed is green, and a born-false claim is green forever inside its window. What separates the
two halves is not age. It is that writing a claim costs nothing while checking one costs a lookup,
and the lookup is skipped at authoring exactly as it is skipped at reading. **A predicate over the
deciding site is the right primitive because writing it FORCES the lookup**: an author who has to
name the line that decides "8k x 2 is the e5-small recipe" goes to that file, fails to find it, and
never types the sentence. That is the one minute that would have bought three nodes back.

TWO HALVES, deliberately different in what they cost the author.

1. **Citations — zero adoption cost, derived.** ``<pkg>/<mod>.py::<symbol>`` is already this repo's
   house style for "the truth lives there": 653 of them in `looplab/`, 471 distinct. Nothing
   resolved them until now, and `docs/BACKLOG.md` §0.3 records the outcome (8 of 8 line citations
   dead). `citation_defects()` re-derives every one against the real tree. The ``<mod>.py:NNN`` form is
   REFUSED outright rather than resolved: a line number is falsified by any edit above it, so it is
   unmaintainable by construction — CLAUDE.md already says to locate by SYMBOL, and this is that
   rule with a guard behind it.

2. **`CLAIM[<slug>] … decided:<predicate>` — opt-in, for the facts a citation cannot carry**: a
   number, a behaviour, a row in a file outside the repo. The predicate vocabulary is the open-item
   index's, evaluated by the code below, which `tests/test_open_item_index.py` also imports — one
   implementation, because `docs/BACKLOG.md` §0.7 found four implementations of one claim/verdict
   join and the drift was between the copies.

WHAT A RED MEANS HERE IS THE OPPOSITE OF WHAT IT MEANS IN THE INDEX, and the two tokens are
distinct so the two readings can never be confused. A red `OPEN[…]` means the item SHIPPED: delete
the marker. A red `CLAIM[…]` means **the sentence is now false**: fix the sentence, or fix the code
it describes. Deleting the pin and keeping the sentence is the one move that defeats this, which is
why the pin's slug is greppable in one command and why the failure message says so.

`decided:` was chosen over `claim:` on a measurement: `claim:` already occurs 64 times in this tree
(research claims are domain vocabulary here), which is the `STILL OPEN` collision the open-item
index was designed to avoid. `decided:` occurs zero times.

THE ABSOLUTE-PATH RULE. A task goal legitimately cites the operator's own machine
(`/home/jovyan/data/vecsearch_benchmarks_readable.md`); a claim inside the repo never may, because
the suite must pass against a bare `git archive HEAD` tree. `predicate_holds` therefore takes
`allow_absolute`, the pytest carrier passes False and the goal carrier passes True.

Run it by hand over a task file before submitting a run::

    python -m looplab.core.claimpin /home/jovyan/data/e5small-v4-task.json

Both halves run in the suite over the repo itself (`tests/test_claim_pins.py`).
"""
from __future__ import annotations

import functools
import io
import json
import tokenize
import re
import sys
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------------------------
# The two greppable tokens.

# One key per pinned claim, exactly as `OPEN[<slug>]` is one key per open item. The slug is the
# identity so the pin survives the sentence moving between files.
CLAIM_MARKER = re.compile(r"\bCLAIM\[([a-z0-9][a-z0-9-]{2,60})\]")
# `decided:` and not `proof:`: the open-item index's `proof:` says "this item is still OPEN
# because…", and its red means delete the marker. This one says "this sentence is TRUE because…",
# and its red means the sentence lies. Sharing a token would merge two opposite repair actions.
# Either a bare predicate (no whitespace) or a BACKTICK-QUOTED one, because a real literal has
# spaces in it — `mount: bool = True` is the line that decides whether repo data is copied or
# symlinked, and a predicate grammar that cannot quote it forces the author to pick a weaker literal,
# which is the satisfiable-by-anything pin this convention exists to refuse.
# The vocabulary, once, as BOTH a regex alternation and a tuple. `tests/test_claim_pins.py` and
# `tests/test_open_item_index.py` each carried their own `startswith((...))` list of kinds beside
# their own regex, so admitting `line:` to the scanners on 2026-08-21 left the open index READING a
# predicate its own well-formedness check then rejected — the guard caught it, which is the system
# working, but the second spelling is why there was anything to catch.
KINDS: tuple[str, ...] = ("present:", "absent:", "missing:", "line:")
_KINDS = "|".join(KINDS)
DECIDED = re.compile(rf"decided:(?:`((?:{_KINDS})[^`]+)`|((?:{_KINDS})\S+))")
# THE SAME GRAMMAR FOR THE OPEN-ITEM INDEX, and it lives here for the reason this module exists at
# all: `predicate_holds` was moved out of the two guards because "two evaluators would eventually
# disagree about what `present:` means". The SCANNERS were left behind, and by 2026-08-21 they had
# already drifted — `tests/test_claim_pins.py` accepted `line:` and the backtick-quoted form,
# `tests/test_open_item_index.py` accepted neither, so the two indexes disagreed about which
# predicates EXIST while sharing the one that evaluates them.
#
# WHAT THE DRIFT COST, measured the day it was found: an item of the shape "the DEFAULT is wrong"
# had no expressible falsifier. `docs/BACKLOG.md` §0.1 #7's live half is `Settings.landlock`
# shipping `"off"`; the only predicate that discriminates it is
# `line:landlock&&"off"@looplab/core/config.py` — True today, False the moment the default flips —
# and `line:` was exactly what the open-item scanner did not admit. The one whitespace-free spelling
# in the tree sits in a COMMENT, which `satisfied_only_by_prose` is there to reject. So the entry
# stayed OUTSIDE the guard, which is how nine other ranked entries got there: three were checked
# against the tree that day and none of the three still described it.
#
# `absent:`/`present:`/`missing:` keep meaning exactly what they meant; this only widens what the
# OPEN scanner will READ, and every predicate is still evaluated by the one function below.
PROOF = re.compile(rf"proof:(?:`((?:{_KINDS})[^`]+)`|((?:{_KINDS})\S+))")


def proof_predicate(match: "re.Match") -> str:
    """The predicate text of a `proof:` match, quoted or bare — the sibling of
    `decided_predicates`, and deliberately its twin rather than a second spelling."""
    return match.group(1) or match.group(2) or ""

# The window a pin's own `decided:` clause must live in — a docstring paragraph, a comment block or
# a markdown row all fit, and it is short enough that a clause cannot describe the NEXT claim.
WINDOW = 900

# ``<pkg>/<mod>.py::Symbol`` / ``<mod>.py::Class.method`` — the house style for "the truth is over there".
# The lookbehind keeps it off URLs, longer paths and dotted continuations.
CITATION = re.compile(
    r"(?<![\w/.-])((?:[a-z_][a-z0-9_]*/)*[a-z_][a-z0-9_]*\.py)::([A-Za-z_][A-Za-z0-9_.]*)")
# The form this module refuses. Not "hard to check" — UNCHECKABLE: an edit anywhere above the cited
# line silently re-points it, which is why 8 of 8 went dead before anyone looked.
LINE_CITATION = re.compile(r"(?<![\w/.-])((?:[a-z_][a-z0-9_]*/)*[a-z_][a-z0-9_]*\.py):(\d+)\b")
# A BARE test-file citation: `tests/<name>(dot)py` with no `::symbol` (review 2026-09-22; doc 50 CO-07).
# `CITATION` above checks the `::` form only, so a comment that names the test holding it true — the
# commonest way this codebase says "pinned by" (371 of them in `looplab/` on 2026-09-23) — could name
# a file that never existed and nothing looked: `core/llm.py` cited a `test_llm_reexport_seam` from
# 2026-08-03 on (a review ledger flagged it a month later, and it still stood three weeks after
# that), and `runtime/applied_params.py` a `test_applied_params` whose check lives in
# `test_param_carriers`. Only the `tests/` prefix is read — it is repo-root relative by construction,
# so it resolves ONE way, where a bare `train.py` in a docstring is an example of a user's repo, not
# a citation of this one. A path meant as history rather than as a citation is spelled `(dot)py`, as
# `citation_defects`'s own docstring does.
TEST_PATH_CITATION = re.compile(
    r"(?<![\w/.-])(tests/(?:[a-z_][a-z0-9_]*/)*[a-z_][a-z0-9_]*\.py)(?!::)")

_SKIP_DIRS = {".git", ".claude", "runs", "node_modules", "dist", "site", "__pycache__",
              ".pytest_cache", ".mypy_cache", ".venv", "venv", "build",
              # JupyterLab keeps a `<name>-checkpoint.md` copy of every file edited in it, markers
              # included — a second declaration of every slug the original carries, so the index
              # went red on the box that edits docs in the hub (review 2026-09-22, TST-08;
              # `tests/_source_scan.py::EXCLUDED_DIRS` already skipped it for the source walk).
              ".ipynb_checkpoints"}
_TEXT_SUFFIXES = {".py", ".md", ".js", ".jsx", ".html", ".txt", ".toml", ".yml", ".yaml"}

# AN AUDIT DOC IS ITS PROTOCOL BEFORE IT IS ITS RESULT (review 2026-09-22, TST-08). A
# `docs/audit/<x>.md` is written first as the protocol a box run follows and only later carries the
# measurement, one dated `RESULT <yyyy-mm-dd> …` line per run. So the falsifier of an item that owes
# a measurement is `absent:RESULT 20@docs/audit/<x>.md` — open until the first dated RESULT lands —
# and neither of the two shapes the index held:
#   * `missing:docs/audit/<x>.md` (six items) reads the PROTOCOL's arrival as the item shipping, and
#     the index's own instruction for a proof that stops holding is "delete the marker";
#   * `absent:RESULT 2026-@…` (three items) can no longer fire once the first result is dated 2027.
# `missing:` stays legal while the doc does not exist (an `absent:` proof may not point at nothing);
# the day it is written, `predicate_holds` says to re-point, not to delete.
AUDIT_DIR = "docs/audit/"
AUDIT_RESULT_LITERAL = "RESULT 20"

# Every token whose LINE is stripped before any predicate reads a file. Both index families are
# here on purpose: without it an `absent:` proof is falsified by the line stating it and a
# `present:` one is satisfied by its own marker text — the "a comment can satisfy the pin" failure
# that `tests/test_open_item_index.py` was fixed for on 2026-08-19, in one place for both.
_MARKER_TOKENS = ("OPEN[", "DECLINED[", "CLAIM[", "proof:", "measured:", "decided:")


def tracked_text_files(root: Path) -> list[Path]:
    """Every readable text file under `root`, minus the directories no guard should read."""
    out: list[Path] = []
    stack = [root]
    while stack:
        d = stack.pop()
        for child in d.iterdir():
            if child.is_symlink():
                continue
            if child.is_dir():
                if child.name not in _SKIP_DIRS and not child.name.endswith(".egg-info"):
                    stack.append(child)
            elif child.suffix in _TEXT_SUFFIXES:
                out.append(child)
    return sorted(out)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def text_without_markers(path: Path) -> str:
    """The file as a predicate sees it: every line carrying a marker token is removed.

    So a marker can neither satisfy nor falsify itself, in EITHER index.
    """
    return "\n".join(line for line in read_text(path).splitlines()
                     if not any(tok in line for tok in _MARKER_TOKENS))


# ---------------------------------------------------------------------------------------------
# Predicates. Shared with `tests/test_open_item_index.py` — one implementation, deliberately.


def _resolve(rel: str, root: Path, *, allow_absolute: bool) -> tuple[Path | None, str]:
    if rel.startswith("/"):
        if not allow_absolute:
            return None, (f"absolute path {rel!r} — a pin inside the repo must cite a "
                          "repo-relative path, or the suite stops passing on a bare checkout")
        return Path(rel), ""
    return (root / rel).resolve(), ""


def prose_spans(path: Path, source: str) -> set:
    """Character offsets of `source` that sit inside a Python COMMENT or STRING token.

    Empty for a non-Python path and for source that will not tokenize — both mean "we cannot tell",
    and the caller must then treat every occurrence as code. Failing the other way would let a
    tokenizer hiccup silently condemn a live pin.
    """
    if path.suffix != ".py":
        return set()
    offsets, pos = [0], 0
    for line in source.splitlines(keepends=True):
        pos += len(line)
        offsets.append(pos)
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return set()
    out = set()
    for tok in toks:
        if tok.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (r1, c1), (r2, c2) = tok.start, tok.end
        if r1 - 1 >= len(offsets) or r2 - 1 >= len(offsets):
            continue
        out.update(range(offsets[r1 - 1] + c1, offsets[r2 - 1] + c2))
    return out


def satisfied_only_by_prose(path: Path, source: str, literal: str) -> bool:
    """Does EVERY occurrence of `literal` lie wholly inside a comment or string?

    THE RULE THIS MAKES ENFORCEABLE. "An `absent:` literal must be one that prose cannot produce"
    was written as a comment beside a single marker, which is to say the guard against
    comment-satisfiable proofs was itself a comment. Both directions have a cost and they are
    different costs: an `absent:` literal a comment can produce goes GREEN the day someone writes the
    word in prose — a false shipped; a `present:` literal only prose carries can never go green at
    all — a marker stuck open, which is noise that teaches readers to ignore the index.

    ONE CHARACTER OF REAL CODE ANYWHERE ANCHORS IT. A literal spanning code and a string
    (`startswith("setup`) is about the call, not about the sentence, and flagging it would refuse
    exactly the pins that name a branch by the constant it tests — measured while writing this: the
    naive "does it survive with strings blanked" question flags 5 markers, of which only 3 are real.
    """
    mask = prose_spans(path, source)
    if not mask:
        return False
    i, seen = source.find(literal), False
    while i != -1:
        seen = True
        if any(j not in mask for j in range(i, i + len(literal))):
            return False
        i = source.find(literal, i + 1)
    return seen


def predicate_holds(pred: str, *, root: Path, allow_absolute: bool = False) -> tuple[bool, str]:
    """Evaluate one predicate against the real tree. Returns (holds, why-not).

    Four forms, the first three inherited verbatim from the open-item index:
      ``absent:<literal>@<path>``   — true while that text is NOT there
      ``present:<literal>@<path>``  — true while that text IS there
      ``missing:<path>``            — true while that path does not exist
      ``line:<a>&&<b>@<path>``      — true while ONE line carries both literals

    `line:` is the one addition and it exists for a measured reason. The e5 goal's fatal sentence
    ("the manual e5-small recipe is 8k x 2 GPUs") is satisfied by a bare `present:8k×2gpu@bench.md`
    — that string IS in the file, on `rubert-tiny-lite`'s row. A predicate that binds the two
    literals to the SAME line is what distinguishes "this string occurs" from "this string is said
    about that subject", and it is the difference between a pin that passes and a pin that would
    have stopped the run.
    """
    if pred.startswith("missing:"):
        rel = pred[len("missing:"):]
        target, why = _resolve(rel, root, allow_absolute=allow_absolute)
        if target is None:
            return False, why
        if target.exists():
            if rel.startswith(AUDIT_DIR):
                return False, (
                    f"{rel} now EXISTS — but an audit doc is its protocol before it is its result, "
                    "so this is not the item shipping: re-point the proof at "
                    f"`absent:{AUDIT_RESULT_LITERAL}@{rel}` (open until a dated RESULT line lands) "
                    "and delete the marker only once one has")
            return False, f"{rel} now EXISTS — what this claim says is absent is no longer absent"
        return True, ""

    for kind in ("absent:", "present:", "line:"):
        if not pred.startswith(kind):
            continue
        body = pred[len(kind):]
        if "@" not in body:
            return False, f"malformed predicate {pred!r} — expected <literal>@<path>"
        literal, rel = body.rsplit("@", 1)
        target, why = _resolve(rel, root, allow_absolute=allow_absolute)
        if target is None:
            return False, why
        if not target.exists():
            # A dead citation is this repo's most-measured form of rot (BACKLOG §0.3: 8 of 8 dead).
            # A pin that can point at nothing is the unverified sentence it was meant to replace.
            return False, f"{rel} does not exist — re-point this claim at where its subject now lives"
        if target.is_dir():
            # Relative to the CITED directory, never the absolute path: this repo's agent worktrees
            # live under `.claude/worktrees/`, which is in `_SKIP_DIRS`, so filtering the absolute
            # path skipped every file in a worktree and made a predicate hold vacuously.
            files = [p for p in target.rglob("*")
                     if p.is_file() and p.suffix in _TEXT_SUFFIXES
                     and not any(part in _SKIP_DIRS for part in p.relative_to(target).parts)]
        else:
            files = [target]

        if kind == "line:":
            if "&&" not in literal:
                return False, (f"malformed predicate {pred!r} — `line:` needs two literals joined "
                               "by `&&`; use `present:` for a single one")
            wanted = [part for part in literal.split("&&") if part]
            for p in files:
                for line in text_without_markers(p).splitlines():
                    if all(w in line for w in wanted):
                        return True, ""
            return False, (f"no single line of {rel} carries all of {wanted!r} — this claim says "
                           "they belong together, and in that file they do not")

        found = any(literal in text_without_markers(p) for p in files)
        if kind == "absent:" and found:
            return False, (f"{literal!r} is now PRESENT in {rel} — this claim rests on its being "
                           "absent, so either the claim is stale or it was never true")
        if kind == "present:" and not found:
            return False, (f"{literal!r} is GONE from {rel} — this claim rests on its being there, "
                           "so either the claim is stale or its subject moved")
        return True, ""

    return False, f"unknown predicate kind in {pred!r} (use absent:/present:/missing:/line:)"


# ---------------------------------------------------------------------------------------------
# Half 2: the opt-in pins.


def decided_predicates(match: "re.Match") -> str:
    """The predicate text of a `decided:` match, quoted or bare."""
    return match.group(1) or match.group(2) or ""


def iter_claims(text: str):
    """Yield (slug, window) for every `CLAIM[…]` in `text`."""
    for m in CLAIM_MARKER.finditer(text):
        yield m.group(1), text[m.end():m.end() + WINDOW]


def check_text(text: str, label: str, *, root: Path, allow_absolute: bool) -> list[str]:
    """Every defect in the pins carried by one blob of text. Empty list == every pin holds."""
    out: list[str] = []
    for slug, window in iter_claims(text):
        decided = DECIDED.search(window)
        if not decided:
            # THE CITATION CASE, NAMED. Five reds in this family came from the same move: prose
            # or a comment REFERRING to an existing claim wrote the slug in bracket form, and the
            # scanner read the reference as a second declaration. The convention already answers
            # it -- one slug names exactly one claim, so a reference drops the brackets -- but the
            # message did not say so, and "carries no `decided:` clause" reads like an instruction
            # to add one, which is the wrong repair and creates a duplicate declaration instead.
            out.append(f"{label}: CLAIM[{slug}] carries no `decided:` clause within {WINDOW} "
                       "chars — an unpinned claim is the sentence this convention replaces. "
                       f"If you meant to CITE the existing claim, drop the brackets and write "
                       f"`{slug}`: one slug names exactly one declaration site.")
            continue
        for pred in decided_predicates(decided).split("+"):
            holds, why = predicate_holds(pred, root=root, allow_absolute=allow_absolute)
            if not holds:
                out.append(f"{label}: CLAIM[{slug}] is FALSE — {why}")
    return out


def check_tree(root: Path) -> list[str]:
    """Every `CLAIM[…]` in the repo, re-derived. Repo-relative predicates only."""
    out: list[str] = []
    for path in tracked_text_files(root):
        text = read_text(path)
        if "CLAIM[" not in text:
            continue
        out.extend(check_text(text, str(path.relative_to(root)), root=root, allow_absolute=False))
    return out


def check_task_goal(task_path: Path, *, root: Path) -> list[str]:
    """The out-of-repo carrier: the pins in a task JSON's goal text.

    This is the surface a pytest can never reach — the file lives outside the repo and cites the
    operator's own machine — and it is the surface with the measured cost, so it gets a carrier of
    its own rather than an honourable mention.
    """
    try:
        blob = json.loads(read_text(task_path))
    except Exception as exc:                                  # noqa: BLE001 — report, never raise
        return [f"{task_path}: not readable as JSON ({exc})"]
    task = blob.get("task", blob) if isinstance(blob, dict) else {}
    goal = str((task or {}).get("goal", "") or "")
    if not goal:
        return [f"{task_path}: no `task.goal` to check"]
    return check_text(goal, f"{task_path} (task.goal)", root=root, allow_absolute=True)


# ---------------------------------------------------------------------------------------------
# Half 1: the derived citation check. No adoption cost — it reads what is already written.


def _citation_candidates(rel: str, root: Path, citing: Path) -> list[Path]:
    """Where `rel` could mean, in the spellings this tree actually uses.

    All four are live: `looplab/engine/evaluate.py` (full), `engine/evaluate.py` (package-relative),
    `routers/misc.py` (relative to the citing module's own directory) and a bare `orchestrator.py`.
    """
    seen: list[Path] = []
    for base in (root, root / "looplab", citing.parent, citing.parent.parent):
        cand = base / rel
        if cand.is_file() and cand not in seen:
            seen.append(cand)
    if seen or "/" in rel:
        return seen
    # A bare filename: unique-match only, so an ambiguous one is reported rather than guessed.
    hits = [p for base in (root / "looplab", root / "tests", root / "ui")
            if base.is_dir() for p in base.rglob(rel)]
    return hits


def _wrapped_at_eol(text: str, end: int) -> bool:
    """Did this citation run off the end of its line?

    At this repo's ~100 columns a long citation wraps mid-identifier
    (`…repair_verify(dot)py::declared_param_` / `overrides` is ONE citation), and reporting that would
    be the guard crying wolf about the house style. Rejoining the lines was tried and rejected: it
    glues a citation to whatever word follows and MANUFACTURES defects (`cli(dot)py::_engineused`).
    A truncated symbol is checked as a PREFIX instead, which can only ever acquit. An explicit
    trailing `*` (`…test_engine_options.py::test_the_salvage_policy_reaches_the_engine_*`, a real
    citation naming a FAMILY of tests) is read the same way.
    """
    return end >= len(text) or text[end] == "\n"


def _identifier_present(body: str, name: str, *, prefix: bool = False) -> bool:
    tail = "" if prefix else r"\b"
    return re.search(r"\b" + re.escape(name) + tail, body) is not None


# ---------------------------------------------------------------------------------------------
# Half 1b: `§` section citations, resolved against the doc they point into (review 2026-09-22,
# TST-07).
#
# WHY, measured on 2026-09-23 over `SECTION_SURFACES`. `§` is the tree's most-cited kind of target:
# 2,291 citations in 510 files, 1,187 of them a bare integer with no doc named — 1,181 of those name
# a section of `docs/56-where-the-budget-goes-2026-08-28.md`, the 1.2 MB campaign notebook whose 435
# numbered sections are that convention's namespace. Nothing resolved one, so nothing noticed a
# citation that points nowhere: `engine/evaluate.py` cited a section 331 doc 56 never had (its 330
# and 332 were written on 2026-09-07/08; the citation arrived with a merge on 2026-09-09), and 70
# more citations resolved to no section of the doc they named or meant.
#
# WHAT IS ADDRESSABLE in a doc (`section_keys`): the number an ATX heading OPENS with (`## 117.`,
# `## §330 — …`, `## F4 · …`, `### 3a. …`, `### 33.1 …`, `##### 21.20.13 …`, but not
# `### §114 is weaker…`, which opens with a citation of its own), a bold sub-label at a line start
# (`**21.1 — …**`, `**P4.2 [MED-HIGH] …**`: doc 56 §21 and doc 15 §P4 number their parts that way),
# and a bold-led top-level list item (`15. **Drift detection…**`: `docs/BACKLOG.md` §0.1 numbers the
# rows that 23 citations call "BACKLOG §15" that way). Fenced code is not read.
#
# WHICH DOC a citation means (`iter_section_citations`):
#   * NAMED — `doc 56`, `Doc 56's`, `docs/56`, `docs/56-<slug>.md`, `(../56-<slug>.md)`,
#     `docs/<file>.md`, an upper-case docs/ stem (`BACKLOG`, `BACKLOG.md`) immediately before the
#     sign, with only quotes, brackets, punctuation or ONE item id between (`doc 25 XP-01/TO-09 §6.6`);
#     `§X of <doc>` after it; or chained to a named one by a separator (`§84/§277`, `§21.7/§21.10`,
#     `§187 and §195 of docs/56`). A named citation must resolve in THAT doc. `arch-review` is the
#     one alias: the 2026-07-11 review (`docs/16-…`) is cited by that label 78 times and never by
#     its number.
#   * BARE — every other citation must name a section of one of `BARE_SECTION_DOCS`: doc 56, the
#     notebook (1,181 of the 1,187 bare integers resolve there), or doc 17, whose PART IV/V
#     numbering (`§21.20.13`, `§22.4`, `§6.3`) is the tree's second bare convention (445 of the 517
#     bare DOTTED citations resolve there, 35 only in doc 56 — the notebook's own `§419.1`-style
#     subsections). When both carry a key the integer is read as doc 56's and the dotted key as
#     doc 17's; that decides only which doc a citation is ATTRIBUTED to, never whether it resolves.
#
# WHAT THIS CANNOT SEE, and it is most of what a bare citation can get wrong: doc 56 numbers §1..§436
# densely, so ANY bare integer in that range resolves. `arch-review §3` is now read against doc 16,
# but 70 of the 1,187 bare integers sit beside ANOTHER doc's name on their line or the one above —
# `Signal-delivery (§1)` (doc 14's §1) 22 times, `PART V §22` 16, `PART IV … §12` (doc 17's
# verifier) 14 — and all but one of them land on a doc-56 section and are reported as nothing.
# This proves a citation points SOMEWHERE; naming its doc is what makes it point at the right place,
# so a new citation should name it.
SECTION_SURFACES: tuple[str, ...] = ("looplab", "tests", "benchmarks", "CLAUDE.md", "docs/guide")
_SECTION_SUFFIXES = frozenset({".py", ".md", ".sh", ".txt", ".json"})
# Data, not prose: recorded fixtures quote OTHER text's signs, and the backlog below is a list of
# `§` keys that would otherwise be read as citations of the very sections they record as missing.
_SECTION_DATA_DIRS = ("tests/data/", "tests/fixtures/")
BARE_SECTION_DOCS: tuple[str, ...] = ("56", "17")
SECTION_DOC_ALIASES: dict[str, str] = {"arch-review": "16"}
# The shrink-only backlog of the pre-existing citations this could not correct with confidence, one
# `SectionCitation.backlog_key` per line (`tests/test_claim_pins.py` refuses a row that names no
# live defect). Read HERE and not only by the test because `python -m looplab.core.claimpin` is an
# operator's pre-flight (NEXT_RUN.md: "must report 0 claim defects"), and a backlog that made it
# red on a clean tree would teach that operator to ignore it.
SECTION_BACKLOG = "tests/data/section_citations_unresolved.txt"

_SECTION_KEY = r"\d+(?:\.\d+)*[a-z]?|[A-Z]{1,3}\d+(?:[.-]\d+)*[a-z]?"
# `§§28` is doc 18's own spelling for "sections 28 and…"; the first key is the one that can be read.
SECTION_CITATION = re.compile(rf"§§?\s?({_SECTION_KEY})(?!\w)")
_SECTION_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_SECTION_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$")
_SECTION_KEY_AT = re.compile(rf"^(?:§\s?)?({_SECTION_KEY})(.*)$")
_SECTION_BOLD_AT = re.compile(rf"^\*\*(?:§\s?)?({_SECTION_KEY})(.*)$")
_SECTION_ITEM_AT = re.compile(r"^(\d+)\.\s+\*\*")
# What may sit between a named doc and its sign, and between two chained signs.
_SECTION_ITEM_ID = r"(?:\s+[A-Z]{1,4}-?\d+[a-z]?(?:/[A-Z]{1,4}-?\d+[a-z]?)*)?"
_SECTION_GLUE = r"(?:(?:'s|’s)(?:\s+own)?)?[`\"'’)\]*]*[\s,:;(\[]*"
_SECTION_CHAIN = re.compile(r"^(?:[\s/,&+–—-]|\band\b|\bor\b|#|\*)*$")


def _heading_section_key(text: str) -> str | None:
    """The key an ATX heading's text OPENS with, or None when it opens with anything else."""
    m = _SECTION_KEY_AT.match(text)
    if not m:
        return None
    key, rest = m.group(1), m.group(2)
    if rest[:1] in ("", ".", ":", ")") or re.match(r"\s*[—–·-](?:\s|$)", rest):
        return key
    # `### 33.1 The syntax gate`, BACKLOG's `### 0.6b The metric…` (spelled there with the sign): a
    # dotted or lettered key is a section even with no separator. A PLAIN integer followed by a word
    # is a heading that opens with a CITATION (`### §114 is weaker than four points made it look`),
    # which is not section 114.
    return key if rest[:1].isspace() and not key.isdigit() else None


def _bold_section_key(line: str) -> str | None:
    """`**21.1 — …**` / `**P4.2 [MED-HIGH] …**` — never `**338.26**`, which is a bold NUMBER."""
    m = _SECTION_BOLD_AT.match(line)
    if not m or m.group(1).isdigit():
        return None
    return m.group(1) if re.match(r"\s+[—–-]\s|\s+\[|[.:]\s|\s+[A-Z]", m.group(2)) else None


@functools.lru_cache(maxsize=256)
def _section_keys_cached(path: str, mtime_ns: int, size: int) -> frozenset[str]:
    keys: set[str] = set()
    fence = ""
    for line in read_text(Path(path)).splitlines():
        f = _SECTION_FENCE.match(line)
        if f:
            if not fence:
                fence = f.group(1)[0]
            elif f.group(1)[0] == fence:
                fence = ""
            continue
        if fence:
            continue
        h = _SECTION_HEADING.match(line)
        if h:
            key = _heading_section_key(h.group(1))
        else:
            item = _SECTION_ITEM_AT.match(line)
            key = _bold_section_key(line) or (item.group(1) if item else None)
        if key:
            keys.add(key)
    return frozenset(keys)


def section_keys(path: Path) -> frozenset[str]:
    """Every section key `path` makes addressable (see the block comment above for the grammar)."""
    st = path.stat()
    return _section_keys_cached(str(path), st.st_mtime_ns, st.st_size)


def _section_doc_files(root: Path, doc: str) -> list[Path]:
    """The file(s) a doc id names: `56` -> `docs/56-*.md` (doc 18 is TWO files, so their union),
    `BACKLOG` -> `docs/BACKLOG.md`, `audit/proxy-accuracy` -> `docs/audit/proxy-accuracy.md`."""
    docs = root / "docs"
    if doc.isdigit():
        return sorted(docs.glob(f"{int(doc):02d}-*.md"))
    path = docs / f"{doc}.md"
    return [path] if path.is_file() else []


def _section_doc_label(doc: str) -> str:
    return f"doc {doc}" if doc.isdigit() else (doc if doc.isupper() else f"docs/{doc}.md")


@functools.lru_cache(maxsize=8)
def _section_doc_name(docs_dir: str) -> str:
    """The alternation that NAMES a doc, with this tree's own upper-case docs/ stems spelled out —
    a generic `[A-Z]+` would read `INCREMENTAL (§21.16` as a doc called INCREMENTAL."""
    stems = sorted((p.stem for p in Path(docs_dir).glob("*.md") if p.stem.isupper()),
                   key=len, reverse=True)
    upper = "|".join(re.escape(s) for s in stems) or r"(?!)"
    aliases = "|".join(re.escape(a) for a in SECTION_DOC_ALIASES)
    return (r"(?:(?i:\bdocs?)(?:\s|/)(?P<num>\d{1,2})(?![\d.])(?:-[\w.-]*?\.md)?"
            r"|(?:\.\./|docs/)(?P<link>\d{2})-[\w.-]+?\.md"
            r"|docs/(?P<path>[\w./-]+?)\.md"
            rf"|\b(?P<stem>{upper})\b(?:\.md)?"
            rf"|(?P<alias>{aliases}))")


def _doc_from(m: "re.Match") -> str:
    if m.group("num") or m.group("link"):
        return str(int(m.group("num") or m.group("link")))
    if m.group("path"):
        return m.group("path")
    if m.group("stem"):
        return m.group("stem")
    return SECTION_DOC_ALIASES[m.group("alias")]


def _section_left_context(text: str, start: int) -> str:
    """The text a sign's doc name can sit in: its own line so far, and — when the sign opens a
    wrapped comment or docstring line — the tail of the line above."""
    line_start = text.rfind("\n", 0, start) + 1
    left = text[line_start:start]
    if re.fullmatch(r"\s*(?:#|//|\*|\"\"\"|''')?\s*", left) and line_start:
        # The continuation's own comment marker is not glue: a doc named at the END of one comment
        # line and cited at the START of the next is one citation, wrapped at ~100 columns.
        prev_start = text.rfind("\n", 0, line_start - 1) + 1
        left = text[prev_start:line_start - 1] + " "
    return left[-160:]


class SectionCitation(NamedTuple):
    """One `§` citation, resolved. `doc` is the doc it NAMES (None when bare); `resolved` is the doc
    whose section it names (None when it names none — the defect)."""
    rel: str
    line: int
    key: str
    doc: str | None
    resolved: str | None

    @property
    def backlog_key(self) -> str:
        """Stable across edits above it: the file, the doc it names, and the key — no line number."""
        return f"{self.rel}::{_section_doc_label(self.doc) + ' ' if self.doc else ''}§{self.key}"

    def message(self, root: Path) -> str:
        where = f"{self.rel}:{self.line}"
        if self.doc is None:
            bare = " or ".join(f"doc {d}" for d in BARE_SECTION_DOCS)
            return (f"{where}: `§{self.key}` names no section of {bare}, the docs a BARE § "
                    "resolves in — name the doc it means (`doc NN §…`) or correct the key")
        label = _section_doc_label(self.doc)
        files = _section_doc_files(root, self.doc)
        if not files:
            return f"{where}: `{label} §{self.key}` — there is no {label} under docs/"
        return (f"{where}: `{label} §{self.key}` — {files[0].relative_to(root).as_posix()} has no "
                f"section {self.key} (a heading, a `**N.M —` sub-label or an `N. **…**` item)")


def _section_files(root: Path, surfaces: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for surface in surfaces:
        base = root / surface
        if base.is_file():
            out.append(base)
            continue
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            if f.suffix not in _SECTION_SUFFIXES or f.is_symlink() or not f.is_file():
                continue
            rel = f.relative_to(root)
            if (any(part in _SKIP_DIRS for part in rel.parts)
                    or rel.as_posix().startswith(_SECTION_DATA_DIRS)):
                continue
            out.append(f)
    return out


def iter_section_citations(root: Path, surfaces: tuple[str, ...] = SECTION_SURFACES):
    """Every `§` citation in `surfaces`, with the doc it names and the doc it resolves in.

    Empty when the tree has no `docs/` at all: with nothing to resolve against, every citation would
    read as dangling, and "we cannot tell" must not condemn (the rule `prose_spans` follows too).
    """
    docs_dir = root / "docs"
    if not docs_dir.is_dir():
        return
    name = _section_doc_name(str(docs_dir))
    before = re.compile(rf"{name}{_SECTION_ITEM_ID}{_SECTION_GLUE}$")
    # No `^`: `Pattern.match(text, pos)` anchors at `pos` already, and a `^` there matches only at
    # the real start of the string, which made this lookahead silently never fire.
    after = re.compile(rf"(?:(?:[\s/,&+–—-]|\band\b|\bor\b)*§§?\s?(?:{_SECTION_KEY}))*"
                       rf"(?:'s|’s)?\s+(?:of|in)\s+`?{name}")
    keys_of: dict[str, frozenset[str]] = {}

    def keys(doc: str) -> frozenset[str]:
        if doc not in keys_of:
            keys_of[doc] = frozenset().union(*(section_keys(p)
                                               for p in _section_doc_files(root, doc)))
        return keys_of[doc]

    for f in _section_files(root, surfaces):
        text = read_text(f)
        if "§" not in text:
            continue
        rel = f.relative_to(root).as_posix()
        prev_end, prev_doc = -1, None
        for m in SECTION_CITATION.finditer(text):
            key = m.group(1)
            named = before.search(_section_left_context(text, m.start()))
            doc = _doc_from(named) if named else None
            if doc is None:
                later = after.match(text, m.end(), m.end() + 240)
                doc = _doc_from(later) if later else None
            if doc is None and prev_doc is not None and _SECTION_CHAIN.match(
                    text[prev_end:m.start()]):
                doc = prev_doc
            if doc is not None:
                resolved = doc if key in keys(doc) else None
            else:
                # Both docs are tried; the ORDER only decides the attribution when both have the
                # key. An integer is the notebook's; a dotted key is PART IV/V's — the 182 bare
                # dotted citations whose key both docs carry (`§21.12`, `§21.7`: doc 56 §21's
                # `**21.N —**` sub-labels vs doc 17's `#### 21.N` headings) all sit in the concept
                # and trust code, and none in benchmarks/, where the notebook is cited.
                order = (BARE_SECTION_DOCS if re.fullmatch(r"\d+[a-z]?", key)
                         else BARE_SECTION_DOCS[::-1])
                resolved = next((d for d in order if key in keys(d)), None)
            yield SectionCitation(rel, text.count("\n", 0, m.start()) + 1, key, doc, resolved)
            prev_end, prev_doc = m.end(), doc


def section_citation_defects(root: Path, surfaces: tuple[str, ...] = SECTION_SURFACES
                             ) -> list[SectionCitation]:
    """Every `§` citation in `surfaces` that names no section of the doc it resolves against —
    backlog rows included; `citation_defects` is the reader that subtracts them."""
    return [c for c in iter_section_citations(root, surfaces) if c.resolved is None]


def section_backlog(root: Path) -> list[str]:
    """The rows of `SECTION_BACKLOG`, in file order; empty when the tree does not carry the file.
    A `#` line is the reason for the rows under it, never a row."""
    path = root / SECTION_BACKLOG
    if not path.is_file():
        return []
    return [ln.strip() for ln in read_text(path).splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def citation_defects(root: Path, subtrees: tuple[str, ...] = ("looplab",), *,
                     section_surfaces: tuple[str, ...] = SECTION_SURFACES) -> list[str]:
    """Re-derive every ``<mod>(dot)py::<symbol>`` citation in `subtrees` against the real tree.

    Two defects, both of which this repo has live: a path that resolves to nothing, and a symbol
    that no longer appears in the file it is cited from (a rename, or a move to a sibling module —
    `events/replay(dot)py::_card_debug_leaf_children` was cited twice while the function lived in
    `events/card_ledger.py`, which a THIRD site spelled correctly).

    A symbol is "present" if every dotted component appears as a word in the target. That is
    deliberately loose: it catches the rot (deletions and renames) without a second, drifting model
    of Python scoping, and a loose check people keep is worth more than a strict one they disable.

    A third defect has no symbol to check: a bare `tests/<name>(dot)py` (`TEST_PATH_CITATION`) that
    names no file — the "pinned by" citation, whose whole promise is that the file exists.

    A fourth reads `section_surfaces`, not `subtrees`, because a `§` citation lives in tests, bench
    scripts and guide pages as much as in the package: a `§` citation that names no section of the
    doc it resolves against (`iter_section_citations`), minus the rows of `SECTION_BACKLOG`.
    """
    out: list[str] = []
    known = set(section_backlog(root))
    out.extend(c.message(root) for c in section_citation_defects(root, section_surfaces)
               if c.backlog_key not in known)
    cache: dict[Path, str] = {}
    for sub in subtrees:
        base = root / sub
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*.py")):
            if any(part in _SKIP_DIRS for part in f.relative_to(root).parts):
                continue
            text = read_text(f)
            # POSIX on every OS: this is a repo path in a message people grep and tests compare,
            # and a Windows `Path` printed `looplab\dead.py` (CI tests-windows run 29).
            where = f.relative_to(root).as_posix()
            # A citation split across two comment lines is not a defect, it is a line WRAP at this
            # repo's ~100 columns — `…repair_verify(dot)py::declared_param_` / `overrides` is one
            # citation, and reporting it would be the guard crying wolf about its own house style.
            # Rejoin ONLY a line that already carries a `.py::` and ends mid-identifier, so no other
            # line is glued and no citation can be manufactured by the join.
            joined = text
            for m in LINE_CITATION.finditer(text):
                out.append(f"{where}: `{m.group(1)}:{m.group(2)}` cites a LINE NUMBER — any edit "
                           "above it silently re-points the citation. Locate by SYMBOL "
                           f"(`{m.group(1)}::<name>`) instead.")
            for m in TEST_PATH_CITATION.finditer(text):
                if not (root / m.group(1)).is_file():
                    out.append(f"{where}: `{m.group(1)}` — no such test file (renamed, merged into "
                               "another, or never written); cite the test that holds the claim, "
                               "as `tests/<file>.py::<test>` where one test does")
            for m in CITATION.finditer(joined):
                rel, sym = m.group(1), m.group(2)
                cands = _citation_candidates(rel, root, f)
                if not cands:
                    out.append(f"{where}: `{rel}::{sym}` — no file at {rel}")
                    continue
                if len(cands) > 1:
                    out.append(f"{where}: `{rel}::{sym}` is ambiguous — {len(cands)} files match")
                    continue
                target = cands[0]
                body = cache.setdefault(target, read_text(target))
                parts = [c for c in sym.split(".") if c]
                # A symbol cut by a line wrap is matched as a prefix; every other part exactly.
                truncated = (_wrapped_at_eol(joined, m.end())
                             or joined[m.end():m.end() + 1] == "*")
                missing = [c for i, c in enumerate(parts)
                           if not _identifier_present(
                               body, c, prefix=truncated and i == len(parts) - 1)]
                if missing:
                    out.append(f"{where}: `{rel}::{sym}` — {', '.join(missing)} is not in "
                               f"{target.relative_to(root)} (renamed, deleted, or in a sibling "
                               "module)")
    return out


def count_claims(text: str) -> int:
    """How many `CLAIM[…]` pins one blob of text carries, defects or not."""
    return sum(1 for _ in iter_claims(text))


def goal_claim_count(task_path: Path) -> int:
    """Pins in a task JSON's goal — `-1` when the file or its goal cannot be read at all."""
    try:
        blob = json.loads(read_text(task_path))
    except Exception:                                         # noqa: BLE001 — report, never raise
        return -1
    task = blob.get("task", blob) if isinstance(blob, dict) else {}
    return count_claims(str((task or {}).get("goal", "") or ""))


def _main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[2]
    problems = check_tree(root) + citation_defects(root)
    # A DENOMINATOR, because "0 defects" over ZERO pins reads exactly like "every claim checks out"
    # and means the opposite. Found 2026-08-20 the first time this was pointed at the live e5 task:
    # it answered "0 claim defect(s)" about a goal carrying no pins at all, i.e. about the very
    # sentences whose falsity cost three nodes. A checker that cannot say "I checked nothing" is the
    # vacuous green this convention exists to abolish — the same defect the open-item index had when
    # a marker inherited its neighbour's falsifier.
    surfaces = [(f"repo tree ({root})", sum(count_claims(read_text(p)) for p in tracked_text_files(root)))]
    for arg in argv:
        problems.extend(check_task_goal(Path(arg), root=root))
        surfaces.append((f"{arg} (task.goal)", goal_claim_count(Path(arg))))
    for line in problems:
        print(line)
    print()
    for label, n in surfaces:
        if n < 0:
            print(f"  UNREADABLE — no pins could be counted: {label}")
        elif n == 0:
            print(f"  NO PINS AT ALL — nothing was checked here: {label}")
        else:
            print(f"  {n} pin(s) evaluated: {label}")
    print(f"\n{len(problems)} claim defect(s) over {sum(max(0, n) for _, n in surfaces)} pin(s).")
    return 1 if problems else 0


if __name__ == "__main__":                                    # pragma: no cover - manual entry
    raise SystemExit(_main(sys.argv[1:]))
