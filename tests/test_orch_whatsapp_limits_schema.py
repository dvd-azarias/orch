from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.orch import OrchWhatsappLimitUpsertRequest


def test_orch_whatsapp_limit_request_accepts_minus_one() -> None:
    model = OrchWhatsappLimitUpsertRequest(phone="5511999999999", allowed_limit=-1)
    assert model.allowed_limit == -1


def test_orch_whatsapp_limit_request_rejects_below_minus_one() -> None:
    with pytest.raises(ValidationError):
        OrchWhatsappLimitUpsertRequest(phone="5511999999999", allowed_limit=-2)
