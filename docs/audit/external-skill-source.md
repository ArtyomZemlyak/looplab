# External skill source admission

Implemented locally on 12 November 2026; this page is not a deployment receipt.
The optional pipeline imports selected text blobs from an existing local Git repository,
executes an authored CPU smoke, then joins the technique to a current supported native
lesson. It does not start training, scoring, an internal model call or a network fetch.
An external reasoning owner drafts the transferable procedure, applicability and resource
limits; source text alone is never promotion evidence.

`looplab skill-distill RUN --repo REPO --revision FULL_COMMIT --source-file algorithm.py
--label LABEL --generation GENERATION --action-id ACTION --lesson-action-id LESSON
--body-file technique.md --smoke-file assertions.py --exclusion-review REVIEW`
prints a request preview with `validated: false` and `published: false`.
Repeat `--source-file` and `--exclude` as needed. Optional `--source-url` is a public HTTPS
citation without credentials, query or fragment. `--publish --url EXISTING_SERVER`
uses the scoped harness credential or the existing anonymous server policy. It never
starts another UI, model client or service. A supplied revision must be a full immutable
Git commit; dirty files, links, submodules, directories, private/hidden paths and mutable
refs are refused. At most 16 files, 64 KiB per file and 128 KiB total are included.

The same request goes to `POST /api/runs/{run_id}/skill-candidates`, adding all four fields
`external_source`, `validation_code`, `excluded_terms` and `exclusion_review` to the existing
lesson/body/action/generation contract. The snapshot carries exact UTF-8 contents, per-file
SHA-256, a canonical snapshot digest and a declared origin. A remote server validates the
received snapshot; it does not attest that a client-declared Git origin really contained it.
The CLI collector reads pinned local Git blobs and never reads Git credential/config files.

Before execution the server checks the current generation, exact action replay, supported
lesson, its own reliable measured nodes, portability and source shape/integrity. The native
task ID and up to 32 declared target/article/artifact exclusions are checked against the
snapshot, draft and smoke with case-insensitive exact Unicode word boundaries. A review
reason is required. This is a mechanical exclusion filter plus an authored review, not a
semantic contamination detector: paraphrases, unlabeled target solutions and historical
training contamination remain unproven. Operators must select genuinely independent material.

The actual smoke uses `DevProbeTools.execute_result`, the existing confined interpreter,
with a 5-second timeout, no visible GPU, a disposable replica and the run/memory stores
protected. The native command sequencer and shared memory writer locks are released while
it runs. Success requires the framework's exit code zero, no timeout/cancellation and no
error; printing a success string cannot satisfy it. Raw program output is not a receipt.
A missing kernel confinement rung refuses execution. The existing probe's documented
security residuals remain: it is not a hostile-code container, does not cut network, and
does not attest arbitrary native-extension behavior. This pipeline neither executes an
unreviewed install/build script nor relaxes the existing fence.

After smoke, the server takes fresh locks and repeats eligibility and exact lesson/native
node/objective binding checks. Reset, retirement or changed evidence refuses publication.
Only then are snapshot, validation program, exclusion policy, native binding and execution
receipt saved to `memory_dir/external_skill_sources/<request-digest>.json`. The public ACK
names its relative reference, artifact digest and source digest. These JSON artifacts are
outside `skills/`, so an imported `SKILL.md` cannot be discovered as a manual skill.
The auto-skill retains a bounded list of source digests through later native refreshes.
Exact action retry returns the original receipt, including after service restart or lesson
retirement, without rerunning smoke or implying current eligibility. Changed content with
the same action ID refuses.

Imported techniques use the existing `provenance: auto` lifecycle. The first supported task
creates a hidden candidate. Additional imports or same-family support do not promote it.
Only recorded support from a sufficiently different task fingerprint can promote it through
the existing native writer; demotion/retirement behavior is unchanged. Actual admission
checks, HTTP/MCP, restart/replay, kernel smoke, failure and evidence-change races are covered
by `tests/test_external_skill_source.py`, with existing lifecycle and knowledge tests.
These fixtures do not measure scientific transfer or the value of manual global skills.
That outcome remains part of the existing
[unmeasured memory outcome](../83-autoresearch-harness-ai-rd-2026-11-06.md)
bound; no production adoption, contamination-free corpus or paid/provider utility is claimed.
