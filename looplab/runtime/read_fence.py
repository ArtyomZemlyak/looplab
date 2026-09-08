"""The source-tree FENCE: make it impossible for a node's process to read — or CHANGE — the
operator's editable SOURCE tree.

It is still called the READ fence, because that is the defect it was built for and the name is in
`Settings.read_fence`, in every snapshot and in the docs. The mutation half arrived on 2026-08-13
(`docs/38-fence-coverage-audit-2026-08-13.md`) and is not an extension of the idea, it is the same
idea applied to the same paths: `open` is not the only way to touch a file.

WHY THIS EXISTS (the defect it closes, measured on `runs/rubertlite-dr-unified-v6`)
-----------------------------------------------------------------------------------
`expect` checks what a stage WRITES and never what it READS. Node 4 of that run — a merge node —
trained for 76 minutes and produced a genuinely good model (`train.log`: `RECALL@100: 0.726350`).
Its `vectorsearch/configs/config.yaml:215` carried

    checkpoint_path: /home/jovyan/data/vectorizer-unified/vectorsearch/experiments/…/final

an ABSOLUTE path into the operator's editable SOURCE tree, where a HUMAN's `model.safetensors` from
2026-07-18 sits. The protected scorer loaded that foreign model and scored it:
`score.log`: `RECALL@100: 0.224975` — and THAT is the number the run recorded as the node's metric.
Every existing gate passed: the artifact contract PASSED (the node did write what it declared), no
`expect_failed`, no salvage, no violation, `feasible=True`. The SAME authoring error killed node 0
loudly (2.6 GPU-hours) only because its absolute path named a directory the human had never created.
Whether this costs an hour or corrupts a result is pure luck.

Every cheaper rung is already spent. `adapters/repo_task.py::_entrypoint_protect` states the residual
hole ("a scorer that reads its checkpoint path from an editable config can still be pointed somewhere
else") and names the stage `expect` contract + the Developer prompt as what closes it — neither does.
The advisory note shipped into `edit_file`'s result FIRES AND IS IGNORED: `spans.jsonl` for node 4's
build carries it verbatim ("NOTE: this content hard-codes an absolute path inside the editable repo's
SOURCE tree … such a path can NEVER name an artifact this node's pipeline produced") and the node
committed the path anyway and trained on it. Prompt-level and advisory-level interventions are
exhausted; this is the mechanism.

WHAT IT IS
----------
A generated, self-contained `sitecustomize.py` written once per run into `<run_dir>/.looplab-fence/`,
whose directory is prepended to the eval process's `PYTHONPATH` (`runtime/sandbox.py::run_argv`,
the universal launch choke point). CPython imports `sitecustomize` at interpreter startup, so the
module installs a `sys.addaudithook` that inspects the `open` audit event — raised by `builtins.open`,
`io.open`, `io.open_code` AND `os.open` — and refuses any path resolving under an editable source
root. Because `PYTHONPATH` is inherited, every python the eval spawns (dataloader workers, a
torchrun rank, a shell script's `python`) is fenced too, at no extra cost.

It also refuses to WRITE the run's own RECORD — see THE RUN RECORD below — and it watches two more
classes of event, each there because the `open` hook alone measurably did not hold:
* `os.chdir` — so the relative-path fast bail below stays true. It is NOT a refusal of the chdir
  itself in the general case (a node may legitimately walk into a directory); the event re-derives
  `_CWD_REACHES_ROOT` from the target, and when that is true a relative open pays an `abspath`
  instead of bailing. Under `deny` a chdir INTO a root is still refused, because that report raises;
* the twelve MUTATION events in `MUTATION_EVENTS`. `os.remove`, `os.rename`, `os.truncate` and
  `os.chmod` raise their own events and NONE of them raises `open`, so until 2026-08-13 a node's
  eval code could delete or rename the operator's editable tree while every read of it was refused.
  Measured, against a real fenced child over a fake source root: 14 of 14 mutation probes went
  THROUGH, `shutil.rmtree(<the source root itself>)` included — the root's own name carries no
  trailing separator, so opening it is not refused, and every file under it is then unlinked with
  `os.remove(<bare name>, dir_fd=...)`, which CPython audits with the RELATIVE name.

THE RUN RECORD (2026-09-06)
---------------------------
The run directory holds the record an evaluation is scored INTO — `events.jsonl`, the two snapshots,
the traces — and the evaluation's own workdir lives inside it (`<run>/nodes/node_N`), so until this
date the launch allow-list granted the whole run dir `readwrite` and nothing in the hook looked at
it. A training script could `open('../../events.jsonl', 'a')` and append a well-formed
`node_evaluated` row naming its own node and a metric of its choosing; the store's foreign-writer
stop fires only on a MALFORMED row, the fold applies the FIRST terminal, and the engine's own
terminal then landed second. Docs/36's line is that the candidate may never elect, and this was
the one channel through which it could. So the fence now carries the RECORD root: any `open`
whose flags ask for a write (`O_WRONLY`/`O_RDWR`/`O_CREAT`/`O_TRUNC`/`O_APPEND`, baked into
`_WRITE_FLAGS` — the `open` audit event carries the resolved flags for every shape, `builtins.open`
modes and `os.open` alike, verified with a recording hook) and any registered MUTATION event whose
target is under the run directory is refused UNLESS it is under a WRITABLE prefix: the launch's
own workdir, handed to the child in `LOOPLAB_EVAL_WORKDIR` by `run_argv` because ONE fence serves
every launch of the run, and the fence's own directory, where `_record` appends the diagnostic.
Reads of the record stay legal (a node may read the run it belongs to), a write through a
planted symlink is closed for the links the process makes itself (`os.symlink`/`os.link` targets
are checked, as they are for the source roots), and the fence is now installed for EVERY run,
not only a repo task's: a toy or dataset node's `solution.py` is model-written code with the same
reach. It is the MESSAGE rung of this boundary exactly as for the source tree; the KERNEL rung is
`read_allowlist.derive`, which grants the run dir `read` and the fence dir `readwrite` beside the
per-launch workdir. A launch that carries the fence marker and NO workdir variable fails CLOSED:
every write under the record is refused, loudly, which is the right failure for a launch path
that forgot to say which directory is the node's.

THE MESSAGE IS THE POINT. A refusal must be actionable by the repair loop, so it is a plain
exception carrying `REFUSAL_MESSAGE` — which names the fix — and it surfaces in the node's own
stderr, i.e. in `eval.log`, in the captured `RunResult.stderr`, and therefore in the repair feedback.

WHY THE EXCEPTION IS NOT AN `OSError`
-------------------------------------
`LoopLabSourceReadRefused` derives from `Exception`, deliberately NOT from `OSError`/`PermissionError`.
The single most common shape around a file read in real training code is
`try: open(p) except (OSError, IOError): <fall back>` — a `PermissionError` would be swallowed by
exactly that pattern and the fence would become the silent skip the hard requirements forbid. A
broad `except Exception` can still swallow it; nothing can prevent that, which is why the refusal is
ALSO appended to the fence's own diagnostic log beside the run.

WHAT IT DOES NOT FENCE (by construction, and each is deliberate)
----------------------------------------------------------------
* the node's own workdir, the run directory, `/tmp`, site-packages, the HF/model cache — none of
  them are under an editable source root, so the prefix test never matches. THE ONE EXCEPTION IS
  THIS FENCE'S OWN GENERATED FILE, which lives in that same unfenced run directory and, until
  2026-08-25, could therefore simply be overwritten by the process it fences — refuse the read,
  `open(<run_dir>/.looplab-fence/sitecustomize.py, "w")`, and every process the run starts after
  that is unfenced. Relocating it does not help (there is nowhere outside the operator's tree that
  the denylist covers), so it is protected by three rungs of its own instead: the kernel
  one (`_harden`, mode 0444, which binds only where the launch lacks CAP_DAC_OVERRIDE —
  `harden_guarantee` is what says whether it does on this box), the hook one (`_SELF`, which
  refuses every mutation event aimed at the fence directory whatever `_ROOTS`/`_ALLOW` say), and,
  since 2026-09-08, the REPAIR one (`reassert`, called by `run_argv` at every launch, which
  restores the file from the engine's own in-memory copy). The first two are needed and neither is
  sufficient — measured, with one rung removed at a time — and the third is there because neither
  covers a writer that is not this interpreter: a `/bin/rm` CHILD removes the file at ANY uid,
  since `unlink` consults the DIRECTORY's write bit and not the file's;
* `data:` / `references:` mount SOURCES — allow-listed explicitly, because a data source is legally
  allowed to live INSIDE the editable tree and mounts are exactly the sanctioned read channel;
* the engine's own machinery — seeding (`engine/workspace.py`), the git plumbing, the fault
  localizer, the agent's repo tools all run in the ENGINE process, which never carries the marker;
* the Docker tiers — the source tree is not bind-mounted into the container at all, so a container
  is fenced by construction (`engine/eval_dispatch.py::_data_binds` mounts only the workdir and the
  declared data/reference sources). `run_argv` therefore skips the `PYTHONPATH` prepend for a
  `docker run` argv rather than pointing the container at a host path that does not exist there;
* a non-Python process (a C binary, a `curl`) — the fence is an interpreter-level hook. A shell
  script IS covered as soon as it invokes python, which is how every eval in practice reads a model.

Residual, stated rather than papered over: on the READ path the check is a PATH fence, not an inode
fence. A symlink inside the workdir pointing into the source tree resolves past `open`, because
closing that hole means `os.path.realpath` on every open — measured at +9,866 ns/open (+88 %; a
2026-08-13 re-measurement makes it +254 % on a 9-component path and ~474 us per call on the geesefs
mount run workdirs live on, so the original figure understates it) versus +311 ns/open (+2.8 %) for
the prefix compare, i.e. unaffordable on a training process that reads thousands of shards. The only
symlinks the engine itself creates into a source are the allow-listed mounts.

The RARE events do resolve symlinks, and the asymmetry is the whole design: `os.chdir` and the
mutation events happen a handful of times per process, so they can buy `realpath` (memoized per
directory, `_real`) and a `/proc/self/fd` lookup (`_fd_path`, ~2 us and flat across filesystems)
that the hot path cannot. Before that, `os.chdir(<a workdir symlink pointing at the source>)` was
neither refused nor recognised, and every bare relative name after it read the tree — which made the
fast bail's stated justification false in a way `_CWD_REACHES_ROOT` alone could not see, because the
flag is re-derived from the chdir ARGUMENT and that argument was a symlink.

What no CPython audit hook can reach, and what therefore stays open (all measured; the options and
their prices are in `docs/38-fence-coverage-audit-2026-08-13.md`):
* NATIVE readers. `safetensors.safe_open`, an HDF5 read through `h5py`, `pyarrow`, a `ctypes` call
  into libc — none of them raises a CPython audit event, so they read straight through. This is not
  a library list to be extended; it is every reader that does not go through CPython's `open`, and
  the only fix that does not become one is a KERNEL boundary. Landlock (ABI 2, verified available on
  this box) is the recommendation;
* a non-Python child: `subprocess.run(["cat", ...])`, or a stage command that is not python at all;
* `python -S` / `-E` / `-I`, or a child launched with `PYTHONPATH` stripped — the delivery mechanism
  is an env var and an import, and all three of those disable it;
* `os.open(name, dir_fd=...)`. CPython's `open` audit event carries `(path, mode, flags)` and NO
  dir_fd, so the hook cannot resolve the relative name even in principle. The mutation events do
  carry theirs, which is why the same shape IS closed there;
* metadata: `os.stat`, `os.lstat`, `os.access`, `os.readlink` and `os.listdir`/`os.scandir` raise no
  event this hook watches. Enumeration leaks NAMES, never bytes — and refusing it would be partial
  by construction anyway, since `os.stat` raises no audit event at all.

MEASURED COST (2026-08-13, this box, 5 reps, best-of)
-----------------------------------------------------
Design candidates, measured before choosing (open+read 4 KiB in a loop):
    no hook                             11,131 ns/open
    + an audit hook that does nothing   11,242 ns/open   (+1.0 %)   <- the floor for ANY hook
    + a prefix check on resolved roots  11,420 ns/open   (+2.8 %,  +311 ns/open)
    + realpath() per open               21,116 ns/open   (+88 %,  +9,866 ns/open)   <- rejected
The SHIPPED fence, same workload, launched through `run_argv` with and without the marker:
    11,739 -> 12,059 ns/open (+319, +2.7 %) and 11,800 -> 12,077 (+278, +2.4 %) on two runs.
Worst case for the RATIO — bare `os.open`/`close` on /dev/shm, no read at all:
    3,297 -> 3,635 ns/open (+337, +10.2 %).
Per-process startup (the `sitecustomize` import plus the chain probe): 17.9 -> 18.2 ms, +0.28 ms.
Import-heavy startup (json/logging/sqlite3/asyncio/…): 38 -> 38 ms, unmeasurable.
So the roots are resolved ONCE at generation time into a tuple of `str`s each ending in `os.sep`, and
the hot path is `event != "open"` (one interned-string compare) followed by `str.startswith(tuple)`.
No syscall and no `realpath` per open: a RELATIVE path with no `..` cannot leave the cwd's subtree,
so it bails before touching the filesystem. That bail is only sound while the CWD ITSELF is outside
every root, which `os.chdir` refusal alone does NOT establish — a launcher sets the cwd at process
creation (`subprocess.run(cwd=…)`, `bash -c 'cd <repo> && python …'`) and `fork_exec` does that
chdir in C, raising no audit event. So the cwd is resolved ONCE at interpreter startup into
`_CWD_REACHES_ROOT` (one `getcwd`, unmeasurable against the 17.9 ms process baseline) and
re-derived on each `os.chdir`; when it is true the relative branch pays an `abspath` instead of
bailing. Absolute paths additionally pay three C-level substring scans, because the prefix compare
is byte-exact and `'/src//repo/x'` / `'/src/./repo/x'` name the same file as `'/src/repo/x'`.

WHAT THE MUTATION HALF COSTS (2026-08-13, this box, N=20,000, best-of-5, one FRESH process per
variant — an audit hook can never be removed, so measuring two variants in one process reports
cumulative cost. `OLD` is this same fence one commit earlier, i.e. the marginal price of the branch):
                                     no hook      OLD          NEW (+mutation)
    open + read 4 KiB              11,865 ns   12,155 (+2.4 %)  12,189 (+2.7 %)   marginal +34 ns
    bare os.open/close              4,633 ns    5,076 (+9.6 %)   5,162 (+11.4 %)  marginal +86 ns
    create + close + remove        23,014 ns   23,820 (+3.5 %)  24,400 (+6.0 %)   marginal +580 ns
    per-process startup              8.68 ms     9.73 ms          9.75 ms         marginal +0.02 ms
The READ hot path pays 34 ns/open, which is inside the run-to-run noise of the fence that was
already there: a training process raises essentially no audited event except `open`, so the mutation
branch is one `dict.get` that is never reached. What DOES pay is a legal mutation of the node's own
workspace — +580 ns per create/remove pair, for the memoized `realpath` of its directory. A node that
deletes ten thousand checkpoint shards spends 6 ms on this.
(An earlier design note priced a 9-event SET-MEMBERSHIP variant at +3.9 % on the read path. That is
not what shipped and not what this costs: membership was tested before the `open` compare there.)
The RECORD rule (2026-09-06) adds nothing to the relative fast bail and, on the resolved-path
branch, one integer `&` on the flags (paid by writes only) plus one `startswith` on a hit; its
numbers, measured on the build container and not yet on the box, are in
`docs/38-fence-coverage-audit-2026-08-13.md` §6.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

from looplab.core.atomicio import atomic_write_text
# The DENY side reads the operator's declared mounts through the GRANT side's enumeration of them —
# see `fence_inputs`. `read_allowlist` imports nothing but stdlib and never imports this module, so
# the edge is one-way and cannot cycle.
from looplab.runtime.read_allowlist import mount_sources

# The env var the engine uses to hand a launch its fence, and the directory name under the run dir.
# `run_argv` consumes the var and prepends the directory to PYTHONPATH; it is deliberately an
# explicit marker rather than a filesystem search, because discovering the fence by walking up from
# the cwd would cost a `stat` of an ABSENT file per level per launch — 105-950 ms each on the
# geesefs/S3 mount a run root usually lives on (see the trace-fence measurements in CLAUDE.md).
FENCE_DIR_ENV = "LOOPLAB_READ_FENCE_DIR"
FENCE_DIRNAME = ".looplab-fence"
VIOLATION_LOG = "violations.log"
# The env var `run_argv` sets beside the marker: the directory THIS launch may write under the run
# record. Per launch rather than baked into the fence, because one generated fence serves every
# launch of the run (the node workdirs, the confirm-phase workdirs, the metric adapter's exec) and
# only the launch knows which of them it is. Inherited by every child, like the marker.
WORKDIR_ENV = "LOOPLAB_EVAL_WORKDIR"

# The mode `install` leaves on the generated `sitecustomize.py`: readable by every interpreter that
# has to IMPORT it, writable by nobody — see `_harden` for why this is the rung that carries the
# self-protection and `_SELF` in the template for the rung that guards it.
FENCE_FILE_MODE = 0o444

# CAP_DAC_OVERRIDE, the capability that makes `FENCE_FILE_MODE` advisory: a holder bypasses the
# file-permission check entirely, so the 0444 bit refuses nothing. Read from `CapEff` rather than
# inferred from the uid alone, because the two come apart in both directions — a container can run
# a non-root uid that still carries the bit through file capabilities, and a root process can have
# been stripped of it (`--cap-drop ALL`, which is what `sandbox.py` passes on the Docker tier).
_CAP_DAC_OVERRIDE = 1

# The policy rungs, in increasing strictness. See `Settings.read_fence` for why `deny` is the
# default rather than `warn`.
POLICIES = ("off", "warn", "deny")

# The refusal text, verbatim in the node's own output. It names the FIX, because the reader is the
# repair loop (and after it, the Developer), not a human tailing a log.
REFUSAL_MESSAGE = (
    "refused: {path} is under the operator's SOURCE tree, not this node's workspace. "
    "This node runs in its own copy; the source tree cannot contain anything your pipeline "
    "produced. Use a workdir-relative path, or ask the operator for a `data:`/`references:` "
    "mount or `seed_mode: \"all\"`."
)

# The MUTATION twin. A separate sentence because the FIX is a different one: for a read the answer
# is "name your own copy", for a delete/rename/chmod there is no legitimate answer at all — the
# source tree is the operator's working tree and a node has no business changing it. Same exception
# class, same non-`OSError` reasoning (see above), because `except OSError:` is just as routine
# around `os.remove` as it is around `open`.
MUTATION_REFUSAL_MESSAGE = (
    "refused: {path} is under the operator's SOURCE tree, which this node may not create, delete, "
    "rename, truncate, link or change. This node runs in its own copy — write to, and clean up "
    "inside, your own workdir. Nothing your pipeline produced is in the source tree."
)

# The RECORD twin (2026-09-06). A third sentence because the fix is a third one: the run directory
# is the record an evaluation is scored INTO, and the only place an evaluation writes is its own
# workdir — the engine reads what it printed and what it declared, and writes the record itself.
RECORD_REFUSAL_MESSAGE = (
    "refused: {path} is the run's own RECORD (its event log, snapshots, traces and the other "
    "nodes' workspaces), which an evaluation may read and never write. Write only inside your own "
    "workdir: the engine records your metric from what you print and what you declare."
)

# WHERE EACH MUTATION EVENT CARRIES ITS PATHS — a registry, per CLAUDE.md, because the alternative
# is `args[0]` everywhere and a silent miss the day one of these grows an argument.
#
# `event -> ((path_index, dir_fd_index | None), ...)`. Every entry was DERIVED, not remembered: the
# calls were run under a recording audit hook on this interpreter (CPython 3.12.11) and the emitted
# `(event, args)` read off. `tests/test_read_fence.py::test_mutation_arg_shapes_match_the_interpreter`
# re-derives the whole table the same way, so an interpreter that moves an argument goes RED here
# rather than quietly fencing the wrong slot. Two shapes are not obvious and both are load-bearing:
#
#   * a `dir_fd` slot exists because CPython audits the RELATIVE name for `unlinkat`-style calls.
#     `os.remove("secret.txt", dir_fd=<fd of the source root>)` raises `os.remove` with the bare
#     name, which the prefix compare cannot possibly match — measured THROUGH before this table,
#     and it is exactly how `shutil.rmtree` deletes every file under a tree;
#   * `os.symlink`/`os.link` index 0 is the link TARGET, which is not a mutation of that path. It is
#     listed anyway: the fence's documented residual is that a read THROUGH a symlink or hardlink
#     into a root resolves past it, and refusing to CREATE the link closes that residual for every
#     link the fenced process makes itself (both measured THROUGH before this table).
#
# Deliberately NOT here: the `shutil.*` events. Each one lowers to an `os.*` event or an `open` on
# the same path — `copyfile`->`open`, `copymode`->`os.chmod`, `copystat`->`os.utime`+`os.chmod`,
# `move`->`os.rename`, `rmtree`->`os.remove`+`os.rmdir` — verified by the same recording hook, so a
# `shutil` row would be a second name for a refusal that has already happened.
MUTATION_EVENTS = {
    "os.remove": ((0, 1),),          # (path, dir_fd)          — os.unlink raises this too
    "os.rename": ((0, 2), (1, 3)),   # (src, dst, src_dir_fd, dst_dir_fd) — os.replace raises this
    "os.truncate": ((0, None),),     # (path_or_fd, length)    — os.ftruncate raises this too
    "os.chmod": ((0, 2),),           # (path, mode, dir_fd)
    "os.chown": ((0, 3),),           # (path, uid, gid, dir_fd)
    "os.utime": ((0, 3),),           # (path, times, ns, dir_fd)
    "os.mkdir": ((0, 2),),           # (path, mode, dir_fd)
    "os.rmdir": ((0, 1),),           # (path, dir_fd)
    "os.symlink": ((0, None), (1, 2)),   # (target, link, dir_fd) — index 0 is the target, see above
    "os.link": ((0, 2), (1, 3)),     # (src, dst, src_dir_fd, dst_dir_fd)
    "os.setxattr": ((0, None),),     # (path_or_fd, attribute, value, flags)
    "os.removexattr": ((0, None),),  # (path_or_fd, attribute)
}


class ReadFenceRefusal(Exception):
    """Host-side twin of the generated fence's own exception class.

    The child process cannot import looplab (it may be a different interpreter in a different
    virtualenv), so the generated `sitecustomize.py` defines its OWN
    `LoopLabSourceReadRefused(Exception)`. This class exists only so host-side code and tests have a
    NAME for the same refusal; the two are matched by message, not by identity."""


def settle_policy(policy) -> str:
    """Settle an arbitrary policy value onto one of `POLICIES`, FAIL-CLOSED.

    `Settings.read_fence` validates the enum, but `EngineOptions.read_fence` (a bare `Engine(...)`,
    a hand-edited snapshot, a Strategist value) does not — and the generated hook tests
    `_POLICY == "deny"` exactly, so an unrecognised spelling used to fall through to the WARN
    branch: every source-tree read allowed, one stderr line, and the operator's config still saying
    `deny`. Every sibling knob in the tree (`metric_salvage.settle_mode`, `widths.settle_width`)
    settles an unknown value to the CONSERVATIVE rung; this one settled to the permissive one, which
    is the whole failure class the fence exists to close. Unknown -> `deny`."""
    text = str(policy or "").strip().lower()
    return text if text in POLICIES else "deny"


def _norm_root(path) -> Optional[str]:
    """Resolve one prefix into the exact form the hot path compares against: absolute, symlinks
    resolved, with a trailing separator so `/srcfoo` never matches the root `/src`."""
    try:
        p = os.path.realpath(str(Path(path).expanduser()))
    except OSError:
        return None
    if not p:
        return None
    return p if p.endswith(os.sep) else p + os.sep


def _too_broad(root: str) -> bool:
    """A root so wide that fencing it would fence the interpreter itself.

    `/`, `/home`, `/home/<user>`, `/usr` — an editable path like `$HOME` is pathological, but the
    failure mode of accepting it is that every python on the box refuses to start, which is a worse
    outcome than an unfenced run. Dropped roots are REPORTED (see `fence_inputs`) rather than
    silently ignored, so an operator whose whole fence evaporated can see why."""
    parts = [p for p in root.strip(os.sep).split(os.sep) if p]
    if len(parts) < 2:
        return True
    home = os.path.realpath(os.path.expanduser("~"))
    return os.path.realpath(root.rstrip(os.sep)) in (home, os.path.realpath(os.sep))


def fence_inputs(repo_spec: Optional[dict], *, allow: Iterable = ()) -> tuple:
    """`(roots, allow, dropped, swallowed)` for a repo spec — the whole policy input, in one place.

    `roots` are the EDITABLE source trees: the thing a node has its own copy of, and therefore the
    one place on the filesystem that provably cannot hold an artifact this node produced.

    `allow` wins over `roots` and carries the two categories that legitimately live inside one:
    the caller's own paths (the run directory — a run may be `--out`-ed inside the repo it edits)
    and every `data:` / `references:` mount SOURCE. A mount is the sanctioned read channel; the
    engine materializes it as a read-only symlink whose target may well be under the editable tree,
    and reading THROUGH that symlink must stay legal. Data mounts are not the editable source root.

    An allow entry that is an ANCESTOR of (or equal to) a root is REFUSED, not kept: `_fenced` tests
    the allow tuple after the root tuple, so such an entry disables the whole fence. `swallowed` is
    the fourth element for exactly that reason — a root dropped by `_too_broad` is warned about, and
    a root neutralized through the allow list has to be equally loud rather than leaving an operator
    who set `read_fence="deny"` with an unfenced run and no diagnostic."""
    spec = repo_spec or {}
    roots: list[str] = []
    dropped: list[str] = []
    for ed in spec.get("editables", []):
        r = _norm_root(ed.get("path"))
        if r is None:
            continue
        if _too_broad(r):
            dropped.append(r)
            continue
        if r not in roots:
            roots.append(r)
    allowed: list[str] = []
    for extra in allow:
        a = _norm_root(extra)
        if a and a not in allowed:
            allowed.append(a)
    # The declared mounts come from `read_allowlist.mount_sources` — ONE enumeration of one operator
    # declaration, not two. This module and that one are the two spellings of a single policy (see
    # its docstring, and `test_the_two_spellings_of_the_boundary_agree_about_the_declared_mounts`),
    # and until 2026-08-15 each walked `data:`/`references:` itself and they already DISAGREED: this
    # side called `ref.get("path")` unguarded, so a `references:` entry that is not a dict raised
    # AttributeError out of the fence's own derivation, while the grant side skipped it. A
    # disagreement here is not a tidiness point — the audit hook would permit a read the kernel
    # ruleset denies, and a Landlock refusal is `EACCES` -> `PermissionError` -> an `OSError`, i.e.
    # precisely the silent `except OSError:` skip both modules' refusal types exist to avoid.
    #
    # The GUARDED reading is the correct one and it is a decision, not a merge artifact. `data:`
    # values have a bare-string back-compat shape (`adapters/repo_task.py::RepoTask.data`) and
    # `mount_sources` keeps it; `references:` has never had one — it is a list of `ReferenceSpec`
    # dumps — so a non-dict entry there is a malformed declaration, and a malformed declaration must
    # degrade to "no declaration" rather than take down the derivation, the same rule
    # `command_eval`'s `isinstance` degradations state for a public entry point handed raw dicts.
    #
    # The MODE (`read` vs `readwrite` for `edit: true`) is deliberately dropped: allow-listing here
    # is an exemption from a DENY prefix, not a write grant, and which mounts may be written is the
    # Landlock tier's question. Carving a mount out of both halves of the fence is the behaviour
    # this has always had.
    for src, _mode in mount_sources(spec):
        a = _norm_root(src)
        if a and a not in allowed:
            allowed.append(a)
    # An allow prefix that is not under any root is dead weight on the hot path — drop it so the
    # `startswith` tuple stays as short as the policy actually needs.
    #
    # An allow prefix that CONTAINS a root is not dead weight, it is a disabled fence: `_fenced`
    # consults the allow tuple after the root tuple, so `/src/` allow-listed against the root
    # `/src/repo/` returns None for every path under the source tree. This is reachable by ordinary
    # operator spelling — `looplab run --out .` beside an editable `./repo` makes `resources.py`
    # pass the repo's own PARENT as `allow=[run_dir]`. Refuse those and hand them back so the caller
    # can say so; only a strict DESCENDANT of a root is a real carve-out.
    swallowed = [a for a in allowed if any(r.startswith(a) for r in roots)]
    allowed = [a for a in allowed
               if a not in swallowed and any(a.startswith(r) for r in roots)]
    return roots, allowed, dropped, swallowed


# The ceiling on what one swallowing grant may be expanded into (see `confine_grants`). A tier with
# more entries than this is refused rather than turned into a thousand-rule ruleset: Landlock adds
# one `open(O_PATH)` + one syscall per rule, the hook compares a `startswith` tuple per read, and a
# grant list nobody can read is a boundary nobody can check. It is a bound on the EXPANSION, not on
# the caller's own list. Sized against what a punch really has to enumerate, measured on this box:
# a machine tier is tens of entries (`/opt` 5, `/usr` 10), a venv is 5, a stdlib 206, and the widest
# thing a root can plausibly sit inside is a fat `site-packages` at 540. It is deliberately several
# times that rather than snug — an expansion is a REFUSAL when it is exceeded, so a bound set at the
# largest observed case turns a new dependency into a probe that will not run. What it catches is a
# tier that is not a tier.
_MAX_GRANT_EXPANSION = 4096


def _grant_expansion(tier: str, roots, budget: int):
    """Every subtree of `tier` that does NOT contain a fenced root, or None if it cannot be built.

    The hole punch. `tier` is a normalized prefix (trailing separator) that CONTAINS one or more
    `roots`; the caller may not grant it, and a kernel allow-list has no way to subtract. So the
    tier is replaced by its children, minus the branch each root sits on, descending only along
    those branches — `/opt` with the root `/opt/myrepo` becomes every other entry of `/opt`, and
    `/opt/conda` (this box's interpreter) survives, which is the case that makes dropping the tier
    outright unusable.

    A child that RESOLVES into a fenced root is skipped, symlink included: the expansion stands in
    for the tier and must not grant more than the tier's own subtree would have. Anything genuinely
    needed from inside a root arrives as its own candidate (a venv in the repo is `sys.prefix`), and
    a candidate under a root is a carve-out the caller states, never one this walk invents.

    STATED RESIDUAL: a punched tier loses the loose FILES sitting directly in it — only directories
    become grants, because a grant is a PREFIX on both sides of this rule (the hook compares
    `startswith` against a trailing-separator string, which no file path can match) and one rule per
    file in a real system tier is the thousand-rule ruleset the budget exists to refuse. It bites
    only where the editable tree is INSIDE such a tier, which is the case this function exists for:
    a repo at `/etc/myrepo` costs the process `/etc/nsswitch.conf`. The failure is a named
    PermissionError on a path, not a quiet widening, and the fix is the one every refusal here
    names — declare it as a mount."""
    out: list[str] = []
    stack = [tier]
    while stack:
        base = stack.pop()
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return None
        for name in names:
            child = _norm_root(os.path.join(base, name))
            if child is None or not os.path.isdir(child):
                # A FILE under the tier is not grantable as a prefix and needs no rule of its own:
                # the kernel grants directories, and the hook's compare is a directory prefix too.
                continue
            if not child.startswith(tier) or any(child.startswith(r) for r in roots):
                continue                      # the root itself, inside one, or a symlink out of here
            if any(r.startswith(child) for r in roots):
                stack.append(child)           # still contains a root: punch one level deeper
                continue
            out.append(child)
            if len(out) > budget:
                return None
    return out


def confine_grants(candidates: Iterable, roots: Iterable) -> tuple:
    """`(grants, refused)` — what a CONFINED process may read, given the roots it may not.

    The companion of `fence_inputs` for the other shape of the same policy. `fence_inputs` answers
    "what is forbidden, and which carve-outs survive"; this answers "what may be granted", which is
    what a Landlock allow-list and `render(confine=True)` both need — and a grant list is where the
    two guarantees `fence_inputs` provides for the deny side have to be provided again:

      * every entry goes through `_norm_root` (realpath + trailing separator), so a grant of `/opt`
        cannot also admit `/optfoo`;
      * an entry that CONTAINS a root is never kept. On the deny side that is a disabled fence
        (`_fenced` consults the allow tuple after the root tuple); on the grant side it is a kernel
        grant of read over the very tree the confinement exists to hide. Neither is survivable, and
        neither may be answered by dropping the entry outright: the interpreter's own tiers must be
        granted or python does not start, which is the failure `_too_broad` exists to prevent one
        layer down. So a swallowing entry is REPLACED by `_grant_expansion` — the same subtree minus
        the roots inside it.

    What cannot be replaced is REFUSED and returned in `refused` as `[(path, reason)]`, never
    silently dropped and never silently kept: a candidate that IS a root (the repo is the venv), a
    tier that cannot be listed, an expansion past `_MAX_GRANT_EXPANSION`. The caller's only correct
    response is to say so and not run — a confined process missing a grant fails loudly at its first
    import, but a confined process holding a swallowing grant is silently unconfined.

    A candidate UNDER a root is kept, deliberately: that is the sanctioned carve-out (a declared
    mount inside the source tree, a venv inside the repo), the same entries `fence_inputs` keeps."""
    rootv = tuple(r for r in (_norm_root(x) for x in roots) if r)
    grants: list[str] = []
    refused: list[tuple] = []
    for raw in candidates:
        c = _norm_root(raw)
        if c is None or c in grants:
            continue
        if not any(r.startswith(c) for r in rootv):
            grants.append(c)
            continue
        if c in rootv:
            refused.append((c, "it IS the fenced source root, so there is nothing left to grant"))
            continue
        if not os.path.isdir(c):
            # A MACHINE tier that is not on this box grants nothing, so it can swallow nothing —
            # dropped at derivation exactly as `read_allowlist._add` drops it, and for its reason.
            continue
        expanded = _grant_expansion(c, rootv, _MAX_GRANT_EXPANSION)
        if expanded is None:
            refused.append((c, "it contains the fenced source root and could not be expanded into "
                               "the subtrees that do not"))
            continue
        for e in expanded:
            if e not in grants:
                grants.append(e)
    # Absorb a grant already covered by another one — `sys.prefix`, `sys.base_prefix` and the
    # interpreter's `bin/` are three spellings of one subtree on most boxes. Only after the punch,
    # and it can never resurrect a swallower: nothing in `grants` contains a root by construction.
    # Lexicographic order does the work in one pass: every entry carries the trailing separator, so
    # an ancestor sorts immediately before the block it contains and nothing else can sort between
    # them (`/a/` < `/a/b/` < `/a0`, because `/` sorts below every name character).
    kept: list[str] = []
    for g in sorted(grants):
        if not (kept and g.startswith(kept[-1])):
            kept.append(g)
    return tuple(kept), tuple(refused)


# The generated fence. Kept as ONE template rather than a shipped file plus a config sidecar so the
# PYTHONPATH entry contains exactly one importable name (`sitecustomize`) — every additional module
# in that directory would shadow a real one for every process in the run.
#
# `__name__` gates installation: exec'ing this source under `_PROBE_NAME` yields the pure predicate
# `_fenced()` WITHOUT installing an irreversible audit hook, which is what lets the truth table be
# tested in-process (an audit hook can never be removed once added).
_PROBE_NAME = "__looplab_fence_probe__"

_TEMPLATE = '''\
"""LoopLab source-tree READ FENCE — GENERATED, do not edit.

Regenerated per run by `looplab/runtime/read_fence.py`; see that module for why this exists.
This file is imported by CPython at interpreter startup because its directory is first on
PYTHONPATH. It refuses reads of the operator's editable SOURCE tree from inside a node's process:
the node runs in its own copy, so the source tree provably cannot hold anything the node produced.
It also refuses every MUTATION of that tree — `open` is not the only way to touch a file.
"""
import os
import sys
import threading

_ROOTS = %(roots)r
_ALLOW = %(allow)r
# CONFINE inverts the policy: instead of "refuse what is under a ROOT", it refuses EVERYTHING that is
# not under `_ALLOW`. The engine's fence never sets it (a training process needs to read the box);
# `tools/dev_probe.py` always does, because a probe's whole legitimate world is its own disposable
# replica plus the interpreter, and a denylist keyed on the editable tree leaves every OTHER
# directory on the machine readable. Measured 2026-08-19: with no editable root declared, the probe
# fence was skipped entirely and a Developer used 150 `run_probe` calls to read the BENCHMARK
# HARNESS's own validation and timing code. A denylist cannot express "only your own workdir".
_CONFINE = %(confine)r
_POLICY = %(policy)r
_LOG = %(log)r
_MESSAGE = %(message)r
_MUTATION_MESSAGE = %(mutation_message)r
_MUTATE = %(mutations)r
_RUN = %(run)r          # provenance: the run this fence was generated for
# THE RUN RECORD: the run directory (events.jsonl, the snapshots, the traces), readable and never
# writable, except under a WRITABLE prefix — the fence's own directory (baked) and this launch's
# workdir (from the env, below). Empty for a fence that guards no record.
_RECORD = %(record)r
_RECORD_MESSAGE = %(record_message)r
_WRITABLE = %(writable)r
_WRITE_FLAGS = %(write_flags)r
_WORKDIR_ENV = %(workdir_env)r

_SEP = os.sep
_DOTDOT = ".."
_DUP_SEP = _SEP + _SEP          # '/src//repo/x' — what f"{root}/{sub}" spells when root ends in '/'
_DOT_SEG = _SEP + "." + _SEP    # '/src/./repo/x'
_DOT_END = _SEP + "."           # '/src/repo/.'  (a trailing no-op segment)
_NT = %(nt)r
_abspath = os.path.abspath
_normpath = os.path.normpath
_realpath = os.path.realpath
_realcache = {}
_seen = set()
_busy = threading.local()

if _RECORD:
    # The one per-launch input. Resolved ONCE at startup, symlinks included, because the record
    # root was `realpath`-ed at generation and the compare is a prefix compare. Absent -> nothing
    # under the record is writable but the fence directory: fail CLOSED, and loudly.
    _wd = os.environ.get(_WORKDIR_ENV) or ""
    if _wd:
        try:
            _wd = _realpath(_wd)
        except Exception:
            # a bug in the fence must never break the launch: fall back to the lexical path, which
            # is the same prefix compare against a root that was itself realpath-ed at generation
            _wd = _abspath(_wd)
        _WRITABLE = _WRITABLE + (_wd if _wd.endswith(_SEP) else _wd + _SEP,)
# THIS FILE'S OWN DIRECTORY — the one region no fenced process may CHANGE, whatever `_ROOTS` and
# `_ALLOW` say. It is the hook half of the self-protection whose other half is the kernel one
# (`install` writes this file mode 0444, and `CapEff` is 0 in the container a node runs in, so the
# owner write bit genuinely binds). Neither rung is sufficient alone: 0444 stops
# `open(fence, "w")` and stops nothing else, because the owner of a file may always chmod it back;
# and an audit-hook refusal is only as good as the hook, which is what the write was trying to
# remove. Together the kernel bit refuses the write and the hook refuses the `os.chmod` /
# `os.remove` / `os.rename` that would take the kernel bit away.
#
# Measured 2026-08-21 and re-measured 2026-08-25 on this tree: a fenced child refused a read of the
# operator's checkpoint, then `open(os.environ["LOOPLAB_READ_FENCE_DIR"] + "/sitecustomize.py",
# "w")` SUCCEEDED, and the same read from the next process of the run returned the bytes. Note what
# the re-measurement settled: the run dir was NOT under an editable root in that reproduction, so
# `fence_inputs` had dropped `allow=[run_dir]` entirely and the allow list was `()`. The hole is not
# the allow entry — it is that this file lives outside every fenced root by construction, which is
# true of every place it could legally be written.
#
# DERIVED FROM `__file__` rather than baked by `render`: the fence protects wherever it actually
# lives, including a copy, and `render` keeps its signature. `_realpath`, because `_fenced_target`
# resolves the DIRNAME of its argument and a byte-exact prefix compare against an unresolved
# `__file__` would miss a fence dir reached through a symlink. Empty under the probe seam
# (`_PROBE_NAME`), where the source is exec'd from a namespace that has no `__file__` — the probe
# yields the pure `_fenced()` predicate and installs nothing, so it has no file to protect.
#
# COST: none on the READ hot path, by construction — `_SELF` is consulted in `_fenced_target` only,
# i.e. on the mutation events a training process never raises. What it adds there is one string
# concat and one `startswith` against a ONE-element tuple, and a create+close+remove loop
# (N=20,000, best-of-5, one fresh process per variant) could not separate it from this box's
# run-to-run noise in either direction. Startup pays one `realpath` of this file's directory.
try:
    _SELF = (_realpath(os.path.dirname(os.path.abspath(__file__))) + _SEP,)
except Exception:
    _SELF = ()

# Is a RELATIVE open able to reach a fenced root from where this process stands? Normally no, and
# that is what buys the syscall-free fast bail for the branch nearly every read takes. But a
# process's cwd is far more often set AT CREATION than by `os.chdir`: `subprocess.run(cwd=...)`,
# `bash -c 'cd <repo> && python score.py'`, a `WorkingDirectory=` wrapper. `fork_exec` performs
# that chdir in C in the child and raises NO `os.chdir` audit event, so the "the cwd cannot be
# inside a root" premise the bail rested on was simply false for every launcher-style command —
# measured: with cwd inside the root, `open('model.safetensors')` returned the human's checkpoint
# under policy "deny" with nothing logged. Resolved ONCE at interpreter startup (one `getcwd`,
# unmeasurable against the 17.9 ms baseline) and re-derived on each `os.chdir`.
_CWD_REACHES_ROOT = False


class LoopLabSourceReadRefused(Exception):
    """A node process tried to read, or to change, the operator's editable SOURCE tree.

    NOT an OSError subclass on purpose: `except (OSError, IOError): <fall back>` is the standard
    shape around a file read — and around a cleanup `os.remove` — and being caught by it would turn
    this refusal into a silent skip."""


def _resolve(p):
    """Normalize an `open` argument to an absolute path string, or None if it cannot name a file
    under a fenced root. Deliberately syscall-free on the hot path.

    The three normalizations below all exist because the check downstream is a PREFIX COMPARE
    against roots that were `realpath`-ed at generation time. Any spelling of the same file that
    does not share those bytes is a bypass, and each of these was reproduced:
      * duplicate or no-op separators — '/src//repo/x' and '/src/./repo/x' were ALLOWED while
        '/src/repo/x' refused, and `f"{root}/{sub}"` produces exactly that whenever root ends in a
        slash. Three C-level substring scans, paid only on the absolute branch;
      * Windows drive-letter and UNC paths never start with `os.sep`, so they took the relative
        fast bail and were never compared against a root at all — the fence was a complete no-op on
        that platform while every surface reported the run as fenced. `_NT` is baked at generation
        time, so POSIX pays one already-false boolean for this and nothing else;
      * a cwd inside a fenced root (see `_CWD_REACHES_ROOT`), which makes a bare relative name reach
        the source tree without ever containing `..`.
    """
    cls = p.__class__
    if cls is not str:
        if cls is int:
            return None            # an already-open fd (os.fdopen): the path was checked at open
        try:
            p = os.fspath(p)
        except TypeError:
            return None
        if p.__class__ is not str:
            try:
                p = os.fsdecode(p)
            except Exception:
                return None
    if _NT:
        # Windows: either slash separates, absolute spellings are drive-letter/UNC rather than
        # sep-leading, and `_ROOTS` are backslash-normalized `realpath` output. There is no correct
        # syscall-free bail here, so normalize unconditionally. Windows is not a perf target for
        # this fence; being INERT there was the defect.
        return _normpath(_abspath(p))
    if p[:1] != _SEP:
        # The branch nearly every open in a training process takes. A relative path with no `..`
        # can only reach the cwd's subtree, so it needs no filesystem work UNLESS the cwd itself
        # stands inside a fenced root.
        if not _CWD_REACHES_ROOT and _DOTDOT not in p:
            return None
        p = _abspath(p)
    elif (_DOTDOT in p or _DUP_SEP in p or _DOT_SEG in p
            or p.endswith(_DOT_END)):
        p = _normpath(p)
    return p


def _cwd_reaches_root():
    """Whether a bare relative open from the CURRENT directory could land under a fenced root.

    False for the overwhelmingly common case (the cwd is the node's own workdir), which is what
    keeps `_resolve`'s relative branch syscall-free. An allow-listed cwd answers False too: reading
    through a sanctioned mount is legal, and a `..` escape out of it is still normalized above."""
    try:
        d = os.getcwd()
    except OSError:
        return True                # cannot prove it is safe -> resolve, and let `_fenced` decide
    d = d if d.endswith(_SEP) else d + _SEP
    if _CONFINE:
        return not (_ALLOW and d.startswith(_ALLOW))
    # A cwd under the RECORD but outside every writable prefix (a launcher standing in the run
    # dir itself) makes a bare relative write reach the record without a `..`: resolve.
    if _RECORD and d.startswith(_RECORD) and not (_WRITABLE and d.startswith(_WRITABLE)):
        return True
    if not d.startswith(_ROOTS):
        return False
    return not (_ALLOW and d.startswith(_ALLOW))


def _fenced_resolved(p):
    """The policy over an ALREADY-RESOLVED path: the refused path, or None.

    ONE rule, two callers. The `open` branch needs the resolved path for its own record check and
    therefore cannot use `_fenced`'s return alone, and the 2026-09-07 merge answered that by
    INLINING the rule there — which silently dropped the `_CONFINE` clause, so a confined probe
    (`developer_probe_confine`) refused only reads under the editable roots and let everything else
    through, the opposite of what confinement means. Split rather than duplicated, so the open path
    keeps `p` and the rule stays in one place.
    """
    if p is None:
        return None
    if _CONFINE:
        return None if (_ALLOW and p.startswith(_ALLOW)) else p
    if not p.startswith(_ROOTS):
        return None
    if _ALLOW and p.startswith(_ALLOW):
        return None
    return p


def _fenced(p):
    """The path this fence refuses, or None. The whole policy, in three string operations."""
    return _fenced_resolved(_resolve(p))


def _record_write(p):
    """The RECORD path a write would change, or None: under the run directory and outside every
    writable prefix. Trailing-separator compare, like `_prefixed`, so the record directory itself
    (`os.rmdir`, `os.chmod` of it) is inside the rule. The caller has resolved `p` and established
    that the access is a WRITE — a read of the record is legal and pays nothing here."""
    if not _RECORD:
        return None
    d = p if p.endswith(_SEP) else p + _SEP
    if not d.startswith(_RECORD):
        return None
    if _WRITABLE and d.startswith(_WRITABLE):
        return None
    return p


def _prefixed(r):
    """The trailing-separator prefix test, on an already-resolved absolute path.

    A directory's own name carries no trailing separator, so `os.chdir('/src/repo')` misses the test
    `open('/src/repo/x')` hits — and symmetrically an allow-listed mount must still be enterable.
    Both are fixed by comparing the trailing-separator form. The `open` hot path deliberately does
    NOT pay for this, because opening a directory raises IsADirectoryError before it can read
    anything."""
    d = r if r.endswith(_SEP) else r + _SEP
    if _CONFINE:
        return None if (_ALLOW and d.startswith(_ALLOW)) else r
    if not d.startswith(_ROOTS):
        return None
    if _ALLOW and d.startswith(_ALLOW):
        return None
    return r


def _fd_path(fd):
    """The path an OPEN DESCRIPTOR names, or None.

    Never reached from `open`, and that asymmetry is the whole reason it is affordable:
    `/proc/self/fd/N` is a procfs read of an already-resolved dentry, ~2 us flat regardless of the
    backing filesystem (measured 1,944 ns on the geesefs mount a run root lives on, against
    474,268 ns for a pre-open `realpath` of the same 13-component path). A node deletes a handful of
    times and opens millions, so the rare events can buy what the hot one cannot."""
    try:
        return os.readlink("/proc/self/fd/%%d" %% fd)
    except Exception:
        return None


def _real(d):
    """`realpath`, MEMOIZED, for the rare events only.

    `realpath` is catastrophic per-open (+254 %%, and ~474 us on geesefs) and nearly free per
    DIRECTORY: a loop that chmods 10,000 files in one directory pays one call, not 10,000. Bounded
    and cleared wholesale rather than evicted — an unbounded dict in a process that touches millions
    of distinct paths is a leak, and the cache is a hint, never the decision.

    The TOCTOU window is real and stated: a symlink repointed after its directory was first cached
    is not seen again. That is sound against the ACCIDENT this fence exists for (an absolute path in
    a config) and worthless against an adversary — the same honesty the module docstring keeps."""
    r = _realcache.get(d)
    if r is None:
        try:
            r = _realpath(d)
        except Exception:
            r = _abspath(d)
        if len(_realcache) >= 4096:
            _realcache.clear()
        _realcache[d] = r
    return r


def _as_str(p):
    """Coerce an audited argument to a path string, or None — WITHOUT `_resolve`'s relative fast
    bail, which is sound only for `open`. The calls that reach here are exactly the ones that can
    change, or step outside, the directory those relative names are read from."""
    cls = p.__class__
    if cls is int:
        return _fd_path(p)                 # os.chdir(fd), os.truncate(fd), os.setxattr(fd)
    if cls is str:
        return p
    try:
        p = os.fspath(p)
    except TypeError:
        return None
    if p.__class__ is str:
        return p
    try:
        return os.fsdecode(p)
    except Exception:
        return None


def _join(base, name):
    """`base` + separator + `name`, WITHOUT doubling the separator when `base` is the root itself.

    The one directory whose name already ends in a separator is `/`, and the naive
    `base + _SEP + name` spells it `//repo`. That is not cosmetic here: `_prefixed` is a byte-exact
    prefix compare, `'//repo/'.startswith(('/repo/',))` is False, and the fenced target directly
    under `/` was therefore NOT refused while the identical call against `/home/x/repo` was — the
    asymmetry a mutation fence cannot have. It reaches `_fenced_target` from both sides: a root one
    level down (`rpartition` leaves an EMPTY head, so the base is `_real('/')`) and a relative name
    resolved against a cwd or a `dir_fd` that IS `/`. `realpath` happens to collapse a leading
    double separator, which is why only the `_abspath` fallback and the empty-head case bite — one
    rule here rather than two accidents relied upon. Reachability, stated rather than assumed: no
    root `fence_inputs` derives has one component (`_too_broad` drops those), so today the shipped
    derivation does not produce that shape. That rule answers a different question — whether fencing
    a root would break the interpreter — and `render` is public, so the join carries its own
    rule."""
    return base + name if base.endswith(_SEP) else base + _SEP + name


def _fenced_dir(p):
    """`_fenced` for an argument naming a DIRECTORY the process is about to work from — `os.chdir`.

    It RESOLVES SYMLINKS, and `open` deliberately does not. Without that, `_CWD_REACHES_ROOT` is
    re-derived from a name that does not look like a root even though it leads to one:
    `os.chdir(<workdir symlink pointing at the source>)` compared as `abspath` reads as harmless,
    the flag stays False, and every bare relative name after it takes the fast bail and reads the
    source — measured THROUGH. The whole argument is resolved (not just its parent) because the
    argument IS the directory in question, and `chdir` happens a handful of times per process. The
    allow-list is applied to the RESOLVED path, so entering a `data:` mount that was materialized as
    a symlink into the tree still works. `os.fchdir` hands a bare fd, which `_as_str` resolves
    through `/proc/self/fd`; when even that fails the caller falls back to assuming the worst."""
    r = _as_str(p)
    return _prefixed(_real(r)) if r is not None else None


def _fenced_target(path, dir_fd):
    """The MUTATION check: the path an event is about to change, or None.

    Three shapes `open` never sees, each measured THROUGH before it was handled: a bare fd
    (`os.truncate(fd, n)`), a name relative to a dir_fd (`os.remove(name, dir_fd=...)`, i.e.
    `unlinkat` — which is how `shutil.rmtree` deletes every file it deletes, and why rmtree of the
    root itself used to take the whole tree), and a path under a symlinked directory.

    Only the DIRNAME is resolved; the final component is kept verbatim. That is not a shortcut, it
    is the meaning: `os.remove`/`os.rename` act on the LINK, not on what it points at, so resolving
    the last component would refuse a node deleting its OWN symlink whose target happens to be a
    mount source. The residual — a symlinked final component under `chmod`/`utime`, which do
    follow — is the same class as the documented read-side one."""
    r = _mutation_path(path, dir_fd)
    return None if r is None else _mutation_fenced(r)


def _mutation_fenced(r):
    """The mutation POLICY over an already-resolved path: the refused path, or None.

    Split from `_fenced_target` for the reason `_fenced_resolved` is split from `_fenced` one
    branch over, and after the same defect: the hook's mutation branch needs `r` for its record
    check, and the 2026-09-07 merge answered that by calling `_prefixed(r)` there directly —
    which skips `_SELF` and left `_fenced_target` with ZERO callers, i.e. the fence's own
    self-protection dead. Driven end to end: a fenced child could `chmod` and then `unlink` the
    generated `sitecustomize.py`, disarming the fence for every process the run started afterwards.
    One rule, and the branch keeps its resolved path.
    """
    # BEFORE the root/allow policy, and deliberately not expressible through it: `_SELF` is
    # refused for every caller, allow-list included. See `_SELF`. A run dir is allow-listed on
    # purpose (a run may be `--out`-ed inside the repo it edits) and, far more often, this file
    # is simply outside every root — so under the ordinary policy the fence's own source is the
    # one file a fenced process may freely delete, rename or chmod, and doing any of those
    # disarms the fence for every process the run starts afterwards. It sits HERE rather than in
    # `_mutation_path` because that helper is the policy-free resolution the record check reads too.
    if _SELF and _join(r, "").startswith(_SELF):
        return r
    return _prefixed(r)


def _mutation_path(path, dir_fd):
    """The absolute path a mutation event names, dirname resolved, final component verbatim — the
    one resolution BOTH the source-root check (`_fenced_target`) and the record check read, so the
    two rules cannot disagree about which file an event is about."""
    r = _as_str(path)
    if r is None:
        return None
    if r[:1] != _SEP:
        if dir_fd is None:
            base = _real(os.getcwd())      # rare-path only; `open`'s bail never pays for this
        else:
            base = _fd_path(dir_fd)
            if base is None:
                return None
        r = _join(base, r)
    head, _sep, tail = r.rpartition(_SEP)
    return _join(_real(head or _SEP), tail)


def _dir_fd(args, index):
    """The dir_fd an audited call passed, or None. CPython spells "no dir_fd" as -1, and an
    interpreter that grows an argument makes the slot absent rather than wrong."""
    if index is None or index >= len(args):
        return None
    fd = args[index]
    return fd if fd.__class__ is int and fd >= 0 else None


def _record(path, rung, event):
    """Append one line to the run's fence diagnostic. Re-entrancy-guarded: this opens a file, which
    raises `open` again — the guard makes that provably terminate rather than relying on the fence
    log being outside every root.

    The guard is THREAD-LOCAL, not a module-level list. A process-global one made a concurrent
    thread's violation vanish: `_report` marks the path seen BEFORE calling here, so a thread that
    found the flag set while another thread was blocked in this `open` (105-950 ms on the
    geesefs/S3 mount a run root usually lives on) lost its line permanently — leaving `warn`, whose
    entire purpose is to produce this audit file, silently under-reporting what the eval read.

    The EVENT is the last column, because "the file is gone" and "the file was read" are different
    incidents and an operator reading this log has to be able to tell them apart."""
    if not _LOG or getattr(_busy, "on", False):
        return
    _busy.on = True
    try:
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write("%%s\\t%%s\\t%%s\\t%%s\\t%%s\\n" %% (
                rung, os.getpid(), sys.argv[0], path, event))
    except Exception:
        pass
    finally:
        _busy.on = False


def _report(path, event, message):
    # The MESSAGE is a parameter, not derived from the policy: a mutation refusal must not tell the
    # node to "use a workdir-relative path", because there is no legitimate way to delete the
    # operator's tree. Keyed by (event, path) for the same reason — a read of a file and a delete of
    # it are two incidents, and collapsing them would drop the second from the diagnostic.
    #
    # Bounded: a retry loop must not write a gigabyte. The bound covers BOTH sinks — gating only
    # `_record` on it INVERTED the guard, because a path never added to a saturated `_seen` is
    # "first" on every subsequent read of it, so the stderr write below fired per `open()` rather
    # than per distinct path (measured: 256 distinct paths then 1000 reads of one more produced
    # 1256 lines). That is a write+flush syscall pair on the hot read path, and it floods the
    # captured stderr that `eval.log` and the repair feedback are built from.
    key = (event, path)
    first = key not in _seen and len(_seen) < 256
    if first:
        _seen.add(key)
        _record(path, _POLICY, event)
    if _POLICY == "deny":
        raise LoopLabSourceReadRefused(message.replace("{path}", path))
    if first:
        try:
            sys.stderr.write(
                "LOOPLAB READ FENCE (warn): " + message.replace("{path}", path) + "\\n")
            sys.stderr.flush()
        except Exception:
            pass


def _hook(event, args):
    # Hot path: one interned-string compare for every audited event that is not an open.
    if event == "open":
        try:
            p = _resolve(args[0])
            bad = _fenced_resolved(p)
        except Exception:
            return                   # a bug in the fence must never break an unrelated open
        if bad is not None:
            _report(bad, event, _MESSAGE)
            return
        # THE RECORD. Flags FIRST — one integer `&` on the resolved-path branch only — so a read
        # pays nothing past the source check it already paid, and a relative open from the
        # workdir still takes the syscall-free bail above (`p is None`).
        if p is not None and _RECORD:
            try:
                flags = args[2]
                hit = (flags.__class__ is int and (flags & _WRITE_FLAGS) != 0
                       and _record_write(p) is not None)
            except Exception:
                return                   # a bug in the RECORD must never break an unrelated open;
                                         # the refusal above has already run, so nothing is let past
            if hit:
                _report(p, event, _RECORD_MESSAGE)
        return
    if event != "os.chdir":
        # MUTATION. One dict lookup, and only for events that are not opens — a training process
        # raises essentially nothing else in its hot loop, so this is off the measured path. It is
        # here because `open` is not the only way to touch a file: `os.remove`, `os.rename`,
        # `os.truncate` and `os.chmod` raise their own events and NONE of them raises `open`, so
        # before this branch a node's eval code could delete or rename the operator's editable tree
        # while every read of it was refused. Deny is the default for the same reason it is for
        # reads: the failure does not announce itself.
        slots = _MUTATE.get(event)
        if slots is None:
            return
        for path_i, fd_i in slots:
            try:
                r = _mutation_path(args[path_i], _dir_fd(args, fd_i))
                bad = _mutation_fenced(r) if r is not None else None
            except Exception:
                r = bad = None       # a bug in the fence must never break an unrelated call
            if bad is not None:
                _report(bad, event, _MUTATION_MESSAGE)   # outside the try: deny RAISES from here
                continue        # …so this is the WARN path only — see the CONTINUE note below
            # THE RECORD, on the same resolved path: a mutation under the run dir outside the
            # writable prefixes — a rename INTO it, a link OF it, a truncate, a chmod, an rmdir.
            if r is not None and _record_write(r) is not None:
                _report(r, event, _RECORD_MESSAGE)
                continue
            # CONTINUE, NOT RETURN, and it matters at exactly one policy. Three events carry TWO
            # slots (`os.rename`, `os.symlink`, `os.link`: source AND destination), and under deny
            # neither `continue` nor `return` is reachable — `_report` raises out of the hook. Under
            # WARN it reports and returns, and returning here ABANDONED the remaining slots: driven,
            # `os.rename` with both sides inside a fenced root recorded ONE violation instead of
            # two, so the destination — the file that would have been created in the operator's
            # tree — was missing from the very log that is warn's whole product. At most one report
            # per slot is still the rule: a path that is both fenced and a record-write is one
            # incident, and the mutation rung is the one that names it.
        return
    # `_CWD_REACHES_ROOT` is what keeps `_resolve`'s relative fast bail correct rather than merely
    # asserted, so a chdir has to be able to TURN IT ON: under `warn` the chdir proceeds, and a
    # process that has just moved into a fenced root must start paying the `abspath` on relative
    # opens. It must never turn it OFF — see the MONOTONIC note below, which is the whole argument.
    global _CWD_REACHES_ROOT
    try:
        bad = _fenced_dir(args[0])
    except Exception:
        return
    # MONOTONIC — this flag is only ever SET here, never cleared, and that is the whole correctness
    # argument. The audit event fires BEFORE the chdir, so a target outside every root does NOT
    # prove the process is about to stand outside one: `try: os.chdir(cfg.output_dir) except
    # OSError: pass` around a directory that does not exist yet is ordinary generated-training-code
    # shape, and clearing the flag from the pre-event would turn OFF the fence for a process whose
    # cwd is still the repo the launcher put it in — the v6-node-4 read, allowed under policy
    # `deny` with nothing logged. Clearing it would need confirmation the chdir SUCCEEDED, which no
    # pre-event hook can have. A stale True costs one `abspath` per relative open and decides
    # nothing: `_fenced` re-checks the resolved path either way.
    if bad is not None:
        _report(bad, event, _MESSAGE)    # under `deny` this RAISES: the chdir never happens at all
    # `os.fchdir` hands an already-open fd. `_fenced_dir` resolves it through /proc/self/fd, but a
    # platform or a race where that fails leaves nothing proving the target is outside a root, so
    # pay the `abspath` on relative opens rather than guess. The leading `_CWD_REACHES_ROOT or` is
    # what makes this the monotonic assignment the comment above describes: written as a plain
    # `= args[0].__class__ is int` it CLEARS the flag whenever the target is outside every root,
    # which is the pre-event guess this whole block exists to refuse to make.
    _CWD_REACHES_ROOT = (
        _CWD_REACHES_ROOT or bad is not None or args[0].__class__ is int)


def _chain():
    """Run whatever `sitecustomize` the environment already had.

    This directory is PREPENDED to PYTHONPATH, so it shadows any other `sitecustomize` on the path
    (coverage's subprocess support, a distro's, a conda env's). Shadowing one silently would be a
    real regression in someone else's tooling, so hand off explicitly.

    The handoff loads the other module by SPEC, under a different name, and never touches
    `sys.modules['sitecustomize']`. The obvious spelling — pop the name and re-`import` — leaves the
    entry missing when there is no other sitecustomize (the common case), and `importlib._bootstrap`
    ends its own load with a `sys.modules.pop(name)` that then raises `KeyError`. `site` catches it
    and prints `Error in sitecustomize; set PYTHONVERBOSE for traceback: KeyError: 'sitecustomize'`
    on the stderr of EVERY fenced process — measured, and diagnosed only because the fence's own
    benchmark reported the module as not loaded while the hook was demonstrably installed."""
    try:
        from importlib.machinery import PathFinder
        from importlib.util import module_from_spec

        me = os.path.abspath(__file__)
        here = os.path.dirname(me)
        # `PathFinder.find_spec`, not `importlib.util.find_spec`: the latter consults `sys.modules`
        # FIRST, where `sitecustomize` is bound to THIS module mid-import — so it hands back our own
        # spec and the chain re-executes the fence against itself. Measured: one refused read became
        # 247 refusals and the fence never reached the real sitecustomize at all.
        spec = PathFinder.find_spec(
            "sitecustomize", [p for p in sys.path if os.path.abspath(p or os.curdir) != here])
        if spec is None or spec.loader is None or os.path.abspath(spec.origin or "") == me:
            return
        other = module_from_spec(spec)
        sys.modules.setdefault("_looplab_chained_sitecustomize", other)
        spec.loader.exec_module(other)
    except Exception:
        pass


if __name__ != "%(probe)s":
    if _POLICY != "off" and (_ROOTS or _RECORD or _CONFINE):
        # Resolve the launcher-set cwd ONCE, before the hook is armed, so the very first relative
        # open is already judged correctly (and so this `getcwd` is not itself audited).
        _CWD_REACHES_ROOT = _cwd_reaches_root()
        sys.addaudithook(_hook)
    _chain()
'''


def render(roots, allow, *, policy: str, log: str = "", run: str = "",
           record_root="", writable: Iterable = (), confine: bool = False) -> str:
    """The generated `sitecustomize.py` source for one run's fence.

    `record_root` is the run directory the fence guards against WRITES ("" for a fence that
    guards roots only — the Developer's probe renders that shape), and `writable` the prefixes
    under it a launch may still write; the launch's own workdir joins them at startup from
    `WORKDIR_ENV`. Both are resolved here, once, into the trailing-separator form the hot path
    compares.

    `confine=False` (the default, and what the engine installs) keeps the historical DENYLIST:
    refuse paths under `roots`, exempting `allow`. `confine=True` INVERTS it into an allow-list
    — refuse everything outside `allow`, `roots` unused — which is the only shape that can
    express "this process may read its own workdir and nothing else". See `_CONFINE` in the
    template for the incident that made the denylist insufficient for `tools/dev_probe.py`.
    The two are ORTHOGONAL: `confine` decides what may be READ, `record_root` what may be
    WRITTEN, and the probe renders `confine=True` with no record root at all."""
    record = _norm_root(record_root) if record_root else ""
    writable_prefixes = tuple(w for w in (_norm_root(x) for x in writable) if w)
    return _TEMPLATE % {
        "roots": tuple(roots), "allow": tuple(allow), "policy": settle_policy(policy),
        "confine": bool(confine),
        "log": str(log), "message": REFUSAL_MESSAGE, "run": str(run), "probe": _PROBE_NAME,
        "mutation_message": MUTATION_REFUSAL_MESSAGE, "mutations": MUTATION_EVENTS,
        "record": record or "", "record_message": RECORD_REFUSAL_MESSAGE,
        "writable": writable_prefixes, "workdir_env": WORKDIR_ENV,
        # Baked from the generating box, like `nt`: the write bits of the `open` audit event's
        # resolved flags (the same for `builtins.open` modes and `os.open`, verified with a
        # recording hook — 'w' is O_WRONLY|O_CREAT|O_TRUNC, 'a' adds O_APPEND, 'r+' is O_RDWR).
        "write_flags": (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND),
        # Baked rather than probed in the child: the fence runs on the same box that generated it
        # (the Docker tiers are fenced by construction and skip the marker entirely), so this keeps
        # the POSIX hot path at one already-false boolean instead of an `os.path.isabs` call.
        "nt": os.name == "nt",
    }


def _harden(target: Path) -> None:
    """Drop every WRITE bit on the generated fence — the KERNEL rung of its self-protection.

    THE DEFECT. Until 2026-08-25 this file was left at `atomic_write_text`'s mode (0600, i.e. the
    `mkstemp` default carried through `os.replace`) inside a directory the fenced process may write,
    and NOTHING refused a write to it: the hook is a denylist over the operator's editable roots, and
    this file is outside every one of them by construction. Driven end to end, twice: a fenced child
    refuses the read, `open(<fence>, "w")` succeeds, and the next process of the run reads the
    operator's checkpoint — i.e. the `rubertlite-dr-unified-v6` node-4 incident, re-enabled from
    inside a node, for the whole rest of the run rather than for one process.

    WHY THE KERNEL AND NOT THE HOOK. The thing being protected IS the hook, so a rung expressed only
    in the hook protects itself with the object under attack. `CapEff` is 0 for a node process in
    this container (no `CAP_DAC_OVERRIDE`), so the owner write bit is not advisory here: it is the
    kernel refusing the `open`, before any Python runs, for every writer including the native ones
    (`ctypes`, a C extension, a `cp`) that raise no audit event at all and that the whole module
    docstring lists as beyond an audit hook's reach.

    *That sentence was a fact about ONE deployment and the module could not tell which one it was
    running in — only the Docker tier drops capabilities (`--cap-drop ALL`), the subprocess tier
    inherits the engine's, and on a privileged runner the write bit refuses nothing. Closed
    2026-09-08 by `harden_guarantee`, which decides both halves of the precondition (the file
    really carries no write bit, and this process really lacks CAP_DAC_OVERRIDE) and returns the
    ONE sentence naming what is reduced; `engine/resources.py` logs it once per run beside the
    dropped-root and swallowed-allow warnings. It does not make a privileged runner safe — nothing
    at this layer can, see below — it makes the deployment read as what it is. Re-measured on a
    root box that day, and it is not only the plain `open`: a `ctypes` `fopen(fence, "w")` from
    inside the fenced interpreter went through too, which no audit hook can ever see.*

    WHAT IT DOES NOT DO, and why `_SELF` exists beside it: the owner of a file may always chmod it
    back, and `CapEff` says nothing about that — ownership, not a capability, is what `chmod`
    checks. So the bit alone converts a one-call escape into `os.chmod` + write. `os.chmod`,
    `os.remove` and `os.rename` all raise audit events, so the template's `_SELF` refuses exactly
    the calls that would remove this bit, and the two rungs hold each other up.

    NOT the DIRECTORY. 0555 on `<run_dir>/.looplab-fence` would also stop the unlink-and-recreate
    path at the kernel rung, and it was measured and rejected: CPython writes
    `__pycache__/sitecustomize.*.pyc` there on first import, that write is silently skipped when the
    directory is read-only, and every fenced process then recompiles the template — 1.02 ms measured
    on this box against a 17.9 ms fenced-process startup, +5.7 %, paid by every python a run
    launches. `_SELF` already refuses the unlink, so the directory bit would buy a second copy of a
    rung that is there, at a price the read path is not allowed to pay.

    Best-effort: a chmod that fails leaves the `_SELF` rung standing, whereas raising here would be
    turned by `_read_fence_dir`'s `except OSError` into an entirely UNFENCED run — strictly worse
    than a fence whose file is one rung down.

    OPEN[read-fence-self-protection-ends-at-an-unfenced-child]
    proof:present:FENCE_FILE_MODE@looplab/runtime/read_fence.py
    The two rungs above hold against every writer that goes through this interpreter — measured
    2026-08-25, 11 of 11 probes refused (`open(fence, "w")`, chmod back, unlink, unlink via
    `dir_fd`, rename away, rename over, truncate, chmod of the directory, `shutil.rmtree` of it,
    rename of it, symlink over the file), in BOTH layouts (run dir inside and outside the editable
    root). They do not hold against a process that is not this interpreter, and the cheapest such
    process is NOT the `-S` child the first version of this note named — re-measured 2026-09-08 at
    euid 0 AND euid 1001, `subprocess.run(["rm", "-f", <fence>])` removes it outright, because
    `unlink` consults the DIRECTORY's write bit and never the file's 0444, and a non-Python child
    raises no audit event at all. The `-S` child (chmod back, rewrite) works too, as does a
    `ctypes` `fopen` under a privileged runner.
    The DIRECTORY bit is not the answer either, and that was measured rather than argued: 0555 on
    the fence dir does refuse `rm`, `mv` and a shell truncate — and its stated price is removable,
    since pre-compiling the pyc at install time takes `sitecustomize` import from 2,351 us back to
    246 us, i.e. indistinguishable from a writable directory (2026-09-08, unprivileged; the
    `NOT the DIRECTORY` paragraph above measured only the un-precompiled variant) — but a
    `/bin/chmod 755 <fence dir>` CHILD then puts the bit back, because chmod asks who OWNS the
    inode and the eval owns everything the engine wrote for it. Every rung expressible in the
    filesystem rests on ownership, and on the subprocess tier the engine and the eval are one uid.
    What is in force since 2026-09-08 is REPAIR rather than prevention: `reassert`, called from
    `run_argv`, compares the file against the engine's own in-memory copy — the one copy a node
    cannot reach — and restores it before each launch. That measured 0.03 ms per launch and it
    bounds the damage to the launch that did the tampering; it does not close this item, because
    the children that launch spawns itself never pass the choke point, and a tamper landing between
    the re-assert and the child's `execve` still wins.
    What would CLOSE it is a rung that does not rest on ownership, and there are exactly two:
    Landlock over the fence directory (`runtime/landlock.py`; it binds on root too and is inherited
    across `exec`, but it is `off` by default, unvalidated on a real GPU eval, and simply ABSENT on
    some boxes — `landlock_create_ruleset` reported no version at all on the 2026-09-08 box), or
    separating the uid that writes the fence from the uid that runs the eval, which is a deployment
    decision this module cannot make. `runtime/seccomp.py` is not a candidate: a classic-BPF filter
    sees syscall numbers and register values, never paths."""
    try:
        os.chmod(target, FENCE_FILE_MODE)
    except OSError:
        pass


def _dac_override() -> str:
    """Why this process's file-mode checks are advisory, or "" when the kernel really enforces them.

    STATABLE rather than buried in a log line, because the sentence `_harden` used to make — "the
    owner write bit genuinely binds" — is a fact about ONE deployment and the module had no way to
    say which one it was running in. `CapEff` first (the precise question is the capability, not the
    uid); `os.geteuid()` is the fallback for a box with no readable `/proc/self/status` and for
    Windows, where `os.geteuid` does not exist at all and the whole rung is advisory anyway."""
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("CapEff:"):
                    if int(line.split()[1], 16) & (1 << _CAP_DAC_OVERRIDE):
                        return "this process holds CAP_DAC_OVERRIDE, which ignores file modes"
                    return ""
    except (OSError, ValueError, IndexError):
        pass
    try:
        if os.geteuid() == 0:                                   # type: ignore[attr-defined]
            return "this process runs as euid 0, which ignores file modes"
    except AttributeError:
        return "this platform has no effective-uid check; the write bit is advisory here"
    return ""


def harden_guarantee(target) -> Optional[str]:
    """`None` when the KERNEL rung of the fence's self-protection actually binds for `target`;
    otherwise ONE sentence naming exactly what is reduced.

    THE DEFECT THIS CLOSES (the `fence-kernel-rung-rests-on-ambient-caps` item, 2026-08-30
    review).
    `_harden` promised "`CapEff` is 0 for a node process in this container, so the owner write bit
    is not advisory here: it is the kernel refusing the `open`". That is true of the production
    deployment and false of a privileged runner, and NOTHING in this module could tell them apart —
    measured 2026-09-08 on a root box: the fenced child's `open(<fence>, "w")` went straight
    THROUGH (and so did a `ctypes` `fopen`, which no audit hook sees), the run was unfenced for
    every process after it, and not one line anywhere said the rung was inert. Only the Docker tier
    drops capabilities (`--cap-drop ALL`); the subprocess tier inherits the engine's.
    This does not make a privileged runner safe — nothing at this layer can, since the eval owns
    the file and the directory (see the marker on `_harden`) — it makes the deployment READ as what
    it is, which is the difference between a stated residual and an unstated one.

    Both halves are required and both are checked, because either alone is the whole rung: the
    file must carry no write bit (`_harden` is best-effort and a failed `chmod` is silent), and the
    caller must not hold the capability that ignores it."""
    reasons = []
    try:
        mode = os.stat(target).st_mode & 0o777
    except OSError as exc:
        return f"the fence file {target} could not be stat'ed ({exc.strerror}), so the kernel rung " \
               f"of its self-protection cannot be said to be in force"
    if mode & 0o222:
        reasons.append(f"{target} carries write bits ({mode:04o}), so `_harden`'s chmod did not take")
    override = _dac_override()
    if override:
        reasons.append(override)
    if not reasons:
        return None
    return ("the read fence's KERNEL self-protection rung is ADVISORY here: " + "; ".join(reasons)
            + ". A node's eval code can overwrite the generated fence and unfence every process "
              "the run starts afterwards; only the audit-hook rung (`_SELF`) is in force")


# The exact bytes `install` last wrote, per fence directory — the ENGINE process's own copy of the
# fence, which a node's eval cannot reach because it is memory in a different process. Keyed by the
# directory rather than the file so `reassert` needs nothing but the marker `run_argv` already
# carries. Bounded by the number of run directories one engine installs into, i.e. one.
_INSTALLED: dict = {}


def _clear_fence_cache(fence_dir) -> None:
    """Remove `<fence_dir>/__pycache__`, so no pyc can shadow the fence's own source. Best-effort:
    a cache that cannot be removed must not take down the launch, and the source rung still runs."""
    try:
        cache = Path(fence_dir) / "__pycache__"
        for entry in cache.iterdir():
            try:
                entry.unlink()
            except OSError:
                pass
        cache.rmdir()
    except OSError:
        pass


def reassert(fence_dir) -> Optional[str]:
    """Repair a tampered fence before the next launch imports it. `None` when nothing was wrong.

    WHY REPAIR AND NOT PREVENTION. The fence file is created by the engine and read by the eval,
    and on the subprocess tier those are the SAME uid — so the eval OWNS the file and its
    directory, and every rung expressed in the filesystem rests on ownership. Measured 2026-09-08,
    unprivileged, against a fence hardened to 0444 in a directory hardened to 0555 with the pyc
    pre-compiled (i.e. the `NOT the DIRECTORY` candidate `_harden` rejected on cost, with the cost
    removed — 2,351 us -> 246 us of `sitecustomize` import, indistinguishable from a writable
    directory): `rm`, `mv` and a shell truncate were all refused, and then `chmod 755 <fence dir>`
    from a `/bin/chmod` CHILD succeeded, because chmod asks who owns the inode and never what
    permissions or capabilities the caller holds. The directory bit buys nothing a determined node
    cannot take back, at any uid. So the file cannot be made unwritable from inside this layer, and
    what is left is to notice.

    WHAT IT BOUNDS. Before this, one write to the fence — `open(fence, "w")` on a privileged
    runner, a `/bin/rm` child on any runner — unfenced EVERY process the run started afterwards,
    including every other node's eval, for the rest of the run. `run_argv` is the universal launch
    choke point, so re-asserting here bounds the damage to the launch that did it (plus a launch
    that races it: a tamper landing between this call and the child's `execve` still wins, which is
    inherent to a repair rung and is why the marker stays open).

    COST, measured 2026-09-08 on this box, N=300, the branch where nothing is wrong (one 33 KB
    read plus a `stat`): 0.025 ms best, 0.027 ms median, against an 18-19 ms fenced-process
    startup — 0.15 %. A launch already pays a `fork`+`execve`; this is not visible beside it.
    """
    # THE SHADOWING CACHE, FIRST, because the source can be pristine and the fence still dead.
    # `__pycache__` beside the fence is written by the interpreter that imports it, the fence dir is
    # a WRITABLE record prefix, and `_SELF` guards the twelve MUTATION events -- not `open`. So a
    # fenced node may write `<fence>/__pycache__/sitecustomize.<tag>.pyc` with a header whose mtime
    # and size it copied from the untouched source, and CPython's timestamp validation then trusts
    # it over the real file. Driven end to end: the source compared EQUAL, this function returned
    # None, and every later launch of the run -- across an engine restart, because `install` does
    # not rewrite an unchanged source either -- ran with no hook and read the operator's checkpoint.
    # That is strictly worse than the tamper this function was written for: it is persistent and it
    # leaves the one thing anybody checks looking correct. Removing the cache costs one recompile of
    # a 33 KB file (2.4 ms measured, against an 18-19 ms fenced startup) and is unconditional on
    # purpose -- a legitimate cache is worth exactly that much, and telling the two apart from the
    # bytes is the game we just lost.
    _clear_fence_cache(fence_dir)
    src = _INSTALLED.get(str(fence_dir or ""))
    if not src:
        # Not a fence THIS process installed: a resumed engine that has not reached `install` yet,
        # a test harness, a hand-set marker. There is no trusted copy to compare against, and
        # guessing one from the file on disk is exactly the thing under attack, so do nothing.
        return None
    target = Path(fence_dir) / "sitecustomize.py"
    try:
        found = target.read_text(encoding="utf-8")
        mode_ok = not (os.stat(target).st_mode & 0o222)
    except OSError:
        found, mode_ok = "", False          # deleted or renamed away — rewrite it
    if found == src and mode_ok:
        return None
    what = "content" if found != src else "mode"
    try:
        atomic_write_text(target, src)
        _harden(target)
    except OSError as exc:
        # Same posture as `install`: a fence that cannot be repaired must not take down the launch.
        return f"read fence at {target} was tampered with ({what}) and could NOT be repaired: {exc}"
    return f"read fence at {target} had been tampered with ({what}); repaired before this launch"


def install(run_dir, *, roots, allow, policy: str, record: bool = True) -> Optional[str]:
    """Materialize the fence beside a run and return the directory to prepend to `PYTHONPATH`.

    `None` when the fence would be a no-op: policy `off`, or nothing to guard — no editable root
    survived `_too_broad` AND `record=False`. With `record=True` (the default since 2026-09-06)
    the run directory is guarded against writes, so a run with no source root at all still gets a
    fence; the caller must not set the marker when this returns None."""
    if settle_policy(policy) == "off" or not (roots or record):
        return None
    d = Path(run_dir) / FENCE_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    src = render(roots, allow, policy=policy, log=str(d / VIOLATION_LOG), run=str(run_dir),
                 record_root=str(run_dir) if record else "", writable=(str(d),) if record else ())
    target = d / "sitecustomize.py"
    # Rewrite only on change: the eval workers call this concurrently, and an unconditional
    # write would let one worker truncate the file another interpreter is mid-import of.
    try:
        if target.read_text(encoding="utf-8") == src:
            # Re-assert the mode even when the bytes already match: `_harden` is what makes the file
            # unwritable, and a run that finds the right content has no idea whether the previous
            # writer was this function or a node that put the content back after widening the bits.
            _harden(target)
            _INSTALLED[str(d)] = src
            return str(d)
    except OSError:
        pass
    # `atomic_write_text`, not a hand-rolled temp+replace: it already owns the per-CALL unique
    # scratch name this needs (the eval workers are THREADS, so a shared `<name>.tmp` would let two
    # of them interleave into one file and `os.replace` the interleaving into place) AND the two
    # things the private copy was missing — an fsync before the replace, so a crashy FUSE/S3 run
    # root cannot publish a truncated `sitecustomize.py` that every python of the run then fails to
    # import, and an `except BaseException: unlink(tmp)` so a failed or cancelled write does not
    # leave a permanent multi-KB `.tmp` in the run dir with nothing to reclaim it.
    # `os.replace` onto a 0444 destination is a DIRECTORY operation and succeeds — the mode of the
    # file being replaced is not consulted — so re-installing over a hardened fence needs no unlock,
    # and the new inode arrives at `mkstemp`'s 0600 and is hardened below.
    atomic_write_text(target, src)
    _harden(target)
    # The ENGINE's own copy of what it wrote, for `reassert` at every later launch. Recorded after
    # the write, so a failed write leaves no claim that the fence on disk is this source.
    _INSTALLED[str(d)] = src
    return str(d)


def prepend_pythonpath(env: dict, fence_dir: str) -> None:
    """Put `fence_dir` first on the child's `PYTHONPATH`, in place.

    PREPEND rather than replace: a task whose eval legitimately sets PYTHONPATH keeps it, and
    prepending is also what makes our `sitecustomize` win the import (it then chains to theirs)."""
    if not fence_dir:
        return
    existing = env.get("PYTHONPATH") or ""
    if existing:
        parts = [p for p in str(existing).split(os.pathsep) if p]
        if parts and parts[0] == fence_dir:
            return
        parts = [p for p in parts if p != fence_dir]
        env["PYTHONPATH"] = os.pathsep.join([fence_dir, *parts])
    else:
        env["PYTHONPATH"] = fence_dir


def violations(run_dir) -> list[str]:
    """The fence's diagnostic lines for a run (`[]` when it never fired). Read by an operator and by
    the tests; the engine does not need it, because a `deny` refusal is already in the node's own
    stderr and a `warn` one is on its stderr too."""
    p = Path(run_dir) / FENCE_DIRNAME / VIOLATION_LOG
    try:
        return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []
