# Security Audit Reference

Load when the audit includes security, privacy, trust boundaries, dependencies, or abuse paths.

## Method

1. Identify assets, principals, trust boundaries, externally controlled inputs, privileged actions, and sensitive outputs.
2. Trace authorization and validation at the boundary where the decision is enforced, not only at UI/caller layers.
3. Inspect dangerous sinks and lifecycle edges: command/query construction, deserialization, file/path access, SSRF-capable networking, secrets, crypto/key use, concurrency, cleanup, error handling, and logging.
4. Use configured SAST/SCA and tests as complementary evidence. Verify alert applicability; do not equate tool silence with safety.
5. For web/application control verification, map to a current, versioned source such as OWASP ASVS when useful. For secure-development process claims, NIST SSDF is an appropriate process reference.

## Evidence rules

- A source-level exploit path can establish a finding without executing an exploit.
- Do not send payloads to production or third-party systems, access data beyond the granted scope, or perform destructive proof-of-concept actions.
- Prefer safe local fixtures, unit/integration tests, or static reasoning when exploit execution would create external consequence.
- Redact secret values. A hardcoded-secret finding should identify type/location and exposure path, not reproduce the credential.
- Dependency CVEs require reachability/applicability analysis when feasible; version match alone may be a risk signal rather than a confirmed exploitable defect.

## Current reference anchors

- NIST SP 800-218 SSDF v1.1 is final; NIST published SSDF v1.2 as an Initial Public Draft in December 2025. Do not silently treat the draft as a final requirement.
- OWASP ASVS 5.0.0 is the current stable ASVS release as of this skill version. When reporting ASVS mappings, include the ASVS version because requirement identifiers can change.
