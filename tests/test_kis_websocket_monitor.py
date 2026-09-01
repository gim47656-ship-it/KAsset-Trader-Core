import logging

import pytest

from kis_websocket_monitor import REMOVAL_MESSAGE, main


@pytest.mark.unit
def test_legacy_kis_websocket_entrypoint_fails_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        exit_code = main()

    assert exit_code == 2
    assert REMOVAL_MESSAGE in caplog.text
