"""Streamable HTTP JSON-RPC client for an external MCP analysis tool."""

from __future__ import annotations

import asyncio
import json
from math import isfinite
from uuid import uuid4

import httpx
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
from mcp.types import LATEST_PROTOCOL_VERSION

from app.extensions.kasset.ai.base import (
    STRUCTURED_ANALYSIS_SYSTEM_INSTRUCTIONS,
    AiProviderUnavailable,
    ReasoningEffort,
)


class McpStructuredJsonClient:
    """Initialize an MCP session, call one tool, and return strict JSON."""

    def __init__(
        self,
        *,
        url: str,
        token: str | None,
        tool_name: str = "run_skill",
        timeout_seconds: float = 30.0,
    ) -> None:
        normalized_url = url.strip()
        normalized_tool_name = tool_name.strip()
        if not normalized_url:
            raise ValueError("MCP URL is required")
        if not normalized_tool_name:
            raise ValueError("MCP tool name is required")
        if not isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120:
            raise ValueError("MCP timeout must be between 0 and 120 seconds")
        self._url = normalized_url
        self._token = token.strip() if token is not None else ""
        self._tool_name = normalized_tool_name
        self._timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "mcp"

    @property
    def tool_name(self) -> str:
        return self._tool_name

    async def request_json(
        self,
        *,
        model: str,
        input_payload: dict[str, object],
        reasoning_effort: ReasoningEffort | None,
        schema_name: str,
        schema: dict[str, object],
        additional_instructions: str | None = None,
    ) -> dict[str, object]:
        instructions = STRUCTURED_ANALYSIS_SYSTEM_INSTRUCTIONS
        if additional_instructions is not None and additional_instructions.strip():
            instructions = f"{instructions} {additional_instructions.strip()}"
        arguments: dict[str, object] = {
            "skill": schema_name,
            "instruction": instructions,
            "context": input_payload,
            "response_schema": schema,
        }
        if reasoning_effort is not None:
            arguments["reasoning_effort"] = reasoning_effort

        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        session_id: str | None = None
        session_headers: dict[str, str] | None = None
        try:
            async with asyncio.timeout(self._timeout_seconds), httpx.AsyncClient(
                timeout=self._timeout_seconds
            ) as client:
                initialize_id = uuid4().hex
                initialize, initialize_response = await self._post_request(
                    client,
                    headers=headers,
                    body={
                        "jsonrpc": "2.0",
                        "id": initialize_id,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": LATEST_PROTOCOL_VERSION,
                            "capabilities": {},
                            "clientInfo": {
                                "name": "kasset-trader-core",
                                "version": "1.0",
                            },
                        },
                    },
                    expected_id=initialize_id,
                    phase="initialize",
                )
                initialize_result = initialize.get("result")
                if not isinstance(initialize_result, dict):
                    raise ValueError("MCP initialize result is malformed")
                protocol_version = initialize_result.get("protocolVersion")
                if (
                    not isinstance(protocol_version, str)
                    or not protocol_version.strip()
                ):
                    raise ValueError("MCP initialize result has no protocol version")
                if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
                    raise ValueError(
                        "MCP initialize result selected an unsupported protocol version"
                    )

                session_headers = {
                    **headers,
                    "MCP-Protocol-Version": protocol_version,
                }
                raw_session_id = initialize_response.headers.get("Mcp-Session-Id")
                if raw_session_id is not None and raw_session_id.strip():
                    session_id = raw_session_id.strip()
                    session_headers["Mcp-Session-Id"] = session_id

                await self._post_notification(
                    client,
                    headers=session_headers,
                    body={
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                    },
                    phase="initialized notification",
                )

                tool_call_id = uuid4().hex
                tool_envelope, _ = await self._post_request(
                    client,
                    headers=session_headers,
                    body={
                        "jsonrpc": "2.0",
                        "id": tool_call_id,
                        "method": "tools/call",
                        "params": {
                            "name": self._tool_name,
                            "arguments": arguments,
                        },
                    },
                    expected_id=tool_call_id,
                    phase="tool call",
                )
                return self._tool_payload(tool_envelope)
        except AiProviderUnavailable:
            raise
        except ValueError:
            raise
        except (httpx.TransportError, httpx.TimeoutException, TimeoutError) as exc:
            raise AiProviderUnavailable(
                f"MCP provider unreachable: {type(exc).__name__}"
            ) from exc
        except Exception as exc:
            status_code = self._http_status_from_exception(exc)
            if status_code is not None and status_code >= 500:
                raise AiProviderUnavailable(
                    f"MCP provider unavailable: HTTP {status_code}"
                ) from exc
            if status_code is not None:
                raise ValueError(
                    f"MCP provider rejected the analysis request: HTTP {status_code}"
                ) from exc
            raise ValueError(
                f"MCP provider failed during session negotiation: "
                f"{type(exc).__name__}"
            ) from exc
        finally:
            if session_id is not None and session_headers is not None:
                await self._terminate_session(session_headers)

    async def _post_request(
        self,
        client: httpx.AsyncClient,
        *,
        headers: dict[str, str],
        body: dict[str, object],
        expected_id: str,
        phase: str,
    ) -> tuple[dict[str, object], httpx.Response]:
        response = await client.post(self._url, json=body, headers=headers)
        self._raise_for_status(response, phase=phase)
        envelope = self._response_envelope(response)
        if envelope.get("jsonrpc") != "2.0":
            raise ValueError(f"MCP {phase} returned an invalid JSON-RPC version")
        if envelope.get("id") != expected_id:
            raise ValueError(f"MCP {phase} returned a mismatched JSON-RPC id")
        if "error" in envelope:
            raise ValueError(f"MCP {phase} returned a JSON-RPC error")
        return envelope, response

    async def _post_notification(
        self,
        client: httpx.AsyncClient,
        *,
        headers: dict[str, str],
        body: dict[str, object],
        phase: str,
    ) -> None:
        response = await client.post(self._url, json=body, headers=headers)
        self._raise_for_status(response, phase=phase)
        if not response.content.strip():
            return
        envelope = self._response_envelope(response)
        if envelope.get("jsonrpc") != "2.0" or "error" in envelope:
            raise ValueError(f"MCP {phase} was rejected")

    @staticmethod
    def _raise_for_status(response: httpx.Response, *, phase: str) -> None:
        status_code = response.status_code
        if status_code >= 500:
            raise AiProviderUnavailable(
                f"MCP provider unavailable during {phase}: HTTP {status_code}"
            )
        if not response.is_success:
            raise ValueError(
                f"MCP provider rejected {phase}: HTTP {status_code}"
            )

    @staticmethod
    def _tool_payload(envelope: dict[str, object]) -> dict[str, object]:
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise ValueError("MCP provider returned a malformed tool result")
        if result.get("isError") is True:
            raise ValueError("MCP provider tool failed or refused the analysis")

        structured_content = result.get("structuredContent")
        if isinstance(structured_content, dict):
            return structured_content

        content = result.get("content")
        if not isinstance(content, list):
            raise ValueError("MCP provider returned malformed tool content")
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "refusal":
                raise ValueError("MCP provider refused the analysis request")
            if part.get("type") != "text":
                continue
            text = part.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            try:
                payload = json.loads(text)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "MCP provider returned malformed structured output"
                ) from exc
            if not isinstance(payload, dict):
                raise ValueError("MCP provider did not return a JSON object")
            return payload
        raise ValueError("MCP provider returned empty structured output")

    @staticmethod
    def _response_envelope(response: httpx.Response) -> dict[str, object]:
        content_type = response.headers.get("content-type", "").lower()
        try:
            if "text/event-stream" not in content_type:
                payload = response.json()
            else:
                payload = McpStructuredJsonClient._last_sse_payload(response.text)
        except (TypeError, ValueError) as exc:
            raise ValueError("MCP provider returned malformed JSON-RPC output") from exc
        if not isinstance(payload, dict):
            raise ValueError("MCP provider returned a malformed JSON-RPC envelope")
        return payload

    @staticmethod
    def _last_sse_payload(response_text: str) -> object:
        payload: object | None = None
        for line in response_text.splitlines():
            if not line.startswith("data:"):
                continue
            candidate = line.removeprefix("data:").strip()
            if candidate and candidate != "[DONE]":
                payload = json.loads(candidate)
        if payload is None:
            raise ValueError("SSE response did not contain JSON-RPC data")
        return payload

    @staticmethod
    def _http_status_from_exception(exc: BaseException) -> int | None:
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code
        if isinstance(exc, BaseExceptionGroup):
            for nested in exc.exceptions:
                status_code = McpStructuredJsonClient._http_status_from_exception(
                    nested
                )
                if status_code is not None:
                    return status_code
        return None

    async def _terminate_session(self, headers: dict[str, str]) -> None:
        try:
            async with httpx.AsyncClient(
                timeout=min(self._timeout_seconds, 2.0)
            ) as client:
                await client.delete(self._url, headers=headers)
        except (httpx.TransportError, httpx.TimeoutException, TimeoutError):
            return


__all__ = ["McpStructuredJsonClient"]
