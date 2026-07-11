# Real MLE-bench runs (D1)

LoopLab can run **real Kaggle competitions** from OpenAI's
[MLE-bench](https://github.com/openai/mle-bench): the engine is given the official `public/`
split (train + unlabeled test + sample submission + description), writes `submission.csv`, and
the **host** scores it with mle-bench's *real* grader against the held-out answers. The metric
the search optimises is therefore the genuine MLE-bench number, and each result carries the
official medal / above-median report derived from the real competition leaderboard.

This is `kind="mlebench_real"`, distinct from the synthetic `kind="mlebench"` (Gaussian blobs)
used for offline tests.

## Architecture (why it works on this box)

- **Auth.** The modern Kaggle `KGAT_…` tokens are **Bearer** tokens. The official `kaggle` PyPI
  client only speaks legacy Basic auth / OAuth and **cannot** use them. So LoopLab downloads
  competition data itself over HTTPS with stdlib `urllib` (`looplab/adapters/kaggle_dl.py`) — no kaggle
  client needed — then feeds the raw zip to mle-bench's real `prepare_fn`
  (`looplab/adapters/mlebench_prep.py`).
- **Grading is host-side and out-of-process** (`looplab/adapters/mlebench_grade.py`): the answer key lives
  only in the mle-bench data dir; `assets()` copies *only* the public files into the candidate
  workspace; grading runs in a child host process. The candidate can neither read nor self-report
  the score (trust model B1).
- **Candidate sandbox stays zero-dep:** the offline baselines and the LLM brief use **numpy + the
  Python standard library only** (CSV via `csv`). pandas/scikit-learn are needed *only* by the host
  grader, never inside the sandbox. Everything here is CPU-only.

## One-time setup

Already done on this box, but for reproduction:

```bash
# mle-bench package (host tooling only) — skip its heavy/incompatible deps (tensorflow,
# pycocotools, …); the CPU-lite comps need just pandas + scikit-learn.
git clone --depth 1 https://github.com/openai/mle-bench.git ../mle-bench-src
pip install -e ../mle-bench-src --no-deps
pip install pandas scikit-learn appdirs diskcache tenacity py7zr kaggle
```

> **sklearn note:** mle-bench's spooky grader calls `log_loss(y_pred=…)`, deprecated in
> scikit-learn 1.9 and **removed in 1.11**. Stay on `scikit-learn<1.11` for the spooky grader,
> or patch that one call upstream. The other two comps are unaffected.

### Token

The token is read (first non-empty wins) from `LOOPLAB_KAGGLE_TOKEN`, then `KAGGLE_KEY`, then
`~/.kaggle/kaggle.json` (`"key"` field). Either:

```bash
export LOOPLAB_KAGGLE_TOKEN="KGAT_…"
# or:  ~/.kaggle/kaggle.json  ->  {"username":"x","key":"KGAT_…"}   (username is ignored for Bearer)
```

Sanity check: `python -c "from looplab.kaggle_dl import check_auth; print(check_auth())"` → `True`.

## ⚠️ Accept competition rules (manual, per competition)

Kaggle returns **403** on download until you accept each competition's rules **once** on the
website (the API cannot do this for you). Click **"I Understand and Accept"** on each:

- https://www.kaggle.com/competitions/spooky-author-identification/rules
- https://www.kaggle.com/competitions/nomad2018-predict-transparent-conductors/rules

> `detecting-insults-in-social-commentary` is **skipped** (its rules can't be accepted on this
> account — download stays 403). The adapter + baseline still exist; prepare it by id if access
> is restored.

## Prepare the data

```bash
python -m looplab.adapters.mlebench_prep --selected           # the CPU-lite comps
# or one at a time:
python -m looplab.adapters.mlebench_prep -c spooky-author-identification
# add --verify to check the prepared split matches mle-bench's committed checksums
```

Data lands in `%LOCALAPPDATA%\mle-bench\data\<competition>\prepared\{public,private}`
(override with `--data-dir`). Re-runs are idempotent (already-prepared comps are skipped).

| competition | type | metric | direction |
|---|---|---|---|
| spooky-author-identification | text (3-class) | multi-class log-loss | lower better |
| nomad2018-predict-transparent-conductors | tabular | mean column-wise RMSLE | lower better |
| ~~detecting-insults-in-social-commentary~~ | text (binary) | AUC-ROC | _skipped (rules)_ |

## Run

```bash
# Offline baseline (numpy NB / ridge) — smoke test the whole pipeline, no LLM:
python -m looplab.cli run examples/mlebench_real_spooky.json --out runs/spooky --backend toy
```

### Live LLM via sglang (Qwen3-Coder-30B-A3B MoE)

The engine runs **on the host** (it needs mle-bench + pandas + the data dir there); the LLM is the
sglang container's OpenAI-compatible endpoint. Bring up sglang (`docker compose up -d sglang`),
then point LoopLab at it:

```bash
export LOOPLAB_LLM_BASE_URL="http://localhost:30000/v1"
export LOOPLAB_LLM_MODEL="qwen3-coder-30b-a3b"   # the served-model-name from docker-compose
export LOOPLAB_LLM_API_KEY="local"
python -m looplab.cli run examples/mlebench_real_spooky.json --out runs/spooky \
    --backend llm --max-nodes 8
python -m looplab.cli run examples/mlebench_real_nomad.json  --out runs/nomad  --backend llm --max-nodes 8
```

The candidate process runs in **UTF-8 mode** (`PYTHONUTF8=1`, set by the sandbox) so LLM-written
`open()` calls don't hit Windows' cp1252 default on the UTF-8 competition data.

> Default model is the non-hybrid `Qwen3-Coder-30B-A3B-Instruct-AWQ` (works on Blackwell today).
> Qwen3.6-35B-A3B (hybrid Mamba+MoE) currently crashes in SGLang on Blackwell — see `.env.example`.

The score, medal status, and `above_median` are graded automatically and recorded:
- the per-node official report (score + medal thresholds + above_median) is written as a
  `mlebench_report.json` artifact in each graded node's workdir,
- a `host_grading` event records the scorer + competition (never the answers).

For **true isolation** of LLM-written candidate code, set the untrusted tier (needs Docker):
`LOOPLAB_TRUST_MODE=untrusted`. The host grader still runs on the host; answers are never mounted.

## Notes / limits (v1)

- nomad's per-sample crystal **geometry files are not mounted** — the CPU baseline + brief use the
  tabular features only (the competition is solvable from them). Mounting geometry is a future add.
- Only mle-bench's bundled competitions (with a committed leaderboard) are supported — that's where
  the medal thresholds come from.
- To run other lite comps, add an offline baseline in `looplab/adapters/mlebench_real.py::_BASELINES` (or
  just use `--backend llm`, which needs no baseline).
