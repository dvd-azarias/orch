from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any

from app.services.phone_normalizer import normalize_phone_to_canonical_ani


MANAGE_CONTACT_CHANNEL_TYPES = {"voice", "whatsapp", "sms", "rcs", "email"}
MANAGE_CONTACT_CHANNEL_OPERATIONS = {"upsert", "deactivate"}
MANAGE_CONTACT_CHANNEL_MAX_ITEMS = 100
MANAGE_CONTACT_CHANNEL_MAX_LABEL_LENGTH = 60
MANAGE_CONTACT_CHANNEL_MAX_PRIORITY = 1000
EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


class ManageContactChannelsError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RequestedContactChannel:
    channel_type: str
    value: str
    label: str | None
    priority: int | None
    is_primary: bool | None

    @property
    def key(self) -> tuple[str, str]:
        return self.channel_type, self.value


@dataclass(frozen=True)
class ContactChannelMutation:
    channels: list[dict[str, Any]]
    changed_keys: list[tuple[str, str]]
    missing_keys: list[tuple[str, str]]

    @property
    def primary_channel(self) -> dict[str, Any] | None:
        return next(
            (
                channel
                for channel in self.channels
                if channel.get("is_primary") is True
                and channel.get("is_valid") is not False
                and channel.get("is_reachable") is not False
            ),
            None,
        )


def _channel_type(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "phone":
        normalized = "voice"
    if normalized not in MANAGE_CONTACT_CHANNEL_TYPES:
        raise ManageContactChannelsError(
            "manage_contact_channels_invalid_channel_type",
            "Cada canal deve usar voice, whatsapp, sms, rcs ou email.",
        )
    return normalized


def _channel_value(channel_type: str, value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set, bool)):
        value = None
    raw = str(value or "").strip()
    if channel_type == "email":
        normalized = raw.lower()
        if len(normalized) > 320 or EMAIL_RE.fullmatch(normalized) is None:
            raise ManageContactChannelsError(
                "manage_contact_channels_invalid_email",
                "O endereço de e-mail informado é inválido.",
            )
        return normalized
    normalized = str(normalize_phone_to_canonical_ani(raw) or "").strip()
    if not normalized or len(normalized) > 32:
        raise ManageContactChannelsError(
            "manage_contact_channels_invalid_phone",
            "O endereço de telefone deve conter somente um número válido.",
        )
    return normalized


