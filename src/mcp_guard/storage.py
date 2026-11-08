"""Where sign-in state is kept: client registrations, each person's Google tokens and personal
keys. One encrypted key-value store, shared by FastMCP's OAuth proxy and the personal keys."""

from pathlib import Path

from cryptography.fernet import Fernet, MultiFernet
from fastmcp import settings as fastmcp_settings
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.stores.memory import MemoryStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper


def _file_store(directory: Path) -> FileTreeStore:
    directory.mkdir(parents=True, exist_ok=True)
    return FileTreeStore(
        data_directory=directory,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(directory),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(directory),
    )


def build_storage(
    url: str | None, encryption_keys: list[str], fallback_secret: str
) -> AsyncKeyValue:
    """The store for `url` (redis://, rediss://, file://, memory://; default: files under
    FASTMCP_HOME), with every value encrypted. `encryption_keys` are Fernet keys, the first
    encrypting; without them one key is derived from `fallback_secret`."""
    store: AsyncKeyValue
    if url is None:
        store = _file_store(fastmcp_settings.home / "mcp-guard")
    elif url.startswith(("redis://", "rediss://")):
        # Imported here: the Redis client is only needed when it's used.
        from key_value.aio.stores.redis import RedisStore  # noqa: PLC0415

        store = RedisStore(url=url)
    elif url.startswith("file://"):
        store = _file_store(Path(url.removeprefix("file://")))
    else:
        store = MemoryStore()
    keys = [k.encode() for k in encryption_keys] or [
        derive_jwt_key(high_entropy_material=fallback_secret, salt="mcp-guard-storage")
    ]
    return FernetEncryptionWrapper(
        key_value=store,
        fernet=MultiFernet([Fernet(k) for k in keys]),
        # A value written under a key no longer configured reads as missing (that person signs
        # in again) instead of failing every request.
        raise_on_decryption_error=False,
    )
