"""Which log belongs to which eval stage, and the attempt-bounded readers over them.

The eval writes one log per phase into the node workdir — `setup.log` for the dep install,
`<stage>.log` per resolved pipeline stage, `eval.log` on the single-command path — and a verdict is
worth only what the engine knows about WHOSE bytes it was formed from. This module is that
knowledge, derived from the SAME resolved stage list the eval runs: the plan (`eval_log_plan`, with
each stage's declared promise and wall), the kill authority a declared training stage spends
(`training_authority_spent`), the attributed live log (`resolve_stage_log`, `active_training_log`,
`monitor_stage_context`), the attempt boundary (`snapshot_training_logs`, `attempt_byte_floor`), and
every reader that honours it — the live tails (`read_training_tail_raw`, `read_training_tail`), the
log tools' source map (`monitor_log_sources`) and a finished stage's whole trajectory
(`read_stage_trajectory`, `stage_check_trajectory`).

No engine and no model: the gates that act on a role are `engine/monitor_gates.py`'s, and the tool
builders that take an engine stay in `engine/train_monitor.py`. Every reader fails CLOSED — a
boundary it cannot establish yields nothing, never a previous attempt's bytes.

Split out of `engine/train_monitor.py` by review 2026-09-22 (ENG3-13, doc 50 EM-06), with
`engine/loss_trajectory.py` and `engine/monitor_gates.py`. Moved VERBATIM, comments included;
`train_monitor` re-exports every name as the SAME object, so `from looplab.engine.train_monitor
import eval_log_plan` keeps working — but a patch aimed at `train_monitor` does not reach a call
made in here (`tests/test_train_monitor_split.py`).
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from looplab.engine.loss_trajectory import (
    LossTrajectory,
    summarize_loss_window,
    summarize_trajectory,
    training_log_digest,
)
from looplab.engine.monitor_gates import _NON_TRAINING_ROLES
# The log-role vocabulary lives in `events/types.py`, where readers below the engine can name a role
# (see the note at `train_monitor`'s own import of it).
from looplab.events.types import (
    LOG_ROLE_AMBIGUOUS,
    LOG_ROLE_SCORE,
    LOG_ROLE_SETUP,
    LOG_ROLE_TRAINING,
    LOG_ROLE_UNKNOWN,
    LOG_ROLE_WORK,
)
# The MANIFEST vocabulary (what a stage may declare) is defined once beside the validator that is
# the single definition of a valid stage; this module maps it to the LOG-ROLE vocabulary above.
# Two names deliberately, not one shared constant: the manifest key is a contract with the agent
# and the operator, the log role is a contract with every reader of the durable alert row, and a
# test pins that they still agree. `engine` imports `runtime` throughout, never the reverse.
from looplab.runtime.command_eval import STAGE_ROLE_TRAINING


# ------------------------------------------------------------------ which log belongs to which stage
_SETUP_LOG = "setup.log"
_SINGLE_COMMAND_LOG = "eval.log"
# `score` is RESERVED for the engine-appended protected scoring stage (`engine/eval_stages.py`
# appends it to every Developer manifest, and `command_eval.validate_stages(reserved=("score",))`
# refuses it to the agent). An operator-declared pipeline MAY own the name, and there it means the
# same thing. This is a BACKSTOP, not the primary test — the structural "last stage" rule in
# `_is_scorer_stage` is — and it is spelled to match `validate_stages` EXACTLY: that validator refuses
# the name case-INSENSITIVELY (`nm.lower() in reserved`), so anything it would have refused to the
# agent must read as "scorer" here too. Comparing `name == "score"` instead was live on every platform
# we run (`os.path.normcase` is identity outside Windows): two byte-identical runs differing only in
# capitalisation ended `node_evaluated metric=0.7` and `node_failed monitor_broken` — an operator
# pipeline spelled `Score` handed a scorer's tail to a training-health judge holding kill authority.
_RESERVED_SCORER_NAMES = frozenset({"score"})


def _log_name_key(name: str) -> str:
    """Case-folded log basename, matching `_log_path_key`'s Windows handling."""
    return os.path.normcase(str(name))


def _is_scorer_stage(name: str, *, index: int, total: int) -> bool:
    """Whether a resolved pipeline stage is the SCORER — structurally first, by name only as a backstop.

    STRUCTURAL: `command_eval.run_command`'s staged branch states the contract verbatim — "The LAST
    stage's stdout carries the metric" — and that stage's output is the only one `read_metric` reads.
    It holds for BOTH shapes `_resolve_stages` produces: the engine appends its protected `score`
    stage last to a Developer manifest, and an operator-declared pipeline scores in its own final
    stage. So POSITION, not spelling, is what the engine actually knows. `total == 1` is excluded
    because a one-stage pipeline is the single-command shape wearing a stage name: that one command
    both trains and scores, exactly like `eval.log`.

    NAME: any spelling `command_eval.validate_stages` would have RESERVED (case-folded `score`) also
    reads as the scorer wherever it appears, so a mid-pipeline stage the agent could never have been
    allowed to name cannot acquire training authority by sitting in an operator's list."""
    if total > 1 and index == total - 1:
        return True
    return str(name).strip().lower() in _RESERVED_SCORER_NAMES


