"""
Per-user data store with encryption. Persists:
- Admin config (Google OAuth credentials, default LLM)
- Per-user data (Gmail tokens, LLM overrides, notification prefs, poll state)
All sensitive fields are encrypted at rest with Fernet.

Backend: Supabase PostgreSQL when SUPABASE_URL + SUPABASE_KEY are set,
otherwise falls back to local JSON file (for local dev).
"""

import json
import logging
import os
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

from .encryption import decrypt, encrypt


# ---------------------------------------------------------------------------
# JSON file-based store (local dev fallback)
# ---------------------------------------------------------------------------

def _get_store_path() -> str:
    from .config import settings
    return os.path.join(settings.effective_data_dir, "store.json")


class _JsonUserStore:
    """Original JSON-file backend, kept for local dev without Supabase."""

    def __init__(self, path: str | None = None):
        if path is None:
            path = _get_store_path()
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self._load()

    # ── admin config ──

    def set_admin_google(self, client_id: str, client_secret: str):
        with self._lock:
            admin = self._data.setdefault("__admin__", {})
            admin["google_client_id"] = encrypt(client_id)
            admin["google_client_secret"] = encrypt(client_secret)
            self._save()

    def get_admin_google(self) -> dict[str, str]:
        admin = self._data.get("__admin__", {})
        id_enc = admin.get("google_client_id", "")
        secret_enc = admin.get("google_client_secret", "")
        return {
            "client_id": decrypt(id_enc) if id_enc else "",
            "client_secret": decrypt(secret_enc) if secret_enc else "",
        }

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

    # ── per-user: memory channel ──

    def set_user_memory_channel_id(self, slack_user_id: str, channel_id: str):
        with self._lock:
            user = self._data.setdefault(slack_user_id, {})
            user["memory_channel_id"] = channel_id
            self._save()

    def get_user_memory_channel_id(self, slack_user_id: str) -> str:
        return self.get_user(slack_user_id).get("memory_channel_id", "")

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

    # ── per-user: poll state ──

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

    def save_oauth_state(self, state: str, slack_user_id: str, code_verifier: str = None):
        with self._lock:
            pending = self._data.setdefault("__oauth_pending__", {})
            pending[state] = {"user_id": slack_user_id, "code_verifier": code_verifier}
            self._save()

    def pop_oauth_state(self, state: str) -> Optional[dict]:
        with self._lock:
            pending = self._data.get("__oauth_pending__", {})
            data = pending.pop(state, None)
            self._save()
            if isinstance(data, str):
                return {"user_id": data, "code_verifier": None}
            return data

    # ── persistence ──

    def _load(self):
        if os.path.exists(self._path):
            with open(self._path, "r") as f:
                self._data = json.load(f)

    def _save(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp_path = self._path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp_path, self._path)


# ---------------------------------------------------------------------------
# Supabase-backed store (production)
# ---------------------------------------------------------------------------

