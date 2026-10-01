"""One lifecycle and evaluator-claim projection shared by progress and owner attention.

The input is one event prefix plus independently read checkpoint rows. The health
receipt is updated for invalid records; no side effect or connection claim occurs.
"""

CHECKPOINT_PHASES = frozenset({"stage_check", "train_monitor", "asha_live", "deadline_grace"})


def current_question_authority(question, state):
    """A pending ASHA question cannot stop compute on a retargeted objective.

    The immutable journal retains its original grant; reads/validation expose the
    current deterministic veto without rewriting a receipt or answering the question.
    """
    if question["phase_id"] == "asha_live" and getattr(state, "objective_key", None) is not None:
        return {**question, "recorded_kill_enabled": question["kill_enabled"],
                "kill_enabled": False, "stop_refusal": "objective_retargeted"}
    return question


def allowed_verdicts(question):
    """Response vocabulary derived from the validated question's actual authority."""
    phase = question["phase_id"]
    if phase == "stage_check":
        return ("proceed", "inconclusive", "fail")
    if phase == "deadline_grace":
        return ("extend", "stop")
    if phase in ("train_monitor", "asha_live"):
        return ("continue", "watch", "abort") if question.get("kill_enabled") is True else ("continue", "watch")
    return ()


def checkpoint_records(rows, health):
    """Validate the shared journal contract before any reader interprets its records."""
    questions, answers = {}, {}
    invalid_records = 0
    for row in rows:
        if (row.get("type") not in ("question", "answer")
                or not isinstance(row.get("checkpoint_id"), str)
                or not row["checkpoint_id"]):
            invalid_records += 1
            continue
        if row["type"] == "question":
            if (type(row.get("node_id")) is not int
                    or type(row.get("node_generation")) is not int
                    or type(row.get("claim_seq")) is not int
                    or type(row.get("kill_enabled")) is not bool
                    or not isinstance(row.get("phase_id"), str)
                    or row["phase_id"] not in CHECKPOINT_PHASES
                    or not all(isinstance(row.get(key), str) for key in
                               ("run_generation", "run_uid", "phase_id", "stage",
                                "expectation", "observation"))
                    or row["checkpoint_id"] in questions):
                invalid_records += 1
                continue
            questions[row["checkpoint_id"]] = row
        else:
            if (not all(isinstance(row.get(key), str) for key in
                        ("verdict", "action_id", "reason"))
                    or row["checkpoint_id"] in answers):
                invalid_records += 1
                continue
            answers[row["checkpoint_id"]] = row
    invalid_records += sum(key not in questions for key in answers)
    health["invalid_record_rows"] = invalid_records
    health["read_complete"] &= invalid_records == 0
    return questions, answers


def project_checkpoints(rows, health, events, state, generation):
    questions, answers = checkpoint_records(rows, health)
    claim_seqs = {}
    for event in events:
        if event.type == "eval_invocation_claimed":
            node_id, attempt = event.data.get("node_id"), event.data.get("generation")
            if type(node_id) is int and type(attempt) is int:
                claim_seqs[(node_id, attempt)] = event.seq
    checkpoint_rows = []
    for q in questions.values():
        if q.get("run_generation") != generation or q.get("run_uid") != state.run_uid:
            continue
        answer = answers.get(q["checkpoint_id"])
        node = state.nodes.get(q.get("node_id"))
        same_attempt = (node is not None and node.attempt == q.get("node_generation")
                        and q.get("claim_seq") ==
                        claim_seqs.get((q["node_id"], q["node_generation"]), -1))
        current = same_attempt and node.status == "pending" and answer is None
        checkpoint_rows.append({"question": current_question_authority(q, state) if current else q, "answer": answer,
                                "lifecycle": "same_node_attempt" if same_attempt else "superseded",
                                "status": "answered" if answer else
                                "pending" if same_attempt and node.status == "pending"
                                else "superseded"})

    return checkpoint_rows
