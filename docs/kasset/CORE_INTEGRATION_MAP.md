# KAsset Trader — Upstream Core Integration Map

This document defines the initial integration boundary for KAsset-specific work on top of the `mgh3326/auto_trader` fork.

The goal is to keep upstream trading safety behavior intact while adding KAsset Android, NH PLUG, AI Skills, and deterministic strategies through additive seams.

## 1. Protected upstream safety path

Do not bypass this path:

`Agent / Strategy -> Proposal -> Validation -> Preview -> Approval / Idempotency -> Broker send -> Accepted ledger -> Reconcile -> Fill evidence -> Journal / P&L`

The following files are high-risk core files and should be treated as protected unless a KAsset feature cannot be implemented additively.

| Area | Upstream location | KAsset rule |
|---|---|---|
| Order coordinator | `app/mcp_server/tooling/order_execution.py` | Do not route around it |
| Order validation | `app/mcp_server/tooling/order_validation.py` | Do not weaken existing guards |
| Shared approval | `app/mcp_server/tooling/order_approval.py` | Reuse; do not invent a second approval contract |
| Toss approval | `app/mcp_server/tooling/toss_approval.py` | Preserve Toss-specific canonical/idempotency behavior |
| KIS order surface | `app/mcp_server/tooling/orders_kis_variants.py` | Prefer adapter/wrapper extension over edits |
| Toss order surface | `app/mcp_server/tooling/orders_toss_variants.py` | Preserve existing live-order gates |
| Modify/cancel | `app/mcp_server/tooling/orders_modify_cancel.py` | Reuse existing state/error semantics |
| KIS live ledger/reconcile | `app/mcp_server/tooling/kis_live_ledger.py` | Never mark fill without broker evidence |
| Generic live ledger | `app/mcp_server/tooling/live_order_ledger.py` | Preserve accepted-only semantics |
| Generic fill evidence | `app/mcp_server/tooling/live_order_evidence.py` | Reuse for execution confirmation |
| Toss live evidence | `app/mcp_server/tooling/toss_live_evidence.py` | Preserve broker-evidence confirmation |
| Toss live ledger | `app/mcp_server/tooling/toss_live_ledger.py` | Preserve accepted/reconcile boundary |
| Order proposal surface | `app/mcp_server/tooling/order_proposal_tools.py` | Preferred handoff target for KAsset decisions |
| MCP registry | `app/mcp_server/tooling/registry.py` | New KAsset tools must use normal registration patterns |

## 2. Safety invariants confirmed in the fork

### 2.1 KIS duplicate-submit protection

KIS does not provide a broker-side client idempotency field on the relevant live order path. Upstream therefore reserves an `OrderSendIntent` locally before live KIS KR/US submission.

A duplicate same-key intent is rejected before another broker send.

KAsset code must not:

- delete this reservation before an uncertain send is reconciled,
- auto-retry an order POST after an unknown transport outcome,
- generate a new key merely to evade a duplicate-intent rejection.

### 2.2 Unknown send outcome is not a retry signal

If an order request crosses the send boundary and the HTTP outcome becomes unknown, upstream raises an outcome-unknown condition and requires reconciliation instead of another POST.

KAsset scheduler/agent logic must interpret this as:

`UNKNOWN -> reconcile`, not `UNKNOWN -> place again`.

### 2.3 Accepted is not Filled

KIS live KR and US paths persist accepted-only order state first. Fill/journal/P&L mutation happens after order-id-keyed broker evidence is obtained by reconciliation.

KAsset Android and AI must display or consume these states separately.

### 2.4 AI cannot override hard execution rules

No Skill is allowed to change:

- live execution flags,
- approval requirements,
- idempotency behavior,
- broker host allowlists,
- risk caps,
- fill-evidence requirements.

AI output is advisory/decision data only.

## 3. Existing read-only surfaces to reuse for Skills

KAsset Skills should consume existing runtime data rather than calling brokers directly.

Useful existing areas include:

- `market_data_quotes.py` — quote/session/freshness information
- `market_data_indicators.py` — technical indicators
- `analysis_screening.py` / `screener_*` — candidate screening
- `fundamentals_handlers.py` / `fundamentals_sources_*` — fundamental enrichment
- `news_handlers.py` — news evidence
- `portfolio_holdings.py` / `portfolio_cash.py` — portfolio context
- `investment_snapshots_*` — analysis snapshots
- `forecast_*` — forecast persistence/calibration
- `trade_retrospective_*` — post-trade review

First KAsset Skill work must be READ ONLY.

## 4. KAsset extension boundary

Prefer new code under additive KAsset-owned modules.

Initial target layout:

```text
extensions/kasset/
├─ agent/
│  ├─ models.py
│  ├─ runner.py
│  └─ relay_client.py
├─ skills/
│  ├─ registry.py
│  ├─ technical/
│  ├─ fundamental/
│  ├─ news/
│  ├─ market_regime/
│  ├─ bull_bear/
│  └─ thesis/
├─ strategies/
│  ├─ registry.py
│  ├─ dual_momentum/
│  ├─ rsi2/
│  ├─ donchian/
│  └─ orb/
└─ android_api/
```

The exact Python package wiring may be adjusted after baseline tests, but ownership should remain additive.

## 5. Skill contract

A Skill must not place an order.

Minimum conceptual output:

```text
SkillEvidence / SkillAssessment
- symbol
- market
- as_of
- data_freshness
- score / direction
- confidence
- evidence
- warnings
```

A synthesis Skill may produce a `TradeProposal`, but execution still goes through the upstream proposal/validation/order path.

## 6. Strategy contract

Deterministic strategies are code, not LLM prompts.

A strategy may calculate:

- candidate eligibility,
- BUY / SELL / HOLD intent,
- reference entry,
- stop/reference levels,
- strategy-specific confidence/score.

A strategy must not:

- call a broker place-order method,
- set `confirm=True` itself to bypass operator policy,
- change live execution configuration,
- decide that an uncertain prior order should simply be resent.

## 7. First implementation sequence

1. Keep `main` upstream-compatible.
2. Keep KAsset work on `kasset-integration` or feature branches from it.
3. Run upstream baseline tests before runtime changes.
4. Implement a READ-ONLY Skill registry.
5. Implement Technical Analysis Skill using existing quote/OHLCV/indicator surfaces.
6. Add Cloudflare AI relay client only after the read-only contract is stable.
7. Add deterministic strategy registry and paper/backtest adapters.
8. Add KAsset Android API facade.
9. Inspect broker-service conventions and add NH PLUG Read Only as a new adapter.
10. Do not add NH live mutation paths until read-only + paper/mock validation is complete.

## 8. Core-change rule

If KAsset must modify a protected upstream file, every such change must document:

1. why an additive extension was insufficient,
2. which safety invariant is affected,
3. exact tests proving the invariant remains intact,
4. expected upstream merge-conflict surface.

No protected-core modification should be merged solely to make a Skill easier to call.
