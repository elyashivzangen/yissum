# Project Instructions

## Workflow Automation

When triggering a GitHub Actions workflow:
1. **Check first** whether the feature branch's changes are merged into the target branch the workflow runs on (`main`).
2. If not merged, **merge automatically** without asking — resolve conflicts (prefer feature branch for pipeline/product code, prefer target branch for CI/CD plumbing like workflow files), then update the target branch via `gh api` if direct `git push` is blocked (403).
3. **Trigger the workflow** immediately after.

Do not ask for confirmation for this sequence — just do it.

## Git Constraints

- Only `claude/*` branches can be pushed directly via `git push`.
- To update `main` or other protected branches, use:
  ```
  gh api repos/<owner>/<repo>/git/refs/heads/<branch> --method PATCH --field sha=<sha> --field force=false
  ```
  The commit must exist on the remote first — push it to a `claude/` branch, then update the ref.
