from __future__ import annotations

import pytest

import app.services.identidade_person_flow_link_service as service


class _Settings:
    target_core_api_base_url = "https://target.example"
    sync_webhook_base_url = None
    target_core_api_bearer_token = "target-secret"
    sync_ws_timeout_seconds = 5.0


@pytest.mark.asyncio
async def test_link_identidade_mailing_confirms_requested_uuid(monkeypatch) -> None:
    calls: list[dict] = []

    def _post_json(**kwargs):
        calls.append(kwargs)
        return 200, '{"data":[{"results":{"linked":["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],"errors":{}}}]}'

    monkeypatch.setattr(service, "_post_json", _post_json)

    result = await service.link_identidade_mailing_to_current_flow(
        settings=_Settings(),
        workspace_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        flow_uuid="cccccccc-cccc-cccc-cccc-cccccccccccc",
        mailing_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )

    assert result.success is True
    assert result.attempts == 1
    assert calls[0]["payload"]["call_origin"] == "identidade_person"
    assert calls[0]["payload"]["mailing_ids_removed"] == []
    assert calls[0]["headers"]["authorization"] == "Bearer target-secret"


@pytest.mark.asyncio
async def test_link_identidade_mailing_treats_http_200_partial_error_as_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "_post_json",
        lambda **_kwargs: (
            200,
            '{"data":[{"results":{"linked":[],"errors":{"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa":["Não autorizado."]}}}]}',
        ),
    )

    result = await service.link_identidade_mailing_to_current_flow(
        settings=_Settings(),
        workspace_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        flow_uuid="cccccccc-cccc-cccc-cccc-cccccccccccc",
        mailing_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )

    assert result.success is False
    assert result.reason == "target_core_link_not_confirmed"
    assert result.message == "Não autorizado."
