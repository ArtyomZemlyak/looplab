"""Staged-eval resolution & selective re-run (Phase 1/2) for the engine — extracted from
orchestrator.py as a MIXIN: `class Engine(EvalStagesMixin, …)` inherits these methods unchanged,
so there is ZERO call-site churn and `self` here IS the engine. The method bodies are verbatim
moves (only the three `Engine.`-qualified staticmethod self-references became
`EvalStagesMixin.`-qualified) and read engine attributes freely (`_eval_spec`,
`_strategy_fidelity`, `_reflect_client`, `_idea_text`, …), exactly as they did inside the class.

The cluster: manifest -> concrete stages (`_resolve_stages` / `_resolved_stages`), the static
import-reachability analysis behind safe stage reuse (`_imported_modules` /
`_module_file_candidates` / `_stage_reachable_files` / `_safe_reuse_start`), and the LLM
stage-check factory (`_stage_check_fn`). Heavy deps (command_eval, json) stay method-local so
monkeypatching through their source modules keeps working.

Layering: no runtime import of the orchestrator and never serve — only core/stdlib at module
level (runtime/agents deps are lazy, method-local imports)."""
from __future__ import annotations

import os
import re
from pathlib import Path

from looplab.core.llm_broker import in_llm_lane

# The reply protocol the inter-stage checker answers in, and the ONLY three things it can mean. The
# vocabulary itself lives in `runtime/command_eval.py` beside the code that acts on it; this is the
# wire format and its parser. See `command_eval.STAGE_CHECK_HARD_KINDS` for the 46.6 GPU-hours that
# made a vocabulary necessary — until 2026-08-13 the rule reading this reply was `startswith("OK")`
# and everything else ended the node.
_STAGE_CHECK_OK = "OK"
_STAGE_CHECK_FAIL = "FAIL"
_STAGE_CHECK_UNSURE = "INCONCLUSIVE"
_STAGE_CHECK_REPLY_RE = re.compile(
    r"^\s*(?P<verb>OK|FAIL|INCONCLUSIVE)\b[ \t]*(?P<kind>[a-z_]+)?[ \t]*[:\-—]?[ \t]*(?P<rest>.*)",
    re.IGNORECASE | re.DOTALL)


def parse_stage_check_reply(text: str, *, declared: bool):
    """The checker's reply -> `StageCheckVerdict | None` (None = the stage passed). Pure and TOTAL.

    Hoisted to module level with a truth table (`tests/test_stage_check_verdict.py`) rather than left
    inside the closure, because it is the rule that decides whether a 15-hour training is thrown
    away, and a rule nobody can state is a rule nobody reviews.

    Every answer that is not an explicit, in-vocabulary `FAIL <kind>` becomes `inconclusive` — prose,
    an out-of-enum kind, a bare `FAIL`, an empty reply. That is the same mechanical enforcement
    `triage.py::coerce_triage_action` applies for the same reason: "nobody could read this" is its
    own fact, and resolving it to the most expensive action is how a checker that was told twice not
    to judge quality went on killing nodes with "recall (0.79) is below previous best (0.8491)".

    `declared` is whether the stage carried an `expect.assert`. Without one,
    `declared_condition_violated` is refused and degrades to `inconclusive` — a checker may not
    invoke a contract that does not exist, and that comparison against "previous best" is precisely
    what an undeclared contract looks like from the inside."""
    from looplab.runtime.command_eval import (STAGE_CHECK_INCONCLUSIVE, StageCheckVerdict,
                                              coerce_stage_check_kind)
    raw = (text or "").strip()
    if not raw:
        return None                      # nothing said is not a failure — historical behaviour
    m = _STAGE_CHECK_REPLY_RE.match(raw)
    if m is None:
        return StageCheckVerdict(STAGE_CHECK_INCONCLUSIVE, raw[:300])
    verb = m.group("verb").upper()
    rest = (m.group("rest") or "").strip()
    kind_word = (m.group("kind") or "").strip().lower()
    if verb == _STAGE_CHECK_OK:
        return None
    detail = (f"{kind_word}: {rest}" if (kind_word and rest) else (rest or kind_word or raw))[:300]
    if verb == _STAGE_CHECK_UNSURE:
        return StageCheckVerdict(STAGE_CHECK_INCONCLUSIVE, detail)
    kind = coerce_stage_check_kind(kind_word)
    if kind == "declared_condition_violated" and not declared:
        kind = STAGE_CHECK_INCONCLUSIVE
    return StageCheckVerdict(kind, detail)


# The deadline judge's reply protocol. Deliberately the SMALLEST possible answer — a verdict word
# and nothing else — because the only thing the engine may take from it is a boolean, and a model
# that is allowed to name its own number will name a large one.
_DEADLINE_FINISHING = "FINISHING"
# NOT_FINISHING is listed FIRST and the verdict is read from the GROUP, never from "did it match".
# Ordered the other way — and read as a bare truthiness test — `NOT_FINISHING` matched the pattern
# and bought the extension it was refusing, which `tests/test_deadline_grace.py` caught.
_DEADLINE_REPLY_RE = re.compile(r"^\s*(NOT_FINISHING|FINISHING)\b", re.IGNORECASE)


def parse_deadline_reply(text: str) -> bool:
    """Did the judge say the stage is about to finish? Pure, TOTAL, and fail-CLOSED.

    Anything that is not an explicit `FINISHING` — prose, `NOT_FINISHING`, an empty answer, a
    hedge — is False, i.e. the historical kill. This direction is the opposite of the stage
    checker's and deliberately so: there, refusing to act on an unreadable answer SAVES a node; here,
    acting on one SPENDS money. The unreadable case must resolve to whichever answer is cheap, and
    that is not the same answer in both places."""
    m = _DEADLINE_REPLY_RE.match((text or "").strip())
    return m is not None and m.group(1).upper() == _DEADLINE_FINISHING


