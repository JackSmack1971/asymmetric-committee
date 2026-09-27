# Evidence model for root AGENTS.md synthesis

Use this reference when repository evidence is incomplete, duplicated, or contradictory.

## Evidence preference

Prefer, roughly in this order, while respecting explicit repository authority:

1. Applicable user / repository instruction files and explicit policy documents.
2. Executable configuration and CI that actively governs the repository.
3. Current source code and generated/source-of-truth relationships.
4. Maintained architecture, contribution, security, and ownership documentation.
5. Current package manifests and tool configuration.
6. Repeated conventions visible across the repository.
7. Historical docs, comments, examples, or inferred patterns.

A lower-ranked source can still be authoritative if the repository explicitly says it is.

## Claim classes

Classify candidate guidance before adding it:

- **Verified invariant:** directly supported and broadly applicable. Include.
- **Verified contextual rule:** supported but only applies under a condition. Usually route to a skill, scoped `AGENTS.md`, or referenced document.
- **Mechanical fact:** enforced automatically by tooling. Include only if agents must know it to avoid a materially worse decision.
- **Probable convention:** inferred from patterns but not authoritative. Usually omit.
- **Transient state:** current branch, issue, migration, incident, roadmap status. Exclude.
- **Conflict:** authorities disagree. Resolve from higher authority or report; do not guess.

## Repository inspection strategy

Start narrow and expand only when needed. Useful targets include:

- root tree and workspace manifests;
- existing `AGENTS.md` / `AGENTS.override.md` files;
- `README`, `CONTRIBUTING`, architecture and security docs;
- `.github/workflows/`, CI config, pre-commit config;
- `pyproject.toml`, `package.json`, lock files, build files, workspace files;
- formatter, linter, type-checker, test-runner config;
- `CODEOWNERS` or documented ownership metadata;
- generation scripts and generated-file headers.

Do not require reading every file. Expand inspection when a candidate rule is consequential or sources disagree.

## Conflict handling

For each conflict record:

- the competing claims;
- their source paths;
- which authority wins and why, if resolvable;
- whether the losing source appears stale;
- whether the conflict should block editing.

Block rather than guess when the conflict affects architecture, safety, permissions, dependency policy, source-of-truth identity, or required validation.
