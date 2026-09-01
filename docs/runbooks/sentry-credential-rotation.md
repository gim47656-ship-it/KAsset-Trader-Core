# Credential rotation runbook — Sentry leak follow-up (ROB-1305)

> **This document is procedure only. No rotation, revocation, regeneration,
> Sentry data deletion, deploy, or restart is performed by writing or reading
> this file.** Every step below is an **operator-owned decision**: whether to
> rotate at all, which credentials, on what schedule, and how to sequence
> deploys around it. ROB-1305 R1 (W0) scope ends at documenting this
> procedure; see `sentry-pii-leak-evidence-rob1305.md` for the evidence that
> motivates it.

## 1. Why this exists

The evidence inventory in `sentry-pii-leak-evidence-rob1305.md` shows the
Telegram bot token reached Sentry unredacted in span descriptions, error
messages, and log entries before the ROB-1305 scrubber fix. Historical KIS
header-path exposure was also flagged, but KIS credentials are no longer part
of deployed runtime configuration after the provider cutover. This runbook
exists so operator-owned rotations are not improvised during an incident.

## 2. Candidate credentials and dependency map

| Credential | Env var(s) | Used by | Blast radius if rotated without coordination |
|---|---|---|---|
| Telegram bot token | `TELEGRAM_TOKEN` | `app/monitoring/trade_notifier/transports.py` (all Telegram send/edit/callback calls); any deployed process that sends trade notifications | Old token stops working immediately at Telegram's API the moment it is revoked in BotFather — all in-flight and future notification sends fail until every deployed process picks up the new value |

Do not assume this table is exhaustive — before executing any rotation, the
operator should re-derive the current credential surface from
`app/core/config.py` and the relevant broker client, since env var names and
call sites drift over time.

## 3. Safe sequencing (per credential, operator-executed)

This is the general shape; the operator adapts per credential based on the
dependency map above.

1. **Freeze intake for the affected surface.** For Telegram: pause any
   scheduled job that sends Telegram notifications, or accept a short gap in
   notifications during the rotation window. Retired KIS credentials may be
   revoked separately; no deployed KIS process should require coordination.
2. **Generate the new credential** at BotFather without revoking the old one
   yet, if the provider supports having two valid credentials briefly.
3. **Stage the new value** in the deployment's secret manager / env file
   (never committed to the repo — see `CLAUDE.md` env variable conventions).
   Do not print or log the new value; do not paste it into Linear, Slack, or
   this runbook.
4. **Roll the new credential to every process that reads it** — this is a
   config/env change, which for this repo's blue/green deploy means a
   redeploy or restart of the affected service color (see
   `docs/runbooks/` native deploy docs for the current mechanism). A stale
   process holding the old credential in memory will keep failing after the
   old credential is revoked in step 5, so confirm the new value is live
   everywhere before proceeding.
5. **Revoke the old credential** at the provider once every deployed process
   has confirmed use of the new one (see verification in §4).
6. **Roll back path**: if verification in §4 fails after revocation, the only
   recovery is re-issuing another new credential and repeating steps 2-5 — a
   revoked provider credential cannot be un-revoked. This is why step 2
   deliberately avoids revoking the old credential until the new one is
   confirmed live everywhere.

## 4. Post-rotation verification (operator-executed, read-only where possible)

- **Telegram**: confirm a real notification send succeeds end-to-end after
  the new token is deployed (this repo's existing `send_telegram_message`
  path returns a typed `TelegramMethodResult`; a non-`ok` result with
  `error_code=401` after rotation means a process is still holding the old
  token).
- **Sentry**: confirm the ROB-1305 scrubber fix is deployed and active
  *before* generating any new credential, so the new credential's traffic
  does not repeat the same exposure. This is verifiable by the regression
  tests in `tests/test_sentry_init.py` (`test_before_send_transaction_scrubs_*`)
  passing in CI on the deployed release.

## 5. Historical Sentry data — retention and purge decision

The events counted in `sentry-pii-leak-evidence-rob1305.md` already exist in
Sentry (as of the query date) and contain the pre-fix unredacted values. This
runbook does **not** delete them. The operator has three options, in
increasing order of intrusiveness:

1. **Do nothing** and rely on Sentry's normal data retention window to age the
   events out naturally. Check the org's configured retention period in
   Sentry project settings before assuming a specific timeline.
2. **Manually delete the specific issues/events** via the Sentry web UI
   (Sentry's issue-delete action, not an API call from this repo or an
   MCP tool) — scoped to the affected date range, after confirming the
   scrubber fix is deployed so newly-ingested events are already clean.
3. **Rotate the credential regardless of retention**, treating any residual
   Sentry-side copy as a reason to rotate rather than a reason to purge — this
   is likely the higher-leverage action since provider access, not Sentry read
   access, is the actual attack surface.

No Sentry deletion API call, secret-store call, or deploy tool call was made
while producing this document or the evidence inventory it references.

## 6. Explicit non-actions

Nothing in this document or in the ROB-1305 R1 (W0) PR performed: credential
rotation, revocation, regeneration, a Sentry data-deletion API call, a
production deploy, a service restart, or a broker/order/DB mutation. Rotation
scope and timing are operator decisions, to be made after reading this runbook
and the evidence inventory, separately from this PR's merge.
