# LoopLab on JupyterHub: from nothing to a running experiment

Everything here runs inside your own single-user server. You never need admin on the hub.

Two halves:

* **[Part A — set it up once](#part-a-set-it-up-once)**, seven steps. Steps A5 and A6 are the two
  that cost people an afternoon, so they say what goes wrong as well as what to type.
* **[Part B — start a run by asking the assistant](#part-b-start-a-run-by-asking-the-assistant)**,
  which is how you will actually use it. A hand-written task file is the fallback, not the path.

Symptom → cause table at the [end](#when-something-is-wrong). Nothing in Part A repeats it.

---

# Part A — set it up once

## A1. Check what you have

```bash
python -V                            # need >= 3.11
nvidia-smi                           # only if you will train; the engine itself needs no GPU
echo "$JUPYTERHUB_SERVICE_PREFIX"    # /user/<you>/ — confirms you are inside a hub server
df -T ~                              # remember the answer; A5 needs it
```

## A2. Get the code and install

```bash
cd ~ && git clone https://github.com/ArtyomZemlyak/looplab.git
```

Then, from inside that directory:

```bash
pip install -e ".[jupyterhub,dev]"
```

`jupyterhub` pulls the UI, the Launcher tile and reliable process-tree kills. `dev` gives you the
suite — keep it; the next step is to prove the install rather than assume it.

## A3. Prove the engine works, offline

```bash
looplab run examples/toy_task.json --out runs/check --max-nodes 4 --backend toy
```

The toy backend needs no LLM and no network. A run directory with an `events.jsonl` means the engine
is sound. `python -m looplab.cli` is always equivalent if `looplab` is not on PATH.

## A4. Point it at an LLM endpoint

```bash
export LOOPLAB_LLM_BASE_URL="http://<endpoint>:<port>/v1"
export LOOPLAB_LLM_API_KEY="<key, or anything if the endpoint ignores it>"
```

The default is a localhost Ollama, which on a hub pod is rarely what you want. A wrong endpoint is
caught by preflight and the run is **refused before it starts**, exit code 2, naming every
unreachable role. A launch failure you can read beats a run full of empty fallback proposals.

## A5. Set the environment the hub needs

Put these where they survive a pod restart — `~/.bashrc` or your Spawner env:

```bash
export LOOPLAB_RUN_ROOT="$HOME/looplab-runs"
export LOOPLAB_UI_HOSTS="jupyterhub.example.org"   # YOUR hub's public hostname
export LOOPLAB_UI_DIST="$HOME/looplab/ui/dist"
```

**`LOOPLAB_UI_HOSTS` is the one that bites.** Without it the proxied UI answers
`{"detail":"untrusted Host header"}` and nothing else. `localhost` and `127.0.0.1` are always
allowed; your hub's real hostname is not, until you say so.

**Object-backed mounts (S3 / geesefs): probe, do not assume either way.** The event log needs
coherent append and real locking, so the engine probes at startup and fails closed rather than
corrupt a run. But "FUSE" is not itself the answer — on a geesefs-backed home `flock` is genuinely
enforced, and that deployment runs with no override at all. Test yours:

```bash
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

Even when it passes, the lock is enforced by the local FUSE daemon: a single-**node** guarantee. Two
pods on one bucket cannot see each other's locks — one engine per run directory per machine. And the
other traits bite regardless: no exec bit, symlinks flatten, `os.link` is `ENOTSUP`, and a `stat` can
cost most of a second. `LOOPLAB_ALLOW_UNLOCKED_WRITER=1` asserts "one engine writes here" on your
word instead of the kernel's; prefer moving the run root.

## A6. Build the UI bundle

```bash
looplab build-ui
```

On a plain machine that is the whole step. On an object-backed volume it fails twice and `build-ui`
names which one you hit in its own error output.

**No exec bit** — `npm` cannot execute `node_modules/.bin/vite`. Build where execution is allowed and
copy back; `dist/` is static and needs no exec bit to be served:

```bash
B=/tmp/uibuild && rm -rf $B && mkdir -p $B
cp -r ui/{package.json,package-lock.json,vite.config.*,index.html,src,scripts,public} $B/ 2>/dev/null
(cd $B && npm ci && npm run build) && cp -r $B/dist/. ui/dist/
```

**Node too old** — `ui/package.json` requires Node ≥ 20; hub images commonly ship 18. conda-forge may
be blocked by a proxy while `nodejs.org` is not:

```bash
node -v || true
curl -sSL -o /tmp/node.tar.xz https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz
tar -xf /tmp/node.tar.xz -C /opt/conda --strip-components=1
hash -r && node -v
```

`/opt/conda` is inside the container image, so this **does not survive a pod restart** — the durable
fix is Node ≥ 20 in the image or Spawner. Until then nobody can ship a UI change, and the bundle in
the browser stays whatever was last built.

**Publishing needs no restart** — the server reads `dist/` per request and serves `index.html`
`no-cache`. Verify rather than assume:

```bash
grep -oE 'assets/[A-Za-z0-9_-]+\.js' ui/dist/index.html | head -1
curl -s http://127.0.0.1:<port>/ | grep -oE 'assets/[A-Za-z0-9_-]+\.js' | head -1
```

Equal means the running server is on the new bundle.

## A7. Open it

```bash
looplab ui
```

Reach it from the JupyterHub **Launcher tile**, or directly at `/user/<you>/proxy/<port>/`. This is
where you will drive everything — Part B assumes it is open.

The same assistant conversation is available in the terminal without a browser, if you ever want it:

```bash
looplab tui
```

On a shared hub every user's app lives on ONE browser origin, so the control plane is token-gated by
default:

```bash
cat ~/.looplab/ui-token
```

Paste it when asked. `LOOPLAB_UI_TOKEN` sets your own value; `LOOPLAB_UI_ANONYMOUS=1` opts out —
loudly, and only on a private origin.

---

# Part B — start a run by asking the assistant

**This is the main path.** You describe the objective in chat; the assistant inspects the repo,
proposes a launch, and you approve it. Every launch — assistant proposal, genesis card, direct API
call — goes through the same `/api/start` funnel, so what you approve is what runs.

## B1. Open the UI and say what you want

Open the UI (Launcher tile, or `/user/<you>/proxy/<port>/` — started in A7) and go to the
**Assistant** chat. Describe the objective in plain language:

> *"Maximize test recall@100 on the ESCI v2 benchmark by fine-tuning the SentenceTransformer at
> /home/jovyan/data/embedder/d0rj/e5-small-en-ru. The repo is /home/jovyan/data/vectorizer-unified.
> Use dataset_version 2."*

## B2. What it needs from you, and what it works out itself

The assistant is a real agent with read-only scout tools: it reads the README, the entry script, the
requirements and the result files. **Give it only what it cannot reach.** The full rule, with the
measurement behind it, is in [tasks.md → *Writing a `goal`*](tasks.md); the short form:

| Tell it | Leave it out — it finds this |
|---|---|
| the objective and the metric's name | how the scorer prints it — it reads the entry script |
| where the repo and the artefacts are | what the config currently says — it reads the config |
| the data selector (`dataset_version`, split) | whether batch N fits — it probes the real machine |
| a **scale caveat** if your reference numbers came from a different corpus | prior runs' numbers — it queries the run record |
| | how many GPUs the box has — the engine stamps that in |

That last column is not a style preference. A goal carrying findings freezes them: measured on this
deployment, one task's goal had grown to 6,409 characters of which **70% were answers its own tools
produce**, and one line had rotted into a falsehood — it announced two metrics as "this run has
already measured" that belonged to a run four generations earlier. Keep the goal short and it stays
true.

## B3. Validate, then Start

The assistant returns a **launch card** — run id, task, budget, seed, policy, backend — as an inline
editor. Press **Validate**: the preview that follows is authoritative, and it shows the *effective*
backend. For repo / dataset / Kaggle tasks the launch defaults to `backend=llm`, so a UI-launched run
never silently falls back to the offline toy developer.

Verify the proposed **command, metric pattern and paths** before Start. Tool use is model-directed,
not a hard gate — the assistant can misread an entry script, and this is the moment that costs
nothing to catch. Editing any field invalidates the validation receipt; validate again.

If a field you need is not on the card, ask the assistant for a new proposal rather than hand-editing
around it.

## B4. Watch it, and ask for standing work

The Assistant chat is the control surface: ask what is running, why a node failed, what the last memo said.
You can also leave standing instructions — *"tell me when a node beats 0.78"* — which persist as
watches under `<runs>/assistant/.watches/` and append their findings to the same conversation.

## B5. The fallback paths

```bash
looplab run <task>.json --out runs/first          # a task file you wrote — see tasks.md
looplab run --goal "..." --kind repo --out runs/x # goal-only; onboarding proposes the eval, you approve
looplab init                                      # scaffold a documented config to edit
```

Use these when you want the task under version control, or when scripting. The assistant path and
these produce the same run.

---

## When something is wrong

| Symptom | Cause |
|---|---|
| `{"detail":"untrusted Host header"}` | `LOOPLAB_UI_HOSTS` does not name your hub's hostname (A5) |
| `/api/*` returns 401/403 | Expected on a shared origin — read `~/.looplab/ui-token` (A7) |
| Engine refuses to start, complains about the lock | Run root cannot lock. Move it (A5) rather than setting `LOOPLAB_ALLOW_UNLOCKED_WRITER` |
| Run refused with exit code 2 before any events | Endpoint preflight — the message names which role could not reach which URL (A4) |
| Runs vanish after a pod restart | Run root is not on a persistent volume. Ask which path is PVC-backed |
| `sh: 1: vite: Permission denied` | No exec bit on the mount — build elsewhere, copy `dist/` back (A6) |
| `node:util does not provide an export named 'styleText'` | Node older than `ui/package.json` requires (A6) |
| A fixed UI bug is still visible | The served bundle predates the fix. Rebuild and compare asset hashes (A6) |
| The run repeats something an earlier run settled | The goal is carrying findings, or it is not — check the run record with `looplab comparability` and see [tasks.md](tasks.md) |

## Where to go next

[tasks.md](tasks.md) — the task file and how to write a goal · [ui.md](ui.md) — the UI and the
assistant in full · [generating-code.md](generating-code.md) — letting the agent write the code ·
[llm-and-agents.md](llm-and-agents.md) — endpoints and coding-agent backends ·
[configuration.md](configuration.md) — every knob.
