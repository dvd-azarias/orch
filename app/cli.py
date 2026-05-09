from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from app.core.database import get_session_factory
from app.services.migration_service import migrate_all_active_workspaces, migrate_workspace


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orch-cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("migrate-all", help="Aplica migrations do orch em todos os workspaces ativos.")

    migrate_workspace_parser = subparsers.add_parser(
        "migrate-workspace",
        help="Aplica migrations do orch em um workspace específico.",
    )
    migrate_workspace_parser.add_argument("workspace_uuid", help="UUID do workspace alvo.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "migrate-all":
        return asyncio.run(_run_migrate_all())
    if args.command == "migrate-workspace":
        return asyncio.run(_run_migrate_workspace(args.workspace_uuid))

    parser.error("Comando inválido.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
