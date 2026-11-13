# Forging BeeCode extensions (read this when the user asks for a feature)

You can extend this agent mid-session: write the files, check them with the
`forge` tool, repair what it names, install them — no restart. What lands
answers from your NEXT turn (the current request's tool list already left).

## The loop

1. Write the files with the `write` tool, straight into
   `.beeagent/plugins/<name>/` (lowercase letters, digits, `-`, `_`).
2. `forge action=validate name=<name>` — fix every `✖`, read every `⚠`.
3. `forge action=install name=<name>` — loads it now; repair loader errors
   the report names, if any.
4. Use it next turn. `forge action=list` shows what you forged;
   `forge action=remove name=<name>` takes one back out.

Trust, in one line: installing pins the exact bytes, so editing a pack
re-arms the question — a pack you change is a pack the user re-approves.

## A skill: `SKILL.md`

Instructions, no code. Frontmatter first (or plain text — both load):

```markdown
---
description: when to use this, in one line
---

# What to do

Steps the model follows after loading this with the `skill` tool...
```

## A plugin pack: `plugin.py` (+ optional `plugin.json`)

Code that runs. Never executed during validation — only on install, through
the same gate as downloaded packs. Two contracts, mix freely:

```python
from beeagent.tools.base import BaseTool, ToolResult

class MyTool(BaseTool):
    name = "mytool"  # lowercase, unique — collisions are refused at load
    description = "What it does, first sentence complete."
    parameters = {"type": "object",
                  "properties": {"path": {"type": "string"}},
                  "required": ["path"]}

    def execute(self, path: str = "") -> ToolResult:
        return ToolResult(output="...", error=False)

    def is_safe(self) -> bool:
        return True  # False when it writes, runs, or reaches the network

TOOLS = [MyTool()]
```

```python
def setup(api):
    # A slash command. handler(ctx, args) returns a string or a
    # beeagent.ui.commands.CommandResult. ctx.agent reaches the live agent.
    def mycmd(ctx, args):
        return "answer text"

    api.command("mycmd", "What /mycmd does", mycmd, usage="/mycmd [args]")
```

More surface, same object: `api.tool(MyTool())`, `api.setting("key",
default, "help")` + `api.get("key")`, `api.event("tool_end", callback)`,
`api.skin(slot, name, value)`. Add, never replace: taken tool/command names
are refused. `plugin.json` (optional): `{"name": "...", "version": "1.0.0",
"type": "plugin", "description": "...", "entry": "plugin.py"}`.

Keep packs small (plugin.py under ~200 KB), UTF-8, stdlib + `rich` only —
anything else will not be where the pack runs.

## Downloading one instead of writing it

- `forge action=search query=<words> [kind=skill|plugin|mcp]` — the shipped
  catalog plus the pool's licence-gated market.
- `forge action=download target=<catalog-name|market-id|git-url>` — a skill is
  instructions and installs directly. A plugin or MCP server is code that runs
  inside BeeCode: ask the user in plain text and pass `trust=true` only after a
  clear yes. Market entries are hash-checked against the index before a byte is
  written.
- A catalog MCP entry adds a server config; `forge action=connect
  name=<server>` starts it and registers its tools for the next turn.
- Market MCP entries are pointers, not artefacts (their download is a licence
  file). They carry a `run` string — add one with `forge action=mcp_add
  name=<server> command=npx args=["-y","package"] trust=true`.
- `forge action=installed` lists everything installed; `forge action=remove
  name=<x>` takes one back out.
