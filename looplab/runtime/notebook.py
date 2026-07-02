"""I4 · Notebook export. Emit a run's champion solution as a runnable Jupyter notebook (.ipynb) —
the artifact data scientists actually want to take away. Builds nbformat-v4 JSON directly (no
nbformat dependency), so it stays zero-dep and works headless or from the UI.
"""
from __future__ import annotations


def champion_notebook(goal: str, code: str, *, params: dict | None = None,
                      metric=None, task_id: str = "", run_id: str = "") -> dict:
    """Build a minimal, valid nbformat-v4 notebook: a markdown header (goal/metric/params/provenance)
    + the champion's code as one runnable cell."""
    header = [
        "# Champion solution\n",
        f"\n*Exported from LoopLab run `{run_id}` (task `{task_id}`).*\n",
        f"\n**Goal:** {goal}\n",
        f"\n**Best metric:** {metric}\n",
        f"\n**Params:** `{params or {}}`\n",
    ]
    src = code if code.endswith("\n") else code + "\n"
    cells = [
        {"cell_type": "markdown", "metadata": {}, "source": header},
        {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
         "source": src.splitlines(keepends=True)},
    ]
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