class EvalStagesMixin:
    """The engine's staged-eval cluster. See the module docstring for the mixin convention
    (`self` is the Engine)."""

    def _resolve_stages(self, workdir, es, params=None, score_cmd=None, score_timeout=None):
        """Resolve the ordered eval pipeline, with the operator's `cmd` (es) AUTHORITATIVE and
        non-overridable (redesign: the agent can't rewrite how it's scored):

          • cmd DECLARES stages (`es["stages"]`) → those ARE the pipeline, canonical. The Developer
            only implements the scripts each stage calls; a `looplab_stages.json` is IGNORED.
          • cmd is a SINGLE command → the Developer's `looplab_stages.json` supplies the PRECEDING
            stages (data_prep / train / …); the operator's cmd is appended as the final, protected
            `score` stage whose stdout the trusted metric reader reads. So the agent can add work
            BEFORE scoring but never replace the scoring itself.

        `score_cmd`/`score_timeout` (the profile- + params-resolved command/timeout from
        `build_command`) drive the appended score stage, so the smoke/full eval PROFILE and its
        timeout still apply in pipeline mode — not just the base command at the default timeout.
        `%params%` tokens in any preceding stage command expand to the node's params. Returns None
        (classic single-command eval) when there are no stages."""
        import json
        from looplab.runtime import command_eval

        def _expand(stages):
            # per-node %params% expansion over an already-VALIDATED clean stage list
            return [dict(s, command=command_eval.expand_params(list(s["command"]), params))
                    for s in stages]

        task_stages = es.get("stages")
        if isinstance(task_stages, list) and task_stages:
            # cmd declares stages → canonical, dev file ignored. EvalSpec validated these at submit
            # time; re-run the shared validator anyway (an old/hand-edited snapshot bypasses pydantic)
            # and fall back to the single command on a bad list rather than run a half-parsed pipeline.
            # `allow_env=True`: this branch reads the OPERATOR's own declared pipeline, which is
            # the only declarer allowed a stage `env`. The developer-manifest branch below goes
            # through `materialized_stages`, which keeps the fail-closed default.
            clean, err = command_eval.validate_stages(task_stages, allow_env=True)
            if err is None:
                return _expand(clean)
            # A BAD operator list falls back to the SINGLE COMMAND, and must not fall THROUGH to the
            # developer-manifest branch below. Falling through handed stage authorship to the
            # agent-authored looplab_stages.json in exactly the case this branch exists to prevent
            # ("the agent can't rewrite how it's scored"): one malformed stage in an old/hand-edited
            # snapshot silently promoted the Developer's preceding stages into the pipeline. The
            # operator's `cmd` still runs — dropping their unusable stage list is the conservative
            # reading, and the alternative (raising) would fail a resumed run on a snapshot the
            # engine can still honour the scoring half of.
            return None

        # single-command cmd: read the Developer's PRECEDING stages, append the protected cmd stage.
        # `materialized_stages` applies the SAME shared rules as the declare_stages tool (reserved
        # 'score', no duplicates, argv shape) and accepts both the wrapped + bare-list manifest shapes
        # — the tool is the friendly authoring path, but write_file / a CLI-agent diff / a pre-redesign
        # run can still hand-write the manifest, and an unvalidated one could smuggle a second 'score'
        # stage (clobbering score.log and confusing stage-scoped re-runs) or a full scorer that
        # double-runs the eval after cmd is appended. Invalid → None (single-command fallback), never a
        # half-cleaned pipeline. It is the SAME helper the repo-Developer prompt reads, so the two can't
        # drift (M7).
        preceding = None
        mf = Path(workdir) / "looplab_stages.json"
        if mf.exists():
            try:
                preceding = command_eval.materialized_stages(json.loads(mf.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001 — a malformed manifest just falls back to the single command
                preceding = None
        if preceding:
            # the operator's cmd is the FINAL + protected scoring stage; reuse the already
            # profile-resolved command/timeout (build_command) so smoke/full still applies.
            final = {"name": "score",
                     "command": list(score_cmd) if score_cmd is not None
                                else command_eval.expand_params(list(es.get("command") or []), params),
                     "timeout": score_timeout if score_timeout is not None else es.get("timeout", 600.0)}
            return _expand(preceding) + [final]
        return None

    # The refusal `_run_eval` reports when the resolved chain invokes a PROTECTED script the workdir
    # does not contain. Hoisted so the two audiences it has to serve are visible in one place and can
    # be pinned: the OPERATOR, who is the only one who can fix it, and the inline-repair loop, which
    # would otherwise spend every attempt asking the Developer to write a file the write gate refuses.
    # `{stage}`/`{script}` are the only placeholders.
    PROTECTED_SCRIPT_MISSING = (
        "stage {stage!r} cannot run: {script} is PROTECTED (the operator owns it) but was never "
        "materialized into this node's workdir, so the command has no file to execute. This is NOT a "
        "repairable code error and the agent must not be asked to fix it — a protected path is exactly "
        "what the write gate refuses, so no edit can create it. Fix the TASK instead: make the file "
        "part of what each node is seeded with (commit it to the editable repo, or set seed_mode "
        "\"all\"), or drop it from `protect` so the Developer may author it.")

    def _unrunnable_protected_scripts(self, chain, workdir, cwd) -> list:
        """`[(stage, script)]` for every PROTECTED script the resolved chain invokes that is not in the
        eval cwd — the one eval failure NOTHING downstream can repair, detected BEFORE the first stage
        runs instead of after a node has paid for a full train.

        The live instance: `protect: ["looplab_eval.py"]` + a `score` stage running
        `python looplab_eval.py`, on a repo where that file was never committed — so `seed_mode="auto"`
        (git-tracked files only) left the workdir without it. `seed_protected_files` closes that hole,
        and this stays as the BACKSTOP for the cases seeding cannot close: a `protect` entry that
        matches nothing in the source (an operator typo — "one eval command but no file"), a glob that
        stopped matching, a protected path outside the repo. What makes it worth its own check rather
        than an ENOENT is the asymmetry: for any OTHER missing script the Developer can write the file,
        and the repair loop is the right answer; for a protected one it is structurally the wrong one.

        Deliberately narrow. Only a workdir-relative `.py` token is considered a script (the same rule
        `_stage_reachable_files` uses), and only one whose workspace-relative name is in
        `protected_names` — so a missing EDITABLE entrypoint keeps its existing behaviour (the eval
        runs, fails, and the Developer repairs it), and a stage that legitimately GENERATES a later
        stage's script cannot be pre-empted, because a generated script is never protected."""
        prot = set((self._repo_spec or {}).get("protected_names") or [])
        if not prot or not chain:
            return []
        wd, cd = Path(workdir).resolve(), Path(cwd).resolve()
        try:
            # The protected names are WORKSPACE-relative; the scripts are named relative to the eval
            # `cwd`. Namespacing by the cwd's own offset is what `RepoTask._eval_protected` does on the
            # other side of the same join. An eval cwd outside the workdir (an external tool dir the
            # `sandbox_cwd` remap trusts as given) shares no namespace with `protect` — check nothing.
            pre = cd.relative_to(wd).as_posix()
        except ValueError:
            return []
        pre = "" if pre in (".", "") else pre + "/"
        out: list = []
        for name, argv in chain:
            for tok in (argv or []):
                if not isinstance(tok, str) or not tok.endswith(".py") or tok.startswith("-"):
                    continue
                rel = tok
                while rel.startswith("./"):
                    rel = rel[2:]
                if os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
                    continue                      # an absolute/escaping path is not a protected name
                key = (pre + rel).replace("\\", "/")
                row = (str(name), key)
                if key in prot and not (cd / rel).exists() and row not in out:
                    out.append(row)
        return out

    def _resolved_stages(self, node, workdir) -> list:
        """Re-resolve the eval pipeline the way `_run_eval` does — used by the inline-repair reuse
        predicate to inspect the stages' COMMANDS (which script each runs). [] for a single-command
        eval (no reuse question). Deterministic given (node, workdir, eval_spec)."""
        if not self._eval_spec:
            return []
        from looplab.runtime import command_eval
        es = self._eval_spec
        prof = (node.idea.eval_profile if node is not None else None)
        if prof is None and self._strategy_fidelity in ("smoke", "full"):
            prof = self._strategy_fidelity
        params = node.idea.params if node is not None else {}
        try:
            cmd, timeout = command_eval.build_command(es, params, prof)
            return self._resolve_stages(str(Path(workdir).resolve()), es, params,
                                        score_cmd=cmd, score_timeout=timeout) or []
        except Exception:  # noqa: BLE001 — a resolution hiccup must not crash the repair loop; no reuse
            return []

    @staticmethod
    def _imported_modules(kw: str, rest: str) -> list:
        """The module names an `import`/`from … import …` clause references. `kw` is the keyword and
        `rest` the text after it. Handles `import a, b as c` (a, b), `from pkg import x, y` (pkg AND the
        possible submodules pkg.x / pkg.y), and relative imports (`from .sub import x` → .sub, .sub.x)."""
        rest = rest.split("#", 1)[0].strip()
        mods: list = []
        if kw == "import":
            for part in rest.split(","):
                name = part.strip().split(" as ")[0].strip()
                if re.match(r"^\.*[\w.]+$", name):
                    mods.append(name)
            return mods
        m = re.match(r"^(\.*[\w.]*)\s+import\s+(.+)$", rest)      # from X import a, b, (c)
        if not m:
            return mods
        base = m.group(1)
        if base:
            mods.append(base)
        names = m.group(2).replace("(", " ").replace(")", " ")
        for nm in names.split(","):
            nm = nm.strip().split(" as ")[0].strip()
            if nm and nm != "*" and re.match(r"^\w+$", nm) and base:
                mods.append(base + nm if base.endswith(".") else f"{base}.{nm}")
        return mods

    @staticmethod
    def _module_file_candidates(mod: str, script_rel: str) -> list:
        """Repo-relative file paths a dotted import `mod` could resolve to, tried against BOTH the
        workdir root AND the importing script's OWN directory (Python puts the script dir on sys.path, so
        a subdir script's sibling import resolves there). A leading-dot relative import anchors at the
        script's package dir only. Emits the module file AND the package `__init__.py` at every depth
        (importing a.b.c also runs a/__init__.py and a/b/__init__.py) — an over-approximation that only
        ever ADDS reachable files, keeping the reuse predicate conservative."""
        script_dir = script_rel.rsplit("/", 1)[0] if "/" in script_rel else ""
        rel = mod
        if mod.startswith("."):                                  # relative: strip dots, anchor at script dir
            rel = mod.lstrip(".")
            bases = [script_dir]
        else:
            bases = ["", script_dir] if script_dir else [""]
        parts = [p for p in rel.split(".") if p]
        out: list = []
        for base in dict.fromkeys(bases):                        # de-dup, preserve order
            cur = base
            for part in parts:
                cur = f"{cur}/{part}" if cur else part
                out.append(f"{cur}.py")
                out.append(f"{cur}/__init__.py")
        return out

    @staticmethod
    def _stage_reachable_files(stages: list, workdir):
        """Repo-relative files the earlier stages' runs REACH — each command's local `.py` script plus
        the TRANSITIVE closure of its workdir-local imports (module files + package `__init__`s, resolved
        against the workdir root AND each script's own directory). Used to decide whether a repair's edits
        could have changed what an earlier (to-be-reused) stage produced.

        Returns None (OPAQUE → treat as UNSAFE, refuse reuse) when a stage runs something we can't
        statically bound: a command with no local `.py` script token (`python -m pkg`, a shell wrapper, a
        bare binary) or a `.py` path outside the workdir. Fail-closed by construction — a spuriously
        'reachable' file only forces a re-train, but a MISSED dependency would silently score a stale
        checkpoint (the invariant `_safe_reuse_start` exists to protect).

        Interim heuristic, superseded long-term by the per-stage ARTIFACT DECLARATION design
        (docs/BACKLOG.md §6 'Deferred design work') — prefer extending that design over adding cases here."""
        wd = Path(workdir)
        imp_re = re.compile(r"^[ \t]*(from|import)[ \t]+(.+?)[ \t]*$", re.M)
        # Parenthesized MULTI-LINE imports (`from pkg import (\n  a,\n  b,\n)`) span lines, so the
        # line-anchored pattern above only sees `from pkg import (` — it credits pkg/__init__.py but
        # MISSES the pkg/a.py / pkg/b.py submodule files, letting a repair to those escape the
        # closure. This companion pattern captures the whole group (a char class matches newlines
        # without re.S); `#` comments are stripped from the whole source BEFORE either scan runs
        # (see below), so a ')' inside a trailing comment can't end the group early.
        paren_re = re.compile(r"^[ \t]*from[ \t]+(\.*[\w.]*)[ \t]+import[ \t]*\(([^)]*)\)", re.M)
        out: set = set()
        pending: list = []
        for s in (stages or []):
            cmd = s.get("command") or []
            scripts: list = []
            for tok in cmd:
                if not isinstance(tok, str) or not tok.endswith(".py"):
                    continue
                rel = tok
                while rel.startswith("./"):
                    rel = rel[2:]
                if os.path.isabs(rel):                           # only trackable if it lives under the workdir
                    try:
                        rel = str(Path(rel).resolve().relative_to(wd.resolve()))
                    except Exception:  # noqa: BLE001 — an out-of-tree script we can't bound → opaque
                        return None
                scripts.append(rel)
            if cmd and not scripts:                              # runs SOMETHING but exposes no local .py → opaque
                return None
            pending.extend(scripts)
        while pending:
            rel = pending.pop()
            if rel in out:
                continue
            out.add(rel)
            p = wd / rel
            if not p.exists():
                continue
            try:
                src = p.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            # Strip `#` comments BEFORE both import scans: the paren pattern's `[^)]*` group stops
            # at the FIRST ')', so a ')' inside a trailing comment (`vit,  # backbone (legacy)`)
            # would end the group early and silently drop every name after it from the closure.
            # The blunt regex also eats '#' inside string literals — that can only ADD spurious
            # 'reachable' candidates, which is fail-closed-safe here (a false positive merely
            # forces a re-train; it's a MISSED dependency that would score a stale checkpoint).
            src = re.sub(r"#[^\n]*", "", src)
            def _credit(mods):
                for mod in mods:
                    for cand in EvalStagesMixin._module_file_candidates(mod, rel):
                        if cand not in out and (wd / cand).exists():
                            pending.append(cand)
            for m in imp_re.finditer(src):
                _credit(EvalStagesMixin._imported_modules(m.group(1), m.group(2)))
            for m in paren_re.finditer(src):
                inner = " ".join(m.group(2).splitlines())    # comments already gone (see above)
                _credit(EvalStagesMixin._imported_modules("from", f"{m.group(1)} import ({inner})"))
        return out

    def _safe_reuse_start(self, stages: list, failed_stage, changed_files, workdir,
                          deleted=None, cwd=None):
        """The stage to RESTART from so a repaired node reuses the completed EARLIER stages (e.g. skip
        re-`train` when only the `score` script was fixed) — or None to re-run the FULL pipeline (the
        safe default that preserves the 'each node trains a FRESH model' invariant).

        SAFE-OR-RETRAIN: reuse the earlier stages' artifacts ONLY when the repair's changed files are
        DISJOINT from those stages' reachable files (their scripts + transitive imports). If a change
        could have altered what an earlier stage produces (train.py / loss.py / model.py it imports, or
        the stage MANIFEST that carries the argv), we must re-train — reusing a stale checkpoint would
        score a model that doesn't reflect the current code. Beyond the disjointness test, reuse is
        REFUSED outright whenever the predicate cannot PROVE the earlier stages' inputs are unchanged:
          • an OPAQUE earlier stage (no resolvable local script: `python -m`, a shell wrapper);
          • the repair DELETED any file (`deleted`) — the closure can't see vanished modules;
          • any changed file is not a `.py` (config/data inputs are invisible to import reachability);
          • the eval runs under a non-default `cwd` (changed-file keys and stage-script paths resolve
            against different bases, so the intersection proves nothing).
        A false negative just re-trains (no worse than a full re-run); a false positive is a silent
        stale score, so the predicate is conservative by construction and fail-closed on anything it
        can't statically bound.

        Interim heuristic, superseded long-term by the per-stage ARTIFACT DECLARATION design
        (docs/BACKLOG.md §6 'Deferred design work') — prefer extending that design over adding cases here."""
        if not stages or not failed_stage:
            return None
        names = [str(s.get("name")) for s in stages]
        if failed_stage not in names:
            return None
        fi = names.index(failed_stage)
        if fi == 0:                                   # nothing before it to reuse
            return None
        # DELETIONS are un-boundable: _write_node_files unlinks them BEFORE this predicate runs, so
        # the reachability closure (walked over files still on disk) can never rediscover an import
        # of the vanished module — `changed ∩ reachable` would be trivially disjoint even when an
        # earlier stage trained THROUGH the deleted file. Any deletion forces a full re-run.
        if deleted:
            return None
        # A non-default eval `cwd` re-bases the stage scripts: the reachable set resolves script
        # paths against the WORKDIR root, but the repair's changed-file keys are workdir-relative —
        # under a subdir cwd the same file appears as `sub/train.py` on one side and `train.py` on
        # the other, so disjointness proves nothing. Fail closed rather than guess the remapping.
        if cwd not in (None, "", "."):
            return None
        changed = {(f[2:] if isinstance(f, str) and f.startswith("./") else f) for f in (changed_files or [])}
        # A change to the stage MANIFEST rewrites the pipeline's argv (e.g. train hyperparams), so the
        # completed checkpoint no longer matches the declared command — never reuse across it.
        if any(str(c).rsplit("/", 1)[-1] == "looplab_stages.json" for c in changed):
            return None
        # Reachability only bounds PYTHON imports: a changed non-.py file (config.yaml, a params
        # file, a writable data copy the train stage reads) can alter what an earlier stage produced
        # without ever appearing in the import closure. Its effect can't be proven absent, so any
        # non-.py change forces a full re-run (the manifest case above is the named instance of the
        # same blind spot; this catches every other one).
        if any(not str(c).endswith(".py") for c in changed):
            return None
        reachable = self._stage_reachable_files(stages[:fi], workdir)
        if reachable is None:                         # an OPAQUE earlier stage (python -m/shell) → re-train
            return None
        if changed & reachable:                       # a change could affect an earlier stage → re-train
            return None
        return failed_stage

    # The CONTRACT clause, appended to the sanity system prompt ONLY for a stage that DECLARED an
    # `expect.assert`. Hoisted out of the closure because it is the one place the quality-judgement
    # ban is deliberately narrowed, and a narrowing of a ban has to be readable on its own.
    #
    # Why this is not the thing the base prompt forbids. The ban exists because a checker with no
    # contract has nothing to judge against except its own taste, and taste produced a measured
    # incident: it failed the run's BEST model for having a loss around 14.6. A DECLARED condition is
    # not taste — it is the declarer's own sentence about what this stage was for, and checking a
    # stage against its own declaration is the opposite of ranking it against other stages' results.
    # The clause therefore SEPARATES the two rather than relaxing one: the declared condition is
    # judged, everything not covered by it keeps the ban verbatim.
    #
    # A stage's declared condition is the ONLY thing the checker may fail it for beyond the hard
    # failures — it may not invent a second condition it thinks the stage should also have met.
    STAGE_CONTRACT_CLAUSE = (
        " THIS STAGE ALSO DECLARED A SUCCESS CONDITION — the sentence under DECLARED CONDITION "
        "below, written by whoever declared the pipeline. Check the stage's output against THAT "
        "sentence and answer `FAIL declared_condition_violated: …` when the output shows the "
        "condition was not met (e.g. it "
        "declares coverage of at least 90% of the training queries and the log reports 9,364 of "
        "764,676). This is NOT the quality judgement forbidden above and does not relax it: you are "
        "not ranking this result against any other run, you are checking whether the stage did the "
        "job it declared. Judge ONLY the declared condition — do not invent a second one. If the "
        "output does not say either way, reply OK: an unstated number is not a violation.")

    # --- ROLLBACK: "this stage's failure is an EARLIER stage's fault" -----------------------------
    # The refusal vocabulary, hoisted so the ladder has a truth table and so the strings a Developer
    # reads back are pinnable. Each is a REASON THE ROLLBACK CANNOT HELP, not a reprimand: the text
    # goes into the next repair prompt, so it has to leave the model with something to do instead.
    # `{suspect}`/`{failed}`/`{names}`/`{script}` are the only placeholders.
    ROLLBACK_UNKNOWN = ("cannot roll back to stage {suspect!r}: this node's pipeline has no such "
                        "stage. Its stages, in order, are: {names}.")
    ROLLBACK_NOT_EARLIER = ("cannot roll back to stage {suspect!r}: it is not EARLIER than the stage "
                            "that failed ({failed!r}). Rolling back means re-running a stage that "
                            "ALREADY COMPLETED because you believe its output is what broke the later "
                            "one; re-running the failing stage itself is what the ordinary repair "
                            "already does.")
    ROLLBACK_REPEATED = ("cannot roll back to stage {suspect!r} again: this node has already re-run "
                         "it once on that same suspicion and the failure came back. Re-running an "
                         "expensive stage a second time on the same guess is not a new hypothesis — "
                         "either fix the failing stage {failed!r} directly, name a DIFFERENT earlier "
                         "stage, or say you no longer know what to change.")
    ROLLBACK_NO_CHANGE = ("cannot roll back to stage {suspect!r}: this repair changed nothing that "
                          "stage reads or runs, so re-running it would produce byte-identical output "
                          "and cost its full runtime for nothing. To roll back to a stage you must "
                          "also CHANGE it — edit the script it runs (or something that script "
                          "imports) in the SAME repair that names it.")
    ROLLBACK_NO_PIPELINE = ("cannot roll back to stage {suspect!r}: this node has no declared stage "
                            "pipeline — the operator's command runs as ONE step, so there is no "
                            "earlier stage to re-run. Fix the code that command runs.")
    ROLLBACK_PROTECTED = ("cannot roll back to stage {suspect!r}: it runs {script}, which is "
                          "PROTECTED — the operator owns that file and no edit you make can change "
                          "it, so re-running the stage would produce exactly what it produced before. "
                          "This is NOT something you can fix. If that stage's output really is wrong, "
                          "the operator has to change {script} or unprotect it; meanwhile fix what you "
                          "CAN reach, or report that you cannot.")

    def _rollback_start(self, stages: list, failed_stage, suspect, changed_files, workdir,
                        already_rolled_back=(), cwd=None):
        """Should this repair RE-RUN an earlier, already-successful stage — and if not, why not?

        Returns ``(start_stage, refusal)``: exactly one is non-None. `start_stage` is the suspect's
        name, to be used as the next eval's `start_stage`, which re-runs the pipeline FROM there and
        so discards every artifact the suspect and everything after it produced.

        THE GAP THIS FILLS. `next_start` and `_safe_reuse_start` only ever move the start point
        FORWARD: they answer "which completed stages may I skip". Neither can express the opposite
        claim, which is the one a train failure most often warrants — that the stage which BROKE is
        not the stage that is WRONG. On `runs/rubert-dr-0807` the `mine` stage exited 0 with 1.2%
        coverage and `train` was handed the result; every repair the loop could make was a repair to
        `train`, because naming `mine` was not something the Developer's action space could say.

        THE LADDER, and each rung is a way a rollback would be pure cost:
          1. UNKNOWN — the name is not in this node's pipeline. A typo must not silently re-run the
             whole pipeline from stage 0 (which is what `_run_stages` does with an unmatched
             `start_stage`, correctly, because for REUSE the fail-safe direction is to run MORE).
             Here the fail-safe direction is the opposite: refuse, and say what the stages are.
          2. NOT EARLIER — rolling back to the failed stage or a later one is either the ordinary
             retry or nonsense. Not an error worth spending a re-run on.
          3. REPEATED — this node already re-ran that stage on that suspicion. THE ANTI-THRASH RUNG,
             and the one that makes the whole feature bounded independently of any budget: a suspect
             may be tried at most once per node, so the worst case over a node's whole life is one
             re-run per stage, not one per attempt. Seeded from the durable log, so a resume does not
             hand back the allowance.
          4. NO CHANGE — the repair did not touch anything the suspect stage runs or imports, so
             re-running it produces the same bytes it produced before. This is the rung that makes
             "name it AND fix it" the contract rather than "name it and hope". It is decided with the
             SAME `_stage_reachable_files` closure `_safe_reuse_start` uses, read in the opposite
             direction: there, an intersection FORBIDS reuse; here, an EMPTY intersection forbids the
             re-run. Where the closure is OPAQUE (a `python -m` stage, a shell wrapper) it cannot
             prove nothing changed, so the rollback is ALLOWED — fail-open here, bounded by rungs 3
             and by the retrain cap the caller charges.
          5. PROTECTED — rung 4 with the reason named. When the suspect's script is one the operator
             protected, no repair can EVER satisfy rung 4, so the refusal has to name the operator's
             file instead of reading as "try harder". This composes with the preflight
             `_unrunnable_protected_scripts` rather than fighting it: that one refuses BEFORE the
             pipeline runs when a protected script is absent, this one refuses a re-run of a protected
             script that is present but unfixable. Both say the same thing — the operator is the only
             participant who can act — and neither asks the agent to edit a file the write gate
             refuses.

        A non-default eval `cwd` skips rung 4/5 (allowing the rollback): changed-file keys and stage
        script paths resolve against different bases there, exactly as `_safe_reuse_start` documents,
        so the intersection proves nothing in EITHER direction. Allowing is the right default for an
        unprovable rung because rung 3 still bounds it; refusing would make rollback permanently
        unavailable on every subdir-cwd task.
        """
        names = [str(s.get("name")) for s in (stages or [])]
        suspect = str(suspect or "").strip()
        if not suspect:
            # THE COMMON CASE, and it must be free and SILENT: an empty ask is not a request, so it
            # neither spends an allowance nor puts engine text in front of the next repair.
            return None, None
        _fmt = {"suspect": suspect, "failed": str(failed_stage or ""),
                "names": ", ".join(names) or "(none)", "script": ""}
        if not names:
            # Asked for on a single-command eval. Distinct from the silent case above: the Developer
            # DID ask, so it gets an answer — an empty refusal would leave it re-asserting the same
            # request every attempt with nothing to learn from.
            return None, self.ROLLBACK_NO_PIPELINE.format(**_fmt)
        if suspect not in names:
            return None, self.ROLLBACK_UNKNOWN.format(**_fmt)
        si = names.index(suspect)
        # A failed stage the pipeline no longer contains (a repair renamed it) leaves `fi` at -1, so
        # `si < fi` is False and the rollback is refused as NOT EARLIER. That is the right answer:
        # with the failure's position unknown we cannot show the suspect precedes it, and this is the
        # rung whose false ACCEPT costs a full re-run of everything from `si` onward.
        fi = names.index(str(failed_stage)) if str(failed_stage) in names else -1
        if fi < 0 or si >= fi:
            return None, self.ROLLBACK_NOT_EARLIER.format(**_fmt)
        if suspect in set(already_rolled_back or ()):
            return None, self.ROLLBACK_REPEATED.format(**_fmt)
        if cwd not in (None, "", "."):
            return suspect, None                  # unprovable rung 4/5 — see the docstring
        changed = {(f[2:] if isinstance(f, str) and f.startswith("./") else f)
                   for f in (changed_files or [])}
        reachable = self._stage_reachable_files([stages[si]], workdir)
        if reachable is None:
            return suspect, None                  # OPAQUE stage — cannot prove nothing changed
        if changed & reachable:
            return suspect, None                  # the repair really did change what this stage runs
        # Nothing this stage reaches was changed. Say WHY it can never be, when that is the case: a
        # protected script is the one shape where rung 4 is not advice but a permanent condition, and
        # a Developer told only "change it first" would spend every remaining attempt trying to.
        prot = set((self._repo_spec or {}).get("protected_names") or [])
        blocked = sorted(p for p in reachable if p in prot)
        if blocked:
            return None, self.ROLLBACK_PROTECTED.format(**{**_fmt, "script": blocked[0]})
        # A non-.py change is invisible to the import closure (`_stage_reachable_files` only bounds
        # PYTHON imports), so an empty intersection does not prove the stage's inputs are unchanged —
        # a rewritten config.yaml or params file the stage reads would not appear. Unprovable, so
        # allow, exactly as the opaque and non-default-cwd cases do. Note the direction is the mirror
        # image of `_safe_reuse_start`'s treatment of the same blind spot, and deliberately so: there
        # an unprovable change forces a re-train (fail closed against a stale score), here it permits
        # one (fail open toward doing the work), because the two mistakes cost different things — a
        # wrong reuse silently scores a stale model, a wrong re-run only spends time.
        if any(not str(c).endswith(".py") for c in changed):
            return suspect, None
        return None, self.ROLLBACK_NO_CHANGE.format(**_fmt)

    def _deadline_grace_fn(self, node):
        """The ONE-SHOT deadline judge: a callback `(log_tail) -> seconds` handed to
        `run_command_eval`, or None when the feature is off / no client is wired.

        THE DECISION IT MAKES is "kill this stage now, or give it a little longer" — what happens
        NEXT, recoverable and re-checkable, and it touches nothing in the record. The metric still
        comes from the operator's own reader over the protected `score` stage; a graced stage that
        still misses the extended wall reports `timed_out` exactly as it always did; and the seconds
        actually bought are stamped on the stage row from the out-of-band `signals`, never read back
        out of the log.

        WHY IT IS NOT A THRESHOLD. All four `stage_finished.status == "timeout"` rows in the corpus
        sit exactly on their 4 h / 6 h / 8 h wall and discarded 22.0 GPU-hours between them, and the
        whole captured record of `rubertlite-dense-retrieval` node 72 — 12.45 hours, five repairs —
        ends `100%|##########| 664/664 [00:17<00:00, 38.13it/s]`. At the wall, "two seconds from a
        checkpoint" and "will never finish" are the identical fact. Raising the budget instead does
        not help: it moves the same cliff later and pays for every genuinely hung run on the way.

        WHAT BOUNDS IT, because a judge reading the CANDIDATE'S OWN log is exactly the input that
        must not be trusted with anything expensive: the answer is a single word, never a number
        (`parse_deadline_reply` — fail-closed, so an unreadable reply kills as before); the seconds
        come from the OPERATOR'S `eval_deadline_grace_s`; `sandbox._granted_grace` clamps them again
        in the runtime; the judge is asked AT MOST ONCE per command; an operator `cancel` is never
        graced; and the whole thing is OFF by default. So a solution that prints a convincing
        progress bar buys `eval_deadline_grace_s` seconds, once, and no other thing."""
        cap = float(getattr(self, "eval_deadline_grace_s", 0.0) or 0.0)
        if cap <= 0:
            return None
        try:
            client = self._reflect_client()
        except Exception:  # noqa: BLE001
            client = None
        if client is None:
            return None
        idea_text = self._idea_text(node.idea) if node is not None else ""

        # EVAL-PATH, not background — the same reasoning as `_stage_check_fn`: this blocks the eval
        # worker at the deadline and there is one per concurrent eval.
        @in_llm_lane("engine")
        def _judge(tail: str) -> float:
            msgs = [{"role": "system", "content":
                     "An ML pipeline stage has just reached its wall-clock time budget and is about "
                     "to be killed, discarding everything it has done. You are shown the tail of its "
                     "LIVE log. Decide ONE thing: is this stage within a few minutes of completing "
                     "the work it is doing, or not? Evidence FOR: a progress bar at or near 100%, a "
                     "final epoch/step counter almost at its total, a checkpoint or evaluation "
                     "already being written. Evidence AGAINST: early progress, a repeating error, no "
                     "movement, or a log you cannot read progress from. Default to NOT_FINISHING — "
                     "the extension is granted once and is paid for in GPU time, so guessing costs "
                     "real money, while being wrong the other way costs one stage that was going to "
                     "be killed anyway. Reply with EXACTLY one word: FINISHING or NOT_FINISHING."},
                    {"role": "user", "content":
                     f"Experiment: {idea_text[:400]}\n\nLive log tail:\n{tail}"}]
            try:
                out = (client.complete_text(msgs) or "").strip()
            except Exception:  # noqa: BLE001 — a judge failure must never turn a timeout into a crash
                return 0.0
            return cap if parse_deadline_reply(out) else 0.0

        return _judge

    def _stage_check_fn(self, node):
        """Phase 3 inter-stage verify: a callback (stage_name, log_tail, expect="") -> verdict|None that
        asks an LLM whether a `check`-flagged stage physically SUCCEEDED (train actually trained + saved a
        checkpoint, no silent fallback) BEFORE the next stage runs. This is a SANITY gate, NOT a
        quality/ranking judgment: it must fail a stage ONLY on a hard, unambiguous failure — never because
        the metric looks 'not good enough' or 'below the previous best' (that is the search's job
        downstream). A real incident motivated the tightening: the checker failed the run's BEST model (val
        recall@100 ≈ 0.855, above the champion) by (a) reading loss MAGNITUDE (~14.6) as 'no learning' —
        loss scale depends on the loss/temperature and is not comparable across configs — and (b) grabbing a
        bystander scalar (val recall@50 = 0.79) as 'the metric' and calling it below-best. Returns None
        (checks skipped) when no client is available. Runs inside the eval worker thread, so
        complete_text blocks there — fine, like the eval.

        `expect` is the stage's own DECLARED success condition
        (`runtime/command_eval.py::_validate_expect`), passed by `_call_stage_check` when the manifest
        carries one. It is the ONE thing the checker may fail a stage for beyond a hard failure, and the
        reason the ban above is not simply wrong: with no contract, "did this work?" and "is this good?"
        are the same question and the checker answered the wrong one. Empty `expect` narrows the
        vocabulary rather than the prompt: `declared_condition_violated` is not offered and is refused
        on the way back in (`parse_stage_check_reply`), so a stage using only `check: true` can be
        failed for physical failure and nothing else.

        WHAT IT RETURNS is a `StageCheckVerdict`, not a concern string, and that is the 2026-08-13
        change: the tightening above was PROMPT-ONLY and did not hold. `rubertlite-dense-retrieval`
        node 21 was killed after the tightening with "validation recall (0.79) is below previous best
        (0.8491)" — the banned comparison, verbatim. Prose can no longer end a node; only a named
        member of `command_eval.STAGE_CHECK_HARD_KINDS` can. Everything else is `inconclusive` and
        the pipeline continues."""
        try:
            client = self._reflect_client()
        except Exception:  # noqa: BLE001
            client = None
        if client is None:
            return None
        idea_text = self._idea_text(node.idea) if node is not None else ""
        # Name the run's OBJECTIVE metric (from the operator's metric reader) so the checker judges the
        # RIGHT number and isn't misled by a bystander scalar in the log (e.g. recall@50 vs the @100 goal).
        _ms = (self._eval_spec or {}).get("metric") or {}
        objective = (str(_ms.get("pattern") or _ms.get("key") or "").split("(")[0].strip()
                     or "the objective metric")

        # EVAL-PATH, not background (`core/llm_broker.py::BACKGROUND_LANE_PRODUCERS`). This ran in
        # the capped `enrichment` lane beside the two live-log watchdogs, but it is the opposite kind
        # of producer: the eval worker BLOCKS on it between stages (see the docstring above), there is
        # one per concurrent eval, and the whole batch inherits its latency. Once the background caps
        # were unwelded from the global total, N evals' stage checks serialized at one and each queued
        # behind whichever watchdog held the permit — measured at 4 concurrent evals: peak 1, 4x the
        # wall time. `engine` is where its eval-path siblings already live (the repair loop reaches it
        # as the fallback lane), and it stays governed by a finite total when the operator sets one.
        @in_llm_lane("engine")
        def _check(stage_name, tail, expect: str = ""):
            msgs = [{"role": "system", "content":
                     "You are a SANITY checker for ONE stage of an ML eval pipeline, run BEFORE the next "
                     "stage. Decide ONLY whether this stage physically SUCCEEDED and produced a usable "
                     "artifact for the next stage. FAIL it ONLY on a HARD, unambiguous failure: a "
                     "crash/traceback, no checkpoint saved, a silent fallback to a stale/pretrained "
                     "model, a NaN/inf loss, or a loss LITERALLY UNCHANGED from the first training step "
                     "(genuinely no learning). Do NOT judge result QUALITY or RANKING: a trained model "
                     "whose metric is merely mediocre, or BELOW a previous run, still PASSES — the search "
                     "ranks and selects downstream, never you. Loss MAGNITUDE is NOT a failure signal (it "
                     "depends on the loss function and temperature; a loss around 14 can be perfectly "
                     "healthy). A present, non-trivial validation metric is strong evidence the stage "
                     "SUCCEEDED. Answer in ONE line, in one of exactly three forms:\n"
                     "  OK — the stage succeeded.\n"
                     "  FAIL <kind>: <one-line evidence> — where <kind> is EXACTLY one of "
                     "crash, nan_or_inf_loss, no_artifact_written, silent_fallback, "
                     "loss_unchanged_from_first_step"
                     + (", declared_condition_violated" if expect else "") + ".\n"
                     "  INCONCLUSIVE: <what you would need to see> — when the output does not let "
                     "you tell. USE THIS FREELY. A stage that printed little or nothing is "
                     "INCONCLUSIVE, not a failure: many stages legitimately do their work quietly, "
                     "and this pipeline has already verified on disk that the stage produced every "
                     "artifact it declared. FAIL only when the evidence for one of the kinds above "
                     "is actually IN the output; if your reason would not fit one of those kinds, "
                     "it is INCONCLUSIVE."
                     + (EvalStagesMixin.STAGE_CONTRACT_CLAUSE if expect else "")},
                    {"role": "user", "content":
                     f"The run's objective metric is `{objective}` — ignore other scalars when judging "
                     f"whether the stage worked.\nExperiment: {idea_text[:400]}\n\n"
                     + (f"DECLARED CONDITION for stage '{stage_name}': {expect}\n\n" if expect else "")
                     + f"Stage '{stage_name}' output tail:\n{tail}"}]
            try:
                out = (client.complete_text(msgs) or "").strip()
            except Exception:  # noqa: BLE001 — a checker failure must never fail the eval
                return None
            return parse_stage_check_reply(out, declared=bool(expect))

        return _check
