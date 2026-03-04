from typing import Any

from .base import BaseLLM


class LLMFactory:
    _providers: dict[str, type[BaseLLM]] = {}

    @classmethod
    def register(cls, name: str, provider_cls: type[BaseLLM]):
        cls._providers[name.lower()] = provider_cls

    @classmethod
    def create(cls, provider: str, api_key: str, model: str, **kwargs: Any) -> BaseLLM:
        # Lazy-import all providers on first call
        if not cls._providers:
            cls._load_providers()

        key = provider.lower()
        if key not in cls._providers:
            available = ", ".join(sorted(cls._providers.keys()))
            raise ValueError(
                f"Unknown LLM provider '{provider}'. Available: {available}"
            )
        return cls._providers[key](api_key=api_key, model=model, **kwargs)

    @classmethod
    def _load_providers(cls):
        from .openai_llm import OpenAILLM
        from .anthropic_llm import AnthropicLLM
        from .gemini_llm import GeminiLLM
        from .groq_llm import GroqLLM
        from .mistral_llm import MistralLLM
        from .cohere_llm import CohereLLM
        from .ollama_llm import OllamaLLM

        cls.register("openai", OpenAILLM)
        cls.register("anthropic", AnthropicLLM)
        cls.register("gemini", GeminiLLM)
        cls.register("groq", GroqLLM)
        cls.register("mistral", MistralLLM)
        cls.register("cohere", CohereLLM)
        cls.register("ollama", OllamaLLM)

    @classmethod
    def available_providers(cls) -> list[str]:
        if not cls._providers:
            cls._load_providers()
        return sorted(cls._providers.keys())
