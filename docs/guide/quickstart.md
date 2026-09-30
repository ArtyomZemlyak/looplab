# Quickstart

## Assistant in the web UI

This is the usual path: describe a task in chat, check the experiment before it starts,
then read the measured result in the same workspace. It uses a live model for the chat
and LoopLab's internal research roles. For setup from a fresh checkout, use
[Installation](installation.md#source-install-for-the-web-ui).

1. Start the UI from the checkout with `looplab ui`, then open `http://127.0.0.1:8765`.
   On JupyterHub, use the LoopLab Launcher tile instead; see
   [JupyterHub onboarding](jupyterhub-onboarding.md).
2. In **LoopLab → Settings → Essential → Model**, check the model and endpoint, then **Save**. **Test active LLM**
   makes one provider request and may be billed. The local defaults (`qwen3:8b` at
   `http://localhost:11434/v1`) require a running Ollama server with that model pulled;
   saved values alone do not prove the model is reachable. The first-run Assistant shows
   the saved model and the last explicit test result on this page. Return to
   Runs after saving. **Experiments & resources** and **Time & model budgets** set defaults
   for new runs. Blank time limits and zero model budgets mean no cap. Cost budgets
   use estimates and require provider prices; Assistant chat is outside those run budgets.
3. Select **Start a new run** in Assistant. For example:

   > Improve accuracy on my dataset. The code is at `/path/on/the/LoopLab/server/repo`
   > and the data is at `/path/on/the/LoopLab/server/data`. Start with at most three
   > experiments. Show me the evaluation command and editable files before launch.

   Give the goal, accessible paths, and constraints. The Assistant can inspect a repo
   and ask for missing information. The paths must exist on the **LoopLab server**,
   which may be a different machine from your browser.
   Keep **Permissions · Plan** to discuss and prepare the proposal. Expand that control
   when you want to change how file edits and run controls are approved; each option
   explains what happens automatically. You can review and start the launch card in Plan.
4. Review **What this run will do** on the launch card: goal, score direction, paths,
   edit rules where applicable, and limits. Missing facts are marked explicitly.
   Choose **Validate**, inspect the effective preview and technical settings, then
   **Start run**. Changing the proposal invalidates its validation, so validate again.
   Chatting about a plan does not start a run. A live model or evaluator may incur cost;
   check the effective limits in the preview. Experiment and time limits alone do not cap
   model spending; set run model budgets in Settings if needed.
5. Open the run's **Report** for the baseline, best measured result, caveats, and
   solution artifacts. Ask Assistant to explain a failure or propose the next experiment.
   A single-seed result is exploratory until the configured confirmation actually runs.

## Offline CLI walkthrough

For an offline proof with no model or network, follow the [CLI walkthrough](cli-walkthrough.md).
It also covers result inspection, a regression example, live LLM setup, and crash recovery.
The web Assistant needs a configured model. To drive LoopLab directly from Codex or Claude Code,
follow the [external-agent quickstart](external-harness.md#first-external-run).
