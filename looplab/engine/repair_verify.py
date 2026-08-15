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

RE-MEASURED 2026-08-15 over the WHOLE of `runs/`, because two of those figures no longer reproduce
and one of them never meant what it looks like. **v4's event log is gone from the box** (v5's already
was), so the six-run list above cannot be re-derived at all; over the five that survive the totals
are 2,444 rows / 101 model-authored / 92 naming something concrete, and over every run directory in
the tree — the five plus v7, v8 and the small offline runs — 2,480 / 137 / 125. The 2,343 boilerplate
count is EXACT and unchanged. The 13 inert are 11 today (v4 held two or three of them; v7 node 1
attempt 3 is a new one), and the surviving named cases are intact: rubertlite-dense-retrieval node 57
still shows three consecutive empty change sets.

**"38 named a concrete change that appears nowhere in the diff" is not the count of `unmet` verdicts
and never was**, which is the figure that matters and the one to quote from here on. It counts rows
with AT LEAST ONE absent token; `verify_repair` returns `unmet` only when EVERY claim is absent, so
one met token acquits the row. Both numbers, replayed with the shipped extractor: reading the fullest
text recoverable for each row, the five-run corpus has 38 rows with an absent token and **7** `unmet`
verdicts; the whole tree on the 300-char durable text has 73 and **17**. Discounting the two rows the
intake fix already turns `verified` and the one `salvage_cause_fix` marker row (which the shipped
code never scores — it writes no `verified` key at all), **14 `unmet` verdicts stand across every
repair this box has ever made.** That is the denominator the two demotions below are argued against.

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
the only half this rung exists to check. Re-measured over the whole of `runs/` on 2026-08-15: 96 of
the 137 model-authored rationales are stored at exactly that cap — the MEDIAN rationale this rung
read was truncated — and over the 82 repairs whose full text could be recovered from `spans.jsonl`
and replayed, 2 of the 9 `unmet`s and 2 of the 8 `unstated`s are `verified` on the text the model
actually wrote, with no verdict ever moving the other way. `engine/triage.py::TRIAGE_RATIONALE_CAP`
is now that intake bound and is deliberately far above every sink's; a sink capping its own column is
fine and always was. IT LIVES IN `triage.py` BECAUSE THE INTAKE WAS NOT THE FIRST CAP: `triage_crash`
is a duck-typed seam with exactly one implementation in the tree, `agents/unified_agent.py`, which is
the shipped default and whose emit finalizer already cut the same string to 300 on its way out — so
raising the bound on what the seam RETURNED changed nothing at all for a day. Both layers now read
that single constant.

