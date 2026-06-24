from __future__ import annotations

import csv
import hashlib
import io
import json
import posixpath
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.workspace import normalize_workspace_uuid, workspace_schema_from_uuid

JOB_TABLE = "orch_generate_file_job"
ROW_BUFFER_TABLE = "orch_generate_file_row_buffer"


def _tz_offset_hours(tz_id: str) -> int:
    mapping = {
        "sp_utc_3": -3,
        "bl_utc_3": -3,
        "fo_utc_3": -3,
        "mn_utc_4": -4,
        "utc": 0,
    }
    return mapping.get(str(tz_id or "utc").strip().lower(), 0)


def _parse_hhmm(raw: Any) -> tuple[int, int]:
    text_value = str(raw or "").strip()
    if not text_value:
        return 0, 0
    parts = text_value.split(":")
    try:
        hh = max(0, min(23, int(parts[0])))
        mm = max(0, min(59, int(parts[1]) if len(parts) > 1 else 0))
        return hh, mm
    except Exception:
        return 0, 0


def _parse_date(raw: Any) -> datetime | None:
    text_value = str(raw or "").strip()
    if not text_value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text_value, fmt)
        except Exception:
            continue
    return None


def compute_next_run_at(config: dict[str, Any]) -> datetime:
    mode = str(config.get("scheduling_run_mode") or "imediato").strip().lower()
    now = datetime.now(timezone.utc)
    if mode == "imediato":
        return now

    if mode == "agendado":
        date_part = _parse_date(config.get("scheduling_date")) or now
        hh, mm = _parse_hhmm(config.get("scheduling_time_agendado"))
        offset = _tz_offset_hours(str(config.get("scheduling_fuso_agandado") or "utc"))
        local_dt = datetime(date_part.year, date_part.month, date_part.day, hh, mm)
        utc_dt = local_dt - timedelta(hours=offset)
        return utc_dt.replace(tzinfo=timezone.utc)

    recurrence = str(config.get("recurrence") or "").strip().lower()
    if recurrence == "5m":
        return now + timedelta(minutes=5)
    if recurrence == "15m":
        return now + timedelta(minutes=15)
    if recurrence == "30m":
        return now + timedelta(minutes=30)
    if recurrence == "hora_a_hora":
        return now + timedelta(hours=1)
    if recurrence == "diario_hora_marcada":
        hh, mm = _parse_hhmm(config.get("scheduling_time"))
        offset = _tz_offset_hours(str(config.get("scheduling_fuso_recorrente") or "utc"))
        local_now = now + timedelta(hours=offset)
        local_target = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if local_target <= local_now:
            local_target = local_target + timedelta(days=1)
        utc_target = local_target - timedelta(hours=offset)
        return utc_target.replace(tzinfo=timezone.utc)
    return now


def _safe_relpath(value: str) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    raw = raw.lstrip("/")
    normalized = posixpath.normpath(raw)
    if normalized in {"", "."}:
        return ""
    if normalized.startswith("..") or "/../" in f"/{normalized}/":
        raise ValueError("destination_path inválido.")
    return normalized


def _append_session_suffix(file_name: str, session_id: int) -> str:
    token = str(session_id).strip()
    if "." not in file_name:
        return f"{file_name}-{token}"
    stem, suffix = file_name.rsplit(".", 1)
    return f"{stem}-{token}.{suffix}"


def _append_internal_suffix(file_name: str, sequence: int) -> str:
    suffix_token = f"_{int(sequence):04d}"
    if "." not in file_name:
        return f"{file_name}{suffix_token}"
    stem, ext = file_name.rsplit(".", 1)
    return f"{stem}{suffix_token}.{ext}"


def _is_permission_like_error(exc: Exception) -> bool:
    message = str(exc or "").lower()
    return (
        "permission denied" in message
        or "permissionerror" in message
        or "errno 13" in message
        or "access denied" in message
    )


