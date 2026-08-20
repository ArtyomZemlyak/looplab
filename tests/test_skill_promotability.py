"""A SKILL is a technique a later run could apply — not a fact about one node.

"The auto classifier is rubbish, it needs improving." It is not bad at ranking; it never asked the
question. The promotion gate tested three things — supported, positive delta, non-empty statement —
and none of them is "does this generalize". Measured 2026-08-12 over the 27 auto-skills the shipped
store had accumulated, EVERY ONE was instance-specific:

    perturb best node 8 (metric=5.4404437)
    perturb node 9 (params={'x': 3.7898})
    mean-merge of nodes 0,1

…including the same operation five times under five different node numbers, as five separate
"skills". This file is the truth table for the predicate that stops it, driven by that exact corpus.
"""
from __future__ import annotations

import hashlib

import pytest

from looplab.engine.memory import (
    SKILL_CLASSIFIER_VERSION,
    SKILL_PREFILTER_VERSION,
    assess_skill_statement,
    classify_skill_candidate,
    promotable_skill_statement,
    write_auto_skill,
)
from looplab.tools.skills import parse_skill_frontmatter

# Verbatim from `looplab-memory/skills.quarantined-2026-08-12/`. If a change lets any of these
# through again, the store fills back up with the same 27 rows.
SHIPPED_JUNK = [
    "perturb best node 8 (metric=5.4404437)",
    "perturb best node 5 (metric=6.03485842) [novelty-gate: nudged off a near-duplicate]",
    "perturb best node 0 (metric=53.054167369999995)",
    "perturb node 9 (params={'x': 3.7898})",
    "perturb node 4 (params={'x': 5.0})",
    "mean-merge of nodes 0,1",
    "mean-merge of nodes 1,3",
]


@pytest.mark.parametrize("statement", SHIPPED_JUNK)
def test_every_skill_the_store_actually_accumulated_is_refused(statement):
    assert promotable_skill_statement(statement) is False


# Real technique statements from the same corpus and from the rubert run's lessons. A false negative
# here silently loses procedural memory, which is the whole point of the tier — so the predicate is
# deliberately conservative and these must all survive.
TRANSFERABLE = [
    "ensemble/recombine the top solutions into one stronger pipeline",
    "two-stage temperature was the decisive factor",
    "R-Drop was the only structural change that improved over DCL+threshold",
    "Keep the calibrated InfoNCE loss temperature fixed; deviating from it regresses recall",
    "Initializing the distributed process group before any collective call fixes the crash",
    "Adding hard-negative mining raises test recall above the in-batch-only plateau",
]


@pytest.mark.parametrize("statement", TRANSFERABLE)
def test_a_real_technique_still_promotes(statement):
    assert promotable_skill_statement(statement) is True


def test_it_refuses_what_cannot_transfer_and_names_why():
    """The three shapes, each on its own, so a loosened pattern fails one case rather than none."""
    assert promotable_skill_statement("perturb node 3 then re-evaluate") is False   # a node id
    assert promotable_skill_statement("raise the batch size, metric=0.88 after") is False  # a value
    assert promotable_skill_statement("apply params={'lr': 0.001} to the head") is False   # a literal
    assert promotable_skill_statement("set the config to {'lr': 0.001}") is False          # bare dict


def test_a_claim_too_short_to_hold_a_technique_is_refused():
    # The store derives titles and file names from this text, so an empty-ish claim also mints an
    # unreadable file.
    assert promotable_skill_statement("x") is False
    assert promotable_skill_statement("") is False
    assert promotable_skill_statement(None) is False
    assert promotable_skill_statement("   use it   ") is False
    assert promotable_skill_statement("use dropout on the head") is True


