"""Structured contracts shared by every KAsset AI provider."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AiProviderMode(StrEnum):
    SUBSCRIPTION = "subscription"
    API = "api"
    HYBRID = "hybrid"


_FORBIDDEN_CONTEXT_KEY_PARTS = (
    "api_key",
    "apikey",
    "app_key",
    "app_secret",
    "client_secret",
    "authorization",
    "password",
    "broker_token",
    "access_token",
    "refresh_token",
    "account_number",
    "account_no",
    "credential",
)


def _scan_for_sensitive_keys(value: Any, *, path: str = "context") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key).lower()
            child_path = f"{path}.{raw_key}"
            if any(part in key for part in _FORBIDDEN_CONTEXT_KEY_PARTS):
                findings.append(child_path)
            findings.extend(_scan_for_sensitive_keys(child, path=child_path))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            findings.extend(_scan_for_sensitive_keys(child, path=f"{path}[{index}]"))
    return findings


class SkillRequest(BaseModel):
    """Provider-neutral, read-only request sent to an AI analysis provider."""

    model_config = ConfigDict(extra="forbid")

    skill: str = Field(min_length=1, max_length=100)
    instruction: str = Field(min_length=1, max_length=20_000)
    symbol: str | None = Field(default=None, max_length=64)
    market: Literal["kr", "us", "crypto"] | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def reject_sensitive_context(self) -> SkillRequest:
        findings = _scan_for_sensitive_keys(self.context)
        if findings:
            raise ValueError(
                "AI context contains forbidden credential-like keys: "
                + ", ".join(findings[:10])
            )
        return self


class SkillResult(BaseModel):
    """Non-executable AI analysis result.

    `signal` is advisory evidence only. It is not an order and intentionally has
    no quantity, broker, account, approval hash, or execution-mode field.
    """

    model_config = ConfigDict(extra="forbid")

    skill: str = Field(min_length=1, max_length=100)
    provider: Literal["subscription", "api"]
    summary: str = Field(min_length=1, max_length=20_000)
    signal: Literal["BUY", "SELL", "HOLD", "WATCH"] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    rationale: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = Field(default=None, max_length=128)
