<p align="center">
  <img src="docs/screenshots/01-banner.png" alt="BeeCode banner" width="720">
</p>

<p align="center">
  <a href="https://t.me/beecodee"><img src="https://img.shields.io/badge/Telegram-BeeCode-2CA5E0?logo=telegram&logoColor=white" alt="Telegram channel"></a>
  <a href="https://github.com/egorVasile/beecode/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0-green" alt="GPL-3.0 license"></a>
</p>

# BeeCode

**A free AI coding agent for your terminal.** No API key, no account, no subscription —
it talks to public model endpoints through [g4f](https://github.com/xtekky/gpt4free)
and edits your real files, runs your real commands.

Type a task in plain language; BeeCode reads the project, writes code, runs tests,
shows every step, and keeps working until the task is done.

```
🐝 > fix the failing tests in ./api and show me what you changed
   💬 storing honey for winter...
   💭 thinking...
   ⏳ glob path='api', pattern='**/*.py'
     ● found files **/*.py
   ⏳ read path='api/test_client.py'
     ● read api/test_client.py
   ⏳ bash command='pytest api -q'
     ● executed pytest api -q
     3 passed in 1.42s
   I changed api/client.py — it retries once on a read timeout; the diff is above.
```

<p align="center">
  <img src="docs/screenshots/02-session.png" alt="A BeeCode session" width="720">
</p>

---

## Install

BeeCode is a Python program, and the launcher below is the npm wrapper around
it — one command either way, and it needs Python 3.10+ on the machine.

**With npm / npx** (nothing to install first; it builds its own private
environment under `~/.beecode`, so your system Python stays untouched):

```bat
npx github:egorVasile/beecode
```

Or put `beecode` on your PATH once:

```bat
npm install -g github:egorVasile/beecode
cd C:\path\to\your\project
beecode
```

The first run installs BeeCode and a fresh `g4f`; every later run only probes
that environment and starts — how long that takes is a property of your disk,
and no number for it has been measured here. `beecode --update` refreshes both,
and it means the same thing from any install path: a checkout is pulled
(`git pull --ff-only`, `update_self` in `beeagent/cli.py`), an installed copy is reinstalled
from the repository.

```bat
beecode --update
```

**With pip**, into whatever Python environment you already use:

```bat
pip install git+https://github.com/egorVasile/beecode.git
```

Then start it from the project you want to work on:

```bat
cd C:\path\to\your\project
beecode
```

Every install path above starts **clean**: sessions and settings live in the
folder you run it from (`.beeagent/` and `beeagent.json`), so a new folder is a
new agent with no history. Nothing is resumed unless you ask for it with
`beecode --continue`.

### Add this to your project's .gitignore first

Running BeeCode once inside a project leaves two things there, and a fresh clone
of your own repository shows them as untracked (`?? beeagent.json`, `??
.beeagent/`) — this repository's `.gitignore` protects BeeCode's sources, not your
project. `beeagent.json` holds `api_keys`, `custom_providers[].key`, your
`vpn_command` and the pool seat token `/pool enroll` wrote (`pool_token` in `beeagent/config/schema.py`); `.beeagent/sessions/*.json` holds every message
and every tool output, including the contents of any file BeeCode read. Put this
in the project before the first run:

```gitignore
beeagent.json
.beeagent/
*.beecode-tmp
```

BeeCode does not write that for you yet, and it does not keep the seat token out
of the project config — both belong to code this document only describes. What it
does today: `beeagent.json` is made owner-readable-only on POSIX when it holds an
API key (`_restrict` in `beeagent/config/loader.py`), and a Windows box gets no such
permission at all.

<details>
<summary>Other install options</summary>

```bat
:: clone and install in editable mode (for hacking on the agent itself)
git clone https://github.com/egorVasile/beecode.git
cd beecode
pip install -e .

:: or run without installing
python -m beeagent.cli
```

</details>

### On a phone — Termux

Android ships no C compiler and no Rust. What that rules out is narrower than it
looks: `g4f` itself is pure Python, and so are the tools that matter here — the
file tools, the shell tool, grep, git and the loop. That is what
`tests/test_termux.py` runs: the whole agent with the optional modules missing,
writing files and executing commands.

What actually breaks a plain `pip install g4f` on Android is two of its five
declared dependencies: `pycryptodome` (no Android wheel, no pure-Python
fallback) and `brotli` (no Android wheel; Termux packages it separately). Neither
is on the path BeeCode uses — measured on g4f 8.5.1 by refusing every compiled
third-party module at its C leaf, where all 85 providers still enumerated,
`g4f.client` still imported, and three keyless providers each answered a real
prompt correctly. `tiktoken` is the same story: optional, with a fallback.

```sh
pkg install python python-pip git
pip install git+https://github.com/egorVasile/beecode.git
termux-setup-storage          # only to reach the SD card; ~/storage
cd ~/my-project && beecode
```

That gives a working agent backed by the pool (the address ships with BeeCode, so
`/pool enroll` is the only command) or by your own key (`/key crax crk_live_…`),
with token counts over-estimated — the safe direction, because it trims the
history early instead of sending a request the model drops.

To add the keyless provider on top, install `g4f` without the two declarations
that cannot build:

```sh
export AIOHTTP_NO_EXTENSIONS=1      # only read when building from the sdist...
pip install --no-deps g4f           # ...so g4f's own pycryptodome/brotli are skipped
pip install requests nest-asyncio2
pip install --no-binary=aiohttp aiohttp   # ...and force the sdist here
```

`--no-deps` is what keeps `pycryptodome` and `brotli` out; neither is on the path
BeeCode uses. Everything else arrives as a pure-Python `py3-none-any` wheel —
`multidict`, `yarl`, `frozenlist` and `propcache` all publish one — so no
compiler is involved. `aiohttp` is the exception: it ships prebuilt Android
wheels for Python 3.13/3.14 instead, and those carry its C parser, which Termux's
Python 3.14 has a reported crash in. `--no-binary=aiohttp` with the variable set
builds it from source without compiling anything, which is why both flags are
needed together.

`pkg install python-brotli` adds the brotli codec back and
`pkg install tur-repo python-tiktoken` the exact token count. No route here needs
`clang`.

None of this has been run on a real phone: the measurement above was made on
desktop Python by blocking compiled modules at import, which establishes what
`g4f` needs, not how Termux's pip behaves.

`/doctor` prints the `pkg install` line for whatever is missing. It is a catalog
plugin, not a built-in command — `/plugin install doctor` first; the REPL and the
TUI both tell you to run it without saying so, which is a UI bug this page flags
rather than repeats as a promise.

## Run

| Command | What it does |
| --- | --- |
| `beecode` | Interactive REPL (the default interface) |
| `beecode --model command-a-03-2025` | Start with a specific model |
| `beecode --provider g4f` | Start with a specific provider |
| `beecode --mode economy` | Cache answers and reuse them across identical requests |
| `beecode --lang ru` | Russian interface (default is English) |
| `beecode --continue` | Resume the last saved session |
| `beecode --tui` | Full-screen Textual interface instead of the REPL |
| `beecode -p "explain this repo"` | One-shot: ask, print the answer, exit |
| `beecode models` | List every model the pinned keyless providers advertise, read off the installed `g4f` |
| `beecode providers` | List the three provider kinds the CLI knows: `g4f`, `openai_compat`, `ollama` |
| `beecode plugins list` | Browse the installable skill / plugin / MCP catalog |
| `beecode mcp list` | Show configured MCP servers |

First question to try: `read README.md and tell me what this project does`.

## What it looks like

<p align="center">
  <img src="docs/screenshots/03-tools.png" alt="Tool registry" width="420">
  <img src="docs/screenshots/04-commands.png" alt="Slash commands" width="520">
</p>

## How it works

BeeCode runs a plain, inspectable loop — no hidden orchestration:

1. **Build context.** System prompt + tool catalog (generated from the live registry,
   so the model can never be told about a tool that does not exist) + conversation
   history trimmed to the token budget.
2. **Stream the answer.** Tokens are printed as they arrive. Reasoning blocks, when a
   model exposes them, stream under a `💭 thinking...` header.
3. **Parse tool calls.** The model replies with a JSON block:
   ````json
   {"tool": "read", "args": {"path": "src/app.py"}}
   ````
   Prose around it stays on screen; the payload itself never flashes as text.
4. **Execute.** Tools run in a worker thread, so the prompt stays alive — you can keep
   typing while the agent works, and those messages are queued and delivered with the
   next model call.
5. **Feed back.** Each result returns to the model as a `[tool result]` message and the
   loop repeats until the model answers in plain text.

### Context window

A model with a small window used to lose the thread for one turn and then "remember" it
after you repeated yourself. Two causes, both fixed:

* **The budget follows the model, not a constant.** A request is built to fit the
  model's own context window minus room for its reply (`window // 8`, floor 512 — `REPLY_RESERVE_*` in
  `beeagent/core/context.py`): 7168 tokens for a `llama-3.1-8b` (8k window),
  28672 for a `gpt-4o` (its name claims 128k, so the request is capped at 32k) —
  instead of a flat 12 000 tokens that a small endpoint then trimmed on its own
  side, silently cutting the oldest history.
