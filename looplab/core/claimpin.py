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
   implementation, because `docs/BACKLOG.md` §0.8 found four implementations of one claim/verdict
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

import io
import json
import tokenize
import re
import sys
from pathlib import Path

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
_KINDS = "present:|absent:|missing:|line:"
DECIDED = re.compile(rf"decided:(?:`((?:{_KINDS})[^`]+)`|((?:{_KINDS})\S+))")

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

_SKIP_DIRS = {".git", ".claude", "runs", "node_modules", "dist", "site", "__pycache__",
              ".pytest_cache", ".mypy_cache", ".venv", "venv", "build"}
_TEXT_SUFFIXES = {".py", ".md", ".js", ".jsx", ".html", ".txt", ".toml", ".yml", ".yaml"}

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
            out.append(f"{label}: CLAIM[{slug}] carries no `decided:` clause within {WINDOW} "
                       "chars — an unpinned claim is the sentence this convention replaces")
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


def citation_defects(root: Path, subtrees: tuple[str, ...] = ("looplab",)) -> list[str]:
    """Re-derive every ``<mod>(dot)py::<symbol>`` citation in `subtrees` against the real tree.

    Two defects, both of which this repo has live: a path that resolves to nothing, and a symbol
    that no longer appears in the file it is cited from (a rename, or a move to a sibling module —
    `events/replay(dot)py::_card_debug_leaf_children` was cited twice while the function lived in
    `events/card_ledger.py`, which a THIRD site spelled correctly).

    A symbol is "present" if every dotted component appears as a word in the target. That is
    deliberately loose: it catches the rot (deletions and renames) without a second, drifting model
    of Python scoping, and a loose check people keep is worth more than a strict one they disable.
    """
    out: list[str] = []
    cache: dict[Path, str] = {}
    for sub in subtrees:
        base = root / sub
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*.py")):
            if any(part in _SKIP_DIRS for part in f.relative_to(root).parts):
                continue
            text = read_text(f)
            where = f.relative_to(root)
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
