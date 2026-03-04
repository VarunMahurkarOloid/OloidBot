from abc import ABC, abstractmethod
from typing import Any

from .prompts import SUMMARIZE_SYSTEM_PROMPT


class BaseLLM(ABC):
    def __init__(self, api_key: str, model: str, **kwargs: Any):
        self.api_key = api_key
        self.model = model

    async def summarize(self, emails_text: str) -> str:
        """Summarize emails. Uses chat() internally — no need to override."""
        return await self.chat(
            [{"role": "user", "content": f"Summarize these emails:\n\n{emails_text}"}],
            system=SUMMARIZE_SYSTEM_PROMPT,
        )

    @abstractmethod
    async def chat(self, messages: list[dict], system: str = "") -> str:
        """Send a conversation and return the assistant's reply."""
