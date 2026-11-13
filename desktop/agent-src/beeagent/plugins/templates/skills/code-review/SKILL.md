---
name: code-review
description: Systematic code review — correctness, security, performance, style
when_to_use: Reviewing a diff, PR, or newly written code before committing
---

# Code Review Skill

Review code in this order; stop early only if a CRITICAL issue is found.

## 1. Correctness
- Trace the happy path and at least one failure path by hand.
- Check boundary conditions: empty input, single item, very large input, None.
- Verify error handling: are exceptions caught at the right level? Are errors swallowed silently?

## 2. Security (OWASP essentials)
- Injection: any string concatenated into SQL / shell / HTML?
- Path traversal: is user input joined into filesystem paths unchecked?
- Secrets: hardcoded keys, tokens, passwords in code or logs?
- Unsafe deserialization or eval of external data?

## 3. Resource safety
- Unclosed files / connections (use context managers).
- Unbounded memory growth (lists that only append, caches without limits).
- Missing timeouts on network / subprocess calls.

## 4. Clarity
- Names that say what the thing does; no single-letter names outside comprehensions.
- Functions that fit on one screen; deep nesting flattened with early returns.
- No dead code, commented-out blocks, or leftover debug prints.

## Output format
Report findings as a list ordered by severity (CRITICAL / MAJOR / MINOR / NIT),
each with `file:line`, what is wrong, and a one-line suggested fix.
End with a verdict: approve / approve-with-nits / request-changes.