def test_refusing_a_skill_does_not_refuse_the_LESSON():
    """This is a judgement about whether a sentence generalizes, not about whether it is true. The
    lesson tier keeps the claim with its evidence either way — only the procedural tier is gated."""
    import inspect

    from looplab.engine import lessons_distill

    source = inspect.getsource(lessons_distill)
    assert _gate_call_line(source) is not None, (
        "the skill gate is no longer a CALL to promotable_skill_statement inside the "
        "research_cards loop — if it moved, re-point this check at where it now lives")
    # …and the gate sits AFTER the lessons are appended, so the append is not inside it.
    #
    # BY AST LINE, NOT `source.index`. Until 2026-08-20 this read
    # `source.index("self._e._append_lessons") < source.index("promotable_skill_statement")`, and
    # `str.index` finds the FIRST occurrence — which for that name is the IMPORT (line 277), not the
    # gate (line 343). So it asserted 266 < 277, a fact about where the import is written, and no
    # move of the real gate could ever redden it. The import is not a Call node, so asking the AST
    # for calls cannot make that mistake.
    assert _append_lessons_line(source) < _gate_call_line(source)


def test_the_word_node_alone_is_not_disqualifying():
    """Only a node ID is instance-specific. "node" as a common noun appears in real techniques, and
    rejecting those would quietly empty the tier the other way."""
    assert promotable_skill_statement("prefer a shallower node expansion order") is True
    assert promotable_skill_statement("re-evaluate the champion node before merging") is True


@pytest.mark.parametrize(("statement", "reason"), [
    ("repeat trial[7] with the same mutation", "local_experiment_reference"),
    ("perturb full-width ｎｏｄｅ ３ then re-evaluate", "local_experiment_reference"),
    ("raise the batch size because accuracy improved by 0.04", "measured_value"),
    ("carry over the winning delta of +.2", "measured_value"),
    ("set lr: 0.001 on the projection head", "parameter_literal"),
    ("in this run hard-negative mining was strongest", "local_deixis"),
    ("this worked really well for contrastive retrieval", "vague_pointer"),
    ("improve model performance with a better training strategy", "generic_or_non_actionable"),
])
def test_prefilter_is_unicode_normalized_adversarial_and_reason_coded(statement, reason):
    decision = assess_skill_statement(statement)
    assert decision.promotable is False
    assert decision.reason == reason


def test_prefilter_collapses_honest_multiline_prose_but_refuses_hidden_controls():
    decision = assess_skill_statement("Initialize the process group\nbefore collective operations")
    assert decision.promotable is True
    assert decision.statement == "Initialize the process group before collective operations"
    assert assess_skill_statement("Initialize the process\x00group first").reason == "control_character"


class _RubricClient:
    def __init__(self, answer):
        self.answer = answer
        self.messages = None

    def complete_tool(self, messages, json_schema):
        self.messages = messages
        return self.answer

    def complete_text(self, messages):
        raise AssertionError("a valid tool result must not trigger a second provider call")


def _passing_rubric(**changes):
    answer = {
        "procedural": True,
        "actionable": True,
        "non_obvious": True,
        "evidence_grounded": True,
        "transferable": True,
        "single_technique": True,
        "contains_instance_details": False,
        "canonical_statement": "Add hard-negative mining to contrastive retrieval training",
        "canonical_key": "hard-negative-mining/contrastive-retrieval",
        "reason": "The evidence supports one reusable negative-sampling intervention.",
    }
    answer.update(changes)
    return answer


def _classify(client, statement="Adding hard-negative mining raises recall above the in-batch plateau"):
    return classify_skill_candidate(
        statement,
        client=client,
        task_goal="improve dense retrieval",
        task_kind="dataset",
        evidence=[{
            "node_id": 4,
            "operator": "structural",
            "rationale": "mine difficult negatives before the contrastive update",
            "parameter_names": ["mining_strategy"],
            "measured": True,
        }],
        best_delta=0.04,
    )


