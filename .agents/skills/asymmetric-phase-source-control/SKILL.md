---
name: asymmetric-phase-source-control
description: >
  Build, modify, or review Asymmetric Committee phase-level Git/change
  management using small conventional commits, one phase branch, one PR to main,
  explicit gate evidence, deliberate staging, and clean separation between edit,
  commit, push, PR, merge, and release authority. Use when planning or executing
  phase branches, commit slicing, staging, PR preparation, rebases, integration,
  progress updates, or source-control recovery. Preserve unrelated dirty state,
  inspect diffs mechanically, and never absorb or publish changes without
  explicit authorization.
  This skill owns Git and PR change management; use
  asymmetric-phase-delivery for phase planning and product implementation and
  combine them only when the requested work crosses into source-control actions.
---

# Asymmetric Committee Phase Source-Control Workflow

Use this skill for:

`known baseline -> phase branch -> small coherent commits -> phase gate -> PR -> integration evidence`

The source-control unit of work is not "whatever happened in the chat." It is:

`known baseline + intended delta + verification evidence + change-management state`

This Skill is procedural guidance only. It does not authorize Git writes, branch
creation, commits, pushes, PR creation, merges, releases, or remote publication.

## 1. Establish the live source-control baseline

Before editing or staging:

1. Read applicable repository instructions.
2. Read:
   - `docs/plans/README.md`
   - active `docs/plans/P<n>.md`
   - `docs/PROGRESS.md`
   - `CLAUDE.md`
   - exact phase gate in `Makefile`
3. Inspect:
   - repository root
   - current branch
   - `HEAD`
   - upstream/tracking branch
   - `git status --short`
   - staged diff
   - unstaged diff
   - untracked files
4. Identify unrelated pre-existing dirty state and preserve it.
5. Determine the active phase and intended branch name.

When shell execution is authorized:

`python .agents/skills/asymmetric-phase-source-control/scripts/source_control_preflight.py`

Use `--repo <path>` when required.

Read `references/unit-of-work.md` before staging or integrating changes.

## 2. Use one phase branch and one PR

The repository convention is:

- branch: `phase/P<n>`;
- one PR from that phase branch to `main`;
- phase plan exists before implementation;
- phase completes only after `make gate-P<n>` passes;
- `docs/PROGRESS.md` is updated after the gate.

Do not silently create extra integration branches unless a recovery/conflict
situation requires them and the user authorizes that workflow.

Do not merge multiple phases into one PR.

## 3. Slice work into small coherent commits

The phase plan should list ordered commit-sized steps.

Each commit should represent one coherent engineering change, such as:

- shared contract/schema;
- migration/store primitive;
- subsystem implementation;
- focused invariant tests;
- integration/replay harness;
- documentation/status closure.

Prefer conventional messages in the repository style, e.g.:

`feat(<area>): <imperative summary>`

Use `fix`, `test`, `refactor`, `docs`, `chore`, or another established type when
semantically correct.

Do not split a single invariant across commits so that intermediate commits are
knowingly nonsensical unless the phase plan explicitly allows a non-buildable
sequence.

Do not bundle unrelated cleanup into a feature commit.

## 4. Keep staging deliberate

Before every commit:

1. inspect `git status --short`;
2. inspect the unstaged diff;
3. stage only files/hunks belonging to that commit;
4. inspect `git diff --cached`;
5. confirm no unrelated dirty state is staged;
6. run the verification appropriate to that slice;
7. commit only after the staged diff matches the intended delta.

Git diff is authoritative over the agent's prose summary.

Never use broad staging such as `git add .` when unrelated changes or untracked
files are present unless the scope has been mechanically verified.

## 5. Preserve dirty-state provenance

Pre-existing changes belong to a different unit of work unless explicitly
adopted.

If the worktree is dirty:

- record which paths were dirty before the task;
- avoid overwriting or staging them;
- if an intended edit overlaps an already-dirty file, inspect the baseline and
  distinguish pre-existing hunks from new hunks;
- do not reset, clean, stash, or discard user work without explicit permission.

If clean isolation is needed for independent work, prefer a separate worktree
rather than sharing one mutable working tree.

## 6. Treat each Git action as a separate authority boundary

