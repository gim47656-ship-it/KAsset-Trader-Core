# KAsset AI Dual Provider

KAsset supports two interchangeable AI execution paths while keeping one Skill contract.

## 1. Subscription Agent

External Codex/Claude-style agent connects to the existing auto_trader MCP server and invokes read-only tools. KAsset normalizes the returned analysis through `SubscriptionAgentProvider`.

```text
Codex / Claude
      |
      | MCP (streamable HTTP / SSE / stdio)
      v
auto_trader MCP
      |
      v
KAsset SkillResult
```

This path is intended for interactive research and operator-supervised analysis. The KAsset runtime does not contain subscription account credentials.

## 2. API Agent

The trading runtime calls a KAsset Cloudflare AI relay. The runtime stores the relay URL and relay token only; the raw OpenAI key remains in Cloudflare secrets.

```text
KAsset Skill
    |
    v
CloudflareAiProvider
    |
    v
Cloudflare AI relay
    |
    v
Model API
```

## 3. Hybrid mode

`AiProviderRouter(mode="hybrid")` prefers the subscription provider and falls back to the API provider only when the subscription provider reports `AiProviderUnavailable`.

Malformed model output, validation failures, or safety-contract failures do not trigger fallback. They fail closed.

## Shared contract

Both providers accept `SkillRequest` and return `SkillResult`. `SkillRequest` recursively rejects credential-like context keys before any provider call.

`SkillResult` is intentionally non-executable. It has no broker, account, quantity, approval-hash, or execution-mode field.

## First Skill

`technical_analysis` is the first runtime Skill. It consumes normalized indicator/price evidence and emits advisory BUY/SELL/HOLD/WATCH analysis only.

It does not call broker APIs and does not place orders.

## Planned wiring

1. Reuse existing auto_trader quote/indicator MCP tools as evidence sources.
2. Add a small subscription-agent bridge for Codex/Claude MCP sessions.
3. Deploy a separate KAsset Cloudflare AI relay endpoint.
4. Add provider configuration without changing upstream live-order flags.
5. Add more read-only Skills: news, fundamentals, regime, bull/bear, thesis synthesis.
6. Only after paper/mock validation should a structured TradeProposal be passed into the existing deterministic validation/risk/order path.
