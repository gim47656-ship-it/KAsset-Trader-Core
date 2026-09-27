"""OpenRouter Decisions API의 Jev 판정 클라이언트와 엄격한 응답 파서.

코어는 이 판정을 두 곳에만 쓴다.

* 뉴스 요약 전 관련성 선별 — 통과 기사를 codex 요약 호출로 넘기기 직전에
  yes/no(``noul``) ``market_relevant`` 확률을 본다.
* 매수 후보 AI 가산점 — codex 검토 verdict를 얻은 후보의 choice ``stance``
  확률 P(AGREE)를 ``ai_bonus``로 쓴다.

전송은 ``httpx``다. 런타임 in-process LLM 경계(ROB-501)와 무관하게 OpenRouter의
HTTP ``/api/alpha/decisions`` 엔드포인트만 호출하며 broker/계좌 자격이나 주문
정보를 보내지 않는다. 판정은 확률·확신도만 돌려주고 주문·수량·손절·Hard Risk를
결정하지 않는다.

실패는 HTTP non-2xx, timeout, 연결 실패, 응답 형식 위반을 모두
:class:`JevJudgmentError` 하나로 올린다. 호출부는 이 예외를 fail-open으로 처리해
판정이 없던 기존 동작을 유지한다. 재시도는 하지 않는다.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import perf_counter
from typing import Final

import httpx

from app.core.config import settings
from app.models.ai_call_events import COST_SOURCE_PROVIDER_REPORTED
from app.services.ai_usage_service import (
    AiAttemptTelemetry,
    AiCallAttempt,
    AiCallStatus,
    capture_ai_attempt,
    current_ai_call_attribution,
    new_logical_call_id,
    record_ai_call_attempts,
    report_ai_attempt_http_status,
    report_ai_attempt_usage,
)

logger = logging.getLogger(__name__)

#: wire 계약(고정값). 모델은 판정 확률 임계값(뉴스 P<0.2)이 흔들리지 않도록
#: 버전을 고정한다. 새 버전으로 올릴 때는 이 값과 임계값을 함께 본다.
JEV_BASE_URL: Final = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL_ID: Final = "typesafe/jev-1.13"

#: AI 호출 원장에 남기는 provider/route 이름. transport가 하나뿐이라 같다.
JEV_PROVIDER_NAME: Final = "openrouter-jev"

#: 원장 ``feature`` 값. 호출처마다 고정 문자열 하나를 쓴다.
JEV_FEATURE_NEWS_RELEVANCE: Final = "kasset_jev_news_relevance"
JEV_FEATURE_CANDIDATE_STANCE: Final = "kasset_jev_candidate_stance"

#: OpenRouter는 choice 확률을 소수 둘째 자리로 반올림해 돌려준다. 확률 합의
#: 허용오차는 label마다 반올림 오차 절반씩이다.
_JEV_PROBABILITY_DECIMALS: Final = 2
#: OpenRouter usage의 ``cost``는 USD 금액이다.
_JEV_COST_CURRENCY: Final = "USD"


class JevJudgmentError(RuntimeError):
    """Jev 판정 실패. HTTP 오류·timeout·응답 형식 위반을 하나로 묶는다."""


@dataclass(frozen=True, slots=True)
class JevBooleanAnswer:
    """yes/no(``noul``) 질문의 답. ``probability``는 true일 확률이다."""

    probability: float


@dataclass(frozen=True, slots=True)
class JevChoiceAnswer:
    """choice 질문의 답. ``probabilities``는 criteria 모든 label의 확률이다."""

    choice: str
    probabilities: Mapping[str, float]
    confidence: float


@dataclass(frozen=True, slots=True)
class JevJudgment:
    """판정 1회의 파싱 결과. ``answers`` 키 집합은 요청한 질문 id와 같다."""

    answers: Mapping[str, JevBooleanAnswer | JevChoiceAnswer]
    input_tokens: int
    output_tokens: int
    #: provider가 보고한 USD 비용. 없거나 형식이 어긋나면 ``None``이다.
    cost_usd: Decimal | None = None

    def boolean(self, question_id: str) -> JevBooleanAnswer:
        answer = self.answers.get(question_id)
        if not isinstance(answer, JevBooleanAnswer):
            raise JevJudgmentError(f"{question_id} is not a boolean answer")
        return answer

    def choice(self, question_id: str) -> JevChoiceAnswer:
        answer = self.answers.get(question_id)
        if not isinstance(answer, JevChoiceAnswer):
            raise JevJudgmentError(f"{question_id} is not a choice answer")
        return answer


def boolean_question(
    *,
    instructions: str,
    true_criterion: str,
    false_criterion: str,
) -> dict[str, object]:
    """true 확률을 묻는 yes/no 질문 하나를 wire(``noul``) 형태로 만든다."""

    return {
        "type": "noul",
        "instructions": instructions,
        "criteria": {"true": true_criterion, "false": false_criterion},
    }


def choice_question(
    *,
    instructions: str,
    criteria: Mapping[str, str],
) -> dict[str, object]:
    """label 중 하나를 고르는 choice 질문 하나를 wire 형태로 만든다."""

    if len(criteria) < 2:
        raise ValueError("a choice question requires at least two labels")
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": dict(criteria),
    }


def _probability(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise JevJudgmentError(f"{field} must be a probability between 0 and 1")
    probability = float(value)
    if not 0.0 <= probability <= 1.0:
        raise JevJudgmentError(f"{field} must be a probability between 0 and 1")
    return probability


def _non_negative_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise JevJudgmentError(f"{field} must be a non-negative integer")
    return value


def _reported_cost(value: object) -> Decimal | None:
    """usage ``cost``를 받는다. 계측값이라 형식이 어긋나도 판정은 실패시키지 않는다."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return Decimal(str(value))


