from __future__ import annotations

import base64

import pytest

import app.services.flow_builder_image as image_service
from app.schemas.orch_flow_builder import FlowBuilderImageInput


PNG_BYTES = b"\x89PNG\r\n\x1a\nflow-builder-test"


def _image(*, mime_type: str = "image/png", data: bytes = PNG_BYTES) -> FlowBuilderImageInput:
    return FlowBuilderImageInput(
        filename="diagrama.png",
        mime_type=mime_type,
        data_base64=base64.b64encode(data).decode("ascii"),
    )


def test_validate_image_accepts_png_and_exposes_only_safe_metadata() -> None:
    validated = image_service.validate_flow_builder_image(_image())

    assert validated.data == PNG_BYTES
    assert validated.mime_type == "image/png"
    assert validated.metadata["binary_persisted"] is False
    assert validated.metadata["size_bytes"] == len(PNG_BYTES)
    assert "data" not in validated.metadata


def test_validate_image_rejects_declared_mime_mismatch() -> None:
    with pytest.raises(image_service.FlowBuilderImageError) as exc_info:
        image_service.validate_flow_builder_image(_image(mime_type="image/jpeg"))

    assert exc_info.value.code == "image_mime_mismatch"


def test_validate_image_rejects_invalid_base64() -> None:
    image = FlowBuilderImageInput(
        filename="diagrama.png",
        mime_type="image/png",
        data_base64="not-base64!!",
    )

    with pytest.raises(image_service.FlowBuilderImageError) as exc_info:
        image_service.validate_flow_builder_image(image)

    assert exc_info.value.code == "image_invalid_base64"


def test_validate_image_rejects_decoded_payload_above_five_mib(monkeypatch) -> None:
    monkeypatch.setattr(image_service, "MAX_FLOW_BUILDER_IMAGE_BYTES", 8)

    with pytest.raises(image_service.FlowBuilderImageError) as exc_info:
        image_service.validate_flow_builder_image(_image(data=PNG_BYTES))

    assert exc_info.value.code == "image_too_large"


@pytest.mark.asyncio
async def test_extract_image_uses_multimodal_request_and_validates_contract(monkeypatch) -> None:
    captured = {}

    def fake_execute(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return {
            "parsed_json": {
                "summary": "Inicia, decide e encerra.",
                "steps": ["Início", "Encerrar"],
                "decisions": ["Aceite ou recusa"],
                "outcomes": ["Sucesso", "Insucesso"],
                "assumptions": [],
                "ambiguities": [],
            }
        }

    monkeypatch.setattr(image_service, "execute_otima_llm_prompt", fake_execute)
    validated = image_service.validate_flow_builder_image(_image())

    extraction = await image_service.extract_flow_builder_image(
        image=validated,
        instruction="Preserve os dois desfechos.",
        model="gpt-5",
        workspace_uuid="ba7eb0ec-e565-447c-8c11-8f870cf72a60",
        workspace_api_key="workspace-key",
        timeout_seconds=60,
    )

    assert extraction.summary == "Inicia, decide e encerra."
    assert captured["user_image_data_url"].startswith("data:image/png;base64,")
    assert captured["workspace_api_key"] == "workspace-key"
    assert "FlowPlan" in captured["system_prompt"]
