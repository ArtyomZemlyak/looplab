# Quickstart

## Assistant in the web UI

This is the usual path: describe a task in chat, check the experiment before it starts, then read
the measured result in the same workspace. It needs a model for the chat and for LoopLab's research
roles. To see the engine work first without one, run `looplab run examples/demo.yaml`
([CLI walkthrough](cli-walkthrough.md)).

1. **Start the UI.** From the checkout ([Installation](installation.md#source-install-for-the-web-ui)),
   run `looplab ui` and open `http://127.0.0.1:8765`. On JupyterHub use the LoopLab Launcher tile
   ([JupyterHub onboarding](jupyterhub-onboarding.md)).
2. **Connect a model.** Open **LoopLab → Settings → Essential → Model**, set **Model** and
   **Base URL**, **Save**, then **Test active LLM**. The test makes one provider request and may be
   billed. The defaults (`qwen3:8b` at `http://localhost:11434/v1`) need a running Ollama with that
   model pulled; `localhost` means the LoopLab server, not your browser's machine. Before the first
   run, Assistant shows the saved model and offers **Check connection…** beside the composer
   ([model setup](llm-and-agents.md#connect-assistant-in-the-ui)).
3. **Describe the goal.** In Assistant, choose **Start a new run**. **I have code** and **I have
   data** fill an editable example; replace the `[brackets]` and **Send**. For example:

   > Improve accuracy on my dataset. The code is at `/path/on/the/LoopLab/server/repo`
   > and the data is at `/path/on/the/LoopLab/server/data`. Start with at most three
   > experiments. Show me the evaluation command and editable files before launch.

   Paths must exist on the **LoopLab server**. Assistant can inspect a repo, ask for what is missing
   and help choose the evaluation command and metric. **Permissions · Plan** (the default) lets it
   discuss and prepare a proposal but change nothing.
4. **Review and start.** The launch card's **What this run will do** shows the goal, score
   direction, paths, edit rules and limits; missing facts are marked. **Validate** shows the
   effective task and settings, then **Start run** begins the experiment. Editing the proposal
   requires validating again. Experiment and time limits do not cap model spend: set **Run LLM
   budget (USD)** on the card or in Settings ([limits and cost](ui.md#launch-card-limits-cost-and-startup)).
   Chatting about a plan never starts a run.
5. **Read the result.** Assistant posts a short free summary after each experiment and when the
   run finishes; **Read Report** opens the evidence, **Open selected code** the solution. A score is
   called *better* only against a comparable recorded evaluation, and a single evaluation stays
   *unconfirmed* until repeat checks run — [how results are compared](ui.md#how-results-are-compared).

With **Language / Язык → Русский** the whole interface, including these buttons, is in Russian.

## Reuse a useful code change

A fix discovered in one experiment can become shared starting code for later experiments.
Start in Assistant with **Permissions · Plan**:

> Help reuse useful code changes in future experiments. Explain what can be shared,
> which checks are needed and their cost. Prepare a plan for review before changing the base.

For a run with a recorded code base, **Overview** or **Report → Code for future experiments → Choose a change
with Assistant** prepares this discussion. With **Language / Язык → Русский**, the panel
and its prepared question use Russian. The button fills the composer; **Send** contacts
the model and may incur provider charges. An existing draft is kept.

If reuse is disabled for that run, **Prepare with Assistant** asks for a **new** launch
proposal. This feature needs a recorded code archive, protected evaluation, and declared
tests and regression checks. Assistant can explain missing prerequisites; enabling it
requires reviewing the new task before launch. See
[task setup](tasks.md#promote-reusable-code-into-a-verified-base) for those requirements.

Reusing code does not copy a score or rewrite previous results. Checks measure the
proposed change and the original behavior; a base update needs explicit approval while
the engine is stopped. Resuming is a separate action. For Codex or Claude Code, the
[external-agent guide](external-harness.md) describes the same guarded workflow.

## Offline CLI walkthrough

For an offline proof with no model or network, follow the [CLI walkthrough](cli-walkthrough.md).
It also covers result inspection, a regression example, live LLM setup, and crash recovery.
To drive LoopLab directly from Codex or Claude Code, follow the
[external-agent quickstart](external-harness.md#first-external-run).
