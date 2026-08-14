"""The READ ALLOW-LIST an eval process may see — DERIVED from what the operator declared, once.

WHY IT IS DERIVED AND NOT WRITTEN DOWN
---------------------------------------
Measured on the 116-workdir corpus (doc 35, E5): **116 of 116 node workdirs read outside their own
workdir** — data roots, base-model caches, HDFS keytabs, site-packages. "A node cannot read outside
its workspace" would break every node ever run here, so the boundary has to be an ALLOW-LIST and can
never be a confinement.

And an allow-list that is hand-written kills real work. The measured false positive is not
hypothetical: `runs/rubertlite-dense-retrieval` node 36 runs

    --teacher_checkpoint /home/jovyan/data/vectorizer/dense-retrieval/models/rubertlite-20e-v7/last.ckpt

— a legitimate distillation from a teacher checkpoint that lives INSIDE the editable source root, in
a run that declares no `data:`/`references:` mount at all. Any allow-list that forgets the operator's
declared mounts refuses that node, and a boundary that makes legitimate work fail is a boundary the
Developer spends repair attempts fighting.

So the allow-list is a FUNCTION of the operator's own declarations plus a documented default set,
and the fix for a refusal is always the same sentence: declare the mount. Nothing here is a literal
somebody has to remember to update per task.

WHAT IS IN IT
-------------
| tier | what | why |
|---|---|---|
| the node's workdir | readwrite | the node's own workspace; everything it produces |
| the run directory | readwrite | logs, the fence dir, the stage logs it writes live |
| `data:` / `references:` mount SOURCES | read (readwrite when `edit: true`) | the sanctioned read channel — a mount source may legally live inside the editable tree, and `edit:true` is the operator saying in-place writes are allowed |
| the interpreter: `sys.prefix`, `sys.base_prefix`, `sys.executable`'s dir | read | site-packages, the venv's `bin/`; without it python does not start |
| `/usr`, `/lib`, `/lib64`, `/bin`, `/sbin`, `/etc` | read | the C runtime, CA certs, `nsswitch.conf` |
| `/tmp`, `/var/tmp`, `/dev/shm` | readwrite | tempfiles, and `/dev/shm` is where torch DataLoader workers talk |
| the model cache (`HF_HOME`, `TRANSFORMERS_CACHE`, `HF_HUB_CACHE`, `TORCH_HOME`, `XDG_CACHE_HOME`, `~/.cache`) | read | a base-model download is a read every repo task makes |
| `/proc`, `/sys`, `/dev` | readwrite | CUDA/NCCL device nodes, `/proc/self`, `/sys/class`. Deliberately WHOLE: doc 35 §7.2 records that an allow-list omitting `/sys` and `/dev` would have failed a real GPU run, and nobody has enumerated those surfaces |
| the stage's declared `needs` | (already inside the workdir) | `needs` entries are workdir-relative by `_validate_rel_paths`, so they add no rule — but they ARE the declaration channel: `engine/eval_stages.py` derives the protected `score` stage's `needs` from the declared metric SUBJECT, so "what the stage says it reads" and "what it may read" come from one place |

The EDITABLE SOURCE ROOT is deliberately absent, and that is the whole point: it is the one place on
the filesystem that provably cannot hold an artifact this node produced. `runtime/read_fence.py`
derives the same boundary from the other side (`fence_inputs` returns the editable roots as DENY
prefixes plus the mounts as exceptions); this module is the complement, expressed as the grant list a
kernel ruleset needs. They are two spellings of one policy, and
`tests/test_read_fence.py::test_the_two_spellings_of_the_boundary_agree_about_the_declared_mounts`
derives BOTH from one spec and pins that the mounts appear in each. (This paragraph named a
`tests/test_read_allowlist.py` from the day it was written; no such file has ever existed — this
module's own tests live in the `The read allow-list` section of `tests/test_metric_subject.py`,
beside the subject binding they shipped with.)

WHY THIS IS NOT IN `read_fence.py`
-----------------------------------
Two reasons, one of them structural. `fence_inputs` answers "what is forbidden"; a Landlock ruleset
needs "what is permitted", and the complement of a deny-list over a live filesystem is exactly the
construction doc 35 §7.4 measured as fragile (211 candidate rules -> 55 accepted, i.e. 156 silent
denials). The second is that `read_fence.py` has a live owner; this is additive beside it, not a
rewrite of it.

Layering: `runtime` imports nothing above `core`; this module imports only stdlib.
"""
from __future__ import annotations