def _build_row_buffer_payload(
    *,
    row_payload: dict[str, Any],
    destination_config: dict[str, Any],
) -> dict[str, Any]:
    return {
        "__row": row_payload,
        "__destination_config": destination_config,
    }


def _extract_row_runtime_payload(
    payload_jsonb: Any,
    *,
    default_destination_config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(payload_jsonb, dict):
        wrapped_row = payload_jsonb.get("__row")
        wrapped_destination = payload_jsonb.get("__destination_config")
        if isinstance(wrapped_row, dict):
            destination_config = dict(default_destination_config)
            if isinstance(wrapped_destination, dict):
                destination_config.update(wrapped_destination)
            return wrapped_row, destination_config
        return payload_jsonb, dict(default_destination_config)
    return {}, dict(default_destination_config)


async def upsert_job_and_buffer_row(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    flow_id: str | None,
    component_ref_id: str,
    session_id: int,
    config: dict[str, Any],
    row_payload: dict[str, Any],
) -> dict[str, Any]:
    safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
    schema = workspace_schema_from_uuid(safe_workspace_uuid).replace('"', '""')
    mode = str(config.get("scheduling_run_mode") or "imediato").strip().lower()
    next_run_at = compute_next_run_at(config)

    destination_config = {
        "path": config.get("destination_path"),
        "file_name": config.get("file_name"),
        "sftp_host": config.get("sftp_host"),
        "sftp_port": config.get("sftp_port"),
        "sftp_user": config.get("sftp_user"),
        "sftp_password": config.get("sftp_password"),
    }
    format_config = {
        "format_type": config.get("format_type"),
        "encoding": config.get("encoding"),
        "write_mode": config.get("write_mode"),
        "include_header": config.get("include_header"),
        "delimiter": config.get("delimiter"),
        "line_break": config.get("line_break"),
        "compression": config.get("compression"),
    }
    scheduling_config = {
        "scheduling_run_mode": config.get("scheduling_run_mode"),
        "scheduling_date": config.get("scheduling_date"),
        "scheduling_time_agendado": config.get("scheduling_time_agendado"),
        "scheduling_fuso_agandado": config.get("scheduling_fuso_agandado"),
        "recurrence": config.get("recurrence"),
        "scheduling_fuso_recorrente": config.get("scheduling_fuso_recorrente"),
        "scheduling_time": config.get("scheduling_time"),
    }

    await db_session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    job_result = await db_session.execute(
        text(
            f"""
            INSERT INTO {JOB_TABLE} (
                flow_id, component_ref_id, destination_type, destination_config, format_config,
                scheduling_config, mode, next_run_at, updated_at
            ) VALUES (
                CAST(NULLIF(:flow_id, '') AS uuid),
                :component_ref_id,
                :destination_type,
                CAST(:destination_config AS jsonb),
                CAST(:format_config AS jsonb),
                CAST(:scheduling_config AS jsonb),
                :mode,
                :next_run_at,
                NOW()
            )
            ON CONFLICT (flow_id, component_ref_id) DO UPDATE SET
                destination_type = EXCLUDED.destination_type,
                destination_config = EXCLUDED.destination_config,
                format_config = EXCLUDED.format_config,
                scheduling_config = EXCLUDED.scheduling_config,
                mode = EXCLUDED.mode,
                next_run_at = CASE
                    WHEN {JOB_TABLE}.next_run_at IS NULL THEN EXCLUDED.next_run_at
                    WHEN LOWER(COALESCE(EXCLUDED.mode, '')) = 'imediato'
                        THEN LEAST({JOB_TABLE}.next_run_at, NOW())
                    ELSE {JOB_TABLE}.next_run_at
                END,
                active = TRUE,
                updated_at = NOW()
            RETURNING id::text AS id
            """
        ),
        {
            "flow_id": flow_id or "",
            "component_ref_id": component_ref_id,
            "destination_type": config.get("destination_type"),
            "destination_config": json.dumps(destination_config, ensure_ascii=False),
            "format_config": json.dumps(format_config, ensure_ascii=False),
            "scheduling_config": json.dumps(scheduling_config, ensure_ascii=False),
            "mode": mode,
            "next_run_at": next_run_at,
        },
    )
    job_row = job_result.mappings().first()
    if job_row is None:
        raise RuntimeError("Falha ao registrar job generate_file.")
    job_id = str(job_row["id"])

    row_buffer_payload = _build_row_buffer_payload(
        row_payload=row_payload,
        destination_config=destination_config,
    )
    payload_text = json.dumps(row_buffer_payload, ensure_ascii=False, sort_keys=True)
    row_hash = hashlib.md5(payload_text.encode("utf-8")).hexdigest()
    row_result = await db_session.execute(
        text(
            f"""
            INSERT INTO {ROW_BUFFER_TABLE} (
                job_id, session_id, payload_jsonb, row_hash, status, updated_at
            ) VALUES (
                CAST(:job_id AS uuid),
                :session_id,
                CAST(:payload_jsonb AS jsonb),
                :row_hash,
                'pending',
                NOW()
            )
            ON CONFLICT (job_id, session_id, row_hash) DO NOTHING
            RETURNING id::text AS id
            """
        ),
        {
            "job_id": job_id,
            "session_id": str(session_id),
            "payload_jsonb": payload_text,
            "row_hash": row_hash,
        },
    )
    queued_row = row_result.mappings().first() is not None
    return {
        "job_id": job_id,
        "queued_row": queued_row,
        "mode": mode,
        "next_run_at": next_run_at.isoformat(),
    }


async def list_due_job_ids(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    limit: int = 200,
) -> list[str]:
    safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
    schema = workspace_schema_from_uuid(safe_workspace_uuid).replace('"', '""')
    await db_session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    result = await db_session.execute(
        text(
            f"""
            SELECT id::text AS id
              FROM {JOB_TABLE}
             WHERE active = TRUE
               AND next_run_at IS NOT NULL
               AND next_run_at <= NOW()
             ORDER BY next_run_at ASC
             LIMIT :limit
            """
        ),
        {"limit": max(1, min(limit, 1000))},
    )
    return [str(row["id"]) for row in result.mappings().all()]


async def _load_job(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    job_id: str,
) -> dict[str, Any] | None:
    safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
    schema = workspace_schema_from_uuid(safe_workspace_uuid).replace('"', '""')
    await db_session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    result = await db_session.execute(
        text(
            f"""
            SELECT
                id::text AS id,
                flow_id::text AS flow_id,
                component_ref_id,
                destination_type,
                destination_config,
                format_config,
                scheduling_config,
                mode
              FROM {JOB_TABLE}
             WHERE id = CAST(:job_id AS uuid)
            """
        ),
        {"job_id": job_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def _reclaim_stale_processing_rows(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    job_id: str,
    stale_minutes: int,
) -> int:
    safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
    schema = workspace_schema_from_uuid(safe_workspace_uuid).replace('"', '""')
    await db_session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    result = await db_session.execute(
        text(
            f"""
            UPDATE {ROW_BUFFER_TABLE}
               SET status = 'pending',
                   updated_at = NOW()
             WHERE job_id = CAST(:job_id AS uuid)
               AND status = 'processing'
               AND updated_at < (NOW() - make_interval(mins => CAST(:stale_minutes AS int)))
            RETURNING id
            """
        ),
        {"job_id": job_id, "stale_minutes": max(1, stale_minutes)},
    )
    return len(result.fetchall())


async def _pick_pending_rows(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    job_id: str,
    limit: int = 500,
) -> list[dict[str, Any]]:
    safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
    schema = workspace_schema_from_uuid(safe_workspace_uuid).replace('"', '""')
    await db_session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    result = await db_session.execute(
        text(
            f"""
            WITH picked AS (
                SELECT id
                  FROM {ROW_BUFFER_TABLE}
                 WHERE job_id = CAST(:job_id AS uuid)
                   AND status = 'pending'
                 ORDER BY created_at ASC
                 LIMIT :limit
                 FOR UPDATE SKIP LOCKED
            )
            UPDATE {ROW_BUFFER_TABLE} b
               SET status = 'processing',
                   updated_at = NOW()
              FROM picked
             WHERE b.id = picked.id
            RETURNING
                b.id::text AS id,
                b.session_id,
                b.payload_jsonb
            """
        ),
        {"job_id": job_id, "limit": max(1, min(limit, 2000))},
    )
    return [dict(row) for row in result.mappings().all()]


async def _mark_rows(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    row_ids: list[str],
    status: str,
    error: str = "",
) -> int:
    if not row_ids:
        return 0
    safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
    schema = workspace_schema_from_uuid(safe_workspace_uuid).replace('"', '""')
    await db_session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    result = await db_session.execute(
        text(
            f"""
            UPDATE {ROW_BUFFER_TABLE}
               SET status = :status,
                   attempts = CASE WHEN :status = 'failed' THEN attempts + 1 ELSE attempts END,
                   last_error = CASE WHEN :status = 'failed' THEN :error ELSE last_error END,
                   sent_at = CASE WHEN :status = 'sent' THEN NOW() ELSE sent_at END,
                   updated_at = NOW()
             WHERE id IN (
                 SELECT CAST(value AS uuid)
                   FROM jsonb_array_elements_text(CAST(:ids_json AS jsonb))
             )
            RETURNING id
            """
        ),
        {
            "status": status,
            "error": error,
            "ids_json": json.dumps(row_ids),
        },
    )
    return len(result.fetchall())


async def _mark_next_run(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    job_id: str,
    next_run_at: datetime,
) -> None:
    safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
    schema = workspace_schema_from_uuid(safe_workspace_uuid).replace('"', '""')
    await db_session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    await db_session.execute(
        text(
            f"""
            UPDATE {JOB_TABLE}
               SET next_run_at = :next_run_at,
                   updated_at = NOW()
             WHERE id = CAST(:job_id AS uuid)
            """
        ),
        {"job_id": job_id, "next_run_at": next_run_at},
    )


async def _store_session_generate_file_result(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    session_id: int,
    result_payload: dict[str, Any],
) -> None:
    safe_workspace_uuid = normalize_workspace_uuid(workspace_uuid)
    schema = workspace_schema_from_uuid(safe_workspace_uuid).replace('"', '""')
    await db_session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    await db_session.execute(
        text(
            """
            UPDATE orch_sessions
               SET runtime_variables =
                   jsonb_set(
                       COALESCE(runtime_variables, '{}'::jsonb),
                       '{generate_file_last_result}',
                       CAST(:result_payload AS jsonb),
                       TRUE
                   ),
                   updated_at = NOW()
             WHERE id = :session_id
            """
        ),
        {
            "session_id": session_id,
            "result_payload": json.dumps(result_payload, ensure_ascii=False),
        },
    )


def _serialize_rows(
    *,
    format_type: str,
    delimiter: str,
    include_header: bool,
    line_break: str,
    rows: list[dict[str, Any]],
) -> str:
    if format_type == "csv":
        field_names = list(rows[0].keys()) if rows else []
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=field_names, delimiter=delimiter, lineterminator=line_break)
        if include_header:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: "" if v is None else str(v) for k, v in row.items()})
        return buffer.getvalue()
    if format_type == "json":
        return json.dumps(rows, ensure_ascii=False)
    if format_type == "jsonl":
        lines = [json.dumps(row, ensure_ascii=False) for row in rows]
        return (line_break.join(lines) + line_break) if lines else ""
    if format_type == "txt":
        lines = [delimiter.join("" if value is None else str(value) for value in row.values()) for row in rows]
        return (line_break.join(lines) + line_break) if lines else ""
    raise ValueError("Formato de arquivo não suportado.")


