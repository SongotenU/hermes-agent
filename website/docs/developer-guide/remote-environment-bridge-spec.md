# Remote environment bridge — design spec (B17)

Status: **spec only** — no code ships with this document. Port target: Claude Code
`bridge/` (31 files, design-only); this page states the contract Hermes would
implement and names what already exists so nothing below reinvents it.

## What Hermes already has

- **Human approval over the web**: `web_routers/chat_ws.py` (approval cards over the
  WebSocket) and `tools/approval_gateway_wait.py` (gateway-side wait for a decision).
  An "approve from your phone" flow needs no new transport.
- Per-profile command execution (`terminal` environments incl. docker/ssh backends),
  so a *remote work environment* is a routing question, not a new executor.

## Gap (what the bridge adds)

1. **Environment registration.** A remote work host registers itself with the local
   Hermes (profile-scoped `bridge.environments` record: id, host label, capabilities,
   last_seen). Deregistration is explicit; a stale record is advisory, never killed.
2. **Long-poll with backoff.** The remote pulls work with a long-poll that backs off
   `2s → 600s` (capped) while idle. Every queued message carries an id; the remote acks
   BEFORE executing (at-most-once delivery), and a reconnect resumes from the last ack —
   never from "whatever is in the queue now".
3. **Lease + heartbeat.** A granted work item holds a lease (`lease_ttl`); the remote
   renews by heartbeat. Lease expiry returns the item to the queue (the local side then
   renders "lost contact with <host>", not silent loss). Only one lease per item at a
   time — enforced locally, keyed on the record.
4. **Scope-tight secrets.** The work host receives ONLY the per-lease secret scope
   (`agent.secret_scope` semantics): the secrets the work item declares, never the
   profile's `.env`. A lease that would need a scope the operator did not grant fails
   closed at dispatch.
5. **Approve with updated input.** An approval decision may carry *modified* tool input
   (the human edits the command before approving). The bridge applies the edited input
   as the executed call's arguments and records both original and edited for audit — the
   existing approval-wait path is the renderer; the bridge only transports the edit.

## Non-goals

- No second approval UI: cards ride the existing web-approve surfaces.
- No remote profile mutation: a work host acts for one profile scope pinned by the
  lease, never switches homes mid-lease.
- No worktree-per-session magic: the remote environment is opaque to Hermes (it may be
  a container, an SSH box, or a CI runner); isolation is the environment's contract.

## Invariants

- Ack-before-execute, resume-from-ack (no double execution after reconnect).
- Lease TTL bounds every grant; heartbeat is the only renewal.
- A work item that expires mid-flight is reported to the user as `lease_expired` with
  the last heartbeat time — never silently retried on another host.
- The bridge is profile-scoped like every other multiplex surface (root profile-scope
  rules apply to its poller threads: bind the owning profile, `spawn_context_thread`).
