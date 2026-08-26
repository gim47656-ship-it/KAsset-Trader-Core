"""Small explicit registry for fork-local KAsset skills."""

from __future__ import annotations

from typing import Any

from app.extensions.kasset.skills.technical_analysis import TechnicalAnalysisSkill

_SKILLS: dict[str, Any] = {
    TechnicalAnalysisSkill.name: TechnicalAnalysisSkill(),
}


def get_skill(name: str) -> Any:
    try:
        return _SKILLS[name]
    except KeyError as exc:
        raise KeyError(f"unknown KAsset skill: {name}") from exc


def list_skills() -> tuple[str, ...]:
    return tuple(sorted(_SKILLS))
