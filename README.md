<p align="center">
  <img src="docs/screenshots/01-banner.png" alt="BeeCode banner" width="720">
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
     found files 12 files
   ⏳ read path='api/test_client.py'
     read api/test_client.py
   ⏳ bash command='pytest api -q'
     executed pytest
     3 passed in 1.42s
   ✅ done — changed api/client.py: retry on timeout, see diff above
```

<p align="center">
  <img src="docs/screenshots/02-session.png" alt="A BeeCode session" width="720">
</p>

---

## Install

One command, in `cmd.exe` (Python 3.10+ required):

```bat
pip install git+https://github.com/egorVasile/beecode.git
```

Then start it from the project you want to work on:

```bat
cd C:\path\to\your\project
beecode
```

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

## Run

| Command | What it does |
| --- | --- |
| `beecode` | Interactive REPL (the default interface) |
| `beecode --model glm-4.7-flash` | Start with a specific model |
| `beecode --provider g4f` | Start with a specific provider |
| `beecode --mode economy` | Cache answers and reuse them across identical requests |
| `beecode --lang ru` | Russian interface (default is English) |
| `beecode --continue` | Resume the last saved session |
| `beecode --tui` | Full-screen Textual interface instead of the REPL |
| `beecode -p "explain this repo"` | One-shot: ask, print the answer, exit |
| `beecode models` | List every model the installed g4f can reach |
| `beecode providers` | List configured providers |
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

A single huge tool output (a big file read, a recursive glob) used to evict the whole
conversation and the model would answer as if the chat had just started. Now oversized
messages are **clipped** (head + tail kept, middle marked as truncated), messages that
still do not fit are skipped individually, and the request you are working on is always
pulled back into the window. When something is dropped you see it:

```
✂ history did not fit the context: dropped 3 older messages, big outputs are clipped — the task stays in view
```

### Mistakes are recovered, not fatal

* `list_files`, `read_directory`, `read_files` — invented names for `list_directory`,
  accepted as aliases: `🔧 tool name corrected: read_directory → list_directory`
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
| `read` | Read a file, lines numbered; `offset`/`limit` for long files |
| `write` | Create or overwrite a file |
| `edit` | Replace exact text in a file |
| `bash` | Run a shell command (Git Bash on Windows, so `&&`, `~`, `mkdir -p` work) |
| `list_directory` | List a folder: directories end with `/`, files show size |
| `glob` | Find files by name pattern |
| `grep` | Search file contents with a regex |
| `git` | Run git (`status`, `diff`, `log`, `commit`…) |
| `web_search` | Search the web |
| `todo` | Keep a task list while working |
| `skill` | Load the full instructions of an installed skill |

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
| `/lang` | Switch the interface language | `/lang <en|ru>` |
| `/mode` | Switch between normal and economy | `/mode <normal|economy>` |
| `/model` | Switch the active model | `/model <name>` |
| `/models` | List available models | `/models` |
| `/provider` | Switch the active provider | `/provider <name>` |
| `/providers` | List available providers | `/providers` |

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
| `/stats` | Show economy/request stats | `/stats` |
| `/thinking` | Show the last model reasoning (scrollable) | `/thinking` |
| `/token` | Show current context token usage | `/token` |
| `/tools` | List registered tools | `/tools` |

### Sessions

| Command | What it does | Usage |
| --- | --- | --- |
| `/continue` | Load a saved session | `/continue <id>` |
| `/export` | Export session to a Markdown file | `/export [path]` |
| `/history` | Open the full history (scrollable) | `/history [list]` |
| `/load` | Alias for /continue | `/load <id>` |
| `/new` | Alias for /reset | `/new` |
| `/reset` | Start a new session (clear history) | `/reset` |
| `/save` | Save the current session now | `/save` |
| `/session` | Show current session info | `/session` |
| `/sessions` | List saved sessions | `/sessions` |

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

## Configuration

`beeagent.json` in the working directory (see `beeagent.example.json`):

| Field | Default | Meaning |
| --- | --- | --- |
| `model` | `gpt-4` | Model id passed to the provider |
| `provider` | `g4f` | Which registered provider to use |
| `mode` | `normal` | `economy` enables answer caching |
| `max_turns` | `50` | Tool-loop iterations per request |
| `language` | `en` | Interface language, `en` or `ru` |
| `custom_providers` | `[]` | `openai_compat` / `ollama` endpoints |
| `economy.cache_dir` | `.beeagent/cache` | Where answers and sessions live |

`BEECODE_LANG=ru` and `BEECODE_NO_ANIM=1` are also honoured.

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
