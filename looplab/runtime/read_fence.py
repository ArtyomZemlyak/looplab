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

It watches two more classes of event, and each is there because the `open` hook alone measurably
did not hold:
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
  them are under an editable source root, so the prefix test never matches;
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
    if not d.startswith(_ROOTS):
        return False
    return not (_ALLOW and d.startswith(_ALLOW))


def _fenced(p):
    """The path this fence refuses, or None. The whole policy, in three string operations."""
    p = _resolve(p)
    if p is None:
        return None
    if _CONFINE:
        return None if (_ALLOW and p.startswith(_ALLOW)) else p
    if not p.startswith(_ROOTS):
        return None
    if _ALLOW and p.startswith(_ALLOW):
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
    return _prefixed(_join(_real(head or _SEP), tail))


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
            bad = _fenced(args[0])
        except Exception:
            return                   # a bug in the fence must never break an unrelated open
        if bad is not None:
            _report(bad, event, _MESSAGE)
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
                bad = _fenced_target(args[path_i], _dir_fd(args, fd_i))
            except Exception:
                bad = None           # a bug in the fence must never break an unrelated call
            if bad is not None:
                _report(bad, event, _MUTATION_MESSAGE)   # outside the try: deny RAISES from here
                return
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
    if _POLICY != "off" and (_ROOTS or _CONFINE):
        # Resolve the launcher-set cwd ONCE, before the hook is armed, so the very first relative
        # open is already judged correctly (and so this `getcwd` is not itself audited).
        _CWD_REACHES_ROOT = _cwd_reaches_root()
        sys.addaudithook(_hook)
    _chain()
'''


def render(roots, allow, *, policy: str, log: str = "", run: str = "",
           confine: bool = False) -> str:
    """The generated `sitecustomize.py` source for one run's fence.

    `confine=False` (the default, and what the engine installs) keeps the historical DENYLIST: refuse
    paths under `roots`, exempting `allow`. `confine=True` INVERTS it into an allow-list — refuse
    everything outside `allow`, `roots` unused — which is the only shape that can express "this
    process may read its own workdir and nothing else". See `_CONFINE` in the template for the
    incident that made the denylist insufficient for `tools/dev_probe.py`."""
    return _TEMPLATE % {
        "roots": tuple(roots), "allow": tuple(allow), "policy": settle_policy(policy),
        "confine": bool(confine),
        "log": str(log), "message": REFUSAL_MESSAGE, "run": str(run), "probe": _PROBE_NAME,
        "mutation_message": MUTATION_REFUSAL_MESSAGE, "mutations": MUTATION_EVENTS,
        # Baked rather than probed in the child: the fence runs on the same box that generated it
        # (the Docker tiers are fenced by construction and skip the marker entirely), so this keeps
        # the POSIX hot path at one already-false boolean instead of an `os.path.isabs` call.
        "nt": os.name == "nt",
    }


def install(run_dir, *, roots, allow, policy: str) -> Optional[str]:
    """Materialize the fence beside a run and return the directory to prepend to `PYTHONPATH`.

    `None` when the fence would be a no-op (policy `off`, or no editable root survived
    `_too_broad`) — the caller must then not set the marker at all, so a non-repo run's child env is
    byte-identical to what it was before this module existed."""
    if settle_policy(policy) == "off" or not roots:
        return None
    d = Path(run_dir) / FENCE_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    src = render(roots, allow, policy=policy, log=str(d / VIOLATION_LOG), run=str(run_dir))
    target = d / "sitecustomize.py"
    # Rewrite only on change: the eval workers call this concurrently, and an unconditional
    # write would let one worker truncate the file another interpreter is mid-import of.
    try:
        if target.read_text(encoding="utf-8") == src:
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
    atomic_write_text(target, src)
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
