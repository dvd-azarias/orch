# Referência: app/services/s3_files_auto_mailing.py
#
# Funções que fazem parsing do evento S3, localizam o fluxo de orquestração
# correto, baixam o arquivo do Files App e criam o mailing automaticamente.

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote
from uuid import UUID

from starlette.datastructures import UploadFile

from app.crud.s3_files_ingestion import S3FilesIngestionCRUD
from app.crud.source_list import SourceListCRUD
from app.crud.workspace_s3_events import WorkspaceS3EventsCRUD
from app.db.connection import postgres_conn
from app.db.utils import normalize_workspace_schema
from app.models.source_list import SourceListOrigin, SourceListStatus
from app.services.files_app import download_file_from_files_app
from app.services.source_list_ingestion import handle_source_list_upload

LOGGER = logging.getLogger("target_core.s3_files_auto_mailing")


@dataclass
class S3FilesTriggerMatch:
    flow_id: UUID
    folder_path: str
    mapping_template_uuid: UUID
    mapping_template_internal_id: int


def _normalize_folder_path(value: Optional[str]) -> str:
    return (value or "").strip().strip("/")


def _extract_folder_from_key(raw_key: str | None) -> str:
    key = unquote((raw_key or "").strip())
    if not key:
        return ""
    if key.startswith("files/"):
        key = key[len("files/") :]
    parts = [part for part in key.split("/") if part]
    if len(parts) < 3 or not parts[0].startswith("workspace-"):
        return ""
    return "/".join(parts[1:-1])


def _extract_workspace_from_key(raw_key: str | None) -> Optional[str]:
    key = unquote((raw_key or "").strip())
    if key.startswith("files/"):
        key = key[len("files/") :]
    parts = [part for part in key.split("/") if part]
    if not parts:
        return None
    prefix = parts[0]
    if not prefix.startswith("workspace-"):
        return None
    candidate = prefix[len("workspace-") :]
    try:
        return str(UUID(candidate))
    except ValueError:
        return None


