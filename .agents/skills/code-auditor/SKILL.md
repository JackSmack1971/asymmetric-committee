---
name: code-auditor
description: Audit code changes or codebases for correctness, security, maintainability, test adequacy, operability, and evidence-backed compliance claims. Use for code, release, security, complexity, or compliance-readiness reviews; not implementation-only work, routine linting, or formal certification.
---

# Code Auditor

Produce a risk-ranked, evidence-backed audit. Treat source inspection, tools, tests, and policy artifacts as evidence with different strengths; do not turn a plausible concern into a confirmed finding without support.

## Route the audit

Choose the requested mode before broad inspection:

- **Change:** diff, PR, patch, branch, or proposed change; compare with the relevant base state.
- **Repository:** subsystem or codebase; bound scope by architecture, entrypoints, data/control boundaries, and stated concerns.
- **Claim:** release, security, quality, or compliance claim; test it against implementation and independent evidence.

Load only decision-relevant references:

- [`references/audit-method.md`](references/audit-method.md) — evidence, findings, severity, verification, verdicts, durable schema.
- [`references/maintainability.md`](references/maintainability.md) — complexity, readability, coupling, refactoring risk.
- [`references/security.md`](references/security.md) — security/privacy/trust-boundary audits.
- [`references/compliance.md`](references/compliance.md) — named frameworks, controls, regulated traceability.
- [`references/research-basis.md`](references/research-basis.md) — methodology/threshold calibration.
- [`references/evaluation.md`](references/evaluation.md) — evaluating this skill itself.

## Workflow

1. **Reconcile scope and authority.** Follow repository instructions. Inspect relevant requirements, architecture, implementation, tests, Git status/diff, and configured validation tooling. Preserve unrelated work. Establish a comparison base before attributing regressions.
2. **Form risk hypotheses.** Prioritize explicit requirements, trust boundaries, data integrity, compatibility, failure recovery, resource limits, and operational guarantees. Treat style and structural metrics as triage signals unless project policy makes them requirements.
3. **Gather independent evidence.** Start with the narrowest repository-native check that can confirm or refute each material hypothesis; broaden only when risk or results justify it. Prefer existing lint/type/test/build/SAST/SCA/runtime tooling over inventing new tooling.
4. **Record findings.** State the violated behavior or credible risk, exact evidence, impact, confidence, and remediation direction. Cite source locations and command/test evidence when available. Separate introduced from pre-existing failures.
5. **Close the evidence loop.** Re-check high-severity conclusions against implementation and independent evidence. Missing, blocked, contradictory, or inaccessible evidence lowers confidence or blocks the claim; it never justifies guessing.
6. **Verdict.** `RED` for a confirmed blocking defect or failed explicit requirement; `PASS` only when the requested scope is adequately evidenced with no blocking finding; otherwise `INCONCLUSIVE`.

## Optional multi-agent audit protocol

Use sub-agents only when the user explicitly requests sub-agent/parallel audit work and the audit contains independently decomposable work whose isolation, specialization, or fresh context is likely to improve evidence quality. Do not fan out merely because agents are available.

Preferred depth-one pattern:

1. Assign at most 1–3 **read-only investigators** to non-overlapping questions such as architecture/scope mapping, a material specialist domain, or test/operability evidence. Give each a bounded completion contract and require concrete source/test/tool evidence.
2. **Synthesis barrier:** wait for the investigators, then have the primary auditor reconcile conflicts against source, requirements, Git state, and executed evidence. Worker reports are evidence inputs, not automatically accepted findings. Never use majority vote as verification.
3. The primary auditor forms candidate findings, owns severity/confidence, and preserves introduced-vs-pre-existing attribution.
4. For consequential `CRITICAL`/`HIGH` findings, a release-blocking conclusion, or a consequential `PASS`, use one **fresh independent reviewer** when practical. Give it only the proposed conclusions plus the minimum relevant evidence; use [`agents/audit-reviewer.yaml`](agents/audit-reviewer.yaml) as the role contract.
5. The primary auditor adjudicates reviewer disagreements from mechanical evidence. Deterministic tests, source, requirements, and authoritative records outrank agent agreement.

Do not give parallel investigators overlapping write ownership. Audits remain read-only unless remediation was explicitly requested. A Skill or agent definition never grants filesystem, shell, network, Git, MCP/app, credential, or external-write authority; effective runtime policy still controls those effects.

When durable review provenance is useful, emit `schema_version: "1.1"` and include `finding_reviews` as defined in [`references/audit-method.md`](references/audit-method.md). Multi-agent review is optional evidence, not a prerequisite for a valid audit.

## Boundaries

- Do not edit code, tests, configuration, or external systems unless remediation is explicitly requested.
- Do not install dependencies, enable network access, relax environment policy, or trigger destructive/external actions merely to increase audit coverage.
- Never infer formal certification, attestation, or organization-wide compliance from repository evidence. Report only evidence readiness and controls observed.
- Do not use universal coverage, complexity, function-length, or working-memory thresholds as release gates unless authoritative project policy requires them.
- Verify tool-alert applicability; absence of alerts is not proof of absence.
- Redact secrets and sensitive values from evidence.
- Stop when scope is adequately evidenced, a blocker makes further work non-informative, or further checks require ungranted authority.

## Output

Lead with findings in descending severity. For each: `ID`, severity, confidence, location, violated behavior/risk, evidence, impact, and remediation direction. Then report verification performed, relevant non-findings/limitations, and the final `PASS` / `RED` / `INCONCLUSIVE` verdict.

For a durable audit artifact, emit `audit.json` using [`references/audit-method.md`](references/audit-method.md), then run:

```bash
python scripts/validate_audit.py audit.json
```

Validator success proves only the evidence-contract structure, not code correctness.
