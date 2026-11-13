---
name: docs-writer
description: Write READMEs and API docs people actually read
when_to_use: Creating or updating README, usage docs, API references
---

# Docs Writer Skill

## README skeleton (in order)
1. **One line** — what this is, for whom.
2. **Quick start** — install + first success in under 2 minutes, copy-pasteable.
3. **Usage** — 3-7 real examples, from simplest to advanced.
4. **Configuration** — table: name | type | default | what it does.
5. **FAQ / Troubleshooting** — real errors a newcomer hits.
6. **Contributing / License** — one line each.

## Rules
- Show, don't tell: every claim about behavior comes with a command or snippet.
- No marketing adjectives ("powerful", "blazing") — facts only.
- Document the DEFAULT of every option; people read defaults more than descriptions.
- API docs: for each callable — signature, parameter types, return value, one example, raised exceptions.

## Procedure
1. Read the code entry points to know what actually exists.
2. Verify every command in Quick start actually runs before writing it down.
3. Write, then reread as a first-time user: what would confuse me on line one?
