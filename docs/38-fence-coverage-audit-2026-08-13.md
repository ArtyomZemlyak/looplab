# Fence coverage audit: every way to touch a path from inside a fenced eval (2026-08-13)

**Status: an audit, plus the part of it that shipped.** The mutation half is implemented, tested and
in this same change (`looplab/runtime/read_fence.py`, `tests/test_read_fence.py`). The native-reader
half is a recommendation with a price, and nothing of it is implemented.

> **Status update (2026-08-14).** The native-reader half is no longer unimplemented: Landlock landed
> 2026-08-13/14 (`de8a6ef` the module, `3306ef7` the wiring, `0838253` an env-name fix) as
> `runtime/landlock.py` plus the mount-derived allow-list `runtime/read_allowlist.py`, applied at
> `run_argv` exactly as §3 recommends — and it ships OPT-IN, `Settings.landlock = "off"`. §3's own
> banner has the item-by-item ledger; §3 item 4 (geesefs / one real GPU eval) and item 2's
> engine-side `EACCES` translation are the two pieces still open.

Every verdict below was **measured on this box, on 2026-08-13, against the REAL generated fence** —
`read_fence.render(...)` written to a directory placed first on a child's `PYTHONPATH`, exactly as
`runtime/sandbox.py::run_argv` does it — over a synthetic editable source root in the session
scratchpad. Nothing under `/home/jovyan/data/vectorizer-unified` and no real run directory was read,
listed or touched. Where a claim is not measured it says so.

Interpreter: CPython **3.12.11** (conda-forge). Filesystems: scratch on `overlay`, run workdirs on
`fuse.geesefs`. This matters — several costs below differ by two orders of magnitude between them.

Prior art this builds on and, in two places, corrects:
[`docs/31-path-isolation-options-2026-08-13.md`](31-path-isolation-options-2026-08-13.md).

---

## 0. The one-paragraph answer

`open` is not the only way to touch a file, and the fence gated only `open`. **Fourteen of fourteen
mutation probes went through**, including `shutil.rmtree(<the operator's source root>)`, which
deleted the whole tree while every *read* of it was refused. That is closed, in this change, for
**34 ns/open** — the branch is a `dict.get` that a training process never reaches, because it raises
essentially no audited event except `open`. The other hole is not closable at this layer at all: a
**native reader** (`safetensors`, `h5py`, anything calling libc) raises no CPython audit event, so it
reads straight through, and no list of libraries fixes that because the list is infinite. The honest
answer is **a Python audit hook cannot cover native code, and the real fix is a kernel boundary** —
and on this box that boundary exists and is permitted: **Landlock ABI 2**, verified working, ~0
steady-state cost, ~1.4–3.5 ms of per-process setup. It should go **beside** the audit hook, never
instead of it, because a kernel refusal can only be `EACCES`, i.e. an `OSError`, i.e. exactly the
silent skip this fence's exception type was designed to avoid.

---

## 1. The complete surface

`hook sees it?` is about the **shipped** fence in this change. `→` marks a row this change moved.
"THROUGH" means the bytes came back, or the mutation happened, in a real fenced child.

### 1a. Reads through CPython — covered, and this is the fence's home ground

| surface | hook sees it | why | what would close it | cost |
|---|---|---|---|---|
| `builtins.open` / `io.open` | **BLOCKED** | audit `open` | — | shipped, +2.7 % |
| `io.FileIO` | **BLOCKED** | audit `open` | — | — |
| `os.open` + `os.read` | **BLOCKED** | audit `open` | — | — |
| `pathlib.Path.read_*` | **BLOCKED** | `os` underneath → `open` | — | — |
| `io.open_code` | **BLOCKED** | raises `open` (the *only* event it raises on 3.12.11 — there is no separate `open_code` row; measured) | — | — |
| bytes / `PathLike` argument | **BLOCKED** | `_resolve` fsdecodes it | — | — |
| `open(..., "w")` into the source | **BLOCKED** | `open` covers write modes | — | — |
| `mmap` over a Python-opened fd | **BLOCKED** | the `os.open` was audited; `mmap.__new__` carries only the fd | — | — |
| `shutil.copyfile` / `copytree` | **BLOCKED** | lowers to `open` | — | — |
| `numpy.load` / `fromfile` / **`memmap`** | **BLOCKED** | all three go through Python `open` (measured) | — | — |
| `torch.load`, incl. `mmap=True` | **BLOCKED** | `_open_file` → Python `open` (measured, both) | — | — |
| `PIL.Image.open` + `.load()` | **BLOCKED** | opens through Python, decodes in C (measured) | — | — |
| `SentenceTransformer(<source dir>)` | **BLOCKED** | first refused read is `README.md` — *incidental*, see §3 | — | — |
| a Python **subprocess** child | **BLOCKED** | `PYTHONPATH` is inherited | — | — |

