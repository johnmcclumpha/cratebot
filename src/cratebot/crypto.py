"""Symmetric encryption for tokens at rest."""

from __future__ import annotations

from cryptography.fernet import Fernet


class TokenCipher:
    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError(
                "TOKEN_ENCRYPTION_KEY is not set. Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; "
                'print(Fernet.generate_key().decode())"'
            )
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode()).decode()
