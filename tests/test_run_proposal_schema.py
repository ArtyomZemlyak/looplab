"""ONE launch-proposal schema for the three New-run planners (doc 27, closed 2026-09-08).

`three-new-run-planners-no-shared-schema` was never about having three planners — the Web
Assistant's `propose_run`, the TUI's `/api/genesis` job and `looplab run --goal` are three ways to
AUTHOR a plan and each earns its existence. It was about each of them then spelling the PROPOSAL,
the `/api/start` body, the run-id slug and the settings filter for itself, so a move of the task
schema had to be repaired in three places and a card built by one surface could not be read by
another.

The tests below DRIVE that, they do not pin it by source text:

  * the three planners' cards round-trip through one another (the property a shared schema has and
    three hand-written dicts do not);
  * the two run-id slugs that used to differ now agree, including on the input where they differed;
  * the body every surface posts is the body the server's own funnel answers about — asserted
    against a real `/api/validate` and a real `/api/start`, not against a fixture of it;
  * `submit_warnings` is the one submit-warning rule, driven through the launch API AND the CLI's
    reporter over the same task.
"""
from __future__ import annotations

import pytest

from looplab.core.run_proposal import (LAUNCH_SECRET_FIELDS, LAUNCH_SETTING_FIELDS, MAX_SETUP_STEPS,
                                       RunProposal, normalize_launch_settings, slug_run_id)


def _toy() -> dict:
    return {"benchmark": "quadratic", "goal": "minimize the objective", "direction": "min"}


# --------------------------------------------------------------------------- the schema itself

def test_a_card_from_any_planner_round_trips_through_the_shared_shape():
    """The property three hand-written dicts never had: what one surface emits, another can read.

    A Web `propose_run` card carries a `proposal_id` the TUI's card does not, and a TUI card is
    normalized while the CLI's is not — none of that may change the six fields that ARE the
    proposal, so re-reading a card and re-emitting it is the identity.
    """
    web = RunProposal(run_id="titanic", task={"kaggle": "titanic", "goal": "g", "direction": "max"},
                      settings={"max_nodes": 12}, rationale="why", setup_steps=("one", "two"),
                      proposal_id="p-1", planner="web").card()
    reread = RunProposal.from_card(web)
    assert reread.card() == web
    assert reread.start_body() == RunProposal.from_card(reread.card()).start_body()
    # ...and a card that never carried the two provenance keys does not grow them.
    plain = RunProposal(run_id="x", task={"benchmark": "quadratic"}).card()
    assert set(plain) == {"run_id", "task", "task_file", "settings", "rationale", "setup_steps"}


def test_a_refine_turn_keeps_what_the_planner_omitted():
    """The genesis REFINE rule, which used to exist only inside `_normalize_genesis`: a partial emit
    (`{settings: {max_nodes: 50}}`) tweaks the operator's tuned card instead of wiping its task."""
    draft = RunProposal(run_id="tuned", task={"benchmark": "quadratic"},
                        settings={"max_nodes": 5, "n_seeds": 2}, rationale="first pass",
                        setup_steps=("check the data",)).card()
    refined = RunProposal.from_card({"settings": {"max_nodes": 50}}, draft=draft)
    assert refined.run_id == "tuned" and refined.task == {"benchmark": "quadratic"}
    assert refined.settings == {"max_nodes": 50, "n_seeds": 2}
    assert refined.rationale == "first pass" and refined.setup_steps == ("check the data",)


def test_one_run_id_slug_where_there_used_to_be_two_that_disagreed():
    """`serve/tui_format.py::slug` and the genesis router's `_slug` each had their own regex and a
    comment saying they must stay in step. They already did not: `"--a--"` slugged to `"a"` on the
    TUI and `"-a-"` in the router, because the router stripped ONE leading and ONE trailing dash."""
    from looplab.serve import tui_format

    assert tui_format.slug is not None
    for value in ("--a--", "  spaces  and---dashes ", "My Run!", "!!!", "", None, "x" * 80):
        assert tui_format.slug(value) == slug_run_id(value)
    assert slug_run_id("--a--") == "a", "a leading dash is a run name the launch funnel refuses"
    assert slug_run_id("x" * 80) == "x" * 40
    # and a name that slugs to nothing stays nothing, so a caller can detect it and fall back
    assert slug_run_id("!!!") == ""


