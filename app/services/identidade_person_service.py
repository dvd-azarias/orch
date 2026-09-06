from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError, URLError
from uuid import UUID

from app.services.phone_normalizer import normalize_phone_to_canonical_ani

IDENTIDADE_PERSON_BASE_URL = "https://api.identidade.io/v1/workspaces"
IDENTIDADE_PERSON_TIMEOUT_SECONDS = 10.0
IDENTIDADE_PERSON_MAX_ATTEMPTS = 2
IDENTIDADE_PERSON_MAX_RESPONSE_BYTES = 2_000_000
IDENTIDADE_PERSON_RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504, 599}


class IdentidadePersonServiceError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class IdentidadePersonQueryResult:
    found: bool
    person: dict[str, Any] | None
    attempts: int
    status_code: int


def normalize_document(value: Any) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    if len(digits) != 11:
        raise IdentidadePersonServiceError(
            "identidade_person_invalid_document",
            "O campo document deve resolver para um CPF com 11 dígitos.",
        )
    return digits


def normalize_workspace_id(value: Any) -> str:
    try:
        return str(UUID(str(value or "").strip()))
    except (TypeError, ValueError, AttributeError) as exc:
        raise IdentidadePersonServiceError(
            "identidade_person_invalid_workspace_id",
            "O campo workspace_id deve conter um UUID válido da Identidade.",
        ) from exc


def mask_document(document: str) -> str:
    digits = re.sub(r"\D+", "", str(document or ""))
    if len(digits) <= 4:
        return "***"
    return f"***{digits[-4:]}"


def _read_response_body(response: Any) -> str:
    raw = response.read(IDENTIDADE_PERSON_MAX_RESPONSE_BYTES + 1)
    if len(raw) > IDENTIDADE_PERSON_MAX_RESPONSE_BYTES:
        raise IdentidadePersonServiceError(
            "identidade_person_response_too_large",
            "A resposta da Identidade excedeu o limite de segurança.",
        )
    return raw.decode("utf-8", errors="replace")


def _http_get(*, url: str, access_token: str, timeout_seconds: float) -> tuple[int, str]:
    req = request.Request(
        url=url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "gohp-orch/identidade-person",
        },
        method="GET",
    )
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:  # noqa: S310
            return int(response.status), _read_response_body(response)
    except HTTPError as exc:
        body = _read_response_body(exc) if hasattr(exc, "read") else ""
        return int(getattr(exc, "code", 500) or 500), body
    except (URLError, TimeoutError, OSError):
        return 599, ""


