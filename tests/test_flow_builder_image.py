from __future__ import annotations

import base64
import io

import pytest
from PIL import Image, ImageDraw

import app.services.flow_builder_image as image_service
from app.schemas.orch_flow_builder import FlowBuilderImageInput


def _png_bytes(
    *,
    size: tuple[int, int] = (64, 32),
    transparent: bool = False,
    noisy: bool = False,
) -> bytes:
    if noisy:
        image = Image.effect_noise(size, 90).convert("RGBA" if transparent else "RGB")
    else:
        background = (255, 255, 255, 0) if transparent else "white"
        image = Image.new("RGBA" if transparent else "RGB", size, background)
        draw = ImageDraw.Draw(image)
        fill = (0, 87, 183, 255) if transparent else "#0057b7"
        draw.rectangle((4, 4, size[0] - 5, size[1] - 5), fill=fill)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


PNG_BYTES = _png_bytes()


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
    assert "transport_data" not in validated.metadata
    assert validated.transport_data == PNG_BYTES
    assert validated.transport_mime_type == "image/png"


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


def test_validate_image_rejects_corrupted_image_after_signature() -> None:
    corrupted = b"\x89PNG\r\n\x1a\nnot-a-real-png"

    with pytest.raises(image_service.FlowBuilderImageError) as exc_info:
        image_service.validate_flow_builder_image(_image(data=corrupted))

    assert exc_info.value.code == "image_invalid_content"


def test_validate_image_rejects_excessive_pixel_count(monkeypatch) -> None:
    monkeypatch.setattr(image_service, "MAX_FLOW_BUILDER_IMAGE_PIXELS", 100)

    with pytest.raises(image_service.FlowBuilderImageError) as exc_info:
        image_service.validate_flow_builder_image(_image(data=_png_bytes(size=(20, 20))))

    assert exc_info.value.code == "image_dimensions_too_large"


def test_large_transparent_png_is_optimized_only_for_transport(monkeypatch) -> None:
    original = _png_bytes(size=(1200, 700), transparent=True, noisy=True)
    monkeypatch.setattr(image_service, "MAX_FLOW_BUILDER_LLM_IMAGE_BYTES", 180_000)

    validated = image_service.validate_flow_builder_image(_image(data=original))

    assert validated.data == original
    assert validated.metadata["size_bytes"] == len(original)
    assert validated.transport_mime_type == "image/jpeg"
    assert len(validated.transport_data) <= 180_000
    assert validated.data_url.startswith("data:image/jpeg;base64,")
    with Image.open(io.BytesIO(validated.transport_data)) as transported:
        assert transported.mode == "RGB"
        assert transported.size[0] <= 1200
        assert transported.size[1] <= 700


def test_small_file_with_large_dimensions_is_resized_for_transport() -> None:
    original = _png_bytes(size=(2400, 80))

    validated = image_service.validate_flow_builder_image(_image(data=original))

    assert len(original) < image_service.MAX_FLOW_BUILDER_LLM_IMAGE_BYTES
    assert validated.data == original
    assert validated.transport_mime_type == "image/jpeg"
    with Image.open(io.BytesIO(validated.transport_data)) as transported:
        assert max(transported.size) <= image_service.MAX_FLOW_BUILDER_LLM_EDGE_PIXELS


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