def test_the_settings_filter_is_one_policy_and_the_start_body_deliberately_does_not_apply_it():
    """A card filters; a BODY does not.

    An unknown or secret key dropped on the way to the server is a launch that silently ignored what
    the operator asked for. The funnel answers it with a 422 that names the key, which is why
    `start_body` passes settings through untouched while the card-building surfaces normalize.
    """
    raw = {"max_nodes": 3, "llm_api_key": "sk-secret", "not_a_field": 1, "n_seeds": None}
    assert normalize_launch_settings(raw) == {"max_nodes": 3}
    assert RunProposal(run_id="r", settings=raw).start_body()["settings"] == raw
    assert LAUNCH_SECRET_FIELDS <= LAUNCH_SETTING_FIELDS
    # the one definition: `serve` re-exports it rather than keeping a second copy
    from looplab.serve.settings_store import _ALLOWED_FIELDS, _SECRET_FIELDS
    assert _ALLOWED_FIELDS == set(LAUNCH_SETTING_FIELDS)
    assert _SECRET_FIELDS == set(LAUNCH_SECRET_FIELDS)


def test_setup_steps_are_bounded_and_blank_steps_are_dropped():
    proposal = RunProposal.from_card({"setup_steps": ["  keep  ", "", None, *[f"s{i}" for i in range(30)]]})
    assert proposal.setup_steps[0] == "keep" and len(proposal.setup_steps) == MAX_SETUP_STEPS


def test_a_catalogue_task_file_wins_over_an_inline_task_in_the_body():
    """The funnel refuses a body carrying both; a card that has picked a file has picked a file."""
    body = RunProposal(run_id="k", task={"kind": "x"}, task_file="examples/toy_task.json").start_body()
    assert body == {"run_id": "k", "settings": {}, "task_file": "examples/toy_task.json"}


# ------------------------------------------------- the three planners emit the one shape, driven

def test_the_web_assistant_proposes_through_the_shared_schema():
    from looplab.tools.machine_runs_tools import RunLauncherTools

    tools = RunLauncherTools()
    out = tools.execute("propose_run", {
        "run_id": "web-card", "task": _toy(), "settings": {"max_nodes": 4, "llm_api_key": "sk-x"},
        "rationale": "because", "setup_steps": ["  a  ", ""]})
    assert "proposed run" in out
    card, = tools.proposals
    assert card["planner"] == "web" and card["proposal_id"]
    assert card["settings"] == {"max_nodes": 4}, "a secret may not reach a stored launch card"
    assert card["setup_steps"] == ["a"]
    # the card IS the shape: reading it back changes nothing
    assert RunProposal.from_card(card).card() == card


def test_the_tui_renders_and_posts_the_shared_shape():
    from looplab.serve import tui_format

    card = RunProposal(run_id="My Run!", task={"kind": "quadratic"},
                       settings={"max_nodes": 3}).card()
    assert tui_format.launch_body(card) == RunProposal.from_card(card).start_body()
    assert tui_format.spec_lines(card) == RunProposal.from_card(card).lines()
    # the empty case is the panel's own copy, not a property of a proposal
    assert tui_format.spec_lines({})[0].startswith("(no plan yet")


def test_the_cli_states_its_plan_in_the_same_words_the_tui_renders():
    """`looplab run --goal` is a planner too; since doc 27's row closed it announces the plan
    through `RunProposal.lines`, the exact renderer `spec_lines` uses."""
    from pathlib import Path

    from looplab.core.config import Settings
    from looplab.core.run_proposal import proposal_for_run_dir
    from looplab.serve import tui_format

    task = {"kind": "mlebench_real", "competition": "titanic"}
    settings = Settings(max_nodes=9, llm_api_key="sk-must-never-be-rendered")
    proposal = proposal_for_run_dir(Path("runs/from-a-goal"), task, settings, rationale="a reason")
    lines = proposal.lines()
    assert lines[0] == "run name : from-a-goal"
    assert "task     : mlebench_real · titanic" in lines
    assert "max_nodes=9" in "\n".join(lines) and "why      : a reason" in lines
    assert "sk-must-never-be-rendered" not in "\n".join(lines)
    assert lines == tui_format.spec_lines(proposal.card())


# ------------------------------------------------------- the body the schema builds is THE body