@dataclass(frozen=True)
class StageDeclaration:
    """What ONE stage promised about itself, read from the CLEANED manifest the engine resolved.

    Both fields are the candidate's own text. That is the point and also the whole of the trust
    argument: the engine is going to CHECK this promise the moment the stage exits, so showing it to
    the live judge widens no trusted set — it names the bar the stage is already being held to.
    """

    assertion: str = ""
    files: tuple = ()


@dataclass(frozen=True)
class EvalLogPlan:
    """Every log file ONE eval attempt can write, mapped to the stage that writes it and its role.

    Built by the engine from the SAME resolved stage list the eval runs (`_resolved_stages`), so the
    watchdogs stop guessing which phase produced the bytes they are reading.
    """

    roles: dict                  # case-folded basename -> (stage name or None, LOG_ROLE_*)
    stage_names: tuple = ()      # the resolved pipeline order; () for a single-command eval
    # The DECLARED outputs of the stage that declared itself the training loop — the evidence
    # `training_authority_spent` uses to notice that training is already over. () whenever the
    # training role was not bought by a declaration (single command, one-stage pipeline), so that
    # path keeps its behaviour byte-for-byte.
    training_artifacts: tuple = ()
    # stage name -> what that stage PROMISED about itself (`expect.assert` / `expect.files`), for
    # every stage that promised anything. Deliberately NOT the same map as `training_artifacts`:
    # that one is the spend condition for an AUTHORITY and is therefore granted only where the
    # `role: "training"` declaration survived every refusal, while this is EVIDENCE and belongs to
    # whichever stage the tick is actually watching — including a `mine` or a `data_prep` stage,
    # which the engine will fail on its declaration exactly as readily. LAST in the field order
    # because every existing construction is keyword-only and it must stay that way for a
    # positional one too.
    declarations: dict = field(default_factory=dict)
    # stage name -> the WALL that stage was declared with. Carried so a watchdog can compare its own
    # projection against the deadline the stage will actually be held to. Without it the engine can
    # measure that a run needs ten hours and be unable to notice that it has seven — which is what
    # happened: `runs/e5small-dr-unified-v4` node 6 was recorded at 15:45 as "6% of a ~10h run"
    # against a 28000 s wall, and was killed on that wall 7 hours later having burned 7.78 GPU-hours.
    timeouts: dict = field(default_factory=dict)


