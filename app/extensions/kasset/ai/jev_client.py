"""Vercel AI Gateway의 Jev 판정 클라이언트와 엄격한 응답 파서.

코어는 이 판정을 두 곳에만 쓴다.

* 뉴스 요약 전 관련성 선별 — 통과 기사를 codex 요약 호출로 넘기기 직전에
  boolean ``market_relevant`` 확률을 본다.
* 매수 후보 AI 가산점 — codex 검토 verdict를 얻은 후보의 choice ``stance``
  확률 P(AGREE)를 ``ai_bonus``로 쓴다.

전송은 ``httpx``다. 런타임 in-process LLM 경계(ROB-501)와 무관하게 게이트웨이의
HTTP ``evaluation-model`` 엔드포인트만 호출하며 broker/계좌 자격이나 주문 정보를
보내지 않는다. 판정은 확률·확신도만 돌려주고 주문·수량·손절·Hard Risk를 결정하지
않는다.

실패는 HTTP non-2xx, timeout, 연결 실패, 응답 형식 위반을 모두
:class:`JevJudgmentError` 하나로 올린다. 호출부는 이 예외를 fail-open으로 처리해
판정이 없던 기존 동작을 유지한다. 재시도는 하지 않는다.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Final

import httpx

from app.core.config import settings
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

#: wire 계약(고정값). gateway가 버전을 올리면 이 값들만 바뀐다.
JEV_BASE_URL: Final = "https://ai-gateway.vercel.sh/v4/ai/evaluation-model"
JEV_MODEL_ID: Final = "typesafe-ai/jev"
JEV_PROTOCOL_VERSION: Final = "0.0.1"
JEV_AUTH_METHOD: Final = "api-key"
JEV_SPECIFICATION_VERSION: Final = "4"

#: AI 호출 원장에 남기는 provider/route 이름. transport가 하나뿐이라 같다.
JEV_PROVIDER_NAME: Final = "vercel-jev"

#: 원장 ``feature`` 값. 호출처마다 고정 문자열 하나를 쓴다.
JEV_FEATURE_NEWS_RELEVANCE: Final = "kasset_jev_news_relevance"
JEV_FEATURE_CANDIDATE_STANCE: Final = "kasset_jev_candidate_stance"

#: ``rounding``이 없을 때 choice 확률 합의 허용오차.
_JEV_SUM_TOLERANCE: Final = 1e-6
#: wire가 허용하는 소수 자릿수 상한.
_JEV_MAX_DECIMALS: Final = 15
_ROUNDING_DECIMAL_KEYS: Final = ("probabilityDecimals", "scoreDecimals")


class JevJudgmentError(RuntimeError):
    """Jev 판정 실패. HTTP 오류·timeout·응답 형식 위반을 하나로 묶는다."""


@dataclass(frozen=True, slots=True)
class JevBooleanAnswer:
    """boolean 질문의 답. ``probability``는 true일 확률이다."""

    probability: float
    confidence: float | None


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
    """true 확률을 묻는 boolean 질문 하나를 wire 형태로 만든다."""

    return {
        "type": "boolean",
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


def _probability_decimals(rounding: object) -> int | None:
    """``rounding``이 있으면 확률 소수 자릿수를, 없으면 ``None``을 돌려준다."""

    if rounding is None:
        return None
    if not isinstance(rounding, Mapping):
        raise JevJudgmentError("rounding must be an object")
    decimals: int | None = None
    for key in _ROUNDING_DECIMAL_KEYS:
        if key not in rounding:
            continue
        value = rounding[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise JevJudgmentError(f"rounding.{key} must be an integer 0..15")
        if not 0 <= value <= _JEV_MAX_DECIMALS:
            raise JevJudgmentError(f"rounding.{key} must be an integer 0..15")
        if key == "probabilityDecimals":
            decimals = value
    return decimals


def _confidence_by_question(provider_metadata: object) -> dict[str, float]:
    """``providerMetadata.typesafe.confidence``만 읽는다. 없으면 빈 map이다."""

    if provider_metadata is None:
        return {}
    if not isinstance(provider_metadata, Mapping):
        raise JevJudgmentError("providerMetadata must be an object")
    typesafe = provider_metadata.get("typesafe")
    if typesafe is None:
        return {}
    if not isinstance(typesafe, Mapping):
        raise JevJudgmentError("providerMetadata.typesafe must be an object")
    raw_confidence = typesafe.get("confidence")
    if raw_confidence is None:
        return {}
    if not isinstance(raw_confidence, Mapping):
        raise JevJudgmentError("providerMetadata.typesafe.confidence is invalid")
    return {
        str(question_id): _probability(value, field=f"confidence[{question_id}]")
        for question_id, value in raw_confidence.items()
    }


def _choice_labels(question: Mapping[str, object]) -> tuple[str, ...]:
    criteria = question.get("criteria")
    if not isinstance(criteria, Mapping):
        raise JevJudgmentError("choice questions require a criteria object")
    labels = tuple(str(label) for label in criteria)
    if not labels:
        raise JevJudgmentError("choice questions require at least one label")
    return labels


def _parse_boolean_answer(
    question_id: str,
    raw: object,
    confidence_by_question: Mapping[str, float],
) -> JevBooleanAnswer:
    if not isinstance(raw, Mapping) or raw.get("type") != "boolean":
        raise JevJudgmentError(f"{question_id} must be answered as a boolean")
    probability = _probability(
        raw.get("probability"),
        field=f"{question_id}.probability",
    )
    return JevBooleanAnswer(
        probability=probability,
        confidence=confidence_by_question.get(question_id),
    )


def _parse_choice_answer(
    question_id: str,
    raw: object,
    labels: tuple[str, ...],
    confidence_by_question: Mapping[str, float],
    probability_decimals: int | None,
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
    tolerance = _JEV_SUM_TOLERANCE
    if probability_decimals is not None:
        tolerance = len(labels) * 0.5 / (10**probability_decimals)
    if abs(sum(probabilities.values()) - 1.0) > tolerance:
        raise JevJudgmentError(f"{question_id}.probabilities must sum to 1")
    choice = raw.get("choice")
    if not isinstance(choice, str) or choice not in probabilities:
        raise JevJudgmentError(f"{question_id}.choice must be a label")
    if probabilities[choice] != max(probabilities.values()):
        raise JevJudgmentError(f"{question_id}.choice must be the largest")
    confidence = confidence_by_question.get(question_id)
    if confidence is None:
        raise JevJudgmentError(f"{question_id} is missing a confidence value")
    return JevChoiceAnswer(
        choice=choice,
        probabilities=probabilities,
        confidence=confidence,
    )


def parse_judgment(
    payload: object,
    *,
    questions: Mapping[str, Mapping[str, object]],
) -> JevJudgment:
    """응답 JSON을 wire 계약 그대로 해석한다. 형식 위반은 모두 실패다.

    ``answers`` 키 집합은 요청한 질문 id와 정확히 같아야 하고, usage의 두 token
    수는 비음수 정수여야 한다. choice 답은 모든 criteria label의 확률을 담고 합이
    1(``rounding`` 허용오차 안)이어야 하며, ``choice``는 최대확률 label이고
    ``providerMetadata.typesafe.confidence``가 있어야 한다. boolean 답에
    confidence가 없으면 ``None``으로 남긴다.
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
    input_tokens = _non_negative_int(usage.get("inputTokens"), field="inputTokens")
    output_tokens = _non_negative_int(usage.get("outputTokens"), field="outputTokens")
    probability_decimals = _probability_decimals(payload.get("rounding"))
    confidence_by_question = _confidence_by_question(payload.get("providerMetadata"))

    parsed: dict[str, JevBooleanAnswer | JevChoiceAnswer] = {}
    for question_id, question in questions.items():
        question_type = question.get("type")
        raw = answers[question_id]
        if question_type == "boolean":
            parsed[question_id] = _parse_boolean_answer(
                question_id, raw, confidence_by_question
            )
        elif question_type == "choice":
            parsed[question_id] = _parse_choice_answer(
                question_id,
                raw,
                _choice_labels(question),
                confidence_by_question,
                probability_decimals,
            )
        else:
            raise JevJudgmentError(f"unsupported question type: {question_type!r}")
    return JevJudgment(
        answers=parsed,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
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
    """게이트웨이 ``evaluation-model`` 한 엔드포인트를 호출하는 transport."""

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
            "state": state,
            "questions": {key: dict(value) for key, value in questions.items()},
        }
        headers = self._headers()
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
                    payload = await self._post(body, headers=headers)
                    judgment = parse_judgment(payload, questions=questions)
                    report_ai_attempt_usage(
                        prompt_tokens=judgment.input_tokens,
                        completion_tokens=judgment.output_tokens,
                        total_tokens=judgment.input_tokens + judgment.output_tokens,
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

    def _headers(self) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "ai-gateway-protocol-version": JEV_PROTOCOL_VERSION,
            "ai-gateway-auth-method": JEV_AUTH_METHOD,
            "ai-evaluation-model-specification-version": JEV_SPECIFICATION_VERSION,
            "ai-model-id": self._model_id,
            "authorization": f"Bearer {self._api_key}",
        }

    async def _post(
        self,
        body: Mapping[str, object],
        *,
        headers: Mapping[str, str],
    ) -> object:
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    self._base_url,
                    json=body,
                    headers=dict(headers),
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
    "JEV_AUTH_METHOD",
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
