"""QA용 Android 세션 토큰 쌍 발급기.

실기기·curl 로 앱 API를 실측할 때 access/refresh 쌍을 얻는 개발 도구다.
컨테이너 안에서 실행한다(앱 의존성과 DB 접근이 필요하다).

    docker compose --env-file .env.kasset -f docker-compose.kasset.yml \
        exec -T api /app/.venv/bin/python scripts/mint_android_qa_token.py

토큰 claim 을 손으로 조립하지 않고 운영 경로인 ``MobileAuthService._issue`` 를
그대로 호출한다. ``get_mobile_session`` 게이트가 요구하는 claim(``sub``, ``uid``,
``deviceId``, ``sessionId``, ``client``, ``type``)과 ``kasset_device_sessions``
행의 ``refresh_token_hash`` 회전이 운영과 동일하게 유지되므로, 손으로 만든
토큰이 게이트 변경 때마다 조용히 401 이 되는 문제가 없다.

기본 ``device_id`` 는 실기기와 겹치지 않는 전용 값이다. 실기기 세션 행을
빼앗지 않으므로 휴대폰이 로그아웃되지 않는다.

만료 없는 토큰은 만들지 않는다. access 는 짧게 두고, 7일짜리 refresh 로
``POST /auth/refresh`` 를 돌려 끊김 없이 쓰는 것이 의도된 사용법이다.
갱신은 SSH 없이 순수 HTTP 로 가능하다 - ``scripts/kasset_qa_token.py`` 를 쓴다.

출력에는 살아 있는 refresh token 이 들어 있다. 터미널에 그대로 띄우지 말고
파일로 리다이렉트해라. 셸 히스토리·CI 로그·스크롤백에 남으면 7일 동안
유효한 자격이 그대로 노출된다.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.extensions.kasset.api.auth import mobile_auth
from app.models.trading import User

_DEFAULT_DEVICE_ID = "qa-cli"
_DEFAULT_DEVICE_NAME = "QA CLI"


async def _mint(*, user_id: int, device_id: str, device_name: str) -> dict[str, str]:
    async with AsyncSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        if user is None:
            raise SystemExit(f"user_id={user_id} 사용자가 없다.")

        tokens = await mobile_auth._issue(  # noqa: SLF001 - 운영 발급 경로를 그대로 쓴다.
            db,
            user,
            device_id=device_id,
            device_name=device_name,
        )
        await db.commit()

    return {
        "accessToken": tokens.access_token,
        "refreshToken": tokens.refresh_token,
        "accessTokenExpiresAt": tokens.access_token_expires_at,
        "refreshTokenExpiresAt": tokens.refresh_token_expires_at,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--user-id", type=int, default=4, help="토큰을 발급할 사용자 id"
    )
    parser.add_argument("--device-id", default=_DEFAULT_DEVICE_ID)
    parser.add_argument("--device-name", default=_DEFAULT_DEVICE_NAME)
    parser.add_argument(
        "--access-only",
        action="store_true",
        help="access token 만 출력한다(파이프로 바로 쓰기 위한 옵션).",
    )
    args = parser.parse_args()

    tokens = asyncio.run(
        _mint(
            user_id=args.user_id,
            device_id=args.device_id,
            device_name=args.device_name,
        )
    )

    if args.access_only:
        print(tokens["accessToken"])
        return
    print(json.dumps(tokens, ensure_ascii=False))


if __name__ == "__main__":
    main()
