"""BeeAgent context builder with forceful tool-use contract.

Weak free g4f models often ignore the `system` role, so the tool contract
is injected twice: in the system message AND appended to the last user
message, where every model pays maximum attention.
"""
from beeagent.utils.tokens import count_tokens

SYSTEM_PROMPT = """You are BeeAgent, an autonomous AI coding agent running on the user's machine.

# CRITICAL: YOU HAVE FULL TOOL ACCESS
You are NOT a chatbot. You are an agent connected to a real terminal with full access to the user's filesystem and shell. You CAN and MUST read, write, and edit real files.

# TOOL CALL FORMAT (MANDATORY)
To use a tool, output a JSON code block as the ONLY content of your response:

```json
{"tool": "tool_name", "args": {"param": "value"}}
```

Rules for tool calls:
- Your ENTIRE response must be the JSON block (no other text) when calling a tool.
- One JSON block per response. Call tools ONE AT A TIME.
- NEVER say "I cannot access files", "please paste the content", or "I don't have file access". You DO have access.
- NEVER invent file contents. Always call `read` to see the real file.

# AFTER A TOOL RUNS
Tool output returns to you as a message starting with `[tool result]`. Act on it:
- If you need more info, call another tool.
- When the task is DONE, reply with plain text (NO JSON) summarizing what you did.
- NEVER claim you edited a file without actually calling `write`/`edit`.

# HOW TO EXPLORE A PROJECT (these tools exist, use them)
- `list_directory` with a path — see what is in a folder (subdirectories end with `/`).
- `glob` with a pattern like `**/*.py` — find files by name.
- `grep` with a regex and a path — find code by content.
- `read` with a path — open one file; pass offset/limit for long ones.
- `bash` for anything else the shell can do.
Never invent another tool name: only the tools in the catalog above exist.
"""


# Short reinforcement appended to the last user message every turn.
REMINDER_SUFFIX = (
    "\n\n---\n"
    "[SYSTEM: You are BeeAgent, an autonomous coding agent connected to this machine "
    "with full filesystem and shell access. You have the tools listed in your system "
    "prompt. To use one, output ONLY a JSON code block:\n"
    '```json\n{"tool": "tool_name", "args": {...}}\n```'
    "\nDo NOT say you lack file access — you do not lack it. "
    "Do NOT ask the user to paste file contents. Call the tools yourself.]"
)


class ContextManager:
    def __init__(self, max_tokens: int = 12000, model: str = "gpt-4"):
        self.max_tokens = max_tokens
        self.model = model
        # Optional extra system-prompt section (installed skills).
        self.skills_section: str | None = None
        # How many messages the last build had to leave out.
        self.trimmed = 0

    # One big tool dump (a file read, a recursive glob) must not evict the
    # whole conversation, so oversized messages are clipped instead.
    MAX_MSG_TOKENS = 2000

    def build_messages(self, session_messages: list[dict], tool_schemas: list[dict]) -> list[dict]:
        from beeagent.core.parser import CommandParser

        parser = CommandParser()

        # Tool catalog as a compact, explicit contract.
        tool_prompt = parser.format_tool_prompt(tool_schemas)
        system_content = SYSTEM_PROMPT + "\n" + tool_prompt
        if self.skills_section:
            system_content += "\n\n" + self.skills_section

        messages = [{"role": "system", "content": system_content}]

        budget = self.max_tokens - count_tokens(system_content, self.model)
        kept, dropped = self._fit(session_messages, budget)
        self.trimmed = dropped
        if dropped:
            messages.append({
                "role": "user",
                "content": f"[…] {dropped} более ранних сообщений не влезли в контекст и опущены.",
            })
        messages.extend(kept)

        # Reinforce the tool contract on the last user message.
        for msg in reversed(messages):
            if msg.get("role") == "user":
                msg["content"] = msg.get("content", "") + REMINDER_SUFFIX
                break
        else:
            # Nothing but the system prompt survived — keep the contract there.
            messages[0]["content"] += REMINDER_SUFFIX

        return messages

    def _fit(self, session_messages: list[dict], budget: int) -> tuple[list[dict], int]:
        """Newest-first window over the history, clipped to fit the budget.

        Messages that do not fit are skipped one by one rather than ending the
        scan, and the request being worked on is always pulled back in — losing
        it is what made the model answer as if the chat had just started.
        """
        task_index = next(
            (i for i, m in enumerate(session_messages) if m.get("role") == "user"), None
        )
        kept: list[tuple[int, dict]] = []
        used = 0
        dropped = 0

        for index in range(len(session_messages) - 1, -1, -1):
            msg = self._clip(session_messages[index])
            cost = count_tokens(str(msg.get("content") or ""), self.model)
            if used + cost > budget:
                dropped += 1
                continue
            kept.insert(0, (index, msg))
            used += cost

        if task_index is not None and task_index not in [i for i, _ in kept]:
            task = self._clip(session_messages[task_index])
            cost = count_tokens(str(task.get("content") or ""), self.model)
            while kept and used + cost > budget:
                _, victim = kept.pop(0)
                used -= count_tokens(str(victim.get("content") or ""), self.model)
                dropped += 1
            kept.insert(0, (task_index, task))
            used += cost

        return [msg for _, msg in kept], dropped

    def _clip(self, msg: dict) -> dict:
        """Cut a message to MAX_MSG_TOKENS, keeping its head and tail."""
        content = str(msg.get("content") or "")
        limit = self.MAX_MSG_TOKENS * 4        # tokens -> chars, the usual estimate
        if len(content) <= limit:
            return msg
        head = content[: int(limit * 0.7)]
        tail = content[int(len(content) - limit * 0.3):]
        removed = len(content) - len(head) - len(tail)
        return {**msg, "content": f"{head}\n[… {removed} символов обрезано …]\n{tail}"}