* **The 32k is a default, not a wall.** With `max_context_tokens` at `0` nothing is
  sent above `MAX_WINDOW` = 32768 (`MAX_WINDOW`, `beeagent/core/context.py`). Raising that key lifts the
  ceiling — as far as the model's name claims for itself, and never past
  `MEASURED_MAX_WINDOW` = 262144 (`window_for`, `beeagent/core/context.py`). `ContextManager.window` is
  `min(window_for(model), cap)`, so a configured value can only ever move the
  budget *towards* the model's window, never past it. A window `/window measure`
  proved on your route skips the 32k clamp entirely.
* **Tokens are counted, not guessed.** `gpt-4`-style names aside, every g4f model id
  fell back to "four characters per token", which read Russian text as half its real
  size — so an oversized request looked like it fit. With no `tiktoken` (Termux) the
  count is a deliberate **over**-estimate, chunked by script (`utils/tokens.py`).

When the conversation still does not fit, nothing is thrown away in silence:

* oversized messages are **clipped** (head + tail kept, the cut marked in tokens);
* the request you are working on is **shrunk rather than dropped** — it always travels;
* everything else is **compressed into a digest that rides in the system prompt**, so
  the model sees the shape of the chat instead of a blank page:

```
# CONVERSATION SO FAR — 42 earlier message(s) were compressed to fit the context window.
user: разбери проект и найди все проблемы в beeagent/core
bee: пункт 0: подробное обоснование найденной проблемы…
tool: → read
user: а теперь почини их и прогони pytest
```

