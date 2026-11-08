"""Persistent state: users with their encrypted upstream refresh tokens, and personal keys.

``Store`` is the interface the rest of the code depends on. ``SqlStore`` implements it
with SQLAlchemy Core, so the same code runs on SQLite (local development, single
instance) and on PostgreSQL (for example Cloud SQL in production) by changing
``storage.database_url``. Refresh tokens are encrypted with ``TokenCipher`` before they
reach the database; personal keys are stored only as sha256 hashes.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import anyio.to_thread
from sqlalchemy import (
    Column,
    Engine,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection

from mcp_ro_proxy.crypto import TokenCipher

KEY_PREFIX = "mrp_"
_TOUCH_INTERVAL_SECONDS = 300


@dataclass(frozen=True)
class UserGrant:
    sub: str
    email: str
    hd: str
    refresh_token: str | None
    scopes: frozenset[str]
    updated_at: int


@dataclass(frozen=True)
class ApiKeyRecord:
    id: str
    user_sub: str
    label: str
    hint: str
    created_at: int
    expires_at: int | None
    last_used_at: int | None
    revoked_at: int | None

    def is_active(self, now: int) -> bool:
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > now)


@dataclass(frozen=True)
class KeyOwner:
    key: ApiKeyRecord
    sub: str
    email: str


class TooManyKeysError(Exception):
    pass


class Store(Protocol):
    async def save_user(
        self, *, sub: str, email: str, hd: str, refresh_token: str | None, scopes: frozenset[str]
    ) -> None: ...

    async def get_user(self, sub: str) -> UserGrant | None: ...

    async def delete_user(self, sub: str) -> UserGrant | None:
        """Delete a user, their stored token and all their keys; return what was deleted."""
        ...

    async def create_api_key(
        self, *, sub: str, label: str, ttl_seconds: int | None, max_active: int
    ) -> tuple[ApiKeyRecord, str]: ...

    async def authenticate_key(self, raw_key: str) -> KeyOwner | None: ...

    async def list_keys(self, sub: str) -> Sequence[ApiKeyRecord]: ...

    async def revoke_key(self, sub: str, key_id: str) -> bool: ...

    async def revoke_all_keys(self, sub: str) -> int: ...

    async def aclose(self) -> None: ...


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def new_raw_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


_metadata = MetaData()

users = Table(
    "users",
    _metadata,
    Column("sub", String(255), primary_key=True),
    Column("email", String(320), nullable=False),
    Column("hd", String(255), nullable=False),
    Column("refresh_token_enc", Text, nullable=True),
    Column("scopes", Text, nullable=False),
    Column("created_at", Integer, nullable=False),
    Column("updated_at", Integer, nullable=False),
)

api_keys = Table(
    "api_keys",
    _metadata,
    Column("id", String(32), primary_key=True),
    Column("user_sub", String(255), ForeignKey("users.sub"), nullable=False),
    Column("key_hash", String(64), nullable=False, unique=True),
    Column("label", String(100), nullable=False),
    Column("hint", String(32), nullable=False),
    Column("created_at", Integer, nullable=False),
    Column("expires_at", Integer, nullable=True),
    Column("last_used_at", Integer, nullable=True),
    Column("revoked_at", Integer, nullable=True),
    Index("ix_api_keys_user_sub", "user_sub"),
)


def _key_record(row: Any) -> ApiKeyRecord:
    return ApiKeyRecord(
        id=row.id,
        user_sub=row.user_sub,
        label=row.label,
        hint=row.hint,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


class SqlStore:
    def __init__(
        self,
        database_url: str,
        cipher: TokenCipher,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        connect_args: dict[str, Any] = {}
        if database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        self._engine: Engine = create_engine(
            database_url, connect_args=connect_args, pool_pre_ping=True
        )
        self._cipher = cipher
        self._clock = clock
        _metadata.create_all(self._engine)

    def _now(self) -> int:
        return int(self._clock())

    async def _run[T](self, fn: Callable[[Connection], T]) -> T:
        def work() -> T:
            with self._engine.begin() as conn:
                return fn(conn)

        return await anyio.to_thread.run_sync(work)

    @staticmethod
    def _context(sub: str) -> str:
        return f"refresh_token|{sub}"

    async def save_user(
        self, *, sub: str, email: str, hd: str, refresh_token: str | None, scopes: frozenset[str]
    ) -> None:
        now = self._now()
        enc = (
            self._cipher.encrypt(refresh_token, context=self._context(sub))
            if refresh_token
            else None
        )
        # A sign-in that returns no refresh token keeps the previously stored one.
        values: dict[str, Any] = {
            "email": email,
            "hd": hd,
            "scopes": " ".join(sorted(scopes)),
            "updated_at": now,
        }
        if enc is not None:
            values["refresh_token_enc"] = enc

        def fn(conn: Connection) -> None:
            exists = conn.execute(select(users.c.sub).where(users.c.sub == sub)).first()
            if exists:
                conn.execute(update(users).where(users.c.sub == sub).values(**values))
            else:
                conn.execute(
                    insert(users).values(
                        {"sub": sub, "created_at": now, "refresh_token_enc": enc, **values}
                    )
                )

        await self._run(fn)

    def _grant(self, row: Any) -> UserGrant:
        token = (
            self._cipher.decrypt(row.refresh_token_enc, context=self._context(row.sub))
            if row.refresh_token_enc
            else None
        )
        return UserGrant(
            sub=row.sub,
            email=row.email,
            hd=row.hd,
            refresh_token=token,
            scopes=frozenset(row.scopes.split()),
            updated_at=row.updated_at,
        )

    async def get_user(self, sub: str) -> UserGrant | None:
        row = await self._run(
            lambda conn: conn.execute(select(users).where(users.c.sub == sub)).first()
        )
        return self._grant(row) if row is not None else None

    async def delete_user(self, sub: str) -> UserGrant | None:
        def fn(conn: Connection) -> Any:
            row = conn.execute(select(users).where(users.c.sub == sub)).first()
            conn.execute(delete(api_keys).where(api_keys.c.user_sub == sub))
            conn.execute(delete(users).where(users.c.sub == sub))
            return row

        row = await self._run(fn)
        return self._grant(row) if row is not None else None

    async def create_api_key(
        self, *, sub: str, label: str, ttl_seconds: int | None, max_active: int
    ) -> tuple[ApiKeyRecord, str]:
        now = self._now()
        raw = new_raw_key()
        record = ApiKeyRecord(
            id=secrets.token_hex(8),
            user_sub=sub,
            label=label.strip()[:100] or "personal key",
            hint=raw[: len(KEY_PREFIX) + 6],
            created_at=now,
            expires_at=now + ttl_seconds if ttl_seconds else None,
            last_used_at=None,
            revoked_at=None,
        )

        def fn(conn: Connection) -> None:
            active = conn.execute(
                select(func.count())
                .select_from(api_keys)
                .where(
                    api_keys.c.user_sub == sub,
                    api_keys.c.revoked_at.is_(None),
                    (api_keys.c.expires_at.is_(None)) | (api_keys.c.expires_at > now),
                )
            ).scalar_one()
            if active >= max_active:
                raise TooManyKeysError(f"at most {max_active} active keys per user")
            conn.execute(
                insert(api_keys).values(
                    id=record.id,
                    user_sub=sub,
                    key_hash=hash_key(raw),
                    label=record.label,
                    hint=record.hint,
                    created_at=now,
                    expires_at=record.expires_at,
                    last_used_at=None,
                    revoked_at=None,
                )
            )

        await self._run(fn)
        return record, raw

    async def authenticate_key(self, raw_key: str) -> KeyOwner | None:
        if not raw_key.startswith(KEY_PREFIX) or len(raw_key) > 128:
            return None
        digest = hash_key(raw_key)
        now = self._now()

        def fn(conn: Connection) -> KeyOwner | None:
            row = conn.execute(
                select(api_keys, users.c.email)
                .join(users, users.c.sub == api_keys.c.user_sub)
                .where(api_keys.c.key_hash == digest)
            ).first()
            # The lookup is by hash, so this comparison is not secret-dependent; it guards
            # against a database that compares case-insensitively.
            if row is None or not hmac.compare_digest(row.key_hash, digest):
                return None
            record = _key_record(row)
            if not record.is_active(now):
                return None
            if record.last_used_at is None or now - record.last_used_at >= _TOUCH_INTERVAL_SECONDS:
                conn.execute(
                    update(api_keys).where(api_keys.c.id == record.id).values(last_used_at=now)
                )
            return KeyOwner(key=record, sub=record.user_sub, email=row.email)

        return await self._run(fn)

    async def list_keys(self, sub: str) -> Sequence[ApiKeyRecord]:
        rows = await self._run(
            lambda conn: conn.execute(
                select(api_keys)
                .where(api_keys.c.user_sub == sub)
                .order_by(api_keys.c.created_at.desc(), api_keys.c.id)
            ).all()
        )
        return [_key_record(r) for r in rows]

    async def revoke_key(self, sub: str, key_id: str) -> bool:
        now = self._now()
        result = await self._run(
            lambda conn: (
                conn.execute(
                    update(api_keys)
                    .where(
                        api_keys.c.id == key_id,
                        api_keys.c.user_sub == sub,
                        api_keys.c.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                ).rowcount
            )
        )
        return bool(result)

    async def revoke_all_keys(self, sub: str) -> int:
        now = self._now()
        return await self._run(
            lambda conn: (
                conn.execute(
                    update(api_keys)
                    .where(api_keys.c.user_sub == sub, api_keys.c.revoked_at.is_(None))
                    .values(revoked_at=now)
                ).rowcount
            )
        )

    async def aclose(self) -> None:
        await anyio.to_thread.run_sync(self._engine.dispose)
