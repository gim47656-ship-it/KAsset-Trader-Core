"""QA용 Android access token 자동 갱신기.

유효한 access token 을 표준출력으로 낸다. 만료가 가까우면 먼저
``POST /auth/refresh`` 로 갱신한 뒤 낸다. 실측 중 401 이 떠서 손으로 다시
발급하는 왕복을 없애는 것이 목적이다.

    export TOK=$(python scripts/kasset_qa_token.py)
    curl -H "Authorization: Bearer $TOK" "$BASE/market/quotes?..."

앱 의존성과 DB 가 필요 없다. 표준 라이브러리만 쓰므로 집 PC 에서 그대로 돈다.
서버 SSH 는 최초 1회 refresh token 을 심을 때만 필요하다:

    # 서버에서 1회
    docker compose ... exec -T api /app/.venv/bin/python \
        scripts/mint_android_qa_token.py
    # 로컬에서 1회 (위 출력의 refreshToken)
    python scripts/kasset_qa_token.py --seed-refresh <refreshToken>

access 는 30분, refresh 는 7일이고 갱신마다 refresh 가 회전한다. 회전된 값을
캐시에 즉시 덮어써야 다음 갱신이 성공하므로, 이 스크립트는 응답을 받자마자
저장한다. 따라서 최초 1회 이후 7일간은 SSH 없이 무한히 갱신된다.

캐시 파일에는 살아 있는 refresh token 이 들어간다. 저장소 안에 두지 않고
홈 디렉터리에 0600 으로 쓴다. 절대 커밋하지 않는다.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_DEFAULT_BASE = "https://175-45-201-51.sslip.io/api/v1"
_DEFAULT_CACHE = Path.home() / ".kasset-qa-token.json"
# access 가 30분이므로 2분 여유면 한 번의 실측 명령이 도중에 만료되지 않는다.
_REFRESH_MARGIN_SECONDS = 120


def _cache_path() -> Path:
    override = os.environ.get("KASSET_QA_TOKEN_CACHE")
    return Path(override) if override else _DEFAULT_CACHE


def _load() -> dict[str, str]:
    path = _cache_path()
    if not path.exists():
        raise SystemExit(
            f"{path} 가 없다. 먼저 --seed-refresh <refreshToken> 으로 심어라."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _save(data: dict[str, str]) -> None:
    """캐시를 0600으로 원자적으로 쓴다.

    권한을 나중에 chmod 하면 생성 직후 한순간 다른 사용자가 읽을 수 있는 창이
    생긴다. 그래서 ``os.open`` 으로 처음부터 0600 으로 만든다. 또 갱신은
    refresh token 을 회전시키므로, 쓰는 중에 죽으면 회전된 값을 잃고 다시
    심어야 한다. 임시 파일에 먼저 쓰고 ``os.replace`` 로 갈아끼워 반쯤 쓰인
    파일이 남지 않게 한다.
    """
    path = _cache_path()
    payload = json.dumps(data, ensure_ascii=False)
    temp = path.with_name(path.name + ".tmp")
    descriptor = os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    os.replace(temp, path)


def _jwt_expiry(token: str) -> int:
    """JWT ``exp`` 를 읽는다. 서명은 검증하지 않는다(갱신 시점 판단용이다)."""
    payload_segment = token.split(".")[1]
    padded = payload_segment + "=" * (-len(payload_segment) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))
    return int(payload["exp"])


def _refresh(base: str, refresh_token: str) -> dict[str, str]:
    request = urllib.request.Request(
        f"{base.rstrip('/')}/auth/refresh",
        data=json.dumps({"refreshToken": refresh_token}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        raise SystemExit(
            f"refresh 실패 {exc.code}: {detail}\n"
            "refresh 가 만료(7일)됐거나 세션이 폐기됐다. "
            "서버에서 mint_android_qa_token.py 로 다시 심어라."
        ) from exc
    return body


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", default=os.environ.get("KASSET_QA_BASE", _DEFAULT_BASE)
    )
    parser.add_argument(
        "--seed-refresh",
        metavar="TOKEN",
        help="mint_android_qa_token.py 가 낸 refreshToken 을 캐시에 심는다.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="만료가 남아 있어도 갱신한다.",
    )
    args = parser.parse_args()

    if args.seed_refresh:
        _save({"refreshToken": args.seed_refresh})
        print(f"{_cache_path()} 에 심었다.", flush=True)

    cached = _load()
    access_token = cached.get("accessToken")

    needs_refresh = args.force or not access_token
    if not needs_refresh and access_token is not None:
        remaining = _jwt_expiry(access_token) - int(time.time())
        needs_refresh = remaining <= _REFRESH_MARGIN_SECONDS

    if needs_refresh:
        body = _refresh(args.base, cached["refreshToken"])
        # 회전된 refresh 를 즉시 덮어쓴다. 놓치면 다음 갱신이 401 이 된다.
        cached = {
            "accessToken": body["accessToken"],
            "refreshToken": body["refreshToken"],
        }
        try:
            _save(cached)
        except OSError as exc:
            # 서버 쪽 refresh 는 이미 회전됐다. 캐시에 못 남기면 직전 값은
            # 죽었으므로 그냥 죽으면 SSH 재발급밖에 남지 않는다. 회전된 값을
            # stderr 로 내보내 --seed-refresh 로 복구할 수 있게 한다.
            print(f"캐시 저장 실패: {exc}", file=sys.stderr)
            print(
                "아래 refreshToken 을 --seed-refresh 로 다시 심어라(직전 값은 죽었다):",
                file=sys.stderr,
            )
            print(cached["refreshToken"], file=sys.stderr)
        access_token = cached["accessToken"]

    print(access_token)


if __name__ == "__main__":
    main()
