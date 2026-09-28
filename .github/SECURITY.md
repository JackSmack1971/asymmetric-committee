# Security Policy

## Supported Versions

Only the following versions of this project receive security updates:

| Version | Supported          |
| ------- | ------------------ |
| 1.x     | :white_check_mark: |
| < 1.0   | :x:                |

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Report vulnerabilities through [GitHub's private vulnerability reporting](https://github.com/JackSmack1971/asymmetric-committee/security/advisories/new)
for this repository.

Please include:

1. The type of issue (e.g., injection, auth bypass, RCE, data leak).
2. Full paths of affected files and a proof of concept or reproduction steps.
3. Impact assessment, including how an attacker could exploit it.
4. Any mitigations you have identified.

### What to Expect

| Stage | Target Response Time |
| ----- | -------------------- |
| Acknowledgment of report | 48 hours |
| Initial assessment | 5 business days |
| Fix or mitigation | Critical: 7 days, others: 30 days per release cadence |
| Public disclosure | After a fix ships, with reporter approval, typically 90 days |

We will keep you informed of progress toward a fix and coordinate public
disclosure timing with you. We credit reporters in release notes unless
anonymity is requested.

## Scope

The following are in scope:

- Vulnerabilities in this project's code as shipped in releases.
- Security regressions introduced by dependencies (with a working exploit path).

The following are generally out of scope:

- Social engineering, phishing, or physical attacks against users.
- Automated scanner output without a demonstrated exploit.
- Vulnerabilities in outdated versions already fixed in a supported release.
- Denial of service against our public infrastructure.

## Disclosure Policy

We follow coordinated disclosure:

1. Report received and acknowledged privately.
2. Maintainers reproduce and assess severity.
3. Fix developed on a private branch with tests.
4. Patch released; advisory published (CVE requested if warranted).
5. Reporter credited (unless anonymous by request).

## Preferred Languages

We prefer reports in English.

## Safe Harbor

Good-faith research that respects user privacy, avoids service degradation,
and follows this policy will not result in legal action against you. If in
doubt, contact us before testing.
