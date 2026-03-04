import google.generativeai as genai

from .base import BaseLLM
from .prompts import CHAT_SYSTEM_PROMPT


class GeminiLLM(BaseLLM):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        genai.configure(api_key=self.api_key)

    async def chat(self, messages: list[dict], system: str = "") -> str:
        model = genai.GenerativeModel(self.model, system_instruction=system or CHAT_SYSTEM_PROMPT)
        history = []
        last_content = ""
        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            if msg == messages[-1]:
                last_content = msg["content"]
            else:
                history.append({"role": role, "parts": [msg["content"]]})

        chat = model.start_chat(history=history)
        resp = await chat.send_message_async(last_content)
        return resp.text
