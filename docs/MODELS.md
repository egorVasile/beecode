# Model probe — what actually answers

BeeCode sends every g4f request to **two pinned keyless providers** (`LLM7` and
`CohereForAI_C4AI_Command`) and nowhere else: g4f's auto-routing was removed as a
fallback because the rest of its catalogue reaches its endpoints by driving a
headless browser through a bot check, or by a broker that demands a key. So the
listing below is short. It is also the whole listing — `/models --all`,
`beecode models` and this page are the same set.

Snapshot taken **2026-09-23** from one machine, g4f 8.5.1, one tiny request per
model id (`Reply with exactly: BEE-OK`) sent through `G4fProvider.chat` itself,
3 models in flight, 95 s timeout, one retry of anything that failed without
naming the model.

## Result

| model id | answers? | route | latency, first and second run | note |
| --- | --- | --- | --- | --- |
| `command-a-03-2025` | **yes** | Cohere ForAI | 44 s / 26 s | the config default; window measured at 32768 here, 65536 on 2026-09-21 |
| `command-r-plus-08-2024` | **yes** | Cohere ForAI | 21 s / 48 s | window unmeasured from this machine; `_MODEL_FAMILIES` in `beeagent/core/context.py` carries 128000 for the Command R line (Cohere's own number), so `/models` prints `~ 128k` while requests are sent at 32768 until `/window measure` says otherwise |
| `command-r-08-2024` | **yes** | Cohere ForAI | 21 s / 55 s | as above |
| `command-r7b-12-2024` | **yes** | Cohere ForAI | 13 s / 30 s | as above |
| `default` | **yes** | LLM7 | 10 s / 0.4 s | LLM7's only id; it does not say which model answers behind it |
| `command-r`, `command-r-plus` | no | Cohere ForAI | 8–23 s | `empty response` twice each — the id is advertised, the space does not fill it |
| `command-r7b-arabic-02-2025` | no | Cohere ForAI | >95 s | silent twice; a timeout is not an answer |

Every answer was the asked-for `BEE-OK` and nothing else, so none of these rows
rests on an endpoint that returned prose. Expect 0.4–56 s for one word: keyless
and slow, whatever the listing says.

Re-run it yourself:

```bat
python scripts\probe_models.py --all --json docs\model-probe.json
```

The committed `docs/model-probe.json` is still the raw 2026-09-20 sweep of all
645 auto-routed names, 643 of which "answered" — through providers BeeCode no
longer calls. Read it as the history section below, not as this table.

The two empty-completion ids and the silent one are named in
`MEASURED_SILENT` in `beeagent/providers/g4f_provider.py`, so a future
`discover_models()` cannot put them back in a picker just because g4f lists them.

## What stopped answering

The curated list BeeCode shipped before this measurement named 31 ids —
`gpt-4o`, `gemini-2.5-pro`, `claude-3.5-sonnet`, `kimi-k2`, `qwen-3-235b`,
`llama-4-scout`, `deepseek-v3`, `glm-5.2`, `grok-3`, `sonar` and the rest. Thirty
of the thirty-one failed; the survivor is the first row of the table above. The
failures came back in these forms:

| error | where it comes from | what it means |
| --- | --- | --- |
| `Model <id> not found`, in 0.0 ms | g4f, before the request leaves | the name is not on the pinned provider, so nothing was ever sent |
| `400 model_unavailable: Model '<id>' is currently unavailable.` | `api.llm7.io` | LLM7 validates the name against its own list |
| `400 unsupported_model_feature: does not support chat endpoints` | `api.llm7.io` | named by a follow-up probe (`gpt-image-2`), not by the old list |
| `401 missing_api_key` | `api.llm7.io` | the name is on the old list but needs an account (`llama-4-maverick`) |

That table also settles the question the pin raised: **LLM7 is not a pass-through
proxy.** llm7.io advertises 44 model ids at `/v1/models`, but of the names
BeeCode asked it for it served exactly one — `default`. So there is no set of
"gpt-4o under another hat" hiding behind the pin.

`gpt-4`, `glm-4.7-flash`, `gpt-3.5-turbo`, `glm-5.2` and `deepseek-chat` were in
the old list because auto-routing answered them; they are gone with it.

## History: the 645-model sweep (2026-09-20, before the pin)

Kept because it explains why the pin exists, **not** because it describes a
request BeeCode can make. That run asked all 645 models g4f advertised, through
auto-routing, at the same time:

| | count |
| --- | --- |
| models g4f advertised | 645 |
| replied with something | 643 |
| **obeyed the instruction** | **620** |
| replied, but ignored the instruction | 23 |
| timed out | 2 |

Latency of the obeying models: median 12.7 s, p90 20.7 s. Did not answer at all:
`mistralai/mistral-7b-instruct`, `llama-3.3-70b`.

Counting only models that obeyed, by the first g4f upstream advertising them:

| upstream | models | note |
| --- | --- | --- |
| BlackboxPro | 369 | more than half the catalog hung on one upstream |
| Cloudflare | 47 | the fastest group — and the one g4f reaches with a Turnstile check |
| Perplexity | 42 | web-grounded, needs a browser session |
| Qwen | 22 | browser |
| Gemini | 20 | browser |
| OpenaiAccount | 17 | your ChatGPT cookies |
| OpenAIFM | 17 | |
| Anthropic | 14 | |
| Grok | 10 | browser |
| (no upstream mapped) | 12 | curated names g4f routed itself |

Most of those groups are the reason for the pin: the biggest of them are reached
by a headless browser, someone's account cookies, or a bot check, and a listing
built on them was a promise BeeCode could only keep by solving the check.

## Adding your own key

Keyless is a shared, throttled resource, and the pinned set is five ids. A free
key of your own is usually faster, steadier and far wider: `/providers` lists the
endpoints, `/key <provider> <token>` stores it, `/provider <name>` switches,
`/models` then lists what that provider offers for your key. Only your own keys —
see the note in the README.