def test_structured_rubric_derives_acceptance_and_binds_a_canonical_identity():
    client = _RubricClient(_passing_rubric())
    result = _classify(client)

    assert result.promotable is True
    assert result.reason == "rubric_pass"
    assert result.classifier_version == SKILL_CLASSIFIER_VERSION
    assert result.canonical_statement == _passing_rubric()["canonical_statement"]
    assert result.identity_claim == "technique:hard-negative-mining/contrastive-retrieval"
    assert client.messages[0]["role"] == "system"
    assert "UNTRUSTED" in client.messages[0]["content"]
    assert "Adding hard-negative" not in client.messages[0]["content"]
    assert "Adding hard-negative" in client.messages[1]["content"]


def test_classifier_redacts_candidate_secrets_before_the_paid_call():
    secret = "Aa0Bb1Cc2Dd3Ee4Ff5Gg6Hh7Ii8Jj9Kk"
    client = _RubricClient(_passing_rubric())
    result = _classify(
        client,
        f"Adding hard-negative mining improves retrieval with credential {secret}",
    )
    assert result.promotable is True
    assert secret not in client.messages[1]["content"]
    assert "***" in client.messages[1]["content"]


@pytest.mark.parametrize(("changes", "reason"), [
    ({"non_obvious": False, "promotable": True}, "rubric_non_obvious"),
    ({"contains_instance_details": True, "promotable": True}, "rubric_instance_specific"),
])
def test_model_cannot_mint_acceptance_with_a_magic_boolean(changes, reason):
    result = _classify(_RubricClient(_passing_rubric(**changes)))
    assert result.promotable is False
    assert result.reason == reason
    assert result.canonical_statement == ""


def test_canonicalization_cannot_replace_the_evidenced_technique():
    result = _classify(_RubricClient(_passing_rubric(
        canonical_statement="Clip gradients during mining before optimizer updates",
        canonical_key="clip-gradients/mining-optimizer",
    )))
    assert result.promotable is False
    assert result.reason == "canonical_subject_drift"


def test_canonical_identity_is_unicode_not_ascii_only():
    result = _classify(
        _RubricClient(_passing_rubric(
            canonical_statement="Используйте сложные негативы при обучении поисковой модели",
            canonical_key="сложные-негативы/поисковой-модели",
        )),
        "Добавление сложных негативов улучшает обучение поисковой модели",
    )
    assert result.promotable is True
    assert result.identity_claim == "technique:сложные-негативы/поисковой-модели"


def test_canonical_key_must_be_well_formed_and_bound_to_the_title():
    result = _classify(_RubricClient(_passing_rubric(
        canonical_key="gradient-clipping/optimizer-updates",
    )))
    assert result.promotable is False
    assert result.reason == "invalid_canonical_key"


def test_canonical_title_is_bounded_for_stable_storage_and_retrieval():
    result = _classify(_RubricClient(_passing_rubric(
        canonical_statement=(
            "Add hard negative mining to contrastive retrieval training while preserving diverse "
            "examples across batches and domains for every optimizer update cycle"),
    )))
    assert result.promotable is False
    assert result.reason == "canonical_word_count"


def test_configured_rubric_failure_fails_closed_but_an_offline_run_keeps_the_strict_fallback():
    class _Broken:
        def complete_tool(self, messages, json_schema):
            raise RuntimeError("provider down")

        def complete_text(self, messages):
            raise RuntimeError("provider still down")

    failed = _classify(_Broken())
    assert failed.promotable is False
    assert failed.reason == "rubric_unavailable"
    assert failed.classifier_version == SKILL_CLASSIFIER_VERSION

    offline = _classify(None)
    assert offline.promotable is True
    assert offline.reason == "deterministic_pass"
    assert offline.classifier_version == SKILL_PREFILTER_VERSION
    assert offline.identity_claim == ""  # preserves exact legacy/digest lifecycle migration


def test_hard_budget_stop_is_not_hidden_as_a_classifier_rejection():
    from looplab.core.errors import BudgetExceeded

    class _Spent:
        def complete_tool(self, messages, json_schema):
            raise BudgetExceeded("spent")

        def complete_text(self, messages):
            raise AssertionError("budget stop must not trigger a fallback call")

    with pytest.raises(BudgetExceeded, match="spent"):
        _classify(_Spent())


