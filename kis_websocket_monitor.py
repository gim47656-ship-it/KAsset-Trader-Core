#!/usr/bin/env python3
"""Removed KIS execution WebSocket entrypoint.

KIS adapters may remain for historical ledger compatibility, but this executable
must never initialize them. Toss fills are confirmed by the scheduled reconcile
poller instead of a WebSocket.
"""

import logging
import sys

logger = logging.getLogger(__name__)

REMOVAL_MESSAGE = (
    "KIS websocket monitoring is not operational; "
    "use toss_live.poll_fills_periodic for Toss fill evidence"
)


def main() -> int:
    """Fail closed if a legacy deployment still invokes this entrypoint."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger.error(REMOVAL_MESSAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
