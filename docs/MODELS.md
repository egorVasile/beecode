# Model probe — what actually answers

Snapshot taken **2026-09-20** from one machine, one tiny request per model
(`Reply with exactly one word: ok`), 8 requests in flight, 40 s timeout.

Free endpoints come and go and rate-limit unpredictably: treat this as a
measurement of that afternoon, not as a promise. Re-run it yourself:

```bat
python scripts\probe_models.py --all --json docs\model-probe.json
```

## Result

| | count |
| --- | --- |
| models g4f advertised | 645 |
| replied with something | 643 |
| **obeyed the instruction** | **620** |
| replied, but ignored the instruction | 23 |
| timed out | 2 |

Latency of the obeying models: **median 12.7 s**, p90 20.7 s. That is the price
of keyless endpoints — plan on it, and switch when one goes quiet.

**Did not answer at all:** `mistralai/mistral-7b-instruct`, `llama-3.3-70b`.

**Answered, but not usable in an agent loop** (23): they replied with prose,
questions back, or invented tool calls instead of the one word asked for. A
model that ignores a one-word instruction will also ignore the tool-call format.

## Where the models actually come from

Counting only models that obeyed, by the first g4f upstream that advertises them:

| upstream | models | note |
| --- | --- | --- |
| BlackboxPro | 369 | more than half the catalog hangs on one upstream |
| Cloudflare | 47 | the fastest group, small context |
| Perplexity | 42 | web-grounded |
| Qwen | 22 | |
| Gemini | 20 | |
| OpenaiAccount | 17 | |
| OpenAIFM | 17 | |
| Anthropic | 14 | |
| Grok | 10 | |
| (no upstream mapped) | 12 | curated names g4f routes itself |

Worth knowing: if Blackbox changes its mind, most of the list changes with it.
`/models <upstream>` shows what each upstream serves, so you can spread your
picks across two or three of them.

## Picks that held up in real use

From the curated list BeeCode ships, verified by the same probe and by actual
agent sessions (tool calls in JSON, following instructions):

`glm-4.7-flash`, `glm-5.2` (Cloudflare), `gpt-4o-mini`, `gpt-4.1-mini`,
`deepseek-v3`, `qwen-3-32b`, `kimi-k2`, `llama-3.1-70b`, `mistral-small-3.1-24b`.

`gemini-3.8-pro` answered but returned chatbot filler ("I'm ready to help…")
when asked to do work — not recommended for the agent loop.

## Adding your own key

Keyless is a shared, throttled resource. A free key of your own is usually
faster and steadier: `/providers` lists the endpoints, `/key <provider> <token>`
stores it, `/provider <name>` switches, `/models` then lists what that provider
offers for your key. Only your own keys — see the note in the README.