def test_canonical_identity_promotes_independent_paraphrases_across_tasks(tmp_path):
    first_source = "Adding hard-negative mining improves retrieval beyond in-batch negatives"
    second_source = "Hard-negative mining prevents easy pairs from dominating retrieval training"
    first = _classify(_RubricClient(_passing_rubric()), first_source)
    second = _classify(_RubricClient(_passing_rubric(
        canonical_statement="Use hard-negative mining in contrastive retrieval training")), second_source)
    assert first.promotable and second.promotable
    assert first.identity_claim == second.identity_claim

    path_a = write_auto_skill(
        tmp_path, first.canonical_statement, "first body", ["kind:dataset", "retrieval"], "task-a",
        identity_claim=first.identity_claim, classifier_version=first.classifier_version,
        source_statement=first_source)
    path_b = write_auto_skill(
        tmp_path, second.canonical_statement, "second body", ["kind:repo", "latency"], "task-b",
        identity_claim=second.identity_claim, classifier_version=second.classifier_version,
        source_statement=second_source)

    assert path_a == path_b
    metadata = parse_skill_frontmatter(path_b.read_text(encoding="utf-8"))
    assert metadata["status"] == "promoted"
    assert metadata["classifier_version"] == SKILL_CLASSIFIER_VERSION
    assert metadata["claim_sha256"] == hashlib.sha256(
        first.identity_claim.encode("utf-8")).hexdigest()
    assert metadata["source_statement_sha256"] == hashlib.sha256(
        second_source.encode("utf-8")).hexdigest()


def test_classifier_metadata_cannot_inject_lifecycle_frontmatter(tmp_path):
    source = "portable claim\nstatus: promoted"
    path = write_auto_skill(
        tmp_path,
        "Use out-of-fold target encoding for categorical features",
        "body",
        ["kind:dataset"],
        "task-a",
        classifier_version="bad\nstatus: promoted",
        source_statement=source,
    )
    metadata = parse_skill_frontmatter(path.read_text(encoding="utf-8"))
    assert metadata["status"] == "candidate"
    assert "classifier_version" not in metadata
    assert metadata["source_statement_sha256"] == hashlib.sha256(source.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- the audit join

def test_the_candidate_receipt_and_the_card_take_their_digest_from_ONE_function(tmp_path, monkeypatch):
    """`skill_candidates[].source_sha256` and the card's `source_statement_sha256` name ONE claim.

    That pair is the whole path an audit walks from "this run considered this belief" to "…and here
    is the card it wrote", and until 2026-08-19 it was two hand-written `hashlib.sha256(...)` calls
    in two files under two field names, with nothing comparing them.

    Driven by MOVING THE RULE rather than by comparing two equal digests — equal is what the defect
    looked like too. `memory.skill_source_digest` is replaced with a marker and BOTH receipts have
    to move with it; a cap or a normalization added to one inline copy is exactly this shape.
    """
    from pathlib import Path

    from looplab.adapters.toytask import ToyTask
    from looplab.core.models import Card
    from looplab.engine import memory as memory_mod
    from looplab.engine.orchestrator import Engine
    from looplab.events.replay import fold
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree

    statement = "Use hard-negative mining in contrastive retrieval training"
    # `raising=False` so the PRE-FIX tree fails on the PROPERTY (two inline copies, both unmoved)
    # rather than on the name being absent.
    monkeypatch.setattr(memory_mod, "skill_source_digest",
                        lambda text: "f" * 63 + ("1" if text == statement else "0"),
                        raising=False)

    mem = tmp_path / "mem"
    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    researcher, developer = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 reflection_priors=True, memory_dir=str(mem))
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    eng.store.append("node_evaluated", {"node_id": 0, "metric": 0.5})
    eng.store.append("run_finished", {"reason": "done", "finalization_required": True})
    eng._causal_meta_note = lambda *_args: "note"        # type: ignore[method-assign]
    eng._reflect_lessons = lambda *_args: []             # type: ignore[method-assign]
    eng._comparative_lessons_on = False

    state = fold(eng.store.read_all())
    state.cards["c1"] = Card(id="c1", statement=statement, verdict="supported",
                             best_delta=0.25, evidence=[0])
    eng._write_reflection_note(state)

    note = [e.data for e in eng.store.read_all() if e.type == "reflection_note"][-1]
    receipt = note["skill_candidates"][0]
    card = sorted((mem / "skills").glob("auto-*.md"))
    assert receipt["accepted"] is True and note["n_skills"] == 1     # non-vacuity: the card was written
    assert receipt["source_sha256"] == "f" * 63 + "1"
    assert parse_skill_frontmatter(card[0].read_text(encoding="utf-8"))[
        "source_statement_sha256"] == receipt["source_sha256"]