### 1b. Mutation — the hole this change closes

All fourteen were **THROUGH** before; all fourteen are **BLOCKED** after. None of these raises `open`.

| surface | before → after | audit event, and its exact arg shape on 3.12.11 |
|---|---|---|
| `os.remove` / `os.unlink` | THROUGH → **BLOCKED** | `os.remove(path, dir_fd)` |
| `os.rename` / `os.replace` | THROUGH → **BLOCKED** | `os.rename(src, dst, src_dir_fd, dst_dir_fd)` |
| rename **into** the source | THROUGH → **BLOCKED** | same event, slot 1 |
| `os.truncate` (path **and** fd form) | THROUGH → **BLOCKED** | `os.truncate(path_or_fd, length)` — `os.ftruncate` raises the same |
| `os.chmod` | THROUGH → **BLOCKED** | `os.chmod(path, mode, dir_fd)` |
| `os.chown` | THROUGH → **BLOCKED** | `os.chown(path, uid, gid, dir_fd)` |
| `os.utime` | THROUGH → **BLOCKED** | `os.utime(path, times, ns, dir_fd)` |
| `os.mkdir` / `os.makedirs` | THROUGH → **BLOCKED** | `os.mkdir(path, mode, dir_fd)` |
| `os.rmdir` | THROUGH → **BLOCKED** | `os.rmdir(path, dir_fd)` |
| `os.symlink` **into** the source | THROUGH → **BLOCKED** | `os.symlink(target, link, dir_fd)` |
| `os.link` source → workdir (the hardlink escape) | THROUGH → **BLOCKED** | `os.link(src, dst, src_dir_fd, dst_dir_fd)` |
| `os.setxattr` / `os.removexattr` | THROUGH → **BLOCKED** | `(path_or_fd, attr, value, flags)` / `(path_or_fd, attr)` |
| **`shutil.rmtree(<source root>)`** | THROUGH → **BLOCKED** | see below — the worst one |
| **`os.remove(<name>, dir_fd=…)`** (`unlinkat`) | THROUGH → **BLOCKED** | CPython audits the **RELATIVE** name |

Three things in that block are worth stating rather than leaving in a table.

**`shutil.rmtree` of the source ROOT deleted the whole tree.** rmtree of a *subdirectory* was already
refused, which is what made the surface look narrower than it was. The root itself is different: its
own name carries no trailing separator, so `os.open('/src/repo')` misses the prefix test; rmtree then
gets a dirfd and unlinks every file with `os.remove(<bare name>, dir_fd=fd)`, which CPython audits
with the relative name, which no prefix compare can match. Measured: `RESULT:ROOT GONE`.

**`shutil.*` events are deliberately NOT in the registry.** Each lowers to an `os.*` event or an
`open` on the same path — `copyfile`→`open`, `copymode`→`os.chmod`, `copystat`→`os.utime`+`os.chmod`,
`move`→`os.rename`, `rmtree`→`os.remove`+`os.rmdir` — verified under a recording hook. A `shutil` row
would be a second name for a refusal that has already happened.

**The registry is re-derived by a test, not remembered.** `MUTATION_EVENTS` maps event → (path slot,
dir_fd slot), and `test_mutation_arg_shapes_match_the_interpreter` performs every one of those calls
under a recording audit hook in a subprocess and asserts the declared slots are the ones actually
holding the paths. A CPython release that inserts an argument would otherwise leave the fence
checking `args[0]` of something that is no longer a path: it would still install, still run, and
silently stop refusing.

### 1c. Symlinks, descriptors and the `chdir` premise — partly closed here