RECOVERING THAT TEXT IS ITSELF A JOIN AND THE OBVIOUS KEY IS WRONG. The full rationale lives in the
`triage_crash` tool call of a `generation` span under the attempt's `triage` span, but `(node_id,
attempt)` is NOT unique across a resume — `rubertlite-dr-unified-v2` reuses attempt ordinals, so an
unvalidated join silently pairs one repair's rationale with another repair's diff and manufactures
`unmet` rows that never happened. Any replay of this measurement must check that the durable 300-char
row is a PREFIX of the span text before trusting the pair; doing so drops 10 of the 92 apparent
recoveries.

AND A SHIPPED FIX IS NOT A LIVE FIX. The finalizer bound landed 2026-08-14 22:33 UTC; the engine
process behind `rubertlite-dr-unified-v8` started at 16:25 that day and a running interpreter does
not reload its source. Every verdict v7 and v8 have produced was therefore computed on a 300-char
PREFIX, and it is visible in the log: v7 node 0 attempt 2 is stamped `unmet ['nll_cos']`, which is
exactly what the prefix yields and which the full 690-char text turns `verified`. A verdict on a
durable row carries no record of the cap that was in force when it was written, and adding one would
be a new column for a transient condition — so the rule is simply that `verified`/`unmet` on a row
written before a restart on 2026-08-15 is a verdict about a prefix. It is also why the corpus
measurement above quotes BOTH bases rather than one.

A CITED NAME AND A CLAIMED NAME ARE THE SAME TOKEN, and no extractor over model-authored text will
reliably tell them apart. "vs the working nll_cos runs" names a baseline being compared AGAINST; "I
removed the nll_cos path" names a change being promised — identical to this module, which sees one
token and one diff. A rationale whose ONLY concrete token is a citation scores `unmet` where the
honest answer is `unstated`.

THAT RESIDUAL WAS LEFT OPEN ON THREE GROUNDS AND ONE OF THEM WAS WRONG, so the demotion below now
exists. The 2026-08-13 text said it was RARE — "ZERO of the four surviving `unmet` verdicts is a
citation artifact" — and that sentence is FALSIFIED. It was a statement about the four rows whose
full text could be recovered from the two runs that had any, and the rung had never been run live.
Re-measured 2026-08-15 over every `node_repaired` in `runs/` (see the corpus figures above), reading
the fullest text available for each row: of the 14 surviving `unmet` verdicts, **4 convict on a token
the rationale only ever used as evidence** — and they split into two sub-shapes, only one of which is
patched here. ONE cites a SIBLING: `rubertlite-dr-unified-v8` node 3 attempt 2, whose `mining_type` /
`n_negatives` are both inside "Node 1's identical mining config (…) already passed and reached
0.7384". THREE cite the CRASH — `rubertlite-dense-retrieval` node 32 attempt 1 (`pos_scores_broadcast`
/ `s_dd_local`, the two shapes in the mismatch message) and `rubertlite-dr-unified-v2` node 1 attempt
2 and node 3 attempt 1 — where the token names the broken thing rather than another experiment. Only
the sibling shape is demoted, and that is a decision and not an oversight: "the crash is in X" sits on
a continuum with the dense-retrieval node 11 family below, where the repair then edited a DIFFERENT
file and saying so is the rung working. A rule wide enough to catch all four would demote those too.
On the two runs the shipped rung actually GRADED (v7 + v8, 4 `unmet` rows) it was right ONCE. A signal
the judge reads that is wrong three times in four does not cost "some precision"; it teaches the
reader to discount the line, which is the whole value of the rung.

The other two grounds still hold and they SHAPE the fix rather than block it. It is still INHERENT —
any discriminator is a second regex over the same adversarially-authorable text — so `_CITATION_RE`
is deliberately NARROW: it recognizes only a clause bound to ANOTHER experiment ("node 1's …",
"node-1", "the previous repair"), not the open-ended "vs" / "unlike" / "compared with" phrasing list
the earlier text rightly refused. And it is still CHEAP, which is what makes the narrow rule safe:
because `unmet` stops nothing, a model that writes around the discriminator gains NOTHING — the
demotion target is `unstated`, which is itself reported, so evading conviction buys an "I could not
check this" and never a "this checked out". That is the direction the 2026-08-13 text prescribed and
it is the one taken: demote TO `unstated`, never drop the token, never reach `verified`. A cited
token can still ACQUIT (if the diff contains it the row is `verified` exactly as before); it can no
longer CONVICT alone. Measured over all 137 model-authored rationales in `runs/`, the rule moves two
rows and only two: v8 node 3 attempt 2 `unmet` -> `unstated`, and `rubertlite-dr-unified-v6` node 1
attempt 1 keeps its `unmet` with `train.py` dropped from the reported list (it sits in "node 1 passed
three CLI args the harness's train.py argparse does not define"). No `unmet` became `verified`, no
`inert` moved, and v8 node 3 attempt 1 — the one TRUE positive on the live run, "Fix mine_stage.py …"
against a diff that edited `vectorsearch/data/mine_negatives.py` — is untouched, because its token is
in the next sentence and not in the citation's clause.

A CLAIM EXPRESSED IN AN ABBREVIATION OF THE IDENTIFIER THE CODE USES is the second false-positive
shape, and the earlier text did not name it at all. v8 node 3 attempt 4 said "halve per-step batch to
4096 and raise grad_accum to 4 so effective batch stays 8192"; the diff it produced sets
`config.train.training.batch_size = 4096` and `config.train.training.gradient_accumulation_steps = 4`.
The repair did exactly what it promised and a literal-token extractor convicted it, because the
codebase spells the parameter `gradient_accumulation_steps` and the model wrote the colloquial
`grad_accum`. `_abbreviated_identifier` closes that: an identifier claim is also met when it is a
PART-WISE PREFIX of an identifier the diff actually contains (`grad_accum` -> `gradient_accumulation_
steps`). This one CAN reach `verified`, which is the dangerous direction — a false `verified` is a
claim the record makes on the agent's behalf — so it is bounded three ways and each bound is load-
bearing. It never applies to a FILE claim, because a file path is exactly the thing "different blast
radius" is measured in and `mine_stage.py` must not be met by `mine_stage_helpers.py`. It requires at
least TWO underscore-separated parts, each at least three characters, so a single word can never
abbreviate anything. And the match must be against an identifier PRESENT IN THE DIFF, not against a
vocabulary this module carries. Measured over all 137 model-authored rationales: it moves exactly ONE
row in the corpus, the one above, and `mine_stage` against that same run's `mine_negatives.py` diff
does NOT match it.

WHAT IS STILL OPEN, having been measured rather than assumed. The 14 surviving `unmet` verdicts split
7 / 2 / 5: SEVEN are genuine discrepancies, i.e. the rung working; TWO are withdrawn by the rules
above (v8 node 3 attempts 2 and 4) and one further row keeps its verdict with a shortened list; and
FIVE are shapes left deliberately unpatched. Three of those five are the crash-citation rows named
above. `rubertlite-dr-unified-v6` node 1 attempt 1 is the fourth, a NEGATED claim — "drop those
unsupported args" — satisfied by deleting the `"%params%"` placeholder that passed them, so the args
it named appear nowhere in the diff precisely BECAUSE the promise was kept; a rule for that would
have to understand the indirection, not the text. The fifth is `sim-nosignal` node 5, which names
`IndentationError`, an exception class read as a claim — the `_NOT_A_CLAIM` frontier, not a new
mechanism. Four of the seven genuine ones are also arguable — `rubertlite-dense-retrieval` node 11's family names the
BROKEN component ("the bug is in NegLogLikelihoodCos_S") and then edits a different file, which is a
useful thing to flag but is a diagnosis rather than a promise. They are left `unmet` on purpose: a
claim-clause whitelist would demote all four, and turning a repair that touched the wrong file into
"nothing to check" is the trade this module refuses.

WHY THESE ARE NOT TRIAGE VERDICTS. `engine/triage.py::TRIAGE_ACTIONS` answers "keep repairing this
node?" and is a JUDGEMENT, three-fifths of which a model may emit. These four answer "what did the
repair just do?" and are FACTS the engine derives from bytes it holds — no model may emit one, no
coercion accepts one off the wire, and `coerce_triage_action` must never see one. They are a
separate vocabulary for the same reason `metric_salvage.SALVAGE_CAUSE_TRIAGE_ACTION` is deliberately
absent from `TRIAGE_ACTIONS`: mixing a marker into a verdict enum breaks the emit schema in one
direction and makes a reader treat the enum as exhaustive in the other. The two tuples are
cross-referenced rather than merged; `tests/test_repair_verification.py` drives both rules.

--------------------------------------------------------------------------------------------------
`declared_param_overrides` — A THIRD RUNG, ASKING A DIFFERENT QUESTION, AND IT NEVER READS THE
RATIONALE AT ALL.

`verify_repair` asks *did the repair do what it SAID*. This asks *does the code the engine committed
still agree with the parameters the record DECLARES* — and both inputs are things the engine holds:
`Idea.params`, minted into `node_created` by the Researcher (never by a repair), and the `.py` bytes
of `node_repaired.files`. So it sits in `REPAIR_INERT`'s trust tier, not `REPAIR_UNMET`'s: no wording
evades it, none summons it, and a model that writes a more persuasive rationale changes nothing here.

**THE INCIDENT, on the live run's CHAMPION.** `rubertlite-dr-unified-v8` node 3 (R-Drop α=0.5 on
node 1's DCL loss) became the run's best at **0.762048**, +0.0236 over the next node while the other
four evaluated nodes sit inside a 0.0017 spread. Three records of that experiment disagree:
`node_created.idea.params` says `train.training.batch_size 8192` / `gradient_accumulation_steps 2`;
the node's own `vectorsearch/configs/config.yaml` says 8192 / 2; and `vectorsearch/train.py` lines
31-32 — written by attempt 4's repair, after `config = Config()` — say **4096 / 4**. The training log
confirms which one ran (`"batch_size": 4096, "gradient_accumulation_steps": 4` in the config dump the
script itself prints). The SCIENCE is fine and the repair said so: 8192x2 and 4096x4 are the same
16384 effective batch. The RECORD is not, because `idea.params` are the COORDINATES the search
machinery models the space with, and it is not decorative — measured on that same run, node 8 is a
`search/operators.py::merge_idea` mean-merge of nodes 3 and 1, i.e. an ENGINE-AUTHORED arithmetic
node whose declared `batch_size 8192` / `grad_accum 2` are the mean of two 8192s and two 2s. Had node
3's coordinates been the ones it ran, node 8 would have been minted at 6144 / 3. Same readers for
`core/numeric.py::numeric_params` throughout: `search/surrogate.py`, `search/panel.py`,
`search/proxy.py`, `search/archive.py`'s niches, `engine/novelty.py`'s distance, the champion
notebook `cli/export_cmds.py` exports, and the "Best so far: node N params={…}" line
`agents/roles.py` puts in front of the Researcher.

**AND THE ENGINE CREATED THE INCENTIVE, which is what makes this a defect of the system rather than
a sloppy agent.** The repair's own comment names the reason it did not edit `config.yaml`: *"Config
is pydantic-mutable, so this is a train.py-only change (no config.yaml edit) that leaves the
completed `mine` stage reusable."* `eval_stages.py::_safe_reuse_start` refuses reuse on ANY non-`.py`
change, and node 3's `mine` stage had just cost **2,304 s**; it was then `reused` at 0.0 s on both of
the two attempts that followed. So the reuse rule paid a repair 4,608 seconds of GPU time to move a
comparison-bearing parameter OUT of the declared config and into code no record reads. That rule is
NOT loosened here and must not be — the `mine` stage on this very node reads `config.yaml` (its
`n_negatives` and mining `batch_size` are in the same file), so a config-only reuse exemption would
have scored a stale checkpoint on precisely this node; `_safe_reuse_start`'s own docstring already
refused the `needs`-based version of that widening on 2026-08-14 and the argument is unchanged.
What changes is that the divergence is now SAID OUT LOUD.

WHAT IT CLAIMS, EXACTLY, AND WHY THE WORDING IS NARROWER THAN THE STORY ABOVE. The derivable fact is
*the node's own committed code assigns this declared parameter a different literal* — a statement
about two artifacts. It is deliberately NOT "this is what ran": an assignment behind a dead branch,
or in a helper nobody calls, is indistinguishable to any static reader, and a rung that overclaimed
would be making an assertion on the candidate's behalf. Absence is likewise SILENCE and never a
certificate — the same rule `champion_caveats.py` states for its empty list. Both directions of
error are cheap by construction: a false positive costs a caveat line nobody can act on, a false
negative leaves the record exactly where it was.

THE BOUNDS, each load-bearing:
  * The declared key must carry at least `PARAM_OVERRIDE_MIN_PARTS` dotted parts. A bare `lr` or `x`
    (the toy/benchmark spaces) would be met by any local of that name; a dotted `train.training.
    batch_size` is a path into a config object and English does not produce one by accident.
  * The code target is parsed with `ast`, never a regex — the input IS Python by construction, and
    the whole false-positive family here is comments and string literals, which `ast` does not have.
    A file that does not parse is skipped (an agent may commit anything), never guessed at.
  * The declared parts must be a contiguous SUFFIX of the target's own parts, so
    `config.train.training.batch_size` and `cfg["train"]["training"]["batch_size"]` both match while
    the receiver's name is nobody's business.
  * Both values must be NUMERIC. A computed right-hand side (`batch // 2`, `args.bs`) is not
    comparable and is dropped rather than resolved — constant-folding agent code is a second
    evaluator, and this rung is not one.
