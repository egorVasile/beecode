---
name: security-audit
description: Practical security audit of a codebase or service
when_to_use: Auditing code or config for vulnerabilities before release
---

# Security Audit Skill

## Scope order (highest value first)
1. **AuthN/AuthZ**: can an anonymous user reach protected endpoints?
   Is the check on EVERY path (including the ones added last week)?
2. **Injection**: SQL (parameterized everywhere?), shell (list args, never
   string commands), path traversal (normalize + prefix check), XSS (contextual escaping).
3. **Secrets**: hardcoded credentials, secrets in logs, secrets in client-shipped code,
   .env committed, tokens with no expiry.
4. **Dependencies**: known CVEs in direct deps; unpinned versions in prod.
5. **Transport & storage**: TLS enforced, sensitive data encrypted at rest,
   password hashing (argon2/bcrypt — never MD5/SHA1/plain).
6. **Abuse**: rate limiting on auth + expensive endpoints, upload size limits,
   SSRF on URL-fetching features (allowlist destinations).

## Procedure
1. Map the attack surface first: entry points, trust boundaries, data stores.
2. For each surface item, walk the checklist above with grep/read — cite
   `file:line` for every finding.
3. Classify: CRITICAL (exploitable now), HIGH (exploitable with effort),
   MEDIUM (defense in depth missing), LOW / hardening.
4. For each finding: the issue, exploitation scenario in one sentence,
   concrete fix (code-level, not "validate input" hand-waving).

## Rules
- No theoretical findings without a plausible exploit path.
- Report what was checked and found CLEAN too — absence of evidence is evidence.
