# Project Instructions

## Workflow Automation

When triggering a GitHub Actions workflow:
1. **Check first** whether the feature branch's changes are merged into the target branch the workflow runs on (`main`).
2. If not merged, **merge automatically** without asking — resolve conflicts (prefer feature branch for pipeline/product code, prefer target branch for CI/CD plumbing like workflow files), then update the target branch via `gh api` if direct `git push` is blocked (403).
3. **Trigger the workflow** immediately after.
4. **Wait** for it to complete (`gh run watch`), then **read `pipeline_run.log`** and verify: papers were fetched, evaluated (score= lines visible), sheet was updated, and no fatal errors. Report the summary to the user.

Do not ask for confirmation for this sequence — just do it.

## Async Monitoring

Never rely on "I'll check back" without actually arranging it. Plain status checks with no follow-up mechanism get silently dropped.

- **Any PR opened in a session**: immediately subscribe to its activity (webhook-based CI/review-comment events) so results push in automatically instead of requiring polling.
- **Any other long-running async work** (a dispatched workflow run, a background job, anything without a webhook): schedule a wake-up right after kicking it off to come back and check, rather than just saying you'll check later.

## Git Constraints

- Only `claude/*` branches can be pushed directly via `git push`.
- To update `main` or other protected branches, use:
  ```
  gh api repos/<owner>/<repo>/git/refs/heads/<branch> --method PATCH --field sha=<sha> --field force=false
  ```
  The commit must exist on the remote first — push it to a `claude/` branch, then update the ref.
