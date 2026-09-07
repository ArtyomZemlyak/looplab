"""What KIND of edit a node made, and whether its lineage already tried the other side (row 31).

A diff says WHICH lines changed and nothing about what KIND of change it is — and the kind is where
the field found the gains: EvoTrace classified committed edits over 121 agent runs into nine types,
found the improvements concentrated in three, and measured that ~30 % of ADDED lines were lines the
same lineage had already DELETED, a share rising over the run in 118 of the 121. LoopLab shipped the
diff and neither fact.

These drive the classifier over real Python, and the re-introduction detector over a built lineage
rather than a mocked one, because the property is about ANCESTRY: a line deleted two nodes back and
added now is the signal; the same line deleted in a SIBLING's branch is not.
"""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.tools import node_diff as nd
from looplab.tools.node_diff import EDIT_TYPES, NO_DIFFERENCE, NOT_RECOVERABLE, NodeDiffTools


class _N:
    def __init__(self, nid, files=None, parents=()):
        self.id, self.files = nid, files or {}
        self.parent_ids = list(parents)
        self.idea = type("I", (), {"params": {}})()
        self.attempt, self.status, self.metric, self.metric_provenance = 0, "ok", None, None


class _S:
    def __init__(self, *nodes):
        self.nodes = {n.id: n for n in nodes}


@pytest.mark.parametrize("line,kind", [
    ("import torch", "import"),
    ("from sklearn.metrics import f1_score", "import"),
    ("# lr = 3e-4 was too high", "comment"),
    ('    """the docstring line"""', "comment"),
    ("def train(model, loader):", "definition"),
    ("class Trainer:", "definition"),
    ("@torch.no_grad()", "definition"),
    ("    for epoch in range(epochs):", "control_flow"),
    ("    if loss < best:", "control_flow"),
    ("    return model", "control_flow"),
    ("LEARNING_RATE = 3e-4", "hyperparameter"),
    ("    batch_size = 512,", "hyperparameter"),
    ("    dropout: float = 0.1", "hyperparameter"),
    ("    optimizer = Adam(model.parameters(), lr=1e-4)", "call_argument"),
    ("    df = pd.read_csv(path)", "data_io"),
    ("    model = AutoModel.from_pretrained(name)", "data_io"),
    ("    print(f'loss={loss}')", "logging"),
    ("    wandb.log({'loss': loss})", "logging"),
    ("", "whitespace"),
    ("        x = y + z", "other"),
])
def test_the_classifier_answers_one_of_the_closed_vocabulary(line, kind):
    assert nd.classify_line(line) == kind
    assert kind in EDIT_TYPES


def test_the_code_part_decides_not_the_comment():
    """`lr = 3e-4  # tuned` is a hyperparameter edit that happens to carry a comment; `# lr = 3e-4`
    is a comment. A classifier that reads the whole line calls both the same."""
    assert nd.classify_line("lr = 3e-4  # tuned down") == "hyperparameter"
    assert nd.classify_line("# lr = 3e-4  # tuned down") == "comment"
    # a `#` INSIDE a string is not a comment marker
    assert nd.classify_line("    sep = '#'") == "hyperparameter"


def test_every_changed_line_is_counted_under_exactly_one_type():
    before = "import os\nlr = 1e-3\n\ndef f(x):\n    return x\n"
    after = "import os\nimport sys\nlr = 3e-4\n\ndef f(x, y):\n    print(x)\n    return x + y\n"
    left = _N(1, files={"train.py": before})
    right = _N(2, files={"train.py": after}, parents=[1])
    state = _S(left, right)
    counts = nd.classify_edits(nd.node_record(state, 1), nd.node_record(state, 2))
    assert counts["files"] == 1
    assert set(counts["added"]) <= set(EDIT_TYPES) and set(counts["removed"]) <= set(EDIT_TYPES)
    assert counts["added"].get("import") == 1 and counts["added"].get("hyperparameter") == 1
    assert counts["added"].get("logging") == 1 and counts["removed"].get("hyperparameter") == 1
    # the totals ARE the diff's line counts — nothing is dropped between the two
    assert sum(counts["added"].values()) == len(counts["added_lines"])


def test_engine_bookkeeping_files_are_not_classified():
    """The same noise rule the diff itself uses: `train.log` differs between any two nodes."""
    state = _S(_N(1, files={"train.log": "a\n"}), _N(2, files={"train.log": "b\n"}, parents=[1]))
    counts = nd.classify_edits(nd.node_record(state, 1), nd.node_record(state, 2))
    assert counts["files"] == 0 and not counts["added"]


def _lineage_state():
    """node 1 writes a line, node 2 DELETES it, node 3 puts it back byte for byte."""
    base = "import torch\nSCHEDULER = 'cosine'\nlr = 1e-3\n"
    without = "import torch\nlr = 1e-3\n"
    back = "import torch\nSCHEDULER = 'cosine'\nlr = 5e-4\n"
    return _S(_N(1, files={"train.py": base}),
              _N(2, files={"train.py": without}, parents=[1]),
              _N(3, files={"train.py": back}, parents=[2]))