"""
from __future__ import annotations

import ast
import difflib
import math
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
#   "unmet"     — the change set is NOT empty, the rationale named concrete things, none of them
#                 occur in what changed, and at least one of those was a PROMISE rather than a
#                 citation of another experiment. Evidence for the judge, never a stop on its own.
#   "unstated"  — the rationale named nothing concrete enough to check — either nothing concrete at
#                 all, or nothing whose absence this rung is willing to hold against the repair
#                 (every unmet token was a citation). Reported rather than silently scored as a
#                 pass, because "I could not check this" and "this checked out" are different facts
#                 and collapsing them is how the rung would rot. It is also the ONLY place the
#                 citation demotion may land: never `verified`, which would be the record making a
#                 claim on the agent's behalf.
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

# --- A token that may CONVICT: the claim/citation discriminator ----------------------------------
# A clause bound to ANOTHER experiment is evidence the rationale reasons FROM, not a promise it
# makes. "Node 1's identical mining config (mining_type=1, n_negatives=2) already passed" names a
# baseline; "Fix mine_stage.py to relax the coverage threshold" names a change. Both are one token
# and one diff to the matcher above, which is why v8 node 3 attempt 2 was convicted for quoting a
# sibling's config correctly.
#
# DELIBERATELY NARROW, and the narrowness is the argument (see the module docstring). This matches a
# reference to another NODE/RUN/ATTEMPT and nothing else — never the open-ended "vs" / "unlike" /
# "compared with" phrasing list, which is a wishlist rather than a rule and would drop the real claim
# in "unlike the old nll_cos path, which I deleted". The window it opens runs to the end of the
# CLAUSE, not the sentence: v8 node 3 attempt 1 cites node 1 and then promises `mine_stage.py` one
# sentence later, and that row is the true positive this whole rung exists for.
_CITATION_RE = re.compile(
    r"\b(?:node|experiment|exp|run|attempt|sibling|trial)\b[\s#\-]*\d+"
    r"|\bnode-\d+"
    r"|\bthe\s+(?:previous|prior|last|earlier)\s+(?:repair|attempt|fix|change|run|node)\b",
    re.IGNORECASE)
_CLAUSE_ENDS = ";.\n"

# --- An abbreviated identifier -------------------------------------------------------------------
# `grad_accum` for `gradient_accumulation_steps`. Both bounds are load-bearing and both exist to keep
# this from reaching `verified` on a coincidence: two parts means a single word can never abbreviate
# anything (`accum` alone would match half the training loop), and three characters per part means
# `a_b` cannot match `alpha_beta`. The candidate set is the identifiers the DIFF actually contains,
# never a vocabulary carried here — a claim is met by what the repair did, or it is not met.
_ABBREV_MIN_PARTS = 2
_ABBREV_MIN_PART_CHARS = 3


@dataclass(frozen=True)
class RepairVerification:
    """What the engine can say about a repair without asking anybody.

    `verdict` is a member of `REPAIR_VERDICTS`; `claims` is every concrete token the rationale
    named; `unmet` is the subset the change set does not contain AND which the rationale used as a
    promise rather than a citation — the CONVICTING subset, which is what the judge is shown. It is
    therefore a subset of "claims not in the diff" and not equal to it: a token the rationale only
    ever used to cite another node is kept in `claims` (nothing is dropped) and left out of `unmet`.
    `unmet` is non-empty only for `REPAIR_UNMET` — for `REPAIR_INERT` it is deliberately left EMPTY
    even though nothing was met, because that verdict is a statement about bytes and attaching a
    text-derived list to it would invite a reader to treat the two tiers as one.
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


