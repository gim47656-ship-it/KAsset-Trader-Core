# KAsset Extensions

This directory is the preferred home for KAsset-specific integration code that should remain additive to the upstream `auto_trader` core.

Planned areas:

```text
extensions/kasset/
├─ api/          # Android-facing API facade
├─ agent/        # KAsset agent runner / skill registry integration
├─ ai/           # Cloudflare relay client integration
├─ brokers/      # KAsset-added broker adapters, starting with NH PLUG
└─ config/       # KAsset extension configuration
```

Guidelines:

- Reuse upstream services through stable interfaces; avoid copying core implementation files.
- Do not fork order-safety logic into a second implementation.
- Do not expose broker credentials to Android or AI modules.
- Keep live-order features fail-closed and disabled until explicitly validated.
- Prefer adapters/wrappers over edits to upstream order/reconciliation internals.
