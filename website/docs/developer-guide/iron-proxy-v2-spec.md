# iron-proxy v2 — design spec (B18)

Status: **spec only** — no code ships with this document. Port target: Grok
`xai-grok-egress-proxy` (design). Hermes today: `agent/proxy_sources/iron_proxy.py`
(~959 LOC per provider) + the docker-egress fail-closed path. The v2 deltas below are
the four behaviours that file does not model.

## Delta 1 — Per-call attribution (vs. per-provider)

Today attribution is per provider source: "traffic went through the iron provider for
proxy X". V2 tags each egress CALL with an attribution record — requesting session id,
profile home key, tool/turn id, destination host — so cost/quota dashboards and audits
can answer "which conversation spent this proxy budget". The record is written by the
proxy source at dispatch, not reconstructed from timing.

- Keys: `(session_id, turn_id, tool_call_id)` — never credentials.
- Retention: the attribution ring follows the same TTL as proxy logs; no new store.

## Delta 2 — Hold-and-ask

A destination the policy marks `hold` parks the OUTBOUND CONNECTION (bounded `600s`)
and raises an approval card through the existing approval surfaces
(`web_routers/chat_ws.py`, `tools/approval_gateway_wait.py`). Approve → the parked
connection proceeds with the recorded request byte-for-byte; deny/timeout → the call
fails closed with `approval_timeout`, and the failure names the destination and the
card id. On no surface to ask (headless cron), `hold` degrades to `deny` — never to
allow.

## Delta 3 — Observe mode

Per-destination `observe` policy: the call proceeds, and the proxy records a
observation record (attribution + outcome). Observe exists to stage a future `hold` or
`deny` — an operator flips destinations to `observe`, reads a week of records, then
promotes. Observe never blocks, never retries, and is capped by the same ring as
attribution.

## Delta 4 — Double-hold budget

At most `8` concurrent holds per process, and `16` park events per session-hour; past
either budget new `hold` traffic degrades to `deny` with `hold_budget_exhausted`. A
parked connection is a resource; the budget is what stops a hostile destination list
from parking every worker.

## Non-goals

- No proxy-server rewrite: the four deltas ride the existing proxy-source seam.
- No global allow policy object: destinations are classified by the existing per-source
  config; v2 only adds the `observe | hold | deny` tri-state and the budgets above.

## Invariants

- Fail-closed everywhere: unknown destination + no policy = current behaviour (deny in
  docker egress; unchanged elsewhere). The v2 tri-state never widens a deny.
- Hold requires a live approval surface; headless contexts degrade `hold→deny`.
- Attribution/observation records never include secrets or request bodies — host,
  identifiers, outcome class only.
- Budget counters are per process and reset on restart; they are a blast-radius bound,
  not a billing meter.
