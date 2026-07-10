"""The write-tool half of the in-house repo Developer, split out of `adapters/repo_developer.py`
along the tool-vs-persona boundary (docs/15 mega-refactor): `RepoWriteTools` (the surface-gated
write/edit/delete/declare_stages tool provider whose writes are COLLECTED into `self.files`, not
applied — the orchestrator materializes them into the node workdir), the stage-input validators
it shares with the persona's STAGES phase (`_missing_stage_input_paths` /
`_missing_paths_feedback` and their helpers), and the `_xlsx_to_markdown` results renderer the
persona's results context uses.

The persona half (`LLMRepoDeveloper`, `LLMOnboarder` and the prompt constants) stays in
`repo_developer.py`, which re-imports these names — so `looplab.adapters.repo_developer` (and
`repo_task`'s own re-export chain on top of it) keeps exporting them, and this module needs
nothing from `repo_developer` at import time (no cycle)."""
from __future__ import annotations

import os
import re
from typing import Optional

from looplab.tools.edit_match import apply_search_replace
from looplab.tools.patch import SurfacePolicy

# Absolute paths to INPUT data files referenced in a stage command. Only clear INPUT-data extensions
# (a checkpoint .ckpt/.pt an earlier stage WRITES is deliberately excluded, and relative paths resolve
# to mounts at eval time so are left to the eval). Used to catch the #1 real failure: a train stage
# pointing at a hallucinated argparse-default `.pck` that isn't on this machine.
# The leading `/` must be a TRUE absolute-path boundary — the negative lookbehind rejects a `/` that is
# part of a relative `./dir/...` or `a/b/...` (those resolve to mounts/workdir at eval time, not here).
_INPUT_DATA_RE = re.compile(
    r"(?<![\w./~])/[^\s\"',:]+\.(?:pck|parquet|csv|tsv|npy|npz|pkl|arrow|jsonl|feather|h5|hdf5)")

# A flag names an OUTPUT (a path the stage WRITES) when it contains one of these hints — matched on the
# de-dashed flag so ANY spelling works (`--outdir`, `--export-dir`, `--save_to`, `--dest`, `--dump`,
# `--write-to`), replacing the old hardcoded list that missed `--outdir`/`--export-dir`/`--dump`.
_OUTPUT_HINT_RE = re.compile(r"(out|save|dest|export|dump|writ)", re.I)


def _stage_output_values(cmd) -> list[str]:
    """Path values a single stage WRITES: the token after an output-ish flag, or the RHS of
    `--outflag=VAL`. These are pipeline intermediates a LATER stage reads (and are this stage's OWN
    outputs, not its inputs), so they must not be flagged as "missing input" at declare time."""
    out: list[str] = []
    for i, tok in enumerate(cmd):
        if not isinstance(tok, str):
            continue
        if tok.startswith("-") and "=" in tok:
            flag, val = tok.split("=", 1)
            if _OUTPUT_HINT_RE.search(flag.lstrip("-")):
                out.append(val)
        elif tok.startswith("-") and _OUTPUT_HINT_RE.search(tok.lstrip("-")) \
                and i + 1 < len(cmd) and isinstance(cmd[i + 1], str):
            out.append(cmd[i + 1])
    return out


def _covered_by(m: str, produced: list) -> bool:
    """True when absolute path `m` equals, or lives under, a path some stage PRODUCES (exact match, or
    `m` under a produced DIRECTORY like `--outdir /x/prep` covering `/x/prep/train.npy`)."""
    for v in produced:
        if v == m or m.startswith(v.rstrip("/") + "/"):
            return True
    return False


