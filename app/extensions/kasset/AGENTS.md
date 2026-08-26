# KASSET EXTENSION KNOWLEDGE BASE

## PURPOSE
`app/extensions/kasset/` contains fork-local KAsset functionality that should remain additive to upstream `auto_trader`.

## HARD SAFETY BOUNDARIES
- No module in this subtree may call broker mutation endpoints directly.
- No AI provider may receive broker credentials, account secrets, raw authorization headers, or OpenAI API keys.
- AI output is analysis/proposal data only. It never bypasses upstream order validation, approval hash, idempotency, ledger, or reconciliation.
- Hybrid AI fallback is allowed only for provider availability failures; invalid/unsafe model output must fail closed rather than silently retry through another provider.
- Live-trading flags and risk limits remain owned by deterministic upstream runtime code.

## AI PROVIDERS
- `subscription`: external Codex/Claude-style agent connected through MCP or another injected bridge.
- `api`: KAsset Cloudflare AI relay. The trading server stores only relay URL/token; the raw OpenAI key stays in Cloudflare secrets.
- `hybrid`: prefer subscription provider and fall back to API only when the subscription provider is unavailable.

## SKILLS
Skills consume already-normalized read-only evidence and emit structured analysis results. They do not fetch secrets, mutate accounts, or place orders.

## CHANGE POLICY
Prefer new files under this subtree over edits to `app/mcp_server/tooling/` or broker services. If a core edit becomes unavoidable, document the affected upstream invariant and add focused tests before merging.
