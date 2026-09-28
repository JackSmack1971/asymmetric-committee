# Compliance and Traceability Evidence Readiness

Load only when the user asks about a named framework, control, regulated workflow, or release evidence chain.

## Boundary: evidence readiness, not certification

A code/repository audit can establish that particular technical or change-control evidence is present, absent, or inconsistent. It cannot by itself establish an organization's formal SOC 2 attestation, ISO certification, GxP validation status, or legal compliance.

For every framework statement distinguish:

- **Observed:** directly evidenced in code/config/tests/logs/artifacts available to the audit.
- **Partial:** some required evidence is present but the control claim needs additional organizational/operational evidence.
- **Unsupported:** available evidence contradicts or fails a required part of the claim.
- **Not assessed:** outside scope or authoritative requirements unavailable.

Do not invent exact control text from memory. For proprietary standards, use the organization's licensed/current standard or a current authoritative public source when available.

## SOC 2 change-management evidence

AICPA Trust Services Criteria `CC8.1` addresses authorization, design/development or acquisition, configuration, documentation, testing, approval, and implementation of changes. A repository can often evidence only part of that lifecycle.

For sampled changes, look for an evidence chain appropriate to the organization's actual process, such as:

`request/authorization -> implementation -> review/approval -> test/security evidence -> merge/release authorization -> deployment/operational verification`

Do not require Jira, CABs, author/approver separation, a particular branch model, or a specific CI product unless the organization's policy or applicable control implementation requires it.

## ISO/IEC 27001:2022

The 2022 edition reorganized Annex A. For software/change audits, commonly relevant controls include:

- `A.8.25` secure development life cycle;
- `A.8.28` secure coding;
- `A.8.29` security testing in development and acceptance;
- `A.8.31` separation of development, test, and production environments;
- `A.8.32` change management;
- `A.8.33` test information;
- `A.8.34` protection of information systems during audit testing.

Do not report obsolete ISO/IEC 27001:2013 identifiers as the current control set. If the organization is intentionally audited against an older edition, state that version explicitly.

## GxP / 21 CFR Part 11 / ISO 13485

These claims are highly context dependent. Before mapping code to a requirement:

1. establish the product/process scope and regulated intended use;
2. identify the authoritative version and the organization's validation procedure;
3. trace requirements to implementation and objective verification evidence;
4. preserve provenance, approvals, configuration/version identity, and deviations where required by that procedure.

If those sources are unavailable, report `INCONCLUSIVE` for the regulated claim rather than manufacturing a generic six-artifact gate.

## Traceability matrix

When traceability is requested, map only what can be proven:

| Requirement/control | Implementation evidence | Verification evidence | Operational/approval evidence | Status |
|---|---|---|---|---|
| versioned ID | file/symbol/config | test/check/result | ticket/review/deploy/log if available | Observed/Partial/Unsupported/Not assessed |

Missing evidence is itself useful audit output; do not fill gaps with assumptions.
