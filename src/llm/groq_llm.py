from groq import AsyncGroq

from .base import BaseLLM
from .prompts import CHAT_SYSTEM_PROMPT


class GroqLLM(BaseLLM):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._client = AsyncGroq(api_key=self.api_key)

    async def chat(self, messages: list[dict], system: str = "") -> str:
        resp = await self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system or CHAT_SYSTEM_PROMPT},
                *messages,
            ],
        )
        return resp.choices[0].message.content