import os
import sys
from typing import Iterable, Optional

# The default set, as data rather than as code, so `looplab landlock-check` can print exactly what
# the launch will grant. `(path, mode)`; an entry that does not exist on this box is dropped at
# derivation (see `_add`'s `declared`) — these are MACHINE tiers, and `/lib32` being absent is a fact
# about the image, not a disagreement with anything the operator said.
#
# `/proc`, `/sys` and `/dev` are granted WHOLE and read-write on purpose. The single largest unretired
# unknown in this design is whether a real GPU eval survives a ruleset at all (doc 35 §7.2: CUDA,
# NCCL, `/dev/nvidia*`, `/dev/shm`, `/sys/class` and geesefs were never exercised — only `open`
# microbenchmarks), and the author of that section records that their own candidate allow-list omitted
# `/sys` and `/dev` and "would have failed such a run". Narrowing these is a change to make AFTER one
# real GPU eval has passed, with the eval that proves it, not before.
_DEFAULT_TIERS: tuple = (
    ("/usr", "read"), ("/lib", "read"), ("/lib64", "read"), ("/lib32", "read"),
    ("/bin", "read"), ("/sbin", "read"), ("/etc", "read"), ("/opt", "read"),
    ("/tmp", "readwrite"), ("/var/tmp", "readwrite"),
    ("/proc", "readwrite"), ("/sys", "readwrite"), ("/dev", "readwrite"),
)

# Environment variables that name a MODEL/dataset cache. Read-only: a cache miss that wants to write
# is a download, and a download into a shared cache from inside an eval is not something this
# boundary should quietly permit — the operator declares a mount for it, which is the same fix every
# other refusal here names.
_CACHE_ENV = ("HF_HOME", "HUGGINGFACE_HUB_CACHE", "HF_HUB_CACHE", "TRANSFORMERS_CACHE",
              "HF_DATASETS_CACHE", "TORCH_HOME", "TORCH_EXTENSIONS_DIR", "XDG_CACHE_HOME")


def _norm(path) -> Optional[str]:
    """Resolve one entry to the absolute, symlink-resolved form a rule is added at.

    Symlinks are resolved because Landlock rules are attached to an INODE (`open(O_PATH)` on the
    path), so granting the link and granting its target are the same grant — but only if we resolve
    consistently, or the same directory arrives twice under two spellings and the dedupe misses it.
    """
    if not path:
        return None
    try:
        p = os.path.realpath(os.path.expanduser(str(path)))
    except (OSError, ValueError):
        return None
    return p or None


def _add(out: dict, path, mode: str, *, declared: bool = False) -> None:
    """Record `path` at `mode`, with readwrite WINNING over read.

    The precedence matters and only in that direction: the workdir may also be reachable as a
    `/tmp` subpath on some hosts, and demoting it to read would make the node unable to write its own
    output. A grant is never narrowed by a later, broader tier.

    `declared` is the difference between the operator's paths and the machine's, and it is the whole
    reason this is one function with a flag rather than two lists. A MACHINE tier that is not on this
    box (`/lib32` here, `/opt` on a slim image) is not a fact about anything and is dropped at
    derivation; an OPERATOR-declared mount that is not there is a real disagreement and is kept, so
    the launcher refuses the eval by name instead of quietly running without it. Getting this
    backwards is not hypothetical: shipping `/lib32` into the ruleset made `landlock.apply` refuse
    every launch on this box in the first end-to-end test.
    """
    p = _norm(path)
    if p is None:
        return
    if not declared and not os.path.exists(p):
        return
    if out.get(p) == "readwrite":
        return
    out[p] = mode