def _sftp_write(
    *,
    destination_config: dict[str, Any],
    format_config: dict[str, Any],
    session_id: int,
    payload_text: str,
) -> dict[str, Any]:
    import paramiko

    destination_path = _safe_relpath(str(destination_config.get("path") or ""))
    file_name = str(destination_config.get("file_name") or "export.csv").strip()
    write_mode = str(format_config.get("write_mode") or "create").strip().lower()
    if write_mode == "create_per_session":
        file_name = _append_session_suffix(file_name, session_id)
    encoding = str(format_config.get("encoding") or "utf-8").strip().lower()
    line_break = str(format_config.get("line_break") or "\n")

    host = str(destination_config.get("sftp_host") or "").strip()
    port_raw = destination_config.get("sftp_port")
    port = int(str(port_raw).strip()) if str(port_raw or "").strip().isdigit() else 22
    user = str(destination_config.get("sftp_user") or "").strip()
    password = str(destination_config.get("sftp_password") or "").strip()
    if not host or not user or not password:
        raise RuntimeError("Credenciais SFTP incompletas.")

    transport = paramiko.Transport((host, port))
    transport.connect(username=user, password=password)
    sftp = paramiko.SFTPClient.from_transport(transport)
    try:
        if destination_path:
            current = ""
            for chunk in [part for part in destination_path.split("/") if part]:
                current = f"{current}/{chunk}" if current else chunk
                try:
                    sftp.listdir(current)
                except Exception:
                    sftp.mkdir(current)

        remote_dir = destination_path or "."
        existing = set(sftp.listdir(remote_dir))
        target_name = file_name
        file_exists = target_name in existing

        remote_file = f"{remote_dir}/{target_name}" if remote_dir != "." else target_name
        payload = payload_text.encode(encoding)

        try:
            if write_mode in {"create", "create_per_session"} and file_exists:
                raise RuntimeError("Arquivo já existe para write_mode=create.")
            if write_mode == "append" and file_exists:
                with sftp.open(remote_file, "rb") as existing_file:
                    previous = existing_file.read().decode(encoding, errors="ignore")
                append_text = payload_text
                if previous and append_text and not previous.endswith(line_break):
                    append_text = f"{line_break}{append_text}"
                payload = append_text.encode(encoding)
                with sftp.open(remote_file, "ab") as file_handle:
                    file_handle.write(payload)
            else:
                with sftp.open(remote_file, "wb") as file_handle:
                    file_handle.write(payload)
        except Exception as exc:
            if write_mode not in {"overwrite", "append"} or not _is_permission_like_error(exc):
                raise

            refreshed_names = set(sftp.listdir(remote_dir))
            for sequence in range(1, 10_000):
                candidate_name = _append_internal_suffix(file_name, sequence)
                if candidate_name in refreshed_names:
                    continue
                candidate_remote_file = f"{remote_dir}/{candidate_name}" if remote_dir != "." else candidate_name
                try:
                    with sftp.open(candidate_remote_file, "wb") as file_handle:
                        file_handle.write(payload)
                    target_name = candidate_name
                    remote_file = candidate_remote_file
                    break
                except Exception as retry_exc:
                    if _is_permission_like_error(retry_exc):
                        continue
                    raise
            else:
                raise RuntimeError("Sem permissão para gravar arquivo no destino.") from exc

        return {
            "file_name": target_name,
            "remote_path": f"/{remote_file.lstrip('/')}",
            "size_bytes": len(payload),
            "md5": hashlib.md5(payload).hexdigest(),
        }
    finally:
        sftp.close()
        transport.close()