def region_identifiers(region: str) -> set:
    """Every identifier the diff contains — the abbreviation matcher's whole search space.

    HOISTED OUT of `_abbreviated_identifier`, which rebuilt it with `set(_IDENT_RE.findall(region))`
    once PER CLAIMED TOKEN. `changed_region` is capped at 40 KB, `claimed_tokens` is uncapped (a
    2,000-char rationale — `triage.TRIAGE_RATIONALE_CAP` — yields tens to low hundreds of tokens),
    and one corpus node recorded 2,345 repairs, so the scan was quadratic in exactly the two numbers
    that grow together. It is a pure function of the region, so computing it once per `verify_repair`
    is the same answer for every token.
    """
    return set(_IDENT_RE.findall(region or ""))


def _abbreviated_identifier(token: str, region: str, identifiers=None):
    """The identifier IN THE DIFF that `token` is an abbreviation of, or None.

    `identifiers` is `region_identifiers(region)` when the caller already has it; `None` derives it,
    which keeps this callable on its own (the truth-table tests do exactly that).

    Pure and total. Part-wise PREFIX matching in order: `grad_accum` -> `gradient_accumulation_steps`
    because "gradient".startswith("grad") and "accumulation".startswith("accum"). The diff's
    identifier may carry EXTRA trailing parts (the codebase is more explicit than the prose) but
    never fewer — `pos_scores_broadcast` is not abbreviated by `pos_scores`, it is a longer name, and
    treating the two as one would let a claim about a variable be met by a different variable.

    NEVER call this for a file claim. A path is exactly the axis "different blast radius" is measured
    on: `mine_stage.py` met by `mine_stage_helpers.py` would erase the true positive this rung was
    built to catch. `_claim_met` enforces that ordering — and that guard is DEFENSIVE rather than
    load-bearing today, which is worth knowing before anyone "simplifies" either regex: `_FILE_RE`
    always requires an extension so every file claim carries a `.`, and `_IDENT_RE` never produces a
    token containing one, so no candidate part could start with `stage.py` anyway. Widening either
    regex is what would make the ordering start mattering; `tests/test_repair_verification.py` checks
    that derivation rather than asserting the ordering it cannot currently observe.

    The bound that IS load-bearing is the one below: an abbreviation must actually SHORTEN a part.
    """
    parts = (token or "").split("_")
    if len(parts) < _ABBREV_MIN_PARTS or any(len(p) < _ABBREV_MIN_PART_CHARS for p in parts):
        return None
    for cand in (region_identifiers(region) if identifiers is None else identifiers):
        if cand == token:
            continue  # an exact hit is `_claim_met`'s own answer, already given
        cand_parts = cand.split("_")
        if len(cand_parts) < len(parts):
            continue
        pairs = list(zip(parts, cand_parts))
        if not all(c.startswith(p) for p, c in pairs):
            continue
        # AND IT MUST ACTUALLY ABBREVIATE SOMETHING. Without this, every part matching EXACTLY is
        # also a "prefix match", so `mine_stage` would be met by a `mine_stage_helper` in the diff —
        # and since `_IDENT_RE` extracts `mine_stage` from the file claim `mine_stage.py`, that is a
        # back door around the file rule the line above is written to protect: the twin identifier
        # gets met, one met claim acquits the row, and the live true positive silently becomes
        # `verified`. A claim that is a whole-part PREFIX of a longer name is a DIFFERENT identifier
        # (`pos_scores` is not `pos_scores_broadcast`); an abbreviation shortens a part.
        if any(len(p) < len(c) for p, c in pairs):
            return cand
    return None


