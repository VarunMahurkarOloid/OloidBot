from google import genai
from google.genai import types

from .base import BaseLLM
from .prompts import CHAT_SYSTEM_PROMPT


class GeminiLLM(BaseLLM):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._client = genai.Client(api_key=self.api_key)

    async def chat(self, messages: list[dict], system: str = "") -> str:
        contents = [
            types.Content(
                role="user" if msg["role"] == "user" else "model",
                parts=[types.Part(text=msg["content"])],
            )
            for msg in messages
        ]
        config = types.GenerateContentConfig(
            system_instruction=system or CHAT_SYSTEM_PROMPT,
        )
        resp = await self._client.aio.models.generate_content(
            model=self.model,
            contents=contents,
            config=config,
        )
        return resp.text