async def process_generate_file_job(
    db_session: AsyncSession,
    *,
    workspace_uuid: str,
    job_id: str,
) -> dict[str, Any]:
    settings = get_settings()
    await _reclaim_stale_processing_rows(
        db_session,
        workspace_uuid=workspace_uuid,
        job_id=job_id,
        stale_minutes=settings.celery_generate_file_stale_processing_minutes,
    )
    job = await _load_job(db_session, workspace_uuid=workspace_uuid, job_id=job_id)
    if job is None:
        return {"job_id": job_id, "rows_selected": 0, "rows_sent": 0, "status": "job_not_found"}

    rows = await _pick_pending_rows(db_session, workspace_uuid=workspace_uuid, job_id=job_id)
    if not rows:
        scheduling = job.get("scheduling_config") if isinstance(job.get("scheduling_config"), dict) else {}
        mode = str(job.get("mode") or "imediato").strip().lower()
        if mode in {"agendado", "recorrente"}:
            await _mark_next_run(
                db_session,
                workspace_uuid=workspace_uuid,
                job_id=job_id,
                next_run_at=compute_next_run_at({"scheduling_run_mode": mode, **scheduling}),
            )
        return {"job_id": job_id, "rows_selected": 0, "rows_sent": 0, "status": "no_rows"}

    destination_config = job.get("destination_config") if isinstance(job.get("destination_config"), dict) else {}
    format_config = job.get("format_config") if isinstance(job.get("format_config"), dict) else {}
    delimiter = str(format_config.get("delimiter") or "|")
    include_header = bool(format_config.get("include_header", True))
    format_type = str(format_config.get("format_type") or "csv").strip().lower()
    line_break = str(format_config.get("line_break") or "\n")

    success_ids: list[str] = []
    failed_ids: list[str] = []
    last_error = ""
    last_result: dict[str, Any] | None = None
    for row in rows:
        try:
            payload_jsonb = row.get("payload_jsonb")
            payload_row, row_destination_config = _extract_row_runtime_payload(
                payload_jsonb,
                default_destination_config=destination_config,
            )
            text_payload = _serialize_rows(
                format_type=format_type,
                delimiter=delimiter,
                include_header=include_header,
                line_break=line_break,
                rows=[payload_row],
            )
            last_result = _sftp_write(
                destination_config=row_destination_config,
                format_config=format_config,
                session_id=int(row["session_id"]),
                payload_text=text_payload,
            )
            await _store_session_generate_file_result(
                db_session,
                workspace_uuid=workspace_uuid,
                session_id=int(row["session_id"]),
                result_payload={
                    **last_result,
                    "status": "success",
                    "format_type": format_type,
                    "write_mode": str(format_config.get("write_mode") or ""),
                    "destination_type": str(job.get("destination_type") or ""),
                },
            )
            success_ids.append(str(row["id"]))
        except Exception as exc:
            failed_ids.append(str(row["id"]))
            last_error = str(exc)
            await _store_session_generate_file_result(
                db_session,
                workspace_uuid=workspace_uuid,
                session_id=int(row["session_id"]),
                result_payload={
                    "status": "error",
                    "error": last_error[:2000],
                    "job_id": job_id,
                },
            )

    if success_ids:
        updated_success = await _mark_rows(
            db_session,
            workspace_uuid=workspace_uuid,
            row_ids=success_ids,
            status="sent",
        )
        if updated_success != len(success_ids):
            raise RuntimeError(
                f"Falha ao atualizar linhas sent: esperado={len(success_ids)} atualizado={updated_success}"
            )
    if failed_ids:
        updated_failed = await _mark_rows(
            db_session,
            workspace_uuid=workspace_uuid,
            row_ids=failed_ids,
            status="failed",
            error=last_error[:2000],
        )
        if updated_failed != len(failed_ids):
            raise RuntimeError(
                f"Falha ao atualizar linhas failed: esperado={len(failed_ids)} atualizado={updated_failed}"
            )

    mode = str(job.get("mode") or "imediato").strip().lower()
    scheduling = job.get("scheduling_config") if isinstance(job.get("scheduling_config"), dict) else {}
    if mode in {"agendado", "recorrente"}:
        await _mark_next_run(
            db_session,
            workspace_uuid=workspace_uuid,
            job_id=job_id,
            next_run_at=compute_next_run_at({"scheduling_run_mode": mode, **scheduling}),
        )

    return {
        "job_id": job_id,
        "rows_selected": len(rows),
        "rows_sent": len(success_ids),
        "rows_failed": len(failed_ids),
        "status": "success" if not failed_ids else "partial_error",
        "last_result": last_result,
        "error": last_error if failed_ids else None,
    }
