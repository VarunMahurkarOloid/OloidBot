"""
Fernet symmetric encryption for storing secrets (API keys, OAuth tokens).
In production: uses FERNET_KEY env var.
In dev: auto-generates an encryption key on first run, stored in data/encryption.key.
"""

import os

from cryptography.fernet import Fernet


def _load_or_create_key() -> bytes:
    from .config import settings

    # Production: use env var
    if settings.fernet_key:
        return settings.fernet_key.encode()

    # Dev: auto-generate and store locally
    key_path = os.path.join(settings.effective_data_dir, "encryption.key")
    if os.path.exists(key_path):
        with open(key_path, "rb") as f:
            return f.read()
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
