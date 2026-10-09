# Tests

The suite runs fully offline: no model, no network, no GPU, no Docker (tests that need one skip).

```bash
python -m pip install -e ".[dev,ui]"
python -m pytest tests/test_events_replay.py              # one file — always start here (seconds)
python -m pytest -m "not corpus and not docker"           # skip the slow bench-corpus and Docker tests
python -m pytest --splits 4 --group 1 --splitting-algorithm least_duration   # one CI shard
python -m pytest --basetemp=/tmp/my-run                   # give concurrent runs their own tmp root
python -m pytest                                          # everything: about 40 minutes
npm --prefix ui test                                      # the UI's node --test suite
```

**Finding the tests for a change.** Files are named after the property they pin, not the module, so
search by the symbol you touched: `grep -rln "my_function\|my_module" tests/`. Most behaviour has a
driving test (it runs the real code) and some has a source pin (it reads the code's text); a red pin
quotes the rule it guards in its docstring.

**Things that surprise newcomers.**

- `--basetemp` matters when two runs share a machine: pytest's tmp root is keyed by OS user, so two
  concurrent runs delete each other's fixtures.
- Several guards are *censuses* with shrink-only backlogs in `tests/data/` (blind `except`
  handlers, oversized settings rows, unresolved citations). A new violation is red; fixing an old
  one means deleting its row.
- Some tests read the documentation: a new CLI command must be named in the guide, a new setting
  needs its row in `docs/guide/configuration.md`, and a new numbered design document moves the count
  in `test_documentation_contracts.py`.
- Live-model tests run only with `LOOPLAB_LIVE_SCENARIOS=1`; the shell's LLM credentials are removed
  for the session otherwise (`tests/conftest.py`).
- Do not edit source while the suite runs: source pins read files from disk.

The rules behind these guards are in [`CLAUDE.md`](../CLAUDE.md).
