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

## 6b. Build the UI bundle once

```bash
looplab build-ui        # npm ci && npm run build in ui/
```

Then point `LOOPLAB_UI_DIST` at the result, so the server never attempts a build at request time.
This matters most on exactly the mounts above: **geesefs carries no exec bit**, so `npm` launched
from a package directory there can fail outright. Build somewhere execution is permitted, then serve
the finished `dist/`. `looplab ui --no-build` then starts instantly.

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
| The UI tries to `npm build` and fails | `LOOPLAB_UI_DIST` unset; run `looplab build-ui` once (step 6) |
