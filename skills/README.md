# KAsset Agent Skills

KAsset Skills are AI-assisted analysis modules. They may produce structured evidence, opinions, or `TradeProposal` objects, but they do **not** have broker execution authority.

Runtime implementations live under `app/extensions/kasset/skills/`; this top-level directory documents the Skill/SOP catalog and future skill packs.

## Planned skills

```text
skills/
├─ technical-analysis/
├─ fundamental-analysis/
├─ news-analysis/
├─ market-regime/
├─ bull-case/
├─ bear-case/
├─ thesis-synthesis/
├─ trade-proposal/
├─ post-trade-review/
└─ forecast-calibration/
```

The first runtime implementation is `technical_analysis`, backed by the shared dual-provider contract in `app/extensions/kasset/ai/`.

## Required output boundary

A decision-capable Skill should emit structured data only, for example:

```json
{
  "symbol": "AAPL",
  "action": "BUY",
  "confidence": 0.82,
  "reason": "example only",
  "entry_reference": null,
  "expires_at": null,
  "evidence_ids": []
}
```

Hard constraints:

- no direct broker API calls
- no direct `place_order` tool access
- no live-trading mode changes
- no broker credential access
- no override of deterministic risk rules
- stale/missing evidence must be reported, not silently treated as fresh

All Skill outputs must pass through runtime validation and the existing deterministic safety layer before any order can be considered.
