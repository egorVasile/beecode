# BeeAgent — Design Spec

## Overview

BeeAgent is a Python-based AI coding agent that uses g4f (GPT4Free) as its primary model provider. It supports custom providers (OpenAI-compatible, Ollama), has a plugin-based architecture, and features an economy mode that minimizes request count without degrading response quality.

## Goals

1. Free AI coding agent — no accounts, no quotas, no registration
2. Plugin architecture — easy to add tools and providers
3. Two modes: normal (full agent) and economy (minimal requests)
4. CLI interface with REPL and non-interactive modes
5. Full toolset: read, write, edit, bash, grep, glob, web search, git, todo, task delegation

## Architecture

### Directory Structure

```
beeagent/
├── beeagent/
│   ├── __init__.py
│   ├── cli.py              # CLI entry point (argparse)
│   ├── core/
│   │   ├── agent.py         # Main agentic loop
│   │   ├── session.py       # Session management
│   │   ├── context.py       # Context window + compaction
│   │   └── economy.py       # Economy mode logic
│   ├── providers/
│   │   ├── base.py          # BaseProvider abstract class
│   │   ├── g4f_provider.py  # g4f wrapper (no quota checks)
│   │   ├── openai_compat.py # OpenAI-compatible endpoints
│   │   ├── ollama.py        # Local models (Ollama)
│   │   └── registry.py      # Provider registry + fallback
│   ├── tools/
│   │   ├── base.py          # BaseTool abstract class
│   │   ├── registry.py      # Tool registry
│   │   ├── read.py
│   │   ├── write.py
│   │   ├── edit.py
│   │   ├── bash.py
│   │   ├── grep.py
│   │   ├── glob.py
│   │   ├── web_search.py
│   │   ├── git.py
│   │   ├── todo.py
│   │   └── task.py          # Sub-task delegation
│   ├── config/
│   │   ├── schema.py        # Pydantic config models
│   │   └── loader.py        # beeagent.json loader
│   └── utils/
│       ├── cache.py         # Response caching (SHA256)
│       ├── tokens.py        # Token counting
│       └── markdown.py      # Terminal markdown rendering
├── beeagent.json            # Default config
├── pyproject.toml
└── README.md
```

### Plugin System

Tools and providers are Python modules that implement base classes:

```python
# Tool plugin
class BaseTool:
    name: str
    description: str
    parameters: dict  # JSON Schema
    
    def execute(self, **kwargs) -> str: ...
    def is_safe(self) -> bool: ...  # auto-approve eligible

# Provider plugin
class BaseProvider:
    name: str
    models: list[str]
    
    async def chat(self, messages, model, stream=False) -> str: ...
    async def chat_stream(self, messages, model) -> AsyncIterator[str]: ...
```

Plugins are discovered by scanning `beeagent/tools/` and `beeagent/providers/` directories.

### Agentic Loop

```
while True:
    1. Build context (system_prompt + history + tool_schemas)
    2. Send to LLM via active provider
    3. If LLM calls tool → execute, add result to history
    4. If LLM returns text → display to user, break
    5. Check economy mode constraints
    6. Repeat
```

Termination conditions:
- LLM returns text without tool calls
- Max turns reached (configurable, default: 50)
- User interrupt (Ctrl+C)

## Providers

### g4f Provider (Primary)

Uses `g4f.client.AsyncClient` directly, bypassing quota/account checks:

```python
from g4f.client import AsyncClient

class G4fProvider(BaseProvider):
    async def chat(self, messages, model, stream=False):
        client = AsyncClient()
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            stream=stream
        )
        return response.choices[0].message.content
```

### OpenAI-Compatible Provider

For any endpoint exposing `/v1/chat/completions`:

```python
class OpenAICompatProvider(BaseProvider):
    def __init__(self, base_url, api_key=None):
        self.base_url = base_url
        self.api_key = api_key
    
    async def chat(self, messages, model, stream=False):
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/chat/completions",
                json={"model": model, "messages": messages},
                headers={"Authorization": f"Bearer {self.api_key}"}
            )
            return resp.json()["choices"][0]["message"]["content"]
```

