"""I4 champion -> Jupyter notebook export."""
from __future__ import annotations

import json

from looplab.notebook import champion_notebook


def test_notebook_is_valid_nbformat_v4():
    code = "import json\nprint(json.dumps({'metric': 0.1}))\n"
    nb = champion_notebook("minimize loss", code, params={"x": 3.0}, metric=0.1,
                           task_id="toy", run_id="r1")
    assert nb["nbformat"] == 4 and "cells" in nb
    assert nb["cells"][0]["cell_type"] == "markdown"
    assert nb["cells"][1]["cell_type"] == "code"
    # code cell preserves the source as a line list that round-trips
    assert "".join(nb["cells"][1]["source"]) == code
    # serializes cleanly
    assert json.loads(json.dumps(nb))["nbformat_minor"] == 5


def test_notebook_handles_code_without_trailing_newline():
    nb = champion_notebook("g", "print(1)", run_id="r")
    assert "".join(nb["cells"][1]["source"]).endswith("\n")