class _SupabaseUserStore:
    """PostgreSQL backend via Supabase. Same public API as _JsonUserStore."""

    def __init__(self, url: str, key: str):
        import httpx
        from supabase import create_client, ClientOptions

        # Configure connection pool for 50+ concurrent requests.
        # Default httpx keeps only 20 keepalive connections; bump to 60.
        transport = httpx.HTTPTransport(
            retries=1,
            limits=httpx.Limits(
                max_connections=80,
                max_keepalive_connections=60,
                keepalive_expiry=30,
            ),
        )
        options = ClientOptions(
            postgrest_client_timeout=30,
        )
        self._sb = create_client(url, key, options)
        # Override the underlying httpx transport for the postgrest client
        self._sb.postgrest.session.transport = transport
        self._ensure_admin_row()

    def _ensure_admin_row(self):
        """Make sure the singleton admin_config row exists."""
        row = self._sb.table("admin_config").select("id").eq("id", 1).execute()
        if not row.data:
            self._sb.table("admin_config").insert({"id": 1}).execute()

    def _ensure_user(self, slack_user_id: str):
        """Upsert a user row if it doesn't exist."""
        self._sb.table("users").upsert(
            {"slack_user_id": slack_user_id},
            on_conflict="slack_user_id",
        ).execute()

    # ── admin config ──

    def set_admin_google(self, client_id: str, client_secret: str):
        self._sb.table("admin_config").update({
            "google_client_id": encrypt(client_id),
            "google_client_secret": encrypt(client_secret),
        }).eq("id", 1).execute()

    def get_admin_google(self) -> dict[str, str]:
        row = self._sb.table("admin_config").select(
            "google_client_id, google_client_secret"
        ).eq("id", 1).execute()
        if not row.data:
            return {"client_id": "", "client_secret": ""}
        r = row.data[0]
        id_enc = r.get("google_client_id", "") or ""
        secret_enc = r.get("google_client_secret", "") or ""
        return {
            "client_id": decrypt(id_enc) if id_enc else "",
            "client_secret": decrypt(secret_enc) if secret_enc else "",
        }

    def is_google_configured(self) -> bool:
        g = self.get_admin_google()
        return bool(g["client_id"] and g["client_secret"])

    def set_admin_llm(self, provider: str, api_key: str, model: str):
        self._sb.table("admin_config").update({
            "llm_provider": provider,
            "llm_api_key": encrypt(api_key) if api_key else "",
            "llm_model": model.strip("[](){}<>\"'"),
        }).eq("id", 1).execute()

    def get_admin_llm(self) -> dict[str, str]:
        row = self._sb.table("admin_config").select(
            "llm_provider, llm_api_key, llm_model"
        ).eq("id", 1).execute()
        if not row.data:
            return {"provider": "", "api_key": "", "model": ""}
        r = row.data[0]
        api_key_enc = r.get("llm_api_key", "") or ""
        return {
            "provider": r.get("llm_provider", "") or "",
            "api_key": decrypt(api_key_enc) if api_key_enc else "",
            "model": r.get("llm_model", "") or "",
        }

    def is_llm_configured(self) -> bool:
        llm = self.get_admin_llm()
        return bool(llm["provider"] and llm["api_key"])

    def set_memory_channel_id(self, channel_id: str):
        self._sb.table("admin_config").update({
            "memory_channel_id": channel_id,
        }).eq("id", 1).execute()

    def get_memory_channel_id(self) -> str:
        row = self._sb.table("admin_config").select(
            "memory_channel_id"
        ).eq("id", 1).execute()
        if not row.data:
            return ""
        return row.data[0].get("memory_channel_id", "") or ""

    # ── per-user: memory channel ──
    # Requires: ALTER TABLE users ADD COLUMN IF NOT EXISTS memory_channel_id TEXT;

    def set_user_memory_channel_id(self, slack_user_id: str, channel_id: str):
        self._ensure_user(slack_user_id)
        try:
            self._sb.table("users").update({
                "memory_channel_id": channel_id,
            }).eq("slack_user_id", slack_user_id).execute()
        except Exception:
            logger.warning("Could not persist memory_channel_id for %s (run migration)", slack_user_id)

    def get_user_memory_channel_id(self, slack_user_id: str) -> str:
        try:
            row = self._sb.table("users").select("memory_channel_id").eq(
                "slack_user_id", slack_user_id
            ).execute()
            if not row.data:
                return ""
            return row.data[0].get("memory_channel_id", "") or ""
        except Exception:
            return ""

    # ── per-user: Gmail token ──

    def get_user(self, slack_user_id: str) -> dict[str, Any]:
        row = self._sb.table("users").select("*").eq(
            "slack_user_id", slack_user_id
        ).execute()
        if not row.data:
            return {}
        return row.data[0]

    def is_gmail_connected(self, slack_user_id: str) -> bool:
        row = self._sb.table("users").select("gmail_token").eq(
            "slack_user_id", slack_user_id
        ).execute()
        if not row.data:
            return False
        return bool(row.data[0].get("gmail_token"))

    def save_gmail_token(self, slack_user_id: str, token_json: dict):
        self._ensure_user(slack_user_id)
        self._sb.table("users").update({
            "gmail_token": encrypt(json.dumps(token_json)),
        }).eq("slack_user_id", slack_user_id).execute()

    def get_gmail_token(self, slack_user_id: str) -> Optional[dict]:
        row = self._sb.table("users").select("gmail_token").eq(
            "slack_user_id", slack_user_id
        ).execute()
        if not row.data:
            return None
        enc = row.data[0].get("gmail_token") or ""
        if not enc:
            return None
        return json.loads(decrypt(enc))

    def update_gmail_token(self, slack_user_id: str, token_json: dict):
        self.save_gmail_token(slack_user_id, token_json)

    def disconnect_gmail(self, slack_user_id: str):
        self._sb.table("users").update({
            "gmail_token": "",
            "last_poll_ts": 0.0,
        }).eq("slack_user_id", slack_user_id).execute()

    def all_connected_users(self) -> list[str]:
        rows = self._sb.table("users").select("slack_user_id").neq(
            "gmail_token", ""
        ).execute()
        return [r["slack_user_id"] for r in rows.data]

    # ── per-user: LLM override ──

    def set_llm_config(
        self, slack_user_id: str,
        provider: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self._ensure_user(slack_user_id)
        updates: dict[str, Any] = {}
        if provider is not None:
            updates["llm_provider"] = provider
        if api_key is not None:
            updates["llm_api_key"] = encrypt(api_key) if api_key else ""
        if model is not None:
            updates["llm_model"] = model.strip("[](){}<>\"'")
        if updates:
            self._sb.table("users").update(updates).eq(
                "slack_user_id", slack_user_id
            ).execute()

    def get_llm_config(self, slack_user_id: str) -> dict[str, str]:
        row = self._sb.table("users").select(
            "llm_provider, llm_api_key, llm_model"
        ).eq("slack_user_id", slack_user_id).execute()
        if not row.data:
            return {"provider": "", "api_key": "", "model": ""}
        r = row.data[0]
        api_key_enc = r.get("llm_api_key", "") or ""
        return {
            "provider": r.get("llm_provider", "") or "",
            "api_key": decrypt(api_key_enc) if api_key_enc else "",
            "model": r.get("llm_model", "") or "",
        }

    # ── per-user: notification preferences ──

    def set_notifications(self, slack_user_id: str, enabled: bool):
        self._ensure_user(slack_user_id)
        self._sb.table("users").update({
            "notifications": enabled,
        }).eq("slack_user_id", slack_user_id).execute()

    def get_notifications(self, slack_user_id: str) -> bool:
        row = self._sb.table("users").select("notifications").eq(
            "slack_user_id", slack_user_id
        ).execute()
        if not row.data:
            return True
        val = row.data[0].get("notifications")
        return val if val is not None else True

    # ── per-user: poll state ──

    def get_last_poll_ts(self, slack_user_id: str) -> float:
        row = self._sb.table("users").select("last_poll_ts").eq(
            "slack_user_id", slack_user_id
        ).execute()
        if not row.data:
            return 0.0
        return float(row.data[0].get("last_poll_ts") or 0.0)

    def set_last_poll_ts(self, slack_user_id: str, ts: float):
        self._ensure_user(slack_user_id)
        self._sb.table("users").update({
            "last_poll_ts": ts,
        }).eq("slack_user_id", slack_user_id).execute()

    # ── per-user: reminders ──

    def add_reminder(self, user_id: str, text: str, fire_at_ts: float) -> str:
        self._ensure_user(user_id)
        reminder_id = f"r_{int(time.time() * 1000)}"
        self._sb.table("reminders").insert({
            "id": reminder_id,
            "slack_user_id": user_id,
            "text": text,
            "fire_at": fire_at_ts,
        }).execute()
        return reminder_id

    def get_all_reminders(self) -> list[dict]:
        rows = self._sb.table("reminders").select("*").execute()
        return [
            {
                "user_id": r["slack_user_id"],
                "id": r["id"],
                "text": r["text"],
                "fire_at": r["fire_at"],
            }
            for r in rows.data
        ]

    def get_user_reminders(self, user_id: str) -> list[dict]:
        rows = self._sb.table("reminders").select(
            "id, text, fire_at"
        ).eq("slack_user_id", user_id).execute()
        return [
            {"id": r["id"], "text": r["text"], "fire_at": r["fire_at"]}
            for r in rows.data
        ]

    def remove_reminder(self, user_id: str, reminder_id: str):
        self._sb.table("reminders").delete().eq(
            "id", reminder_id
        ).eq("slack_user_id", user_id).execute()

    # ── OAuth state management ──

    def save_oauth_state(self, state: str, slack_user_id: str, code_verifier: str = None):
        self._sb.table("oauth_pending").insert({
            "state": state,
            "slack_user_id": slack_user_id,
            "code_verifier": encrypt(code_verifier) if code_verifier else None,
        }).execute()

    def pop_oauth_state(self, state: str) -> Optional[dict]:
        row = self._sb.table("oauth_pending").select("*").eq(
            "state", state
        ).execute()
        if not row.data:
            return None
        r = row.data[0]
        # Delete the consumed state
        self._sb.table("oauth_pending").delete().eq("state", state).execute()
        verifier_enc = r.get("code_verifier") or ""
        return {
            "user_id": r["slack_user_id"],
            "code_verifier": decrypt(verifier_enc) if verifier_enc else None,
        }


# ---------------------------------------------------------------------------
# Singleton — pick backend based on env vars
# ---------------------------------------------------------------------------

def _create_store():
    from .config import settings
    if settings.supabase_url and settings.supabase_key:
        return _SupabaseUserStore(settings.supabase_url, settings.supabase_key)
    return _JsonUserStore()


user_store = _create_store()
