from __future__ import annotations

import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from app.services import restriction_list_check_service as service


LIST_A = "11111111-1111-4111-8111-111111111111"
LIST_B = "22222222-2222-4222-8222-222222222222"


class _Response:
    def __init__(self, payload: dict, *, status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit < 0 else self._body[:limit]


def _settings(**overrides: object) -> SimpleNamespace:
    values = {
        "target_core_api_base_url": "https://target-crud.internal",
        "target_core_supplier_api_base_url": "https://target-supplier.internal",
        "target_core_api_bearer_token": "internal-token",
        "restriction_list_check_http_timeout_seconds": 5.0,
        "restriction_list_check_max_attempts": 2,
        "restriction_list_check_retry_backoff_seconds": 0.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _payload(
    *,
    decision: str = "restricted",
    evaluated_list_ids: list[str] | None = None,
) -> dict:
    restricted = decision == "restricted"
    return {
        "data": {
            "decision": decision,
            "restricted": restricted,
            "matches": (
                [
                    {
                        "entry_id": "33333333-3333-4333-8333-333333333333",
                        "restriction_list_id": LIST_A,
                        "restriction_list_name": "Lista A",
                        "field_code": "phone",
                        "value_masked": "*********0806",
                        "raw_value": "must-not-be-persisted",
                    }
                ]
                if restricted
                else []
            ),
            "evaluated_list_ids": evaluated_list_ids or [LIST_A],
            "evaluated_fields": ["phone"],
            "evaluated_at": "2026-09-13T01:13:24Z",
        }
    }


def test_evaluate_restriction_lists_posts_person_scope_and_parses_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _urlopen(req, *, timeout):  # type: ignore[no-untyped-def]
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Response(_payload())

    monkeypatch.setattr(service.request, "urlopen", _urlopen)

    result = service.evaluate_restriction_lists(
        workspace_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        restriction_list_ids=[LIST_A],
        evaluation_scope="person",
        person_uuid="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        settings=_settings(),  # type: ignore[arg-type]
    )

    assert result.decision == "restricted"
    assert result.runtime_payload()["match_count"] == 1
    assert "raw_value" not in result.runtime_payload()["matches"][0]
    assert captured["url"] == (
        "https://target-supplier.internal/v2/contact-supplier/"
        "restrictions/evaluate"
    )
    assert captured["body"] == {
        "restriction_list_ids": [LIST_A],
        "evaluation_scope": "person",
        "subject": {"person_uuid": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"},
    }
    assert captured["headers"]["X-workspace-uuid"] == (
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
    assert captured["timeout"] == 5.0


def test_evaluate_restriction_lists_posts_current_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _urlopen(req, *, timeout):  # type: ignore[no-untyped-def]
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Response(_payload(decision="allowed"))

    monkeypatch.setattr(service.request, "urlopen", _urlopen)

    result = service.evaluate_restriction_lists(
        workspace_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        restriction_list_ids=[LIST_A],
        evaluation_scope="current_channel",
        channel_type="voice",
        channel_address="5511975620806",
        settings=_settings(),  # type: ignore[arg-type]
    )

    assert result.decision == "allowed"
    assert captured["body"] == {
        "restriction_list_ids": [LIST_A],
        "evaluation_scope": "current_channel",
        "subject": {
            "channel": {"type": "voice", "address": "5511975620806"}
        },
    }


def test_evaluate_restriction_lists_retries_transient_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def _urlopen(req, *, timeout):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HTTPError(req.full_url, 503, "unavailable", {}, io.BytesIO(b"{}"))
        return _Response(_payload(decision="allowed"))

    monkeypatch.setattr(service.request, "urlopen", _urlopen)

    result = service.evaluate_restriction_lists(
        workspace_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        restriction_list_ids=[LIST_A],
        evaluation_scope="current_channel",
        channel_type="voice",
        channel_address="551100000000",
        settings=_settings(),  # type: ignore[arg-type]
    )

    assert result.decision == "allowed"
    assert result.attempts == 2
    assert attempts == 2


def test_evaluate_restriction_lists_does_not_retry_configuration_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def _urlopen(req, *, timeout):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        raise HTTPError(req.full_url, 422, "invalid", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(service.request, "urlopen", _urlopen)

    with pytest.raises(service.RestrictionListCheckError) as exc_info:
        service.evaluate_restriction_lists(
            workspace_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            restriction_list_ids=[LIST_A],
            evaluation_scope="current_channel",
            channel_type="voice",
            channel_address="551100000000",
            settings=_settings(),  # type: ignore[arg-type]
        )

    assert exc_info.value.code == "check_restriction_lists_target_core_http_error"
    assert exc_info.value.status_code == 422
    assert attempts == 1


def test_evaluate_restriction_lists_fails_closed_when_supplier_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
    )

    with pytest.raises(service.RestrictionListCheckError) as exc_info:
        service.evaluate_restriction_lists(
            workspace_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            restriction_list_ids=[LIST_A],
            evaluation_scope="current_channel",
            channel_type="voice",
            channel_address="551100000000",
            settings=_settings(),  # type: ignore[arg-type]
        )

    assert exc_info.value.code == "check_restriction_lists_target_core_unavailable"


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {"decision": "allowed", "restricted": True}},
        _payload(evaluated_list_ids=[LIST_B]),
        {"data": {"decision": "unknown", "restricted": False, "matches": []}},
    ],
)
def test_evaluate_restriction_lists_rejects_inconsistent_responses(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict,
) -> None:
    monkeypatch.setattr(
        service.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(payload),
    )

    with pytest.raises(service.RestrictionListCheckError) as exc_info:
        service.evaluate_restriction_lists(
            workspace_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            restriction_list_ids=[LIST_A],
            evaluation_scope="current_channel",
            channel_type="voice",
            channel_address="551100000000",
            settings=_settings(),  # type: ignore[arg-type]
        )

    assert exc_info.value.code == "check_restriction_lists_target_core_invalid_response"


def test_evaluate_restriction_lists_requires_configuration() -> None:
    with pytest.raises(service.RestrictionListCheckError) as exc_info:
        service.evaluate_restriction_lists(
            workspace_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            restriction_list_ids=[LIST_A],
            evaluation_scope="current_channel",
            channel_type="voice",
            channel_address="551100000000",
            settings=_settings(target_core_supplier_api_base_url=None),  # type: ignore[arg-type]
        )

    assert exc_info.value.code == (
        "check_restriction_lists_supplier_base_url_missing"
    )
