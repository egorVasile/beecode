---
name: api-design
description: Design clean HTTP/REST APIs and SDKs
when_to_use: Designing or reviewing an API surface, endpoints, or SDK client
---

# API Design Skill

## Resource design (REST)
- Nouns, not verbs: `/users/{id}/orders`, not `/getUserOrders`.
- Collections plural; nesting max 2 levels deep, then use query params.
- Consistency beats local cleverness: same pagination, filtering, error shape
  everywhere once you pick one.

## Versioning & compatibility
- Break only in a new major version; additive changes only within a version.
- Never repurpose an existing field's meaning; add a new field instead.
- Deprecate visibly: header/log notice + removal date, keep old path 2 releases.

## Errors
Machine-readable code + human-readable message, always:
```json
{"error": {"code": "order_not_found", "message": "Order 42 does not exist",
           "details": {"order_id": 42}}}
```
Use proper status codes: 400 (bad input), 401 (no auth), 403 (no rights),
404 (missing), 409 (conflict), 422 (validation), 429 (rate limit), 500 (our bug).

## SDK / client ergonomics
- The happy path in one line; everything else optional with sane defaults.
- Validate inputs client-side before the network round-trip.
- Expose raw response escape hatch for everything not covered.

## Review checklist
[ ] Each endpoint: one purpose, obvious name
[ ] Pagination, filtering, sorting consistent across collections
[ ] Error format uniform; no leaked stack traces
[ ] Breaking changes flagged and versioned