def eval_log_plan(stages) -> EvalLogPlan:
    """The log plan for a resolved eval pipeline. Pure/deterministic — no I/O — so what the watchdogs
    are allowed to judge is unit-testable without a filesystem.

    `stages` is `Engine._resolved_stages`' output: the ordered pipeline, or `[]`/None for the classic
    single-command eval (whose one command trains AND scores in one process — see the
    `command_eval.py` comment on that branch — so its `eval.log` IS a training log).

    WHICH STAGES MAY KILL. Only `LOG_ROLE_TRAINING` carries kill authority, and this plan grants it
    only where the log is PROVABLY the run's own training:

    - the single-command `eval.log`, and a ONE-stage pipeline (the same shape wearing a stage name):
      that command is the whole eval, so there is nothing else its output could be. `command_eval`
      says as much on that branch — "A single-command RepoTask eval IS the training (train->eval in
      one process, often multi-hour)" — and it is also the only path the runtime leaves WITHOUT its
      own deterministic divergence kill (`health_check=True` is passed for every declared stage and
      omitted here), so the LLM watchdog is that path's only early stop;
    - a stage the MANIFEST declares as the training loop (`role: "training"`, validated by
      `command_eval.validate_stages`, at most one per pipeline, never the positional scorer) AND
      that declares the `expect.files` its authority is spent against — see below;
    - every other pipeline stage is `LOG_ROLE_WORK`: still read, still judged, still alerting — but
      ADVISORY.

    WHY A DECLARATION IS ADMISSIBLE EVIDENCE. Everything below argues that a stage NAME proves
    nothing, and none of that changed — `train` is still just a slug. What the declaration adds is
    not a better guess but a different KIND of fact: the manifest is the same authenticated
    artifact the engine already trusts to say what runs, in what order, with what timeout and what
    output contract, and it can only ever be spent in one direction. Omit `role` and the stage
    keeps precisely the advisory role it has today; write it and the only thing bought is the power
    to have YOUR OWN stage stopped — a kill carries no repair, no retry and no refunded slot, so
    there is no reading under which a declarer profits. Compare the alternative that was rejected:
    admitting `LOG_ROLE_WORK` to the kill set whenever the measured trajectory corroborates. That
    fails on this function's own worked example — the `data_prep` stage printing a flat
    `loss: 0.6931` is exactly a frozen curve, so the corroboration fires hardest on the false
    positive it was meant to filter, and it would promote the deterministic half from VETO to
    CONFIRM, which is a widening of authority docs/36 reserves for evidence the record can
    authenticate.

    That last line is the substantive narrowing, and it is deliberate. The previous rule — "every
    stage whose name is not the exact string `score` is training" — was justified in this docstring by
    a claim about `command_eval` that is false for the staged path: `run_argv` is called with
    `health_check=True` for EVERY declared stage, the appended scorer included, so the runtime draws
    no train/not-train line there at all. Nothing else draws one either: `validate_stages` accepts any
    filesystem-safe slug, the manifest carries no role field, and the appended `score` stage is the
    operator's `cmd` — which the operator is explicitly invited to point at an entrypoint "the agent
    must BUILD", i.e. one that may itself train. So a pipeline's work stages cannot even be argued to
    CONTAIN the training by elimination.

    Measured, not theorised: driving the real `_evaluate` over `data_prep -> train -> score` with a
    `data_prep` stage printing framework warnings, `CUDA not available - falling back to CPU` and a
    flat `loss: 0.6931`, `deepseek-v4-flash` answered `broken` at confidence 0.9 — while being told,
    in the prompt, that it was looking at stage `data_prep` of that pipeline, and it armed the kill
    gate. The stage identity is a mitigation, not a guarantee, so a stage the plan cannot PROVE is
    training must not hold the authority to discard a multi-hour run with no repair, no retry and no
    refunded `max_nodes` slot. A `LOG_ROLE_WORK` verdict still reaches the alert row,
    `watchdog_reflection`, the attention feed and the audit trail — the watchdog keeps its whole
    advisory job on those stages, only not the gun.

    AMBIGUOUS FILENAMES. A log basename with more than one possible writer cannot be attributed to a
    phase, so it produces no tick. That is ONE rule covering two real collisions: a pipeline stage
    named `setup` shadows the dep install's `setup.log` (`command_eval` writes pip output there
    regardless of any stage), and on Windows two stage names differing only in case fold onto one
    file. Previously the shadowing stage "won the name" and inherited kill authority over pip output.
    """
    raw = list(stages or [])
    names = tuple(str(s.get("name")) for s in raw
                  if isinstance(s, dict) and s.get("name") is not None)
    # The manifest's own answer to "which stage is the training loop", when it gave one — mapped to
    # the artifacts that can SPEND the authority again. `validate_stages` is the single definition of
    # a valid stage and admits exactly one such declaration, so this reads at most one name; anything
    # else is a manifest that never reached here. Read from the CLEANED dicts the engine resolved,
    # never from raw operator/agent text.
    #
    # A DECLARATION WITH NO `expect.files` BUYS NOTHING, and that is fail-closed rather than mean.
    # `training_authority_spent` is the entire price of admitting a declaration: the authority ends
    # the moment the stage's own promised artifact exists, because a stage that also scores
    # in-process (`e5small-dr-unified-v2`'s `train.log` ends `RECALL@100: 0.793344`) cannot be taken
    # at its word about which phase it is in. With no declared artifact there is nothing to observe
    # and the authority could never be handed back — so the gun would be held over the in-process
    # scoring phase too, which is the H-1 defect this whole mechanism exists to keep out. Granting
    # `LOG_ROLE_WORK` instead is exactly the behaviour the manifest had before it declared anything,
    # and it is VISIBLE: with no `LOG_ROLE_TRAINING` in the plan, every tick's span carries
    # `kill_reachable: false` from the first one, which is the same signal a pipeline that declared
    # nothing gets. (Not enforced in `validate_stages` on purpose: refusing the manifest would fail
    # a node over a permission it did not need, and the stage still runs exactly as declared.)
    declared_training: dict = {}
    # ...and, beside it, EVERY stage's own promise, for `stage_contract_context`. Two separate maps
    # over one loop because they answer different questions and must not inherit each other's
    # refusals: `declared_training` is an AUTHORITY grant and is withheld from a stage with no
    # `expect.files`, from the positional scorer and from an incompletely resolved pipeline;
    # `declarations` is EVIDENCE about the bar `verify_stage_artifacts` and the inter-stage checker
    # are already going to hold that stage to, and withholding it from a stage that promised
    # something would hide a check the engine is certainly going to run.
    declarations: dict = {}
    timeouts: dict = {}
    for stage in raw:
        if not isinstance(stage, dict) or stage.get("name") is None:
            continue
        _wall = stage.get("timeout")
        if type(_wall) in (int, float) and math.isfinite(float(_wall)) and float(_wall) > 0:
            timeouts[str(stage.get("name"))] = float(_wall)
        expect = stage.get("expect") or {}
        files = expect.get("files") if isinstance(expect, dict) else None
        promised = tuple(str(f) for f in (files or []) if isinstance(f, str))
        assertion = expect.get("assert") if isinstance(expect, dict) else None
        assertion = str(assertion) if isinstance(assertion, str) else ""
        if promised or assertion:
            declarations[str(stage.get("name"))] = StageDeclaration(assertion=assertion,
                                                                    files=promised)
        if str(stage.get("role") or "").strip().lower() != STAGE_ROLE_TRAINING:
            continue
        if promised:
            declared_training[str(stage.get("name"))] = promised
    # A row this cannot name is a broken resolved pipeline (`_resolve_stages` only ever returns
    # `validate_stages`-cleaned dicts), and dropping it would RENUMBER the rest: a 3-stage list with
    # two unusable rows would otherwise collapse to a "one-stage pipeline" and hand the survivor kill
    # authority. Position-derived SCORE stays (marking more logs unjudged is the safe direction);
    # only the TRAINING grant is withheld.
    complete = len(names) == len(raw)
    roles: dict = {}

    def _claim(key: str, value: tuple) -> None:
        # A second writer for the same basename -> unattributable, and unattributable is not judged.
        roles[key] = value if roles.get(key, value) == value else (None, LOG_ROLE_AMBIGUOUS)

    _claim(_log_name_key(_SETUP_LOG), (None, LOG_ROLE_SETUP))
    if names:
        for index, name in enumerate(names):
            if _is_scorer_stage(name, index=index, total=len(names)):
                # POSITION FIRST, always. The scorer is the operator's protected final stage and a
                # `score.log` verdict once killed a training that had just SUCCEEDED; a manifest
                # must not be able to buy that back by writing `role` on it.
                role = LOG_ROLE_SCORE
            elif len(names) == 1 and complete:
                role = LOG_ROLE_TRAINING     # a one-stage pipeline IS the single-command shape
            elif name in declared_training and complete:
                # DECLARED, not guessed. `complete` for the same reason the one-stage grant needs
                # it: a pipeline this cannot fully name is a broken resolution, and a broken
                # resolution must not hand out the one role that ends nodes.
                role = LOG_ROLE_TRAINING
            else:
                role = LOG_ROLE_WORK
            _claim(_log_name_key(f"{name}.log"), (name, role))
    else:
        _claim(_log_name_key(_SINGLE_COMMAND_LOG), (None, LOG_ROLE_TRAINING))
    # The artifacts belong to the declaration only if the declaration actually BOUGHT the role: the
    # positional scorer rule and the `complete` guard both refuse it, and a stage that was refused
    # must not carry a spend condition for an authority it does not hold.
    artifacts: tuple = ()
    for name, promised in declared_training.items():
        if roles.get(_log_name_key(f"{name}.log"), (None, None))[1] == LOG_ROLE_TRAINING:
            artifacts = promised
    return EvalLogPlan(roles=roles, stage_names=names, training_artifacts=artifacts,
                       declarations=declarations, timeouts=timeouts)