### Ollama Provider

For local models:

```python
class OllamaProvider(BaseProvider):
    async def chat(self, messages, model, stream=False):
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "http://localhost:11434/api/chat",
                json={"model": model, "messages": messages, "stream": stream}
            )
            return resp.json()["message"]["content"]
```

### Provider Registry + Fallback

```
Priority: custom_providers → g4f → openai_compat → ollama
On failure → try next provider
```

## Economy Mode

Two modes controlled by `--mode normal|economy`:

### Normal Mode
- Full context sent with every request
- All tools available
- No request optimization
- Maximum quality

### Economy Mode
Three mechanisms to minimize request COUNT:

| Mechanism | How | Savings |
|---|---|---|
| Prompt cache | SHA256(prompt+model) → cached response | 100% for repeats |
| Batch tools | 3+ small tool calls → 1 LLM request | ~60% small calls |
| Smart routing | grep/glob → tiny model, codegen → big | ~40% tokens |
| Context trim | Remove old tool results from context | ~30% context |

## Tools

| Tool | Description | Safe | Parallel |
|---|---|---|---|
| `read` | Read file contents | ✅ | ✅ |
| `write` | Create/overwrite file | ❌ | ❌ |
| `edit` | Precise text replacement | ❌ | ❌ |
| `bash` | Execute shell command | ❌ | ❌ |
| `grep` | Search file contents (regex) | ✅ | ✅ |
| `glob` | Find files by pattern | ✅ | ✅ |
| `web_search` | Search the internet | ✅ | ✅ |
| `git` | Git operations | ❌ | ❌ |
| `todo` | Manage task list | ✅ | ✅ |
| `task` | Delegate sub-task to agent | ✅ | ❌ |

### Tool Interface

Each tool returns a structured result:

```python
@dataclass
class ToolResult:
    output: str       # Text output
    error: bool       # Is error?
    metadata: dict    # Extra info (file size, line count, etc.)
```

## CLI Interface

### Commands

```bash
# Interactive REPL
beeagent

# One-shot query
beeagent -p "Find the bug in auth.py"

# With model selection
beeagent -p "Generate tests" --model gpt-4

# Economy mode
beeagent -p "Explain this code" --mode economy

# Continue last session
beeagent --continue

# Custom provider
beeagent -p "Hello" --provider my-ollama

# List available models
beeagent models

# List providers
beeagent providers
```

### Output Formats

- Default: Rich terminal output with syntax highlighting
- `--format json`: Structured JSON for scripting
- `--format stream`: Streaming tokens as they arrive

## Configuration

`beeagent.json` at project root:

```json
{
  "model": "gpt-4",
  "provider": "g4f",
  "mode": "normal",
  "max_turns": 50,
  "custom_providers": [
    {
      "name": "my-ollama",
      "type": "ollama",
      "url": "http://localhost:11434",
      "model": "codellama"
    },
    {
      "name": "my-api",
      "type": "openai_compat",
      "url": "https://api.example.com",
      "key": "${MY_API_KEY}",
      "model": "custom-model"
    }
  ],
  "economy": {
    "cache_enabled": true,
    "batch_tools": true,
    "smart_routing": true,
    "cache_dir": ".beeagent/cache"
  },
  "tools": {
    "bash": {
      "safe_commands": ["git status", "ls", "pwd"]
    }
  }
}
```

## Dependencies

```toml
dependencies = [
    "g4f>=0.3.0",
    "rich>=13.0",
    "pydantic>=2.0",
    "httpx>=0.25",
    "tiktoken",
]
```

## Future: IDE Version

The plugin architecture enables future IDE integration:
- LSP server for code intelligence
- WebSocket connection for real-time updates
- VS Code extension as first IDE target
- Same core engine, different frontend