You see it happen, and `/token` shows the window it was fitted to:

```
✂ history did not fit the context: compressed 42 messages into a summary in the system prompt, big outputs are clipped — the task stays in view
```

### Mistakes are recovered, not fatal

* `list_files`, `read_directory`, `read_files`, `list_dir`, `webfetch`, `fetch`,
  `read_url`, `open_url`, `mv`, `rename`, `move_file`, `rm`, `delete`,
  `remove_file` — invented names for `list_directory`, `web_fetch`, `move` and
  `remove`, accepted as aliases (`aliases` on those tools). Only
  `ToolRegistry.get()` takes them, so `/tools` keeps advertising one canonical name:
  `🔧 tool name corrected: read_directory → list_directory`
  (`tool_renamed` in `beeagent/ui/repl.py`)
* Near-miss typos (`reaid` → `read`) are repaired; anything else is refused with the
  real tool list handed back to the model, so it corrects itself.
* Missing arguments are reported to the model as `grep needs path; its parameters are:
  pattern, path, include` instead of crashing.
* Windows paths written with single backslashes inside JSON are repaired before parsing.

## Tools

The model can use these (type `/tools` to see the live list, including tools added by
plugins and MCP servers):

| Tool | Purpose |
| --- | --- |
| `read` | Read a file, lines numbered from 1; `offset` (0-based) / `limit` (default 2000) for long files |
| `write` | Create or overwrite a file |
| `edit` | Replace exact text in a file |
| `bash` | Run a shell command (Git Bash on Windows, so `&&`, `~`, `mkdir -p` work; `timeout` is clamped to 1–1800 s) |
| `list_directory` | List a folder: directories end with `/`, files show size; `recursive` is capped at 300 entries |
| `glob` | Find files by name pattern |
| `grep` | Search file contents with a regex; skips `.git`, `node_modules`, `__pycache__`, `.venv`, `.beeagent`, `build`, `dist` |
| `git` | Run git (`status`, `diff`, `log`, `commit`…) — the argv is parsed and guarded, so this is not a shell wearing a git hat |
| `web_search` | Search the web. **Needs `/allow`** even in `ask` mode: the query leaves the machine, and read-then-search is an exfiltration pair (`WebSearchTool.is_safe`) |
| `web_fetch` | Read one http(s) page as text: title kept, scripts and styles dropped, size capped out loud. A 404, a binary file, an internal address or a non-http scheme are named and refused rather than returned empty. **Needs `/allow`**, like `web_search` (`WebFetchTool.is_safe`) |
| `patch` | Apply one unified diff across several files: hunks are verified against the context, an offset is announced, and a diff that half-applies leaves every file byte-for-byte as it was |
| `move` | Rename or move a file or a directory — case-only renames included — inside the working roots, journalled so `/undo` gives it back |
| `remove` | Delete one file, or a tree with `recursive=true`; refuses the project folder itself, a link that leaves it, and `.git` without its own flag |
| `diagnostics` | Run the checkers that are already installed and return `path:line severity message (checker)`. Syntax needs nothing third-party; ruff / pyflakes / mypy / tsc are used only when present and are named as missing when not — so "never checked" can never read as "no problems". it writes nothing (compiles in-process, `--no-cache`, `--no-incremental`), so it runs without a grant — but mypy and tsc, which import plugins named in the repository's own config, are skipped until `/allow diagnostics` |
| `todo` | Keep a task list while working — and it writes it, to `.beeagent/todo.json` |
| `skill` | Load the full instructions of an installed skill. Registered by the built-in skill loader (`SkillTool` in `beeagent/plugins/loader.py`), not by the core tool loop, so `/extensions` does not list it as a plugin |
| `diagram` | Draw boxes and arrows, and **return the picture as text**, so the model reads back what it drew and fixes the overlaps itself; the same lines go to an `.svg` beside it — a plain name inside the working directory, `diagram.svg` by default |

## Providers and models

BeeCode ships with one working provider — **g4f**, keyless — and can use any
free-tier endpoint that speaks the OpenAI API once **you** add your own key.

```
/providers            what can serve requests right now, and what still needs a key
/key groq <token>     store a key you obtained yourself (saved to beeagent.json, never echoed)
/provider groq        switch; the model list and the default model follow the provider
/models               recommended first: widest context, then the rest of the catalog
/models --all         every model the pinned providers advertise, read off the installed g4f
/models command-r     filter by name, or by an upstream: CohereForAI_C4AI_Command
/model command-a-03-2025  pick one
```

Each row carries a window: `✔ 32k` was measured on this machine by the endpoint
refusing or forgetting a prompt, `~ 1M` is what the model id claims — a `-128k`
suffix in the name, or the family table in `beeagent/core/context.py`
(`_MODEL_FAMILIES`), which for the pool's models records what *their owner* states
rather than anything BeeCode measured. A `~` row is still sent at most 32768
tokens until `/window measure` proves your route. Measured wide
models lead the list; a model measured at 2k sits at the bottom whatever its name
says, because the claim describes a name and the measurement describes your route.

### What answers without a key