def training_authority_spent(workdir, plan: Optional[EvalLogPlan]) -> bool:
    """Whether a DECLARED training stage has already written what it promised — i.e. whether the
    thing a kill would now destroy is a finished training rather than a running one.

    This is the price of admitting a declaration, paid in the same currency the rest of the file
    uses. `e5small-dr-unified-v2`'s `train` stage does not only train: its own log ends with the
    retrieval evaluation it runs in-process (`RECALL@100: 0.793344` is a line in `train.log`), which
    is the H-1 shape — a judge holding kill authority reading scorer output — moved INSIDE one
    stage, where no plan can split it by filename. A stage that declares `role: "training"` cannot
    be taken at its word about a phase it does not distinguish, so the authority is spent the moment
    its declared artifact exists: after that the verdict is advisory again, exactly as if the stage
    had never declared anything. Not a heuristic about the text — `expect.files` is the manifest's
    own output contract and the file is an exact filesystem fact.

    Fail-closed on I/O trouble: unreadable means the authority is treated as spent (advisory), never
    as live. `()` artifacts answer False here, and the reason that is safe is upstream rather than
    obvious: `eval_log_plan` only ever leaves this empty for a plan whose training role was NOT
    bought by a declaration — the single-command eval and the one-stage pipeline, which never
    promised anything whose arrival could end them. A declaration that named no `expect.files` does
    not reach this function at all, because it is refused `LOG_ROLE_TRAINING` in the first place; an
    authority with no spend condition is one that outlives the training it was granted over, which
    is the exact defect this function exists to prevent.
    """
    if plan is None or not plan.training_artifacts:
        return False
    for rel in plan.training_artifacts:
        try:
            if (Path(workdir) / rel).exists():
                return True
        except (OSError, ValueError):
            # ValueError is not hypothetical: an embedded NUL in a path raises it before any
            # syscall, so a `Path` this cannot even form must land on the same side as one it
            # cannot stat.
            return True
    return False


@dataclass(frozen=True)
class ActiveStageLog:
    """The log a watchdog tick is looking at, plus WHICH eval phase wrote it."""

    path: Path
    stage: Optional[str]
    role: str


def resolve_stage_log(workdir, plan: Optional[EvalLogPlan] = None) -> Optional[ActiveStageLog]:
    """The workdir's live log, ATTRIBUTED to the eval phase that writes it. None when there is nothing
    the caller may read (no `*.log` yet, or none the plan can name).

    Freshest-mtime still tracks the moving active stage — that part of the old heuristic was right, and
    the sandbox's live stage cursor genuinely is unobservable from here. What was wrong was acting on
    the answer without knowing WHICH stage it named:

    REVIEW NOTE (superseded): this glob used to be deliberately broad ("the failure is benign — a
    slightly-less-relevant tail feeds an ADVISORY verdict"). That premise died when `train_monitor_kill`
    became the default: the freshest `*.log` is `setup.log` during a minutes-long pip install and
    `score.log` during the ALWAYS-appended final score stage (`engine/eval_stages.py`), and both were
    fed to a training-health judge holding kill authority, with the changed-digest gate guaranteeing a
    fresh LLM call on every file switch. A `score.log` verdict killed the training that had just
    SUCCEEDED. The engine knows the resolved stage list; pass it in (`eval_log_plan`) and the answer is
    named instead of guessed.

    With a plan, logs the plan cannot name are IGNORED rather than read: a stray `*.log` the candidate's
    own code drops is at best a duplicate of the stage log (which captures the subprocess's whole
    stdout/stderr), and silently judging unattributable bytes is the exact defect above. Without a plan
    the old freshest-file answer stands, tagged `LOG_ROLE_UNKNOWN` so callers can degrade to advisory.
    """
    try:
        logs = list(Path(workdir).glob("*.log"))
    except OSError:
        return None
    if plan is not None:
        logs = [p for p in logs if _log_name_key(p.name) in plan.roles]
    if not logs:
        return None
    try:
        newest = max(logs, key=lambda f: f.stat().st_mtime)
    except OSError:
        return None
    if plan is None:
        return ActiveStageLog(path=newest, stage=None, role=LOG_ROLE_UNKNOWN)
    stage, role = plan.roles[_log_name_key(newest.name)]
    return ActiveStageLog(path=newest, stage=stage, role=role)


