"""KAsset Cloudflare AI relay provider.

The trading server sends read-only skill context to a KAsset relay endpoint. The
raw model-provider API key remains outside this runtime.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.extensions.kasset.ai.base import AiProviderUnavailable
from app.extensions.kasset.ai.models import SkillRequest, SkillResult


class CloudflareAiProvider:
    def __init__(
        self,
        *,
        relay_url: str,
        relay_token: str,
        timeout_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not relay_url.strip():
            raise ValueError("relay_url is required")
        if not relay_token.strip():
            raise ValueError("relay_token is required")
        self._relay_url = relay_url.rstrip("/")
        self._relay_token = relay_token
        self._timeout_s = timeout_s
        self._client = client

    @property
    def name(self) -> str:
        return "api"

    async def run_skill(self, request: SkillRequest) -> SkillResult:
        payload: dict[str, Any] = {
            "skill": request.skill,
            "instruction": request.instruction,
            "symbol": request.symbol,
            "market": request.market,
            "context": request.context,
            "correlation_id": request.correlation_id,
        }
        headers = {"X-KAsset-AI-Relay-Token": self._relay_token}

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout_s)
        try:
            try:
                response = await client.post(
                    self._relay_url,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise AiProviderUnavailable(
                    f"AI relay unavailable: {type(exc).__name__}"
                ) from exc
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code >= 500:
                    raise AiProviderUnavailable(
                        f"AI relay returned HTTP {exc.response.status_code}"
                    ) from exc
                raise

            body = response.json()
            result_payload = body.get("result", body)
            if not isinstance(result_payload, dict):
                raise ValueError("AI relay result must be a JSON object")
            result_payload = dict(result_payload)
            result_payload["provider"] = "api"
            result_payload["skill"] = request.skill
            result_payload["correlation_id"] = request.correlation_id
            return SkillResult.model_validate(result_payload)
        finally:
            if owns_client:
                await client.aclose()