def _claim_met(token: str, changed_paths, region: str, identifiers=None) -> bool:
    """A file claim is met by the CHANGE SET (a path, not text); everything else by the region."""
    if _FILE_RE.fullmatch(token):
        base = token.rsplit("/", 1)[-1]
        for p in changed_paths or ():
            p = str(p)
            if p == token or p.endswith("/" + token) or p.rsplit("/", 1)[-1] == base:
                return True
        # A file the repair named but did not touch can still be met by the region: a stage manifest
        # that now RUNS `looplab_eval.py` mentions it without that file being in the change set.
        # And a file claim STOPS HERE — no abbreviation, ever. See `_abbreviated_identifier`.
        return token in (region or "")
    if token in (region or ""):
        return True
    # THE CODEBASE'S SPELLING, NOT THE PROSE'S. Last, so it can only ever turn a miss into a hit and
    # never change an exact match's answer.
    return _abbreviated_identifier(token, region, identifiers) is not None


def _citation_clauses(text: str) -> list:
    """Half-open spans of `text` that are talking about ANOTHER experiment."""
    out = []
    for m in _CITATION_RE.finditer(text or ""):
        end = len(text)
        for k in range(m.end(), len(text)):
            if text[k] in _CLAUSE_ENDS:
                end = k
                break
        out.append((m.start(), end))
    return out


