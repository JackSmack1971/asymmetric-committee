# Output patterns

These are patterns, not a mandatory template. Omit sections that the repository does not support.

## Compact root pattern

```markdown
# Repository guidance

## Repository map
- `src/` contains production code; `tests/` mirrors package boundaries.
- `docs/architecture.md` is authoritative for service boundaries.

## Engineering rules
- Do not manually edit files under `generated/`; update their source definitions and regenerate.
- Use the repository's pinned package manager; do not introduce a second lockfile.

## Validation
- Run the narrowest relevant checks first, then broaden when shared contracts are touched.
- Before completion, inspect the diff and run the repository-required gate documented in `CONTRIBUTING.md`.

## Safety and external actions
- Do not publish, deploy, rotate credentials, or rewrite remote history without explicit authorization.

## Workflow routing
- Use the schema-migration skill for database schema changes.
```

## Bad patterns

Avoid generic filler:

```markdown
Write clean code.
Follow best practices.
Test thoroughly.
```

Avoid unconditional context loading:

```markdown
Before every task, read every document under docs/.
```

Avoid embedding an entire conditional workflow:

```markdown
For releases: first run A, then if B happens run C, then collect D...
```

Replace that with a short durable constraint plus a skill pointer when appropriate.
