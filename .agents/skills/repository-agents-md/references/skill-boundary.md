# AGENTS.md vs Skill boundary

Root `AGENTS.md` should contain persistent guidance that normally applies whenever an agent works in the repository. A skill should own a recognizable, repeatable workflow that should load only when relevant.

## Skill-extraction test

Recommend a separate skill when most of these are true:

- the workflow repeats;
- sequence or branching matters;
- the same standards should be applied every time;
- templates, examples, or specialized references are useful;
- deterministic scripts can reduce recurring error;
- the job does not require a separate independent context or reviewer;
- parallel execution provides little benefit.

Strong signals include release validation, schema migration, experiment validation, incident investigation, dependency upgrades, coverage analysis, docs synchronization, or any multi-step procedure whose detailed instructions would otherwise bloat root context.

## Keep in root AGENTS.md

Keep the durable rule or routing constraint, not the procedure. Examples:

- `Database schema changes must use the schema-migration workflow; do not edit generated schema output directly.`
- `Release publication requires explicit authorization.`
- `Changes to shared protocol contracts require compatibility validation.`

The corresponding skill can hold exact sequencing, branch logic, templates, commands, references, and failure recovery.

## Prefer scoped AGENTS.md instead of a skill when

The guidance is still persistent, but only for a directory subtree. Examples:

- frontend-only style conventions;
- service-specific architecture invariants;
- generated-code restrictions for one package;
- test commands unique to one workspace.

## Neither root AGENTS.md nor a skill

Do not preserve:

- one-off task plans;
- temporary rollout state;
- current issue assignments;
- generic advice a capable agent already knows;
- duplicate prose that adds no decision value beyond enforced tooling.