def _choice_labels(question: Mapping[str, object]) -> tuple[str, ...]:
    criteria = question.get("criteria")
    if not isinstance(criteria, Mapping):
        raise JevJudgmentError("choice questions require a criteria object")
    labels = tuple(str(label) for label in criteria)
    if not labels:
        raise JevJudgmentError("choice questions require at least one label")
    return labels


def _parse_boolean_answer(question_id: str, raw: object) -> JevBooleanAnswer:
    if not isinstance(raw, Mapping) or raw.get("type") != "noul":
        raise JevJudgmentError(f"{question_id} must be answered as a noul")
    return JevBooleanAnswer(
        probability=_probability(raw.get("noul"), field=f"{question_id}.noul"),
    )


def _parse_choice_answer(
    question_id: str,
    raw: object,
    labels: tuple[str, ...],
) -> JevChoiceAnswer:
    if not isinstance(raw, Mapping) or raw.get("type") != "choice":
        raise JevJudgmentError(f"{question_id} must be answered as a choice")
    raw_probabilities = raw.get("probabilities")
    if not isinstance(raw_probabilities, Mapping):
        raise JevJudgmentError(f"{question_id}.probabilities must be an object")
    if set(raw_probabilities) != set(labels):
        raise JevJudgmentError(f"{question_id}.probabilities must be the labels")
    probabilities = {
        label: _probability(raw_probabilities[label], field=f"{question_id}.{label}")
        for label in labels
    }
    tolerance = len(labels) * 0.5 / (10**_JEV_PROBABILITY_DECIMALS)
    if abs(sum(probabilities.values()) - 1.0) > tolerance:
        raise JevJudgmentError(f"{question_id}.probabilities must sum to 1")
    choice = raw.get("choice")
    if not isinstance(choice, str) or choice not in probabilities:
        raise JevJudgmentError(f"{question_id}.choice must be a label")
    if probabilities[choice] != max(probabilities.values()):
        raise JevJudgmentError(f"{question_id}.choice must be the largest")
    if raw.get("confidence") is None:
        raise JevJudgmentError(f"{question_id} is missing a confidence value")
    return JevChoiceAnswer(
        choice=choice,
        probabilities=probabilities,
        confidence=_probability(
            raw.get("confidence"), field=f"{question_id}.confidence"
        ),
    )