"Free" upstreams move constantly, so this is measured rather than believed. Nothing
below was re-measured while this page was edited — re-checking a README by spending
the user's quota is not a trade worth making, and `python scripts\probe_models.py`
is how you do it yourself in a minute. Two runs sit in the table: the window
numbers from g4f 8.5.7 on 2026-09-21, sent as prompts whose first line holds a
random code the model has
to repeat back; and the "does it still answer" column from g4f 8.5.1 on 2026-09-23
— one request per model id to each of the providers pinned then (`LLM7`,
Cohere ForAI), retried once, with the surviving ids asked again through the
shipped listing. `KiloCode` joined `KEYLESS_PROVIDERS` on 2026-09-24 and is
measured on its own line below:

| Route | Keyless | Result of the measurement |
| --- | --- | --- |
| `command-a-03-2025` (Cohere ForAI) | no | read the code back from **65536 tokens, twice**, ~2 s a step; answered again on 2026-09-23 in 26 s and 44 s for one word (`docs/MODELS.md`) |
| `command-r-plus-08-2024`, `command-r-08-2024`, `command-r7b-12-2024` | no | answered the one-word prompt on the same space in 13–55 s each; none has a measured window, so `/models` prints `~ 128k` — which is Cohere's claim for the Command R line read off `_MODEL_FAMILIES`, not a number BeeCode saw. Requests go at 32768 unless you raise `max_context_tokens` |
| `default` (LLM7) | no | read 65536 once, then answered round two with `429 rate_limit_exceeded`; answered again in 0.4–10 s. llm7.io advertises 44 ids at `/v1/models` and served exactly one of the names BeeCode asked for — `default` (`docs/MODELS.md`) |
| `command-r`, `command-r-plus`, `command-r7b-arabic-02-2025` | no | advertised by the pinned provider, but the space sent an empty completion twice for the first two and stayed silent for 95 s twice for the third — so they are dropped by name in `MEASURED_SILENT` and `discover_models()` cannot put them back |
| `gpt-4o`, `gemini-2.5-pro`, `kimi-k2`, `qwen-*`, `llama-4-*`, `grok-3`, `claude-*` | no | **dead on these routes.** Auto-routing served them; asked of the pinned providers they die as `Model gpt-4o not found` (Cohere ForAI) or `400 model_unavailable` (llm7.io). BeeCode ships no route to them without a key |
| `glm-*`, `deepseek-*`, `nvidia/*`, `cohere/*` and nine other vendor families | no | **not dead since 2026-09-24, and not measured either.** `KiloCode` joined the pin and its `KILOCODE_MEASURED` list carries 17 free ids (`z-ai/glm-5.2:free`, `nvidia/nemotron-3-super-120b-a12b:free`, …) — every one of them a name the row above called unreachable a day earlier. They were checked with a two-word prompt and a needle, never with a window probe, so `/models` shows them `~` and the table above says nothing about their size |
| `gpt-4` (Yqcloud) | no | **measured 2048** — refused 4k with 文字过长, "text too long"; dropped, it reaches its upstream through an undocumented relay with spoofed browser headers |
| `search` (GoogleSearch) | no | took 32768 without complaining, recall not verified; not pinned, g4f reaches it through a browser |
| Cloudflare | no | went silent on a 2048-token prompt for 90 s; dropped, g4f reaches it with a headless browser that clears a Turnstile check |
| `glm-4.7-flash`, `deepseek-chat`, `gemini-2.5-flash` | no | fine at 2048 **while auto-routing was in use**; at 4096 the free path returns 401, a key request, or goes quiet for minutes |
| OpenRouterFree, Nvidia, Pollinations, GeminiPro, G4FSpace, RelayRouter, OrcaRouter | no | hidden behind g4f's own broker, which demands proof-of-work "cake credits" (402) |
| DeepInfra, Copilot, Cerebras, HuggingChat, Airforce, KiloCode, OpenCode, MetaAI, OperaAria, DeepSeek | no | Turnstile token, a live Chrome over CDP, your browser cookies, an account, or a plain 401 — **except `KiloCode`, which was dropped from this row on 2026-09-24** when it became a pinned keyless route (two rows up) |

That is why `command-a-03-2025` is the default model: it is the one keyless route
measured to hold a real working session. The same page once reported a file-reading
turn at 5.0 s through it against 35.1 s on the `gpt-4` route; that pair is a
one-machine observation from before the pin dropped `gpt-4`, it is recorded nowhere
in this repository, and nobody has reproduced it since — treat it as an anecdote,
and `python scripts\probe_models.py` as the way to get a number you can believe.
Run `/window measure <model>` in
your own session to see what *your* route carries today — the answer is cached per
model and wins over any name-based guess.

`/providers` lists the built-in free-tier endpoints with the page where each key is issued:

| Provider | Where the free key comes from | What the free tier gives |
| --- | --- | --- |
| `g4f` | no key at all | the providers in `KEYLESS_PROVIDERS` — `LLM7`, `CohereForAI_C4AI_Command`, and `KiloCode` since 2026-09-24 — with 5 ids shipped and 22 the pin advertises today |
| `openrouter` | openrouter.ai/settings/keys | a shelf of `:free` models, no card |
| `groq` | console.groq.com/keys | very fast llama/qwen, generous free quota |
| `gemini` | aistudio.google.com/apikey | flash/mini models free per minute |
| `huggingface` | huggingface.co/settings/tokens | free inference credits |
| `github` | github.com/settings/tokens | gpt-4o/llama endpoints for your own account |
| `cerebras` | cloud.cerebras.ai | qwen/llama at high speed, free tier |
| `mistral` | console.mistral.ai | experiment tier, rate limited |
| `nvidia` | build.nvidia.com | free credits without a card, wide open-model catalog |
| `deepinfra` | deepinfra.com | trial credits on signup, pay-per-token after |
| `together` | api.together.xyz/settings/api-keys | one-time credit, some open models free |
| `crax` | gpt.crax.lol → Settings → API keys | OpenAI-compatible catalog; 40 req/min per IP, daily allowance per account |

`crax` is the one endpoint that answers `429` with two different problems, and
BeeCode separates them: a spent **daily allowance** moves to the next key (another
account), because waiting until midnight does not; a **per-IP** limit does not
care how many keys you have, so it stops and asks — wait it out, or raise your own
`vpn_command`, which is printed in full and runs only after you press the button.
Several keys go in one line, comma-separated: `/key crax <key1>,<key2>`.

