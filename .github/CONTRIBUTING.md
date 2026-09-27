# Contributing to This Project

First off, thanks for taking the time to contribute. This document defines the
workflow for bug reports, feature requests, and pull requests.

## Ground Rules

1. **Be respectful.** Follow the code of conduct (or basic civility if none exists yet).
2. **Search first.** Check open issues and discussions before filing duplicates.
3. **Stay in scope.** PRs should do one thing. Unrelated drive-by changes get rejected.
4. **Never commit secrets.** No credentials, tokens, keys, or generated files.

## Getting Started

```bash
# 1. Fork, then clone your fork
git clone https://github.com/YOUR_USERNAME/REPO.git
cd REPO

# 2. Create a branch named after your change
git checkout -b fix/short-description

# 3. Make your changes, run the test suite

# 4. Commit using conventional commits
git commit -m "fix: correct off-by-one in boundary check"

# 5. Push and open a pull request against the default branch
git push -u origin fix/short-description
```

## How to Contribute

### Reporting Bugs

Open an issue using the **Bug Report** template. A good report includes:

- Exact steps to reproduce.
- Expected vs. actual behavior with error output.
- Environment (version, branch/commit, platform).

### Suggesting Features

Open an issue using the **Feature Request** template. Lead with the problem,
not the solution — it gives maintainers room to find a better fix.

### Pull Requests

Every PR must:

- Reference the issue it fixes (`Fixes #123`).
- Pass all CI checks.
- Include tests for new behavior.
- Update documentation when behavior or interfaces change.
- Follow the review checklist in the PR template.

## Commit Message Convention

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional scope>): <short imperative summary>

<optional body: what and why, not how>

<optional footer: BREAKING CHANGE, issue refs>
```

Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`, `ci`.

## Branch Naming

- `fix/` — bug fixes
- `feat/` — new features
- `docs/` — documentation
- `chore/` — tooling, deps, CI

## Code Review Expectations

Reviews focus on correctness, security, performance, and maintainability.
Reviewers should respond within a reasonable window; authors should treat
feedback as iterative, not adversarial. Reviews are merged when:

- All checklist items in the PR template are satisfied.
- Required CODEOWNERS approvals are green.
- CI passes on the final commit.

## Style Guidelines

- Match the existing code style of the file you are editing.
- Keep functions small and single-purpose.
- Prefer explicit over clever.
- Comment the why, never the what.

## Licensing

By submitting a contribution, you agree that it will be licensed under the same
license that covers this project (see [LICENSE](../LICENSE)). If your
contribution includes third-party code, identify its source and license
explicitly in the PR description.