Keep these operations distinct:

1. edit files
2. stage files
3. create commit
4. rewrite commit/history
5. create/update branch
6. push branch
7. open/update PR
8. merge PR
9. tag/release/deploy

Authorization for one does not imply authorization for the next.

A request to "implement" does not automatically authorize push or merge.
A successful local gate does not authorize publication.

Never force-push, rebase published history, or rewrite commits merely for
cleanliness without explicit authorization.

## 7. Verify every commit's actual scope

After a commit:

- capture commit SHA;
- inspect `git show --stat --oneline <sha>`;
- inspect `git show <sha>` or equivalent diff as needed;
- confirm commit message and content match;
- confirm unrelated paths were not absorbed;
- run any post-commit verification required by the phase.

Do not rely on the commit command's success alone.

## 8. Keep branch integration intentional

Before push/PR preparation:

1. identify the target base (`main` per repository convention);
2. fetch/refresh remote state only when network authorization exists;
3. determine whether the phase branch is behind/diverged;
4. choose merge/rebase/update strategy deliberately;
5. resolve conflicts from source/spec evidence, not by blindly selecting one
   side;
6. rerun affected verification after conflict resolution.

A rebase changes commit identities. Record new SHAs rather than referring to
pre-rebase SHAs as if they still identify the branch.

## 9. Gate the phase before declaring it ready

Before calling the branch/PR ready:

1. run focused verification during implementation;
2. run the exact `make gate-P<n>` acceptance command;
3. require a real PASS;
4. update `docs/PROGRESS.md` with the phase outcome/evidence;
5. inspect the full phase diff against the intended base.

If the gate is blocked, skipped, or not implemented, the phase is not complete.

Do not substitute CI from an older commit or a narrower local test for the phase
gate.

## 10. Review the full phase diff

Before PR creation/review:

- determine the merge base against `main`;
- inspect all changed files from base to phase tip;
- compare against the phase plan;
- confirm each planned step is represented;
- identify extra/unplanned changes;
- confirm generated artifacts are legitimate;
- confirm docs/progress reflect actual evidence;
- verify no secrets/credentials/build debris entered the branch.

Review scope must name the exact base and head.

## 11. Prepare PR evidence, not just a summary

A PR should make the unit of work reconstructable.

Include:

- phase/scope;
- important invariants;
- ordered commit sequence;
- exact gate command;
- actual gate result;
- relevant service/environment caveats;
- spec changes/reconciliations;
- known limitations or follow-ups.

Do not claim CI status unless checked for the PR/head commit.

## 12. Handle recovery explicitly

For interrupted, malformed, or mixed work:

1. capture current HEAD/branch/status;
2. identify the known good baseline;
3. preserve backup refs before destructive history repair when authorized;
4. isolate the smallest recoverable delta;
5. rebuild clean commits from mechanical diff evidence;
6. rerun verification;
7. only then replace/publish branch history if explicitly authorized.

Never silently absorb unrelated local work while "fixing the branch."

## 13. Verify from narrow to broad

Evidence ladder:

1. staged diff inspection
2. slice-specific tests
3. commit diff inspection
4. cumulative branch tests
5. exact phase gate
6. full base..head diff review
7. remote/CI status for exact head, if required and checked
8. PR review/merge evidence

Each layer answers a different source-control question.

## 14. Review the final source-control state

Confirm:

- baseline/branch/HEAD are known;
- unrelated dirty state is preserved;
- commits are coherent and conventional;
- staged content matched each intended commit;
- phase branch contains only intended work;
- exact phase gate passed;
- `docs/PROGRESS.md` reflects executed evidence;
- base..head diff matches the phase plan;
- push/PR/merge state is reported accurately;
- no authority boundary was crossed implicitly.

## 15. Completion output

Report only:

- baseline branch and starting HEAD
- current branch and ending HEAD
- pre-existing dirty state preserved
- commits created, with SHA + message
- verification commands and actual outcomes
- phase gate result
- push state
- PR state
- merge state
- blockers or unverified remote/CI state

Do not stage, commit, rewrite history, push, open PRs, merge, tag, release, or
deploy unless explicitly requested and authorized.