def parse_judgment(
    payload: object,
    *,
    questions: Mapping[str, Mapping[str, object]],
) -> JevJudgment:
    """응답 JSON을 wire 계약 그대로 해석한다. 형식 위반은 모두 실패다.

    ``answers`` 키 집합은 요청한 질문 id와 정확히 같아야 하고, usage의 두 token
    수는 비음수 정수여야 한다. choice 답은 모든 criteria label의 확률을 담고 합이
    1(반올림 허용오차 안)이어야 하며, ``choice``는 최대확률 label이고
    ``confidence``가 있어야 한다. ``noul`` 답은 true 확률 하나만 담는다.
    """

    if not isinstance(payload, Mapping):
        raise JevJudgmentError("response must be a JSON object")
    answers = payload.get("answers")
    if not isinstance(answers, Mapping):
        raise JevJudgmentError("response is missing an answers object")
    if set(answers) != set(questions):
        raise JevJudgmentError("answers must carry the requested question ids")
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        raise JevJudgmentError("response is missing a usage object")
    input_tokens = _non_negative_int(usage.get("input_tokens"), field="input_tokens")
    output_tokens = _non_negative_int(
        usage.get("output_tokens"), field="output_tokens"
    )

    parsed: dict[str, JevBooleanAnswer | JevChoiceAnswer] = {}
    for question_id, question in questions.items():
        question_type = question.get("type")
        raw = answers[question_id]
        if question_type == "noul":
            parsed[question_id] = _parse_boolean_answer(question_id, raw)
        elif question_type == "choice":
            parsed[question_id] = _parse_choice_answer(
                question_id, raw, _choice_labels(question)
            )
        else:
            raise JevJudgmentError(f"unsupported question type: {question_type!r}")
    return JevJudgment(
        answers=parsed,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=_reported_cost(usage.get("cost")),
    )


def _attempt_row(
    *,
    logical_call_id: str,
    started_at: datetime,
    started_perf: float,
    feature: str,
    model_id: str,
    status: AiCallStatus,
    error_type: str | None,
    telemetry: AiAttemptTelemetry,
) -> AiCallAttempt:
    """판정 1회의 원장 행. ``finished_at``은 단조시계로만 계산한다."""

    latency_ms = max(0, round((perf_counter() - started_perf) * 1000))
    attribution = current_ai_call_attribution()
    return AiCallAttempt(
        logical_call_id=logical_call_id,
        attempt_no=1,
        started_at=started_at,
        finished_at=started_at + timedelta(milliseconds=latency_ms),
        latency_ms=latency_ms,
        feature=feature,
        route_name=JEV_PROVIDER_NAME,
        provider=JEV_PROVIDER_NAME,
        model_name=model_id,
        status=status,
        error_type=error_type,
        telemetry=telemetry,
        owner_user_id=attribution.owner_user_id,
        correlation_id=attribution.correlation_id,
    )