def _is_citation_only(token: str, rationale: str, clauses=None) -> bool:
    """Does EVERY occurrence of this token sit inside a clause about another experiment?

    `clauses` is `_citation_clauses(rationale)` when the caller already has it — same hoist, same
    reason, as `region_identifiers` above: this ran once per UNMET token over the whole rationale.

    Every, not any: "node 1 used nll_cos; I deleted the nll_cos path" promises something, and one
    citation of a name must not buy amnesty for a real claim about it elsewhere in the same text.
    """
    text = rationale if isinstance(rationale, str) else ""
    clauses = _citation_clauses(text) if clauses is None else clauses
    if not clauses:
        return False
    hits = [m.start() for m in re.finditer(re.escape(token), text)]
    return bool(hits) and all(any(a <= h < b for a, b in clauses) for h in hits)


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
    # ONE derivation each, shared by every token. Both were per-token scans of an input that does not
    # change across the loop — see `region_identifiers` for the two numbers that made that quadratic.
    identifiers = region_identifiers(region)
    unmet = tuple(t for t in claims if not _claim_met(t, changed_paths, region, identifiers))
    if len(unmet) < len(claims):
        return RepairVerification(REPAIR_VERIFIED, claims)
    # NOTHING WAS MET — but a token the rationale only ever used to cite ANOTHER experiment is not a
    # promise this repair broke, and convicting on one is how the signal loses the judge (see the
    # module docstring's 2026-08-15 re-measurement). A citation may ACQUIT, above, where the diff
    # really contains it; it may not CONVICT here. Demoting rather than dropping is the point: the
    # token stays in `claims`, and `unstated` says "I could not check this", which is a fact the
    # judge is already shown and which a model gains nothing by steering into.
    clauses = _citation_clauses(rationale if isinstance(rationale, str) else "")
    convicting = tuple(t for t in unmet if not _is_citation_only(t, rationale, clauses))
    if not convicting:
        return RepairVerification(REPAIR_UNSTATED, claims)
    return RepairVerification(REPAIR_UNMET, claims, convicting)


# --- The declared-parameter override rung (see the module docstring) -----------------------------
# A declared key must be DOTTED to be checkable. `train.training.batch_size` is a path into a config
# object; `lr` is a word, and a rung that convicted on a bare word would fire on any local of that
# name in any file. Same shape and same reason as `_ABBREV_MIN_PARTS` above.
PARAM_OVERRIDE_MIN_PARTS = 2

# How many overrides a single row may carry. A durable event column, so it is bounded like every
# other one; the cap can only ever UNDER-report, which is the direction this rung fails in anyway.
PARAM_OVERRIDE_CAP = 12

