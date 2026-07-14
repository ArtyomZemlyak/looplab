"""Advisory verifier — the calibrated §12 "LLM-as-a-Verifier" primitive (PART IV keystone B, §21.13).

**Why this exists.** LoopLab has no first-class critic (§3 ⬜). D3 (failed-direction re-examination),
D6 (lesson over-generalization guard), and even the shipped foresight confidence all need a judge, and
the `rubertlite` offline measurements proved a **blind single-shot LLM judge is unreliable**: on the
node_63 re-examination it *reproduced the loop's own mistake* AND self-contradicted between two framings
(§21.10), and across four real regressions it was **high-variance, not consistently biased** — the same
input flipped "re-examine" ↔ "don't" at temp 0.6 (§21.12). Separately, the shipped foresight confidence
correlates ≈0 with realized outcome (Pearson −0.10, §21.12). The lesson (§21.13 keystone B): a judge that
steers must be **grounded + repeated + criteria-decomposed**, never a single blind call.

**What this is.** A general advisory scorer:
  * **criteria-decomposed** — the judgment is split into named `Criterion`s, each graded on an ordinal
    scale, so the verdict is legible ("it over-generalizes on *scope* but the *direction* is sound")
    rather than one opaque yes/no;
  * **repeated** — each criterion is sampled `samples` times and the score is the SAMPLE-MEAN of the
    ordinal values. §12's calibrated score is a logit-EXPECTATION over the verdict distribution; the LLM
    client here exposes no logprobs (confirmed: `core/llm.py` never sends `logprobs`), so we approximate
    that expectation by sampling — the discrete fallback §21.13/0c prescribes. Repetition is what tames
    the measured single-shot variance;
  * **grounded** — the caller passes the checkable `evidence` (node outcomes, the D1 prior-art brief,
    §17 distance-from-seed); the optional tool-reading variant additionally lets the judge READ the run
    it is scoring (mirrors `trust/verify.py::verify_memo`).

**Discipline (normative, §21.7).** Strictly ADVISORY / audit — a `VerdictReport` never overrides a
fixed-metric ground truth and is not read by best-selection. Best-effort: no client, or any model
failure, degrades to an explicit `method="unavailable"` report rather than blocking the caller. The
critic is a *primitive the Strategist consults*, not a Strategist sub-mode (the §4 ownership split).

**Phase 0 scope.** Offline foundation: the scorer + a `calibrate` harness that measures whether the
score tracks a labelled gold outcome (§12's own evaluation gate — "calibrate on the run's labelled
cases"). Wiring it into live selection (foresight replacement, novelty re-exam) is Phase 1c/2c.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean, pstdev
from typing import Callable, Optional


# --------------------------------------------------------------------------- #
# Ordinal verdict scale (the sampling-based expectation the no-logprob backend forces)
# --------------------------------------------------------------------------- #
#
# A small 5-level ordinal per criterion. Weak local models grade an ordinal reliably where they emit
# noisy free floats; the sample-mean of the ordinal VALUES is the continuous score. Higher = the
# criterion's proposition holds MORE strongly (the caller frames each criterion so "high" is the thing
# it wants to measure). §12's conservative default (uncertain -> unsupported) maps to the neutral 0.5.

_VERDICT_VALUE: dict[str, float] = {
    "strong_no": 0.0, "no": 0.25, "unclear": 0.5, "yes": 0.75, "strong_yes": 1.0,
}
_VERDICT_ORDER = ["strong_no", "no", "unclear", "yes", "strong_yes"]
# Synonyms a model reaches for — normalized before lookup so a near-miss verdict still scores.
_VERDICT_SYNONYM = {
    "true": "yes", "false": "no", "supported": "yes", "unsupported": "no",
    "yes.": "yes", "no.": "no", "likely": "yes", "unlikely": "no", "maybe": "unclear",
    "strongly_yes": "strong_yes", "strongly_no": "strong_no", "definitely": "strong_yes",
    "definitely_not": "strong_no", "n/a": "unclear", "unknown": "unclear",
}


def _verdict_to_score(v) -> Optional[float]:
    """Map a raw model verdict to [0,1]. Accepts the ordinal labels, common synonyms, or a bare number
    in [0,1] (a model that ignored the scale and emitted a float). Returns None on the truly
    unparseable — a MISSING/blank verdict (a criterion the model skipped) is dropped, NOT fabricated
    into a neutral 0.5 vote, so aggregation reflects only the criteria actually graded."""
    if v is None or isinstance(v, bool):
        return None                                         # a bool is not a score; None = not graded
    if isinstance(v, (int, float)):
        f = float(v)
        return min(1.0, max(0.0, f)) if 0.0 <= f <= 1.0 else None
    s = str(v).strip().lower().replace(" ", "_").replace("-", "_")
    if s == "":
        return None                                         # blank -> dropped, not an `unclear` vote
    s = _VERDICT_SYNONYM.get(s, s)
    if s in _VERDICT_VALUE:
        return _VERDICT_VALUE[s]
    # a stray float-in-string ("0.8")
    try:
        f = float(s)
        return min(1.0, max(0.0, f)) if 0.0 <= f <= 1.0 else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Public data shapes
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Criterion:
    """One decomposed sub-question of a verdict. `question` is graded on the ordinal scale where a HIGH
    score means the proposition is true. `weight` folds into the overall score."""
    key: str
    question: str
    weight: float = 1.0


@dataclass
class SampleVerdict:
    """One repeated evaluation: a per-criterion score in [0,1] plus the model's short rationale."""
    scores: dict[str, float]
    rationales: dict[str, str] = field(default_factory=dict)


