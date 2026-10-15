"""A defaulted field added to an EXISTING eval sub-spec must not change that spec's dump.

`eval.canary` (2026-10-07) gained `mode` on 2026-10-14; `eval.artifact_sync` and `eval.tracks`
(2026-10-07) gained `env_passthrough` / `gpus` on 2026-10-08. A task that declared any of them
before the field existed dumped without the key, and `task.model_dump()` feeds the run-start config
hash (`engine/setup_phase.py`), the setup manifest digest and the stage input key — so a resume
across the upgrade read a task change nobody made (review 2026-10-15). The `EvalSpec`-level fields
already leave the dump at their defaults (`EvalSpec._omit_absent_scorer_boundary`); these are the
same rule one level down.
"""
from __future__ import annotations

from looplab.adapters.repo_task import EvalSpec

_PRE_FIELD = {
    "command": ["python", "train.py"],
    "canary": {"env": {"MAX_USERS": "2000"}, "timeout": 120.0},
    "artifact_sync": {"command": ["mc", "cp", "-r", "{workdir}", "minio/b/"]},
    "tracks": {"drift": {"command": ["python", "drift.py", "{workdir}"]}},
}


def test_a_pre_field_declaration_dumps_without_the_new_keys():
    """MUTATION: drop any of the three sub-spec serializers -> its key reappears in the dump."""
    dump = EvalSpec.model_validate(_PRE_FIELD).model_dump(mode="json")
    assert "mode" not in dump["canary"]
    assert "env_passthrough" not in dump["artifact_sync"]
    assert "env_passthrough" not in dump["tracks"]["drift"]
    assert "gpus" not in dump["tracks"]["drift"]
    # ...and the default dump round-trips to the same spec.
    assert EvalSpec.model_validate(dump).model_dump(mode="json") == dump


def test_a_declared_value_is_still_dumped_and_round_trips():
    declared = {**_PRE_FIELD,
                "canary": {**_PRE_FIELD["canary"], "mode": "smoke"},
                "artifact_sync": {**_PRE_FIELD["artifact_sync"],
                                  "env_passthrough": ["AWS_SECRET_ACCESS_KEY"]},
                "tracks": {"drift": {**_PRE_FIELD["tracks"]["drift"],
                                     "env_passthrough": ["SCORER_KEY"], "gpus": 2}}}
    dump = EvalSpec.model_validate(declared).model_dump(mode="json")
    assert dump["canary"]["mode"] == "smoke"
    assert dump["artifact_sync"]["env_passthrough"] == ["AWS_SECRET_ACCESS_KEY"]
    assert dump["tracks"]["drift"]["env_passthrough"] == ["SCORER_KEY"]
    assert dump["tracks"]["drift"]["gpus"] == 2
    again = EvalSpec.model_validate(dump)
    assert again.canary.mode == "smoke" and again.tracks["drift"].gpus == 2
