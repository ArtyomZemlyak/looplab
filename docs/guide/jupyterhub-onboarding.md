# Setting LoopLab up on JupyterHub, from nothing

A hand-over guide for a colleague getting their own LoopLab on a JupyterHub single-user server.
Nine steps, in order. Steps 5 and 6 are the two that people lose an afternoon to, so they say what
goes wrong as well as what to type.

Everything here runs inside your own single-user server — you never need admin on the hub.

---

## 1. Check what you have

```bash
python -V                    # need >= 3.11
nvidia-smi                   # only if you plan to train; the engine itself needs no GPU
echo "$JUPYTERHUB_SERVICE_PREFIX"   # e.g. /user/<you>/  — confirms you are inside a hub server
```

## 2. Get the code

```bash
cd ~            # or wherever your persistent home is
git clone https://github.com/ArtyomZemlyak/looplab.git
cd looplab
```

## 3. Install

```bash
pip install -e ".[jupyterhub,dev]"
```

`jupyterhub` pulls the UI (fastapi + uvicorn), `jupyter-server-proxy` for the Launcher tile and
`psutil` for reliable process-tree kills. `dev` gives you the test suite — keep it, you will want to
prove the install rather than assume it.

## 4. Prove the install before trusting it

```bash
looplab --help
looplab run examples/toy_task.json --out runs/check --max-nodes 4 --backend toy
```

The toy backend needs no LLM and no network. If this produces a run directory with an
`events.jsonl`, the engine works. If `looplab` is not on PATH, `python -m looplab.cli` is always
equivalent.

## 5. Point it at an LLM endpoint

```bash
export LOOPLAB_LLM_BASE_URL="http://<your-endpoint>:<port>/v1"
export LOOPLAB_LLM_API_KEY="<key, or anything if the endpoint ignores it>"
```

The default is a localhost Ollama, which on a hub pod is usually not what you want. A wrong or
unreachable endpoint is caught by the preflight and the run is **refused before it starts** — you
get one message naming every unreachable role and exit code 2. That is deliberate: a failed launch
you can read beats a run full of empty fallback proposals.

## 6. Set the four environment variables the hub actually needs

Put these in `~/.bashrc` (or your Spawner env) so they survive a pod restart:

```bash
export LOOPLAB_RUN_ROOT="$HOME/looplab-runs"
export LOOPLAB_UI_HOSTS="jupyterhub.example.org"     # YOUR hub's public hostname
export LOOPLAB_UI_DIST="$HOME/looplab/ui/dist"       # after one `looplab build-ui`
```

**`LOOPLAB_UI_HOSTS` is the one that bites.** Without it the proxied UI answers
`{"detail":"untrusted Host header"}` and nothing else — the server rejects every Host it was not
told about, to stop DNS rebinding. `localhost` and `127.0.0.1` are always allowed; your hub's real
hostname is not, until you say so.

**`LOOPLAB_RUN_ROOT` and object-backed FUSE (S3 / geesefs): check, do not assume.** The event log
depends on coherent append, tail repair, locking and fsync, and many object-backed mounts provide
none of them — so the engine probes the lock at startup and FAILS CLOSED with an actionable message
rather than corrupt a run. But "FUSE" is not the answer by itself. Measured on a geesefs-backed
JupyterHub home, `flock` is genuinely enforced: a parent takes the lock, a second process is refused
with `EWOULDBLOCK`. That deployment runs on it with no override at all.

Test YOUR mount rather than trusting either claim:

```bash
df -T ~                      # what am I actually on?
python - <<'PROBE'
import fcntl, subprocess, sys, textwrap
f = open("probe.lock", "a+"); fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
print(subprocess.run([sys.executable, "-c", textwrap.dedent("""
    import fcntl
    g = open("probe.lock", "a+")
    try:
        fcntl.flock(g.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        print("SECOND PROCESS GOT IT TOO -> the lock enforces nothing; use a local disk")
    except OSError:
        print("second process refused -> the lock is real on this mount")
""")], capture_output=True, text=True).stdout.strip())
PROBE
```

What holds even when the probe passes: the lock is enforced by the LOCAL FUSE daemon, so it is a
single-NODE guarantee. Two pods mounting the same bucket cannot see each other's locks — one engine
per run directory per machine, or nothing protects the log. And the other geesefs traits bite
regardless: no exec bit, symlinks flatten, `os.link` is `ENOTSUP`, and a `stat` can cost most of a
second, which is why several engine paths bound their filesystem walks.

`LOOPLAB_ALLOW_UNLOCKED_WRITER=1` exists for a mount that fails the probe and that you nonetheless
accept — it asserts "one engine writes here" on your word instead of the kernel's. Prefer moving the
run root.

## 6b. Build the UI bundle — two blockers, both measured on this deployment

```bash
looplab build-ui        # npm ci && npm run build in ui/
```

On a plain machine that is the whole step. On a JupyterHub whose data volume is object-backed it
fails twice, and `build-ui` predicts both in its own error output. **Read the failure text — it
names which one you hit.**

