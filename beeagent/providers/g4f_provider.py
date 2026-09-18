from typing import AsyncIterator
from .base import BaseProvider

class G4fProvider(BaseProvider):
    name = "g4f"
    models = [
        "gpt-4", "gpt-4o", "gpt-3.5-turbo",
        "claude-3.5-sonnet", "claude-3-haiku",
        "gemini-pro", "llama-3.1-70b",
        "deepseek-chat", "qwen-72b",
    ]
    
    async def chat(self, messages: list[dict], model: str = "gpt-4", stream: bool = False) -> str:
        from g4f.client import AsyncClient
        client = AsyncClient()
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            stream=stream,
        )
        return response.choices[0].message.content
    
    async def chat_stream(self, messages: list[dict], model: str = "gpt-4") -> AsyncIterator[str]:
        from g4f.client import AsyncClient
        client = AsyncClient()
        stream = await client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