Keys are read from `beeagent.json` (`api_keys`) or the matching environment
variable each preset names (`env` in `beeagent/providers/presets.py`, e.g.
`GROQ_API_KEY`). `beeagent.json` is gitignored **in this repository**, which ships
`beeagent.example.json` instead; it is not ignored in yours until you say so — see
[Add this to your project's .gitignore first](#add-this-to-your-projects-gitignore-first).
**Only your own keys**: BeeCode does not ship,
harvest or share other people's credentials, and using leaked ones gets the key,
the account and often the user's IP banned.

### Is a model actually working?

Free endpoints come and go, so ask the code instead of a README:

```bat
python scripts\probe_models.py            :: the 5 ids BeeCode ships (`G4fProvider.models`)
python scripts\probe_models.py --all      :: every id the pinned providers advertise (22 today, read offline)
```

The two sets are not one set any more: `KiloCode` joined `KEYLESS_PROVIDERS` on
2026-09-24 and advertises its own ids, so `--all` is larger than the shipped list.
Only the ids in `KILOCODE_MEASURED` were checked against that storefront; the rest
of its 394-entry catalogue is a paid page, which is why the pin carries a list
instead of reading it.

Each model gets one tiny request; the script prints `✅`/`❌`, latency and the
reply, and `--json` writes the raw results.

Last full run of the shipped list: **5 of 5** answered a one-word instruction
(2026-09-23, 0.4–55 s each — `docs/MODELS.md` has both passes per model). The run
that reported **620 of 645** measured g4f's
whole catalogue, and most of those names reached their endpoint through
auto-routing — including the browser-cleared ones — so the number no longer
describes a BeeCode request. [docs/MODELS.md](docs/MODELS.md) keeps both, and
says which is which.

### How big is the context window, really?

g4f does not publish window sizes — its `Model` carries a name and providers, and
provider classes keep `max_tokens = None` — so BeeCode **measures** them. Two
signals come off the endpoint: how large a prompt it refuses, and how large a
prompt the model still *saw*.

```
/window                      what BeeCode believes about the current model, and why
/window measure gpt-4o-mini  ask the endpoint (a few minutes of real requests)
```

```bat
python scripts\probe_window.py glm-4.7-flash --ceiling 32768
python scripts\probe_window.py --all-candidates
```

The second signal is the one that matters on free endpoints. They often do not
refuse an oversized prompt — they trim it and answer as if the conversation had
just started, which no error would ever reveal. So each probe request buries a
random code at the very start of the filler and asks for it back: a size the
model cannot repeat is a size it did not receive, and the window stops at the
last size it genuinely read. An endpoint that fumbles the trick at the smallest
prompt is dim rather than dishonest, and the measurement falls back to refusals.

Measurements are cached in `.beeagent/windows.json` — in the folder you ran it
from, which your project only stops committing if you ignored `.beeagent/` first —
and then win
over the guess taken from the model name, so the request ceiling follows the model
you actually picked. A refusal that names its limit (`maximum context length is
8192 tokens`) is used as stated; a refusal that only says "too long" still narrows
the window to the largest prompt that fitted. A rate limit, a revoked key or a
prompt that simply took too long says nothing about size, so the probe stops and
reports it instead of caching the last number it happened to send — a sick or slow
endpoint is not a small one.

## Slash commands

Type `/` in the REPL and the menu filters as you type; `Tab` completes, arguments
(models, providers, sessions, catalog entries) complete too, and list commands open a
mouse-clickable picker.

<!-- generated from beeagent/ui/commands.py by scripts/sync_readme.py -->
<!-- COMMANDS:BEGIN -->
| Command | What it does | Usage |
| --- | --- | --- |

### Model, Provider, Mode

| Command | What it does | Usage |
| --- | --- | --- |
| `/allow` | Grant one unsafe tool for this session | `/allow <tool>` |
| `/extensions` | What the installed plugins added | `/extensions` |
| `/key` | Store your own API key for a provider | `/key <provider> <token>` |
| `/lang` | Switch the interface language | `/lang <en|ru>` |
| `/mode` | Switch between normal and economy | `/mode <normal|economy>` |
| `/model` | Switch the active model | `/model <name>` |
| `/models` | List models with the widest context first, --all for every one | `/models [name|upstream] [--all]` |
| `/permissions` | Who may touch the machine: ask, auto or readonly | `/permissions <ask|auto|readonly>` |
| `/provider` | Switch the active provider | `/provider <name>` |
| `/providers` | List providers and which ones have a key | `/providers` |
| `/skin` | Choose interface variants: frames, banner, spinner | `/skin [slot] [variant]` |
| `/skins` | Skins that are code: list them, switch, see why one was refused | `/skins [name]` |
| `/trust` | Let this folder change how BeeCode behaves (plugins, permission gate) | `/trust [yes|no|reset]` |
| `/undo` | Undo what BeeCode wrote to your files, newest first | `/undo [n|list|clear]` |

### Skills, Plugins, Mcp

| Command | What it does | Usage |
| --- | --- | --- |
| `/mcp` | Manage MCP servers | `/mcp <list|add|remove|connect|tools>` |
| `/plugin` | Install/remove/enable an extension | `/plugin <install|remove|list|enable|disable> [name]` |
| `/plugins` | Browse the installable catalog | `/plugins [filter]` |
| `/skill` | Show a skill's instructions | `/skill <name>` |
| `/skills` | List installed skills | `/skills` |

### Git

| Command | What it does | Usage |
| --- | --- | --- |
| `/diff` | git diff | `/diff` |
| `/log` | git log | `/log [n]` |
| `/status` | git status | `/status` |

### Info And Status

| Command | What it does | Usage |
| --- | --- | --- |
| `/about` | About BeeCode | `/about` |
| `/config` | Show current configuration | `/config` |
| `/help` | Show all available commands | `/help` |
| `/pool` | Address, seat and budget of a key pool | `/pool [url <адрес> | enroll | status]` |
| `/stats` | Show economy/request stats | `/stats` |
| `/thinking` | Show the last model reasoning (scrollable) | `/thinking` |
| `/token` | Show current context token usage | `/token` |
| `/tools` | List registered tools | `/tools` |
| `/update` | Check for a newer BeeCode and install it | `/update` |
| `/window` | Show or measure the model context window | `/window [measure] [model]` |

### Sessions

| Command | What it does | Usage |
| --- | --- | --- |
| `/compact` | Fold the oldest turns into a digest now (no model call, no quota) | `/compact [N] | /compact yes [N]` |
| `/continue` | Load a saved session | `/continue <id>` |
| `/export` | Export session to a Markdown file | `/export [path]` |
| `/history` | Open the full history (scrollable) | `/history [list]` |
| `/load` | Alias for /continue | `/load <id>` |
| `/new` | Alias for /reset | `/new` |
| `/reset` | Start a new session (clear history) | `/reset` |
| `/save` | Save the current session now | `/save` |
| `/session` | Show current session info | `/session` |
| `/sessions` | List saved sessions | `/sessions` |
| `/stop` | Interrupt the answer that is being written now | `/stop` |
| `/tasks` | Show the task list the agent is keeping | `/tasks` |

### Run Tools Directly

| Command | What it does | Usage |
| --- | --- | --- |
| `/find` | Find files by glob | `/find <glob> [path]` |
| `/read` | Read a file | `/read <path>` |
| `/run` | Run a shell command | `/run <command>` |
| `/search` | Grep files by regex | `/search <pattern> [path]` |

### Ui

| Command | What it does | Usage |
| --- | --- | --- |
| `/bee` | Toggle the animated bee | `/bee` |
| `/clear` | Clear the screen / log | `/clear` |
| `/quit` | Exit BeeCode | `/quit` |
| `/theme` | Switch color theme | `/theme <name>` |
<!-- COMMANDS:END -->

## Skills, plugins and MCP servers

BeeCode has one extension mechanism with three kinds, browsed from a bundled catalog:

* **skills** — markdown instruction packs the model can load on demand (`/skills`, `/skill <name>`);
* **plugin packs** — Python packages that register extra tools (`/plugin install <name>`);
* **MCP servers** — external tools over the Model Context Protocol (`/mcp add`, `/mcp connect`).

```
/plugins                    catalog in a mouse-pickable list
/plugin install code-review install from the catalog (or a git URL)
/plugin list                what is installed, and what is switched off
/mcp add memory npx -y @modelcontextprotocol/server-memory
/mcp connect memory         start it and cache its tool schemas
```

First-time MCP discovery can take a minute (npm downloads), so it never runs at startup:
startup loads cached schemas only, and `/mcp connect` does the rest explicitly.

Two of these packs need something installed to do their job, and each one says so
when its dependency is not there instead of failing quietly:

| Pack | What it adds | Without its dependency |
| --- | --- | --- |
| `browser` | the `browser` tool for the model: navigate, click, type, read text, run javascript, screenshot — every answer structured, and the screenshot also drawn for the human (`/allow browser` first, it reaches the network) | Playwright is not installed: the tool answers with the one command that fixes it and never pretends to have opened a page |
| `edit-ui` | `/edit-ui <file>` — a full-screen file editor *you* type into, beside the agent's own `edit` tool. CRLF and Cyrillic kept byte-for-byte, a big file opened as a capped window with the real size named, unsaved changes asked about once | no full-screen app (the classic REPL): the command answers with words and touches no file |
| `skin-baseline` | the quiet status line: one repaint when something actually changed | — |
| `skin-pulse` | a status line that breathes at 12 fps and counts the wait | — |
| `skin-pet` | a bee that blinks, hides on an error and shivers on a refusal | — |
| `skin-hud` | takes the lines over: its own wording on the status and waiting lines, and a strip above them with the running tool, a bar for how long this turn has taken, and the token count | — |
| `skin-shimmer` | draws the **answer**: a honey→leaf outline on all four edges, list markers and headings that walk the ramp, a railed streaming answer, heavy borders on every panel | the classic REPL gets the frame, banner and rail; the answer block and the HUD work in both |

`/skins` lists them, switches between them and shows why a skin was refused or
demoted; the format a skin implements is in [docs/SKINS.md](docs/SKINS.md).

## Making BeeCode your own

Two knobs, both reachable without reading the source.

**Interface slots.** Every panel asks the skin how to be framed, so the decoration
is one setting, not a fork:

```
/skin                       slots, what is on now, and what you can pick
/skin frame none            take the borders away (rounded · heavy · square · ascii)
/skin banner none           no animated logo (shimmer · static · none)
/skin spinner dots          quiet waiting line instead of "wiping honey off the keyboard…"
/skin reset                 back to defaults
```

The choice is saved to `beeagent.json` under `ui`, so it survives a restart.

**The plugin API.** A plugin directory with a `setup(api)` function can add
capabilities and never has to import the REPL or patch a module:

```python
# .beeagent/plugins/word-count/plugin.py
from beeagent.tools.base import BaseTool, ToolResult

class WordCountTool(BaseTool):
    name = "word_count"
    description = "Count words and lines in a file."
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}

    def execute(self, path: str = "") -> ToolResult:
        text = open(path, encoding="utf-8").read()
        return ToolResult(output=f"{len(text.split())} words", error=False)

TOOLS = [WordCountTool()]                    # a tool for the model

def setup(api):
    api.setting("echo", True, "note after every answer")        # its own setting
    api.command("wc", "Count words", my_handler, usage="/wc <path>")
    api.event("done", lambda event, data: print(len(data["text"].split())))
    api.skin("frame", "plain", {"box": my_space_box})           # an interface variant
```

`/extensions` lists what every plugin added and who added it. A plugin may **add**,
never **replace**: a command or tool whose name is taken is refused rather than
quietly winning — an extension that could take over `bash` would inherit the
permission you gave the real one, and a plugin that crashes shows a note instead of
killing the REPL.

**Shipped skins.** Four plugins exist purely to prove the slots, and each is a
working example of a different kind of extension:

| plugin | what it changes |
| --- | --- |
| `plain` | frameless panels, static logo, quiet waiting line |
| `skin-work` | thinnest possible: no logo at all, no phrases, minimal borders |
| `skin-hive` | its own frame colour, a hex-comb opening, buzzing waiting lines |
| `skin-terminal` | ASCII borders, a one-line header, and **its own answer renderer** |

The last one is the interesting case: a slot can hold a *callable* (banner,
spinner) or a *class* (stream), so a plugin can decide how the model's answer
appears on screen, not just what colour its border is. `/skin stream default`
puts the built-in renderer back.

Also in the catalog: `word-count` — tool + command + setting + event listener in
one file.

## A key pool

If you (or someone you trust) runs a BeeCode pool — a small proxy that holds API
keys and answers instead of handing them out — BeeCode talks to it as a provider:

```
/pool enroll          # the address is already set; change it with /pool url …
/provider pool
/pool status
```

`/pool enroll` asks the pool for a seat and writes its token to `pool_token` in
`beeagent.json` — the same plaintext file as your API keys, in the folder you are
working in, so it is yours to ignore before you commit anything. The screen only
ever shows the last four characters. The address it writes to is the one BeeCode
ships with (`pool_url` in `beeagent/config/schema.py`), and running your own pool
means pointing at it with `/pool url https://your-host:8077`; `/pool url` on a plain
`http://` address warns you that the seat token travels in the clear.

A seat token is not one of your provider keys and the pool never sees yours — but
it is a credential: it authorises this install to spend the operator's budget, and
it is worth keeping as privately as a key. The pool decides which account serves a
model, so `/provider pool` works
with the model names you already use. Errors come back as what they are — no seat
yet, seat awaiting approval, today's budget spent, every key rate-limited — with
the retry wait attached.

## Configuration

`beeagent.json` in the working directory (see `beeagent.example.json`):

| Field | Default | Meaning |
| --- | --- | --- |
| `model` | `command-a-03-2025` | Model id passed to the provider |
| `provider` | `g4f` | Which registered provider to use |
| `mode` | `normal` | `economy` enables answer caching |
| `native_tools` | `true` | Send the calls in the request's `tools` field when the provider takes them, instead of as JSON prose |
| `max_turns` | `50` | Tool-loop iterations per request |
| `max_context_tokens` | `0` | Ceiling for one request. `0` takes the model's window capped at 32768; a larger value lifts that cap towards what the model claims for itself, never past 262144. It is a ceiling, not a pin: it can also cut the budget below the model's window |
| `stream_idle_timeout` | `90` | Seconds a streamed reply may stay silent before the request is cut |
| `language` | `en` | Interface language, `en` or `ru` |
| `custom_providers` | `[]` | `openai_compat` / `ollama` endpoints, each carrying its own `key` |
| `api_keys` | `{}` | Your own keys per provider, added with `/key` (never printed in full) |
| `pool_url` | `https://beecode-pool.onrender.com` | The key pool this install enrolls with |
| `pool_token` | `""` | The seat token `/pool enroll` wrote here — a credential in that plaintext file; see the gitignore note above |
| `vpn_command` | `""` | The command BeeCode offers, in full, to change your exit address |
| `permissions.mode` | `ask` | `ask` · `auto` · `readonly` — see [Permissions](#permissions) |
| `permissions.allowed` | `[]` | Tools pre-approved for every session, e.g. `["bash", "write"]` |
| `ui` | `{}` | Interface slots: `frame`, `banner`, `spinner`, `stream` — see `/skin` |
| `extensions` | `{}` | Settings owned by plugins, keyed by plugin name |
| `economy.cache_enabled` | `true` | Turn answer caching off even in economy mode |
| `economy.cache_dir` | `.beeagent/cache` | Where cached answers live — sessions sit beside it in `.beeagent/sessions/` |
| `economy.cache_ttl_minutes` | `30` | How long a cached answer may stand before it is re-asked |

Every field of `BeeConfig` has a row here, and `tests/test_docs_match_code.py`
fails when a new one ships without one.
`BEECODE_LANG=ru` and `BEECODE_NO_ANIM=1` are also honoured.

## Permissions

BeeCode edits real files and runs real commands, so **the model does not decide
what is allowed — you do.** A reply from a free endpoint is a guess; guessing
`rm -rf` should not be enough to run it.

| Mode | What runs without asking | Switch |
| --- | --- | --- |
| `ask` (default) | Readers: `read`, `grep`, `glob`, `list_directory`, `skill`, `diagnostics` — plus `todo` and `diagram`, which need no grant but each write one file of their own (`.beeagent/todo.json`, a `.svg`) | `/permissions ask` |
| `auto` | Everything | `/permissions auto` |
| `readonly` | Only the readers — anything that writes is refused here too: `todo`, `diagram`, `patch`, `move`, `remove`, and every tool you granted by hand | `/permissions readonly` |

`web_search` and `web_fetch` are in none of those lists: `is_safe()` returns
`False` for them on purpose, because the request leaves the machine and a tool
that can read any file plus a tool that can send text out is an exfiltration
pair. Grant them with `/allow web_search` and `/allow web_fetch` when you want
them — a model that has not been granted one says so instead of quietly
inventing an answer.

In `ask` mode a tool that changes the machine — `write`, `edit`, `bash`, `git`,
`web_search`, `web_fetch`, and anything a plugin or MCP server adds — is refused, and you see why:

```
⛔ bash command='pytest -q'  blocked — no permission
   allow it: /allow bash   or /permissions auto to trust the model
```

`/allow <tool>` grants one tool for the session (`/allow remove <tool>` takes it
back), and the model is told the rules too, so it asks you instead of retrying.
Put names in `permissions.allowed` in `beeagent.json` to keep a grant forever.

The same rule covers extensions: `/plugin install <git-url>` clones code that
later runs inside BeeCode, so it needs `--trust` — after you have read it.

The economy cache keeps only final plain-text answers, never a half-finished
tool step, and expires entries after `cache_ttl_minutes` because a reply about
a file stops being true when the file changes.

## Language

The interface is English by default. `/lang ru` (or `--lang ru`, or `BEECODE_LANG=ru`)
switches every message, table and status line to Russian at runtime and saves the
choice to `beeagent.json`. Both languages live side by side in the source —
`L("english", "русский")` — so a new string cannot ship in one language only.

## Development

```bat
git clone https://github.com/egorVasile/beecode.git
cd beecode
pip install -e .
python -m pytest tests\ -q        200+ tests
python scripts\make_screenshots.py   regenerate the images above
```

Layout: `beeagent/core` (loop, context, parser, session), `beeagent/tools` (the tool
registry), `beeagent/providers` (g4f and custom endpoints), `beeagent/plugins`
(catalog, installer, MCP client), `beeagent/ui` (REPL, streaming renderer, commands,
scroll viewer), `tests/`.

## What we borrowed

BeeCode stands on other people's work, deliberately and by name:

* **[g4f](https://github.com/xtekky/gpt4free)** — free access to public model endpoints.
  Without it "no API key" would be a marketing phrase.
* **[Model Context Protocol](https://modelcontextprotocol.io)** — the JSON-RPC-over-stdio
  tool protocol implemented in `beeagent/plugins/mcp.py`.
* **DeepSeek Harness** — the idea that skills, plugins and MCP servers are one catalog
  with one installer, borrowed from its plugin model.
* **[rich](https://github.com/Textualize/rich)** and **[prompt_toolkit](https://github.com/Textualize/python-prompt-toolkit)** —
  terminal rendering and input; both by the Textualize team.
* The agent-loop shape (system prompt → tool call → `[tool result]` → repeat) is the
  pattern popularised by Claude Code, Aider and OpenHands.

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).

BeeCode was written in a terminal, with an AI agent, on Windows. It is a young project:
expect rough edges, and please [open an issue](https://github.com/egorVasile/beecode/issues)
when you find one.

---

<p align="center">
  News, releases and screenshots: <a href="https://t.me/beecodee">t.me/beecodee</a>
</p>
