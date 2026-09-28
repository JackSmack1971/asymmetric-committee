# Packaged evaluation corpus

`corpus.json` is the machine-readable form of `references/evaluation-protocol.md` used by the telemetry runner.

It contains:

- 50 routing cases: 20 positive, 20 negative, 10 neighboring/ambiguous;
- 10 representative task cases with synthetic project evidence and deterministic report checks;
- 5 adverse/recovery cases.

The harness stages a production-only copy of the skill that omits this directory, telemetry, and the evaluation protocol. This prevents the evaluated agent from reading expected activation labels or grader rules from its own package.

Do not load this corpus during ordinary user audits.

Routing prompts are run without an explicit skill mention. Task and recovery prompts are treatment-neutral in the corpus; the harness prepends the explicit `$asymmetric-predictive-validity-audit` invocation only to the skill arm, while the baseline arm receives the same neutral prompt with no target skill staged.