def _first_non_empty_str(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _resolve_event_context(payload: Dict[str, Any]) -> Dict[str, Any]:
    file_payload = payload.get("file") or payload.get("data", {}).get("file", {})
    raw_key = _first_non_empty_str(
        payload.get("Key"),
        payload.get("key"),
        payload.get("object_key"),
        payload.get("objectKey"),
        payload.get("s3_key"),
    )
    folder_path = _normalize_folder_path(
        _first_non_empty_str(
            file_payload.get("folder_path"),
            payload.get("folder_path"),
            _extract_folder_from_key(raw_key),
        )
    )
    workspace_uuid = _first_non_empty_str(
        file_payload.get("workspace_uuid"),
        payload.get("workspace_uuid"),
        _extract_workspace_from_key(raw_key),
    )
    try:
        workspace_uuid = str(UUID(workspace_uuid))
    except (ValueError, TypeError):
        workspace_uuid = ""

    return {
        "workspace_uuid": workspace_uuid,
        "folder_path": folder_path,
        "file_id": _first_non_empty_str(file_payload.get("id"), payload.get("file_id")),
        "file_url": _first_non_empty_str(file_payload.get("url"), payload.get("file_url")),
        "file_name": _first_non_empty_str(file_payload.get("original_name"), payload.get("file_name"), "arquivo.csv"),
        "object_key": raw_key or "",
        "event_name": _first_non_empty_str(payload.get("EventName"), payload.get("eventName")),
        "event_timestamp": _first_non_empty_str(payload.get("eventTime"), payload.get("created_at")),
        "file_mime_type": _first_non_empty_str(file_payload.get("mime_type"), payload.get("mime_type")),
        "file_size_bytes": file_payload.get("size_bytes") or payload.get("file_size"),
    }


def _ensure_download_url(file_url: str) -> str:
    if not file_url:
        return file_url
    if "download=" in file_url:
        return file_url
    separator = "&" if "?" in file_url else "?"
    return f"{file_url}{separator}download=true"


def _find_matching_trigger(*, workspace_uuid: str, folder_path: str, crud: SourceListCRUD) -> Optional[S3FilesTriggerMatch]:
    schema = normalize_workspace_schema(workspace_uuid)
    query = f"""
        SELECT
            f.id AS flow_id,
            COALESCE(
                cr.definition -> 'canvas_properties' -> 'orchestration_trigger' ->> 'folder_path',
                dr.definition -> 'canvas_properties' -> 'orchestration_trigger' ->> 'folder_path',
                ''
            ) AS trigger_folder_path,
            COALESCE(
                cr.definition -> 'canvas_properties' -> 'orchestration_trigger' ->> 'mapping_template_id',
                dr.definition -> 'canvas_properties' -> 'orchestration_trigger' ->> 'mapping_template_id',
                ''
            ) AS mapping_template_id
          FROM "{schema}".flow_v2 f
     LEFT JOIN "{schema}".flow_v2_revision cr ON cr.id = f.current_revision_id
     LEFT JOIN "{schema}".flow_v2_revision dr ON dr.id = f.draft_revision_id
         WHERE f.deleted_at IS NULL
           AND f.archived_at IS NULL
           AND f.is_active IS TRUE
           AND lower(COALESCE(cr.definition ->> 'mode', dr.definition ->> 'mode', '')) = 'orchestration';
    """
    result = postgres_conn.fetch_query(query=query, params={}, origin="s3_auto_mailing.find_triggers")
    if result["status"] != "ok":
        raise RuntimeError(result.get("error") or "Falha ao consultar flows de orquestração.")

    normalized_target = _normalize_folder_path(folder_path)
    matches: List[S3FilesTriggerMatch] = []
    for row in result.get("data") or []:
        flow_folder = _normalize_folder_path(row.get("trigger_folder_path"))
        if flow_folder != normalized_target:
            continue
        mapping_template_raw = str(row.get("mapping_template_id") or "").strip()
        try:
            mapping_template_uuid = UUID(mapping_template_raw)
        except ValueError:
            LOGGER.warning(
                "s3_auto_mailing.invalid_mapping_template_uuid",
                extra={"workspace_uuid": workspace_uuid, "flow_id": row.get("flow_id"), "mapping_template_id": mapping_template_raw},
            )
            continue
        template_row = crud.get_template_by_uuid(workspace_uuid=workspace_uuid, template_uuid=mapping_template_uuid)
        if not template_row:
            LOGGER.warning(
                "s3_auto_mailing.mapping_template_not_found",
                extra={"workspace_uuid": workspace_uuid, "flow_id": row.get("flow_id"), "mapping_template_id": str(mapping_template_uuid)},
            )
            continue
        matches.append(
            S3FilesTriggerMatch(
                flow_id=UUID(str(row["flow_id"])),
                folder_path=flow_folder,
                mapping_template_uuid=mapping_template_uuid,
                mapping_template_internal_id=int(template_row["id"]),
            )
        )

    if len(matches) > 1:
        raise RuntimeError("Conflito: mais de um flow orchestration monitora a mesma pasta.")
    return matches[0] if matches else None


def process_s3_files_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    context = _resolve_event_context(payload)
    workspace_uuid = context["workspace_uuid"]
    folder_path = context["folder_path"]
    file_id_raw = context["file_id"]
    file_url = context["file_url"]

    if not workspace_uuid or not folder_path or not file_id_raw or not file_url:
        LOGGER.warning(
            "s3_auto_mailing.payload_missing_required_fields",
            extra={
                "workspace_uuid": workspace_uuid or None,
                "folder_path": folder_path or None,
                "file_id": file_id_raw or None,
            },
        )
        return {"status": "ignored", "reason": "payload_missing_required_fields"}

    workspace_events = WorkspaceS3EventsCRUD()
    event_id = workspace_events.create_event(
        workspace_uuid=workspace_uuid,
        event_name=context["event_name"],
        event_timestamp=context["event_timestamp"],
        object_key=context["object_key"],
        folder_path=folder_path,
        file_id=file_id_raw,
        file_url=file_url,
        file_name=context["file_name"],
        file_mime_type=context["file_mime_type"],
        file_size_bytes=context["file_size_bytes"],
        payload=payload,
    )

    try:
        file_id = UUID(file_id_raw)
    except ValueError:
        workspace_events.update_event(
            workspace_uuid=workspace_uuid,
            event_id=event_id,
            status="ignored",
            orch_import_trigger_detected=False,
            orch_import_trigger_result={"reason": "invalid_file_id"},
            error_detail="file.id inválido no payload.",
        )
        return {"status": "ignored", "reason": "invalid_file_id", "workspace_event_id": event_id}

    ingestion_crud = S3FilesIngestionCRUD()
    if not ingestion_crud.claim_event(
        workspace_uuid=workspace_uuid,
        file_id=file_id,
        object_key=context["object_key"],
        folder_path=folder_path,
        payload=json.dumps(payload, ensure_ascii=False),
    ):
        workspace_events.update_event(
            workspace_uuid=workspace_uuid,
            event_id=event_id,
            status="ignored",
            orch_import_trigger_detected=False,
            orch_import_trigger_result={"reason": "duplicate_event"},
        )
        return {"status": "ignored", "reason": "duplicate_event", "workspace_event_id": event_id}

    source_list_crud = SourceListCRUD()
    trigger = _find_matching_trigger(workspace_uuid=workspace_uuid, folder_path=folder_path, crud=source_list_crud)
    if trigger is None:
        ingestion_crud.mark_ignored(
            workspace_uuid=workspace_uuid,
            file_id=file_id,
            reason="Nenhum flow orchestration monitora a pasta informada.",
        )
        workspace_events.update_event(
            workspace_uuid=workspace_uuid,
            event_id=event_id,
            status="ignored",
            orch_import_trigger_detected=False,
            orch_import_trigger_result={"reason": "no_matching_flow"},
        )
        return {"status": "ignored", "reason": "no_matching_flow", "workspace_event_id": event_id}

    workspace_events.update_event(
        workspace_uuid=workspace_uuid,
        event_id=event_id,
        status="processing",
        orch_import_trigger_detected=True,
        orch_import_trigger_result={
            "reason": "trigger_detected",
            "flow_id": str(trigger.flow_id),
            "mapping_template_id": str(trigger.mapping_template_uuid),
        },
        flow_id=str(trigger.flow_id),
        mapping_template_uuid=str(trigger.mapping_template_uuid),
    )

    temp_file = tempfile.NamedTemporaryFile(prefix="s3_auto_", suffix=Path(context["file_name"]).suffix or ".csv", delete=False)
    temp_path = Path(temp_file.name)
    temp_file.close()

    try:
        download_file_from_files_app(
            workspace_uuid=workspace_uuid,
            file_url=_ensure_download_url(file_url),
            target_path=temp_path,
        )
        upload_stream = temp_path.open("rb")
        upload = UploadFile(file=upload_stream, filename=context["file_name"])
        try:
            upload_result = asyncio.run(
                handle_source_list_upload(
                    workspace_uuid=workspace_uuid,
                    description=f"Carga automática via evento de arquivos ({folder_path}/{context['file_name']})",
                    upload_file=upload,
                    crud=source_list_crud,
                    origin=SourceListOrigin.api,
                    mapping_template_id=trigger.mapping_template_internal_id,
                )
            )
        finally:
            upload_stream.close()
    finally:
        temp_path.unlink(missing_ok=True)

    if upload_result.status != SourceListStatus.ready_to_ingest:
        ingestion_crud.mark_failed(
            workspace_uuid=workspace_uuid,
            file_id=file_id,
            error_detail="Template aplicado mas mapeamento não ficou pronto para ingestão automática.",
        )
        workspace_events.update_event(
            workspace_uuid=workspace_uuid,
            event_id=event_id,
            status="failed",
            orch_import_trigger_detected=True,
            orch_import_trigger_result={"reason": "mapping_not_ready"},
        )
        return {"status": "failed", "reason": "mapping_not_ready", "workspace_event_id": event_id}

    workspace_events.update_event(
        workspace_uuid=workspace_uuid,
        event_id=event_id,
        status="ready",
        orch_import_trigger_detected=True,
        orch_import_trigger_result={
            "reason": "ready_to_ingest",
            "flow_id": str(trigger.flow_id),
            "mapping_template_id": str(trigger.mapping_template_uuid),
            "mailing_id": str(upload_result.source_list_id),
        },
        flow_id=str(trigger.flow_id),
        mapping_template_uuid=str(trigger.mapping_template_uuid),
        mailing_id=str(upload_result.source_list_id),
    )

    return {
        "status": "ready",
        "workspace_uuid": workspace_uuid,
        "file_id": str(file_id),
        "flow_id": str(trigger.flow_id),
        "folder_path": trigger.folder_path,
        "mapping_template_id": str(trigger.mapping_template_uuid),
        "mailing_id": str(upload_result.source_list_id),
        "internal_mailing_id": upload_result.internal_source_list_id,
        "steps": list(upload_result.steps),
        "workspace_event_id": event_id,
    }
