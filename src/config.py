import os

from pydantic import Field
from pydantic_settings import BaseSettings

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-haiku-20240307",
    "gemini": "gemini-1.5-flash",
    "groq": "llama3-70b-8192",
    "mistral": "mistral-large-latest",
    "cohere": "command-r",
    "ollama": "llama3.1",
}


def get_default_model(provider: str) -> str:
    return DEFAULT_MODELS.get(provider, "")


class Settings(BaseSettings):
    slack_bot_token: str = Field(..., description="Slack bot token (xoxb-)")
    slack_app_token: str = Field(..., description="Slack app token (xapp-)")
    slack_user_token: str = Field("", description="Slack user token (xoxp-) for reading user messages")
    base_url: str = Field("", description="Public base URL of the bot (e.g. https://oloidbot.onrender.com)")
    oauth_redirect_uri: str = Field("", description="OAuth redirect URI (auto-derived from BASE_URL if empty)")
    oauth_server_port: int = Field(8080)
    ollama_base_url: str = Field("http://localhost:11434")
    poll_interval_minutes: int = Field(5)
    data_dir: str = Field("", description="Override data directory path")
    fernet_key: str = Field("", description="Fernet encryption key (set in production)")
    supabase_url: str = Field("", description="Supabase project URL")
    supabase_key: str = Field("", description="Supabase anon/service key")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def effective_base_url(self) -> str:
        """Public base URL. Uses BASE_URL env var, or falls back to localhost for dev."""
        if self.base_url:
            return self.base_url.rstrip("/")
        return f"http://localhost:{self.effective_port}"

    @property
    def effective_oauth_redirect_uri(self) -> str:
        """OAuth redirect URI. Uses OAUTH_REDIRECT_URI if set, else derives from base URL."""
        if self.oauth_redirect_uri:
            return self.oauth_redirect_uri
        return f"{self.effective_base_url}/oauth/callback"

    @property
    def effective_data_dir(self) -> str:
        if self.data_dir:
            return self.data_dir
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

    @property
    def effective_port(self) -> int:
        """Use PORT env var (set by Render) if available, else oauth_server_port."""
        return int(os.environ.get("PORT", self.oauth_server_port))


settings = Settings()
