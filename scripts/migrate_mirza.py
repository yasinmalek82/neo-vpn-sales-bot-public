#!/usr/bin/env python3
"""Offline preview by default; explicit reviewed apply into an explicit target.

This command does not grant operational authorization. Follow the migration
runbook and owner-approved scope; never point a rehearsal at a customer database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if __name__ == "__main__":
    # Shared model imports also import application Settings. An offline command
    # must not read a nearby production .env or require a real Telegram token.
    os.environ["NEO_DISABLE_DOTENV"] = "1"
    os.environ["BOT_TOKEN"] = "migration-no-telegram"

from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from api_client.factory import get_pasarguard_client  # noqa: E402
from bot.config import Settings  # noqa: E402
from database.base import end_read_transaction  # noqa: E402
from services.mirza_import import (  # noqa: E402
    MigrationError,
    MigrationPlan,
    MigrationPolicy,
    PanelBinding,
    apply_migration,
    plan_migration,
)
from services.mirza_reconcile import Reconciliation, reconcile_migration  # noqa: E402
from services.mirza_source import DumpError, read_mirza_dump  # noqa: E402
from services.subscription_identity import reviewed_subscription_matches  # noqa: E402


def _policy(path: Path) -> MigrationPolicy:
    data = json.loads(path.read_text())
    allowed = {
        "namespace",
        "source_unit",
        "archive_invoice_ids",
        "accept_quarantine",
        "panel_bindings",
    }
    if not isinstance(data, dict) or data.keys() - allowed:
        raise MigrationError("invalid_policy")
    bindings = tuple(PanelBinding(**row) for row in data.get("panel_bindings", []))
    return MigrationPolicy(
        namespace=data["namespace"],
        source_unit=data.get("source_unit"),
        archive_invoice_ids=frozenset(data.get("archive_invoice_ids", [])),
        accept_quarantine=frozenset(data.get("accept_quarantine", [])),
        panel_bindings=bindings,
    )


async def _apply(plan: MigrationPlan, target_url: str, confirmation: str) -> bool:
    engine = create_async_engine(target_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # Panel reads precede the financial transaction. Nothing here can create,
        # renew, reset, delete or change ownership on the VPN panel.
        if plan.policy.panel_bindings:
            settings = Settings(
                _env_file=None, BOT_TOKEN="migration-no-telegram", DATABASE_URL=target_url
            )
            async with factory() as session:
                client = await get_pasarguard_client(session, settings)
                await end_read_transaction(session)
                try:
                    for binding in plan.policy.panel_bindings:
                        live = await client.get_user(binding.panel_username)
                        if (
                            live.id != binding.panel_user_id
                            or live.username != binding.panel_username
                            or not binding.subscription_url
                            or not await reviewed_subscription_matches(
                                client, live, binding.subscription_url
                            )
                        ):
                            raise MigrationError("panel_snapshot_changed")
                finally:
                    await client.aclose()
        async with factory() as session:
            try:
                created = await apply_migration(session, plan, confirm_sha256=confirmation)
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
        return created
    finally:
        await engine.dispose()


async def _verify(plan: MigrationPlan, target_url: str, confirmation: str) -> Reconciliation:
    url = make_url(target_url)
    backend = url.get_backend_name()
    if backend == "sqlite":
        path = Path(url.database or "").resolve()
        if url.database in {None, "", ":memory:"} or not path.is_file():
            raise MigrationError("verify_target_missing")
        url = url.set(database=path.as_uri(), query={**url.query, "mode": "ro", "uri": "true"})
    elif backend != "postgresql":
        raise MigrationError("unsupported_verify_target")
    options = {"isolation_level": "REPEATABLE READ"} if backend == "postgresql" else {}
    engine = create_async_engine(url, **options)
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.exec_driver_sql(
                "SET TRANSACTION READ ONLY" if backend == "postgresql" else "PRAGMA query_only=ON"
            )
            async with AsyncSession(
                bind=connection, expire_on_commit=False, autoflush=False
            ) as session:
                return await reconcile_migration(session, plan, confirm_sha256=confirmation)
    finally:
        await engine.dispose()


def _run(args: argparse.Namespace) -> int:
    source = read_mirza_dump(args.source.read_bytes())
    plan = plan_migration(source, _policy(args.policy))
    if args.source_sha256 is not None and args.source_sha256 != source.sha256:
        raise MigrationError("source_hash_mismatch")
    created = None
    verified = None
    if args.apply or args.verify:
        if args.apply and args.output is not None:
            raise MigrationError("output_only_for_preview")
        if args.source_sha256 is None:
            raise MigrationError("source_hash_required")
        if args.confirm_plan_sha256 != plan.sha256:
            raise MigrationError("confirmation_mismatch")
        if plan.blockers:
            raise MigrationError("plan_blocked")
        if not args.target_url_env or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", args.target_url_env):
            raise MigrationError("explicit_target_required")
        target = os.environ.get(args.target_url_env)
        if not target:
            raise MigrationError("explicit_target_required")
        if args.apply:
            created = asyncio.run(_apply(plan, target, args.confirm_plan_sha256))
        else:
            verified = asyncio.run(_verify(plan, target, args.confirm_plan_sha256))
    if args.output is not None:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as out:
            json.dump(
                (
                    verified.document(private=True)
                    if verified is not None
                    else {"plan_sha256": plan.sha256, **plan.document()}
                ),
                out,
                ensure_ascii=False,
                indent=2,
            )
            out.write("\n")
    print(
        json.dumps(
            {
                "source_sha256": source.sha256,
                "plan_sha256": plan.sha256,
                "users": len(plan.users),
                "records": len(plan.records),
                "opening_total_toman": plan.opening_total_toman,
                "blocker_count": len(plan.blockers),
                "applied": created,
                **(verified.document() if verified is not None else {}),
            }
        )
    )
    return 0 if verified is None or verified.ok else 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, help="New private preview JSON file; never overwritten"
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument(
        "--verify", action="store_true", help="Read-only frozen-import reconciliation"
    )
    parser.add_argument("--source-sha256")
    parser.add_argument("--confirm-plan-sha256")
    parser.add_argument(
        "--target-url-env", help="Environment variable holding the explicit target URL"
    )
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except (MigrationError, DumpError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        # Driver/transport errors can include DSNs or credentials. Keep them out
        # of operator logs; the stage and exception type are enough to diagnose.
        print(f"migration_failed:{type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