def test_a_line_the_lineage_already_deleted_is_reported_as_re_introduced():
    state = _lineage_state()
    cycle = nd.reintroduced_lines(state, 3)
    assert cycle["depth"] == 3 and cycle["count"] == 1
    assert cycle["examples"][0]["line"] == "SCHEDULER = 'cosine'"
    assert cycle["examples"][0]["deleted_between"] == "node 1 -> node 2"
    # and the node that only CHANGED a line has nothing to report about itself
    assert nd.reintroduced_lines(state, 2)["count"] == 0


def test_a_deletion_in_a_sibling_lineage_is_not_this_nodes_history():
    """The lineage is the first-parent chain on purpose: node 4 branches from node 1 and adds the
    line node 2 deleted — but node 2 is not node 4's ancestor, so nothing was re-introduced."""
    state = _lineage_state()
    state.nodes[4] = _N(4, files={"train.py": "import torch\nSCHEDULER = 'cosine'\nlr = 2e-3\n"},
                        parents=[1])
    assert nd.reintroduced_lines(state, 4)["count"] == 0


def test_a_trivial_line_is_not_evidence_of_cycling():
    """`)` and `pass` come and go in any refactor; counting them would put the rate near 100 %."""
    state = _S(_N(1, files={"m.py": "f(\n    a,\n)\n"}),
               _N(2, files={"m.py": "f(a)\n"}, parents=[1]),
               _N(3, files={"m.py": "f(\n    a,\n)\n"}, parents=[2]))
    assert nd.reintroduced_lines(state, 3)["count"] == 0


def test_a_cycle_in_the_parent_chain_terminates():
    """A hand-edited log can name a parent that names it back — the walk must not hang."""
    a, b = _N(1, files={"m.py": "x = 1\n"}), _N(2, files={"m.py": "x = 2\n"}, parents=[1])
    a.parent_ids = [2]
    state = _S(a, b)
    assert nd.lineage(state, 2) == [1, 2] or nd.lineage(state, 2) == [2, 1]
    nd.reintroduced_lines(state, 2)      # must return, not recurse


def test_the_tool_section_says_the_kind_and_the_cycling():
    state = _lineage_state()
    tools = NodeDiffTools()
    tools.bind_state(state)
    out = tools.execute("diff_nodes", {"left": 2, "right": 3, "section": "edits"})
    assert "edit types:" in out and "hyperparameter" in out
    assert "re-introduced lines: 1 of" in out and "node 1 -> node 2" in out
    # and it is part of the default answer, not an opt-in corner
    assert "edit types:" in tools.execute("diff_nodes", {"left": 2, "right": 3})


def test_an_unreadable_file_set_is_not_reported_as_no_edit():
    """The module's headline property, extended to the new section: absence is not identity."""
    state = _S(_N(1, files={"train.py": "x = 1\n"}), _N(2, parents=[1]))
    out = "\n".join(nd.diff_edits(nd.node_record(state, 1), nd.node_record(state, 2), state))
    assert NOT_RECOVERABLE in out and NO_DIFFERENCE not in out
    same = _S(_N(1, files={"train.py": "x = 1\n"}),
              _N(2, files={"train.py": "x = 1\n"}, parents=[1]))
    out = "\n".join(nd.diff_edits(nd.node_record(same, 1), nd.node_record(same, 2), same))
    assert NO_DIFFERENCE in out


def test_the_instrument_counts_the_run_and_names_what_paid(tmp_path):
    """`looplab edit-types` over a real log: the table, the direction-aware gain, the cycling rate."""
    rd = tmp_path / "run"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    files = ["import torch\nSCHEDULER = 'cosine'\nlr = 1e-3\n",
             "import torch\nlr = 1e-3\n",
             "import torch\nSCHEDULER = 'cosine'\nlr = 5e-4\n"]
    for index, text in enumerate(files):
        store.append("node_created", {"node_id": index, "parent_ids": [index - 1] if index else [],
                                      "operator": "improve", "files": {"train.py": text},
                                      "code": text,
                                      "idea": {"operator": "improve", "params": {}, "rationale": ""}})
        store.append("node_evaluated", {"node_id": index, "metric": 1.0 - 0.1 * index})
    result = CliRunner().invoke(app, ["edit-types", str(rd)])
    assert result.exit_code == 0, result.output
    assert "edit types over 2 parent->child pair(s), 2 with both metrics (direction=min)" in result.output
    assert "hyperparameter" in result.output and "improved" in result.output
    assert "re-introduced lines: 1 of" in result.output
    assert "node 2:" in result.output and "SCHEDULER = 'cosine'" in result.output
    # the fold is the source: the gain is direction-aware, so every pair here IMPROVED
    assert "2/2" in result.output


def test_the_instrument_says_so_when_there_is_nothing_to_classify(tmp_path):
    rd = tmp_path / "run"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "files": {"train.py": "x = 1\n"}, "code": "x = 1\n",
                                  "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    result = CliRunner().invoke(app, ["edit-types", str(rd)])
    assert result.exit_code == 0 and "nothing to classify" in result.output
    assert fold(EventStore(rd / "events.jsonl").read_all()).nodes[0].files
