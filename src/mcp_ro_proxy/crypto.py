"""Encryption of stored upstream tokens.

AES-256-GCM with a key ring for rotation. Each ciphertext records the id of the key that
produced it, and is bound to its purpose and owner (for example the user's ``sub``) as
associated data, so a ciphertext copied onto another user's row does not decrypt.

Key ring format (env/Secret Manager): ``kid1:base64key,kid0:base64key``. The first key
encrypts; every listed key can decrypt. Generate one with ``mcp-ro-proxy generate-key``.
"""

from __future__ import annotations

import base64
import os
import re
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_VERSION = "v1"


class CryptoError(Exception):
    pass


def generate_key(kid: str | None = None) -> str:
    kid = kid or f"k{secrets.token_hex(4)}"
    if not _KID.match(kid):
        raise CryptoError("key id must be 1-32 characters of [A-Za-z0-9_-]")
    return f"{kid}:{base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256)).decode()}"


class TokenCipher:
    def __init__(self, keyring: str) -> None:
        keys: dict[str, AESGCM] = {}
        order: list[str] = []
        for entry in (e.strip() for e in keyring.split(",")):
            if not entry:
                continue
            kid, sep, b64 = entry.partition(":")
            if not sep or not _KID.match(kid):
                raise CryptoError("each key must look like 'kid:base64key'")
            try:
                raw = base64.urlsafe_b64decode(b64.encode())
            except ValueError as exc:
                raise CryptoError(f"key {kid} is not valid base64") from exc
            if len(raw) != 32:
                raise CryptoError(f"key {kid} must be 32 bytes (AES-256)")
            if kid in keys:
                raise CryptoError(f"duplicate key id {kid}")
            keys[kid] = AESGCM(raw)
            order.append(kid)
        if not order:
            raise CryptoError("no encryption keys configured")
        self._keys = keys
        self._primary = order[0]

    def encrypt(self, plaintext: str, *, context: str) -> str:
        nonce = os.urandom(12)
        aad = self._aad(self._primary, context)
        ct = self._keys[self._primary].encrypt(nonce, plaintext.encode("utf-8"), aad)
        body = base64.urlsafe_b64encode(nonce + ct).decode()
        return f"{_VERSION}.{self._primary}.{body}"

    def decrypt(self, token: str, *, context: str) -> str:
        try:
            version, kid, body = token.split(".", 2)
        except ValueError as exc:
            raise CryptoError("malformed ciphertext") from exc
        if version != _VERSION:
            raise CryptoError("unsupported ciphertext version")
        key = self._keys.get(kid)
        if key is None:
            raise CryptoError(f"ciphertext uses unknown key id {kid}")
        try:
            raw = base64.urlsafe_b64decode(body.encode())
            plaintext = key.decrypt(raw[:12], raw[12:], self._aad(kid, context))
        except (ValueError, InvalidTag) as exc:
            raise CryptoError("ciphertext failed authentication") from exc
        return plaintext.decode("utf-8")

    def needs_rotation(self, token: str) -> bool:
        parts = token.split(".", 2)
        return len(parts) != 3 or parts[1] != self._primary

    @staticmethod
    def _aad(kid: str, context: str) -> bytes:
        return f"{_VERSION}|{kid}|{context}".encode()
