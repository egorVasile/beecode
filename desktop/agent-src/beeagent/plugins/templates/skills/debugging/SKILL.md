---
name: debugging
description: Methodical root-cause debugging instead of guess-and-check
when_to_use: Investigating a bug, crash, or unexpected behavior
---

# Debugging Skill

## Phases — do them in order, no skipping

### 1. Reproduce
Get an exact, minimal, runnable reproduction. If it cannot be reproduced
deterministically, find the missing condition first (concurrency? time? state?).

### 2. Locate
Bisect the search space, don't scan it:
- Binary-search the commit history (`git bisect`) for regressions.
- Binary-search the input (halve the failing input until minimal).
- Binary-search the code path (log midpoints, or drop a debugger breakpoint).

### 3. Understand
State the bug as a violated expectation: "X should be A here, but it is B".
Find WHERE the expectation first breaks. The bug is upstream of the first
wrong value — the crash site is usually downstream.

### 4. Fix the cause, not the symptom
- The fix should make the reproduction pass AND the original report pass.
- If the "fix" is an extra `if` around the crash, you patched a symptom — keep digging.

### 5. Prove it closed
- Add a regression test from the reproduction (red before fix, green after).
- Run the full test suite.
- Ask: what OTHER code has the same bug pattern? Fix or file it too.

## Rules
- One hypothesis at a time; state it out loud before testing it.
- Never "fix" by deleting the check that caught the problem.
- Log what you ruled out — negative results are results.
