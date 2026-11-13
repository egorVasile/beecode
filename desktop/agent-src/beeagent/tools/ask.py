"""Ask the user a question mid-task and wait for their answer.

Without this tool the model has no way to request human input.  It either
guesses, stalls, or does something the user didn't want — and the user
watches, unable to redirect until the turn ends.
"""
from .base import BaseTool, ToolResult


class AskTool(BaseTool):
    name = "ask"
    description = (
        "Ask the user a question mid-task and wait for the answer. "
        "Yes/no questions get faster replies."
    )
    parameters = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "Your question to the user.  Keep it concise and "
                               "offer clear options.",
            },
        },
        "required": ["question"],
    }
    # Set by the agent loop before execute(): fires when the tool is called so
    # the UI can prompt the user.  Returns the user's answer as a string.
    on_ask = None

    def execute(self, question: str) -> ToolResult:
        if callable(self.on_ask):
            return self.on_ask(question)
        return ToolResult(
            output=f"[ASK] {question}\n(User input not available in this mode)",
            error=True,
        )

    def is_safe(self) -> bool:
        return True
