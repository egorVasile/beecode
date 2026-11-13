---
name: commit-messages
description: Write clean, conventional git commit messages
when_to_use: Creating commits; user asks to commit or write a commit message
---

# Commit Messages Skill

## Format (Conventional Commits)
```
<type>(<scope>): <subject in lowercase, no period>

<optional body: WHY the change was made, 72-char wrap>

<optional footer: BREAKING CHANGE:, refs #123>
```

## Types
feat | fix | refactor | perf | docs | test | chore | build | ci | style

## Rules
- Subject: imperative mood ("add", not "added"/"adds"), <= 50 chars, no period.
- One logical change per commit. If the diff does two unrelated things, suggest splitting.
- Body explains motivation, not the diff — the diff is readable.
- Breaking changes must be called out in the footer.

## Procedure
1. Run `git diff --staged` (or `git diff` and propose what to stage).
2. Summarize the ONE thing this change does.
3. Draft the message, show it to the user, then commit on approval.
