# Conditional recorded-tree policy replay

12 November 2026. Source-only implementation in the isolated audit branch; no live deployment.

The historical §70.6 gap was real: the search policy is pure, but no read-only instrument could
apply another policy to recorded outcomes and report which decisions were supported. The new
`looplab policy-replay` uses the existing native event decoder, fold and actual named policies.
It supplies a fixed proposal tape, choosing the earliest unused exact operator/parent match;
the match never uses its metric. The policy receives only chosen lifecycle facts through their
original terminal. Parent order is preserved: reversing a merge's primary parent is a different action. The source champion is a target identity, never a hidden policy input.

## Acceptance

`tests/test_policy_replay.py` checks:

- Actual Greedy and Evolutionary on one recorded tree reach the same original merge node in
  six and three completed nodes, respectively. This is a conditional fixture result, not an
  estimate of gains on ML work. Actual MCTS requests an absent branch after three supported
  decisions: coverage 3/4, target count null. It does not choose a worse available action instead.
- A policy observer sees no unchosen/future metrics or global advice. An earlier losing child is
  consumed before a later better child for the same action. Original failures retain null metric,
  original constraint violations retain infeasibility, and both metric directions work.
- Pending outcomes, unavailable `uses` dependencies, differing protocol versions, reset,
  recreation, tombstone and late trust evidence stop explicitly. Confirmation, holdout,
  verifier, approval, trust-setting and retarget schedules are refused rather than simulated.
- An actual CPU ToyTask Engine produces eight native nodes. The diagnostic reads its journal
  and reaches the same recorded champion; patched engine/subprocess entry points prove that
  replay starts neither. This CPU integration does not validate scientific-model fidelity.
- Real Typer invocation and the console entry point in a fresh Python process agree. Journal
  bytes and run-directory files remain exact. Input digest and effective policy/code identity
  are explicit. Canonical atomic batches expand; invalid batches, version, dense sequence,
  junk, torn newline and an input changed during capture are refused without repair.
- Budget exhaustion keeps a null target count even with coverage 1. No scalar champion is
  explicitly unavailable. Unsupported policies/boolean or out-of-range parameters are refused.

The first acceptance run exposed fixture errors: wrong direction-state attribute/tombstone schema,
an unsupported Python module entry point, and a supposed batch-content checksum that does not
exist. Tests now use the real native schemas and registered console entry point; they claim only
batch structural validation and captured-byte SHA, not author authenticity. CPU Engine integration
also exposed legitimate setup events before `run_started`; exactly one start is accepted with
pre-start diagnostics, while pre-start node facts and duplicate starts are refused.

## Bounds

Only current pure default Greedy/MCTS/Evolutionary selection is replayed. The report is not the
original launch configuration. No proposal generation, repair/confirmation counterfactual,
scientific execution, heldout inference, paid value model, parallel schedule, GPU cost or grant
is manufactured. Counts are completed recorded nodes, not evaluator invocations. A reset hides
the original terminal-position count rather than undercounting old attempts. Legacy null protocol
versions are unknown. Coverage describes this stopping prefix, not a source population estimate.

Changed ordering could have produced entirely different code. A shorter supported path therefore
does not establish a causal policy improvement. ML measurement and live adoption remain unverified.
Only a complete stable local descriptor within 128 MiB is read; readback does not prove S3 flush.
