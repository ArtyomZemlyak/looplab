"""LoopLab — autonomous ML/DS research engine.

This package is the **P0 working-loop slice** (iterations I0-I6 of
``06-implementation-plan.md`` + the I10 variance gate). It is deliberately a
flat module set; the deeper subpackage layout in the plan is the target shape.

Module -> plan mapping:
    models      -> domain/models.py            (I0)
    config      -> config/                      (I0, ADR-11)
    atomicio    -> events/store.py helper       (I1, ADR-17)
    eventstore  -> events/store.py              (I1, ADR-1/17)
    replay      -> events/replay.py             (I1, ADR-12)
    readmodel   -> events/readmodel.py          (I1, ADR-17)
    roles       -> roles/                        (I5, ADR-7)
    sandbox     -> sandbox/                      (I3, ADR-13)
    policy      -> search/policy.py             (I6, ADR-18)
    gate        -> trust/gate.py                (I10, ADR-15)
    orchestrator-> engine/orchestrator.py       (I6, ADR-12/18)
    htmlview    -> ui/html.py                   (I6, ADR-1)
    toytask     -> adapters/single_file.py      (I6, ADR-2)
"""

__version__ = "0.1.0"