def monitor_stage_context(resolved: Optional[ActiveStageLog],
                          plan: Optional[EvalLogPlan] = None) -> str:
    """One line telling the observer WHICH stage's log it is about to read. Pure/deterministic.

    The judge used to receive a tail headed "LIVE TRAINING LOG" with nothing saying which phase of the
    eval produced it, so it had no way to notice it was being shown a scorer. This rides in the
    caller-supplied `context` (the system prompt and the log header stay verbatim — prompt text is a
    contract), and it is the only thing that makes an UNKNOWN attribution visible to the model at all.
    """
    if resolved is None:
        return ""
    if resolved.role == LOG_ROLE_UNKNOWN or plan is None:
        return ("NOTE: this log could not be attributed to a named eval stage, so it may not be the "
                "training stage's output. Judge only what the lines themselves support.")
    if not plan.stage_names:
        return ("This eval runs ONE command that both trains and scores in a single process; the log "
                "below is that command's complete live output.")
    order = " -> ".join(plan.stage_names)
    if resolved.stage in plan.stage_names:
        position = plan.stage_names.index(resolved.stage) + 1
        line = (f"This is the live log of pipeline stage {resolved.stage!r} "
                f"(stage {position} of {len(plan.stage_names)}; the pipeline is {order}). "
                "Judge THIS stage's output only.")
        if resolved.role == LOG_ROLE_WORK:
            # A WORK stage is one the plan cannot prove is the training step (see `eval_log_plan`), and
            # it is exactly where a confident wrong `broken` was measured: a `data_prep` stage printing
            # framework warnings and a flat loss drew `broken` 0.9 WITH the sentence above present.
            # The role carries the safety (no kill authority); this only helps the model answer the
            # right question. Additive — the sentence above is unchanged, and every other attribution
            # still renders byte-identically to before.
            line += (" This stage may be data preparation, export or another non-training step rather "
                     "than the training loop itself; if these lines are not a training run, say that "
                     "instead of judging them as one.")
        return line
    return f"This eval runs the pipeline {order}. Judge only the stage output shown below."


def active_training_log(workdir, plan: Optional[EvalLogPlan] = None) -> Optional[Path]:
    """The live log an observer may read, or None.

    A thin role filter over `resolve_stage_log`, and the filter is READABILITY, never kill authority:
    `setup.log` (dep install), the pipeline's scorer and an unattributable filename
    (`_NON_TRAINING_ROLES`) carry no training signal at all, so they are not returned — a tick during
    those phases has nothing to observe, exactly like a tick before the first log exists. A
    `LOG_ROLE_WORK` stage IS returned: its verdict is advisory (`should_monitor_kill` is where that is
    enforced) but it is the candidate's own running code, and it is also where the sibling ASHA
    watchdog finds the live metric curve. Without a plan this is the historical freshest-`*.log`
    answer (see `resolve_stage_log`'s superseded review note).
    """
    resolved = resolve_stage_log(workdir, plan)
    if resolved is None or resolved.role in _NON_TRAINING_ROLES:
        return None
    return resolved.path


@dataclass(frozen=True)
class TrainingLogCursor:
    """The immutable boundary between two eval attempts for one existing log file."""

    offset: Optional[int]
    identity: Optional[tuple[int, int]]
    probe_start: int = 0
    probe: Optional[bytes] = None


@dataclass(frozen=True)
class TrainingLogSnapshot:
    """Best-effort cursors for every ``*.log`` that existed before an eval attempt started."""

    cursors: dict[str, TrainingLogCursor]
    complete: bool = True


_CURSOR_PROBE_BYTES = 64


def _log_path_key(path: Path) -> str:
    """Stable-enough process-local path identity, including Windows case folding."""
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _file_identity(stat_result) -> Optional[tuple[int, int]]:
    """Return an OS file identity when the filesystem exposes one (Windows does via ``st_ino`` too).

    Deliberately a SUBSET of `core/atomicio.file_identity`: this asks only "is this the same file?",
    never "is it unchanged?" — the monitor tails a log that is expected to grow between reads, so
    including size/mtime would report a rotation on every ordinary append. It also returns None when
    the filesystem cannot prove identity (inode 0), which the canonical tuple has no way to express.
    """
    try:
        device = int(stat_result.st_dev)
        inode = int(stat_result.st_ino)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    return (device, inode) if inode else None


