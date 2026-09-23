from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import app.services.generate_file_dispatch_service as service
from app.services.generate_file_dispatch_service import (
    _build_sftp_target_lock_key,
    _build_row_buffer_payload,
    _build_recurring_file_name,
    _extract_row_runtime_payload,
    _append_internal_suffix,
    _append_session_suffix,
    _is_permission_like_error,
    _read_row_group_key,
    _safe_relpath,
    _sftp_path_exists,
    compute_next_run_at,
)


def test_compute_next_run_at_imediato_returns_now_window() -> None:
    before = datetime.now(timezone.utc)
    value = compute_next_run_at({"scheduling_run_mode": "imediato"})
    after = datetime.now(timezone.utc)
    assert before <= value <= after


def test_compute_next_run_at_agendado_with_timezone() -> None:
    value = compute_next_run_at(
        {
            "scheduling_run_mode": "agendado",
            "scheduling_date": "2026-05-10",
            "scheduling_time_agendado": "09:30",
            "scheduling_fuso_agandado": "sp_utc_3",
        }
    )
    assert value == datetime(2026, 5, 10, 12, 30, tzinfo=timezone.utc)


def test_compute_next_run_at_recurrence_5m() -> None:
    before = datetime.now(timezone.utc)
    value = compute_next_run_at(
        {
            "scheduling_run_mode": "recorrente",
            "recurrence": "5m",
        }
    )
    delta = value - before
    assert timedelta(minutes=4, seconds=50) <= delta <= timedelta(minutes=5, seconds=10)


def test_safe_relpath_rejects_parent_escape() -> None:
    with pytest.raises(ValueError):
        _safe_relpath("../etc/passwd")


def test_append_session_suffix_keeps_extension() -> None:
    assert _append_session_suffix("arquivo.csv", 42) == "arquivo-42.csv"


def test_append_internal_suffix_keeps_extension_with_four_digits() -> None:
    assert _append_internal_suffix("arquivo.csv", 1) == "arquivo_0001.csv"
    assert _append_internal_suffix("arquivo", 12) == "arquivo_0012"


def test_permission_like_error_detector() -> None:
    assert _is_permission_like_error(RuntimeError("Permission denied")) is True
    assert _is_permission_like_error(RuntimeError("Errno 13")) is True
    assert _is_permission_like_error(RuntimeError("Falha de conexão")) is False


def test_sftp_path_exists_uses_stat_and_preserves_unexpected_errors() -> None:
    class _FakeSftp:
        def stat(self, path: str) -> object:
            if path == "missing.csv":
                raise FileNotFoundError(2, "No such file")
            if path == "forbidden.csv":
                raise PermissionError(13, "Permission denied")
            return object()

    sftp = _FakeSftp()
    assert _sftp_path_exists(sftp, "existing.csv") is True
    assert _sftp_path_exists(sftp, "missing.csv") is False
    with pytest.raises(PermissionError):
        _sftp_path_exists(sftp, "forbidden.csv")


def test_sftp_target_lock_key_resolves_per_session_name_without_password() -> None:
    lock_key = _build_sftp_target_lock_key(
        destination_config={
            "sftp_host": "SFTP.EXAMPLE.COM",
            "sftp_port": 22,
            "sftp_user": "orch",
            "sftp_password": "do-not-leak",
            "path": "/Arquivos/monitoramento/upload",
            "file_name": "create_customer.csv",
        },
        format_config={"write_mode": "create_per_session"},
        session_id=109030,
    )

    assert lock_key.endswith("Arquivos/monitoramento/upload:create_customer-109030.csv")
    assert "do-not-leak" not in lock_key


@pytest.mark.asyncio
async def test_immediate_job_claims_one_row_without_job_wide_lock(monkeypatch) -> None:
    class _Settings:
        celery_generate_file_stale_processing_minutes = 5

    reclaim = AsyncMock(return_value=0)
    load_job = AsyncMock(
        return_value={
            "mode": "imediato",
            "destination_config": {},
            "format_config": {},
            "scheduling_config": {},
        }
    )
    pick_rows = AsyncMock(return_value=[])
    db_session = AsyncMock()

    monkeypatch.setattr(service, "get_settings", lambda: _Settings())
    monkeypatch.setattr(service, "_reclaim_stale_processing_rows", reclaim)
    monkeypatch.setattr(service, "_load_job", load_job)
    monkeypatch.setattr(service, "_pick_pending_rows", pick_rows)

    result = await service.process_generate_file_job(
        db_session,
        workspace_uuid="253148c7-a85f-42a3-bc8b-5ffd9d885efe",
        job_id="79850bc2-ba05-4376-86e0-465ddd6f1b14",
    )

    assert result["status"] == "no_rows"
    pick_rows.assert_awaited_once_with(
        db_session,
        workspace_uuid="253148c7-a85f-42a3-bc8b-5ffd9d885efe",
        job_id="79850bc2-ba05-4376-86e0-465ddd6f1b14",
        limit=1,
    )
    db_session.execute.assert_not_awaited()


def test_row_buffer_payload_wraps_row_and_destination_snapshot() -> None:
    payload = _build_row_buffer_payload(
        row_payload={"nome": "Ana", "telefone": "5511999999999"},
        destination_config={"file_name": "carga_ana.csv", "path": "upload"},
    )
    assert payload["__row"]["nome"] == "Ana"
    assert payload["__destination_config"]["file_name"] == "carga_ana.csv"


def test_extract_row_runtime_payload_supports_wrapped_and_legacy() -> None:
    default_destination = {"file_name": "default.csv", "path": "upload"}
    wrapped_payload = {
        "__row": {"nome": "Paulo", "telefone": "5511988887777"},
        "__destination_config": {"file_name": "carga_paulo.csv"},
    }
    row_payload, destination = _extract_row_runtime_payload(
        wrapped_payload,
        default_destination_config=default_destination,
    )
    assert row_payload["nome"] == "Paulo"
    assert destination["file_name"] == "carga_paulo.csv"
    assert destination["path"] == "upload"

    legacy_row_payload, legacy_destination = _extract_row_runtime_payload(
        {"nome": "Legado"},
        default_destination_config=default_destination,
    )
    assert legacy_row_payload["nome"] == "Legado"
    assert legacy_destination["file_name"] == "default.csv"


def test_read_row_group_key_prefers_carteira_variants() -> None:
    assert _read_row_group_key({"Carteira": "EmpreX"}) == "EmpreX"
    assert _read_row_group_key({"carteira": "DNC"}) == "DNC"
    assert _read_row_group_key({"CARTEIRA": "Medway"}) == "Medway"
    assert _read_row_group_key({"nome": "Sem carteira"}) == "geral"


def test_build_recurring_file_name_adds_group_and_timestamp() -> None:
    value = _build_recurring_file_name(
        base_file_name="acan.csv",
        group_key="Parcela Mais",
        reference_at=datetime(2026, 7, 21, 12, 34, 56, tzinfo=timezone.utc),
    )
    assert value == "acan_parcela_mais_20260721_093456.csv"