def _missing_stage_input_paths(stages) -> list[str]:
    """ABSOLUTE input-data paths referenced in stage commands that DON'T exist on disk — almost always
    a hallucinated default (the recurring failure: a train stage's `--train_dataset /…/train.pck` that
    was copied from the repo's argparse default and isn't here). Absolute paths are location-invariant,
    so a declare-time existence check is sound; relative paths (mounts) and `%params%` are skipped.
    A path an EARLIER stage PRODUCES (or a parent dir of one), or this stage's OWN output, is excluded —
    a valid data_prep→train pipeline's intermediate legitimately doesn't exist yet at declare time. The
    check is stage-ORDER-aware: only outputs of stages at-or-before the reader count, so a read-before-
    write ordering (train reads what a LATER export writes) is still flagged as the FileNotFoundError
    it is."""
    missing: list[str] = []
    produced: list = []                     # output paths of stages processed so far (order-aware)
    for s in (stages or []):
        if not isinstance(s, dict):
            continue
        cmd = [t for t in (s.get("command") or []) if isinstance(t, str)]
        own_outputs = _stage_output_values(cmd)
        known = produced + own_outputs      # this stage's own outputs are not its inputs
        for tok in cmd:
            if "%params%" in tok:
                continue
            for m in _INPUT_DATA_RE.findall(tok):
                if m in missing or _covered_by(m, known):
                    continue
                if not os.path.exists(m):
                    missing.append(m)
        produced.extend(own_outputs)        # available to every stage that FOLLOWS
    return missing


def _missing_paths_feedback(missing: list[str]) -> str:
    """The actionable bounce message shown to the Developer so it re-declares with a real path.
    Deliberately does NOT tell it to list/inspect the data itself: its scout tools reach ONLY the
    editable repo (mounted inputs materialize in per-node EVAL workdirs it cannot see from here), so
    the old "`list_dir` the actual data" advice just burned the phase's retries on "(path not
    allowed…)" refusals (P13). The authoritative source for a data path is the task/goal/data brief."""
    return ("these data paths in your stage command(s) DO NOT EXIST on this machine: "
            + ", ".join(missing[:5]) + ". Do NOT use the repo's DEFAULT argparse dataset paths — they "
            "are the original author's and aren't here. Take the dataset path from the task/goal/data "
            "brief VERBATIM (mounted inputs appear at ./<name> in the EVAL workdir at run time — your "
            "scout tools cannot list them here) and use it in the stage command, spelled exactly as "
            "given. (If a path is produced by an EARLIER stage, reference it relatively.)")


