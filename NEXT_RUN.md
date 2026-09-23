# Launching the next e5small run

`eval_env` is an ENGINE SETTING, not a field of the task file. Every task snapshot on this box
carries `eval.env = {}` — v2, v4 and v11 got their data root from the SETTING, and v12 went out
without it because it was launched from a copied task snapshot alone (#147). The result: every v12
node crashes on S3 (`InvalidAccessKeyId`) and pays a triage+repair to rediscover the local corpus.

`core/config.py::Settings.eval_env` writes down why it is a setting and not only a shell export,
and `core/models.py::RunState.eval_env` records what it decides: **which corpus a node trained
on**. A run whose
corpus is chosen per-node by a repair is not comparable to the 0.793426 champion.

## The command

    cd /home/jovyan/data/looplab
    cp runs/<previous-run>/task.snapshot.json e5small_vNN.json
    # THEN PUT THE CORPUS ROOT IN THE TASK FILE, not only in the launch line below:
    #   "eval": { ..., "env": {"VS_LOCAL_DATA_ROOT": "/home/jovyan/data/dr-local"} }
    # A SETTING rides config.snapshot.json (so a RESUME reproduces it) but NOT
    # task.snapshot.json — and the copy above is exactly how the next run starts. That is how
    # v12 lost it: every node crashed on S3 and node 14 died. `run_started` now records
    # `eval_env_absent_from_task: true` when the setting is carrying a fact the task does not.
    python -m looplab.core.claimpin e5small_vNN.json          # must report 0 claim defects
    nvidia-smi                                                # both H200s must be free
    setsid nohup python -m looplab.cli run e5small_vNN.json \
        --out runs/e5small-dr-unified-vNN \
        --backend llm --max-nodes 24 \
        -s eval_env=VS_LOCAL_DATA_ROOT=/home/jovyan/data/dr-local \
        > runs/e5small-dr-unified-vNN.console.log 2>&1 < /dev/null &

`setsid`, because a plain `nohup` dies with the tool timeout. Then verify BOTH before reporting a
launch: a live pid AND a non-empty `events.jsonl`.

## After launch, confirm the corpus rather than assuming it

    python -c "import json; d=json.load(open('runs/e5small-dr-unified-vNN/config.snapshot.json')); print(d['eval_env'])"

must print `{"VS_LOCAL_DATA_ROOT": "/home/jovyan/data/dr-local"}`. If it prints `{}` the run will
still work — each node repairs its way to a corpus — but every node's root has to be checked
individually before any metric is compared to the champion.

---

## v14 (2026-09-17): the dependency half, which nothing in this file used to mention

A fresh container loses the whole ML stack — measured again today, 11 of 14 modules the testbed
imports were absent (`transformers`, `sentence_transformers`, `pytorch_lightning`, `faiss`,
`polars`, `accelerate`, `loguru`, `bm25s`, `nltk`, `mlflow`, `ecom.s3`). **You do not hand-install
them.** `looplab/runtime/deps.py` does it: `find_declaration` reads a `requirements.txt` at the
EDITABLE ROOT and `_do_run_setup` runs `pip install -r` there once, before node 0.

**It read nothing here until today, and that is what cost the earlier runs.** `find_declaration`
looks at `base / "requirements.txt"` with NO walk, and `vectorizer-unified`'s real file is at
`vectorsearch/requirements.txt`, one level down; its root holds only `pyproject.toml` and
`poetry.lock`, both deliberately observe-only. So the engine found no declaration and fell back to
its crash-time installer, which resolves BARE NAMES to the latest release — the failure
`deps.py`'s own comment measured: transformers-5.14.1 against a repo pinning 4.51.0,
pytorch_lightning-2.6.5 against a repo pinning 1.5.1, and `runs/rubert-dr-0807` node 0 dead on a
Lightning 2.x default with 7 of its 12 repair attempts artifacts of an API nobody asked for.

There is now a curated `/home/jovyan/data/vectorizer-unified/requirements.txt`. It is NOT a copy of
the one under `vectorsearch/`, and the header there says why per line; the two that matter are that
`numpy>=1.24,<2.0` would downgrade the installed numpy under a torch built on the 2.x ABI, and
`faiss-gpu-cu12==1.9.0.0` dies at IMPORT here.

### The launch line needs NO_PROXY, for the same reason it needs VS_LOCAL_DATA_ROOT

`ecom-s3`/`ecom-mlflow` are imported unconditionally (`vectorsearch/config.py:16`,
`vectorsearch/test.py:10`) and live on the internal index only. This session exports
`HTTPS_PROXY=http://127.0.0.1:18080`, so pip sends an internal host through an external exit and
fails `SSL: UNEXPECTED_EOF_WHILE_READING` — which reads exactly like "the host is down". Measured
2026-09-17: unreachable with the proxy, **HTTP 200** with `nexus.samokat.io` in `NO_PROXY`.

It belongs in the LAUNCH LINE and NOT in `eval.env`: `_do_run_setup` calls
`_run_argv(cmd, cwd, to, log_path=…)` with **no `env=`**, so the declaration install inherits the
ENGINE's environment. `eval.env` reaches the eval STAGES; it does not reach run_setup.

### Know this before you launch, because it moves a shared interpreter

run_setup will DOWNGRADE `pandas` 3.0.5 -> 2.3.3 and `protobuf` 7.36.0 -> 6.33.6. That is what the
repo's own dependencies ask for, not a loose pin: the two `ecom-*` packages require
`mlflow >=2.22.0,<2.23.0`, and mlflow 2.22.x requires `pandas<3` and `protobuf<7` — pinning pandas
at the installed version makes the resolve IMPOSSIBLE. The run records the delta in its
`deps_installed` receipt. The 2026-08-11 incident on this box was a pandas downgrade performed
while a node was importing pandas, so **launch on a quiet box** and check for other agents' pytest
runs first.

### The v14 command

    cd /home/jovyan/data/looplab
    python -m looplab.core.claimpin e5small_v14.json     # must report 0 claim defects
    nvidia-smi                                           # the H200s must be free
    NO_PROXY="$NO_PROXY,nexus.samokat.io" no_proxy="$no_proxy,nexus.samokat.io" \
    setsid nohup python -m looplab.cli run e5small_v14.json \
        --out runs/e5small-dr-unified-v14 \
        --backend llm --max-nodes 24 \
        -s eval_env=VS_LOCAL_DATA_ROOT=/home/jovyan/data/dr-local \
        > runs/e5small-dr-unified-v14.console.log 2>&1 < /dev/null &

`e5small_v14.json` already carries `eval.env` (so a copy of its snapshot cannot lose the corpus the
way v12 did) AND the setting is still passed, because the two travel in different snapshots. Then
verify a live pid AND a non-empty `events.jsonl` before reporting a launch, and confirm the corpus
with the `config.snapshot.json` check above.