@pytest.mark.parametrize("card, ready", [
    ({"run_id": "shared-ok", "task": _toy(), "settings": {"max_nodes": 4}}, True),
    ({"run_id": "shared-bad", "task": {"goal": "no kind and no capability"}}, False),
    ({"run_id": "", "task": _toy()}, False),
])
def test_validate_proposal_asks_the_server_funnel_about_the_shared_shape(tmp_path, card, ready):
    """`serve/launch.py::validate_proposal` is `validate_launch` is `preflight_start` — the one rule
    `/api/start` refuses through. What changes is only that the caller no longer hand-builds the
    body it asks about."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from looplab.serve.launch import validate_proposal
    from looplab.serve.server import make_app

    app = make_app(tmp_path)
    client = TestClient(app)
    proposal = RunProposal.from_card(card)
    verdict = validate_proposal(app.state.looplab, proposal)
    assert verdict["ready"] is ready
    # the same verdict the HTTP route gives for the body the schema builds
    over_http = client.post("/api/validate", json=proposal.start_body()).json()
    assert over_http["ready"] is ready
    if ready:
        assert over_http["validation_token"] == verdict["validation_token"]
    else:
        assert over_http["code"] == verdict["code"] and over_http["status"] == verdict["status"]
    assert not (tmp_path / (proposal.run_id or "shared-ok")).exists(), "no side effect"


def test_a_start_bound_to_the_schemas_token_launches_that_exact_proposal(tmp_path, monkeypatch):
    """End to end: validate through the schema, then POST the SAME `start_body`. A drifted body
    would be refused as a stale receipt, which is the whole point of building it once."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from looplab.serve.launch import validate_proposal
    from looplab.serve.server import make_app

    spawned = []
    monkeypatch.setattr("looplab.serve.routers.control._spawn_engine",
                        lambda *args, **kwargs: spawned.append((args, kwargs)) or 4321)
    app = make_app(tmp_path)
    client = TestClient(app)
    proposal = RunProposal(run_id="bound", task=_toy(), settings={"max_nodes": 3}, planner="tui")

    verdict = validate_proposal(app.state.looplab, proposal)
    assert verdict["ready"] is True
    body = {**proposal.start_body(), "validation_token": verdict["validation_token"]}
    started = client.post("/api/start", json=body)

    assert started.status_code == 200, started.text
    assert spawned, "the launch bound to the schema's own body must be accepted"


# ---------------------------------------------------------- one submit-warning rule, two surfaces

def _repo_task_with_an_unprotectable_scorer(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "train.py").write_text("print('{\"score\": 1.0}')\n", encoding="utf-8")
    return {
        "goal": "maximize the score", "direction": "max", "repo": str(repo),
        # an argv naming NO in-repo entrypoint file at all (a shell wrapper) — LoopLab cannot
        # protect the code the score stage runs, which is exactly what the warning exists to say
        "cmd": {"command": ["bash", "run_eval.sh"],
                "metric": {"reader": "stdout_json", "key": "score"}},
    }


def test_the_launch_api_and_the_cli_print_the_same_submit_warnings(tmp_path, capsys):
    """The CLI's copy of these was written by hand from the server's, which is how a third warning
    lands on one surface only. Both now read `adapters/tasks.py::submit_warnings`; driven over a
    task that actually earns one, on both surfaces, and compared."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from looplab.adapters.tasks import submit_warnings, validate_task
    from looplab.cli.run_cmds import _report_task_warnings
    from looplab.serve.server import make_app

    spec = _repo_task_with_an_unprotectable_scorer(tmp_path)
    adapter = validate_task(dict(spec))
    expected = submit_warnings(adapter)
    assert expected, "the fixture must actually earn a warning, or this test proves nothing"

    verdict = TestClient(make_app(tmp_path / "root")).post(
        "/api/validate", json={"run_id": "warned", "task": spec}).json()
    assert verdict["ready"] is True
    assert all(warning in verdict["warnings"] for warning in expected)

    _report_task_warnings(adapter, dict(spec))
    printed = capsys.readouterr().err
    assert all(warning in printed for warning in expected)


def test_submit_warnings_is_total_over_a_task_that_earns_none():
    """Every launch surface calls it unconditionally, including on an injected dict adapter in
    tests, so it must answer `()` rather than raise for anything that is not a repo task."""
    from looplab.adapters.tasks import submit_warnings, validate_task

    assert submit_warnings(validate_task(_toy())) == ()
    assert submit_warnings({"kind": "not-an-adapter"}) == ()
