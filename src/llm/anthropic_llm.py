import anthropic

from .base import BaseLLM
from .prompts import CHAT_SYSTEM_PROMPT


class AnthropicLLM(BaseLLM):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._client = anthropic.AsyncAnthropic(api_key=self.api_key)

    async def chat(self, messages: list[dict], system: str = "") -> str:
        resp = await self._client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=system or CHAT_SYSTEM_PROMPT,
            messages=messages,
        )
        return resp.content[0].text
