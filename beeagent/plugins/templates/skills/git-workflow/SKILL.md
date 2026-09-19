---
name: git-workflow
description: Clean git workflows — branches, rebases, conflict resolution
when_to_use: Branching, merging, rebasing, resolving conflicts, undoing mistakes
---

# Git Workflow Skill

## Golden rules
- Commit small, commit often; push before you leave the desk.
- Never rewrite history that others may have pulled (public branches).
- Before any destructive command (reset --hard, rebase, branch -D):
  `git status` + `git stash list` FIRST, and prefer a reversible step
  (branch/tag backup) over deletion.

## Branching
- One branch = one purpose, named after it: `fix/cart-total`, `feat/dark-mode`.
- Branch off the latest main: `git switch main && git pull && git switch -c <name>`.

## Integrating (prefer rebase for local work)
```
git fetch origin
git rebase origin/main        # replay my commits on top
# conflicts: fix file, git add <file>, git rebase --continue
```
Merge commits only for PRs into shared branches.

## Conflict resolution procedure
1. `git status` — list conflicted files.
2. For each file: read BOTH sides' intent, not just the markers.
   The right answer is often BOTH changes combined.
3. Resolve, `git add`, continue. Verify: run tests before completing the rebase.
4. If it goes sideways: `git rebase --abort` restores the pre-rebase state — use it.

## Undo table
| Situation | Command |
|---|---|
| Uncommitted changes, keep | `git stash` (pop later) |
| Last commit, keep edits | `git reset --soft HEAD~1` |
| Committed to wrong branch | `git switch -b right && git cherry-pick <sha>` |
| Need an old file version | `git show <sha>:path > path` |
