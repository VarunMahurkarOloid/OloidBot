import requests

from .base import BaseLLM
from .prompts import CHAT_SYSTEM_PROMPT


class OllamaLLM(BaseLLM):
    def __init__(self, base_url: str = "http://localhost:11434", **kwargs):
        super().__init__(**kwargs)
        self.base_url = base_url.rstrip("/")

    async def chat(self, messages: list[dict], system: str = "") -> str:
        resp = requests.post(
            f"{self.base_url}/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system or CHAT_SYSTEM_PROMPT},
                    *messages,
                ],
                "stream": False,
            },
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]