def _query_person_sync(
    *,
    workspace_id: str,
    access_token: str,
    document: str,
    require_phone: bool,
    require_email: bool,
    timeout_seconds: float,
    max_attempts: int,
) -> IdentidadePersonQueryResult:
    query = {
        "document": document,
        "format": "identidade",
    }
    if require_phone:
        query["require_phone"] = "true"
    if require_email:
        query["require_email"] = "true"
    url = f"{IDENTIDADE_PERSON_BASE_URL}/{workspace_id}/person?{parse.urlencode(query)}"

    status_code = 599
    body = ""
    attempts = 0
    for attempts in range(1, max(1, min(int(max_attempts), 3)) + 1):
        status_code, body = _http_get(
            url=url,
            access_token=access_token,
            timeout_seconds=timeout_seconds,
        )
        if status_code not in IDENTIDADE_PERSON_RETRYABLE_STATUSES or attempts >= max_attempts:
            break
        time.sleep(0.2 * attempts)

    if status_code == 599:
        raise IdentidadePersonServiceError(
            "identidade_person_unavailable",
            "Não foi possível acessar a API da Identidade.",
            status_code=status_code,
        )
    if not 200 <= status_code < 300:
        raise IdentidadePersonServiceError(
            "identidade_person_http_error",
            f"A API da Identidade respondeu com HTTP {status_code}.",
            status_code=status_code,
        )

    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise IdentidadePersonServiceError(
            "identidade_person_invalid_response",
            "A API da Identidade retornou JSON inválido.",
            status_code=status_code,
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise IdentidadePersonServiceError(
            "identidade_person_invalid_response",
            "A resposta da Identidade não contém o campo data esperado.",
            status_code=status_code,
        )

    person = next((item for item in payload["data"] if isinstance(item, dict)), None)
    return IdentidadePersonQueryResult(
        found=person is not None,
        person=dict(person) if person is not None else None,
        attempts=attempts,
        status_code=status_code,
    )


async def query_identidade_person(
    *,
    workspace_id: Any,
    access_token: Any,
    document: Any,
    require_phone: bool = False,
    require_email: bool = False,
    timeout_seconds: float = IDENTIDADE_PERSON_TIMEOUT_SECONDS,
    max_attempts: int = IDENTIDADE_PERSON_MAX_ATTEMPTS,
) -> IdentidadePersonQueryResult:
    normalized_workspace_id = normalize_workspace_id(workspace_id)
    normalized_document = normalize_document(document)
    normalized_token = str(access_token or "").strip()
    if not normalized_token:
        raise IdentidadePersonServiceError(
            "identidade_person_missing_access_token",
            "O campo access_token é obrigatório.",
        )
    return await asyncio.to_thread(
        _query_person_sync,
        workspace_id=normalized_workspace_id,
        access_token=normalized_token,
        document=normalized_document,
        require_phone=bool(require_phone),
        require_email=bool(require_email),
        timeout_seconds=max(1.0, min(float(timeout_seconds), 30.0)),
        max_attempts=max_attempts,
    )


def _value_from_object(value: Any, *keys: str) -> Any:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if candidate is not None and str(candidate).strip():
                return candidate
        return None
    return value


def _text(value: Any, *keys: str) -> str | None:
    candidate = _value_from_object(value, *keys)
    if candidate is None:
        return None
    normalized = str(candidate).strip()
    return normalized or None


def _rank(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _normalize_email_channels(raw_emails: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_emails, list):
        return []
    ordered = sorted(
        (entry for entry in raw_emails if isinstance(entry, dict)),
        key=lambda item: _rank(item.get("ranking"), 999_999),
    )
    channels: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in ordered:
        value = _text(entry.get("email"), "raw", "formatted", "value")
        if value is None:
            value = _text(entry.get("address"), "raw", "formatted", "value")
        if value is None:
            value = _text(entry.get("value"), "raw", "formatted")
        if value is None or "@" not in value:
            continue
        value = value.lower()
        key = ("email", value)
        if key in seen:
            continue
        seen.add(key)
        ranking = _rank(entry.get("ranking"), len(channels) + 1)
        channels.append(
            {
                "type": "email",
                "value": value,
                "label": f"identidade_email_{ranking}",
                "is_primary": False,
                "provider": "identidade",
                "ranking": ranking,
            }
        )
    return channels


def _normalize_phone_channels(raw_phones: Any, *, phone_policy: str) -> list[dict[str, Any]]:
    if phone_policy == "none" or not isinstance(raw_phones, list):
        return []
    ordered = sorted(
        (entry for entry in raw_phones if isinstance(entry, dict)),
        key=lambda item: (_rank(item.get("ranking"), 999_999), -_rank(item.get("score"), 0)),
    )
    eligible = [entry for entry in ordered if entry.get("do_not_disturb") is not True]
    if phone_policy == "best_eligible":
        eligible = eligible[:1]

    channels: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in eligible:
        raw_number = _text(entry.get("number"), "raw", "formatted", "value")
        value = str(normalize_phone_to_canonical_ani(raw_number) or "").strip()
        if not value:
            continue
        ranking = _rank(entry.get("ranking"), len(channels) + 1)
        channel_types = ["voice"]
        if entry.get("has_whatsapp") is True:
            channel_types.append("whatsapp")
        if entry.get("has_rcs") is True:
            channel_types.append("rcs")
        for channel_type in channel_types:
            key = (channel_type, value)
            if key in seen:
                continue
            seen.add(key)
            channels.append(
                {
                    "type": channel_type,
                    "value": value,
                    "label": f"identidade_tel_{ranking}",
                    "is_primary": False,
                    "provider": "identidade",
                    "ranking": ranking,
                }
            )
    return channels


def _normalize_address(raw_addresses: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not isinstance(raw_addresses, list):
        return None, []
    addresses = sorted(
        (dict(entry) for entry in raw_addresses if isinstance(entry, dict)),
        key=lambda item: _rank(item.get("ranking"), 999_999),
    )
    return (addresses[0] if addresses else None), addresses


def normalize_identidade_person(
    person: dict[str, Any],
    *,
    fallback_document: str,
    phone_policy: str,
) -> dict[str, Any]:
    document = normalize_document(_text(person.get("document"), "raw", "formatted") or fallback_document)
    name = _text(person.get("name"), "formatted", "raw")
    best_address, addresses = _normalize_address(person.get("addresses"))
    state = _text(best_address.get("state"), "code", "description") if best_address else None
    city = _text(best_address.get("city"), "formatted", "raw") if best_address else None
    country = "Brasil" if best_address else None

    phone_channels = _normalize_phone_channels(person.get("phones"), phone_policy=phone_policy)
    email_channels = _normalize_email_channels(person.get("emails"))
    channels = phone_channels + email_channels
    if channels:
        channels[0]["is_primary"] = True

    raw_phones = [dict(entry) for entry in person.get("phones", []) if isinstance(entry, dict)]
    identity_metadata = {
        "document": person.get("document"),
        "zodiac_sign": person.get("zodiac_sign"),
        "mothers_name": person.get("mothers_name"),
        "deceased": person.get("deceased"),
        "deceased_date": person.get("deceased_date"),
        "document_status": person.get("document_status"),
        "income": person.get("income"),
        "occupation": person.get("occupation"),
        "addresses": addresses,
        "companies": person.get("companies") if isinstance(person.get("companies"), list) else [],
        "emails": person.get("emails") if isinstance(person.get("emails"), list) else [],
        "phones": raw_phones,
        "relatives": person.get("relatives") if isinstance(person.get("relatives"), list) else [],
    }
    primary_channel = channels[0] if channels else {}
    companies = person.get("companies") if isinstance(person.get("companies"), list) else []
    first_company = next((item for item in companies if isinstance(item, dict)), None)
    gender_code = _text(person.get("gender"), "code")
    return {
        "identifier": document,
        "full_name": name,
        "company": (
            _text(first_company.get("trade_name"), "formatted", "raw")
            or _text(first_company.get("legal_name"), "formatted", "raw")
            if first_company
            else None
        ),
        "gender": gender_code if gender_code in {"M", "F"} else None,
        "role": _text(person.get("occupation"), "description", "code"),
        "country": country,
        "state": state,
        "city": city,
        "birthdate": _text(person.get("birthday")),
        "primary_channel_type": primary_channel.get("type"),
        "primary_channel_value": primary_channel.get("value"),
        "primary_channel_label": primary_channel.get("label"),
        "channels": channels,
        "extras": {"identidade": identity_metadata},
    }


def build_normalized_output(
    *,
    normalized_person: dict[str, Any],
    local_action: dict[str, Any],
    mailing_action: dict[str, Any],
) -> dict[str, Any]:
    extras = normalized_person.get("extras") if isinstance(normalized_person.get("extras"), dict) else {}
    identity_metadata = extras.get("identidade") if isinstance(extras.get("identidade"), dict) else {}
    return {
        "found": True,
        "person": {
            "identifier": normalized_person.get("identifier"),
            "full_name": normalized_person.get("full_name"),
            "gender": normalized_person.get("gender"),
            "birthdate": normalized_person.get("birthdate"),
            "country": normalized_person.get("country"),
            "state": normalized_person.get("state"),
            "city": normalized_person.get("city"),
            "channels": normalized_person.get("channels") or [],
            "deceased": identity_metadata.get("deceased"),
            "document_status": identity_metadata.get("document_status"),
        },
        "local_action": local_action,
        "mailing_action": mailing_action,
    }


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == [] or value == {}


def _merge_dict(existing: dict[str, Any], incoming: dict[str, Any], *, overwrite: bool) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if value is None:
            continue
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_dict(current, value, overwrite=overwrite)
        elif overwrite or _is_blank(current):
            merged[key] = value
    return merged


def merge_person_payload(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    *,
    enrichment_policy: str,
) -> dict[str, Any]:
    overwrite = enrichment_policy == "overwrite_non_null"
    merged = dict(existing)
    for field in ("full_name", "company", "gender", "role", "country", "state", "city", "birthdate"):
        incoming_value = incoming.get(field)
        if incoming_value is not None and (overwrite or _is_blank(existing.get(field))):
            merged[field] = incoming_value

    existing_channels = existing.get("channels") if isinstance(existing.get("channels"), list) else []
    incoming_channels = incoming.get("channels") if isinstance(incoming.get("channels"), list) else []
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    channel_sources = [(channel, False) for channel in existing_channels] + [
        (channel, True) for channel in incoming_channels
    ]
    for channel, is_incoming in channel_sources:
        if not isinstance(channel, dict):
            continue
        key = (str(channel.get("type") or "").strip().lower(), str(channel.get("value") or "").strip())
        if not all(key):
            continue
        if key not in by_key:
            by_key[key] = dict(channel)
            order.append(key)
        elif overwrite and is_incoming:
            by_key[key] = _merge_dict(by_key[key], channel, overwrite=True)
    merged_channels = [by_key[key] for key in order]
    merged["channels"] = merged_channels

    existing_extras = existing.get("extras") if isinstance(existing.get("extras"), dict) else {}
    incoming_extras = incoming.get("extras") if isinstance(incoming.get("extras"), dict) else {}
    merged["extras"] = _merge_dict(existing_extras, incoming_extras, overwrite=overwrite)

    provider_primary = next(
        (item for item in incoming_channels if isinstance(item, dict) and item.get("is_primary")),
        None,
    )
    if provider_primary is None and incoming_channels:
        provider_primary = incoming_channels[0] if isinstance(incoming_channels[0], dict) else None
    has_existing_primary = not _is_blank(existing.get("primary_channel_type")) and not _is_blank(
        existing.get("primary_channel_value")
    )
    if provider_primary is not None and (overwrite or not has_existing_primary):
        merged["primary_channel_type"] = provider_primary.get("type")
        merged["primary_channel_value"] = provider_primary.get("value")
        merged["primary_channel_label"] = provider_primary.get("label")
    return merged