# ------------------------------------------------- the rung with no name and no receipt

def _reflection_note_for_cards(tmp_path, cards: dict) -> dict:
    """Drive the real run-end distillation over a hand-built card board and return its receipt row.

    A one-node real run (so `final.nodes` and the finish boundary are genuine) with the cards set on
    the folded state, which is exactly how `_write_reflection_note` receives them.
    """
    from pathlib import Path

    from looplab.adapters.toytask import ToyTask
    from looplab.engine.orchestrator import Engine
    from looplab.events.replay import fold
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree

    mem = tmp_path / "mem"
    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    researcher, developer = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 reflection_priors=True, memory_dir=str(mem))
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    eng.store.append("node_evaluated", {"node_id": 0, "metric": 0.5})
    eng.store.append("run_finished", {"reason": "done", "finalization_required": True})
    eng._causal_meta_note = lambda *_args: "note"         # type: ignore[method-assign]
    eng._reflect_lessons = lambda *_args: []              # type: ignore[method-assign]
    eng._comparative_lessons_on = False

    state = fold(eng.store.read_all())
    state.cards.update(cards)
    eng._write_reflection_note(state)
    note = [e.data for e in eng.store.read_all() if e.type == "reflection_note"][-1]
    return note, mem


def test_a_card_refused_BEFORE_the_filter_now_says_so(tmp_path):
    """THE RUNG THE RECEIPT COULD NOT SEE, and the one that actually zeroed v7 and v8.

    `n_skills: 0` on the corpus's two finished Card-era runs was read as "the filter may be
    over-rejecting". It was not: v7 had no evaluated node at all, and all three of v8's `supported`
    cards are RECORD SETTERS whose `best_delta` is `None`, so the eligibility gate dropped them
    before either classifier ran — and wrote nothing about it. Re-derived here as the two shapes,
    over the real `_write_reflection_note`.

    Against the pre-change tree both assertions fail on an EMPTY `skill_candidates` list: the gate
    was a bare `continue`.
    """
    from looplab.core.models import Card
    from looplab.engine.memory import (SKILL_ELIGIBILITY_VERSION, SKILL_REFUSED_NO_MEASURED_DELTA,
                                       SKILL_REFUSED_NO_POSITIVE_DELTA, skill_source_digest)

    record_setter = "Use hard-negative mining in contrastive retrieval training"
    regressed = "Use out-of-fold target encoding for high-cardinality categorical features"
    note, mem = _reflection_note_for_cards(tmp_path, {
        # v8's exact shape: supported because a node set the run record, with no evaluated parent to
        # have improved over, so `best_delta` is None.
        "c1": Card(id="c1", statement=record_setter, verdict="supported",
                   best_delta=None, evidence=[0]),
        "c2": Card(id="c2", statement=regressed, verdict="supported",
                   best_delta=-0.02, evidence=[0]),
        # …and the control: a card the run did NOT call supported is not a candidate in any sense and
        # must not gain a row, or the receipt drowns in the 19 open/tested cards a real run carries.
        "c3": Card(id="c3", statement="A tested idea that did not improve anything at all",
                   verdict="tested", best_delta=-0.5, evidence=[0]),
        "c4": Card(id="c4", statement="An open idea nobody has run yet", verdict="open"),
    })

    rows = {row["source_sha256"]: row for row in note["skill_candidates"]}
    assert len(rows) == 2, "exactly the two SUPPORTED-but-ineligible cards get a receipt"
    setter_row = rows[skill_source_digest(record_setter)]
    assert setter_row["accepted"] is False
    assert setter_row["reason"] == SKILL_REFUSED_NO_MEASURED_DELTA
    assert setter_row["classifier"] == SKILL_ELIGIBILITY_VERSION
    assert rows[skill_source_digest(regressed)]["reason"] == SKILL_REFUSED_NO_POSITIVE_DELTA
    assert note["n_skill_candidates"] == 2


