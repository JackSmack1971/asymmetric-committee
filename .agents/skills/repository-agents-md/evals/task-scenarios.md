# Representative task and failure scenarios

Use identical repository fixtures for baseline and skill-enabled runs. Fix the success rubric before execution.

## Representative tasks

1. Small Python service with no AGENTS.md, documented architecture, pyproject test/lint commands, and CI.
2. Existing AGENTS.md containing generic advice plus one valid generated-file prohibition.
3. Monorepo with root invariants and frontend/backend-specific commands.
4. Repository where README commands are stale but CI and package scripts are current.
5. Repository with a detailed release procedure embedded in root AGENTS.md.
6. Repository with CODEOWNERS plus a security policy and explicit deployment approval boundary.
7. Repository where format/lint are fully automated but a package-manager restriction is not.
8. Repository with generated API clients and a documented generator/source-of-truth path.
9. Repository with several nested AGENTS.md files and duplicated root guidance.
10. Repository whose architecture changed and current AGENTS.md still names removed directories.

## Failure scenarios

1. README and CI disagree on the canonical test command.
2. Existing AGENTS.md contains a consequential safety rule that no other current source mentions.
3. Repository root is unclear because the task starts inside a nested checkout/worktree.
4. Python is unavailable, so the packaged validator cannot run.
5. User has unrelated dirty working-tree changes adjacent to AGENTS.md.

## Suggested success rubric

A run succeeds only if all applicable conditions hold:

- correct repository root identified;
- no unsupported factual rule added;
- durable root invariants captured;
- conditional workflow detail excluded or routed to a skill;
- subsystem-only rules not incorrectly promoted to root;
- consequential authority conflicts surfaced rather than guessed through;
- commands and paths in final AGENTS.md are verifiably real or explicitly authoritative;
- unrelated user changes preserved;
- final diff inspected;
- completion claims match evidence.
