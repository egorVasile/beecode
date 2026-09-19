---
name: refactoring
description: Safe, incremental refactoring with tests as the safety net
when_to_use: Improving code structure without changing behavior
---

# Refactoring Skill

## Before you start
- Establish the safety net: run the tests, note failures. If coverage of the
  target area is weak, ADD characterization tests for current behavior first.
- Define the one goal of this refactor (name the smell: long function,
  duplicated logic, feature envy, ...). One refactor, one goal.

## Mechanics (strict order)
1. **Prepare**: make the code easier to change — extract variable for opaque
   expressions, remove dead code, fix naming. No behavior change yet.
2. **Small step**: ONE transformation — extract function / inline variable /
   move method / rename. Smallest unit that keeps the code compiling.
3. **Verify**: run tests. Green → commit or note the checkpoint.
   Red → revert the step immediately; do not "fix forward" mid-refactor.
4. Repeat 2-3 until the smell is gone, then stop.

## Rules
- Never mix a refactor with a bug fix or feature in the same change.
- If a step turns out to require a cascade of edits in many files, the design
  is fighting you — back out and reconsider the seam first.
- Public API: rename only when the blast radius is known; otherwise deprecate
  in two steps (add new, alias old, remove later).

## Report format
What was the smell → what steps were applied → tests before/after → behavior
unchanged (or explicitly what intentionally changed).
