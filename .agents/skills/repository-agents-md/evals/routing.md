# Routing evaluation corpus

Expected labels: ACTIVATE, DO_NOT_ACTIVATE, or NEIGHBOR/AMBIGUOUS.

## Positive — ACTIVATE

1. Create a root AGENTS.md for this repository based on the actual repo conventions.
2. Audit our existing AGENTS.md and remove stale or overly procedural rules.
3. Figure out what instructions should always apply to Codex in this repo and update AGENTS.md.
4. Regenerate the repository-level agent guidance from CI, architecture docs, and package config.
5. Our root AGENTS.md is bloated. Compress it to durable rules and route workflows elsewhere.
6. Inspect this monorepo and create repository-wide Codex instructions.
7. Review root AGENTS.md for commands that no longer exist and fix it.
8. Establish the repository's persistent architecture, test, dependency, and safety rules for Codex.
9. Replace our generic AGENTS.md with evidence-backed repository guidance.
10. Determine which current instructions belong in AGENTS.md versus skills, then update the root file.
11. Create the initial Codex repository guidance for this new codebase.
12. Make root AGENTS.md reflect the actual source-of-truth and generated-file boundaries.
13. Audit whether root-level agent instructions match our current CI requirements.
14. Clean up AGENTS.md so it stops forcing irrelevant docs on every task.
15. Update AGENTS.md after our repository architecture changed.
16. Inspect package manager and test conventions and capture the durable ones in AGENTS.md.
17. Make repository-wide safety and external-action constraints explicit in root AGENTS.md.
18. Review nested guidance and decide what truly belongs at root.
19. Build a concise root agent map for this repository.
20. Check whether our definition of done in AGENTS.md matches the repository's actual gates.

## Negative — DO_NOT_ACTIVATE

1. Fix this failing unit test.
2. Implement the new authentication endpoint.
3. Review this pull request for bugs.
4. Write a migration plan for PostgreSQL 18.
5. Explain how AGENTS.md precedence works in Codex.
6. Create a release-validation skill.
7. Update README installation instructions.
8. Add a scoped AGENTS.md only under frontend/ for React conventions.
9. Run our test suite and summarize failures.
10. Refactor this class without changing behavior.
11. Create a coding style guide for human contributors.
12. Investigate a production incident.
13. Add CODEOWNERS entries for the API team.
14. Create a GitHub Actions workflow.
15. Find unused dependencies.
16. Explain the difference between a skill and AGENTS.md without editing the repo.
17. Create a deployment runbook.
18. Design an experiment-validation workflow.
19. Update package.json scripts.
20. Summarize our architecture document.

## Neighbor / ambiguous

1. Our backend has special rules; decide whether they need a nested AGENTS.md or a skill.
2. Improve our Codex control plane, including AGENTS.md and skills.
3. We need persistent rules for database work, but migrations also require a long procedure.
4. Audit all repository instructions, not just root AGENTS.md.
5. Create agent guidance for a monorepo with different rules per package.
6. Our release instructions are currently in AGENTS.md; reorganize them appropriately.
7. Codex keeps editing generated files; fix the repository guidance.
8. Review AGENTS.md and CONTRIBUTING.md for duplication.
9. Make our repository easier for coding agents to navigate.
10. The test commands differ by subsystem; decide where the guidance belongs.
