#!/usr/bin/env python3
"""보관된 KIS 실행 진입점. 현재 운영에서 사용할 수 없다."""

from __future__ import annotations

import sys

_DISABLED_MESSAGE = "archived KIS entrypoint is disabled: rob278_kr_dryrun"


def main(*_args: object, **_kwargs: object) -> int:
    print(_DISABLED_MESSAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
