from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.generate_file_dispatch_service import (
    _build_row_buffer_payload,
    _build_recurring_file_name,
    _extract_row_runtime_payload,
    _append_internal_suffix,
    _append_session_suffix,
    _is_permission_like_error,
    _read_row_group_key,
    _safe_relpath,
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
