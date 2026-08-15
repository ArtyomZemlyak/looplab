"""Cross-run memory (I19, ADR-10): an episodic case library over a VectorStore.
Cases are keyed by a task description embedding; `retain_if_improved` keeps a case
only when its metric beats the stored one (retain-on-improvement). This is the
top-system differentiator — solved tasks make later similar tasks easier.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Callable, Optional

from looplab.core.atomicio import atomic_write_text
from looplab.core.text import WORD_RE as _WORD_UNICODE
from looplab.core.models import NODE_CONCEPT_PROVENANCE_CLASSIFIER
from looplab.events.eventstore import (read_jsonl_lenient, read_jsonl_lenient_with_health,
                                       replace_jsonl_rows_atomic_preserving_quarantine)
from looplab.tools.vectorstore import Hit, Item, VectorStore, hash_embed

# Lesson HYGIENE moved to its own module (doc 25 EM-10); re-exported so both spellings name
# the SAME objects and every existing import / monkeypatch seam keeps working.
from looplab.engine.lesson_hygiene import (  # noqa: F401
    _NEGATIVE,
    _VERDICTS,
    _accumulated_evidence,
    _agentic_merge_lessons,
    _lesson_index_text,
    _verdict_base,
    consolidate_lessons,
    distilled_claim_stance,
    filter_contradicted,
    lesson_rank_key,
    normalize_statement,
    prompt_slot_key,
    retrieve_lessons_harmonic,
)

# Both moved DOWN to core (doc 25 EM-10): the concept-capsule split needs them below both
# modules, and neither may import the other. Re-exported so every existing spelling and
# monkeypatch seam keeps naming the SAME objects.
from looplab.core.text import fingerprint_similarity  # noqa: F401
from looplab.core.fitness import finite_or_absent_metric as _is_finite_metric  # noqa: F401

# Concept CAPSULES moved to their own module (doc 25 EM-10); re-exported so both spellings
# name the SAME objects and every import / monkeypatch seam keeps working.
from looplab.engine.concept_capsules import (  # noqa: F401
    CONCEPT_CAPSULE_VERSION,
    ConceptCapsuleStore,
    _CONCEPT_NEUTRAL_BAND_FRAC,
    _CapsuleRows,
    _EMPTY_CAPSULE_STORE_HEALTH,
    _LEGACY_CONCEPT_CAPSULE_VERSION,
    _MAX_CAPSULE_CONCEPTS,
    _MAX_CAPSULE_FINGERPRINT,
    _MAX_CAPSULE_ID_CHARS,
    _MAX_CAPSULE_OUTCOMES,
    _MAX_CAPSULE_SOURCE_ITEMS,
    _MAX_CAPSULE_TOKEN_CHARS,
    _MAX_OVERVIEW_CARD_CONCEPTS,
    _MAX_OVERVIEW_CONCEPTS,
    _MAX_OVERVIEW_RUNS_PER_CONCEPT,
    _MAX_OVERVIEW_RUN_CARDS,
    _capsule_completeness,
    _capsule_concept_evidence_completeness,
    _capsule_fingerprint_scope_complete,
    _capsule_rows,
    _capsule_source_summary,
    _concept_profit_signs,
    _dedup_valid_capsules,
    _filter_capsule_rows,
    _portfolio_concept_overview_data,
    _valid_capsule_record,
    build_concept_capsule,
    concept_profit_tendencies,
    portfolio_concept_overview,
)

_STOP = {"the", "a", "an", "to", "of", "and", "or", "for", "on", "in", "with", "from", "predict",
         "using", "use", "data", "dataset", "model", "target", "column", "columns", "features",
         "given", "this", "that", "is", "are", "by", "your", "my", "it", "as", "at", "be"}


# Goal-keyword tokenizers. LEGACY (default): ASCII `[a-z0-9]+` — the original fingerprint. It has a
# silent train/serve skew for non-Latin goals: a Russian/CJK goal has ZERO `[a-z0-9]` runs, so its
# fingerprint collapses to just the kind/dir/metric/param tokens and cross-run transfer never reaches
# it (verified on the live `rubertlite` Russian run). UNIVERSAL (opt-in, `fingerprint_universal`):
# `[^\W_]+` under re.UNICODE = word runs of ANY script MINUS underscore — same splitting as the legacy
# regex (underscore stays a separator), just without the alphabet allowlist, over `.casefold()` for
# correct cross-script case folding. This is the CR Step-0 fix: remove the hardcoded charset, don't
# special-case one language. Flagged (not default) because it changes which stored fingerprints a
# LIVE run matches — a running portfolio must not silently re-key mid-flight (see docs/17 §21.20.12).
_WORD_ASCII = re.compile(r"[a-z0-9]+")


def _goal_tokens(goal: str, *, universal: bool) -> list[str]:
    r"""Salient goal keywords, filtered to len>2 non-stopwords. `universal=False` is byte-identical to
    the original `[a-z0-9]+`/`.lower()`; `universal=True` keeps every script via `[^\W_]+`/`.casefold()`."""
    if universal:
        return [w for w in _WORD_UNICODE.findall((goal or "").casefold())
                if len(w) > 2 and w not in _STOP]
    return [w for w in _WORD_ASCII.findall((goal or "").lower())
            if len(w) > 2 and w not in _STOP]


def task_fingerprint(kind: str, direction: str, goal: str, metric: str = "",
                     param_names: Optional[list[str]] = None, *, universal: bool = False) -> list[str]:
    """A cheap, deterministic content fingerprint of a task as a token SET (M2). Cross-run transfer
    should reach a *similar* task, not only the exact same `task_id` — so we key priors/lessons on the
    overlap of these tokens (Jaccard, `fingerprint_similarity`) instead of an exact id match. Tokens:
    the kind/direction/metric (weighted by prefixing), plus salient goal keywords and param names.
    `universal` (opt-in) removes the ASCII-only allowlist on goal keywords so non-Latin goals are not
    silently dropped; default False keeps the legacy fingerprint byte-identical (see `_goal_tokens`)."""
    # NOTE: kind/direction/metric are Jaccard TOKENS here, not hard compatibility gates — two
    # incompatible tasks (min/rmse vs max/recall) can clear the fuzzy floor on shared goal words. The live
    # cross-run consumer therefore applies a HARD `direction` gate on top (engine/novelty._cross_run_prior);
    # the full immutable-facet ComparisonContract that would make this rigorous is the CR0 TODO (§21.20.13).
    toks = {f"kind:{(kind or '').lower()}", f"dir:{(direction or '').lower()}"}
    if metric:
        toks.add(f"metric:{str(metric).lower()}")
    for w in _goal_tokens(goal, universal=universal):
        toks.add(w)
    for p in (param_names or []):
        toks.add(f"param:{str(p).lower()}")
    return sorted(toks)




# --------------------------------------------------------------------------- #
# D2 · Memory hygiene (Phase 3 dep): consolidation, contradiction-quarantine, forgetting.
# Misevolution (ICLR 2026, arXiv:2509.26354) shows append-only cross-run memory causes
# deployment-time reward hacking — agents repeat actions that merely correlated with past
# positive feedback. The memory surveys (arXiv:2512.13564) make consolidation & forgetting
# first-class lifecycle operations. These pure helpers implement both for lessons.jsonl.
# --------------------------------------------------------------------------- #

# Outcomes that CONFLICT with a positive lesson: if the SAME statement was later tested and
# didn't hold (or was abandoned), the earlier "supported" must not be injected any more.
# NOT here (deliberately): `_NEUTRAL` ("noted") — the neutral outcome an untagged reflection line
# gets in `parse_credit_lessons`. It is neither positive nor negative, so it must never quarantine
# a "supported" duplicate nor add support to one (an unknown/legacy outcome behaves the same way:
# not "supported" and not in this set == inert). The WRITE path honors the same neutrality via
# `_verdict_base` / `_accumulated_evidence` (the two shared per-group rules that `consolidate_lessons`
# and `_agentic_merge_lessons` must both apply — which row carries the verdict, and how much support
# the group ends up claiming).
_NEUTRAL = "noted"
# The full verdict vocabulary a row can carry; anything outside it never wins a duplicate group.
_CLAIM_STANCES = frozenset({"support", "oppose", "neutral"})




















# --------------------------------------------------------------------------- #
# M6 · Comparative lesson distillation (MARS "Comparative Reflective Memory", doc 13 §7 item 2):
# credit-assigned lessons from PAIRS of solutions — a child vs. the parent it improved on or
# regressed from ("Solution Lessons"), and a repair vs. the failure it fixed ("Debugging
# Lessons") — instead of only one-shot reflection over a ranked list. Pure helpers: the
# orchestrator owns the LLM call; these select pairs, assign deterministic param-level credit
# (the offline fallback), render code diffs, and parse the LLM's per-pair verdicts.
# --------------------------------------------------------------------------- #

def _improvement(child_metric: float, parent_metric: float, direction: str) -> float:
    """Signed improvement of child over parent, positive = better (direction-aware)."""
    return ((child_metric - parent_metric) if direction == "max"
            else (parent_metric - child_metric))


def unreliable_metric_ids(state) -> set:
    """Node ids whose METRIC may not ground a CROSS-RUN claim — the knowledge-side twin of
    `events/replay.py::promotion_eligible_nodes`.

    WHY THIS IS NOT THE SAME PREDICATE AS PROMOTION ELIGIBILITY, which is the whole reason it is a
    separate rule rather than a call to that one. Promotion eligibility asks "may this node be
    CHAMPION"; this asks the weaker question "is this node's number a measurement at all". The two
    disagree on exactly one population and the disagreement is deliberate: a node that breached one
    of the operator's hard CONSTRAINTS is infeasible and so ineligible for promotion, but its metric
    was measured by the scoring path and its exclusion is a fact about the bound, not about the
    number — `engine/lessons_distill.py` has said so since the first salvage fix ("nothing else is
    filtered") and it keeps its historical place in every reflection population. Routing this
    through `promotion_eligible_nodes` would silently retire that stance.

    TWO MEMBERS, each a node the run has already decided may not be selected ON ITS NUMBER:

      * SALVAGED AND NOT ADMITTED (`metric_salvage.metric_unmeasured`). Nobody measured the value —
        it was recovered from a failed eval by the operator's declared reader. This is the leak this
        function was written for: the `metric_salvaged` violation gated BREEDING and not KNOWLEDGE,
        so a node barred from champion selection still supplied both sides of an M6 comparison pair,
        and the credit-assignment lesson that came out of it ("changing lr 0.1->0.3 improved the
        metric by 0.45") went into the shared `lessons.jsonl` with that node's id as its evidence.
      * TRUST-FLAGGED under `gate`/`block` (`events/replay.py::flagged_node_ids`). A high-precision
        reward-hack or leakage signal, in a run whose operator asked for it to be enforced. The
        number may be a number nobody EARNED, which for a credit-assignment lesson is the same
        defect one step over: the lesson would credit whatever difference produced the cheat. Empty
        under `audit`, exactly as the selection path is — this function never enforces a gate the
        operator turned off, it only refuses to publish across runs what THIS run already refuses to
        select on.

    Deliberately NOT included: tombstoned/aborted nodes (every caller already drops those on its own
    lifecycle grounds) and unevaluated ones (no number to be unreliable).

    ITS INTERSECTION WITH THE CHAMPION IS EMPTY, and that is a theorem rather than a corpus fact —
    both members are populations the SELECTOR already refuses (a `metric_salvaged` row makes the node
    infeasible; `flagged_node_ids` is passed straight into `SearchFitness.eligible`). So this is the
    wrong question to ask about `best_node_id`, and `engine/champion_caveats.py` is the right one: it
    states the COMPLEMENTARY half of these same two families — salvaged-and-ADMITTED, flagged-and-NOT
    ENFORCED — through these same two primitives. The two are one rule read on either side of the
    selection boundary, which is why neither restates the other's predicate.

    Not wrapped in a containment `except`: both halves are total by construction —
    `metric_unmeasured` reads one list and `flagged_node_ids` is the same helper the fold itself
    calls on every replay — and swallowing an error here would return the EMPTY set, i.e. would
    answer "everything is reliable" for a state nobody could read. That is the one wrong answer.
    """
    from looplab.engine.metric_salvage import metric_unmeasured
    from looplab.events.replay import flagged_node_ids
    ids = {n.id for n in (getattr(state, "nodes", None) or {}).values() if metric_unmeasured(n)}
    return ids | set(flagged_node_ids(state))


def select_comparison_pairs(state, k: int = 3, exclude=None) -> list[dict]:
    """Deterministically pick the most informative parent→child pairs to distill from. Two kinds:
    `solution` (both evaluated — the biggest |Δ| wins and regressions are as informative as wins;
    exact ties are skipped: the outcome vocabulary has no 'no effect', so a Δ=0 pair could only be
    mislabeled) and `debug` (parent FAILED, child evaluated — what fixed it). `exclude` = (child,
    parent) id tuples already distilled (later firings must not re-spend LLM budget on the same
    pair). Sorted debug-first then by |Δ| then by ids, so the output is stable under replay.

    EVERY PAIR IS A CLAIM ABOUT THE METRIC, which is why `unreliable_metric_ids` bars a node from
    BOTH sides. A `solution` pair's entire content is the Δ between two numbers and the lesson
    credits whichever difference produced it; a `debug` pair's prompt states that the repair
    "reached metric=X". So a node whose number nobody measured (a salvaged node under the default
    `audit` rung) or whose number the run's own trust gate refuses cannot be one half of a pair
    without the lesson asserting precisely what that exclusion denies — and unlike a champion pick,
    the assertion LEAVES the run: it is appended to the shared `lessons.jsonl` and retrieved by
    later runs as evidence. This is the knowledge half of the boundary
    `engine/metric_salvage.py::metric_unmeasured` documents; what such a node observed that is
    INDEPENDENT of its metric is still recorded (its concept tags in the run's capsule, its failure
    in the reflection prompt's observation rows) — only the metric claim is refused."""
    from looplab.core.models import NodeStatus
    excl = {tuple(p) for p in (exclude or [])}
    aborted = set(getattr(state, "aborted_nodes", None) or [])
    unreliable = unreliable_metric_ids(state)
    pairs: list[dict] = []
    for n in state.nodes.values():
        if n.metric is None or n.tombstoned or n.id in aborted or n.id in unreliable:
            continue
        for pid in n.parent_ids:
            p = state.nodes.get(pid)
            # deleted/aborted attempts are audit history, never live evidence from which a
            # reusable comparative lesson may be distilled or re-derived; a node whose METRIC this
            # run refuses to select on is the same rule one field over (see the docstring).
            if (p is None or p.tombstoned or pid in aborted or pid in unreliable
                    or (n.id, pid) in excl):
                continue
            if p.metric is not None:
                delta = _improvement(n.metric, p.metric, state.direction)
                if delta != 0:
                    pairs.append({"kind": "solution", "a": n.id, "b": pid, "delta": delta})
            elif p.status is NodeStatus.failed:
                pairs.append({"kind": "debug", "a": n.id, "b": pid, "delta": None})
    pairs.sort(key=lambda pr: (0 if pr["kind"] == "debug" else 1,
                               -abs(pr["delta"] or 0.0), pr["a"], pr["b"]))
    return pairs[:max(0, k)]


def param_credit_statement(winner, loser, delta: float):
    """Deterministic (offline) credit assignment for a solution pair: when the two ideas differ in
    a SMALL number of params, the changed params ARE the credited difference. None when the diff
    is empty, too wide to attribute cleanly (>3 params), or the metric didn't move (a Δ=0 change
    is neither GOOD nor BAD) — no lesson beats a mushy lesson."""
    if not delta:
        return None
    pa = dict(getattr(winner.idea, "params", None) or {})
    pb = dict(getattr(loser.idea, "params", None) or {})
    changed = [(name, pb.get(name), pa.get(name))
               for name in sorted(set(pa) | set(pb)) if pa.get(name) != pb.get(name)]
    if not changed or len(changed) > 3:
        return None
    diff_txt = ", ".join(f"{name} {old!r}->{new!r}" for name, old, new in changed)
    verb = "improved" if delta > 0 else "regressed"
    return f"changing {diff_txt} {verb} the metric by {abs(delta):.4g}"


def code_diff(old: str, new: str, max_lines: int = 60) -> str:
    """Compact unified diff of two solutions (the comparative prompt's evidence). Empty when
    either side has no code or the codes are identical."""
    import difflib
    if not (old or "").strip() or not (new or "").strip():
        return ""
    lines = list(difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(),
                                      fromfile="loser", tofile="winner", lineterm="", n=2))
    return "\n".join(lines[:max_lines])


_PAIR_LINE = re.compile(r"^P(\d+)\b\s*[:.\-]?\s*(.*)$", re.I)


def parse_credit_lessons(text: str, n_pairs: int, limit: Optional[int] = None) -> list[tuple[int, str, str]]:
    """Parse the LLM's per-pair verdict lines (`P<n> [GOOD|BAD] <lesson>`) into
    (pair_index, statement, outcome) tuples. pair_index is -1 when the line carries no usable
    P-marker (the lesson still counts, unattributed). Tolerant of bullets/numbering; capped.

    `n_pairs` only clamps index VALIDITY (a P-marker beyond the real pair count collapses to -1).
    The COUNT cap is `limit` (default `max(3, n_pairs)` for the comparative caller, whose lessons
    naturally track its pair count). The whole-run reflection caller passes n_pairs=0 (its lines
    carry no valid P-marker) and MUST pass an explicit limit — otherwise the default max(3,0)=3
    silently capped reflection lessons at 3 instead of the intended 8 (architecture-review M6)."""
    cap = limit if limit is not None else max(3, n_pairs)
    out: list[tuple[int, str, str]] = []
    for line in (text or "").splitlines():
        s = line.strip().lstrip("-*•0123456789.) ").strip()
        m = _PAIR_LINE.match(s)
        idx = (int(m.group(1)) - 1) if m else -1
        body = m.group(2) if m else s
        low = body.lower()
        good, bad = "[good]" in low, "[bad]" in low
        body = re.sub(r"\[(good|bad)\]", "", body, flags=re.I).strip(" :-–")
        if len(body) < 8:
            continue
        # An UNTAGGED line gets the NEUTRAL outcome `_NEUTRAL` ("noted") — the model didn't say
        # which way the evidence points, so the lesson must neither corroborate nor contradict
        # anything. The old default was "tested", which is in `_NEGATIVE`: one tag-noncompliant
        # reflection line could quarantine a matching "supported" lesson at read time
        # (filter_contradicted). "noted" is excluded from both sides by construction (not
        # "supported", not in _NEGATIVE); rows already stored with the old value keep their
        # (negative) meaning — no migration, readers tolerate both.
        out.append((idx if 0 <= idx < n_pairs else -1, body,
                    "failed" if bad else ("supported" if good else _NEUTRAL)))
        if len(out) >= cap:
            break
    return out


# --------------------------------------------------------------------------- #
# M4 · Auto-distilled skills: episodic → procedural memory. A technique that repeatedly won
# in a run is drafted as a candidate SKILL.md under <memory_dir>/skills/; it is PROMOTED when
# a later run with a DIFFERENT task fingerprint confirms it (won on two distinct tasks).
# --------------------------------------------------------------------------- #

def skill_slug(statement: str) -> str:
    """Legacy human-readable auto-skill slug (kept for stored-file compatibility)."""
    norm = re.sub(r"[^a-z0-9]+", "-", normalize_statement(statement)).strip("-")[:48]
    return norm or "skill"


def _canonical_auto_skill_claim(statement: str) -> str:
    """Uncapped claim identity; presentation helpers intentionally truncate and cannot be keys."""
    return " ".join(str(statement or "").split()).lower()


def _auto_skill_identity(statement: str) -> tuple[str, str, str]:
    """Return normalized claim, full digest and collision-resistant readable storage id."""
    canonical = _canonical_auto_skill_claim(statement)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    # Readability is presentation only and may be capped/collide; the complete digest is the key.
    readable = re.sub(
        r"[^a-z0-9]+", "-", normalize_statement(statement)).strip("-")[:40] or "skill"
    return canonical, digest, f"{readable}-{digest}"


def _legacy_auto_skill_claim_matches(text: str, canonical: str) -> bool:
    """Prove an old slug-only file was written for this exact normalized statement.

    Pre-digest files did not persist a claim id.  Their writer did persist the statement as the
    leading Markdown heading; reuse historical fingerprints only when that complete heading block
    normalizes exactly. Ambiguous/malformed legacy content starts a new candidate instead.
    """
    match = re.match(r"\A---\r?\n.*?\r?\n---\r?\n?(.*)\Z", text, re.DOTALL)
    if not match:
        return False
    heading = re.split(r"\r?\n\r?\n", match.group(1), maxsplit=1)[0].strip()
    return (heading.startswith("# ")
            and _canonical_auto_skill_claim(heading[2:]) == canonical)


def _stored_skill_fingerprints(raw: str) -> list[list[str]]:
    """Parse the bounded shape written in auto-skill frontmatter, failing closed on drift.

    This is trust-bearing lifecycle evidence, not generic JSON.  Accepting a dict/string here makes
    iteration look superficially valid and can falsely satisfy the cross-task promotion test.
    Six histories is the writer's existing retention cap; the generous inner bounds prevent a
    hand-edited file from turning this best-effort path into unbounded work without rejecting normal
    task fingerprints.
    """
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(value, list) or len(value) > 6:
        return []
    for fingerprint in value:
        if (not isinstance(fingerprint, list) or len(fingerprint) > 512
                or any(not isinstance(token, str) or len(token) > 1024
                       for token in fingerprint)):
            return []
    return value


# A SKILL IS A TECHNIQUE, NOT A FACT ABOUT ONE NODE. The promotion gate asked three things —
# supported, positive delta, non-empty statement — and never whether the statement generalizes. So
# every auto-distilled skill in the shipped store was instance-specific. Measured 2026-08-12 over the
# 27 files this had written:
#
#     perturb best node 8 (metric=5.4404437)
#     perturb node 9 (params={'x': 3.7898})
#     mean-merge of nodes 0,1
#
# Twenty-seven of twenty-seven. Not one is a technique a later run could apply: they name a node id
# from one run's tree and a float from one run's objective, and the same operation appears five times
# under five node numbers as five separate "skills". That is the operator's "the auto classifier is
# rubbish" in full — the classifier is not bad at ranking, it never asked the question.
#
# Deliberately CONSERVATIVE, and shaped by that corpus rather than by imagination: it rejects only
# what demonstrably cannot transfer — a node id, an embedded parameter literal, an embedded metric
# value. Anything else still promotes, because a false negative here silently loses procedural memory
# and the whole point of the tier is to accumulate it.
_INSTANCE_SPECIFIC_SKILL = (
    re.compile(r"\bnodes?\s+\d", re.I),          # "perturb node 9", "mean-merge of nodes 0,1"
    re.compile(r"\bmetric\s*=\s*[-+0-9.]"),      # "(metric=5.4404437)"
    re.compile(r"\bparams?\s*=?\s*[{(]"),        # "params={'x': 3.7898}"
    re.compile(r"[{\[]\s*['\"]\w+['\"]\s*:"),   # a bare parameter dict anywhere in the text
)


def promotable_skill_statement(statement) -> bool:
    """Can this claim become a SKILL — something a later run could apply?

    See `_INSTANCE_SPECIFIC_SKILL` for the measured corpus this is shaped by. Returning False is not
    a judgement about the claim's truth: the lesson tier still keeps it with its evidence. It is a
    judgement about whether the sentence says anything a different run could act on.
    """
    text = str(statement or "").strip()
    if len(text) < 12:
        # Shorter than "use dropout" — there is no technique in it, and the store's own titles are
        # derived from this text, so an empty-ish claim also mints an unreadable file name.
        return False
    return not any(pattern.search(text) for pattern in _INSTANCE_SPECIFIC_SKILL)


def write_auto_skill(skills_dir: str | Path, statement: str, body: str,
                     fingerprint: list[str], task_id: str) -> Optional[Path]:
    """Draft/refresh an auto-distilled skill. New claim -> status: candidate. If a candidate
    with the same full normalized-claim identity exists from a DIFFERENT task fingerprint
    (Jaccard < 0.6), the technique generalized -> status: promoted. Never raises (best-effort
    memory). A readable prefix is not lifecycle identity: the filename/name also carry the full
    SHA-256 so two long claims with the same prefix cannot share promotion evidence."""
    try:
        d = Path(skills_dir)
        d.mkdir(parents=True, exist_ok=True)
        canonical_claim, claim_digest, storage_id = _auto_skill_identity(statement)
        digest_path = d / f"auto-{storage_id}.md"
        legacy_path = d / f"auto-{skill_slug(statement)}.md"
        from contextlib import ExitStack
        from looplab.events.eventstore import _interprocess_lock
        # One directory-level identity lock makes legacy-path selection and the read-modify-write
        # atomic together. Per-file locks cannot protect two different full claims that alias the
        # same old 48-character slug while one process is deciding whether the legacy evidence is
        # reusable. Auto-skill writes happen only at reflection/finalize, so this bounded
        # serialization does not sit on the experiment hot path.
        # A filesystem that cannot provide the lock leaves the draft unwritten (the outer except
        # returns None): skipping one best-effort skill beats clobbering another run's evidence.
        with ExitStack() as locks:
            locks.enter_context(_interprocess_lock(d / ".auto-skills.lock", required=True))
            p = digest_path
            if not digest_path.exists() and legacy_path.exists():
                legacy_text = legacy_path.read_text(encoding="utf-8")
                from looplab.tools.skills import parse_skill_frontmatter
                legacy_metadata = parse_skill_frontmatter(legacy_text)
                if (legacy_metadata.get("provenance", "").strip().lower() == "auto"
                        and (legacy_metadata.get("claim_sha256") == claim_digest
                             or (not legacy_metadata.get("claim_sha256")
                                 and _legacy_auto_skill_claim_matches(
                                     legacy_text, canonical_claim)))):
                    # Keep the old path so promoted/candidate history remains continuous. The
                    # rewritten frontmatter gains the exact digest and collision-resistant tool
                    # name; a different same-prefix claim cannot enter this branch.
                    p = legacy_path
            # Retain the historical per-identity lock as an observable concurrency contract for
            # existing cooperating writers, nested after the directory identity-selection lock.
            locks.enter_context(_interprocess_lock(Path(str(p) + ".lock"), required=True))
            status, fps = "candidate", [fingerprint]
            if p.exists():
                head = p.read_text(encoding="utf-8")
                # Read lifecycle state only from the real leading frontmatter.  The skill body is
                # model-authored Markdown and may legitimately discuss a line such as
                # ``status: promoted``; treating that substring as authority promoted a one-task
                # candidate on its next same-task refresh.  Share the reader's fence parser so the
                # writer and production visibility gate agree on the trust boundary.
                from looplab.tools.skills import parse_skill_frontmatter
                metadata = parse_skill_frontmatter(head)
                exact_claim = (
                    metadata.get("provenance", "").strip().lower() == "auto"
                    and (metadata.get("claim_sha256") == claim_digest
                         or (p == legacy_path and not metadata.get("claim_sha256")
                             and _legacy_auto_skill_claim_matches(head, canonical_claim)))
                )
                if exact_claim:
                    raw_fingerprints = metadata.get("fingerprints")
                    if raw_fingerprints is not None:
                        fps = _stored_skill_fingerprints(raw_fingerprints)
                    prior_status = metadata.get("status", "").strip().lower()
                    different = any(
                        fingerprint_similarity(fingerprint, old) < 0.6 for old in fps if old)
                    status = (
                        "promoted" if different or prior_status == "promoted" else "candidate")
                    if fingerprint not in fps:
                        fps = (fps + [fingerprint])[-6:]
            source_task = json.dumps(str(task_id), ensure_ascii=False)
            # json.dumps escapes CR/LF and ASCII controls, but deliberately leaves these three
            # Unicode line separators intact under ensure_ascii=False.  Escape them explicitly so
            # the audit scalar is one logical AND physical line for non-Python frontmatter readers.
            for separator, escape in (("\u0085", r"\u0085"), ("\u2028", r"\u2028"),
                                      ("\u2029", r"\u2029")):
                source_task = source_task.replace(separator, escape)
            text = ("---\n"
                    f"name: auto-{storage_id}\n"
                    f"description: {normalize_statement(statement)[:120]}\n"
                    "provenance: auto\n"
                    f"status: {status}\n"
                    f"claim_sha256: {claim_digest}\n"
                    # ``task_id`` is operator-authored.  A raw newline here could inject a later
                    # ``status``/``provenance`` field into the trust-bearing frontmatter (whose
                    # duplicate-key compatibility rule is last-one-wins).  JSON keeps it on one
                    # physical line while retaining the full Unicode identifier for audit.
                    f"source_task: {source_task}\n"
                    f"fingerprints: {json.dumps(fps)}\n"
                    "---\n\n"
                    f"# {statement.strip()}\n\n{body.strip()}\n")
            atomic_write_text(p, text)
        return p
    except Exception:  # noqa: BLE001 — skill distillation is best-effort, never fails a run
        return None


def valid_case_record(case) -> bool:
    """Whether one unversioned durable case is safe for comparison and retrieval.

    ``cases.jsonl`` predates an explicit schema version. A version/kind discriminator therefore
    belongs to a future contract and must stay quarantined until an explicit migration understands it.
    """
    if not isinstance(case, dict) or "v" in case or "record_kind" in case:
        return False
    task_id = case.get("task_id")
    if (not isinstance(task_id, str) or not task_id.strip()
            or len(task_id) > 500):
        return False
    for key, maximum in (("goal", 4000), ("rationale", 8000)):
        value = case.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > maximum):
            return False
    direction = case.get("direction", "min")
    if direction not in ("min", "max"):
        return False
    metric = case.get("metric")
    if not _is_finite_metric(metric):
        return False
    params = case.get("params")
    return params is None or isinstance(params, dict)


class JsonlCaseLibrary:
    """THE case store the engine actually uses (I19, ADR-10) — `lessons.py::store_case` builds it.

    Cases on disk as JSONL, keyed by (task_id, direction) with retain-on-improvement. Loads existing cases on init
    so it accumulates across runs. `search` does a keyword/recency lookup (no embedding dependency).

    The vector-backed `CaseLibrary` above claims the same I19/ADR-10 role in its own docstring but is
    unwired; that ambiguity used to be resolvable only by grepping for constructors (doc 25 EM-11).

    One lesson worth carrying if the harmonic path is ever wired in: `CaseLibrary._consolidate` had to
    learn that two cases are only comparable when their objective DIRECTION matches, or merging picks
    the wrong winner across a min/max boundary. This store sidesteps it by keying on task_id, which
    carries the direction with it."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cases: list[dict] = []
        self._reload()   # load existing cases (same malformed-line-tolerant parse used on every reload)

    def _reload(self) -> None:
        """(Re)read the on-disk cases into `self.cases`. Used by __init__ AND inside the interprocess
        lock on every add() so a concurrent run's cases aren't clobbered by this run's stale in-memory
        copy. One malformed/truncated line is skipped, never making the whole cross-run memory
        permanently unloadable."""
        rows = read_jsonl_lenient(self.path, loads=json.loads, dicts_only=True)
        # a syntactically-valid scalar (or a dict with an incompatible metric/params
        # shape) is still a poisoned case row. Quarantine it here so search and the next locked
        # upsert cannot crash on ``.get`` or on a cross-type metric comparison.
        self.cases = [case for case in rows if self._valid_case(case)]

    @staticmethod
    def _valid_case(case) -> bool:
        return valid_case_record(case)

    def add(self, case: dict) -> bool:
        """Upsert by task_id, retaining the better metric. Returns True if stored.

        Under the same required interprocess lock the other mutable cross-run stores use: `add` is a
        read-modify-write, so two runs sharing `memory_dir` would otherwise clobber each other. We
        RE-READ inside the lock so this run's possibly-stale in-memory cases cannot overwrite a concurrent
        write. A filesystem unable to provide that guarantee fails closed and finalize retries later."""
        if not self._valid_case(case):
            return False
        from looplab.events.eventstore import _interprocess_lock
        with _interprocess_lock(Path(str(self.path) + ".lock"), required=True):
            self._reload()
            return self._add_locked(case)

    def _add_locked(self, case: dict) -> bool:
        tid = case.get("task_id")
        direction = case.get("direction", "min")
        metric = case.get("metric")
        run_uid = case.get("run_uid")
        if isinstance(run_uid, str) and run_uid:
            # Modern rows are source contributions, not a destructive champion slot. Re-finalizing
            # one run replaces that run's contribution even when reset/replay made it worse; retaining
            # inactive siblings lets the next-best source become active after such a withdrawal.
            group = [c for c in self.cases
                     if c.get("task_id") == tid and c.get("direction", "min") == direction]
            candidates = [c for c in group if c.get("run_uid") != run_uid] + [dict(case)]
            measured = [c for c in candidates if c.get("metric") is not None]
            if measured:
                winner = (min(measured, key=lambda c: c["metric"]) if direction == "min"
                          else max(measured, key=lambda c: c["metric"]))
            else:
                winner = candidates[-1]
            projected = [{**c, "active": c is winner} for c in candidates]
            replace_jsonl_rows_atomic_preserving_quarantine(
                self.path, projected,
                replace_if=lambda row: (
                    valid_case_record(row) and row.get("task_id") == tid
                    and row.get("direction", "min") == direction),
                loads=json.loads, dumps=json.dumps,
            )
            self._reload()
            return winner is candidates[-1]
        prev = next((c for c in self.cases
                     if c.get("task_id") == tid and c.get("direction", "min") == direction), None)
        if prev is not None:
            # Keep the old case only when both metrics are comparable and the new one is not better.
            # An UNMEASURED new case never displaces a MEASURED stored one: `valid_case_record`
            # admits `metric=None`, and the incomparable branch used to fall straight through to the
            # replace — inverting the module's retain-on-improvement contract for exactly the writer
            # that has no evidence to justify the replacement. Replacing an unmeasured prior is still
            # allowed (nothing is lost), as is the first write for a task.
            if metric is not None and prev.get("metric") is not None:
                better = metric < prev["metric"] if direction == "min" else metric > prev["metric"]
                if not better:
                    return False
            elif metric is None and prev.get("metric") is not None:
                return False
        # quarantine is a read decision, never permission for an unrelated upsert to erase
        # malformed or future-schema bytes. Replace only understood current rows for this task; retain every
        # other raw line byte-for-byte and append the new current record atomically under the required lock.
        replace_jsonl_rows_atomic_preserving_quarantine(
            self.path, [case],
            replace_if=lambda row: (
                valid_case_record(row) and row.get("task_id") == tid
                and row.get("direction", "min") == direction),
            loads=json.loads, dumps=json.dumps,
        )
        self._reload()
        return True

    def search(self, query: str, k: int = 3) -> list[dict]:
        try:
            limit = max(0, min(int(k), 64))
        except (TypeError, ValueError, OverflowError):
            return []
        if not isinstance(query, str) or not limit:
            return []
        q = set(query.lower().split())
        # `valid_case_record` admits an explicit `goal: null` row, so `c.get("goal")` can be None here;
        # `... or ""` degrades that to an empty string instead of raising TypeError on `None + " "`.
        active = [c for c in self.cases if c.get("active") is not False]
        scored = [(len(q & set(((c.get("goal") or "") + " " + c.get("task_id", "")).lower().split())), c)
                  for c in active]
        scored.sort(key=lambda t: -t[0])
        return [c for _, c in scored[:limit]]

    def all(self) -> list[dict]:
        return [c for c in self.cases if c.get("active") is not False]


# Schema version for the durable capsule record — bump when the shape changes so a reader can migrate/
# reject incompatible generations instead of silently mis-reading them (the full record
# — evidence node-refs, visibility/retention/purge key, concept UID+taxonomy version — is the CR1a TODO).
# v2 makes the evidence producer explicit. V1 and unversioned rows predate the authored-vs-classifier
# trust boundary and cannot be upgraded honestly from their payload alone, so readers quarantine them.

























# A concept whose outcome sits within this fraction of the run's own outcome SPREAD around the median is
# scored NEUTRAL, not forced onto a side. Without it a median split labels ~half the concepts helped and
# half hurt every run (a weak, self-balancing signal); the band lets only concepts that clearly out- or
# under-performed their run's field carry a sign, so the cross-run rollup is sparser and more meaningful.














_MAX_DIGEST_AXES = 512
_MAX_DIGEST_CONCEPTS_PER_AXIS = 64




def portfolio_digest(capsules: list[dict], *, aliases: Optional[dict] = None,
                     splits: Optional[dict] = None) -> dict:
    """PART IV cross-run Step 7 (lean, GATED): a flat, display-only rollup above the concept overview.

    Concepts are grouped by the conventional prefix before ``/`` (for example
    ``data/hard-negative-mining`` -> ``data``). This is deterministic and does not claim to be a persisted
    semantic hierarchy. Per the §21.20.11 hierarchy gate it ships as inspector data only; it is not wired
    into prompts until a versioned taxonomy proves its value on the benchmark corpus.
    """
    from looplab.engine.concept_registry import canonicalize_concepts

    valid_capsules = _dedup_valid_capsules(capsules)
    clusters: dict[str, dict] = {}
    for capsule in valid_capsules:
        run_id = capsule["run_id"]
        for concept in canonicalize_concepts(
                capsule.get("concepts") or [], aliases=aliases, splits=splits):
            # This is an unenforced display convention, not a hierarchy: every unprefixed concept lands in
            # one bucket and changing a display slug can move it. Do not infer semantic ancestry from ``/``.
            axis = concept.split("/", 1)[0] if "/" in concept else "(ungrouped)"
            cl = clusters.setdefault(axis, {"axis": axis, "_concepts": set(), "_runs": set()})
            cl["_concepts"].add(concept)
            cl["_runs"].add(run_id)

    # compute exact axis/concept/run totals from the full validated, de-duplicated retained
    # snapshot BEFORE bounding display collections. Building on `portfolio_concept_overview` silently capped
    # each axis at 64 run ids and the whole digest at 512 concepts while presenting both counts as exact.
    axes = []
    for cluster in clusters.values():
        concepts = sorted(cluster["_concepts"])
        retained = concepts[:_MAX_DIGEST_CONCEPTS_PER_AXIS]
        axes.append({
            "axis": cluster["axis"],
            "n_concepts": len(concepts),
            "n_runs": len(cluster["_runs"]),
            "concepts": retained,
            "concepts_omitted": len(concepts) - len(retained),
        })
    axes.sort(key=lambda c: (-c["n_concepts"], -c["n_runs"], c["axis"]))
    retained_axes = axes[:_MAX_DIGEST_AXES]
    n_concepts = sum(len(cluster["_concepts"]) for cluster in clusters.values())
    retained_concepts = sum(len(axis["concepts"]) for axis in retained_axes)
    return {
        "n_axes": len(axes),
        "n_concepts": n_concepts,
        "axes": retained_axes,
        "axes_omitted": len(axes) - len(retained_axes),
        "concepts_omitted": n_concepts - retained_concepts,
        **_capsule_source_summary(valid_capsules),
    }


class CaseLibrary:
    """UNWIRED (doc 25 EM-11): the vector-backed episodic case store, kept for the Memora path.

    Nothing under `looplab/` constructs this — the engine's real case store is `JsonlCaseLibrary`
    below, reached through `lessons.py::store_case`. Only tests exercise this class today, so read it
    as a prototype of the harmonic path rather than as live behaviour; a reader tracing "where do
    cases come from" wants `JsonlCaseLibrary`.

    It is retained rather than deleted because its tests are the only coverage of Memora
    consolidation/expansion, which is still the intended direction for the case path. Wiring it in
    means giving it `JsonlCaseLibrary`'s durability contract (whole-file reload, quarantine-preserving
    rewrite, retain-on-improvement across runs) — it has none of those today.

    Episodic case store over a `VectorStore`. Optionally *harmonic* (Memora): pass an `abstract`
    callable (see `tools.memora.make_abstractor`) to index each case by a short abstraction + cue
    anchors instead of its raw task text, CONSOLIDATE a near-duplicate case into the existing entry on
    `add`, and EXPAND `retrieve` through the top hits' anchors. With `abstract=None` (the default) every
    method is byte-identical to the pre-Memora behavior."""

    def __init__(self, store: VectorStore, embed: Callable[[str], list[float]] = hash_embed,
                 index: str = "cases", abstract: Optional[Callable[[str], object]] = None,
                 consolidate_threshold: float = 0.86, expand: bool = True):
        self.store = store
        self.embed = embed
        self.index = index
        self.abstract = abstract
        self.consolidate_threshold = consolidate_threshold
        self.expand = expand

    @staticmethod
    def _content(task_desc: str, payload: dict) -> str:
        """The rich memory VALUE the abstraction summarizes: the task plus the case's own words."""
        extra = " ".join(str(payload.get(k, "")) for k in ("rationale", "params", "operator"))
        return f"{task_desc} {extra}".strip()

    def _harmonic_item(self, case_id: str, task_desc: str, payload: dict):
        """Build the `(vector, payload, abstraction)` for a harmonic case: embed the abstraction+anchors
        (not the raw text) and carry the anchors in the payload so retrieval can expand through them."""
        ab = self.abstract(self._content(task_desc, payload))  # type: ignore[misc]
        vec = self.embed(ab.index_text())
        p = {**payload, "abstraction": ab.primary, "anchors": list(ab.anchors)}
        return Item(case_id, vec, p), ab

    def add(self, case_id: str, task_desc: str, payload: dict) -> None:
        if self.abstract is None:                       # legacy path — byte-identical to before
            self.store.upsert(self.index, [Item(case_id, self.embed(task_desc), payload)])
            return
        item, ab = self._harmonic_item(case_id, task_desc, payload)
        # Consolidation: if a stored case sits at/above the threshold under the SAME abstraction, merge
        # into it rather than growing a chain of near-duplicates (Memora: ~half the entries of a flat
        # store). Never merge onto self (a re-add of the same id is a plain upsert).
        near = self.store.search(self.index, item.vector, 1)
        if near and near[0].id != case_id and near[0].score >= self.consolidate_threshold:
            self._consolidate(near[0], ab, payload)
            return
        self.store.upsert(self.index, [item])

    def _consolidate(self, target: Hit, ab, payload: dict) -> None:
        """Fold a new case into `target`: union the anchors, keep the richer abstraction, keep the
        better metric, and re-embed the merged abstraction under the target's id."""
        from looplab.tools.memora import Abstraction
        prev = Abstraction(str(target.payload.get("abstraction", "")),
                           list(target.payload.get("anchors", [])))
        merged_ab = prev.merge(ab)
        # Consolidation fires on embedding similarity alone, which says nothing about the two
        # cases' OBJECTIVES. Folding a min-task metric and a max-task metric under one direction
        # keeps the WORSE number for whichever case disagrees — silently, and the merged case then
        # advises future runs with it. When the directions differ (or one is unknown) keep the
        # TARGET's own metric instead of picking a winner across incomparable scales.
        old_dir, new_dir = target.payload.get("direction"), payload.get("direction")
        direction = new_dir or old_dir or "min"
        comparable = (old_dir or direction) == (new_dir or direction)
        p = {**target.payload, **payload}               # newer content wins for scalar fields
        om, nm = target.payload.get("metric"), payload.get("metric")
        if om is not None and nm is not None and not comparable:
            p["metric"] = om                            # incomparable objectives -> keep the target's
        elif om is not None and nm is not None:
            p["metric"] = min(om, nm) if direction == "min" else max(om, nm)
        elif om is not None:
            p["metric"] = om
        p["abstraction"] = merged_ab.primary
        p["anchors"] = list(merged_ab.anchors)
        p["merged"] = int(target.payload.get("merged", 1)) + 1
        self.store.upsert(self.index, [Item(target.id, self.embed(merged_ab.index_text()), p)])

    def retrieve(self, task_desc: str, k: int = 3) -> list[Hit]:
        hits = self.store.search(self.index, self.embed(task_desc), k)
        if self.abstract is None or not self.expand:    # legacy: exactly k, no expansion
            return hits
        from looplab.tools.memora import expand_by_anchors
        extra = expand_by_anchors(self.store, self.index, hits, self.embed, k=k)
        seen = {h.id for h in hits}
        return hits + [h for h in extra if h.id not in seen]

    def retain_if_improved(self, case_id: str, task_desc: str, payload: dict,
                           metric: float, direction: str = "min") -> bool:
        """Store/replace only if better than the existing case. Returns True if stored. Keyed by
        `case_id` (no consolidation here — that would break the id-based lookup); when harmonic, the
        stored entry still carries abstraction+anchors so retrieval can expand through them."""
        existing: Optional[Hit] = None
        getter = getattr(self.store, "get", None)
        if callable(getter):
            existing = getter(self.index, case_id)
        if existing is not None:
            prev = existing.payload.get("metric")
            if prev is not None:
                better = metric < prev if direction == "min" else metric > prev
                if not better:
                    return False
        if self.abstract is None:                       # legacy: unchanged payload shape
            self.store.upsert(self.index, [Item(case_id, self.embed(task_desc),
                                                {**payload, "metric": metric})])
        else:
            item, _ = self._harmonic_item(case_id, task_desc,
                                          {**payload, "metric": metric, "direction": direction})
            self.store.upsert(self.index, [item])
        return True