**Blocker 1 — no exec bit.** geesefs mounts carry none, so `npm` cannot execute its own
`node_modules/.bin/vite` and you get `sh: 1: vite: Permission denied`. Nothing about the build is
wrong; the filesystem simply refuses to run a file. Build somewhere execution IS permitted and copy
the result back — `dist/` is static files and needs no exec bit to be SERVED:

```bash
B=/tmp/uibuild && rm -rf $B && mkdir -p $B
cp -r ui/{package.json,package-lock.json,vite.config.*,index.html,src,scripts,public} $B/ 2>/dev/null
(cd $B && npm ci && npm run build)
cp -r $B/dist/. ui/dist/
```

**Blocker 2 — the Node on PATH is too old.** `ui/package.json` has required Node ≥ 20 since
2026-07-13; JupyterHub images commonly ship 18, and you get
`SyntaxError: The requested module 'node:util' does not provide an export named 'styleText'`.
conda-forge may be unreachable behind a corporate proxy while `nodejs.org` is not, so the direct
tarball is the reliable route:

```bash
node -v                 # if this is < 20, install one
curl -sSL -o /tmp/node.tar.xz https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz
tar -xf /tmp/node.tar.xz -C /opt/conda --strip-components=1   # /opt/conda/bin is ahead on PATH
hash -r && node -v      # v22.x
```

`/opt/conda` is inside the container image, so this **does not survive a pod restart** — the durable
fix is Node ≥ 20 in the image or the Spawner. Until it is there, nobody can ship a UI change: the
bundle in the browser is whatever was last built, and every later `ui/src` commit is invisible.
Measured here on 2026-08-19: the served bundle was four days old and **nineteen** `ui/src` commits
post-dated it, which made a fixed defect look live (the trace-paging fix had shipped its server half
and not its client half — the button did nothing).

**Publishing needs no restart.** The server reads `dist/` per request and serves `index.html`
`no-cache`, so copying a fresh bundle in is enough. Verify rather than assume:

```bash
grep -oE 'assets/[A-Za-z0-9_-]+\.js' ui/dist/index.html | head -1   # what the bundle references
curl -s http://127.0.0.1:<port>/ | grep -oE 'assets/[A-Za-z0-9_-]+\.js' | head -1   # what is served
```

Equal means the running server is on the new bundle. `LOOPLAB_UI_DIST` points the server at a bundle
built elsewhere, which is the tidier form of the same workaround.

## 7. Open the UI

Two ways, and they are equivalent:

```bash
looplab ui                              # then use the Launcher tile, or the proxied URL
```

The Launcher tile appears automatically (the `jupyter_serverproxy_servers` entry point) and proxies
to `/user/<you>/proxy/<port>/`.

**On a shared hub origin the control plane is protected by default.** Every user's app lives on ONE
browser origin, so an unauthenticated control plane there could be driven by any same-origin page.
The server mints a token into `~/.looplab/ui-token` (mode 0600), logs it at startup and denies
`/api/*` without it:

```bash
cat ~/.looplab/ui-token
```

Paste that into the UI when it asks. Set `LOOPLAB_UI_TOKEN` yourself if you prefer your own value.
`LOOPLAB_UI_ANONYMOUS=1` opts out — loudly, and only do it on a private origin.

## 8. Run the test suite once

```bash
python -m pytest -q -m "not docker"
```

It runs fully offline in a couple of minutes on an idle box. On a loaded box a handful of
wall-clock-sensitive tests can flake; re-run just those in isolation before believing a failure.

## 9. Your first real run

```bash
looplab run <your-task>.json --out runs/first
```

See [tasks.md](tasks.md) for the task file, [llm-and-agents.md](llm-and-agents.md) for endpoints and
coding-agent backends, and [configuration.md](configuration.md) for the knobs.

---

## If something is wrong

| Symptom | Cause |
|---|---|
| `{"detail":"untrusted Host header"}` | `LOOPLAB_UI_HOSTS` does not name your hub's hostname (step 6) |
| `/api/*` returns 401/403 | Expected on a shared origin — read the token from `~/.looplab/ui-token` (step 7) |
| Engine refuses to start, complains about the lock | Run root is on a FUSE/S3 mount that cannot lock. Move it (step 6) rather than setting `LOOPLAB_ALLOW_UNLOCKED_WRITER` |
| Run refused with exit code 2 before any events | Endpoint preflight — the message names which role could not reach which URL (step 5) |
| Runs vanish after a pod restart | The run root is not on a persistent volume. Ask your hub admin which path is backed by a PVC |
| `sh: 1: vite: Permission denied` | The mount carries no exec bit — build elsewhere and copy `dist/` back (step 6b) |
| `node:util does not provide an export named 'styleText'` | Node on PATH is older than `ui/package.json` requires (step 6b) |
| A fixed UI bug is still visible | The served bundle predates the fix. Rebuild and compare the asset hashes (step 6b) — nothing about this is cached in your browser |
