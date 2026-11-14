# Context handoff

Work crosses agent boundaries all the time: a parent spawns a subagent, Hermes drives Claude Code or
Codex on a coding task, a session continues on a different model, a side question peels off from the
main thread. Every one of those crossings is a **context transfer**, and the failure mode is always
the same — the receiving side either gets pasted raw history (noise, stale tool output, hidden
reasoning) or gets nothing and re-derives wrong assumptions.

This page defines the handoff contract Hermes uses when it moves work across a boundary. It is
adapted from the Context-Transfer model in T3 Code's `orchestration-v2` docs (pingdotgg/t3code,
repo pin `6577c6b61851`, MIT): relationship + source point + lazy resolution, with the handoff
summary as an explicit, auditable artifact.

## The model

```text
transfer = thread relationship + source point + optional context handoff
```

- **Relationship** — why the target exists relative to the source. One of:
  `subagent_spawn`, `subagent_result`, `fork`, `provider_handoff`, `merge_back`.
- **Source point** — the exact state being transferred. Prefer a **completed turn** or a
  checkpoint (Hermes checkpoints: `checkpoints.enabled`). Never transfer from a mid-stream turn;
  partial state cannot be replayed and poisons the target.
- **Resolution** — how the target receives the source. Pick the first rung that applies:

| Rung | When | Artifact |
|---|---|---|
| native | same harness keeps its own live session | none — pass the handle |
| delta | target already covered runs 1..N, source advanced past them | delta summary |
| full | target can resume but the delta chain is too complex | full handoff summary |
| fresh | target cannot resume, settings incompatible, or context is stale | full summary + fresh context |
| refuse | no safe reconstruction path | fail explicitly before work starts |

## The handoff artifact

A handoff is explicit data, not an invisible prompt hack. It names its source, its coverage, and how
it was produced, so it can be audited later:

- `from` / `to` — source session/turn and target session.
- `covered` — the turn range or checkpoint the summary covers.
- `strategy` — `delta` | `full` | `checkpoint` | `manual`.
- `summary` — the text (see checklist below).
- `created_by` — which agent/model produced it (quality signal when reviewing later).

If the same summary is regenerated, the old one is superseded, never silently mutated.

## What the summary must contain

Canonical thread state, not a transcript replay:

- goal and hard constraints (what is out of scope counts);
- decisions made and the reasons;
- files created/modified, and the current git branch;
- commands and tests run, with their verdicts;
- todo/plan state and what remains;
- open failures and known-bad paths;
- pending questions or approvals;
- for subagent results: the child's answer + artifacts, not its whole log.

Never include: raw tool output superseded by later turns, hidden reasoning, provider protocol
noise, or a large diff where a file summary suffices (reference paths/commits instead).

## Budget

A handoff summary is bounded by the **target's** context policy, not the source's verbosity: cap the
summary, prefer file references over pasted content, and shrink by raising the strategy rung
(delta before full). A summary that alone overflows the target is a signal the transfer should
have been a fork with lazy resolution, not a dump.

## Staleness and rollback

- Staleness signals: too many off-target turns since the source, too many chained handoffs, the
  model/runtime changed incompatibly, a resume failed, or the user asked for a clean context.
  Stale → rebuild as `fresh` from the app-canonical history.
- Rollback invalidates everything that covers rolled-back turns: handoffs covering them are marked
  superseded, and a target resuming from them is divergent. Re-derive from the retained history.

## How this maps to Hermes today

- **`delegate_task`** — `subagent_spawn`/`subagent_result`. The child receives ONLY `goal` +
  `context` (+ optional role/schema); parent history is never copied. Write `context` AS a handoff
  summary (checklist above), not as pasted conversation. The child's final answer is the
  `subagent_result` transfer back.
- **`/btw`** — a side question is a read-only view of the parent context, not a transfer: nothing
  is carried back, so no artifact is needed.
- **`/model` switch inside a session** — the message list itself is the canonical history and it
  travels whole; no handoff summary is produced (switching providers mid-thread is an in-place
  change, not a transfer). The frozen launch policy is what pins credentials, not context.
- **Cross-harness delegation** (Claude Code / Codex CLIs driven from skills) — a real cross-provider
  transfer in the sense of this model: the prompt you compose IS the handoff artifact. Use the
  checklist; cap it by the CLI's context; tell the target what changed since the last handoff
  (`delta`), not the whole story again.
- **Kanban / cron** — cards and jobs carry their own durable description; treat a card body as a
  handoff artifact and a completed card as a `subagent_result`.

A full lineage graph (persisted transfers, run ordinals, lazy fork resolution) is deliberately not
implemented: Hermes' canonical history is the session message list plus checkpoints, and the
crossings we have are covered by the contract above. Revisit only if side chats, forks, or
merge-back become first-class surfaces.

## Ground rules

1. One transfer, one artifact: name source, coverage, strategy.
2. Prefer the cheapest rung; escalate only when resume is impossible.
3. Summaries describe the canonical thread, never the provider's transcript.
4. Never transfer mid-turn state.
5. Rollback supersedes, it never patches a summary in place.
6. On no safe path, refuse **before** the target starts working.