class RepoWriteTools:
    """Write side of the in-house repo developer (the LLM authors/edits files via tools). Writes are
    COLLECTED into `self.files` (path -> content) rather than applied to disk — the orchestrator
    materializes them into the node workdir as the node's files, surface-gated + protected-skipped
    just like an external coding agent's diff. The SAME gates are enforced here so the model gets
    immediate feedback (a refused write) instead of having the edit silently dropped downstream."""

    def __init__(self, surface, protected, prefixes=None, editables=None,
                 operator_stages: bool = False, data_mounts=None):
        self.files: dict[str, str] = {}
        self.deleted: list[str] = []
        self._surface = list(surface or [])
        self._protected = set(protected or [])
        self._prefixes = list(prefixes or [])
        # The OPERATOR declared this task's pipeline via `cmd.stages`: the engine runs it verbatim
        # and IGNORES any Developer manifest (_resolve_stages prefers a valid operator list), so
        # declare_stages must REFUSE instead of "succeeding" into a file nobody reads — a repair that
        # "fixed" a stage timeout via the manifest otherwise loops the identical failure to abandon
        # (mega-review P12).
        self._operator_stages = bool(operator_stages)
        # Names of read-only DATA MOUNTS (they sit in the protect list defensively — see
        # RepoTask._protected_names) so a refused write can name the REAL reason: "read-only data
        # mount, write derived data elsewhere" rather than the misleading "the operator owns the eval".
        self._data_mounts = [str(n).rstrip("/") for n in (data_mounts or []) if n]
        # Editable repo roots ({name,path}...) so edit_file can patch a file the node hasn't staged
        # yet: current content = staged overlay first, else the original file on disk.
        self._roots = [(e.get("name") or "", e.get("path")) for e in (editables or []) if e.get("path")]

    def _current(self, p: str):
        """The file's CURRENT content for patching: the staged overlay wins (parent files pre-seeded
        by implement_from, or an earlier write this turn), else the original from an editable root.
        Staged paths are workdir-relative and PREFIXED with the editable's name in multi-editable
        setups (the repo mounts at wd/<name>), so strip the owning prefix before joining its root —
        a bare join would probe <root>/<name>/<file> and read a missing (or wrong) original."""
        if p in self.files:
            return self.files[p]
        from pathlib import Path as _P
        for name, r in self._roots:
            rel = p[len(name) + 1:] if name and name != "." and p.startswith(name + "/") else p
            f = _P(r) / rel
            try:
                if f.is_file():
                    return f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        return None

    @staticmethod
    def _safe_rel(p: str):
        """Canonicalize to a REPO-RELATIVE path or None. Rejects absolute paths and `..` escapes so
        the agent can only stage files inside the repo it edits — without this, an absolute path like
        `/tmp/x.py` slips past a `**/*.py` surface glob (fnmatch's `*` crosses `/`) and the write is
        silently dropped downstream (it's outside the node workdir)."""
        p = str(p or "").replace("\\", "/").strip()
        while p.startswith("./"):
            p = p[2:]
        if not p or p.startswith("/") or p.startswith("~") or p == ".." \
                or p.startswith("../") or "/../" in p:
            return None
        return p

    def specs(self) -> list[dict]:
        from looplab.tools._base import fn_spec
        return [
            fn_spec("edit_file",
                     "Edit an EXISTING file with a minimal SEARCH/REPLACE patch — STRONGLY PREFERRED "
                     "over write_file for changing existing code: it is far faster and safer than "
                     "re-generating a whole file. `search` must be copied EXACTLY (including "
                     "whitespace/indentation) from the file's current content and must occur exactly "
                     "once; `replace` is its replacement. Make several small edit_file calls for "
                     "several changes. Use write_file only for NEW files.",
                     {"path": {"type": "string", "description": "repo-relative path"},
                      "search": {"type": "string", "description": "exact existing snippet (unique in the file)"},
                      "replace": {"type": "string", "description": "the replacement snippet"}},
                     ["path", "search", "replace"]),
            fn_spec("write_file",
                     "Create or OVERWRITE a file in the experiment repo you are editing. Provide the "
                     "FULL file content (not a diff, not a shell command). Use this ONLY to author the "
                     "eval entrypoint and code edits — NOT to inspect files. Path is REPO-RELATIVE "
                     "(e.g. test_looplab.py); absolute paths and paths outside the repo are rejected.",
                     {"path": {"type": "string", "description": "repo-relative path, e.g. test_looplab.py"},
                      "content": {"type": "string", "description": "the complete file content"}},
                     ["path", "content"]),
            fn_spec("delete_file",
                     "Delete a file you previously wrote in this experiment (within your surface).",
                     {"path": {"type": "string"}}, ["path"]),
            # The pipeline is AUTHORED in the Developer's dedicated STAGES phase (its `declare_stages`
            # emit) BEFORE implement — but a write session still needs a validated route to FIX the
            # manifest: a repair whose root cause is a bad stage (wrong argv / too-low timeout) has no
            # other way to change it (write_file refuses under the default *.py surface; without this
            # spec every repair repeats the identical stage failure until abandon — mega-review D1).
            fn_spec("declare_stages",
                     "FIX the eval pipeline manifest (looplab_stages.json). The stages were already "
                     "declared in the STAGES phase — call this ONLY when the failure you are fixing is "
                     "IN the pipeline itself (a stage's command/timeout/name is wrong), passing the "
                     "FULL corrected ordered list of preceding stages (the operator's protected `score` "
                     "step stays appended after them). `%params%` in a command injects this node's "
                     "hyperparameters; give a long `train` a generous `timeout` (seconds). It VALIDATES "
                     "the manifest and reports errors back. Do not use it to re-plan working stages."
                     + (" NOTE: THIS task's pipeline is OPERATOR-declared (`cmd.stages`) and runs "
                        "verbatim — this tool will refuse; fix the failing stage's script instead."
                        if self._operator_stages else ""),
                     {"stages": {"type": "array", "description":
                                 "ordered preceding stages, each {name, command:[argv...], timeout?, check?}"}},
                     ["stages"]),
        ]

    def execute(self, name: str, args: dict) -> str:
        args = args or {}
        if name == "declare_stages":
            return self._declare_stages((args or {}).get("stages"))
        p = self._safe_rel(args.get("path", ""))
        if name == "write_file":
            return self._write(p, args)
        if name == "edit_file":
            return self._edit(p, args)
        if name == "delete_file":
            return self._delete(p)
        return f"(unknown tool: {name})"

    def _declare_stages(self, stages) -> str:
        """Validate + stage a `looplab_stages.json` of PRECEDING stages. Reserves the final `score`
        stage for the operator's cmd (appended by the engine), so a Developer can add train/prep work
        but never rewrite the scoring. Returns a clear error string on any problem (nothing staged) so
        the tool loop gets actionable feedback instead of the silent malformed-manifest fallback."""
        import json
        # OPERATOR-declared `cmd.stages` pipelines run VERBATIM: the engine's _resolve_stages takes a
        # valid operator list and never reads the Developer manifest, so "declaring" one here would
        # succeed into a file nobody consumes and the repaired node would re-run the identical
        # pipeline until abandon (P12). Refuse with the real route to a fix.
        if self._operator_stages:
            return ("(refused: this task's pipeline is OPERATOR-declared (`cmd.stages`) and runs "
                    "verbatim; the manifest cannot change it — fix the failing stage's script/code "
                    "instead)")
        # The manifest itself is TOOL-OWNED (validated here, engine-validated again at consume time):
        # gate it on the PROTECT list only — an operator may explicitly protect 'looplab_stages.json'
        # to disable Developer pipelines — NOT on the edit surface. The legacy default surface is
        # ["**/*.py"], which no root .json file can ever match, so the old surface gate made this
        # REQUIRED tool refuse on every legacy repo task (the prompt mandates it for training runs).
        # The surface still governs the STAGE SCRIPTS the manifest points at (write_file), and the
        # declared commands run under the same sandbox tier as the eval — declaring a stage grants
        # nothing an in-surface .py edit (imported by the eval) couldn't already run.
        reason = SurfacePolicy(None, self._protected, self._prefixes,
                               protected_exact=True, check_escapes=False).check("looplab_stages.json")
        if reason is not None:
            return ("(refused: looplab_stages.json is protected — the operator owns the eval; "
                    "you may not declare stages in it)")
        # The shared stage rules (runtime/command_eval.validate_stages) — the SAME validator the
        # engine's _resolve_stages runs at consume time, so a manifest this tool accepts is never
        # silently re-filtered engine-side. The refusal strings stay byte-identical to the original
        # inline loop (the model steers on them): validate_stages returns the bare reason, this site
        # wraps it in its historical "(refused: …)" envelope.
        from looplab.runtime.command_eval import validate_stages
        clean, err = validate_stages(stages, reserved=("score",))
        if err is not None:
            return f"(refused: {err})"
        miss = _missing_stage_input_paths(clean)      # catch a hallucinated non-existent data path
        if miss:
            return f"(refused: {_missing_paths_feedback(miss)})"
        self.files["looplab_stages.json"] = json.dumps({"stages": clean}, indent=1)
        chain = " → ".join(s["name"] for s in clean) + " → score (operator cmd)"
        return f"declared {len(clean)} preceding stage(s): {chain}"

    def _refusal(self, p: str, verb: str):
        """Run the shared SurfacePolicy (tools/patch.py) over an already-canonicalized path and map
        its reason codes onto THIS tool's historical refusal strings (byte-identical — the model
        steers on them). `p` came through `_safe_rel`, which is this site's escape gate — hence
        `check_escapes=False`: _safe_rel's rules differ from patch._escapes (it also strips `./`,
        rejects `~`, and accepts a drive-letter path on POSIX). Protected matching is EXACT and
        case-sensitive here (`protected_exact=True`) — the protect entries arrive pre-normalized
        from RepoTask._protected_names — unlike the diff gate's case-insensitive globs. Prefixes
        pass through VERBATIM (no rstrip); see SurfacePolicy's docstring. Returns None when the
        write may proceed."""
        reason = SurfacePolicy(self._surface, self._protected, self._prefixes,
                               protected_exact=True, check_escapes=False).check(p)
        if reason == SurfacePolicy.PROTECTED:
            # Two distinct situations land in PROTECTED: the operator's eval/scorer files, and a
            # read-only DATA MOUNT (protected defensively so the write refuses VISIBLY — see
            # RepoTask._protected_names). Name the real reason so the model takes the right next
            # step: leave the scorer alone vs write derived data to a different path.
            for nm in self._data_mounts:
                if p == nm or p.startswith(nm + "/"):
                    return (f"(refused: {p} is a read-only data mount; you may not {verb} the "
                            "original — write derived/processed data to a different path)")
            return f"(refused: {p} is protected — the operator owns the eval; you may not {verb} it)"
        if reason is not None:
            return f"(refused: {p} is outside your editable surface: {', '.join(self._surface)})"
        return None

    @staticmethod
    def _py_syntax_error(path: str, content: str) -> Optional[str]:
        """Auto-validator (aider/Claude-Code style: compile after every edit, feed the error straight
        back). For a *.py result, the first compile() error as "line N: msg", else None. Uses
        compile() (not ast.parse) so it ALSO catches the AST-validation errors ast.parse lets through
        — a repeated keyword arg, `return` outside a function, an unmatched paren, a duplicate param.
        The eval sandbox for a repo task runs on THIS interpreter, so ANY compile error here means the
        code won't run there either — hence ALL of them are hard-rejected, not just indentation (a
        stray `unmatched ')'` crashed a real training run). The rare cost: a Docker tier on a NEWER
        Python could reject valid PEP-695-style syntax — acceptable, and the developer should target
        the run's interpreter anyway."""
        if not path.endswith(".py"):
            return None
        try:
            compile(content, path, "exec")
            return None
        except SyntaxError as e:           # IndentationError/TabError subclass this
            return f"line {e.lineno}: {e.msg}"
        except ValueError as e:            # source with NUL bytes etc. — genuinely unrunnable
            return str(e)[:80]

    def _write(self, p, args: dict) -> str:
        if not p:
            return ("(refused: path must be REPO-RELATIVE and inside the repo — no absolute paths, "
                    "no `..`. Write the eval entrypoint, e.g. write_file path='test_looplab.py'.)")
        refusal = self._refusal(p, "modify")
        if refusal:
            return refusal
        content = args.get("content", "")
        err = self._py_syntax_error(p, content)
        if err is not None:
            return (f"(refused: the content you wrote for {p} is not valid Python — {err}. "
                    "Fix the syntax and write_file again; nothing was staged.)")
        self.files[p] = content
        if p in self.deleted:
            self.deleted.remove(p)
        return f"wrote {p} ({len(content)} bytes)"

    def _edit(self, p, args: dict) -> str:
        if not p:
            return ("(refused: path must be REPO-RELATIVE and inside the repo — no absolute paths, "
                    "no `..`.)")
        refusal = self._refusal(p, "modify")
        if refusal:
            return refusal
        cur = self._current(p)
        if cur is None:
            return (f"(no such file to edit: {p} — it is neither staged this turn nor in the repo. "
                    "Create it with write_file instead.)")
        search = str(args.get("search") or "")
        replace = str(args.get("replace") or "")
        # Exact-match + whitespace-tolerant line-anchored fallback live in tools/edit_match.py
        # (shared, delicate, test-covered); this method only stages the result.
        new, msg = apply_search_replace(cur, search, replace, path=p)
        if new is None:
            return msg
        # Auto-validate: reject an edit that INTRODUCES a compile error (bad indentation, an unmatched
        # paren, a repeated kwarg — all crashed real runs), but only when the ORIGINAL compiled
        # cleanly, so we never punish the model for editing an already-broken file. The error flies
        # straight back so the model fixes it NOW instead of ~112 min later as a training crash.
        cur_err = self._py_syntax_error(p, cur)          # None => original compiled cleanly
        new_err = self._py_syntax_error(p, new)
        if cur_err is None and new_err is not None:
            return (f"(refused: this edit makes {p} invalid Python — {new_err}. Check the "
                    "indentation/brackets of your `replace` block against the surrounding code and "
                    "try again. Nothing was staged.)")
        self.files[p] = new
        if p in self.deleted:
            self.deleted.remove(p)          # an edit resurrects a previously-deleted file
        return msg

    def _delete(self, p) -> str:
        if not p:
            return ("(refused: path must be REPO-RELATIVE and inside the repo — no absolute paths, "
                    "no `..`.)")
        # SAME gates as write_file: a deletion must not remove a protected file (the operator's
        # eval/metric/grader) or reach outside the editable surface. Without these, delete_file
        # was a hole around the write-surface enforcement.
        refusal = self._refusal(p, "delete")
        if refusal:
            return refusal
        self.files.pop(p, None)
        if p not in self.deleted:
            self.deleted.append(p)
        return f"deleted {p}"


