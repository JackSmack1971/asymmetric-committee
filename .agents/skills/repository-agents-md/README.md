# repository-agents-md

A Codex skill for creating or updating repository-root `AGENTS.md` from actual repository evidence.

Its governing question is:

> What rules should generally apply whenever an agent works in this repository?

The skill deliberately separates always-loaded repository policy from conditional workflows. Long repeatable procedures are identified as skill candidates instead of being embedded into root context.

## Package

- `SKILL.md` — routing, workflow, decision envelope, verification, and failure handling.
- `references/evidence-model.md` — evidence ranking and conflict handling.
- `references/skill-boundary.md` — root AGENTS.md vs scoped AGENTS.md vs skill classification.
- `references/output-patterns.md` — compact examples and anti-patterns.
- `scripts/validate_agents_md.py` — heuristic lint for context bloat, transient state, generic filler, and procedure-heavy prose.
- `evals/` — routing and representative task/failure scenarios for paired evaluation.

The validator is intentionally heuristic. It cannot establish whether a repository claim is true; the skill requires repository evidence for that.
