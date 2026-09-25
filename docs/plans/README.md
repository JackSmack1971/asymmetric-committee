# Phase plans

Each phase starts in plan mode, and the plan is written here **before any code**.

- File name: `P<n>.md` (e.g. `P0.md`, `P3.md`), one per phase from §17 of the blueprint.
- Branch: `phase/P<n>`, one PR to `main` per phase.
- Required sections:
  1. **Scope** — the §17 deliverable, with spec section references.
  2. **Invariants touched** — which CLAUDE.md invariants apply, and the tests written first for each.
  3. **Steps** — ordered, small commits (`feat(<area>): …`).
  4. **Gate** — the exact `make gate-P<n>` command body and what it proves.
  5. **Spec issues** — any ambiguity found; propose a blueprint edit instead of diverging.
- A phase is done only when `make gate-P<n>` passes; then update `docs/PROGRESS.md`.
