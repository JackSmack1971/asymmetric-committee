# Source-control unit of work

This reference is grounded in repository-history Workflow 9 and the repository
phase-plan convention.

## Identity

A source-control unit of work is:

`known baseline + intended delta + verification evidence + change-management state`

Conversation identity is not Git identity.

## Baseline

Record:

- repository;
- worktree;
- branch;
- HEAD;
- upstream when relevant;
- dirty/staged/untracked state.

A delegated change without a known baseline is not safely attributable.

## Commit scope

A good commit is one coherent logical delta whose staged diff can be explained
without "and also."

The repository phase-plan guidance explicitly calls for ordered small
conventional commits such as:

`feat(<area>): …`

## Diff authority

Use:

`git diff` / `git diff --cached` / `git show` / base..head diff

as mechanical evidence of actual change scope.

Agent summaries are secondary.

## Dirty state

Unrelated existing changes are not implicitly part of the task.

Do not:

- stage them;
- reset them;
- clean them;
- stash them;
- overwrite them;

without explicit user authorization.

## Authority boundaries

Separate:

`edit -> stage -> commit -> rewrite -> push -> PR -> merge -> release`

Each requires its own runtime/policy/user authorization as applicable.

A Skill can prescribe this separation. It cannot authorize any of these effects.

## Phase integration

Repository convention:

`phase/P<n> -> one PR -> main`

Completion:

`exact make gate-P<n> PASS -> update docs/PROGRESS.md -> full diff review -> PR ready`

## Rebase/history rewrite

A rebase changes commit identity.

After any rewrite:

- discard stale SHA references;
- capture new SHAs;
- rerun affected verification;
- re-check branch diff against the intended base.

## Review scope

Every code review should identify exact base/head.

"Review the PR" without resolving its base/head is weaker than reviewing the
mechanical diff for those refs.

## Publication evidence

Local branch existence != pushed branch.
Pushed branch != open PR.
Open PR != merged PR.
Merged PR != deployed/released state.

Report each state separately.