@dataclass
class VerdictReport:
    """Aggregate of the repeated, criteria-decomposed evaluation.

    score        - overall calibrated-ready score in [0,1]: weighted mean of the per-criterion sample
                   means (None when unavailable / no client)
    per_criterion- {key: {mean, std, n, scores:[...]}} per criterion across samples
    agreement    - cross-sample stability in [0,1]: mean over criteria of the modal-verdict fraction
                   (1.0 = every sample agreed; the single-shot-variance detector §21.12 asks for)
    n_samples    - how many samples actually produced a usable verdict
    method       - "llm" | "unavailable"
    samples      - the raw per-sample verdicts (audit)
    """
    score: Optional[float]
    per_criterion: dict[str, dict]
    agreement: float
    n_samples: int
    method: str
    samples: list[SampleVerdict] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Ready-made criteria for the two `rubertlite` jobs (§21.7) — general scorer, concrete presets
# --------------------------------------------------------------------------- #

def lesson_overgeneralization_criteria() -> list[Criterion]:
    """D6 lesson guard (§21.7): a HIGH score = the lesson OVER-generalizes (a single failed
    implementation was distilled into a whole-direction prohibition — the node_63 mis-lesson)."""
    return [
        Criterion("over_generalizes",
                  "Does this lesson generalize from ONE failed implementation to a whole research "
                  "DIRECTION (rather than staying scoped to the specific setup that failed)?", 1.0),
        Criterion("direction_sound",
                  "Is the underlying DIRECTION the lesson warns against actually sound / worth keeping "
                  "open (i.e. the failure was about the implementation, not the direction)?", 1.0),
    ]


def reexamination_criteria() -> list[Criterion]:
    """D3 failed-direction re-examination (§21.4): a HIGH `implementation_bound` score = the failure was
    the IMPLEMENTATION's fault and the direction should be re-opened (the node_63 archetype)."""
    return [
        Criterion("implementation_bound",
                  "Did this experiment fail because of its specific IMPLEMENTATION (a bug, a bad "
                  "loss-side hack, wrong hyperparameters) rather than the DIRECTION being unsound?", 1.0),
        Criterion("reexamine",
                  "Given the evidence, is the direction promising enough to RE-EXAMINE with a different "
                  "implementation (re-research + retry), as opposed to abandoning it?", 1.0),
    ]


# --------------------------------------------------------------------------- #
# The scorer
# --------------------------------------------------------------------------- #

_SYSTEM = (
    "You are a strict, calibrated research verifier. You judge a CLAIM against the EVIDENCE provided — "
    "not whether it sounds plausible. The judgment is decomposed into numbered CRITERIA; grade EACH one "
    "independently on this ordinal scale: strong_no, no, unclear, yes, strong_yes. Ground every grade in "
    "the evidence; when the evidence is insufficient, answer `unclear` (do not guess). Call `emit` "
    "exactly once with `verdicts` (one scale value per criterion, in order) and `rationales` (one short "
    "evidence-cited reason per criterion, in order)."
)


def _prompt(subject: str, evidence: str, criteria: list[Criterion]) -> list[dict]:
    crit_lines = "\n".join(f"CRITERION {i}: {c.question}" for i, c in enumerate(criteria, start=1))
    user = (f"CLAIM / SUBJECT:\n{subject.strip()}\n\n"
            f"EVIDENCE:\n{(evidence or '(no evidence supplied)').strip()}\n\n"
            f"CRITERIA (grade each in order):\n{crit_lines}")
    return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]


