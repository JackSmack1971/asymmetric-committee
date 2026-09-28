# Phase source-control checklist

## Baseline
- [ ] Repository/worktree identified.
- [ ] Current branch recorded.
- [ ] Starting HEAD recorded.
- [ ] Upstream recorded if relevant.
- [ ] Staged/unstaged/untracked state inspected.
- [ ] Pre-existing dirty paths recorded.

## Phase
- [ ] Active `P<n>` plan exists.
- [ ] Branch is `phase/P<n>` or deviation is explicit.
- [ ] Target base is `main`.
- [ ] Planned commit sequence identified.
- [ ] Exact phase gate identified.

## Per commit
- [ ] Intended delta is coherent.
- [ ] Only relevant files/hunks staged.
- [ ] `git diff --cached` inspected.
- [ ] Unrelated dirty state excluded.
- [ ] Slice verification executed.
- [ ] Conventional message matches diff.
- [ ] Resulting commit SHA captured.
- [ ] `git show` inspected.

## Integration
- [ ] Branch/base divergence understood.
- [ ] Conflict strategy deliberate.
- [ ] Post-conflict verification run.
- [ ] Rewritten SHAs refreshed after rebase.
- [ ] Full base..head diff reviewed.

## Phase closure
- [ ] Exact `make gate-P<n>` PASS.
- [ ] `docs/PROGRESS.md` updated.
- [ ] Plan matches implemented scope.
- [ ] No unplanned artifacts/secrets.
- [ ] CI status checked only if required.

## Authority
- [ ] Edit authorization distinct.
- [ ] Commit authorization distinct.
- [ ] Push authorization distinct.
- [ ] PR authorization distinct.
- [ ] Merge authorization distinct.
- [ ] Release/deploy authorization distinct.

## Completion report
- [ ] Start/end refs reported.
- [ ] Commits reported.
- [ ] Gate evidence reported.
- [ ] Push/PR/merge states reported separately.
- [ ] Blockers/unverified remote state reported.
