"""구독 CLI 한 프로세스와 한 번 교환해 임의 JSON Schema 응답을 받아온다.

CLI 계약은 기존 `KASSET_AI_SUBSCRIPTION_CMD` 브리지와 같다. argv를 그대로
실행하고 prompt를 stdin으로 넣고 stdout에서 마지막 JSON 객체를 읽는다.
다른 점은 응답 형식이 고정된 `SkillResult`가 아니라 호출자가 넘긴
`response_schema`라는 것뿐이다.

실패는 두 부류로만 분류한다.

* `SidecarUnavailable`: 모델 출력이 아예 없는 provider 가용성 실패
  (프로세스 기동 불가, timeout, 비정상 종료). fallback 대상이다.
* `SidecarContractError`: 출력이 있었지만 계약을 위반한 경우
  (인자 한도 위반, 스키마 불일치, JSON 부재). fail-closed 대상이다.

이 구분은 `app/extensions/kasset/AGENTS.md`의 경계를 따른다. 가용성 실패에만
fallback이 허용되고, 잘못된 모델 출력은 다른 provider로 재시도하지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

#: 도구 인자 한도. prompt 크기를 유한하게 묶어 CLI 호출 비용을 예측 가능하게 한다.
MAX_SKILL_CHARS = 64
MAX_INSTRUCTION_CHARS = 8_000
MAX_CONTEXT_BYTES = 256 * 1024
MAX_SCHEMA_BYTES = 64 * 1024
#: CLI stdout 상한. 넘어서면 사용할 수 없는 출력이므로 계약 위반으로 막는다.
MAX_OUTPUT_BYTES = 1024 * 1024

REASONING_EFFORTS = ("low", "medium", "high")

_SKILL_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]*")

_OUTPUT_CONTRACT = (
    "You are KAsset Core's read-only market-analysis layer. Use only the JSON "
    "given in the context section below. Print exactly one JSON object that "
    "validates against the response_schema section as the last thing you "
    "print, with no markdown fence and no commentary after it. Never call "
    "tools, request more data, or emit broker, account, credential, leverage, "
    "quantity, or order-execution instructions."
)


class SidecarUnavailable(RuntimeError):
    """provider 가용성 실패다. 모델 출력이 없었으므로 fallback 대상이다."""


class SidecarContractError(ValueError):
    """계약 실패다. 요청이나 출력이 규격을 어겼으므로 fail-closed 대상이다."""


@dataclass(frozen=True, slots=True)
class SkillInvocation:
    """한도·스키마 검증을 통과한 단일 호출 입력."""

    skill: str
    instruction: str
    context_json: str
    schema: dict[str, Any]
    schema_json: str
    reasoning_effort: str | None


def build_invocation(
    *,
    skill: str,
    instruction: str,
    context: Mapping[str, Any],
    response_schema: Mapping[str, Any],
    reasoning_effort: str | None,
) -> SkillInvocation:
    """도구 인자를 검증해 호출 입력으로 정규화한다.

    한도 위반과 스키마 오류는 모두 계약 실패다. CLI를 부르기 전에 막는다.
    """

    normalized_skill = skill.strip()
    if (
        not _SKILL_PATTERN.fullmatch(normalized_skill)
        or len(normalized_skill) > MAX_SKILL_CHARS
    ):
        raise SidecarContractError(
            f"skill must be an identifier of at most {MAX_SKILL_CHARS} characters"
        )

    normalized_instruction = instruction.strip()
    if not normalized_instruction:
        raise SidecarContractError("instruction must not be empty")
    if len(normalized_instruction) > MAX_INSTRUCTION_CHARS:
        raise SidecarContractError(
            f"instruction exceeds {MAX_INSTRUCTION_CHARS} characters"
        )

    context_json = _encode_json(context, field="context", max_bytes=MAX_CONTEXT_BYTES)
    schema_json = _encode_json(
        response_schema, field="response_schema", max_bytes=MAX_SCHEMA_BYTES
    )

    schema = dict(response_schema)
    if schema.get("type") != "object":
        raise SidecarContractError(
            'response_schema must declare "type": "object" so the result can be '
            "returned as MCP structuredContent"
        )
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise SidecarContractError(
            f"response_schema is not a valid JSON Schema: {exc.message}"[:200]
        ) from None

    if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
        raise SidecarContractError(
            "reasoning_effort must be one of " + ", ".join(REASONING_EFFORTS)
        )

    return SkillInvocation(
        skill=normalized_skill,
        instruction=normalized_instruction,
        context_json=context_json,
        schema=schema,
        schema_json=schema_json,
        reasoning_effort=reasoning_effort,
    )


def build_prompt(invocation: SkillInvocation) -> bytes:
    """CLI stdin으로 넣을 prompt를 만든다.

    `reasoning_effort`는 CLI argv를 바꾸지 않는다. 명령 원문은 운영자가
    소유하고 CLI 종류도 고정이 아니므로, 요청된 분석 깊이는 prompt 안의
    참고 값으로만 전달한다.
    """

    sections = [_OUTPUT_CONTRACT, f"skill:\n{invocation.skill}"]
    if invocation.reasoning_effort is not None:
        sections.append(f"requested_reasoning_effort:\n{invocation.reasoning_effort}")
    sections.append(f"instruction:\n{invocation.instruction}")
    sections.append(f"response_schema:\n{invocation.schema_json}")
    sections.append(f"context:\n{invocation.context_json}")
    return ("\n\n".join(sections) + "\n").encode()


class SkillRunner:
    """CLI 프로세스 하나와 한 번 교환한다. 호출 간 상태를 보관하지 않는다."""

    __slots__ = ("_command", "_timeout_seconds")

    def __init__(self, *, command: tuple[str, ...], timeout_seconds: float) -> None:
        self._command = command
        self._timeout_seconds = timeout_seconds

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    async def run(self, invocation: SkillInvocation) -> dict[str, Any]:
        """CLI를 호출해 `response_schema`를 만족하는 객체 하나를 돌려준다."""

        stdout = await self._exchange(build_prompt(invocation))
        payload = _last_json_object(stdout.decode("utf-8", errors="replace"))
        if payload is None:
            raise SidecarContractError(
                _failure_message("returned no JSON object", exit_code=0, stdout=stdout)
            )
        try:
            Draft202012Validator(invocation.schema).validate(payload)
        except ValidationError as exc:
            # 위반 위치와 위반 키워드만 남긴다. 모델 출력 본문은 넣지 않는다.
            raise SidecarContractError(
                f"output violates response_schema at {exc.json_path}: {exc.validator}"
            ) from None
        return payload

    async def _exchange(self, prompt: bytes) -> bytes:
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            raise SidecarUnavailable(
                _failure_message(
                    f"could not start: {type(exc).__name__}", exit_code=None
                )
            ) from None

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(prompt),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            await _kill(process)
            raise SidecarUnavailable(
                _failure_message("timed out", exit_code=process.returncode)
            ) from None
        except asyncio.CancelledError:
            await _kill(process)
            raise
        except Exception as exc:
            await _kill(process)
            raise SidecarUnavailable(
                _failure_message(
                    f"communication failed: {type(exc).__name__}",
                    exit_code=process.returncode,
                )
            ) from None

        if process.returncode != 0:
            raise SidecarUnavailable(
                _failure_message(
                    "exited non-zero",
                    exit_code=process.returncode,
                    stdout=stdout,
                    stderr=stderr,
                )
            )
        if len(stdout) > MAX_OUTPUT_BYTES:
            raise SidecarContractError(
                _failure_message(
                    f"exceeded the {MAX_OUTPUT_BYTES} byte stdout limit",
                    exit_code=process.returncode,
                    stdout=stdout,
                    stderr=stderr,
                )
            )
        return stdout


def _encode_json(value: Mapping[str, Any], *, field: str, max_bytes: int) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise SidecarContractError(f"{field} is not JSON-serializable") from exc
    size = len(encoded.encode())
    if size > max_bytes:
        raise SidecarContractError(
            f"{field} is {size} bytes and exceeds the {max_bytes} byte limit"
        )
    return encoded


def _last_json_object(text: str) -> dict[str, Any] | None:
    """stdout에서 마지막 JSON 객체를 고른다.

    구독 CLI는 최종 답변 앞에 진행 로그나 코드 fence를 붙일 수 있으므로
    앞쪽 잡음을 건너뛴다. 마지막 객체만 답으로 취급하고, 그 객체가 스키마를
    어기면 다른 객체로 되돌아가지 않고 계약 실패로 막는다.
    """

    decoder = json.JSONDecoder()
    cursor = 0
    last_object: dict[str, Any] | None = None

    while True:
        start = text.find("{", cursor)
        if start < 0:
            return last_object
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        cursor = end
        if isinstance(value, dict):
            last_object = value


def _failure_message(
    reason: str,
    *,
    exit_code: int | None,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> str:
    """CLI 실패를 크기 정보만으로 요약한다.

    stdout/stderr 본문은 절대 넣지 않는다. 구독 CLI 오류 출력에는 인증 경로나
    토큰 상태가 섞일 수 있고, 이 문자열은 MCP 호출자에게 그대로 전달된다.
    """

    return (
        f"subscription CLI {reason} (exit_code={exit_code}, "
        f"stdout_bytes={len(stdout)}, stderr_bytes={len(stderr)})"
    )[:160]


async def _kill(process: asyncio.subprocess.Process) -> None:
    with suppress(Exception):
        process.kill()
    with suppress(Exception):
        await process.wait()


__all__ = [
    "MAX_CONTEXT_BYTES",
    "MAX_INSTRUCTION_CHARS",
    "MAX_OUTPUT_BYTES",
    "MAX_SCHEMA_BYTES",
    "MAX_SKILL_CHARS",
    "REASONING_EFFORTS",
    "SidecarContractError",
    "SidecarUnavailable",
    "SkillInvocation",
    "SkillRunner",
    "build_invocation",
    "build_prompt",
]
