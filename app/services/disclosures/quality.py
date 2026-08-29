"""DART 공시의 요약·표시 우선순위를 정하는 공통 품질 규칙."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from sqlalchemy import or_

DART_HIGH_VALUE_TITLE_TERMS: tuple[str, ...] = (
    "잠정실적",
    "매출액또는손익구조",
    "영업실적",
    "단일판매",
    "공급계약",
    "유상증자",
    "무상증자",
    "전환사채",
    "신주인수권부사채",
    "합병",
    "분할",
    "영업양수",
    "타법인주식",
    "유형자산",
    "시설투자",
    "배당",
    "감자",
    "주식교환",
    "공개매수",
    "상장폐지",
    "영업양도",
    "자기주식",
    "최대주주변경",
    "소송",
    "회생절차",
    "부도",
    "정정",
)

DART_LOW_INFORMATION_TITLE_TERMS: tuple[str, ...] = (
    "대규모기업집단현황",
    "기업집단현황공시",
    "특정증권등소유상황보고서",
    "효력발생안내",
    "증권발행실적보고서",
)

_TITLE_NOISE_RE = re.compile(r"[\s·ㆍ_\-\[\]()]+")


def _title_key(title: str | None) -> str:
    return _TITLE_NOISE_RE.sub("", (title or "").casefold())


def is_high_value_dart_title(title: str | None) -> bool:
    """실적·자본·계약·구조 변경처럼 투자 판단에 직접 연결되는 제목인지 판별한다."""

    key = _title_key(title)
    return any(_title_key(term) in key for term in DART_HIGH_VALUE_TITLE_TERMS)


def is_low_information_dart_title(title: str | None) -> bool:
    """전사 최신 피드를 압도하지만 개별 투자 사건 정보가 낮은 반복 서식인지 판별한다."""

    key = _title_key(title)
    return any(_title_key(term) in key for term in DART_LOW_INFORMATION_TITLE_TERMS)


def title_matches_any(column: Any, terms: Sequence[str]) -> Any:
    """Python 규칙과 동일한 제목 부분문자열 조건을 SQLAlchemy 식으로 만든다."""

    return or_(*(column.ilike(f"%{term}%") for term in terms))


__all__ = [
    "DART_HIGH_VALUE_TITLE_TERMS",
    "DART_LOW_INFORMATION_TITLE_TERMS",
    "is_high_value_dart_title",
    "is_low_information_dart_title",
    "title_matches_any",
]