def snapshot_training_logs(workdir) -> TrainingLogSnapshot:
    """Capture attempt-start byte cursors for the workdir's existing stage logs.

    The small boundary probe distinguishes append from truncate-and-regrow even when a filesystem does
    not expose a useful inode. A path that cannot be snapshotted is retained as an unreadable cursor so
    a transient permission/stat failure cannot make a later monitor consume prior-attempt bytes.
    """
    try:
        paths = list(Path(workdir).glob("*.log"))
    except OSError:
        return TrainingLogSnapshot({}, complete=False)
    cursors: dict[str, TrainingLogCursor] = {}
    for path in paths:
        key = _log_path_key(path)
        try:
            with open(path, "rb") as fh:
                stat_result = os.fstat(fh.fileno())
                size = max(0, int(stat_result.st_size))
                probe_start = max(0, size - _CURSOR_PROBE_BYTES)
                fh.seek(probe_start)
                probe = fh.read(size - probe_start)
            cursors[key] = TrainingLogCursor(
                offset=size,
                identity=_file_identity(stat_result),
                probe_start=probe_start,
                probe=probe,
            )
        except (OSError, TypeError, ValueError, OverflowError):
            cursors[key] = TrainingLogCursor(offset=None, identity=None, probe=None)
    return TrainingLogSnapshot(cursors)


def read_training_tail_raw(workdir, *, max_read_bytes: int = 131_072,
                           snapshot: Optional[TrainingLogSnapshot] = None,
                           plan: Optional[EvalLogPlan] = None) -> str:
    """The RAW (un-digested) utf-8 tail of the active stage log — the last `max_read_bytes`. Bounded
    seek-to-tail read so a multi-GB log never loads into memory; a torn leading line is dropped by the
    'replace' decode. '' when there is no log yet. Used by the ASHA watchdog, which feeds it to the
    eval's own metric reader (digesting first would collapse the very metric lines it must parse).

    With an attempt-start ``snapshot``, bytes that predate the current eval are excluded. Replacement,
    rotation, and truncation start a fresh file at byte zero; an unreadable/ambiguous old boundary fails
    closed to an empty tail rather than reusing a stale metric.

    With an eval ``plan`` the read is confined to the stage logs that can carry training output —
    `setup.log` and the protected scorer return '' rather than a tail, so neither watchdog classifies
    (or ranks) another phase's bytes as this training's.
    """
    path = active_training_log(workdir, plan)
    if path is None:
        return ""
    limit = max(0, int(max_read_bytes))
    if limit == 0:
        return ""
    try:
        with open(path, "rb") as fh:
            size = max(0, int(os.fstat(fh.fileno()).st_size))
            floor = attempt_byte_floor(fh, path, snapshot)
            if floor is None:
                return ""
            fh.seek(max(floor, size - limit))
            raw = fh.read(limit)
    except (OSError, TypeError, ValueError, OverflowError):
        return ""
    return raw.decode("utf-8", "replace")


def attempt_byte_floor(fh, path, snapshot: Optional[TrainingLogSnapshot]) -> Optional[int]:
    """The byte offset at or above which THIS eval attempt's bytes begin in the already-open `fh`.

    `None` means the boundary cannot be established and the caller must return nothing: a transient
    permission/stat failure at snapshot time must not let a monitor consume prior-attempt bytes.
    `0` (no snapshot, or a fresh file) means the whole file is this attempt's.

    Extracted from `read_training_tail_raw` so the SECOND reader of these bytes — the log tools the
    judge queries (`tools/log_tools.py`, wired in `monitor_log_sources`) — cannot come to a different
    conclusion about where the previous attempt ended. One boundary, two readers; the alternative is a
    role that seeks past a floor the digest respects and reads a dead attempt's curve as the live one's.
    Leaves `fh`'s position undefined — every caller seeks before reading.
    """
    if snapshot is not None and not snapshot.complete:
        return None
    stat_result = os.fstat(fh.fileno())
    size = max(0, int(stat_result.st_size))
    current_identity = _file_identity(stat_result)
    cursor = snapshot.cursors.get(_log_path_key(Path(path))) if snapshot is not None else None
    if cursor is None and snapshot is not None and current_identity is not None:
        # A rotation can rename the old file to another ``*.log`` path. Follow identity across
        # that rename so the renamed prior-attempt bytes are not mistaken for a brand-new log.
        cursor = next((old for old in snapshot.cursors.values()
                       if old.identity == current_identity), None)
    if cursor is None and snapshot is not None:
        # Some filesystems expose no stable inode. A matching old EOF probe is sufficient to
        # classify an unknown path as a renamed old log; a false match only suppresses advisory
        # evidence (safe), whereas treating it as new could resurrect a stale kill metric.
        for old in snapshot.cursors.values():
            if old.offset is None or old.probe is None or size < old.offset:
                continue
            fh.seek(old.probe_start)
            if fh.read(len(old.probe)) == old.probe:
                cursor = old
                break
    if cursor is None:
        return 0
    if cursor.offset is None or cursor.probe is None:
        return None
    replaced = (cursor.identity is not None and current_identity is not None
                and cursor.identity != current_identity)
    truncated = size < cursor.offset
    boundary_changed = False
    if not replaced and not truncated:
        fh.seek(cursor.probe_start)
        boundary_changed = fh.read(len(cursor.probe)) != cursor.probe
    # only a proven append may inherit the old EOF. Rotation/replacement or a
    # truncate-and-regrow (including past the old size before the first watchdog tick) starts
    # at zero; if identity is unavailable, the boundary probe supplies the same protection.
    return 0 if (replaced or truncated or boundary_changed) else cursor.offset


