# LoopLab

> An autonomous ML/DS research engine. Give it a goal; it **invents → implements → tests → improves** candidate solutions in a loop and returns the best *verified* result.

[![Python](https://img.shields.io/badge/python-%3E%3D3.11-blue.svg)](https://www.python.org/)
[![Tests](https://github.com/ArtyomZemlyak/looplab/actions/workflows/tests.yml/badge.svg?branch=master)](https://github.com/ArtyomZemlyak/looplab/actions/workflows/tests.yml)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](#license)
[![Docs](https://img.shields.io/badge/docs-mkdocs--material-0f9c8c.svg)](https://artyomzemlyak.github.io/looplab/)

📖 **[Documentation](https://artyomzemlyak.github.io/looplab/)** · 🗺️ **[Architecture](https://artyomzemlyak.github.io/looplab/guide/architecture/)**

A **Researcher** proposes ideas, a **Developer** writes the code, a sandbox runs it, an evaluator
scores it, and the loop refines and merges the best candidates until the budget runs out. Every
decision is appended to an event log, so a run can be replayed and resumed after a crash.

## Install

Python ≥ 3.11. A source checkout also needs Node 20.19+, 22.13+ or 24+ for the first UI build
([exact versions and Windows commands](docs/guide/installation.md)).

```bash
git clone https://github.com/ArtyomZemlyak/looplab.git && cd looplab
python -m pip install -e ".[ui]"
```

## See it work in seconds — no model needed

```bash
looplab run examples/demo.yaml      # six experiments on a toy objective, fully offline
looplab inspect runs/demo           # best result, why it stopped, trust checks
looplab ui                          # open http://127.0.0.1:8765 and click the `demo` run
```

## Use it on your own problem

1. **Connect a model.** In the UI, open **LoopLab → Settings → Essential → Model**, set the model and
   endpoint (any OpenAI-compatible server: Ollama, vLLM, SGLang, OpenAI), **Save**, then
   **Test active LLM**. From a terminal, `looplab smoke` runs the same check.
2. **Describe the goal.** Click **Start a new run** in Assistant. Say what to improve, where the code
   or data live on the LoopLab server, and a limit such as "three experiments".
3. **Review and start.** Assistant drafts a launch card. **Validate** it, read the task, metric and
   limits, then **Start run**. Chatting about a plan never starts one.
4. **Read the result.** The run's **Report** and the chat summaries show the best score, how it
   compares with earlier experiments, and what is still unconfirmed.

The [Quickstart](docs/guide/quickstart.md) walks through these steps.

**Prefer the terminal?** `looplab init` writes a documented `looplab.yaml`; edit the task and run
`looplab run looplab.yaml`. The [CLI walkthrough](docs/guide/cli-walkthrough.md) covers inspection,
replay, a live model and crash recovery.

**Working in Codex or Claude Code?** Your coding agent can propose the candidates while LoopLab runs
and scores them: see the [external-agent quickstart](docs/guide/external-harness.md#first-external-run)
and [`AGENTS.md`](AGENTS.md).

## What it can work on

| You have | Task kind | Example |
|---|---|---|
| A dataset and a target | `dataset` — the agent writes the whole solution | `examples/dataset_task.json` |
| An existing repo with an evaluation | `repo` — the agent edits allowed files; the repo's own eval scores it | `examples/repo_task.json` |
| A Kaggle competition | `mlebench_real` — official split and grader | `examples/mlebench_real_spooky.json` |
| Just curiosity | `quadratic`, `regression`, `classification`, `timeseries` — offline synthetic tasks | `examples/demo.yaml` |

[`examples/README.md`](examples/README.md) says which examples need a model. The
[task reference](docs/guide/tasks.md) lists every field.

## Commands you will use

```bash
looplab init                       # scaffold a documented looplab.yaml
looplab run CONFIG|TASK [-s k=v]   # start a run; -s sets any non-secret setting
looplab ui                         # web UI with Assistant (needs the [ui] extra)
looplab inspect RUN_DIR            # best result first; --config adds the raw launch snapshot
looplab resume RUN_DIR             # continue a stopped or crashed run from its event log
looplab stop RUN_DIR               # stop without the wrap-up; resumable
looplab smoke                      # check the configured model endpoint
```

`looplab --help` groups all commands, starting with these. Every flag is in the
[CLI reference](docs/guide/cli-reference.md), and every setting in [Configuration](docs/guide/configuration.md).

## Good to know

- **No Docker needed locally.** Candidates run as sandboxed subprocesses, each in its own copy of
  your code, and may not read back into your source tree. The Docker tier (`--network none`) is for
  untrusted code on shared machines; the bundled Compose stack also serves a 30B model and needs a
  GPU with about 24 GB. See [Deployment](docs/guide/deployment.md).
- **The UI has no login by default** and binds to `127.0.0.1`. Before sharing it, set
  `LOOPLAB_UI_REQUIRE_AUTH=1` and `LOOPLAB_UI_CHECK_ORIGIN=1` (and `LOOPLAB_UI_HOSTS` behind a proxy);
  see [Web UI → Exposure & auth](docs/guide/ui.md#exposure-auth).
- **The default backend is `llm`.** A run without a reachable model is refused before it starts, with
  the fix named. Use `--backend toy`, or a file that sets it like `examples/demo.yaml`, to stay offline.
- **Results are honest about evidence.** A score is called better only when the two evaluations are
  recorded as comparable, and a single evaluation is labelled unconfirmed until repeat checks run.

## Documentation

| Guide | Contents |
|---|---|
| [Installation](docs/guide/installation.md) | Requirements, extras, Windows commands |
| [Quickstart](docs/guide/quickstart.md) | Your first run through Assistant |
| [CLI walkthrough](docs/guide/cli-walkthrough.md) | Offline runs, inspection, replay, a live model, crash recovery |
| [Web UI](docs/guide/ui.md) | Assistant, the run workspace, Report |
| [Tasks](docs/guide/tasks.md) · [Generating code](docs/guide/generating-code.md) | What a task file can say; letting the agent write the code |
| [LLM & coding agents](docs/guide/llm-and-agents.md) · [External harness](docs/guide/external-harness.md) | Models, per-role routing, external agents |
| [Concepts](docs/guide/concepts.md) · [Memory](docs/guide/memory.md) | Event log and replay, search, trust gates, cross-run memory |
| [CLI reference](docs/guide/cli-reference.md) · [Configuration](docs/guide/configuration.md) | Every command and every setting |
| [Deployment](docs/guide/deployment.md) · [MLE-bench runbook](docs/MLEBENCH.md) | Docker Compose, Kaggle competitions |

Design records — the *why* behind the architecture — are indexed in [`docs/00-INDEX.md`](docs/00-INDEX.md).
Contributors: start with [`CLAUDE.md`](CLAUDE.md) and [`tests/README.md`](tests/README.md).

## Testing

```bash
python -m pip install -e ".[dev,ui]"
python -m pytest tests/test_events_replay.py   # one file: seconds
python -m pytest                               # everything, fully offline: about 40 minutes
```

CI runs the suite in four shards, the UI tests, a packaging check and a Windows leg on every push.

## License

MIT — see [LICENSE](LICENSE).