class JevClient:
    """OpenRouter ``/api/alpha/decisions`` 한 엔드포인트를 호출하는 transport."""

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float = 5.0,
        base_url: str = JEV_BASE_URL,
        model_id: str = JEV_MODEL_ID,
    ) -> None:
        normalized_key = api_key.strip()
        if not normalized_key:
            raise ValueError("Jev api_key is required")
        if not base_url.strip():
            raise ValueError("Jev base_url is required")
        self._api_key = normalized_key
        self._base_url = base_url.strip()
        self._model_id = model_id.strip() or JEV_MODEL_ID
        self._timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return JEV_PROVIDER_NAME

    async def judge(
        self,
        *,
        state: object,
        questions: Mapping[str, Mapping[str, object]],
        feature: str,
    ) -> JevJudgment:
        """질문 묶음을 한 번의 HTTP 호출로 판정한다. 실패는 예외 하나다."""

        if not questions:
            raise ValueError("at least one Jev question is required")
        normalized_feature = feature.strip()
        if not normalized_feature:
            raise ValueError("Jev feature is required")
        body: dict[str, object] = {
            "model": self._model_id,
            "state": state,
            "questions": {key: dict(value) for key, value in questions.items()},
        }
        logger.info(
            "KAsset Jev judgment attempt provider=%s model=%s feature=%s",
            JEV_PROVIDER_NAME,
            self._model_id,
            normalized_feature,
        )
        logical_call_id = new_logical_call_id()
        started_at = datetime.now(UTC)
        started_perf = perf_counter()
        status: AiCallStatus = "failure"
        error_type: str | None = None
        attempts: list[AiCallAttempt] = []
        try:
            with capture_ai_attempt() as telemetry:
                try:
                    payload = await self._post(body)
                    judgment = parse_judgment(payload, questions=questions)
                    cost = judgment.cost_usd
                    report_ai_attempt_usage(
                        prompt_tokens=judgment.input_tokens,
                        completion_tokens=judgment.output_tokens,
                        total_tokens=judgment.input_tokens + judgment.output_tokens,
                        cost_amount=cost,
                        cost_currency=_JEV_COST_CURRENCY if cost is not None else None,
                        cost_source=(
                            COST_SOURCE_PROVIDER_REPORTED if cost is not None else None
                        ),
                    )
                except Exception as exc:
                    # 원장에는 bounded classifier만 남긴다. provider 본문은 요청
                    # header를 되돌려줄 수 있어 절대 넘기지 않는다.
                    error_type = type(exc).__name__
                    raise
                else:
                    status = "success"
                    return judgment
                finally:
                    attempts.append(
                        _attempt_row(
                            logical_call_id=logical_call_id,
                            started_at=started_at,
                            started_perf=started_perf,
                            feature=normalized_feature,
                            model_id=self._model_id,
                            status=status,
                            error_type=error_type,
                            telemetry=telemetry,
                        )
                    )
        finally:
            # 계측은 관문이 아니다. 이 호출은 자기 실패를 삼키므로 성공 payload와
            # 올라가는 예외 어느 쪽도 원장 쓰기 때문에 바뀌지 않는다.
            await record_ai_call_attempts(attempts)

    async def _post(self, body: Mapping[str, object]) -> object:
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {self._api_key}",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    self._base_url,
                    json=body,
                    headers=headers,
                )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise JevJudgmentError(f"jev unreachable: {type(exc).__name__}") from exc

        report_ai_attempt_http_status(response.status_code)
        if not response.is_success:
            raise JevJudgmentError(f"jev rejected: HTTP {response.status_code}")

        try:
            return response.json()
        except ValueError as exc:
            raise JevJudgmentError("jev returned a malformed JSON body") from exc


def build_jev_client() -> JevClient | None:
    """설정된 Jev transport를 만든다. 키가 없으면 ``None``(=비활성)이다."""

    secret = settings.KASSET_JEV_API_KEY
    api_key = secret.get_secret_value().strip() if secret is not None else ""
    if not api_key:
        return None
    return JevClient(
        api_key=api_key,
        timeout_seconds=settings.KASSET_JEV_TIMEOUT_SECONDS,
    )


__all__ = [
    "JEV_BASE_URL",
    "JEV_FEATURE_CANDIDATE_STANCE",
    "JEV_FEATURE_NEWS_RELEVANCE",
    "JEV_MODEL_ID",
    "JEV_PROVIDER_NAME",
    "JevBooleanAnswer",
    "JevChoiceAnswer",
    "JevClient",
    "JevJudgment",
    "JevJudgmentError",
    "boolean_question",
    "build_jev_client",
    "choice_question",
    "parse_judgment",
]