def monitor_log_sources(workdir, plan: Optional[EvalLogPlan] = None,
                        snapshot: Optional[TrainingLogSnapshot] = None) -> list:
    """The `tools/log_tools.LogSource` map for ONE eval: every stage log the plan can NAME, each with
    the eval phase that writes it and this attempt's byte floor.

    This is the whole of rule 2 in `tools/log_tools.py`'s boundary. What a role may read is exactly
    what `eval_log_plan` derived from the resolved pipeline — the node's own workdir output — so the
    tool never constructs a path from model input and there is nothing outside the workdir to name.
    A log the plan cannot attribute (`LOG_ROLE_AMBIGUOUS`) is left OUT for the same reason
    `resolve_stage_log` refuses to judge it: bytes nobody can attribute to a phase are not evidence.

    Deliberately WIDER than `read_training_tail_raw`'s single active log and deliberately NOT wider
    than the plan: `setup.log` and the scorer carry no TRAINING-health authority (`_NON_TRAINING_ROLES`,
    enforced in `should_monitor_kill`), but a judge that can read them can answer "did the dep install
    actually get the CUDA build" and "has the scorer started yet", which is the question the tail's
    absence of an answer used to be mistaken for evidence about. The ROLE rides on every source, so
    the model is always told which phase it is reading — the same fix `monitor_stage_context` makes
    for the spliced tail.

    Returns [] when there is no log yet. Import is function-local: `tools/` sits BELOW `engine/`, so a
    module-level import here would be the wrong direction for a module `tools` must never import back.
    """
    from looplab.tools.log_tools import LogSource
    try:
        candidates = sorted(Path(workdir).glob("*.log"))
    except OSError:
        return []
    sources: list = []
    for path in candidates:
        key = _log_name_key(path.name)
        if plan is not None:
            if key not in plan.roles:
                continue
            stage, role = plan.roles[key]
            if role == LOG_ROLE_AMBIGUOUS:
                continue
        else:
            role = LOG_ROLE_UNKNOWN
        floor = 0
        try:
            with open(path, "rb") as fh:
                boundary = attempt_byte_floor(fh, path, snapshot)
            if boundary is None:
                continue          # fail closed — the same direction `read_training_tail_raw` fails
            floor = boundary
        except (OSError, TypeError, ValueError, OverflowError):
            continue
        sources.append(LogSource(name=path.name, path=path, role=role, floor=floor))
    return sources


# How many windows a FINISHED stage log is reduced to. The monitor's tracker gets one window per
# tick because it reads a live file it can only ever see the tail of; a stage check runs after the
# stage has EXITED, so the whole of this attempt's bytes are on disk and the windowing is a choice
# rather than a constraint. 32 mirrors the tick granularity a multi-hour eval actually produces at
# `train_monitor_interval_s`, and the direction test only reads the first and last NUMERIC window's
# medians plus the median of the per-window noise floors, so it is not sensitive to the exact count —
# what it must not be is 1, which `summarize_trajectory` already refuses ("ONE window is a tail by
# another name").
STAGE_TRAJECTORY_WINDOWS = 32
# ...and the bound on what one window costs in memory, since the window size is derived from the
# file. A 53.6 MB stage log — the largest in `runs/` — reduces to 32 x 1.67 MB chunks; the floor
# stops a small log from being cut into 32 slivers that each hold one progress-bar render.
STAGE_TRAJECTORY_MIN_CHUNK = 65_536
STAGE_TRAJECTORY_MAX_CHUNK = 4 * 1024 * 1024


