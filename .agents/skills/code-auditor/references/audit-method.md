# Audit Method and Evidence Contract

Use this reference for every substantive audit.

## Audit domains

Select only domains that are material to the requested scope:

1. **Functional correctness & reliability** — requirements, edge conditions, error propagation, idempotency, state transitions, concurrency, and recovery.
2. **Security & privacy** — trust boundaries, authentication/authorization, input handling, secrets, sensitive data, dependency exposure, and abuse paths.
3. **Maintainability & comprehensibility** — coupling, cohesion, duplication, naming, control/data-flow complexity, change amplification, and architectural fit.
4. **Performance & resource behavior** — complexity, allocation, I/O, concurrency, backpressure, timeouts, and load-sensitive failure modes.
5. **Test adequacy & verification** — whether tests exercise the changed/claimed behavior and can fail for the relevant defect classes.
6. **Operability & observability** — diagnosability, telemetry, health behavior, safe configuration, rollback/recovery evidence, and failure visibility.
7. **Architecture & contracts** — API/schema compatibility, dependency direction, generated/source-of-truth boundaries, and repository invariants.
8. **Compliance & traceability** — only when a named control, release gate, or regulated claim is in scope.

ISO/IEC 25010:2023 defines a nine-characteristic product quality model; do not represent the eight domains above as the ISO characteristic list. They are an audit-oriented synthesis.

## Evidence ladder

Prefer corroboration rather than a single signal:

- **E0 — assertion only:** comment, ticket, README, policy prose, or user claim.
- **E1 — source evidence:** implementation/configuration directly supports or contradicts the claim.
- **E2 — automated static evidence:** repository-native type/lint/SAST/SCA/build results with applicability checked.
- **E3 — dynamic evidence:** focused tests, runtime probes, reproduction, or integration behavior.
- **E4 — independent operational evidence:** CI, deployment, immutable logs, external control records, or equivalent evidence from a separate system.

A finding can be valid at E1 when the defect is logically decisive. Do not require runtime evidence where execution is impossible or adds no information. Conversely, do not call a dynamic/operational property proven from E0/E1 alone.

## Finding validity

Record a finding only when all are present:

- **Claim:** a specific behavior, requirement, invariant, or risk is violated.
- **Evidence:** exact source location and/or reproducible observation.
- **Impact:** plausible consequence in the audited context.
- **Confidence:** `HIGH`, `MEDIUM`, or `LOW`, based on evidence quality and unresolved assumptions.
- **Remediation direction:** the smallest behavior-preserving direction, not an unsolicited rewrite.

Use `LOW` confidence sparingly; if the issue is merely speculative, place it under limitations or follow-up hypotheses rather than findings.

## Severity

Severity is consequence-based, not aesthetics-based:

- **CRITICAL:** credible path to catastrophic confidentiality/integrity/availability harm, irreversible loss, broad compromise, or a critical safety/regulatory failure.
- **HIGH:** breaks an explicit requirement or creates material security, data-integrity, availability, or compatibility risk in realistic use.
- **MEDIUM:** meaningful defect or maintainability/operability risk with bounded blast radius or less likely activation.
- **LOW:** real issue with limited consequence; do not use for preferences that lack an observable cost.

Repository policy may override this taxonomy. State the override.

## Verification strategy

Start with the narrowest check that can falsify the hypothesis. Broaden when:

- the affected boundary spans multiple modules;
- the focused result exposes a wider regression risk;
- a release/compliance claim requires broader evidence;
- the repository requires a wider gate.

Do not run every available check reflexively. Record commands actually executed and their exit status. If a command is unavailable, do not pretend an equivalent ran.

## Verdict

- `RED`: at least one confirmed blocking finding, explicit acceptance criterion fails, or the requested release/control claim is contradicted.
- `PASS`: no blocking findings and the requested scope has enough evidence to support the claim being evaluated.
- `INCONCLUSIVE`: evidence needed for the requested claim is missing, inaccessible, contradictory, or blocked and there is no already-confirmed reason for `RED`.

A clean source review with unexecuted relevant tests is not automatically `PASS`.

## Durable `audit.json`

Use schema `1.0` for ordinary audits. Use schema `1.1` only when recording optional independent finding-review provenance. `1.1` is backward-compatible except for the additional optional `finding_reviews` member.

```json
{
  "schema_version": "1.0|1.1",
  "scope": {
    "mode": "change|repository|claim",
    "targets": ["path/or/claim"],
    "baseline": "optional base revision or observed pre-change state"
  },
  "findings": [
    {
      "id": "AUD-001",
      "severity": "CRITICAL|HIGH|MEDIUM|LOW",
      "confidence": "HIGH|MEDIUM|LOW",
      "category": "correctness|security|maintainability|performance|testing|operability|architecture|compliance",
      "location": "path:line or artifact reference",
      "claim": "specific violated behavior or risk",
      "evidence": ["source/test/tool/record reference"],
      "impact": "contextual consequence",
      "remediation": "smallest useful remediation direction"
    }
  ],
  "verification": [
    {
      "command": "exact command or check name",
      "status": "PASSED|FAILED|NOT_RUN",
      "exit_code": 0,
      "notes": "optional context"
    }
  ],
  "framework_claims": [
    {
      "framework": "SOC 2 / ISO 27001 / ...",
      "control": "versioned control identifier",
      "status": "SUPPORTED|PARTIAL|UNSUPPORTED|NOT_ASSESSED",
      "evidence": ["artifact references"]
    }
  ],
  "finding_reviews": [
    {
      "finding_id": "AUD-001",
      "reviewer_role": "audit-reviewer",
      "status": "CONFIRMED|DOWNGRADE|REJECT|INSUFFICIENT_EVIDENCE",
      "rationale": "evidence-backed independent adjudication",
      "evidence": ["source/test/tool/record reference"],
      "unresolved_assumptions": []
    }
  ],
  "blockers": [],
  "verdict": "PASS|RED|INCONCLUSIVE"
}
```

`finding_reviews` is optional and valid only with schema `1.1`. It records reviewer provenance; it does not override the primary auditor or prove a finding by itself. A review may challenge a finding without mutating the finding record; the primary auditor must adjudicate the conflict and make the final durable audit internally consistent.

Use `null` for an exit code only when the check did not execute. Do not use placeholders such as `TBD`, `TODO`, `N/A`, or `unknown` where a required field should contain evidence.