def mount_sources(repo_spec: Optional[dict]) -> list:
    """`[(path, mode)]` for every declared `data:` / `references:` mount SOURCE.

    `edit: true` on a data source is the operator saying in-place writes are permitted — the same
    fact `engine/eval_dispatch.py::_data_binds` turns into a read-only-or-not container bind — so it
    maps to `readwrite` here and everything else to `read`. A bare-string data spec is the
    back-compat shape and is read-only, exactly as `_data_binds` treats it.
    """
    spec = repo_spec or {}
    out: list = []
    for _name, ds in (spec.get("data") or {}).items():
        if isinstance(ds, dict):
            if ds.get("path"):
                out.append((ds["path"], "readwrite" if ds.get("edit") else "read"))
        elif ds:
            out.append((ds, "read"))
    for ref in (spec.get("references") or []):
        if isinstance(ref, dict) and ref.get("path"):
            out.append((ref["path"], "read"))
    return out


def derive(*, workdir, run_dir=None, repo_spec: Optional[dict] = None,
           needs: Iterable = (), extra: Iterable = ()) -> list:
    """The whole allow-list for one eval launch, as an ordered, de-duplicated `[(path, mode)]`.

    `needs` is accepted and contributes NO rule, deliberately. Every `needs` entry is workdir-relative
    (`command_eval._validate_rel_paths` refuses an absolute or escaping one), so it is inside the
    workdir grant already. It is in the signature because it is the DECLARATION channel this
    allow-list is supposed to be derived from — doc 35 §4c — and because a future entry vocabulary
    that can name an outside input must arrive here and nowhere else. Silently omitting the parameter
    would leave the next author deriving a second allow-list at the call site.

    `extra` is for the caller's own paths (the run's log dir when it lives outside the run dir, a
    test's fixture root). Ordering is workdir first, then run dir, then the operator's mounts, then
    the interpreter, then the defaults — so a `landlock-check` printout reads most-specific-first and
    the operator sees their own declarations before the machine's.
    """
    out: dict = {}
    _add(out, workdir, "readwrite", declared=True)
    if run_dir:
        _add(out, run_dir, "readwrite", declared=True)
    for path, mode in mount_sources(repo_spec):
        _add(out, path, mode, declared=True)
    for item in extra or ():
        if isinstance(item, (tuple, list)) and len(item) == 2:
            _add(out, item[0], str(item[1]), declared=True)
        else:
            _add(out, item, "read", declared=True)
    # The interpreter itself. Without `sys.prefix` (site-packages) and `sys.base_prefix` (the stdlib
    # of a venv's base) the child python does not reach `import`; without `sys.executable`'s directory
    # a venv's `bin/python -> ../../bin/python3` cannot be exec'd.
    _add(out, sys.prefix, "read")
    _add(out, sys.base_prefix, "read")
    try:
        _add(out, os.path.dirname(os.path.realpath(sys.executable)), "read")
    except (OSError, ValueError, TypeError):
        pass
    for var in _CACHE_ENV:
        _add(out, os.environ.get(var), "read")
    _add(out, os.path.join(os.path.expanduser("~"), ".cache"), "read")
    for path, mode in _DEFAULT_TIERS:
        _add(out, path, mode)
    del needs   # see the docstring: workdir-relative by construction, so it adds no rule
    return list(out.items())


def refusal_hint(path) -> str:
    """The sentence a refused read must carry — it names the FIX, because the reader is the repair
    loop and after it the operator, and 1/116 measured refusals are LEGITIMATE work (node 36's
    teacher checkpoint). A boundary whose message is only "denied" is a boundary that costs repair
    attempts."""
    return (f"read refused: {path} is outside this eval's declared read set. This node runs in its "
            "own workspace; anything it legitimately reads from elsewhere has to be DECLARED — add a "
            "`data:` or `references:` mount for it in the task, or use a workdir-relative path. "
            "(`looplab landlock-check <run_dir>` prints the exact set this run grants.)")
