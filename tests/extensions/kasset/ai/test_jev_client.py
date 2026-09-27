"""OpenRouter Decisions API Jev 클라이언트의 wire 계약과 엄격한 응답 파서.

payload 모양은 2026-09-27 실제 ``/api/alpha/decisions`` 응답을 따른다.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.extensions.kasset.ai import jev_client as jev_module
from app.extensions.kasset.ai.jev_client import (
    JEV_BASE_URL,
    JEV_MODEL_ID,
    JevClient,
    JevJudgmentError,
    boolean_question,
    build_jev_client,
    choice_question,
    parse_judgment,
)

_BOOLEAN = boolean_question(
    instructions="relevant?",
    true_criterion="yes",
    false_criterion="no",
)
_CHOICE = choice_question(
    instructions="stance?",
    criteria={"AGREE": "a", "DISAGREE": "d", "INSUFFICIENT": "i"},
)
_USAGE = {"input_tokens": 410, "output_tokens": 45, "cost": 0.00001722}


def _choice_payload(
    probabilities: dict[str, float],
    *,
    choice: str = "AGREE",
    confidence: float | None = 0.8,
) -> dict[str, object]:
    answer: dict[str, object] = {
        "type": "choice",
        "choice": choice,
        "probabilities": probabilities,
    }
    if confidence is not None:
        answer["confidence"] = confidence
    return {
        "model": "typesafe/jev-1.13-20260917",
        "answers": {"stance": answer},
        "usage": dict(_USAGE),
    }


def test_parse_noul_answer_reads_probability_and_reported_cost() -> None:
    judgment = parse_judgment(
        {
            "answers": {"relevant": {"type": "noul", "noul": 0.13}},
            "usage": {"input_tokens": 345, "output_tokens": 23, "cost": 0.00001449},
        },
        questions={"relevant": _BOOLEAN},
    )

    assert judgment.boolean("relevant").probability == pytest.approx(0.13)
    assert (judgment.input_tokens, judgment.output_tokens) == (345, 23)
    assert judgment.cost_usd == Decimal("0.00001449")


def test_parse_choice_answer_reads_all_label_probabilities() -> None:
    judgment = parse_judgment(
        _choice_payload({"DISAGREE": 0, "INSUFFICIENT": 0.01, "AGREE": 0.99}),
        questions={"stance": _CHOICE},
    )

    answer = judgment.choice("stance")
    assert answer.choice == "AGREE"
    assert answer.probabilities["AGREE"] == pytest.approx(0.99)
    assert answer.confidence == pytest.approx(0.8)


def test_two_decimal_rounding_is_within_the_sum_tolerance() -> None:
    # 실제 응답: 0.19 + 0.5599999999999999 + 0.25, 반올림 합이 1을 조금 벗어난 경우.
    for probabilities in (
        {"AGREE": 0.19, "DISAGREE": 0.5599999999999999, "INSUFFICIENT": 0.25},
        {"AGREE": 0.34, "DISAGREE": 0.33, "INSUFFICIENT": 0.34},
    ):
        choice = max(probabilities, key=probabilities.__getitem__)
        payload = _choice_payload(probabilities, choice=choice)
        assert parse_judgment(payload, questions={"stance": _CHOICE}).choice("stance")


def test_malformed_cost_does_not_fail_the_judgment() -> None:
    payload = _choice_payload({"AGREE": 0.7, "DISAGREE": 0.2, "INSUFFICIENT": 0.1})
    payload["usage"] = {"input_tokens": 1, "output_tokens": 1, "cost": "free"}

    assert parse_judgment(payload, questions={"stance": _CHOICE}).cost_usd is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "answers": {"other": {"type": "noul", "noul": 0.5}},
                "usage": dict(_USAGE),
            },
            "requested question ids",
        ),
        (
            {
                "answers": {"relevant": {"type": "boolean", "probability": 0.5}},
                "usage": dict(_USAGE),
            },
            "answered as a noul",
        ),
        (
            {
                "answers": {"relevant": {"type": "noul", "noul": 1.2}},
                "usage": dict(_USAGE),
            },
            "between 0 and 1",
        ),
        (
            _choice_payload({"AGREE": 0.7, "DISAGREE": 0.2}),
            "must be the labels",
        ),
        (
            _choice_payload({"AGREE": 0.6, "DISAGREE": 0.2, "INSUFFICIENT": 0.1}),
            "sum to 1",
        ),
        (
            _choice_payload(
                {"AGREE": 0.2, "DISAGREE": 0.7, "INSUFFICIENT": 0.1},
                choice="AGREE",
            ),
            "largest",
        ),
        (
            _choice_payload(
                {"AGREE": 0.7, "DISAGREE": 0.2, "INSUFFICIENT": 0.1},
                confidence=None,
            ),
            "confidence",
        ),
        (
            {
                "answers": {"stance": {"type": "choice"}},
                "usage": {"input_tokens": -1, "output_tokens": 1},
            },
            "non-negative",
        ),
    ],
)
def test_malformed_responses_are_judgment_failures(
    payload: dict[str, object],
    message: str,
) -> None:
    questions = (
        {"stance": _CHOICE}
        if "stance" in payload.get("answers", {})  # type: ignore[operator]
        else {"relevant": _BOOLEAN}
    )
    with pytest.raises(JevJudgmentError, match=message):
        parse_judgment(payload, questions=questions)


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, response: httpx.Response | Exception) -> None:
        self._response = response
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _patch(monkeypatch: pytest.MonkeyPatch, transport: _Transport) -> list[object]:
    original_init = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    recorded: list[object] = []

    async def record(attempts: object) -> bool:
        recorded.extend(attempts)  # type: ignore[arg-type]
        return True

    monkeypatch.setattr(jev_module, "record_ai_call_attempts", record)
    return recorded


@pytest.mark.asyncio
async def test_judge_sends_the_decisions_wire_contract_and_records_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport(
        httpx.Response(
            200,
            json={
                "model": "typesafe/jev-1.13-20260917",
                "answers": {"relevant": {"type": "noul", "noul": 0.95}},
                "usage": {"input_tokens": 350, "output_tokens": 23, "cost": 0.0000147},
                "provider": "TypeSafe",
            },
        )
    )
    recorded = _patch(monkeypatch, transport)

    judgment = await JevClient(api_key="sk-or-secret").judge(
        state={"title": "t"},
        questions={"relevant": _BOOLEAN},
        feature="kasset_jev_news_relevance",
    )

    assert judgment.boolean("relevant").probability == pytest.approx(0.95)
    request = transport.requests[0]
    assert str(request.url) == JEV_BASE_URL
    assert request.headers["authorization"] == "Bearer sk-or-secret"
    assert json.loads(request.content) == {
        "model": JEV_MODEL_ID,
        "state": {"title": "t"},
        "questions": {"relevant": _BOOLEAN},
    }
    (attempt,) = recorded
    assert attempt.status == "success"  # type: ignore[attr-defined]
    assert attempt.provider == "openrouter-jev"  # type: ignore[attr-defined]
    telemetry = attempt.telemetry  # type: ignore[attr-defined]
    assert (telemetry.prompt_tokens, telemetry.completion_tokens) == (350, 23)
    assert telemetry.cost_amount == Decimal("0.0000147")
    assert telemetry.cost_currency == "USD"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": {"message": "auth"}}),
        httpx.Response(429, json={"error": {"message": "rate limited"}}),
        httpx.Response(503, text="unavailable"),
        httpx.Response(200, text="not json"),
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("down"),
    ],
)
@pytest.mark.asyncio
async def test_transport_failures_raise_one_error_and_never_leak_the_key(
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response | Exception,
) -> None:
    recorded = _patch(monkeypatch, _Transport(response))

    with pytest.raises(JevJudgmentError) as raised:
        await JevClient(api_key="sk-or-secret").judge(
            state="s",
            questions={"relevant": _BOOLEAN},
            feature="kasset_jev_news_relevance",
        )

    assert "sk-or-secret" not in str(raised.value)
    assert [attempt.status for attempt in recorded] == ["failure"]  # type: ignore[attr-defined]


def test_missing_key_disables_jev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "KASSET_JEV_API_KEY", None)
    assert build_jev_client() is None

    monkeypatch.setattr(settings, "KASSET_JEV_API_KEY", SecretStr("   "))
    assert build_jev_client() is None

    monkeypatch.setattr(settings, "KASSET_JEV_API_KEY", SecretStr("sk-or-secret"))
    assert isinstance(build_jev_client(), JevClient)
