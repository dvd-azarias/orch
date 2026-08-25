from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from collections.abc import Sequence

from sqlalchemy import text

from app.core.database import get_session_factory
from app.core.config import get_settings
from app.services.migration_service import migrate_all_active_workspaces, migrate_workspace
from app.core.workspace import workspace_schema_from_uuid
from app.services.billing_snapshot_service import (
    backfill_billing_snapshot_outbox_batch,
    count_missing_billing_snapshots,
    rearm_exhausted_billing_snapshots,
)
from app.services.workspace_service import list_completed_workspaces


async def _run_migrate_all() -> int:
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        results = await migrate_all_active_workspaces(db_session)

    total_applied = sum(len(item.applied_versions) for item in results)
    total_skipped = sum(len(item.skipped_versions) for item in results)

    print(f"total_workspaces={len(results)}")
    print(f"summary_applied={total_applied}")
    print(f"summary_skipped={total_skipped}")
    for item in results:
        print(
            f"{item.workspace_uuid} schema={item.schema} "
            f"applied={item.applied_versions} skipped={item.skipped_versions}"
        )
    return 0


async def _run_migrate_workspace(workspace_uuid: str) -> int:
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        result = await migrate_workspace(
            db_session,
            workspace_uuid=workspace_uuid,
        )

    print(
        f"{result.workspace_uuid} schema={result.schema} "
        f"applied={result.applied_versions} skipped={result.skipped_versions}"
    )
    return 0


async def _run_billing_backfill(
    period: str,
    batch_size: int,
    max_batches: int,
    dry_run: bool,
    rearm_exhausted: bool,
) -> int:
    period_start = datetime.strptime(period, "%Y-%m").replace(tzinfo=timezone.utc)
    if period_start.month == 12:
        period_end = datetime(period_start.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        period_end = datetime(period_start.year, period_start.month + 1, 1, tzinfo=timezone.utc)
    session_factory = get_session_factory()
    total = 0
    total_rearmed = 0
    async with session_factory() as db_session:
        workspaces = await list_completed_workspaces(db_session)
        await db_session.commit()
        for workspace in workspaces:
            workspace_uuid = str(workspace["workspace_uuid"])
            schema = workspace_schema_from_uuid(workspace_uuid).replace('"', '""')
            async with db_session.begin():
                await db_session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                missing = await count_missing_billing_snapshots(db_session, period_start=period_start, period_end=period_end)
            if not missing:
                continue
            if dry_run:
                print(f"workspace={workspace_uuid} missing={missing}")
                total += missing
                continue
            if rearm_exhausted:
                async with db_session.begin():
                    await db_session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                    rearmed = await rearm_exhausted_billing_snapshots(
                        db_session,
                        period_start=period_start,
                        period_end=period_end,
                        max_attempts=get_settings().orch_billing_publish_max_attempts,
                    )
                total_rearmed += rearmed
            inserted = 0
            for _ in range(max(1, max_batches)):
                async with db_session.begin():
                    await db_session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                    created = await backfill_billing_snapshot_outbox_batch(db_session, workspace_uuid=workspace_uuid, period_start=period_start, period_end=period_end, batch_size=batch_size)
                inserted += created
                if created < batch_size:
                    break
            total += inserted
            print(
                f"workspace={workspace_uuid} inserted={inserted} "
                f"rearmed={rearmed if rearm_exhausted else 0} "
                f"remaining_estimate={max(0, missing-inserted)}"
            )
    print(f"total={'missing' if dry_run else 'inserted'}={total}")
    if rearm_exhausted and not dry_run:
        print(f"total_rearmed={total_rearmed}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orch-cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("migrate-all", help="Aplica migrations do orch em todos os workspaces ativos.")

    migrate_workspace_parser = subparsers.add_parser(
        "migrate-workspace",
        help="Aplica migrations do orch em um workspace específico.",
    )
    migrate_workspace_parser.add_argument("workspace_uuid", help="UUID do workspace alvo.")
    billing_backfill_parser = subparsers.add_parser("billing-backfill", help="Cria snapshots retroativos idempotentes na outbox.")
    billing_backfill_parser.add_argument("--period", required=True, help="Mês UTC no formato YYYY-MM.")
    billing_backfill_parser.add_argument("--batch-size", type=int, default=500)
    billing_backfill_parser.add_argument("--max-batches", type=int, default=1)
    billing_backfill_parser.add_argument("--dry-run", action="store_true")
    billing_backfill_parser.add_argument(
        "--rearm-exhausted",
        action="store_true",
        help="Rearma snapshots pendentes ainda não publicadas antes da inserção.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "migrate-all":
        return asyncio.run(_run_migrate_all())
    if args.command == "migrate-workspace":
        return asyncio.run(_run_migrate_workspace(args.workspace_uuid))
    if args.command == "billing-backfill":
        return asyncio.run(
            _run_billing_backfill(
                args.period,
                args.batch_size,
                args.max_batches,
                args.dry_run,
                args.rearm_exhausted,
            )
        )

    parser.error("Comando inválido.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
