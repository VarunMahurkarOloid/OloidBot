"""
Per-user data store with encryption. Persists:
- Admin config (Google OAuth credentials, default LLM)
- Per-user data (Gmail tokens, LLM overrides, notification prefs, poll state)
All sensitive fields are encrypted at rest with Fernet.
"""

import json
import os
import threading
import time
from typing import Any, Optional

from .encryption import decrypt, encrypt


def _get_store_path() -> str:
    from .config import settings
    return os.path.join(settings.effective_data_dir, "store.json")


# Fields that get encrypted before saving
_ENCRYPTED_FIELDS = {"gmail_token", "llm_api_key", "google_client_secret"}


class UserStore:
    def __init__(self, path: str | None = None):
        if path is None:
            path = _get_store_path()
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self._load()

    # ── admin config (Google OAuth, default LLM) ──

    def set_admin_google(self, client_id: str, client_secret: str):
        with self._lock:
            admin = self._data.setdefault("__admin__", {})
            admin["google_client_id"] = client_id
            admin["google_client_secret"] = encrypt(client_secret)
            self._save()

    def get_admin_google(self) -> dict[str, str]:
        admin = self._data.get("__admin__", {})
        client_id = admin.get("google_client_id", "")
        secret_enc = admin.get("google_client_secret", "")
        secret = decrypt(secret_enc) if secret_enc else ""
        return {"client_id": client_id, "client_secret": secret}

    def is_google_configured(self) -> bool:
        g = self.get_admin_google()
        return bool(g["client_id"] and g["client_secret"])

    def set_admin_llm(self, provider: str, api_key: str, model: str):
        with self._lock:
            admin = self._data.setdefault("__admin__", {})
            admin["llm_provider"] = provider
            admin["llm_api_key"] = encrypt(api_key) if api_key else ""
            admin["llm_model"] = model.strip("[](){}<>\"'")
            self._save()

    def get_admin_llm(self) -> dict[str, str]:
        admin = self._data.get("__admin__", {})
        api_key_enc = admin.get("llm_api_key", "")
        return {
            "provider": admin.get("llm_provider", ""),
            "api_key": decrypt(api_key_enc) if api_key_enc else "",
            "model": admin.get("llm_model", ""),
        }

    def is_llm_configured(self) -> bool:
        llm = self.get_admin_llm()
        return bool(llm["provider"] and llm["api_key"])

    def set_memory_channel_id(self, channel_id: str):
        with self._lock:
            admin = self._data.setdefault("__admin__", {})
            admin["memory_channel_id"] = channel_id
            self._save()

    def get_memory_channel_id(self) -> str:
        return self._data.get("__admin__", {}).get("memory_channel_id", "")

    # ── per-user: Gmail token ──

    def get_user(self, slack_user_id: str) -> dict[str, Any]:
        return self._data.get(slack_user_id, {})

    def is_gmail_connected(self, slack_user_id: str) -> bool:
        return bool(self.get_user(slack_user_id).get("gmail_token"))

    def save_gmail_token(self, slack_user_id: str, token_json: dict):
        with self._lock:
            user = self._data.setdefault(slack_user_id, {})
            user["gmail_token"] = encrypt(json.dumps(token_json))
            self._save()

    def get_gmail_token(self, slack_user_id: str) -> Optional[dict]:
        enc = self.get_user(slack_user_id).get("gmail_token")
        if not enc:
            return None
        return json.loads(decrypt(enc))

    def update_gmail_token(self, slack_user_id: str, token_json: dict):
        self.save_gmail_token(slack_user_id, token_json)

    def disconnect_gmail(self, slack_user_id: str):
        with self._lock:
            user = self._data.get(slack_user_id, {})
            user.pop("gmail_token", None)
            user.pop("last_poll_ts", None)
            self._save()

    def all_connected_users(self) -> list[str]:
        return [
            uid for uid, data in self._data.items()
            if not uid.startswith("__") and isinstance(data, dict) and data.get("gmail_token")
        ]

    # ── per-user: LLM override ──

    def set_llm_config(
        self, slack_user_id: str,
        provider: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        with self._lock:
            user = self._data.setdefault(slack_user_id, {})
            if provider is not None:
                user["llm_provider"] = provider
            if api_key is not None:
                user["llm_api_key"] = encrypt(api_key) if api_key else ""
            if model is not None:
                # Strip brackets/quotes that users accidentally include
                user["llm_model"] = model.strip("[](){}<>\"'")
            self._save()

    def get_llm_config(self, slack_user_id: str) -> dict[str, str]:
        user = self.get_user(slack_user_id)
        api_key_enc = user.get("llm_api_key", "")
        return {
            "provider": user.get("llm_provider", ""),
            "api_key": decrypt(api_key_enc) if api_key_enc else "",
            "model": user.get("llm_model", ""),
        }

    # ── per-user: notification preferences ──

    def set_notifications(self, slack_user_id: str, enabled: bool):
        with self._lock:
            user = self._data.setdefault(slack_user_id, {})
            user["notifications"] = enabled
            self._save()

    def get_notifications(self, slack_user_id: str) -> bool:
        return self.get_user(slack_user_id).get("notifications", True)

    # ── per-user: poll state (for real-time email monitoring) ──

    def get_last_poll_ts(self, slack_user_id: str) -> float:
        return self.get_user(slack_user_id).get("last_poll_ts", 0.0)

    def set_last_poll_ts(self, slack_user_id: str, ts: float):
        with self._lock:
            user = self._data.setdefault(slack_user_id, {})
            user["last_poll_ts"] = ts
            self._save()

    # ── per-user: reminders ──

    def add_reminder(self, user_id: str, text: str, fire_at_ts: float) -> str:
        with self._lock:
            user = self._data.setdefault(user_id, {})
            reminders = user.setdefault("reminders", [])
            reminder_id = f"r_{int(time.time() * 1000)}"
            reminders.append({"id": reminder_id, "text": text, "fire_at": fire_at_ts})
            self._save()
            return reminder_id

    def get_all_reminders(self) -> list[dict]:
        results = []
        for uid, data in self._data.items():
            if uid.startswith("__") or not isinstance(data, dict):
                continue
            for r in data.get("reminders", []):
                results.append({"user_id": uid, **r})
        return results

    def get_user_reminders(self, user_id: str) -> list[dict]:
        user = self._data.get(user_id, {})
        return list(user.get("reminders", []))

    def remove_reminder(self, user_id: str, reminder_id: str):
        with self._lock:
            user = self._data.get(user_id, {})
            reminders = user.get("reminders", [])
            user["reminders"] = [r for r in reminders if r["id"] != reminder_id]
            self._save()

    # ── OAuth state management ──

    def save_oauth_state(self, state: str, slack_user_id: str):
        with self._lock:
            pending = self._data.setdefault("__oauth_pending__", {})
            pending[state] = slack_user_id
            self._save()

    def pop_oauth_state(self, state: str) -> Optional[str]:
        with self._lock:
            pending = self._data.get("__oauth_pending__", {})
            user_id = pending.pop(state, None)
            self._save()
            return user_id

    # ── persistence ──

    def _load(self):
        if os.path.exists(self._path):
            with open(self._path, "r") as f:
                self._data = json.load(f)

    def _save(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2)


# Singleton
user_store = UserStore()
