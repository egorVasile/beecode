# BeeCode changelog

Status: **beta**. Versions are tags; every entry below shipped behind one.

## next — unreleased (v10.1.0 tag withdrawn, version stays 10.0.9)

- Green `main`: provider presets, live seat stats and docs-vs-code suites pass.
- Commands ported from Claude Code / Codex with the same job: `/init`, `/memory`,
  `/context`, `/fork`, `/goal`, `/doctor` (built in; the catalog plugin upgrades
  it), `/dirs`, plus the `/usage` and `/resume` names.
- Trusted dirs: reaching outside the workdir asks decline / once / always.
- Pool keys merge dashboard env with the committed `keys.json`.
- Key pool grows to 7 crax keys; one key measured clean at 24 parallel requests.

## v10.0.x — what got it here

- `/providers import` from other agents' configs (json+jsonc, key tails only).
- Custom endpoints (`/providers add/edit`, `/key`), per-endpoint context rules.
- Echo guard: a model parroting the prompt fails fast with evidence.
- Think-loop breaker, structured `think` plans, persistent `/goal` file.
- Undo journal (`/undo`), project trust gate (`/trust`), compact digests.
- Sessions: auto-resume, `/fork`, export, crash-safe saves.
- Pool client: enroll secret, install-id auth (Ed25519, no bearer on the wire),
  dead-seat recovery, 429 retries, live seat budget in `/stats --live`.
- Markup (`/b/ /i/ /u/ /с.color/`), `table` tool, REPL + Textual TUI, skins.
- Licence-gated market client (`/plugins market`): upstream skills and MCP.
