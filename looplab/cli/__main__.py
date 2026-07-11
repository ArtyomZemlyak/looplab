"""`python -m looplab.cli` — the module-execution twin of the console scripts.

The flat `looplab/cli.py` ended with `if __name__ == "__main__": app()`; after the package split
(docs/15 §P5.2) that spelling lives here, so subprocess callers keep working unchanged:
tests/test_end_to_end.py and tests/live/scenarios.py spawn `[sys.executable, "-m", "looplab.cli",
"run"/"resume", …]`, serve/engine_proc.py launches engines the same way, and the docs' `python -m
looplab.cli <command>` examples stay valid."""
from looplab.cli import app

if __name__ == "__main__":
    app()