def _channel_label(value: Any) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, (dict, list, tuple, set, bool)):
        raise ManageContactChannelsError(
            "manage_contact_channels_invalid_label",
            "A label do canal deve ser um texto simples.",
        )
    normalized = str(value).strip()
    if (
        len(normalized) > MANAGE_CONTACT_CHANNEL_MAX_LABEL_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ManageContactChannelsError(
            "manage_contact_channels_invalid_label",
            "A label do canal deve ter até 60 caracteres e não pode conter controles.",
        )
    return normalized


def _channel_priority(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ManageContactChannelsError(
            "manage_contact_channels_invalid_priority",
            "A prioridade do canal deve ser um inteiro entre 1 e 1000.",
        )
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ManageContactChannelsError(
            "manage_contact_channels_invalid_priority",
            "A prioridade do canal deve ser um inteiro entre 1 e 1000.",
        ) from exc
    if not numeric.is_integer() or numeric < 1 or numeric > MANAGE_CONTACT_CHANNEL_MAX_PRIORITY:
        raise ManageContactChannelsError(
            "manage_contact_channels_invalid_priority",
            "A prioridade do canal deve ser um inteiro entre 1 e 1000.",
        )
    return int(numeric)


def _optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "sim"}:
        return True
    if normalized in {"0", "false", "no", "off", "nao", "não"}:
        return False
    raise ManageContactChannelsError(
        "manage_contact_channels_invalid_primary",
        "O campo is_primary deve ser verdadeiro ou falso.",
    )


def _existing_bool(value: Any, *, default: bool) -> bool:
    try:
        parsed = _optional_bool(value)
    except ManageContactChannelsError:
        return default
    return default if parsed is None else parsed


def parse_requested_contact_channels(value: Any) -> list[RequestedContactChannel]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ManageContactChannelsError(
                "manage_contact_channels_invalid_channels",
                "O campo channels deve conter uma lista JSON válida.",
            ) from exc
    if not isinstance(value, list) or not value or len(value) > MANAGE_CONTACT_CHANNEL_MAX_ITEMS:
        raise ManageContactChannelsError(
            "manage_contact_channels_invalid_channels",
            "O campo channels deve conter entre 1 e 100 canais.",
        )

    requested: dict[tuple[str, str], RequestedContactChannel] = {}
    requested_order: list[tuple[str, str]] = []
    primary_count = 0
    for item in value:
        if not isinstance(item, dict):
            raise ManageContactChannelsError(
                "manage_contact_channels_invalid_channels",
                "Cada item de channels deve ser um objeto.",
            )
        channel_type = _channel_type(item.get("type"))
        address = item.get("address") if "address" in item else item.get("value")
        channel = RequestedContactChannel(
            channel_type=channel_type,
            value=_channel_value(channel_type, address),
            label=_channel_label(item.get("label")),
            priority=_channel_priority(item.get("priority")),
            is_primary=_optional_bool(item.get("is_primary")),
        )
        existing = requested.get(channel.key)
        if existing is not None and existing != channel:
            raise ManageContactChannelsError(
                "manage_contact_channels_conflicting_duplicate",
                "O mesmo canal foi informado mais de uma vez com configurações diferentes.",
            )
        if existing is None:
            requested[channel.key] = channel
            requested_order.append(channel.key)
            if channel.is_primary is True:
                primary_count += 1

    if primary_count > 1:
        raise ManageContactChannelsError(
            "manage_contact_channels_multiple_primary",
            "Somente um canal pode ser marcado como principal por operação.",
        )
    return [requested[key] for key in requested_order]


def _existing_channels(value: Any) -> tuple[dict[tuple[str, str], dict[str, Any]], list[dict[str, Any]]]:
    raw_channels = value if isinstance(value, list) else []
    managed: dict[tuple[str, str], dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_channels):
        if not isinstance(raw, dict):
            continue
        try:
            channel_type = _channel_type(raw.get("type"))
            channel_value = _channel_value(channel_type, raw.get("value"))
        except ManageContactChannelsError:
            passthrough.append(copy.deepcopy(raw))
            continue
        key = (channel_type, channel_value)
        normalized = copy.deepcopy(raw)
        normalized["type"] = channel_type
        normalized["value"] = channel_value
        try:
            normalized["label"] = _channel_label(raw.get("label"))
        except ManageContactChannelsError:
            normalized["label"] = None
        try:
            priority = _channel_priority(raw.get("priority"))
        except ManageContactChannelsError:
            priority = None
        normalized["priority"] = priority or min(index + 1, MANAGE_CONTACT_CHANNEL_MAX_PRIORITY)
        active = (
            str(raw.get("state") or "active").strip().lower() != "inactive"
            and _existing_bool(raw.get("is_valid"), default=True)
            and _existing_bool(raw.get("is_reachable"), default=True)
        )
        normalized["state"] = "active" if active else "inactive"
        normalized["is_valid"] = active
        normalized["is_reachable"] = active
        normalized["is_primary"] = (
            _existing_bool(raw.get("is_primary"), default=False) and active
        )
        if key not in managed:
            managed[key] = normalized
        else:
            previous = managed[key]
            previous["is_primary"] = bool(previous.get("is_primary")) or bool(
                normalized.get("is_primary")
            )
            previous["priority"] = min(
                int(previous.get("priority") or MANAGE_CONTACT_CHANNEL_MAX_PRIORITY),
                int(normalized.get("priority") or MANAGE_CONTACT_CHANNEL_MAX_PRIORITY),
            )
            if not previous.get("label") and normalized.get("label"):
                previous["label"] = normalized["label"]
    return managed, passthrough


def _channel_sort_key(channel: dict[str, Any]) -> tuple[int, int, str, str]:
    return (
        0 if channel.get("is_primary") is True else 1,
        int(channel.get("priority") or MANAGE_CONTACT_CHANNEL_MAX_PRIORITY),
        str(channel.get("type") or ""),
        str(channel.get("value") or ""),
    )


def apply_contact_channel_operation(
    *,
    existing_channels: Any,
    requested_channels: list[RequestedContactChannel],
    operation: str,
) -> ContactChannelMutation:
    operation = str(operation or "").strip().lower()
    if operation not in MANAGE_CONTACT_CHANNEL_OPERATIONS:
        raise ManageContactChannelsError(
            "manage_contact_channels_invalid_operation",
            "A operação deve ser upsert ou deactivate.",
        )
    managed, passthrough = _existing_channels(existing_channels)
    before = copy.deepcopy(managed)
    missing_keys: list[tuple[str, str]] = []
    explicitly_demoted: set[tuple[str, str]] = set()
    requested_primary = next(
        (channel.key for channel in requested_channels if channel.is_primary is True),
        None,
    )

    if operation == "deactivate":
        missing_keys = [channel.key for channel in requested_channels if channel.key not in managed]
        if missing_keys:
            return ContactChannelMutation(
                channels=sorted(managed.values(), key=_channel_sort_key) + passthrough,
                changed_keys=[],
                missing_keys=missing_keys,
            )
        for requested in requested_channels:
            current = managed[requested.key]
            current["state"] = "inactive"
            current["is_valid"] = False
            current["is_reachable"] = False
            current["is_primary"] = False
    else:
        next_priority = max(
            (int(item.get("priority") or 0) for item in managed.values()),
            default=0,
        )
        for requested in requested_channels:
            current = managed.get(requested.key)
            if current is None:
                next_priority = min(next_priority + 1, MANAGE_CONTACT_CHANNEL_MAX_PRIORITY)
                current = {
                    "type": requested.channel_type,
                    "value": requested.value,
                    "label": requested.label or "manual",
                    "priority": requested.priority or next_priority,
                    "is_primary": requested.is_primary is True,
                    "state": "active",
                    "is_valid": True,
                    "is_reachable": True,
                    "provider": "orch",
                }
                managed[requested.key] = current
            else:
                current["state"] = "active"
                current["is_valid"] = True
                current["is_reachable"] = True
                if requested.label is not None:
                    current["label"] = requested.label
                if requested.priority is not None:
                    current["priority"] = requested.priority
                if requested.is_primary is not None:
                    current["is_primary"] = requested.is_primary
            if requested.is_primary is False:
                explicitly_demoted.add(requested.key)

    active_channels = [
        (key, channel)
        for key, channel in managed.items()
        if channel.get("is_valid") is not False and channel.get("is_reachable") is not False
    ]
    if requested_primary is not None and operation == "upsert":
        for key, channel in active_channels:
            channel["is_primary"] = key == requested_primary
    else:
        primary_candidates = [
            (key, channel)
            for key, channel in active_channels
            if channel.get("is_primary") is True and key not in explicitly_demoted
        ]
        primary_candidates.sort(key=lambda item: _channel_sort_key(item[1]))
        chosen_key = primary_candidates[0][0] if primary_candidates else None
        if chosen_key is None and active_channels and not explicitly_demoted:
            chosen_key = min(active_channels, key=lambda item: _channel_sort_key(item[1]))[0]
        for key, channel in active_channels:
            channel["is_primary"] = key == chosen_key

    changed_keys = sorted(
        key
        for key in set(before) | set(managed)
        if before.get(key) != managed.get(key)
    )
    channels = sorted(managed.values(), key=_channel_sort_key) + passthrough
    return ContactChannelMutation(
        channels=channels,
        changed_keys=changed_keys,
        missing_keys=[],
    )