def _xlsx_to_markdown(path: str, *, max_rows: int = 120, cap: int = 9000) -> Optional[str]:
    """Best-effort render of a results spreadsheet to a compact markdown table so an agent can read
    it (an .xlsx is opaque binary otherwise). Rows with numeric cells become table rows; free-text
    rows between them are folded into the preceding row's trailing 'notes' column (that's how these
    experiment logs are usually laid out). Returns None if openpyxl isn't installed or the file can't
    be read — never raises."""
    try:
        import openpyxl  # optional dependency; absent -> skip gracefully
    except Exception:  # noqa: BLE001
        return None
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb[wb.sheetnames[0]]
    except Exception:  # noqa: BLE001
        return None

    def _num(x):
        try:
            float(x); return True
        except (TypeError, ValueError):
            return False
    rows = []
    cur = None
    for r in ws.iter_rows(values_only=True):
        c = list(r)
        nums = [x for x in c[1:] if _num(x)]
        if c and c[0] not in (None, "") and nums:                 # a data row (label + numbers)
            cur = {"label": str(c[0]).strip(),
                   "vals": [("" if x is None else (round(float(x), 4) if _num(x) else str(x)))
                            for x in c[1:]],
                   "notes": []}
            rows.append(cur)
        elif cur is not None:                                     # a free-text note -> attach above
            note = " ".join(str(x).strip() for x in c if x not in (None, "")).strip()
            if note:
                cur["notes"].append(note)
        if len(rows) >= max_rows:
            break
    if not rows:
        return None
    ncol = max(len(r["vals"]) for r in rows)
    header = "| label | " + " | ".join(f"c{i+1}" for i in range(ncol)) + " | notes |"
    sep = "|" + "---|" * (ncol + 2)
    lines = [header, sep]
    for r in rows:
        vals = r["vals"] + [""] * (ncol - len(r["vals"]))
        notes = ("; ".join(r["notes"])[:200]).replace("|", "/")
        lines.append(f"| {r['label'][:60]} | " + " | ".join(str(v) for v in vals) + f" | {notes} |")
    return "\n".join(lines)[:cap]
