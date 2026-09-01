"""Title-level noise classification for market news surfaces (ROB-502).

Read-time port of news-ingestor's ``news_ingestor/noise.py`` (the ingestor
tags new rows as ``raw.noise_categories`` at collection time, but that raw
payload is not persisted to ``news_articles``, and historical rows predate
tagging — so the MCP exposure gate classifies titles again at read time).

Keep the category names in sync with the ingestor module; the categories
follow the ROB-502 issue: personal finance Q&A, lifestyle/celebrity,
sponsored/ads, pure price-prediction spam, and broad Web3/AI/general-tech
coverage. One deliberate divergence: ``broad_tech`` here is narrower than the
ingestor's (no bare AI model names) because the ingestor only *tags* matches
while this gate *excludes* them — a BTC market story that name-drops an AI
model must not vanish from the briefing.
"""

from __future__ import annotations

import re

NOISE_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "personal_finance": [
        re.compile(p, re.IGNORECASE)
        for p in (
            r"\bmy (plumber|husband|wife|mother|father|landlord|boss|in-laws?)\b",
            r"\bdo i (pay|owe|have to)\b",
            r"\bshould i (buy|sell|pay|retire|invest)\b",
            r"\b(credit card|mortgage|savings account) (debt|rates?)\b",
            r"\bsocial security\b",
            r"\binheritance\b",
            # NB: bare "청약" would also hit IPO subscriptions (공모주 청약),
            # which are market-relevant — keep this to housing subscriptions.
            r"주택청약|청약통장|연금 수령|절세 팁|재테크 꿀팁",
        )
    ],
    "lifestyle": [
        re.compile(p, re.IGNORECASE)
        for p in (
            r"\b(actress|actor|celebrity|singer)\b",
            r"\b(his|her|their) \$?[\d.]+ ?(million|billion)? ?(home|mansion|penthouse)\b",
            r"\bmidcentury\b",
            r"\brecipe\b",
            r"\bworld cup\b",
            r"맛집|여행기|연예인",
        )
    ],
    "sponsored": [
        re.compile(p, re.IGNORECASE)
        for p in (
            r"\bsponsored\b",
            r"\bpartner content\b",
            r"\bpress release\b",
            r"\b(top|best) \d+ (coins?|stocks?|cryptos?) to buy\b",
            r"\[광고\]|협찬",
        )
    ],
    "price_prediction": [
        re.compile(p, re.IGNORECASE)
        for p in (
            r"\bprice prediction\b",
            r"\bcould (reach|hit|soar to) \$\d",
            r"\bnext (100|1000)x\b",
            r"\bto the moon\b",
        )
    ],
    # Broad Web3/AI/general-tech coverage that is not market news. Conservative
    # on purpose; a market story that merely name-drops an AI vendor may still
    # be tagged — the gate reports the reason, callers can inspect excluded
    # items rather than losing them silently.
    "broad_tech": [
        re.compile(p, re.IGNORECASE)
        for p in (
            r"\b(openai|chatgpt|anthropic)\b",
            r"\bai (model|chatbot|assistant|startup|agent)s?\b",
            r"\b(video ?gam(e|es|ing)|gaming|esports)\b",
            r"\bmetaverse\b",
            r"\b(whatsapp|instagram|tiktok)\b",
            r"\bnft (art|collectibles?)\b",
        )
    ],
}


def classify_title_noise(title: str) -> list[str]:
    """Return the (possibly empty) list of noise categories a title matches."""
    text = (title or "").strip()
    if not text:
        return []
    return [
        category
        for category, patterns in NOISE_PATTERNS.items()
        if any(p.search(text) for p in patterns)
    ]


#: 모델 호출 **이전** gate가 써도 되는 category. ``broad_tech``는 의도적으로 빠져 있다.
#: 읽기 시점 gate는 broad_tech를 배제해도 원본 행이 그대로 남지만, 요약 이전 gate에서
#: 배제한 기사는 한국어 분석을 영구히 받지 못하고 한국어 분석을 요구하는 읽기 경로에서
#: 사라진다. "OpenAI가 Broadcom과 공급 계약" 같은 시장 이동 뉴스를 잃지 않기 위해
#: 그 category는 읽기 시점 판단으로만 남긴다.
PRE_SUMMARY_NOISE_CATEGORIES: frozenset[str] = frozenset(
    {"personal_finance", "lifestyle", "sponsored", "price_prediction"}
)


def classify_pre_summary_noise(title: str) -> list[str]:
    """Return only the noise categories safe to exclude before an AI summary."""
    return [
        category
        for category in classify_title_noise(title)
        if category in PRE_SUMMARY_NOISE_CATEGORIES
    ]


def noise_reason(categories: list[str]) -> str:
    """Stable excluded-reason string, e.g. ``noise:personal_finance``."""
    return "noise:" + ",".join(categories)
