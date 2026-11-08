import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from mcp_guard.storage import build_storage

OLD, NEW = Fernet.generate_key().decode(), Fernet.generate_key().decode()


def _on_disk(directory: Path) -> str:
    return "".join(p.read_text() for p in directory.rglob("*") if p.is_file())


async def test_values_are_encrypted_at_rest(tmp_path: Path) -> None:
    store = build_storage(f"file://{tmp_path}", [NEW], "unused")
    await store.put("1001", {"refresh_token": "1//very-secret"}, collection="people")
    assert await store.get("1001", collection="people") == {"refresh_token": "1//very-secret"}
    assert "very-secret" not in _on_disk(tmp_path)


async def test_encryption_key_rotation(tmp_path: Path) -> None:
    url = f"file://{tmp_path}"
    await build_storage(url, [OLD], "unused").put("1001", {"v": 1}, collection="people")
    # A new key goes first; the old one still decrypts what was written with it.
    rotated = build_storage(url, [NEW, OLD], "unused")
    assert await rotated.get("1001", collection="people") == {"v": 1}
    await rotated.put("1002", {"v": 2}, collection="people")
    # Once the old key is dropped, what only it could read is gone (that person signs in again),
    # rather than an error on every request.
    new_only = build_storage(url, [NEW], "unused")
    assert await new_only.get("1001", collection="people") is None
    assert await new_only.get("1002", collection="people") == {"v": 2}


async def test_default_key_is_derived_from_the_secret(tmp_path: Path) -> None:
    url = f"file://{tmp_path}"
    await build_storage(url, [], "client-secret-1").put("k", {"v": 1})
    assert await build_storage(url, [], "client-secret-1").get("k") == {"v": 1}
    assert await build_storage(url, [], "client-secret-2").get("k") is None


@pytest.mark.skipif(not os.environ.get("GUARD_TEST_REDIS_URL"), reason="needs GUARD_TEST_REDIS_URL")
async def test_redis_is_shared_between_replicas() -> None:
    url = os.environ["GUARD_TEST_REDIS_URL"]
    first, second = build_storage(url, [NEW], "unused"), build_storage(url, [NEW], "unused")
    await first.put("1001", {"refresh_token": "1//x"}, collection="test_people", ttl=60)
    assert await second.get("1001", collection="test_people") == {"refresh_token": "1//x"}
    await second.delete("1001", collection="test_people")
    assert await first.get("1001", collection="test_people") is None
