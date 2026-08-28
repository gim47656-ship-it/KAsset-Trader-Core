"""Subprocess bridge for an operator-authenticated subscription AI CLI."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import Any

from pydantic import ValidationError

from app.extensions.kasset.ai.base import AiProviderUnavailable
from app.extensions.kasset.ai.models import SkillRequest, SkillResult
from app.extensions.kasset.ai.subscription_provider import SubscriptionInvoker

_SYSTEM_CONTRACT = (
    "You are KAsset Core's read-only market-analysis layer. Use only the "
    "SkillRequest JSON supplied below. Return one JSON object with a required "
    "non-empty summary, signal BUY/SELL/HOLD/WATCH or null, confidence from 0 "
    "to 1 or null, rationale as a string array, and optional metadata as an "
    "object. Never call tools or request more data. Do not return executable "
    "order instructions, account data, credentials, markdown, or explanatory "
    "text outside the JSON object."
)
_REQUEST_FIELDS = {"skill", "instruction", "symbol", "market", "context"}


def _build_prompt(request: SkillRequest) -> bytes:
    payload = request.model_dump(
        mode="json",
        include=_REQUEST_FIELDS,
        exclude_none=True,
    )
    request_json = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{_SYSTEM_CONTRACT}\n\nSkillRequest JSON:\n{request_json}\n".encode()


def _last_json_object(text: str) -> Mapping[str, Any] | None:
    decoder = json.JSONDecoder()
    cursor = 0
    last_object: Mapping[str, Any] | None = None

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
    return (
        f"subscription CLI {reason} (exit_code={exit_code}, "
        f"stdout_bytes={len(stdout)}, stderr_bytes={len(stderr)})"
    )[:120]


async def _kill_process(process: asyncio.subprocess.Process) -> None:
    with suppress(Exception):
        process.kill()
    with suppress(Exception):
        await process.wait()


async def _invoke(
    command: tuple[str, ...],
    timeout_seconds: float,
    request: SkillRequest,
) -> SkillResult:
    try:
        prompt = _build_prompt(request)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:
        raise AiProviderUnavailable(
            _failure_message(
                f"could not start: {type(exc).__name__}",
                exit_code=None,
            )
        ) from None

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(prompt),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        await _kill_process(process)
        raise AiProviderUnavailable(
            _failure_message("timed out", exit_code=process.returncode)
        ) from None
    except asyncio.CancelledError:
        await _kill_process(process)
        raise
    except Exception as exc:
        await _kill_process(process)
        raise AiProviderUnavailable(
            _failure_message(
                f"communication failed: {type(exc).__name__}",
                exit_code=process.returncode,
            )
        ) from None

    if process.returncode != 0:
        raise AiProviderUnavailable(
            _failure_message(
                "failed",
                exit_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        )

    raw_result = _last_json_object(stdout.decode("utf-8", errors="replace"))
    if raw_result is None:
        raise AiProviderUnavailable(
            _failure_message(
                "returned no JSON object",
                exit_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        )

    payload = dict(raw_result)
    payload.update(
        skill=request.skill,
        provider="subscription",
        correlation_id=request.correlation_id,
    )
    try:
        return SkillResult.model_validate(payload)
    except ValidationError:
        raise AiProviderUnavailable(
            _failure_message(
                "returned invalid SkillResult",
                exit_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        ) from None


def build_cli_invoker(
    command: Sequence[str],
    timeout_seconds: float,
) -> SubscriptionInvoker:
    """Build an async invoker that exchanges one request with one CLI process."""

    frozen_command = tuple(command)

    async def invoke(request: SkillRequest) -> SkillResult:
        return await _invoke(frozen_command, timeout_seconds, request)

    return invoke


__all__ = ["build_cli_invoker"]