| surface | hook sees it | why | what closes it | cost |
|---|---|---|---|---|
| `os.chdir(<source>)` then relative `open` | BLOCKED | `os.chdir` hook | — | — |
| **`os.chdir(<workdir symlink → source>)`** | THROUGH → **BLOCKED** | `abspath`/`normpath`, not `realpath` | resolve on the rare path | shipped |
| **`os.chdir(<fd of source>)`** | THROUGH → **BLOCKED** | CPython audits the bare int | `/proc/self/fd` | shipped |
| **mutation under a symlinked directory** | THROUGH → **BLOCKED** | dirname now resolved, memoized | — | shipped |
| **read** through a workdir symlink (file) | THROUGH | path fence, documented | `realpath` per open | **+254 %**, ~474 µs/call on geesefs — refused |
| **read** through a workdir symlink (dir) | THROUGH | same | memoized `realpath(dirname)` | +6.1 % (doc 31) |
| **read** through a hardlink into the workdir | THROUGH | no path relation exists at all | inode identity, or a kernel fence | see §4 |
| `os.open(name, dir_fd=…)` (`openat`) | **THROUGH — structurally** | the `open` audit event carries `(path, mode, flags)` and **no dir_fd**; the hook cannot resolve the relative name *even in principle* | a kernel fence | see §4 |

That fourth-from-last row corrects the module docstring's own reasoning, and doc 31 §3(b) spotted it
first: the relative-path fast bail was justified *because* a process cannot `chdir` into a root, and
it could — through a symlink, and through a descriptor. Both are closed now. `chdir` and the mutation
events resolve; `open` still never does, and that asymmetry is the design.

### 1d. Metadata and enumeration — NOT closed, deliberately

| surface | hook sees it | why | what would close it | decision |
|---|---|---|---|---|
| `os.stat` / `os.lstat` / `os.access` / `os.statvfs` | THROUGH | **no audit event exists** | nothing at this layer | cannot |
| `os.path.exists` / `getsize` | THROUGH | ditto | — | cannot |
| `os.readlink` | THROUGH | no audit event | — | cannot |
| `os.listdir` / `pathlib.iterdir` | THROUGH | `os.listdir` event, unwatched | add the event | **not done** |
| `os.scandir` / `os.walk` | THROUGH | `os.scandir` event, unwatched | add the event | **not done** |
| `glob.glob` | THROUGH | `glob.glob` + `os.scandir` | add the event | **not done** |

Enumeration leaks **names, never bytes**, and refusing it would be partial by construction: `os.stat`
raises no audit event at all, so "does this path exist" leaks regardless. The line drawn here is
*deny where the audited event is the complete channel for the effect* (which mutation is, at the
CPython layer) and *report where the fence could only ever be partial*. The asymmetry in
consequences also matters: a false refusal kills a 76-minute node, while a leaked directory listing
cannot corrupt a metric — a stage still has to `open` something to get a number, and that is refused.
Adding `os.listdir`/`os.scandir` is one row each in `MUTATION_EVENTS`' sibling dispatch if the
operator wants it; it is a decision, not an omission.

### 1e. Native readers — THROUGH, and not closable here

Measured first-hand, each in its own fenced child, against real files in the fenced root:

| surface | hook sees it | why |
|---|---|---|
| **`safetensors.torch.load_file`** | **THROUGH** — `tensor([0., 1.])` came back | Rust `std::fs`, never a CPython event |
| **`safetensors.safe_open`** | **THROUGH** | same |
| **`h5py.File(...)` read** | **THROUGH** — the array came back | the HDF5 **C** library opens the file |
| `ctypes` → libc `open` / `fopen` | **THROUGH** — `fd=3` | no audit event |
| `mmap` over a ctypes-opened fd | **THROUGH** | the fd was never audited |
| `pyarrow` | **not installed here**; same class as h5py by construction | C++ file layer |
| `sqlite3.connect` | THROUGH | raises `sqlite3.connect`, not `open` |

`numpy.memmap`, `numpy.load`, `torch.load` (both the zipfile and the `mmap=True` path) and
`PIL.Image.open` are all **BLOCKED**, because each opens through Python and only then hands a
descriptor or a buffer to C. That is the actual dividing line, and it is invisible from the outside:
`torch.load` is safe and `safetensors.load_file` is not, for reasons internal to two libraries that
do the same job on the same file type.

**This is why an allow-list of libraries is the wrong instrument, and the operator is right to refuse
one.** The property is not "safetensors is dangerous"; it is "any reader that does not enter CPython
is invisible here". A list would have to name every such library that exists or ever will, and would
be wrong the day `torch.load` changes its file layer.

### 1f. Children and exec — THROUGH, and the delivery mechanism is the reason

