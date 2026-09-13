from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError
from uuid import UUID

from app.core.config import Settings, get_settings


_MAX_RESPONSE_BYTES = 1024 * 1024
_RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
_SAFE_MATCH_FIELDS = (
    "entry_id",
    "restriction_list_id",
    "restriction_list_name",
    "field_code",
    "value_masked",
    "reason",
    "effective_at",
    "expires_at",
)


class RestrictionListCheckError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class RestrictionListCheckResult:
    decision: str
    restricted: bool
    matches: list[dict[str, Any]]
    evaluated_list_ids: list[str]
    evaluated_fields: list[str]
    evaluated_at: str
    attempts: int

    def runtime_payload(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "restricted": self.restricted,
            "matches": [dict(item) for item in self.matches],
            "match_count": len(self.matches),
            "evaluated_list_ids": list(self.evaluated_list_ids),
            "evaluated_fields": list(self.evaluated_fields),
            "evaluated_at": self.evaluated_at,
            "attempts": self.attempts,
        }


def evaluate_restriction_lists(
    *,
    workspace_uuid: str,
    restriction_list_ids: list[str],
    evaluation_scope: str,
    person_uuid: str | None = None,
    channel_type: str | None = None,
    channel_address: str | None = None,
    settings: Settings | None = None,
) -> RestrictionListCheckResult:
    resolved_settings = settings or get_settings()
    base_url = str(
        resolved_settings.target_core_supplier_api_base_url or ""
    ).strip().rstrip("/")
    bearer = str(resolved_settings.target_core_api_bearer_token or "").strip()
    if not base_url:
        raise RestrictionListCheckError(
            "check_restriction_lists_supplier_base_url_missing",
            (
                "TARGET_CORE_SUPPLIER_API_BASE_URL não configurada para consultar "
                "Listas de Restrição."
            ),
        )
    if not bearer:
        raise RestrictionListCheckError(
            "check_restriction_lists_target_core_bearer_missing",
            "TARGET_CORE_API_BEARER_TOKEN não configurado para consultar Listas de Restrição.",
        )

    subject: dict[str, Any]
    if evaluation_scope == "person":
        if not person_uuid:
            raise RestrictionListCheckError(
                "check_restriction_lists_missing_person_uuid",
                "A sessão não possui person_uuid para consultar a pessoa completa.",
            )
        subject = {"person_uuid": person_uuid}
    elif evaluation_scope == "current_channel":
        if not channel_type or not channel_address:
            raise RestrictionListCheckError(
                "check_restriction_lists_missing_channel",
                "A sessão não possui tipo e endereço do canal atual para consulta.",
            )
        subject = {
            "channel": {
                "type": channel_type,
                "address": channel_address,
            }
        }
    else:
        raise RestrictionListCheckError(
            "check_restriction_lists_invalid_evaluation_scope",
            "O escopo da consulta deve ser person ou current_channel.",
        )

    body = json.dumps(
        {
            "restriction_list_ids": restriction_list_ids,
            "evaluation_scope": evaluation_scope,
            "subject": subject,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    timeout_seconds = max(
        1.0,
        float(resolved_settings.restriction_list_check_http_timeout_seconds),
    )
    max_attempts = max(
        1,
        min(int(resolved_settings.restriction_list_check_max_attempts), 5),
    )
    backoff_seconds = max(
        0.0,
        float(resolved_settings.restriction_list_check_retry_backoff_seconds),
    )
    url = f"{base_url}/v2/contact-supplier/restrictions/evaluate"

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        req = request.Request(
            url=url,
            method="POST",
            data=body,
        )
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Bearer {bearer}")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-WORKSPACE-UUID", workspace_uuid)

        status_code = 599
        response_body = b""
        try:
            with request.urlopen(req, timeout=timeout_seconds) as response:  # noqa: S310
                status_code = int(response.status)
                response_body = response.read(_MAX_RESPONSE_BYTES + 1)
                last_error = None
        except HTTPError as exc:
            status_code = int(exc.code)
            response_body = exc.read(_MAX_RESPONSE_BYTES + 1)
            last_error = exc
        except (URLError, TimeoutError, OSError) as exc:
            last_error = exc

        if len(response_body) > _MAX_RESPONSE_BYTES:
            raise RestrictionListCheckError(
                "check_restriction_lists_target_core_invalid_response",
                "A resposta do Supplier excedeu o limite seguro.",
                status_code=status_code if status_code != 599 else None,
            )

        if 200 <= status_code < 300:
            return _parse_response(
                response_body,
                requested_list_ids=restriction_list_ids,
                attempts=attempt,
            )

        if attempt >= max_attempts or status_code not in _RETRYABLE_STATUS_CODES:
            break
        if backoff_seconds > 0:
            time.sleep(backoff_seconds * attempt)

    if status_code == 599:
        raise RestrictionListCheckError(
            "check_restriction_lists_target_core_unavailable",
            "O Supplier está indisponível para consultar Listas de Restrição.",
        ) from last_error
    raise RestrictionListCheckError(
        "check_restriction_lists_target_core_http_error",
        "O Supplier rejeitou a consulta às Listas de Restrição.",
        status_code=status_code,
    ) from last_error


def _parse_response(
    response_body: bytes,
    *,
    requested_list_ids: list[str],
    attempts: int,
) -> RestrictionListCheckResult:
    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RestrictionListCheckError(
            "check_restriction_lists_target_core_invalid_response",
            "O Supplier devolveu uma resposta inválida para a consulta.",
        ) from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RestrictionListCheckError(
            "check_restriction_lists_target_core_invalid_response",
            "O Supplier não devolveu o envelope esperado para a consulta.",
        )

    decision = str(data.get("decision") or "").strip().lower()
    restricted = data.get("restricted")
    matches = data.get("matches")
    evaluated_ids = data.get("evaluated_list_ids")
    evaluated_fields = data.get("evaluated_fields")
    evaluated_at = str(data.get("evaluated_at") or "").strip()
    if (
        decision not in {"restricted", "allowed"}
        or not isinstance(restricted, bool)
        or restricted is not (decision == "restricted")
        or not isinstance(matches, list)
        or any(not isinstance(item, dict) for item in matches)
        or bool(matches) is not restricted
        or not isinstance(evaluated_ids, list)
        or not isinstance(evaluated_fields, list)
        or any(not isinstance(item, str) for item in evaluated_fields)
        or not evaluated_at
    ):
        raise RestrictionListCheckError(
            "check_restriction_lists_target_core_invalid_response",
            "O Supplier devolveu dados incompatíveis com o contrato de consulta.",
        )

    try:
        normalized_requested = {str(UUID(str(item))) for item in requested_list_ids}
        normalized_evaluated = {str(UUID(str(item))) for item in evaluated_ids}
    except (TypeError, ValueError) as exc:
        raise RestrictionListCheckError(
            "check_restriction_lists_target_core_invalid_response",
            "O Supplier devolveu identificadores de lista inválidos.",
        ) from exc
    if normalized_requested != normalized_evaluated:
        raise RestrictionListCheckError(
            "check_restriction_lists_target_core_invalid_response",
            "O Supplier não confirmou a avaliação de todas as listas solicitadas.",
        )

    safe_matches: list[dict[str, Any]] = []
    for match in matches:
        try:
            matched_list_id = str(UUID(str(match.get("restriction_list_id"))))
        except (TypeError, ValueError) as exc:
            raise RestrictionListCheckError(
                "check_restriction_lists_target_core_invalid_response",
                "O Supplier devolveu uma ocorrência com lista inválida.",
            ) from exc
        if matched_list_id not in normalized_requested:
            raise RestrictionListCheckError(
                "check_restriction_lists_target_core_invalid_response",
                "O Supplier devolveu uma ocorrência fora das listas solicitadas.",
            )
        safe_match = {
            key: match.get(key)
            for key in _SAFE_MATCH_FIELDS
            if key in match
        }
        safe_match["restriction_list_id"] = matched_list_id
        safe_matches.append(safe_match)

    return RestrictionListCheckResult(
        decision=decision,
        restricted=restricted,
        matches=safe_matches,
        evaluated_list_ids=[str(UUID(str(item))) for item in evaluated_ids],
        evaluated_fields=list(evaluated_fields),
        evaluated_at=evaluated_at,
        attempts=attempts,
    )


__all__ = [
    "RestrictionListCheckError",
    "RestrictionListCheckResult",
    "evaluate_restriction_lists",
]