def verify(subject: str, evidence: str, criteria: list[Criterion], *, client=None,
           samples: int = 3, parser: str = "tool_call", tools=None) -> VerdictReport:
    """Grounded + repeated + criteria-decomposed advisory scoring. Returns a `VerdictReport`.

    subject   - the claim/lesson/direction under judgment.
    evidence  - the checkable, grounded context (node outcomes, D1 brief, §17 distance-from-seed…).
    criteria  - the decomposed sub-questions; a HIGH per-criterion score means the question is `yes`.
    client    - an LLMClient (complete_tool/complete_text). None -> method="unavailable", score=None.
    samples   - repeated evaluations; the score is their mean (the sampling-based §12 expectation).
    tools     - optional read-only run tools; when given, the judge READS the run before grading
                (agentic, mirrors verify_memo). None -> plain structured parse.

    Best-effort: a sample that fails to parse is dropped; if NONE parse the report is `method="llm"`
    with `n_samples=0` and `score=None`. Never raises on a model/endpoint failure."""
    if client is None or not criteria:
        return VerdictReport(score=None, per_criterion={}, agreement=0.0, n_samples=0,
                             method="unavailable")

    from pydantic import BaseModel, Field

    from looplab.core.parse import parse_structured

    class _Verdicts(BaseModel):
        verdicts: list[str] = Field(default_factory=list)
        rationales: list[str] = Field(default_factory=list)

    msgs = _prompt(subject, evidence, criteria)

    def _one_sample() -> Optional[_Verdicts]:
        try:
            if tools is not None:
                # Grounded/agentic variant: read the run, then emit (mirrors verify_memo exactly).
                from looplab.agents.agent import agentic_struct
                return agentic_struct(
                    client, tools, msgs, _Verdicts, parser=parser, loop_opts={"max_turns": 15},
                    fallback=lambda m: parse_structured(client, m, _Verdicts, parser))
            return parse_structured(client, msgs, _Verdicts, parser)
        except Exception:  # noqa: BLE001 — a bad sample is dropped, never crashes the verifier
            return None

    per_scores: dict[str, list[float]] = {c.key: [] for c in criteria}
    per_verdicts: dict[str, list[str]] = {c.key: [] for c in criteria}
    raw_samples: list[SampleVerdict] = []
    for _ in range(max(1, samples)):
        out = _one_sample()
        if out is None:
            continue
        s_scores: dict[str, float] = {}
        s_rats: dict[str, str] = {}
        for i, c in enumerate(criteria):
            raw = out.verdicts[i] if i < len(out.verdicts) else None
            val = _verdict_to_score(raw)
            if val is None:
                continue
            per_scores[c.key].append(val)
            per_verdicts[c.key].append(_nearest_label(val))
            s_scores[c.key] = val
            if i < len(out.rationales):
                s_rats[c.key] = str(out.rationales[i])[:200]
        if s_scores:
            raw_samples.append(SampleVerdict(scores=s_scores, rationales=s_rats))

    per_criterion: dict[str, dict] = {}
    for c in criteria:
        vals = per_scores[c.key]
        per_criterion[c.key] = {
            "mean": round(mean(vals), 4) if vals else None,
            "std": round(pstdev(vals), 4) if len(vals) > 1 else 0.0,
            "n": len(vals),
            "scores": list(vals),
        }

    # Overall = weight-averaged per-criterion mean over the criteria that got any usable sample.
    scored = [(c, per_criterion[c.key]["mean"]) for c in criteria
              if per_criterion[c.key]["mean"] is not None]
    if scored:
        wsum = sum(c.weight for c, _ in scored) or 1.0
        overall = round(sum(m * c.weight for c, m in scored) / wsum, 4)
    else:
        overall = None

    agreement = _agreement(per_verdicts)
    # n_samples = samples that produced >=1 usable verdict (what the field documents). Using the MAX
    # per-criterion count would under-report when different samples graded disjoint criteria.
    return VerdictReport(score=overall, per_criterion=per_criterion, agreement=agreement,
                         n_samples=len(raw_samples), method="llm", samples=raw_samples)


def _nearest_label(val: float) -> str:
    """The ordinal label closest to a score — so a numeric-emitting model still contributes to the
    modal-agreement measure on the SAME 5-level grid the ordinal graders use."""
    return min(_VERDICT_ORDER, key=lambda lab: abs(_VERDICT_VALUE[lab] - val))


def _agreement(per_verdicts: dict[str, list[str]]) -> float:
    """Cross-sample stability in [0,1]: for each criterion the fraction of samples that agreed with the
    MODAL verdict, averaged across criteria. 1.0 = perfectly stable; low = the single-shot-variance the
    §21.12 measurement flagged. 0.0 when there is <=1 sample (no stability to measure)."""
    fracs: list[float] = []
    for labels in per_verdicts.values():
        if len(labels) <= 1:
            continue
        top = max(set(labels), key=labels.count)
        fracs.append(labels.count(top) / len(labels))
    return round(mean(fracs), 4) if fracs else 0.0


