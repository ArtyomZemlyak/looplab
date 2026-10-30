# Research campaign browser smoke

This CPU-only smoke starts a new synthetic run and an isolated LoopLab server on a temporary loopback
port. It uploads tiny per-query arrays through the native API and uses Chromium to follow a question,
open its genuine paired comparison, inspect a complete earlier report, open the question board and
return to the current report. It does not contact a live run or use a GPU. Start from the repository
root with the UI already built (`npm --prefix ui run build`).

Install Playwright and Chromium in a disposable directory and provide the module, browser directory,
Python, and PATH explicitly. For example:

```sh
npm install --prefix /tmp/looplab-playwright --no-save playwright@1.64.1
PLAYWRIGHT_BROWSERS_PATH=/tmp/looplab-playwright/browsers /tmp/looplab-playwright/node_modules/.bin/playwright install chromium
export LOOPLAB_PLAYWRIGHT_MODULE=/tmp/looplab-playwright/node_modules/playwright/index.mjs
export PLAYWRIGHT_BROWSERS_PATH=/tmp/looplab-playwright/browsers
export LOOPLAB_PYTHON=/path/to/looplab-python
tests/browser/run_research_smoke.sh
```

The server is stopped by the script's trap. Its temporary fixture and log remain for inspection. On a
minimal OS image Chromium may need additional shared libraries; install them in the test environment
or point `LD_LIBRARY_PATH` at an isolated extracted copy. `--no-sandbox` is used only for this
loopback fixture in environments where the browser sandbox cannot start.
