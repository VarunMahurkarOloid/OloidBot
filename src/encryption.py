"""
Fernet symmetric encryption for storing secrets (API keys, OAuth tokens).
In production (Supabase): uses FERNET_KEY env var only — no disk needed.
In dev (JSON file store): auto-generates an encryption key on first run, stored in data/encryption.key.
"""

import os

from cryptography.fernet import Fernet


def _load_or_create_key() -> bytes:
    from .config import settings

    # Priority 1: FERNET_KEY env var (required for production / Supabase)
    if settings.fernet_key:
        key = settings.fernet_key.encode()
        # When using Supabase, no disk backup needed.
        # For local dev with JSON store, still save to disk as backup.
        if not (settings.supabase_url and settings.supabase_key):
            try:
                key_path = os.path.join(settings.effective_data_dir, "encryption.key")
                os.makedirs(os.path.dirname(key_path), exist_ok=True)
                if not os.path.exists(key_path):
                    with open(key_path, "wb") as f:
                        f.write(key)
            except Exception:
                pass
        return key

    # Priority 2: existing key file on persistent disk (local dev)
    key_path = os.path.join(settings.effective_data_dir, "encryption.key")
    if os.path.exists(key_path):
        with open(key_path, "rb") as f:
            return f.read()

    # Priority 3: auto-generate new key (first run, local dev only)
    key = Fernet.generate_key()
    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    with open(key_path, "wb") as f:
        f.write(key)
    return key


_key = _load_or_create_key()
_fernet = Fernet(_key)


def encrypt(plaintext: str) -> str:
    """Encrypt a string, return base64-encoded ciphertext."""
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a base64-encoded ciphertext back to string."""
    return _fernet.decrypt(ciphertext.encode()).decode()
