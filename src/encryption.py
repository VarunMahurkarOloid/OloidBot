"""
Fernet symmetric encryption for storing secrets (API keys, OAuth tokens).
In production: uses FERNET_KEY env var.
In dev: auto-generates an encryption key on first run, stored in data/encryption.key.
"""

import os

from cryptography.fernet import Fernet


def _load_or_create_key() -> bytes:
    from .config import settings

    key_path = os.path.join(settings.effective_data_dir, "encryption.key")

    # Priority 1: FERNET_KEY env var (recommended for production)
    if settings.fernet_key:
        key = settings.fernet_key.encode()
        # Also save to disk as backup so data survives if env var is ever lost
        try:
            os.makedirs(os.path.dirname(key_path), exist_ok=True)
            if not os.path.exists(key_path):
                with open(key_path, "wb") as f:
                    f.write(key)
        except Exception:
            pass
        return key

    # Priority 2: existing key file on persistent disk
    if os.path.exists(key_path):
        with open(key_path, "rb") as f:
            return f.read()

    # Priority 3: auto-generate new key (first run)
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
