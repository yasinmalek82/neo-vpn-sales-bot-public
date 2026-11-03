from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, ORMExecuteState, Session, SessionTransaction

from bot.config import Settings


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


# --------------------------------------------------------------------------- #
# SAVEPOINT on SQLite
#
# The pysqlite/aiosqlite driver opens a transaction implicitly for DML but not
# for SAVEPOINT, so a ``session.begin_nested()`` block runs in autocommit and
# its writes are committed the moment the savepoint is released — an outer
# ``rollback()`` then cannot undo them.
#
# Measured on this stack: the same write was undone by a rollback without a
# savepoint and survived one with it. Money code uses savepoints to collapse a
# concurrent duplicate onto the winner's row, so this silently turns a
# rollback-safety test green for the wrong reason and hides a whole class of
# bugs from every local run.
#
# The fix is SQLAlchemy's documented one: stop the driver managing
# transactions and emit BEGIN ourselves. It is registered against the Engine
# class rather than one instance, because test fixtures build their own
# engines and would otherwise keep the broken behaviour. PostgreSQL needs none
# of this, so both listeners no-op on any other dialect.
# --------------------------------------------------------------------------- #


@event.listens_for(Engine, "connect")
def _sqlite_disable_driver_transactions(dbapi_connection: Any, record: Any) -> None:
    if record.info.get("dialect_name") == "sqlite" or _is_sqlite_connection(dbapi_connection):
        dbapi_connection.isolation_level = None
        _sqlite_concurrency_pragmas(dbapi_connection)


def _sqlite_concurrency_pragmas(dbapi_connection: Any) -> None:
    """Let a reader and a writer coexist, and wait instead of failing.

    One process runs both the Telegram handlers and the scheduler, and every
    update writes ``users.last_seen_at``. Under SQLite's default rollback
    journal a reader blocks a writer outright, so a broadcast or report job
    landing mid-request raised «database is locked» and the customer saw an
    error — seen during the owner's live smoke, on an ordinary tap.

    WAL lets readers run while one writer commits. ``busy_timeout`` covers the
    remaining writer-vs-writer overlap by waiting five seconds rather than
    giving up instantly. ``synchronous=NORMAL`` is the documented companion to
    WAL: durable across a process crash, which is the failure this code
    actually has to survive.

    PostgreSQL runs production and needs none of it; this is SQLite-only by
    construction.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


@event.listens_for(Engine, "begin")
def _sqlite_emit_begin(conn: Any) -> None:
    if conn.engine.dialect.name == "sqlite":
        conn.exec_driver_sql("BEGIN")


# --------------------------------------------------------------------------- #
# A read snapshot must not be held across the network
#
# The ``BEGIN`` above is deferred, so a session's first SELECT pins a WAL read
# snapshot. If another connection commits before this session's first write,
# SQLite refuses the write at once (SQLITE_BUSY_SNAPSHOT, «database is
# locked»; ``busy_timeout`` does not apply). The scheduler commits from this
# same process every few seconds, so «read, await Telegram or the panel,
# write» in one transaction fails whenever the await is slow. Seen live three
# times: ``users.last_seen_at``, the report-group attach (19 s of Telegram
# calls, eight topics lost), and a paid order after the panel call.
#
# ``end_read_transaction`` is the cure: call it before the await. It is safe
# on a money path because it releases a transaction only when that transaction
# has written nothing. One that has written holds SQLite's write lock (no
# other commit can land, so it cannot go stale), and committing it early would
# make durable what its caller may still roll back, a wallet retry debit for
# one. Whatever is written after the await must be decided on rows read after
# it: the snapshot error was also, by accident, what refused to overwrite a
# row someone else had changed meanwhile.
#
# ``BEGIN IMMEDIATE`` for every transaction is not the answer: a handler
# waiting on the network would then block every writer for seconds.
# --------------------------------------------------------------------------- #

_WROTE = "transaction_has_written"


@event.listens_for(Session, "after_flush")
def _note_flush(session: Session, _flush_context: Any) -> None:
    session.info[_WROTE] = True


@event.listens_for(Session, "do_orm_execute")
def _note_statement(state: ORMExecuteState) -> None:
    # Anything that is not a plain SELECT counts as a write: wrongly keeping a
    # transaction open is the old behaviour, wrongly committing one is not safe.
    if not state.is_select:
        state.session.info[_WROTE] = True


@event.listens_for(Session, "after_transaction_end")
def _forget_writes(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is None:
        session.info.pop(_WROTE, None)


async def end_read_transaction(session: AsyncSession) -> bool:
    """Release this session's read snapshot before a long await. True if it did.

    Does nothing, and returns False, when the transaction has written or has
    changes waiting to be flushed (see the block above), and for a session
    whose objects would expire on commit: an expired row is a lazy load, which
    an async caller cannot survive.
    """
    sync = session.sync_session
    if not sync.in_transaction():
        return True
    if sync.info.get(_WROTE) or sync.new or sync.dirty or sync.deleted or sync.expire_on_commit:
        return False
    await session.commit()
    return True


def _is_sqlite_connection(dbapi_connection: Any) -> bool:
    """aiosqlite wraps sqlite3, so match on the driver rather than the URL."""
    module = type(dbapi_connection).__module__ or ""
    return "sqlite" in module


def init_db(settings: Settings) -> None:
    global _engine, _session_factory
    _engine = create_async_engine(
        settings.database_url,
        echo=settings.debug,
        pool_pre_ping=True,
        pool_recycle=3600,
    )
    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _session_factory


async def close_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for FastAPI/aiogram to get a DB session."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