| surface | hook sees it | why | what would close it |
|---|---|---|---|
| python child (`subprocess`, default env) | **BLOCKED** | `PYTHONPATH` is inherited, so it re-installs | — |
| **non-python child** (`cat`, `rm -rf`, a compiled trainer) | **THROUGH** | there is no interpreter to hook | a kernel fence |
| **`python -S`** child | **THROUGH** | `-S` skips `site`, so `sitecustomize` is never imported |a kernel fence |
| **`python -E`** child | **THROUGH** | `-E` ignores `PYTHONPATH` | a kernel fence |
| **`python -I`** child | **THROUGH** | isolated mode implies both | a kernel fence |
| child with `PYTHONPATH` stripped | **THROUGH** | the marker is an env var | a kernel fence |
| `os.execv` into a non-python binary | **THROUGH** | the process image is replaced | a kernel fence |
| `subprocess.Popen` / `os.exec*` / `os.posix_spawn` themselves | audited (`subprocess.Popen`, `os.exec`, `os.posix_spawn`, `os.system`, `os.fork`) but **not watched** | — | refusing a *launch* is a different policy |

The `-S`/`-E`/`-I` rows are new here — doc 31 tested a plain python child and a `cat` child, not
these. They matter because they are not adversarial: `-E` and `-I` appear in real launcher scripts
and in some `torchrun`/conda wrappers, and any of them silently unfences a stage. **A stage command
that is not python at all is fenced by nothing**, which is the same fact stated in the module
docstring, now with the two extra ways a *python* stage loses it.

We could watch `subprocess.Popen`/`os.exec` and refuse a launch that strips the fence. That is a
different and much more intrusive policy (it fails a node for how it spawns, not for what it reads),
it is trivially evaded by `os.posix_spawn` or a shell, and it is exactly the kind of enumeration the
kernel option makes unnecessary — landlock restrictions are inherited across `exec` and cannot be
dropped, verified on this box.

---

## 2. What shipped, and what it cost

Closed in this change: all of §1b, plus the three `→ BLOCKED` rows in §1c.

`MUTATION_EVENTS` (twelve events) + `_fenced_target` (fd resolution through `/proc/self/fd`, dir_fd
joins, memoized `realpath` of the dirname) + a distinct `MUTATION_REFUSAL_MESSAGE`. The refusal keeps
the deliberate **non-`OSError`** shape: `except OSError: <fall back>` is exactly as routine around a
cleanup `os.remove` as it is around a read, and a `PermissionError` would turn the refusal into the
silent skip the fence exists to prevent. `warn` still lets mutations through and logs them; the
violation log gained a fifth column naming the event that was attempted.

**Measured** (N=20,000, best-of-5, one **fresh process per variant** — an audit hook can never be
removed, so measuring two variants in one process reports cumulative cost. `OLD` is this same fence
one commit earlier, i.e. the marginal price of the branch):

| workload | no hook | OLD | NEW | marginal |
|---|---|---|---|---|
| `open` + read 4 KiB | 11,865 ns | 12,155 (+2.4 %) | 12,189 (+2.7 %) | **+34 ns/open (+0.3 %)** |
| bare `os.open`/`close` (worst case for the ratio) | 4,633 ns | 5,076 (+9.6 %) | 5,162 (+11.4 %) | +86 ns |
| create + close + **remove**, in the node's own workdir | 23,014 ns | 23,820 (+3.5 %) | 24,400 (+6.0 %) | **+580 ns/pair** |
| per-process startup | 8.68 ms | 9.73 ms | 9.75 ms | +0.02 ms |

The read hot path pays 34 ns, inside the noise of the fence that was already there, because a
training process raises no audited event except `open` and the mutation branch is one `dict.get` it
never reaches. What genuinely pays is a **legal** mutation of the node's own workspace: +580 ns per
create/remove pair, for the memoized `realpath` of its directory. A node deleting ten thousand
checkpoint shards spends 6 ms on this.

**Doc 31 §5.3 priced a "9-event set membership" variant at +3.9 % on the read path. That is not what
shipped and not what this costs**: that variant tested set membership *before* the `open` compare.
Ordering the `open` fast path first makes the difference between +3.9 % and +0.3 %.

Escape battery over the real generated fence, whole-file: **5 BLOCKED / 34 THROUGH before, 21 BLOCKED
/ 20 THROUGH after.**