# --------------------------------------------------------------------------- #
# Calibration harness (§12's own evaluation gate — "calibrate on the run's labelled cases")
# --------------------------------------------------------------------------- #

@dataclass
class LabelledCase:
    """A gold-labelled verification case for calibration. `gold` is the ground-truth value the primary
    criterion's score should track — a float in [0,1] (or 0/1). `criterion_key` names which criterion's
    score to compare (defaults to the first criterion)."""
    subject: str
    evidence: str
    criteria: list[Criterion]
    gold: float
    criterion_key: Optional[str] = None
    name: str = ""


def calibrate(cases: list[LabelledCase], *, client=None, samples: int = 3,
              parser: str = "tool_call",
              verify_fn: Optional[Callable] = None) -> dict:
    """Run the verifier over labelled cases and measure whether its score TRACKS the gold outcome — the
    §12 evaluation gate that must pass before the score is allowed to steer. Returns:

      n           - cases scored (those that produced a usable score)
      pearson     - Pearson correlation of verifier-score vs gold (None when <2 varied points)
      best_threshold, accuracy - the score cutoff maximising binary-label accuracy (gold>=0.5) and that
                    accuracy; the operating point a live gate would adopt
      mean_agreement - average cross-sample agreement (low => still too high-variance to steer)
      rows        - per-case {name, gold, score, agreement} for inspection

    Pure orchestration around `verify` (injectable via `verify_fn` for tests). No I/O of its own."""
    vf = verify_fn or verify
    rows: list[dict] = []
    for i, case in enumerate(cases):
        rep = vf(case.subject, case.evidence, case.criteria, client=client, samples=samples,
                 parser=parser)
        key = case.criterion_key or (case.criteria[0].key if case.criteria else None)
        score = None
        if key and key in (rep.per_criterion or {}):
            score = rep.per_criterion[key]["mean"]
        elif rep.score is not None:
            score = rep.score
        rows.append({"name": case.name or f"case-{i}", "gold": float(case.gold),
                     "score": score, "agreement": rep.agreement})

    scored = [r for r in rows if r["score"] is not None]
    golds = [r["gold"] for r in scored]
    preds = [r["score"] for r in scored]
    pearson = _pearson(preds, golds)
    thr, acc = _best_threshold(preds, golds)
    agrees = [r["agreement"] for r in scored if r["agreement"] is not None]
    return {
        "n": len(scored),
        "pearson": pearson,
        "best_threshold": thr,
        "accuracy": acc,
        "mean_agreement": round(mean(agrees), 4) if agrees else 0.0,
        "rows": rows,
    }


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    """Pearson correlation; None when fewer than 2 points or either side has zero variance (undefined —
    exactly the `foresight confidence ↔ outcome` degenerate case §21.12 had to report honestly)."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = mean(xs), mean(ys)
    sx = sum((x - mx) ** 2 for x in xs)
    sy = sum((y - my) ** 2 for y in ys)
    if sx <= 0 or sy <= 0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return round(cov / (sx ** 0.5 * sy ** 0.5), 4)


def _best_threshold(preds: list[float], golds: list[float]) -> tuple[Optional[float], float]:
    """The score cutoff maximising accuracy of `score >= t` predicting `gold >= 0.5`. Ties break toward
    the lower threshold (more inclusive). Returns (threshold, accuracy); `threshold is None` means either
    nothing to score OR the best operating point predicts NOTHING positive (gold skewed negative) — the
    accuracy distinguishes the two (0.0 vs a real value). The +inf candidate makes that all-negative
    operating point representable, which `sorted(set(preds))` alone cannot (its max still predicts the
    top point positive)."""
    if not preds:
        return None, 0.0
    labels = [1 if g >= 0.5 else 0 for g in golds]
    # candidate thresholds = distinct predicted scores (predict positive at p>=t), PLUS +inf = predict
    # nothing positive (the all-negative operating point).
    cands = sorted(set(preds)) + [float("inf")]
    best_t, best_acc = cands[0], -1.0
    for t in cands:
        correct = sum(1 for p, y in zip(preds, labels) if (1 if p >= t else 0) == y)
        acc = correct / len(preds)
        if acc > best_acc:
            best_t, best_acc = t, acc
    thr = None if best_t == float("inf") else round(best_t, 4)
    return thr, round(best_acc, 4)
