# KAsset Trader Fork Plan

This fork keeps `mgh3326/auto_trader` as the trading/runtime core and adds KAsset-specific extensions with minimal upstream divergence.

## Safety boundary

Do not weaken or bypass existing upstream protections:

- dry-run defaults
- confirm / approval-hash gates
- idempotency / order-intent guards
- accepted-only order ledger
- fill-evidence reconciliation
- broker host allowlists
- fail-closed live-trading flags

AI/agent code must not call broker order endpoints directly. The expected flow is:

`Skill/Agent -> TradeProposal -> Validator -> Risk/Safety Gate -> Order Preview -> Approval -> Broker Adapter -> Reconcile`

## Branch policy

- `main`: upstream-compatible fork baseline
- `kasset-integration`: KAsset customization and integration work
- feature branches should branch from `kasset-integration`

Do not develop KAsset features directly on `main` unless an upstream-sync decision explicitly requires it.

## KAsset extension areas

1. Agent skills
   - technical analysis
   - fundamental analysis
   - news/sentiment analysis
   - market-regime analysis
   - bull/bear review when additional scrutiny is required
   - thesis synthesis / trade proposal
   - post-trade review / forecast calibration

2. Deterministic strategies
   - dual momentum
   - RSI(2) mean reversion
   - Donchian breakout
   - ORB / Stocks-in-Play

3. Broker expansion
   - preserve existing KIS/Toss integrations
   - add NH PLUG as a separate adapter, Read Only first
   - do not enable NH live orders during initial integration

4. KAsset Android integration
   - expose a narrow authenticated API facade for account, positions, orders, fills, system status, kill switch, and agent results
   - Android must not receive or persist broker secrets

5. AI connectivity
   - OpenAI requests go through the KAsset Cloudflare AI relay
   - Trading runtime stores only relay endpoint/token, never the raw OpenAI API key
   - broker credentials remain isolated from AI inputs

## Implementation order

1. Verify fork builds/tests without behavior changes.
2. Document upstream runtime and safety seams used by KAsset.
3. Add Skill and Strategy registries without live-order capability.
4. Connect one read-only analysis Skill end-to-end.
5. Add deterministic backtest/paper validation for strategies.
6. Add KAsset Android API facade.
7. Add NH PLUG Read Only adapter.
8. Only after paper/mock validation, consider tightly gated live-order integration.

## Upstream sync rule

Keep KAsset-specific code in additive modules/directories where possible. Avoid broad edits to core order/reconciliation code so updates from `mgh3326/auto_trader` can be merged with minimal conflict.

If a core change is unavoidable, document:

- upstream file changed
- reason for the change
- safety invariant affected
- tests proving the invariant remains intact

## Initial non-goals

- enabling live trading by default
- letting an LLM change execution mode
- letting an LLM calculate or override hard risk limits
- replacing upstream reconciliation
- copying secrets or account credentials into Git
- removing PostgreSQL/Redis/MCP components before baseline behavior is understood
