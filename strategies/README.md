# KAsset Deterministic Strategies

Strategy modules are deterministic trading rules. They are separate from LLM Skills so the same market inputs produce the same rule result.

## Planned strategies

```text
strategies/
├─ dual-momentum/
├─ rsi2-mean-reversion/
├─ donchian-breakout/
└─ orb-stocks-in-play/
```

## Contract

A strategy may generate a candidate signal, but it does not submit an order directly.

Expected flow:

`Strategy -> Signal -> Validator -> Risk/Safety Gate -> Order Preview -> Approval -> Broker`

## Validation requirements

Before a strategy is eligible for anything beyond paper/mock trading, test at minimum:

- look-ahead bias
- fees and taxes where applicable
- slippage assumptions
- in-sample vs out-of-sample split
- walk-forward results
- max drawdown
- Sharpe or equivalent risk-adjusted metric
- profit factor
- trade count and win/loss distribution
- benchmark comparison
- duplicate signal/order behavior

No strategy may bypass upstream order guards, reconciliation, or kill-switch behavior.
