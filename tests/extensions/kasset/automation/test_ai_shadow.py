from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.extensions.kasset.automation import backtest, portfolio_backtest
from app.extensions.kasset.automation.ai_shadow import (
    AI_SHADOW_SCHEMA_VERSION,
    PERSISTED_FINAL_SELECTIONS_ONLY,
    build_ai_shadow_observation,
    derive_persisted_final_selections_only_stats,
    validate_selected_ai_shadow_evidence,
)
from app.services.research_canonical_hash import canonical_sha256

_NOW = datetime(2026, 8, 30, 3, 0, tzinfo=UTC)


def _verdict(*, model_id: str = "configured-terra-model") -> SimpleNamespace:
    normalized_input = {
        "kind": "candidate_review",
        "payload": {
            "symbol": "005930",
            "rawPrompt": "do-not-persist-this-prompt",
            "secret": "do-not-persist-this-secret",
        },
    }
    return SimpleNamespace(
        input_hash=canonical_sha256(normalized_input),
        provider="direct-api",
        tier="terra",
        model_id=model_id,
        action="BUY",
        risk="MEDIUM",
        bullish_score=84,
        bearish_score=16,
        rationale_tags=["breakout_confirmed", "risk_bounded"],
        confidence=0.9,
    )


def _selected_evidence(
    *, model_id: str = "configured-terra-model"
) -> dict[str, object]:
    return build_ai_shadow_observation(
        _verdict(model_id=model_id),
        observed_at=_NOW,
    ).as_selected_evidence()


def test_selected_shadow_evidence_is_closed_and_secret_free() -> None:
    evidence = _selected_evidence()

    assert evidence["kind"] == "ai_shadow"
    assert evidence["schemaVersion"] == AI_SHADOW_SCHEMA_VERSION
    assert evidence["provider"] == "direct-api"
    assert evidence["tier"] == "terra"
    assert evidence["modelId"] == "configured-terra-model"
    assert evidence["validatedResponse"] == {
        "action": "BUY",
        "risk": "MEDIUM",
        "bullishScore": 84,
        "bearishScore": 16,
        "rationaleTags": ["breakout_confirmed", "risk_bounded"],
    }
    assert evidence["confidence"] == "0.9"
    assert evidence["selected"] is True
    assert evidence["selectionReason"] == "ranked_final_selection_after_technical_gate"
    assert evidence["observedAt"] == "2026-08-30T03:00:00Z"

    serialized = json.dumps(evidence, sort_keys=True)
    assert "rawPrompt" not in serialized
    assert "do-not-persist-this-prompt" not in serialized
    assert "secret" not in serialized
    assert "do-not-persist-this-secret" not in serialized
    assert "Authorization" not in serialized
    assert "providerPayload" not in serialized


@pytest.mark.parametrize("forbidden_key", ["rawPrompt", "secret", "authorization"])
def test_shadow_evidence_rejects_forbidden_extra_fields(
    forbidden_key: str,
) -> None:
    tainted = {**_selected_evidence(), forbidden_key: "must-not-persist"}

    with pytest.raises(ValueError):
        validate_selected_ai_shadow_evidence(tainted)


def test_unobserved_exact_model_id_fails_closed() -> None:
    with pytest.raises(ValueError, match="exact model_id"):
        build_ai_shadow_observation(
            _verdict(model_id=""),
            observed_at=_NOW,
        )


def test_stats_are_limited_to_persisted_final_selections() -> None:
    first = _selected_evidence()
    second = _selected_evidence(model_id="configured-sol-model")
    second["confidence"] = "0.6"
    validated = second["validatedResponse"]
    assert isinstance(validated, dict)
    validated["action"] = "SELL"
    unselected = {**first, "selected": False}

    stats = derive_persisted_final_selections_only_stats(
        [
            [first],
            [second],
            [unselected],
            [first, first],
            [{"kind": "ai_analysis", "confidence": "1.0"}],
        ]
    )

    assert stats == {
        "name": "AI shadow stats — persisted final selections only",
        "scope": PERSISTED_FINAL_SELECTIONS_ONLY,
        "count": 2,
        "modelCounts": {
            "configured-sol-model": 1,
            "configured-terra-model": 1,
        },
        "actionCounts": {"BUY": 1, "SELL": 1},
        "averageConfidence": "0.750000",
        "selectionCount": 2,
    }


def test_deterministic_backtests_do_not_import_or_call_ai_providers() -> None:
    forbidden_calls = {"analyze", "analyze_for_owner", "request_json", "run_skill"}
    for module in (backtest, portfolio_backtest):
        tree = _module_ast(module)
        imported_modules = {
            name.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for name in node.names
        }
        imported_modules.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        called_names = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        called_names.update(
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        )

        assert not any(
            name.startswith("app.extensions.kasset.ai") for name in imported_modules
        )
        assert called_names.isdisjoint(forbidden_calls)


def _module_ast(module: ModuleType) -> ast.Module:
    path = Path(str(module.__file__))
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
