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
            clean, err = command_eval.validate_stages(task_stages)
            if err is None:
                return _expand(clean)

        # single-command cmd: read the Developer's PRECEDING stages, append the protected cmd stage
        dev = None
        mf = Path(workdir) / "looplab_stages.json"
        if mf.exists():
            try:
                data = json.loads(mf.read_text(encoding="utf-8"))
                dev = data.get("stages") if isinstance(data, dict) else data
            except Exception:  # noqa: BLE001 — a malformed manifest just falls back to the single command
                dev = None
        if isinstance(dev, list) and dev:
            # SAME shared rules as the declare_stages tool (reserved 'score', no duplicates, argv
            # shape) — the tool is the friendly authoring path, but write_file / a CLI-agent diff /
            # a pre-redesign run can still hand-write the manifest, and an unvalidated one could
            # smuggle a second 'score' stage (clobbering score.log and confusing stage-scoped
            # re-runs) or a full scorer that double-runs the eval after cmd is appended. Invalid →
            # ignore the whole manifest (single-command fallback), never a half-cleaned pipeline.
            preceding, err = command_eval.validate_stages(dev, reserved=("score",))
            if err is None and preceding:
                # the operator's cmd is the FINAL + protected scoring stage; reuse the already
                # profile-resolved command/timeout (build_command) so smoke/full still applies.
                final = {"name": "score",
                         "command": list(score_cmd) if score_cmd is not None
                                    else command_eval.expand_params(list(es.get("command") or []), params),
                         "timeout": score_timeout if score_timeout is not None else es.get("timeout", 600.0)}
                return _expand(preceding) + [final]
        return None

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

    def _stage_check_fn(self, node):
        """Phase 3 inter-stage verify: a callback (stage_name, log_tail) -> concern|None that asks an LLM
        whether a `check`-flagged stage physically SUCCEEDED (train actually trained + saved a checkpoint,
        no silent fallback) BEFORE the next stage runs. This is a SANITY gate, NOT a quality/ranking
        judgment: it must fail a stage ONLY on a hard, unambiguous failure — never because the metric
        looks 'not good enough' or 'below the previous best' (that is the search's job downstream). A
        real incident motivated the tightening: the checker failed the run's BEST model (val recall@100
        ≈ 0.855, above the champion) by (a) reading loss MAGNITUDE (~14.6) as 'no learning' — loss scale
        depends on the loss/temperature and is not comparable across configs — and (b) grabbing a
        bystander scalar (val recall@50 = 0.79) as 'the metric' and calling it below-best. Returns None
        (checks skipped) when no client is available. Runs inside the eval worker thread, so
        complete_text blocks there — fine, like the eval."""
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

        def _check(stage_name, tail):
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
                     "SUCCEEDED. Reply EXACTLY 'OK' if the stage succeeded, otherwise a ONE-LINE concern "
                     "naming the HARD failure."},
                    {"role": "user", "content":
                     f"The run's objective metric is `{objective}` — ignore other scalars when judging "
                     f"whether the stage worked.\nExperiment: {idea_text[:400]}\n\n"
                     f"Stage '{stage_name}' output tail:\n{tail}"}]
            try:
                out = (client.complete_text(msgs) or "").strip()
            except Exception:  # noqa: BLE001 — a checker failure must never fail the eval
                return None
            return None if (not out or out.upper().startswith("OK")) else out[:300]

        return _check