Tests (`tests/test_read_fence.py`, 53 passing, was 31) drive the property against the filesystem, not
the source text — a real fenced child tries to delete the operator's checkpoint and the file is still
there afterwards, with an unfenced control half that really deletes it. Per CLAUDE.md a source pin
would be one comment away from vacuous here; "the bytes are still on disk" is not.

---

## 3. The native-reader hole: options, prices, recommendation

The constraint is explicit and correct: **no hand-maintained allow-list of libraries**, and yet if a
path is legitimately readable and `safetensors` is the only way to read it, that must still work. It
is a PATH fence — the question is *which path*, never *which library*.

This is live, not theoretical. v6 ran merge nodes, and a merge stage calling `load_file(ckpt)`
directly is the ordinary way to write one. The fence stopped v6 node 4's read only **incidentally**:
`sentence_transformers` happens to open `README.md` and `modules.json` through Python first. Nobody
should rely on that.

### The four candidates, priced

Landlock, seccomp and the mount namespace were probed by running them on this box. LD_PRELOAD numbers
are from that probe plus doc 31 §5.4.

| mechanism | available here | covers native readers? | non-python children? | symlinks? | mutation? | survives exec | overhead | setup |
|---|---|---|---|---|---|---|---|---|
| **Landlock ABI 2** | **YES** (`landlock_create_ruleset(NULL,0,VERSION)` → 2) | **yes** — safetensors blocked | **yes** — `cat` blocked | **yes**, target resolved | yes | **yes, irrevocable** | ~0 (within noise) | 1.4–3.5 ms/process |
| seccomp | installs fine (`NoNewPrivs=1`, filter → `Seccomp: 2`) | **irrelevant** | — | — | — | yes | — | — |
| mount namespace, ro bind | **NO** | — | — | — | — | — | — | — |
| `LD_PRELOAD` shim | yes (gcc 13.3) | yes, *dynamic only* | yes, *dynamic only* | only with `realpath` | if you hook those too | **no** | ~0 unsafe / **+190 %** symlink-safe | ~0.4 ms |

**Seccomp cannot do this, as a matter of design, not of configuration.** A seccomp filter sees the
syscall number and the scalar register arguments and **cannot dereference the path pointer**. It can
block `openat` entirely; it cannot block `openat` *of one directory*. It is the wrong instrument and
no amount of privilege changes that.

**The mount namespace is blocked on this box.** `unshare -U --map-root-user` works and
`/proc/sys/user/max_user_namespaces` is 8,255,467, but plain `unshare -m` is EPERM and
`unshare -Urm --propagation unchanged` — which does enter the namespace with full capabilities —
then fails **every** mount with rc=32: `mount --bind -o ro`, `mount -o remount,bind,ro`, binding an
empty directory over the source, and `mount -t tmpfs` alike. The container runs under AppArmor
profile `cri-containerd.apparmor.d (enforce)`, which is the uniform denial signature. And the detail
that settles it: **geesefs stays fully mounted and readable inside the new namespace** — so even a
partial namespace gives no isolation of the tree run workdirs live on. Doc 31 §5.5 marked this
UNVERIFIED and design-only; it is now verified, and the answer is no. (Not verified: the AppArmor
policy text itself — `/etc/apparmor.d` is absent and `dmesg` unreadable — so "AppArmor blocks it" is
inferred from the profile name plus the uniform rc.)

**`LD_PRELOAD` is a soft guardrail, not a boundary.** Two bypasses were demonstrated, not argued: a
`syscall(SYS_openat, …)` through `ctypes` read the file (the shim wraps libc symbols, not syscalls),
and a child that does `env -u LD_PRELOAD` read it too. It also needs a compiled `.so` per
architecture and per libc — the first non-pure-Python artifact in `runtime/` — and being symlink-safe
costs `realpath` on every open, ~+190 % here and far worse on geesefs.

### Recommendation: Landlock ABI 2, applied at `run_argv`, ALONGSIDE the audit hook

