"""Authenticated Android API for persisted AI recommendation review."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.extensions.kasset.automation.job import run_approved_recommendation_once
from app.models.trading import User, UserRole
from app.routers.dependencies import get_authenticated_user
from app.schemas.ai_recommendations import (
    RecommendationDecisionRequest,
    RecommendationErrorEnvelope,
    RecommendationListResponse,
    RecommendationResponse,
    RecommendationStatusGroup,
    build_recommendation_response,
)
from app.services.ai_recommendations import (
    AIRecommendationService,
    RecommendationNotFoundError,
    RecommendationStateConflictError,
    RecommendationValidationError,
)

router = APIRouter(
    prefix="/api/v1/ai/recommendations",
    tags=["AI Recommendations"],
    dependencies=[Depends(get_authenticated_user)],
)

_VALIDATION_MESSAGES = {
    "action_not_approvable": "BUY 또는 SELL 추천만 승인할 수 있습니다.",
    "rationale_required": "근거가 있는 추천만 승인할 수 있습니다.",
    "valid_until_required": "유효 시간이 지정된 추천만 승인할 수 있습니다.",
    "valid_until_invalid": "추천 유효 시간이 올바르지 않습니다.",
    "recommendation_expired": "유효 시간이 지나지 않은 추천만 승인할 수 있습니다.",
}


def _service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AIRecommendationService:
    return AIRecommendationService(db)


def _supports_paper_execution(row: object) -> bool:
    return any(
        isinstance(item, dict) and item.get("kind") == "ai_vertical_slice"
        for item in (getattr(row, "evidence", None) or [])
    )


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            }
        },
    )


def _parse_list_query(request: Request) -> tuple[str, int] | JSONResponse:
    status_values = request.query_params.getlist("status")
    limit_values = request.query_params.getlist("limit")
    if len(status_values) > 1 or len(limit_values) > 1:
        return _error_response(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="VALIDATION_ERROR",
            message="요청 형식이나 값이 올바르지 않습니다.",
        )

    status_value = status_values[0] if status_values else "PENDING"
    if status_value not in {"PENDING", "RESOLVED"}:
        return _error_response(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="VALIDATION_ERROR",
            message="status는 PENDING 또는 RESOLVED여야 합니다.",
            details={"field": "status"},
        )

    limit_text = limit_values[0] if limit_values else "50"

    if len(limit_text) > 3 or not limit_text.isascii() or not limit_text.isdecimal():
        return _error_response(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="VALIDATION_ERROR",
            message="limit은 1 이상 100 이하의 정수여야 합니다.",
            details={"field": "limit"},
        )
    limit_value = int(limit_text)
    if limit_value < 1 or limit_value > AIRecommendationService.MAX_LIMIT:
        return _error_response(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="VALIDATION_ERROR",
            message="limit은 1 이상 100 이하의 정수여야 합니다.",
            details={"field": "limit"},
        )
    return status_value, limit_value


async def _parse_decision_body(
    request: Request,
) -> RecommendationDecisionRequest | JSONResponse:
    try:
        payload = await request.json()
        return RecommendationDecisionRequest.model_validate(payload)
    except ValueError:
        return _error_response(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="VALIDATION_ERROR",
            message="본문에는 APPROVED 또는 REJECTED decision 하나만 허용됩니다.",
        )


@router.get(
    "",
    response_model=RecommendationListResponse,
    response_model_exclude={
        "recommendations": {
            "__all__": {
                "status",
                "regime",
                "regime_detail",
                "strategy_votes",
                "ai_rationale",
                "event_evidence",
                "entry_price",
                "stop_price",
                "target_price",
                "ranking",
                "portfolio",
                "hard_risk",
                "paper_order",
            }
        }
    },
    responses={
        400: {
            "model": RecommendationErrorEnvelope,
            "description": "Invalid status or limit",
        }
    },
)
async def list_recommendations(
    request: Request,
    current_user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[AIRecommendationService, Depends(_service)],
) -> RecommendationListResponse | JSONResponse:
    parsed = _parse_list_query(request)
    if isinstance(parsed, JSONResponse):
        return parsed
    status_group, limit = parsed
    recommendations = await service.list_recommendations(
        current_user.id,
        status=cast(RecommendationStatusGroup, status_group),
        limit=limit,
    )
    paper_orders = await service.load_paper_orders(recommendations)
    symbol_names = await service.load_symbol_names(recommendations)
    return RecommendationListResponse(
        recommendations=[
            build_recommendation_response(
                row,
                paper_order=paper_orders.get(row.id),
                resolved_name=symbol_names.get((row.market, row.symbol)),
            )
            for row in recommendations
        ]
    )


@router.get(
    "/{recommendation_id}",
    response_model=RecommendationResponse,
    response_model_exclude_none=True,
    response_model_exclude_defaults=True,
    responses={
        404: {
            "model": RecommendationErrorEnvelope,
            "description": "Recommendation not found",
        }
    },
)
async def get_recommendation(
    recommendation_id: str,
    current_user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[AIRecommendationService, Depends(_service)],
) -> RecommendationResponse | JSONResponse:
    try:
        row = await service.get_recommendation(
            current_user.id,
            recommendation_id,
        )
    except RecommendationNotFoundError:
        return _error_response(
            status_code=status.HTTP_404_NOT_FOUND,
            code="NOT_FOUND",
            message="추천을 찾을 수 없습니다.",
        )
    paper_orders = await service.load_paper_orders([row])
    symbol_names = await service.load_symbol_names([row])
    return build_recommendation_response(
        row,
        paper_order=paper_orders.get(row.id),
        resolved_name=symbol_names.get((row.market, row.symbol)),
    )


@router.post(
    "/{recommendation_id}/decision",
    response_model=RecommendationResponse,
    response_model_exclude_none=True,
    response_model_exclude_defaults=True,
    responses={
        400: {
            "model": RecommendationErrorEnvelope,
            "description": "Invalid decision or approval guard",
        },
        404: {
            "model": RecommendationErrorEnvelope,
            "description": "Recommendation not found",
        },
        409: {
            "model": RecommendationErrorEnvelope,
            "description": "Recommendation state conflict",
        },
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": RecommendationDecisionRequest.model_json_schema()
                }
            },
        }
    },
)
async def decide_recommendation(
    recommendation_id: str,
    request: Request,
    current_user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[AIRecommendationService, Depends(_service)],
) -> RecommendationResponse | JSONResponse:
    if current_user.role not in {UserRole.trader, UserRole.admin}:
        return _error_response(
            status_code=status.HTTP_403_FORBIDDEN,
            code="FORBIDDEN",
            message="이 작업을 수행할 권한이 없습니다.",
        )
    parsed = await _parse_decision_body(request)
    if isinstance(parsed, JSONResponse):
        return parsed

    try:
        row = await service.decide(
            current_user.id,
            recommendation_id=recommendation_id,
            decision=parsed.decision,
        )
    except RecommendationNotFoundError:
        return _error_response(
            status_code=status.HTTP_404_NOT_FOUND,
            code="NOT_FOUND",
            message="추천을 찾을 수 없습니다.",
        )
    except RecommendationStateConflictError:
        return _error_response(
            status_code=status.HTTP_409_CONFLICT,
            code="RECOMMENDATION_STATE_CONFLICT",
            message="이미 완료된 추천의 결정을 변경할 수 없습니다.",
        )
    except RecommendationValidationError as exc:
        return _error_response(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="VALIDATION_ERROR",
            message=_VALIDATION_MESSAGES.get(
                exc.reason,
                "요청 형식이나 값이 올바르지 않습니다.",
            ),
            details={"reason": exc.reason},
        )
    if parsed.decision == "APPROVED" and _supports_paper_execution(row):
        await run_approved_recommendation_once(
            current_user.id,
            recommendation_id,
        )
        row = await service.get_recommendation(
            current_user.id,
            recommendation_id,
        )
    paper_orders = await service.load_paper_orders([row])
    symbol_names = await service.load_symbol_names([row])
    return build_recommendation_response(
        row,
        paper_order=paper_orders.get(row.id),
        resolved_name=symbol_names.get((row.market, row.symbol)),
    )


__all__ = ["router"]
