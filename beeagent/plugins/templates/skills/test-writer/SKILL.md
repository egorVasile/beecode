---
name: test-writer
description: Design and write meaningful tests, not coverage theater
when_to_use: Writing or expanding a test suite for changed or new code
---

# Test Writer Skill

## Order of value
1. One test per behavior, not per method. Name it after the behavior: `test_empty_cart_total_is_zero`.
2. Cover: happy path → edge cases (empty, one, huge, None) → error paths.
3. A regression test for every bug fixed: reproduce first (red), then fix (green).

## Structure (AAA)
```python
def test_<behavior>():
    # Arrange — the smallest possible setup
    ...
    # Act — one action
    ...
    # Assert — the observable outcome
    ...
```

## Anti-patterns to avoid
- Testing implementation details (private methods, call order of mocks).
- Snapshot tests of huge blobs — they rot immediately.
- Fixtures doing real I/O in unit tests; keep those for integration tests.
- Random data without a fixed seed.

## Procedure
1. Read the code under test and list its behaviors.
2. Write the smallest failing-first test for each behavior.
3. Run the suite; anything not deterministic (time, network, randomness) gets injected or faked.
4. Report: N behaviors, M tests written, what remains untested and why.
