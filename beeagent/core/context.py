from beeagent.utils.tokens import count_tokens

SYSTEM_PROMPT = """You are BeeAgent, an AI coding assistant. You help users with software engineering tasks.

You have access to tools for reading files, writing code, running commands, and more.
Always think step by step. Use tools to gather information before making changes.
Be concise and direct in your responses."""

class ContextManager:
    def __init__(self, max_tokens: int = 8000, model: str = "gpt-4"):
        self.max_tokens = max_tokens
        self.model = model
        self.system_prompt = SYSTEM_PROMPT

    def build_messages(self, session_messages: list[dict], tool_schemas: list[dict]) -> list[dict]:
        messages = [{"role": "system", "content": self.system_prompt}]

        if tool_schemas:
            tools_text = "\nAvailable tools:\n"
            for t in tool_schemas:
                tools_text += f"- {t['name']}: {t['description']}\n"
            messages[0]["content"] += tools_text

        total_tokens = count_tokens(messages[0]["content"], self.model)
        added = []
        for msg in reversed(session_messages):
            msg_tokens = count_tokens(str(msg), self.model)
            if total_tokens + msg_tokens > self.max_tokens:
                break
            added.insert(0, msg)
            total_tokens += msg_tokens

        messages.extend(added)
        return messages

    def compact(self, messages: list[dict]) -> list[dict]:
        compacted = []
        tool_count = 0
        for msg in messages:
            if msg.get("role") == "tool":
                tool_count += 1
                if tool_count > 10:
                    continue
            compacted.append(msg)
        return compacted