> **Status update (2026-08-14).** Implemented, opt-in, OFF (`Settings.landlock`, `off`/`enforce`;
> `runtime/landlock.py`, wired through `engine/resources.py::_landlock_allow` →
> `runtime/sandbox.py::run_argv`). The five priced items, one by one: **(1)** the allow-list
> inversion was resolved the mount-derived way — `runtime/read_allowlist.py` grants the operator's
> declared surfaces (workdir, run dir, mounts, interpreter, model cache, machine tiers) and never
> walks the complement; an absent machine tier is dropped at derivation, an absent DECLARED mount is
> kept so the launcher refuses it by name. **(2)** the refusal still degrades to `EACCES`, and the
> engine-side translation into the fence's sentence is NOT built; the root close is at the repair
> boundary — when the eval ran under `enforce` and a stage fails with `EACCES`/`FileNotFoundError`
> naming a path outside the derived allow-list, rewrite the failure text handed to triage with the
> fence's own message before the Developer sees it (`engine/evaluate.py`, where `res.stderr` feeds
> `_triage_crash`). **(3)** unchanged — ABI 2, metadata unmediated, the audit hook keeps
> `ftruncate`. **(4)** STILL THE LARGEST UNKNOWN and the reason for `off`: no ruleset has been run
> through a real GPU eval or geesefs. `looplab landlock-check <run_dir>` (`cli/inspect_cmds.py`) is
> the shipped procedure — zero skipped rules, then ONE real train+score under
> `LOOPLAB_LANDLOCK=enforce`, then flip the default; the evidence bar is written into
> `Settings.landlock`'s comment. **(5)** as recommended: applied outermost in `run_argv` as a
> LAUNCHER (not a `preexec_fn`), Docker tiers skipped, with an ABI probe and a clean hook-only
> fallback (`abi_version`/`unavailable_reason`).

It is the only mechanism here that is a real kernel boundary **and** permitted **and** cheap. Applied
to a scratch source directory it produced, verified by running it:

* python `open` → EACCES; a `/bin/cat` **child** → `Permission denied`; **`safetensors.load_file`
  → blocked**; a read through a **symlink** to the tree → blocked (the resolved target is checked);
  write and unlink → blocked; the rest of the filesystem unaffected;
* it **survives `exec`**: an `os.execv` into a fresh python image kept the restriction, and that
  process's own `/bin/cat` grandchild was still denied. Restrictions are inherited and **cannot be
  dropped** — an attempt to install a permissive ruleset afterwards changed nothing;
* steady-state cost ~0 (6,090 → 6,290 ns/open, within noise); setup 1.4–3.5 ms per process.

It is a PATH mechanism and library-blind by construction, which is exactly the operator's stated
requirement.

**Five things to price honestly before anyone starts.**

1. **It is an ALLOW-list, and it unions down the ancestor chain.** There is no way to say "deny this
   subtree": `landlock_add_rule` with an empty access set is `ENOMSG`, and a nested rule cannot revoke
   a broader grant. "Everything except P" has to be expressed by walking root→P and granting every
   **sibling** off that path — 44 rules in the probe. That inversion is the real work, and it has a
   failure mode the current fence does not: **a top-level directory created after the ruleset is
   built is not granted, and reads of it fail**. A long-lived training process that touches a path
   nobody enumerated at launch gets EACCES. This needs a design decision (grant `/` broadly and rely
   on the deny-by-omission of only the source roots' *parents*? re-derive per exec?), not just code.
2. **The refusal degrades to `EACCES`.** That is `PermissionError`, that is `OSError`, and
   `except OSError: <fall back>` is the shape the whole non-`OSError` design exists to defeat. Worse,
   measured: **`safetensors` converts it to `FileNotFoundError: No such file or directory`** — under
   both landlock and the LD_PRELOAD shim — which is *actively misleading* to the repair loop. So
   landlock must be **added beside** the audit hook, never instead of it: the hook keeps the good
   message on the Python path, landlock is the backstop for what the hook cannot see, and the engine
   should translate a landlock-era `EACCES`/`FileNotFoundError` on a source path into the fence's own
   sentence. The audit hook is not made redundant by this; it is made the *diagnostic* half.
3. **ABI 2 does not mediate metadata.** `os.stat` still works under it — the same §1d gap.
   `LANDLOCK_ACCESS_FS_TRUNCATE` is ABI 3, so `ftruncate` on an already-open descriptor is not
   mediated here either; the audit hook covers that one.
4. **Not tested against geesefs.** The probe restricted overlay scratch directories only. Landlock
   rules on a FUSE mount are unexercised, and run workdirs are on FUSE. This is the single largest
   unknown and should be the first thing anyone checks.
5. **Where to apply it.** In the child, before the stage command execs — i.e. `run_argv`, the same
   choke point that already prepends `PYTHONPATH`. Applying it from the generated `sitecustomize`
   would cover a python stage and everything it spawns, but not a stage command that is not python,
   which is half the point. It also must not apply to the Docker tiers, which are fenced by
   construction.

**Price: ~3–5 days.** ~150 lines of `ctypes` (no build artifact, no packaging change — a real
advantage over the LD_PRELOAD option doc 31 priced at ~1 week plus per-arch packaging), the
ancestor-walk allow-list derivation and its refresh policy, an ABI probe with a clean fallback to
hook-only on a kernel without it, the geesefs verification, engine-side translation of `EACCES` into
the fence's message, and tests that drive a real `safetensors` read being refused.

### The inode-preflight option, and the recorded measurement it has to answer to

CLAUDE.md records the fence as *a PATH fence and not an inode one, because `realpath` per open cost
+88 % against the prefix compare's +2.8 %*. **That measurement stands and this change does not
overturn it — it narrows it.** Doc 31 re-measured the same thing at +254 % on a 9-component path and
~474 µs per call on geesefs, so if anything the recorded figure was optimistic. What this change
establishes is that the *conclusion* was over-generalized: `realpath` is unaffordable **per open**,
and free **per rare event**. `os.chdir` and the twelve mutation events now resolve fully, memoized
per directory, and the read hot path still measures +34 ns. The rule is not "no realpath"; it is "no
realpath on the hot path".

A pure inode preflight — stat the declared checkpoint before the stage, remember `(st_dev, st_ino)`,
and compare afterwards — is a different mechanism and is worth its own note: it is off the hot path
entirely, so it costs nothing, but it is **detect-after**, the GPU hours are already spent, and it
cannot see a read the node never declared. Doc 31's O2 (bind the metric to a digest of the artifact
the scorer actually read) is the strictly better version of that idea and is the one to build; it is
also the only option in either document that survives a *successful* read, which is what the v6
node 4 incident actually was.

