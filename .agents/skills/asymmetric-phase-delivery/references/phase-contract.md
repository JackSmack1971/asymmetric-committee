# Asymmetric Committee phase contract

This reference captures repository-specific facts the workflow should verify
against the live repository before acting. Repository state wins over this
reference when the two differ.

## Authority and durable records

- The blueprint is the design/specification authority named by the repository's
  current project guidance.
- `docs/PROGRESS.md` is the durable phase-status, decision, deviation, and open
  issue record.
- `docs/plans/P<n>.md` is the per-phase execution plan.
- `docs/plans/README.md` requires planning before code, one numbered phase plan,
  ordered small commits, invariant mapping, an exact gate, and explicit spec
  issues.
- `Makefile` owns the executable `gate-P<n>` entry point.

## Repository invariants to map when relevant

Verify the live repository wording before relying on this summary:

1. Inter-stage messages are typed contracts in `contracts/`.
2. Decision-side database reads go through point-in-time store boundaries.
3. No information with availability later than the decision `as_of` may leak
   into features, prompts, or decisions.
4. LLM inputs must preserve anonymity requirements.
5. Evaluation/scoring must respect commitment/anchoring requirements.
6. Portfolio sizing is deterministic code, not LLM output.
7. The actually served model is persisted where required.
8. Task idempotency is run-scoped and only completed tasks short-circuit.

## Planning rule

A phase plan should answer:

- What exact spec requirement is being implemented?
- Which invariant/boundary can regress?
- What test fails if that invariant is violated?
- What is the smallest coherent implementation order?
- What exact gate proves phase acceptance?
- What cannot be decided from current authority?

## Evidence strength

Prefer, in descending order for engineering claims:

1. current executed result
2. current Git diff / committed object
3. live repository source
4. repository-authored progress/plan assertion
5. memory or narrative summary

A documentation statement such as "gate passes" is a historical/reporting claim.
It does not become a fresh verification result until the command is executed
successfully in the relevant environment.

## Source-control boundary

The workflow may recommend a phase branch and focused conventional commits
because that is the repository pattern. Creating commits, branches, pushes, PRs,
or merges is a separate source-control action and requires available capability
plus authorization.
