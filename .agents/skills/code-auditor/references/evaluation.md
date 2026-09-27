# Skill Evaluation Plan

Use this only to evaluate or revise `code-auditor` itself.

## Claimed workflow

This skill exists to produce an evidence-backed, risk-ranked audit of a code change, codebase, or engineering/compliance claim when the user requests review rather than implementation.

## Primary value hypothesis

Compared with the same agent without the skill, `code-auditor` should reduce unsupported/false-positive findings and premature PASS verdicts while preserving or improving detection of material defects, with no more than 20% median tool-call overhead on successful runs.

## Routing corpus

Default minimum:

- 20 positive prompts: PR/code audits, release-readiness reviews, maintainability reviews, security reviews, named compliance evidence-readiness audits.
- 20 negative prompts: implementation, feature design, bug fixing without audit request, documentation drafting, routine lint/format commands.
- 10 neighboring prompts: test-only requests, architecture reviews, threat modeling, formal certification requests, performance benchmarking, remediation requests after an audit.

Run routing prompts at least three times per condition. Targets: precision >= 0.90, recall >= 0.90, neighbor false activation <= 0.15.

## Representative task corpus

Use at least 10 tasks spanning:

- changed-code correctness regression;
- subtle authorization defect;
- false-positive SAST alert;
- misleading high coverage with missing assertion quality;
- high complexity but correct/idiomatic code;
- low complexity with semantic defect;
- pre-existing failing test unrelated to change;
- generated/source-of-truth mismatch;
- SOC 2 CC8.1 evidence gap;
- unavailable runtime evidence requiring `INCONCLUSIVE`.

Define success rubrics before outcomes. Compare skill vs. no-skill from the same starting state and repeat each condition at least five times where practical.

## Failure scenarios

At minimum include missing tool, missing requirement artifact, pre-existing failure, contradictory evidence, and a requested proof that would require unsafe/external action.

## Metrics

Track:

- routing precision/recall/neighbor false activation;
- material-finding precision and recall against seeded/known defects;
- unsupported-finding rate;
- task success vs. baseline;
- safe failure-recovery rate;
- critical autonomy/policy violations;
- premature completion and unnecessary continuation;
- at least two efficiency measures (for example tool calls and wall-clock time).

Do not claim validated performance without paired repeated runtime evidence.