# Total source bytes `declared_param_overrides` will parse for one node. A repaired working set is
# whatever the Developer wrote and this runs inside the attempt loop; over the bound the remaining
# files are skipped, which under-reports and never mis-reports.
_PARAM_SOURCE_CAP = 2_000_000

# The one name the whole-file solution artifact is reported by — the same one `changed_region` uses,
# so a reader meets one spelling for the source that has no path.
_WHOLE_FILE = "<whole-file solution>"


@dataclass(frozen=True)
class ParamOverride:
    """One declared `Idea.params` key whose committed code assigns it a DIFFERENT numeric literal.

    `param` is the declared key verbatim (the coordinate name the search machinery uses), `declared`
    the value `node_created` recorded, `code` the literal the assignment carries, `path` the file it
    is in and `line` its line. Frozen and JSON-shaped: `as_row` is what rides on the durable event.
    """
    param: str
    declared: float
    code: float
    path: str
    line: int

    def as_row(self) -> dict:
        return {"param": self.param, "declared": self.declared, "code": self.code,
                "file": self.path, "line": self.line}


def _numeric_literal(node):
    """The float value of a numeric literal AST node (`4096`, `-1`, `0.5`), else None.

    `ast.UnaryOp(USub)` is spelled out because a negative literal is not one node in Python's
    grammar. Anything with a NAME or a CALL in it is not a literal and is not resolved — see the
    docstring's fourth bound. Bools are excluded: `True` is `isinstance(int)` and comparing it to a
    declared `1.0` would report agreement nobody wrote."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _numeric_literal(node.operand)
        return None if inner is None else -inner
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        val = float(node.value)
        # `1e400` parses to `inf` and a huge int overflows the conversion; either would ride onto a
        # durable event as a bare `Infinity`, which is not JSON. Same rule as the declared side.
        return val if math.isfinite(val) else None
    return None


def _assignment_target_parts(node):
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


def _assigned_numeric_paths(source: str) -> dict:
    """`(dotted path) -> (value, line)` for every numeric-literal assignment in one Python source.

    LAST WRITE WINS on a repeated path, matching what the interpreter would do if both ran in order —
    and if they are in exclusive branches the rung is over-reading either way, which is the residual
    the docstring states rather than guesses at. Unparseable source answers `{}`: an agent may commit
    anything, and a `SyntaxError` is not evidence about a parameter.

    Walks `Assign` and `AnnAssign` (`config.train.batch_size: int = 4096`) and deliberately NOT
    `AugAssign`: `x += 1` carries no absolute value to compare a declaration against.
    """
    try:
        tree = ast.parse(source or "")
    except (SyntaxError, ValueError, RecursionError, MemoryError):  # noqa: BLE001 — not evidence
        return {}
    out: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        val = _numeric_literal(value)
        if val is None:
            continue
        for tgt in targets:
            parts = _assignment_target_parts(tgt)
            if parts:
                out[tuple(parts)] = (val, getattr(node, "lineno", 0))
    return out


def declared_param_overrides(params, files, *, code: str = "", baseline_files=None,
                             baseline_code: str = ""):
    """The declared `Idea.params` keys this working set's own `.py` code assigns a DIFFERENT number.

    Pure, total and deterministic over two things the engine holds — the Researcher's declaration and
    the bytes the engine committed. It never reads a rationale, so no agent text can summon or evade
    a row; see the module docstring for the incident, the bounds and what the claim is NOT.

    `files` is a `{path: content}` map (`node.files` / `node_repaired.files`); `code` is the
    whole-file solution artifact when there is one, reported under the same `<whole-file solution>`
    name `changed_region` uses. `baseline_files` optionally narrows the answer to overrides THIS
    repair INTRODUCED: a path already assigned the same value in the baseline is not this repair's
    doing, and the attribution matters because a divergence a repair created and one the Developer
    authored at build time are different facts about the record. Omit BOTH for the whole-node
    question — handing over either one is what turns the attribution on.
    `baseline_code` is that same baseline for the whole-file artifact and is NOT optional when
    `baseline_files` is given on a code-artifact task: the artifact has no path, so without it the
    attribution has no prior to compare against and every later attempt re-reports an override the
    FIRST one introduced — the judge's history would then accuse each attempt of a line none of them
    wrote.

    Ordered by declared key, then file, then line, so the same inputs always produce the same list
    (a durable event column and a projection both read this — a set-ordered answer would make an
    unchanged run look changed to every client diffing it).
    """
    declared: dict = {}
    for key, value in (params or {}).items():
        if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        # NON-FINITE IS DROPPED ON BOTH SIDES, and it is not fastidiousness. A NaN/inf param is
        # REACHABLE — `search/archive.py` carries the same guard and names the routes (a `1e309`
        # literal JSON-folds straight to `inf`; NaN is agent-supplied) — and it would break this rung
        # twice over. `nan != anything` is True, so every declaration carrying one would report a
        # divergence against any assignment at all; and the value then rides on a DURABLE event,
        # where Python's `json.dumps` writes a bare `NaN`/`Infinity` that is not JSON and that every
        # browser reader of the log fails to parse. Silence is the right answer for a coordinate that
        # cannot be compared.
        if not math.isfinite(float(value)):
            continue
        parts = tuple(p for p in key.split(".") if p)
        if len(parts) < PARAM_OVERRIDE_MIN_PARTS:
            continue                          # a bare name is a word, not a path (see the constant)
        declared[key] = (parts, float(value))
    if not declared:
        return ()

    # A SOUND PRE-FILTER, and it is a cost decision rather than a rule. An assignment reaching
    # `train.training.batch_size` MUST contain the literal `batch_size` — as an attribute, or as the
    # string constant of a subscript key — so a file without it cannot produce a row and skipping it
    # cannot change the answer.
    #
    # MEASURED, AND IT BUYS NOTHING ON THE CASE THIS WAS BUILT FOR, which is worth saying rather than
    # implying: all five `.py` files in the v8 champion's 112 KB working set match one of the fifteen
    # declared tails (`config.py` on `temperature`/`learning_rate`, `mine_negatives.py` on
    # `n_negatives`/`batch_size`, `loss.py` on `rdrop_alpha`, …), so nothing is skipped and the whole
    # call is 15.5 ms — 30.4 ms with a baseline, which parses the same paths twice. That is the
    # budget to know: `champion_metric_caveats` calls the whole-node form on every `/api/runs` poll,
    # and over all 46 preserved runs it totals 17-21 ms, essentially all of it the two repo runs whose
    # champion carries a working set (15-17 ms + 11-14 ms across them; every other run is ~0.01 ms
    # because its champion declares bare names or ships no `.py`). The filter is kept because a
    # substring scan is strictly cheaper than a parse and a working set naming parameters it does not
    # touch is the ordinary case; it is not kept on the strength of a saving it did not produce here.
    tails = {parts[-1] for parts, _ in declared.values()}
    sources: list = []
    budget = _PARAM_SOURCE_CAP
    for path in sorted(files or {}):
        text = (files or {}).get(path)
        if not isinstance(text, str) or not str(path).endswith(".py"):
            continue
        if not any(t in text for t in tails):
            continue
        if budget <= 0:
            break
        budget -= len(text)
        sources.append((str(path), text))
    if isinstance(code, str) and code.strip() and any(t in code for t in tails):
        sources.append((_WHOLE_FILE, code))

    # The baseline is only ever CONSULTED for a path a source above produced, so parse those files
    # and no others (same pre-filter, same soundness argument).
    # ATTRIBUTING AT ALL is decided by whether ANY baseline was handed over, because a code-artifact
    # task has no `files` to pass and would otherwise have no way to ask the narrowed question.
    attribute = baseline_files is not None or bool(baseline_code)
    base_paths: dict = {}
    if attribute:
        _wanted = {p for p, _ in sources}
        for path in sorted(baseline_files or {}):
            text = (baseline_files or {}).get(path)
            if str(path) in _wanted and isinstance(text, str):
                base_paths[str(path)] = _assigned_numeric_paths(text)
        # The whole-file artifact under the SAME synthetic name it is reported by, so the one source
        # that has no path is attributed by the same rule as the ones that do.
        if _WHOLE_FILE in _wanted and isinstance(baseline_code, str):
            base_paths[_WHOLE_FILE] = _assigned_numeric_paths(baseline_code)

    out: list = []
    for path, text in sources:
        assigned = _assigned_numeric_paths(text)
        for target, (val, line) in assigned.items():
            for key, (parts, decl) in declared.items():
                # SUFFIX, not equality: the receiver (`config`, `cfg`, `self.conf`) is the caller's
                # local name and says nothing about which declaration the path reaches.
                if len(target) < len(parts) or tuple(target[-len(parts):]) != parts:
                    continue
                if val == decl:
                    continue                  # the code AGREES with the declaration — nothing to say
                if attribute:
                    prior = base_paths.get(path, {}).get(target)
                    if prior is not None and prior[0] == val:
                        continue              # already there before this repair — not its doing
                out.append(ParamOverride(param=key, declared=decl, code=val,
                                         path=path, line=int(line)))
    out.sort(key=lambda o: (o.param, o.path, o.line))
    return tuple(out[:PARAM_OVERRIDE_CAP])


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
