"""Repair VERIFICATION — did the repair DO what its rationale said it would do?

THE DEFECT, AND THE MEASUREMENT THAT SIZED IT. A repair's stated rationale was never compared
against what the repair actually changed. `engine/evaluate.py` has known this since the change-set
column was added — the comment beside `repair_log.append` says it out loud ("the developer's own
rationale says what it INTENDED to change, this says what it did") — but nothing ever put the two
side by side, so a fix that claimed "cutting epochs 10 -> 5" and did not touch the epoch count was
indistinguishable from one that worked.

Measured 2026-08-13 over every `node_repaired` row in the shipped corpus (`runs/`
rubertlite-dr-unified-v2 / v4 / v6, rubert-dr-0807, rubert-dr-0804, rubertlite-dense-retrieval —
2,477 repairs; v5's log no longer exists):

  * 2,343 carry the engine's own `fallback: attempt repair` boilerplate — the rubert-dr-0804
    dead-provider incident. No model claim to check; that condition already has its own rung
    (`triage.repair_artifact_defect` / `_repair_provider_failure`).
  *   134 carry a model-authored rationale. 126 of those name at least one concrete, checkable
    thing (a file, a flag, a parameter, a literal).
  *    38 of those 126 (hand-audited to 31 genuine, 7 extractor false positives) named a concrete
    change that appears NOWHERE in the diff the repair produced. ~25 % of substantively-explained
    repairs.
  *    13 of them changed NOTHING AT ALL — `changed: []` committed as a repair, then a full
    re-evaluation of byte-identical inputs. rubertlite-dr-unified-v4 node 6 spent two of these in a
    row on a 2.7-hour train; rubertlite-dense-retrieval node 57 spent three consecutive attempts in
    which its developer made thirteen `read_file` calls and not one write.

AND THE OTHER HALF OF THE MEASUREMENT, WHICH IS THE REASON THIS MODULE IS SHAPED THE WAY IT IS: the
engine did not drop, truncate or mis-apply a single edit. Every `edit_file`/`write_file` call in
those runs' `spans.jsonl` that reported success — 198 of them across the traced repairs — is present
in the `node_repaired.files` the engine then committed. The five apparent misses all explain
themselves: four were superseded by a later write or delete inside the SAME repair, and the fifth
wrote a file byte-identical to the one already there. So this is not an application bug to fix; it
is a missing rung. The engine had no way to notice.

--------------------------------------------------------------------------------------------------
DETERMINISTIC ONLY, AND THE ANCHOR IS THE BYTES — the same rule `engine/metric_salvage.py` states
for its own tier and for the same reason. The agent writes the repair, which means the agent writes
the very text an extractor would read; a model asked to grade a rationale against a diff is a model
grading its own text, and it would be graded on a run where BOTH inputs came from it. So:

  * `REPAIR_INERT` is decided on FILE BYTES ONLY. The rationale is not consulted, cannot be
    consulted, and no wording can evade it: either the repair's change set is empty or it is not.
    That verdict is the only one this module lets the loop ACT on.
  * `REPAIR_UNMET` is decided by a pure token extractor over the rationale. It is EVIDENCE, never a
    stop: a model can dodge it by writing vaguely, and dodging it lands on `REPAIR_UNSTATED`, which
    is itself reported. It is rendered into the judge's repair history and stamped on the durable
    row, exactly the way `metric_salvage` makes a salvaged metric visible-but-not-selectable rather
    than silently trusted.

--------------------------------------------------------------------------------------------------
WHAT `unmet` IS PRECISE ABOUT, AND WHAT IT IS NOT. Two limits, different in kind — one is a caller's
obligation this module cannot enforce, the other is a ceiling of the approach.

THE INPUT MUST BE THE WHOLE RATIONALE, and that is the CALLER's to get right. `claimed_tokens` reads
the text it is handed and has no way to know it was handed a PREFIX, so a caller that truncates first
gets a confident verdict about a sentence the model did not finish. Not hypothetical:
`crash_repair._triage_crash` capped the rationale at 300 chars — the same number as the DURABLE sink
one layer down — and a crash rationale is written diagnosis-first ("diverged right after the R-Drop
KL term, unlike the working nll_cos runs (0.728)") and fix-second ("Fix: <the concrete things>"). The
cut therefore landed on that seam and delivered the CITATIONS while dropping the CLAIMS, which is
the only half this rung exists to check. Measured over `runs/` on 2026-08-14: 83 of the 123
model-authored rationales in the corpus are stored at exactly that cap — the MEDIAN rationale this
rung read was truncated — and over the 54 repairs whose full text could be recovered from
`spans.jsonl` and replayed, 3 of the 7 `unmet`s and 2 of the 3 `unstated`s are `verified` on the text
the model actually wrote. `engine/triage.py::TRIAGE_RATIONALE_CAP` is now that intake bound and is
deliberately far above every sink's; a sink capping its own column is fine and always was. IT LIVES
IN `triage.py` BECAUSE THE INTAKE WAS NOT THE FIRST CAP: `triage_crash` is a duck-typed seam with
exactly one implementation in the tree, `agents/unified_agent.py`, which is the shipped default and
whose emit finalizer already cut the same string to 300 on its way out — so raising the bound on what
the seam RETURNED changed nothing at all for a day. Both layers now read that single constant.

A CITED NAME AND A CLAIMED NAME ARE THE SAME TOKEN, and no extractor over model-authored text will
reliably tell them apart. "vs the working nll_cos runs" names a baseline being compared AGAINST; "I
removed the nll_cos path" names a change being promised — identical to this module, which sees one
token and one diff. That is a real residual: a rationale whose ONLY concrete token is a citation
scores `unmet` where the honest answer is `unstated`. It is deliberately NOT patched, on three
grounds. It is INHERENT — the discriminator would be a second regex over the same
adversarially-authorable text, and "vs" / "unlike" / "compared with" / "the documented X recipe" is a
phrasing list rather than a rule, so a model that writes around it is back where it started while an
honest one writing "unlike the old nll_cos path, which I deleted" has its real claim dropped. It is
CHEAP — `unmet` stops nothing (see the tiering above), so the cost is precision in a signal a model
reads, never a wrong stop. And on the evidence it is RARE: replayed over the corpus's full-text
rationales, ZERO of the four surviving `unmet` verdicts is a citation artifact, and the one case that
looked like one was the truncation above. Widening the extractor to chase a class the corpus does not
contain is the same trade `_NOT_A_CLAIM` refuses in the other direction. If it ever does need fixing,
fix it TO `unstated` — the "I could not check this" verdict already exists — and never by dropping
the token, which would make a real broken promise unverifiable.

WHY THESE ARE NOT TRIAGE VERDICTS. `engine/triage.py::TRIAGE_ACTIONS` answers "keep repairing this
node?" and is a JUDGEMENT, three-fifths of which a model may emit. These four answer "what did the
repair just do?" and are FACTS the engine derives from bytes it holds — no model may emit one, no
coercion accepts one off the wire, and `coerce_triage_action` must never see one. They are a
separate vocabulary for the same reason `metric_salvage.SALVAGE_CAUSE_TRIAGE_ACTION` is deliberately
absent from `TRIAGE_ACTIONS`: mixing a marker into a verdict enum breaks the emit schema in one
direction and makes a reader treat the enum as exhaustive in the other. The two tuples are
cross-referenced rather than merged; `tests/test_repair_verification.py` drives both rules.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

# --- The verdict vocabulary (a REGISTRY: CLAUDE.md) ---------------------------------------------
# The single spelling of what a repair did to the tree. Duck-typed across three sites — the
# `node_repaired.verified` column `engine/evaluate.py` writes, the judge-history row
# `_durable_repair_ledger` reads back off the log, and the renderer in
# `engine/crash_repair.py::_format_repair_log` — so a typo'd literal here would silently turn "this
# repair changed nothing" into "no opinion" and restore the unbounded inert chain this bounds.
#
#   "verified"  — the rationale named something concrete and the change set contains it.
#   "inert"     — THE CHANGE SET IS EMPTY. No file content moved, nothing was deleted, the
#                 whole-file artifact is byte-identical to the one it replaced. A fact about bytes;
#                 the rationale is not read. This is the only verdict the loop acts on.
#   "unmet"     — the change set is NOT empty, the rationale named concrete things, and none of them
#                 occur in what changed. Evidence for the judge, never a stop on its own.
#   "unstated"  — the rationale named nothing concrete enough to check. Reported rather than
#                 silently scored as a pass, because "I could not check this" and "this checked out"
#                 are different facts and collapsing them is how the rung would rot.
REPAIR_VERIFIED = "verified"
REPAIR_INERT = "inert"
REPAIR_UNMET = "unmet"
REPAIR_UNSTATED = "unstated"
REPAIR_VERDICTS = (REPAIR_VERIFIED, REPAIR_INERT, REPAIR_UNMET, REPAIR_UNSTATED)

# How many CONSECUTIVE inert repairs a node may make before the loop stops repairing it. NOT
# operator-settable, and deliberately smaller than `_UNPARSEABLE_REPAIR_LIMIT` (3), because the
# evidence is stronger: an unparseable answer might be one truncated generation, while an empty
# change set is the engine's own byte comparison saying the next eval re-runs inputs it has already
# run. One is allowed because a developer can genuinely spend a turn budget reading before it edits;
# a second in a row is a chain that cannot make progress, and every link costs a whole evaluation
# (rubertlite-dr-unified-v4 node 6: 2.7 h of GPU per link).
INERT_REPAIR_LIMIT = 2

# --- Extracting what a rationale CLAIMED ---------------------------------------------------------
# Concrete = something a diff could literally contain. Prose cannot be checked and must not be
# guessed at, so anything that does not look like code is dropped rather than scored.
_FILE_RE = re.compile(
    r"\b[\w][\w\-.]*(?:/[\w\-.]+)*\.(?:py|json|ya?ml|sh|txt|cfg|toml|ini|parquet|safetensors)\b")
_FLAG_RE = re.compile(r"--[A-Za-z][\w\-]{2,}")
_QUOTED_RE = re.compile(r"[`'\"]([^`'\"\n]{2,60})[`'\"]")
# An identifier is a token English does not produce: it carries an underscore, or mixes case
# mid-word, or is an ALLCAPS env-var spelling. A bare lowercase word is prose and is never a claim.
_IDENT_RE = re.compile(
    r"\b(?:[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+"
    r"|[a-z]+[A-Z][A-Za-z0-9]*"
    r"|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*"
    r"|[A-Z][A-Z0-9_]{3,})\b")

# Library, framework and venue names the identifier rule cannot tell from code. Every one of these
# produced a false `unmet` on the measured corpus — "the crash is a mechanical PyTorch in-place
# operation bug" names no change at all, and scoring it as an unmet claim would be the extractor
# inventing an accusation. When in doubt a token is dropped: an over-eager extractor turns this rung
# into noise the judge learns to ignore, which costs more than a missed mismatch.
_NOT_A_CLAIM = frozenset({
    "PyTorch", "TensorFlow", "SentenceTransformer", "SentenceTransformers", "HuggingFace",
    "RuBERT", "InfoNCE", "SigLIP", "CVPR", "NeurIPS", "ICLR", "NCCL", "CUDA", "PyPI", "LoopLab",
    "OpenRouter", "OpenAI", "JSON", "YAML", "None", "True", "False", "This", "The", "NaN",
})


@dataclass(frozen=True)
class RepairVerification:
    """What the engine can say about a repair without asking anybody.

    `verdict` is a member of `REPAIR_VERDICTS`; `claims` is every concrete token the rationale
    named; `unmet` is the subset the change set does not contain. `unmet` is non-empty only for
    `REPAIR_UNMET` — for `REPAIR_INERT` it is deliberately left EMPTY even though nothing was met,
    because that verdict is a statement about bytes and attaching a text-derived list to it would
    invite a reader to treat the two tiers as one.
    """
    verdict: str
    claims: tuple[str, ...] = ()
    unmet: tuple[str, ...] = ()

    @property
    def actionable(self) -> bool:
        """Does this verdict license the loop to STOP? Only the byte-anchored one does."""
        return self.verdict == REPAIR_INERT


def claimed_tokens(rationale) -> tuple[str, ...]:
    """The concrete things a rationale said it would make true, deduplicated and ordered.

    Pure and total — every input is an answer, never a raise, because it runs inside the attempt
    loop on model-authored text of arbitrary shape.

    HAND IT THE WHOLE RATIONALE. A prefix is indistinguishable from a short answer here, and a crash
    rationale puts its citations before its claims, so a truncated input reads as "named a baseline
    and changed something else" — see the module docstring's measurement and
    `engine/triage.py::TRIAGE_RATIONALE_CAP`, the intake bound BOTH the seam's finalizer and
    `crash_repair._ask_triage` read, which is what keeps this true.
    """
    text = rationale if isinstance(rationale, str) else ""
    if not text.strip():
        return ()
    out: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        tok = tok.strip()
        if len(tok) < 4 or tok in _NOT_A_CLAIM or tok in seen:
            return
        seen.add(tok)
        out.append(tok)

    for m in _FILE_RE.finditer(text):
        _add(m.group(0))
    for m in _FLAG_RE.finditer(text):
        _add(m.group(0))
    for m in _IDENT_RE.finditer(text):
        _add(m.group(0))
    for m in _QUOTED_RE.finditer(text):
        tok = m.group(1).strip()
        # A quoted PHRASE is prose in quotes ("changed: nothing"); a quoted token is a literal the
        # code can carry ('esc', "ddp_spawn"). Only the second is checkable.
        if " " not in tok and not tok.endswith((".", ",")):
            _add(tok)
    return tuple(out)


def changed_region(prev_files, repaired_files, prev_code, new_code, *, cap: int = 40000) -> str:
    """The TEXT of what this repair changed — every differing line of every changed file, with a
    little context, plus the whole-file artifact when that is what the repair shipped.

    Context matters and is not decoration: a stage manifest is JSON, so `"--gpus"` and its value sit
    on ADJACENT lines and a zero-context diff of a `--gpus 2 -> 1` change contains only `"1"`. Read
    with no context, seven real `--gpus` repairs in rubertlite-dr-unified-v4 scored as unmet claims.

    Bounded, because this runs per attempt on a repo the Developer may have rewritten wholesale and
    the result is only ever searched for short tokens. Over the cap it stops mid-diff, which can only
    make a claim look unmet — and `REPAIR_UNMET` never stops the loop, so the failure mode of the
    bound is a noisier prompt rather than a node killed on a truncation.
    """
    prev_files = prev_files or {}
    repaired_files = repaired_files or {}
    parts: list[str] = []
    size = 0
    pairs = [(k, prev_files.get(k, ""), repaired_files.get(k, ""))
             for k in sorted(set(prev_files) | set(repaired_files))]
    if (new_code or "") != (prev_code or ""):
        pairs.append(("<whole-file solution>", prev_code or "", new_code or ""))
    for name, old, new in pairs:
        if old == new:
            continue
        parts.append(name)
        size += len(name)
        for line in difflib.unified_diff(str(old).splitlines(), str(new).splitlines(),
                                         n=3, lineterm=""):
            parts.append(line)
            size += len(line)
            if size > cap:
                return "\n".join(parts)
    return "\n".join(parts)


def _claim_met(token: str, changed_paths, region: str) -> bool:
    """A file claim is met by the CHANGE SET (a path, not text); everything else by the region."""
    if _FILE_RE.fullmatch(token):
        base = token.rsplit("/", 1)[-1]
        for p in changed_paths or ():
            p = str(p)
            if p == token or p.endswith("/" + token) or p.rsplit("/", 1)[-1] == base:
                return True
        # A file the repair named but did not touch can still be met by the region: a stage manifest
        # that now RUNS `looplab_eval.py` mentions it without that file being in the change set.
    return token in (region or "")


def verify_repair(rationale, *, changed, deleted=(), code_changed: bool = False,
                  region: str = "") -> RepairVerification:
    """Compare what a repair SAID against what it DID. See the module docstring for the tiering.

    `changed`/`deleted` are `_repair_change_set`'s own deltas and `code_changed` is the whole-file
    artifact's — i.e. the three halves of "did anything move", all of them the engine's own byte
    comparisons. `region` is `changed_region(...)`. Nothing here calls a model, does I/O, or reads
    anything the agent could rewrite after the fact.
    """
    changed_paths = sorted({str(c) for c in (changed or ())} | {str(d) for d in (deleted or ())})
    if not changed_paths and not code_changed:
        # THE BYTES SAID NOTHING MOVED. Decided before the rationale is even looked at, so no
        # wording — vague, confident, or quoting the diff back at itself — can reach this verdict.
        return RepairVerification(REPAIR_INERT)
    claims = claimed_tokens(rationale)
    if not claims:
        return RepairVerification(REPAIR_UNSTATED)
    unmet = tuple(t for t in claims if not _claim_met(t, changed_paths, region))
    if len(unmet) < len(claims):
        return RepairVerification(REPAIR_VERIFIED, claims)
    return RepairVerification(REPAIR_UNMET, claims, unmet)


def inert_streak(repair_log) -> int:
    """How many of the MOST RECENT repairs in a row changed nothing.

    Trailing rather than total on purpose: a chain that made one inert attempt and then a real fix
    is working, and charging it forever would stop nodes that recovered. Reads the same row shape
    `_format_repair_log` renders and `_durable_repair_ledger` rebuilds, so the count survives a
    resume — a bound a resume refunds is not a bound (see `_durable_repair_ledger`).

    A row with NO `verified` key is not inert and BREAKS the streak: that is a row written before
    this column existed, or a `salvage_cause_fix` marker row, and reading either as "changed
    nothing" would terminalize a node on evidence nobody recorded.
    """
    n = 0
    for row in reversed([r for r in (repair_log or []) if isinstance(r, dict)]):
        if row.get("verified") != REPAIR_INERT:
            break
        n += 1
    return n
