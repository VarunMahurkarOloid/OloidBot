import cohere

from .base import BaseLLM
from .prompts import CHAT_SYSTEM_PROMPT


class CohereLLM(BaseLLM):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._client = cohere.AsyncClientV2(api_key=self.api_key)

    async def chat(self, messages: list[dict], system: str = "") -> str:
        resp = await self._client.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": system or CHAT_SYSTEM_PROMPT},
                *messages,
            ],
        )
        return resp.message.content[0].text
