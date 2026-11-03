"""Nonblocking provisioning ownership, retained through the caller's commit.

PostgreSQL uses a session advisory lock on a dedicated connection, with no open
transaction during panel IO. SQLite uses an OS file lock (local databases only).
Both release automatically on process/connection death, with no expired lease
allowing a second sender to enter a still-running panel operation.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

_LOCKS = "neo_provision_locks"


class ProvisionBusy(Exception):
    """Another transaction is already applying this order."""


class _Ownership:
    def __init__(self) -> None:
        self.depth = 1
        self.file: BinaryIO | None = None
        self.connection: AsyncConnection | None = None
        self.key = 0

    def release(self) -> None:
        """SQLAlchemy's session events run within its async greenlet bridge."""
        if self.file is not None:
            self.file.close()
            self.file = None
        if self.connection is not None:
            conn = self.connection.sync_connection
            self.connection = None
            try:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": self.key})
                conn.commit()
            except BaseException:
                # A connection with an uncertain advisory lock must never return
                # to the pool: transaction rollback alone does not unlock it.
                conn.invalidate()
                raise
            finally:
                conn.close()


@event.listens_for(Session, "after_transaction_end")
def _release_finished_ownership(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is not None:
        return
    locks: dict[int, _Ownership] = session.info.get(_LOCKS, {})
    for order_id, ownership in list(locks.items()):
        if ownership.depth == 0:
            try:
                ownership.release()
            finally:
                locks.pop(order_id, None)


async def _acquire(session: AsyncSession, order_id: int) -> _Ownership:
    owner = _Ownership()
    engine = session.bind
    if not isinstance(engine, AsyncEngine):
        raise RuntimeError("provisioning_requires_engine_bound_session")
    if engine.dialect.name == "postgresql":
        owner.key = int.from_bytes(
            hashlib.blake2b(f"neo:provision:{order_id}".encode(), digest_size=8).digest(),
            "big",
            signed=True,
        )
        conn = await engine.connect()
        owner.connection = conn
        try:
            acquired = await conn.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": owner.key}
            )
            await conn.commit()
        except BaseException:
            # Invalidate rather than pool a connection after uncertain acquisition.
            if owner.connection is not None:
                await conn.invalidate()
                await conn.close()
            raise
        if not acquired:
            await conn.close()
            owner.connection = None
            raise ProvisionBusy
        return owner
    if engine.dialect.name != "sqlite":
        raise RuntimeError("unsupported_provisioning_lock_backend")
    database = engine.url.database
    identity = (
        str(Path(database).resolve())
        if database and database != ":memory:"
        else f"memory:{id(engine)}"
    )
    directory = Path(tempfile.gettempdir()) / f"neo-provision-locks-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    name = hashlib.sha256(f"{identity}:{order_id}".encode()).hexdigest()
    descriptor = os.open(directory / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    owner.file = os.fdopen(descriptor, "r+b")
    try:
        fcntl.flock(owner.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        owner.file.close()
        raise ProvisionBusy from exc
    return owner


@asynccontextmanager
async def provision_ownership(session: AsyncSession, order_id: int) -> AsyncIterator[None]:
    locks: dict[int, _Ownership] = session.info.setdefault(_LOCKS, {})
    existing = locks.get(order_id)
    if existing is not None:
        # Repeated calls in the same caller transaction share its ownership.
        existing.depth += 1
        owner = existing
    else:
        owner = await _acquire(session, order_id)
        locks[order_id] = owner
    try:
        yield
    finally:
        owner.depth -= 1
        if owner.depth == 0 and not session.in_transaction():
            await session.run_sync(lambda _session: owner.release())
            locks.pop(order_id, None)
