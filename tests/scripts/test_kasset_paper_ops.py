from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import scripts.kasset_paper_ops as cli


def test_promotion_approve_accepts_only_candidate_identity_and_reason() -> None:
    args = cli.parse_args(
        [
            "promotion-approve",
            "--candidate-id",
            "41",
            "--reason",
            "운영자 검토 완료",
        ]
    )

    assert args.candidate_id == 41
    assert args.reason == "운영자 검토 완료"
    assert not hasattr(args, "metrics")
    assert not hasattr(args, "evidence")

    with pytest.raises(SystemExit):
        cli.parse_args(
            [
                "promotion-approve",
                "--candidate-id",
                "41",
                "--reason",
                "운영자 검토 완료",
                "--total-return",
                "0.25",
            ]
        )


@pytest.mark.asyncio
async def test_promotion_approve_delegates_persisted_candidate_id_only(
    monkeypatch,
    capsys,
) -> None:
    args = cli.parse_args(
        [
            "promotion-approve",
            "--candidate-id",
            "41",
            "--reason",
            "운영자 검토 완료",
        ]
    )
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    calls: list[tuple[int, str]] = []

    class FakeService:
        def __init__(self, db: object) -> None:
            assert db is session

        async def approve_candidate(
            self,
            candidate_id: int,
            *,
            at: object,
            operator_reason: str,
        ) -> object:
            assert at is not None
            calls.append((candidate_id, operator_reason))
            return SimpleNamespace(
                as_evidence=lambda: {
                    "promotionCandidateId": candidate_id,
                    "state": "paper_approved",
                }
            )

    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(cli, "StrategyPromotionService", FakeService)

    rc = await cli.run(args)

    assert rc == 0
    assert calls == [(41, "운영자 검토 완료")]
    output = capsys.readouterr().out
    assert '"promotionCandidateId": 41' in output
    assert '"state": "paper_approved"' in output