---

## 4. What was deliberately NOT done

> **Status update (2026-08-14).** The second bullet is superseded: the landlock implementation now
> exists (see the banner in §3 — opt-in, OFF, with the geesefs/GPU validation and the `EACCES`
> translation still open). The other bullets stand as written; the read-side symlink and hardlink
> residuals are closed only for a run that opts into `landlock="enforce"`.

* **No library allow-list.** Explicitly refused, per the constraint, and §1e says why it could never
  have worked: `torch.load` is covered and `safetensors.load_file` is not, for reasons internal to
  those libraries.
* **No landlock implementation.** It is a recommendation with a price. It needs the allow-list
  inversion decision in §3.1 and the geesefs verification in §3.4 first, and it changes the refusal
  message for every fenced read, which is a policy change the operator should make knowingly.
* **`os.listdir` / `os.scandir` not refused.** §1d: partial by construction, and the false-refusal
  cost is asymmetric.
* **No refusal of `subprocess`/`exec` launches.** §1f: it fails a node for how it spawns rather than
  for what it reads, and it is evaded by a shell.
* **The read-side symlink and hardlink residuals stay open**, unchanged and still documented. Closing
  them at this layer means `realpath` per open, which the recorded measurement rules out; the kernel
  option closes them for free.
* **`os.open(dir_fd=…)` reads stay open.** Not a decision — the `open` audit event carries no dir_fd,
  so the hook cannot close it at all.
* **Nothing was done about the live run.** Doc 31 §7a still holds: a running engine predates the
  fence and cannot acquire it without a restart.

## 5. Reproducing this

Everything is in the session scratchpad, nothing in the repo or under `runs/`:

```
<scratchpad>/f34/
  events_probe.py   every path-touching operation under a RECORDING audit hook   -> §1 arg shapes
  battery.py        41-probe escape battery vs the real generated fence          -> §1, §2
  native.py         native-reader probes (safetensors/h5py/numpy/torch/PIL)      -> §1e
  bench.py          hot-path cost, one fresh process per variant                 -> §2
<scratchpad>/kprobe/   landlock / seccomp / mount-ns / LD_PRELOAD probes         -> §3
```

`bench.py` takes the variant as `argv[1]` and must be run once per variant. Note that probing
landlock required installing `safetensors` 0.8.0 into the box's conda environment — that is a real
side effect of this audit and the only one outside the scratchpad.
