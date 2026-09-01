from __future__ import annotations

import pytest

from app.core.config import Settings

pytestmark = pytest.mark.unit


def test_obsolete_kis_toss_precedence_flags_are_not_registered() -> None:
    settings = Settings()

    assert not hasattr(settings, "invest_quotes_toss_first_kr")
    assert not hasattr(settings, "invest_quotes_toss_first_us")
