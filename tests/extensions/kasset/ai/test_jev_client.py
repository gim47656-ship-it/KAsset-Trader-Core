"""Vercel AI Gateway Jev 클라이언트의 wire 계약과 엄격한 응답 파서."""

from __future__ import annotations

import json

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


def _choice_payload(
    probabilities: dict[str, float],
    *,
    choice: str = "AGREE",
    confidence: float | None = 0.8,
    rounding: dict[str, int] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "answers": {
            "stance": {
                "type": "choice",
                "choice": choice,
                "probabilities": probabilities,
            }
        },
        "usage": {"inputTokens": 120, "outputTokens": 3},
    }
    if confidence is not None:
        payload["providerMetadata"] = {
            "typesafe": {"confidence": {"stance": confidence}}
        }
    if rounding is not None:
        payload["rounding"] = rounding
    return payload


def test_parse_boolean_answer_reads_probability_and_optional_confidence() -> None:
    judgment = parse_judgment(
        {
            "answers": {"relevant": {"type": "boolean", "probability": 0.12}},
            "usage": {"inputTokens": 10, "outputTokens": 1},
        },
        questions={"relevant": _BOOLEAN},
    )

    answer = judgment.boolean("relevant")
    assert answer.probability == pytest.approx(0.12)
    assert answer.confidence is None
    assert (judgment.input_tokens, judgment.output_tokens) == (10, 1)


def test_parse_choice_answer_reads_all_label_probabilities() -> None:
    judgment = parse_judgment(
        _choice_payload({"AGREE": 0.7, "DISAGREE": 0.2, "INSUFFICIENT": 0.1}),
        questions={"stance": _CHOICE},
    )

    answer = judgment.choice("stance")
    assert answer.choice == "AGREE"
    assert answer.probabilities["AGREE"] == pytest.approx(0.7)
    assert answer.confidence == pytest.approx(0.8)


def test_rounding_widens_the_probability_sum_tolerance() -> None:
    rounded = _choice_payload(
        {"AGREE": 0.34, "DISAGREE": 0.33, "INSUFFICIENT": 0.34},
        rounding={"probabilityDecimals": 2},
    )

    assert parse_judgment(rounded, questions={"stance": _CHOICE}).choice("stance")

    unrounded = _choice_payload({"AGREE": 0.34, "DISAGREE": 0.33, "INSUFFICIENT": 0.34})
    with pytest.raises(JevJudgmentError, match="sum to 1"):
        parse_judgment(unrounded, questions={"stance": _CHOICE})


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "answers": {"other": {"type": "boolean", "probability": 0.5}},
                "usage": {"inputTokens": 1, "outputTokens": 1},
            },
            "requested question ids",
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
                "usage": {"inputTokens": -1, "outputTokens": 1},
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
async def test_judge_sends_the_gateway_wire_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport(
        httpx.Response(
            200,
            json={
                "answers": {"relevant": {"type": "boolean", "probability": 0.9}},
                "usage": {"inputTokens": 5, "outputTokens": 1},
            },
        )
    )
    recorded = _patch(monkeypatch, transport)

    judgment = await JevClient(api_key="vck_secret").judge(
        state={"title": "t"},
        questions={"relevant": _BOOLEAN},
        feature="kasset_jev_news_relevance",
    )

    assert judgment.boolean("relevant").probability == pytest.approx(0.9)
    request = transport.requests[0]
    assert str(request.url) == JEV_BASE_URL
    assert request.headers["ai-model-id"] == JEV_MODEL_ID
    assert request.headers["ai-gateway-protocol-version"] == "0.0.1"
    assert request.headers["ai-gateway-auth-method"] == "api-key"
    assert request.headers["ai-evaluation-model-specification-version"] == "4"
    assert request.headers["authorization"] == "Bearer vck_secret"
    assert json.loads(request.content) == {
        "state": {"title": "t"},
        "questions": {"relevant": _BOOLEAN},
    }
    assert [attempt.status for attempt in recorded] == ["success"]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": {"message": "auth"}}),
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
        await JevClient(api_key="vck_secret").judge(
            state="s",
            questions={"relevant": _BOOLEAN},
            feature="kasset_jev_news_relevance",
        )

    assert "vck_secret" not in str(raised.value)
    assert [attempt.status for attempt in recorded] == ["failure"]  # type: ignore[attr-defined]


def test_missing_key_disables_jev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "KASSET_JEV_API_KEY", None)
    assert build_jev_client() is None

    monkeypatch.setattr(settings, "KASSET_JEV_API_KEY", SecretStr("   "))
    assert build_jev_client() is None

    monkeypatch.setattr(settings, "KASSET_JEV_API_KEY", SecretStr("vck_secret"))
    assert isinstance(build_jev_client(), JevClient)
