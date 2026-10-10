"""Every documented key recipe names the endpoint the key belongs to (doc 75 UX-23).

`LOOPLAB_LLM_API_KEY` is half of an ATOMIC pair with `LOOPLAB_LLM_API_KEY_BASE_URL` — a key is bound
to the URL it may be sent to, and `smoke`/`run` refuse a key set without its URL. That rule is a
security decision and stays. The walkthrough, the LLM page and the JupyterHub guide each told the
reader to set the key alone, so every user of a hosted model met that refusal on their first real
step. Held here: in every shell block of the entry pages and the guide, an assignment of the key
(commented or not) is followed in the same block by an assignment of its URL.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = [ROOT / "README.md", ROOT / "AGENTS.md", *sorted((ROOT / "docs" / "guide").glob("*.md"))]
KEY = re.compile(r"^\s*#?\s*(?:export\s+)?LOOPLAB_LLM_API_KEY=", re.M)
URL = re.compile(r"^\s*#?\s*(?:export\s+)?LOOPLAB_LLM_API_KEY_BASE_URL=", re.M)


def _shell_blocks(text: str):
    return re.findall(r"```(?:bash|sh|shell|console)?\n(.*?)```", text, re.S)


def test_every_key_assignment_in_a_shell_block_carries_its_base_url():
    unpaired = [f"{page.relative_to(ROOT)}: {block.strip().splitlines()[0]}"
                for page in PAGES if page.is_file()
                for block in _shell_blocks(page.read_text(encoding="utf-8"))
                if KEY.search(block) and not URL.search(block)]
    assert unpaired == []


def test_the_rule_is_red_on_the_recipe_that_shipped():
    shipped = "```bash\nexport LOOPLAB_LLM_BASE_URL=x\n# export LOOPLAB_LLM_API_KEY=sk-...  # hosted\n```"
    block = _shell_blocks(shipped)[0]
    assert KEY.search(block) and not URL.search(block)