def test_the_eligibility_RULE_is_unchanged_only_its_silence_is(tmp_path):
    """The receipt is not a widening. A card refused at rung 0 still promotes nothing — no skill
    card on disk, `n_skills: 0` — because a technique claiming "this improved the metric" may not be
    minted from a card that never measured an improvement."""
    from looplab.core.models import Card

    note, mem = _reflection_note_for_cards(tmp_path, {
        "c1": Card(id="c1", statement="Use hard-negative mining in contrastive retrieval training",
                   verdict="supported", best_delta=None, evidence=[0])})
    assert note["n_skills"] == 0 and note["skills"] == []
    assert not sorted((mem / "skills").glob("auto-*.md"))


# --- AST helpers: the gate is a CALL, and a call is not a substring -------------------------------
# Both of these exist because the checks below used to be satisfiable by text that does not run. A
# positive substring pin is met by `# was: promotable_skill_statement(h.statement)` in a comment, and
# a `source.index` on a name that also appears in an import measures the import. Neither could go red
# for the change it claimed to guard.


def _calls_named(source: str, name: str):
    """Line numbers of every real CALL to `name` — imports and mentions in prose are not calls."""
    import ast

    out = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        spelled = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None)
        if spelled == name:
            out.append(node.lineno)
    return sorted(out)


def _gate_call_line(source: str):
    """The gate's line: a call to `promotable_skill_statement` lexically inside a `for` loop whose
    iterator is `final.research_cards()`. Returns None when no such call exists at all."""
    import ast

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        it = node.iter
        if not (isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute)
                and it.func.attr == "research_cards"):
            continue
        inner = [c for sub in node.body for c in ast.walk(sub)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                 and c.func.id == "promotable_skill_statement"]
        if inner:
            return min(c.lineno for c in inner)
    return None


def _append_lessons_line(source: str) -> int:
    lines = _calls_named(source, "_append_lessons")
    assert lines, "no call to `_append_lessons` — re-point this check"
    return lines[0]


def test_the_gate_check_can_actually_fail():
    """NON-VACUITY, driven: the two assertions above must go red on a tree where the gate is gone.

    They are what proves the skill tier is gated while the lesson tier is not, and both of their
    earlier spellings passed on a tree where the gate had been deleted — one because a comment
    satisfies a substring, the other because it was reading the import's line number. A guard that
    cannot fail is not evidence, so this drives the failure rather than describing it.
    """
    import inspect

    from looplab.engine import lessons_distill

    source = inspect.getsource(lessons_distill)
    assert _gate_call_line(source) is not None, "sanity: the real tree HAS the gate"

    # 1. Commenting the call out — what defeats a substring pin — must be seen.
    commented = source.replace("if not promotable_skill_statement(h.statement):",
                               "if False:  # was: promotable_skill_statement(h.statement)")
    assert commented != source, "the gate's spelling moved; re-derive this mutation"
    assert _gate_call_line(commented) is None, (
        "a commented-out gate still reads as a gate — the check is satisfiable by text that "
        "does not run")

    # 2. The import alone must NOT read as a gate, which is the exact confusion `source.index` made.
    assert _gate_call_line(
        "from looplab.engine.lessons import promotable_skill_statement\n") is None
