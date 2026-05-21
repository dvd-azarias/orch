from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.generate_file_dispatch_service import (
    _append_internal_suffix,
    _append_session_suffix,
    _is_permission_like_error,
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
