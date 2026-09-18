# BeeAgent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a Python CLI AI coding agent using g4f with plugin architecture, economy mode, and full toolset.

**Architecture:** Plugin-based monolith — core agent loop discovers tools and providers from module directories. g4f provides free model access, with fallback to OpenAI-compatible and Ollama providers.

**Tech Stack:** Python 3.10+, g4f, Rich, Pydantic, httpx, tiktoken

---

## File Structure

```
C:\agent\
├── beeagent/
│   ├── __init__.py
│   ├── cli.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── agent.py
│   │   ├── session.py
│   │   ├── context.py
│   │   └── economy.py
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── g4f_provider.py
│   │   ├── openai_compat.py
│   │   ├── ollama.py
│   │   └── registry.py
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── registry.py
│   │   ├── read.py
│   │   ├── write.py
│   │   ├── edit.py
│   │   ├── bash.py
│   │   ├── grep.py
│   │   ├── glob_tool.py
│   │   ├── web_search.py
│   │   ├── git.py
│   │   ├── todo.py
│   │   └── task.py
│   ├── config/
│   │   ├── __init__.py
│   │   ├── schema.py
│   │   └── loader.py
│   └── utils/
│       ├── __init__.py
│       ├── cache.py
│       ├── tokens.py
│       └── markdown.py
├── beeagent.json
├── pyproject.toml
└── tests/
    ├── __init__.py
    ├── test_tools.py
    ├── test_providers.py
    ├── test_agent.py
    └── test_economy.py
```

---

## Task 1: Project Scaffolding

**Files:** `pyproject.toml`, `beeagent/__init__.py`, `beeagent.json`, all `__init__.py`

- [ ] Create `pyproject.toml` with dependencies: g4f, rich, pydantic, httpx, tiktoken
- [ ] Create `beeagent/__init__.py` with `__version__ = "0.1.0"`
- [ ] Create all empty `__init__.py` files for subpackages
- [ ] Create default `beeagent.json` config
- [ ] Run `pip install -e .` to verify
- [ ] Git init + commit

## Task 2: Config Schema + Loader

**Files:** `beeagent/config/schema.py`, `beeagent/config/loader.py`, `tests/test_config.py`

- [ ] Write tests for Pydantic config models (BeeConfig, EconomyConfig, CustomProvider)
- [ ] Run tests to verify they fail
- [ ] Implement schema.py with Pydantic BaseModel classes
- [ ] Implement loader.py with load_config() and save_config()
- [ ] Run tests — all pass
- [ ] Commit

## Task 3: Base Tool Interface + Registry

**Files:** `beeagent/tools/base.py`, `beeagent/tools/registry.py`, `tests/test_tools.py`

- [ ] Write tests for BaseTool, ToolResult, ToolRegistry
- [ ] Run tests to verify they fail
- [ ] Implement base.py with BaseTool ABC and ToolResult dataclass
- [ ] Implement registry.py with register(), get(), list_names(), to_schemas()
- [ ] Run tests — all pass
- [ ] Commit

## Task 4: Tool — Read

**Files:** `beeagent/tools/read.py`

- [ ] Write tests for ReadTool (read file, missing file, is_safe)
- [ ] Implement ReadTool with offset/limit support
- [ ] Run tests — all pass
- [ ] Commit

## Task 5: Tool — Write

**Files:** `beeagent/tools/write.py`

- [ ] Write tests for WriteTool (write file, create dirs, is_not_safe)
- [ ] Implement WriteTool with auto directory creation
- [ ] Run tests — all pass
- [ ] Commit

## Task 6: Tool — Edit

**Files:** `beeagent/tools/edit.py`

- [ ] Write tests for EditTool (replace, not found)
- [ ] Implement EditTool with exact match + ambiguity detection
- [ ] Run tests — all pass
- [ ] Commit

## Task 7: Tool — Bash

**Files:** `beeagent/tools/bash.py`

- [ ] Write tests for BashTool (simple, error, timeout)
- [ ] Implement BashTool with subprocess + timeout
- [ ] Run tests — all pass
- [ ] Commit

## Task 8: Tools — Grep + Glob

**Files:** `beeagent/tools/grep.py`, `beeagent/tools/glob_tool.py`

- [ ] Write tests for GrepTool and GlobTool
- [ ] Implement GrepTool with regex + include filter
- [ ] Implement GlobTool with pathlib.glob
- [ ] Run tests — all pass
- [ ] Commit

## Task 9: Tools — Web Search + Git + Todo + Task

**Files:** `beeagent/tools/web_search.py`, `beeagent/tools/git.py`, `beeagent/tools/todo.py`, `beeagent/tools/task.py`

- [ ] Implement WebSearchTool (DuckDuckGo HTML scraping)
- [ ] Implement GitTool (subprocess wrapper)
- [ ] Implement TodoTool (JSON persistence)
- [ ] Implement TaskTool (placeholder for delegation)
- [ ] Write tests for all four
- [ ] Run tests — all pass
- [ ] Commit

## Task 10: Provider — Base + Registry

**Files:** `beeagent/providers/base.py`, `beeagent/providers/registry.py`, `tests/test_providers.py`

- [ ] Write tests for BaseProvider, ProviderRegistry
- [ ] Implement BaseProvider ABC with chat() and chat_stream()
- [ ] Implement ProviderRegistry with fallback()
- [ ] Run tests — all pass
- [ ] Commit

## Task 11: Provider — g4f Integration

**Files:** `beeagent/providers/g4f_provider.py`

- [ ] Write tests for G4fProvider init and model list
- [ ] Implement G4fProvider using g4f.client.AsyncClient
- [ ] Run tests — all pass
- [ ] Commit

## Task 12: Providers — OpenAI-Compat + Ollama

**Files:** `beeagent/providers/openai_compat.py`, `beeagent/providers/ollama.py`

- [ ] Implement OpenAICompatProvider with httpx
- [ ] Implement OllamaProvider with httpx
- [ ] Commit

## Task 13: Economy Mode

**Files:** `beeagent/core/economy.py`, `beeagent/utils/cache.py`, `beeagent/utils/tokens.py`, `tests/test_economy.py`

- [ ] Write tests for ResponseCache and EconomyManager
- [ ] Implement ResponseCache with SHA256 hashing
- [ ] Implement token counter with tiktoken fallback
- [ ] Implement EconomyManager with cache/batch/routing
- [ ] Run tests — all pass
- [ ] Commit

## Task 14: Session + Context Management

**Files:** `beeagent/core/session.py`, `beeagent/core/context.py`

- [ ] Implement Session with JSON persistence
- [ ] Implement ContextManager with token-aware message building
- [ ] Commit

## Task 15: Main Agent Loop

**Files:** `beeagent/core/agent.py`

- [ ] Implement Agent class with full agentic loop
- [ ] Wire providers, tools, economy, session, context together
- [ ] Implement tool call parsing from LLM response
- [ ] Commit

## Task 16: CLI Entry Point

**Files:** `beeagent/cli.py`

- [ ] Implement argparse CLI with -p, --model, --provider, --mode, --continue
- [ ] Implement REPL mode
- [ ] Implement models/providers subcommands
- [ ] Test: `python -m beeagent.cli models`
- [ ] Commit

## Task 17: Markdown Rendering

**Files:** `beeagent/utils/markdown.py`

- [ ] Implement Rich-based markdown and code rendering
- [ ] Commit

## Task 18: Integration Test

**Files:** `tests/test_agent.py`

- [ ] Write agent initialization test
- [ ] Write tool parsing test
- [ ] Run full test suite
- [ ] Final commit