def read_stage_trajectory(path, *, floor: int = 0,
                          windows: int = STAGE_TRAJECTORY_WINDOWS) -> LossTrajectory:
    """Measure the loss trajectory over THIS attempt's bytes of a finished stage log.

    STREAMED, never slurped: the file is read from `floor` to EOF in record-aligned chunks and each
    chunk is reduced to one `LossWindow` on the way past, so peak memory is one chunk and every byte
    above the floor is covered. A head+tail read was the obvious cheaper alternative and is refused —
    `_anomaly_of`'s non-finite rung asks a question about EVERY window, and a `loss=nan` in the middle
    of a run that recovers is exactly the evidence a bounded read would drop. Measured on the largest
    stage log in `runs/` (53.6 MB, `e5small-dr-unified-v2` node 2): 2.70 s, ~19.8 MB/s, once per
    checked stage, on the eval worker thread that is about to block on an LLM call anyway.

    `floor` is `attempt_byte_floor`'s answer and is NOT optional in practice: stage logs are opened
    `"a"` (`sandbox._tee_drain`), so a repaired or re-run stage appends to its predecessor's bytes and
    a floorless read splices two curves into one — inventing both a jump and a direction. Driven in
    `tests/test_stage_trajectory.py`.

    Returns an empty `LossTrajectory` (`windows=0`, `direction="unknown"`) for every failure — no
    file, no permission, nothing above the floor, no loss value in the bytes. That is the value
    `trajectory_acquits_stage_check` refuses on, so an unreadable log leaves the checker's verdict
    exactly as it was."""
    try:
        want = max(2, int(windows))
    except (TypeError, ValueError):
        want = STAGE_TRAJECTORY_WINDOWS
    rows: list = []
    try:
        with open(path, "rb") as fh:
            size = max(0, int(os.fstat(fh.fileno()).st_size))
            start = max(0, int(floor or 0))
            region = size - start
            if region <= 0:
                return LossTrajectory()
            # ...and never so large that the region is ONE window. `summarize_trajectory` refuses a
            # direction on a single window ("ONE window is a tail by another name"), so a chunk floor
            # that swallowed a short log would answer `unknown` about a curve plainly visible in it —
            # the same silent narrowing as the tail, arriving by a different route. Driven: a 44 KB
            # eval log is 1 chunk at the bare floor and 2 with this clamp.
            chunk = max(1, min(max(region // want, STAGE_TRAJECTORY_MIN_CHUNK),
                               STAGE_TRAJECTORY_MAX_CHUNK, region // 2))
            fh.seek(start)
            carry = b""
            remaining = region
            while remaining > 0:
                raw = fh.read(min(chunk, remaining))
                if not raw:
                    break
                remaining -= len(raw)
                buf = carry + raw
                # Align on a record boundary — `\n` OR `\r`, because a tqdm bar writes its whole life
                # into one newline-delimited line (the same rule `tools/log_tools._RECORD_SPLIT`
                # states). Splitting mid-render would cut a `loss=13.3` in half and lose the point.
                cut = max(buf.rfind(b"\n"), buf.rfind(b"\r"))
                if cut < 0:
                    # No boundary anywhere in this chunk. Split it anyway once the buffer has reached
                    # a full chunk. A log that never writes `\n` or `\r` is not hypothetical (a
                    # script printing with `end=""`), and letting the carry grow is the slurp this
                    # function streams to avoid — worse, a carry bounded at the whole region yields
                    # ONE window, which `summarize_trajectory` refuses a direction on, so the reader
                    # would answer `unknown` about a curve it had just read every point of. The
                    # forced split can cut ONE render in half, costing one loss value per split out
                    # of thousands.
                    carry = buf
                    if len(carry) < chunk:
                        continue
                    cut = len(carry) - 1
                buf, carry = buf[:cut + 1], buf[cut + 1:]
                window = summarize_loss_window(buf.decode("utf-8", "replace"))
                if window is not None:
                    rows.append(window)
            if carry:
                window = summarize_loss_window(carry.decode("utf-8", "replace"))
                if window is not None:
                    rows.append(window)
    except (OSError, TypeError, ValueError, OverflowError):
        return LossTrajectory()
    return summarize_trajectory(rows)


def stage_check_trajectory(workdir, stage: str, *, plan: Optional[EvalLogPlan] = None,
                           snapshot: Optional[TrainingLogSnapshot] = None) -> LossTrajectory:
    """The trajectory of the stage the inter-stage checker is about to judge, or an empty one.

    The path is NEVER constructed from anything a model said: `stage` is the resolved pipeline's own
    stage name, the basename is the one `command_eval._run_stages` writes (`ex.log(f"{name}.log")`),
    and `plan` — the same `eval_log_plan` the watchdogs use — must agree that this basename belongs
    to THIS stage. A `LOG_ROLE_AMBIGUOUS` name (two stages folding onto one file, or a stage called
    `setup` shadowing the dep install's `setup.log`) is refused for the reason `monitor_log_sources`
    refuses it: bytes nobody can attribute to a phase are not evidence.

    `snapshot` is the pre-attempt `snapshot_training_logs`, taken before any stage of this eval ran,
    which is what makes `attempt_byte_floor` able to answer at all. With no snapshot the floor is 0
    and a repaired stage's earlier curve is in scope — so the caller that has one must pass it."""
    if not str(stage or "").strip():
        return LossTrajectory()
    name = f"{stage}.log"
    if plan is not None:
        claimed = plan.roles.get(_log_name_key(name))
        if claimed is None or claimed[0] != stage or claimed[1] == LOG_ROLE_AMBIGUOUS:
            return LossTrajectory()
    try:
        path = Path(workdir) / name
        with open(path, "rb") as fh:
            floor = attempt_byte_floor(fh, path, snapshot)
    except (OSError, TypeError, ValueError, OverflowError):
        return LossTrajectory()
    if floor is None:
        return LossTrajectory()     # fail closed — the boundary could not be established
    return read_stage_trajectory(path, floor=floor)


def read_training_tail(workdir, *, max_read_bytes: int = 131_072,
                       max_lines: int = 40, max_chars: int = 4000,
                       snapshot: Optional[TrainingLogSnapshot] = None,
                       plan: Optional[EvalLogPlan] = None) -> str:
    """Read only the LAST `max_read_bytes` of the active stage log and digest it (collapse tqdm
    re-renders, keep the recent trajectory). '' when there is no log yet.

    REVIEW NOTE (accepted, not fixed): this bounded seek-to-tail pattern (stat size → seek → read) also
    appears inline in `serve/routers/runs.py::_tail` and `events/eventstore.py::_disk_last_seq`. Each copy
    differs in its line-boundary handling (and there is no existing shared helper — `sandbox._clamp_tail_bytes`
    clamps an in-memory STRING, not a file), so a 3-call-site extraction is deferred as not worth the churn."""
    raw = read_training_tail_raw(workdir, max_read_bytes=max_read_bytes, snapshot=snapshot, plan=plan)
    if not raw:
        return ""
    return training_log_digest(raw, max_lines=max_lines, max_chars=max_chars)
