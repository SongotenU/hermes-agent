# Coordination doctrine — continue vs. spawn

Design port of Claude Code's coordinator doctrine (`coordinator/coordinatorMode.ts`),
expressed as rules Hermes already enforces at runtime where it can. The model-facing
summary lives in the `delegate_task` description tail; this page is the long form.

## 1. Continue vs. spawn — decide by context overlap

The child starts with zero conversation context. If the task needs what you already
read, spawning pays an expensive re-read; if it needs a fresh scope, spawning isolates
noise. Decide on overlap, not task size:

| Situation | Do |
| --- | --- |
| You already hold most of the needed context in this conversation | **Continue yourself.** |
| Independent file/module scope, or noisy exploration you don't want in your transcript | **Spawn** (one child per scope; batch independent ones in `tasks`). |
| Overlap is partial: shared background, disjoint deliverables | **Spawn**, repeating the shared background in each child's `context` (children see only their own). |
| Two children would read the same 10 files | **Split by file, not by task** — or continue serially. |
| The work is a single tool call | **Neither** — just call the tool. |

## 2. Synthesize — never outsource your own turn

The parent owns the answer. After children return:

- Answer from their findings **directly**, in your own response.
- Never reply "based on your findings…", never paste a child summary as your reply,
  and never tell the user to read the child's output.
- Child summaries are self-reports, not verified facts: for anything with an external
  side effect, verify the handle yourself (URL, ID, path) before claiming success.

## 3. Fresh-eyes verifier

Verification deserves a context the implementer can't poison: spawn the reviewer
**without** the implementation conversation (no rationale, no "we already tried X"),
pointing only at the artifact (diff, files, running instance). A verifier that inherits
the implementer's assumptions will rubber-stamp them. Keep review output as findings;
the parent applies fixes.

## 4. Scratchpads stay out of prompts

A child's scratchpad (drafts, half-formed notes, dead ends) never belongs in a prompt
or a handoff. Pass the distilled result: goal, inputs, constraints. If a child needs
its own memory across steps, it uses its own files/terminal — not a transcript dump.

## 5. Tool allowlists go in the child's context

Children cannot see the parent's toolset reasoning. State, in the child's `context`,
exactly which tools/paths/skills the task may use — and the ones it must not assume
(e.g. "no browser; fetch with `web_extract`"). Hermes narrows the child's surface via
the `toolsets` parameter; the context sentence is what stops the child *assuming*
capabilities it lacks. Capability depth (whether a child may itself delegate) is
depth-derived at runtime (`delegation.max_spawn_depth`) — never promise it in prose.

## Runtime hooks

- `delegate_task` description tail (`tools/delegate_tool.py::_DESCRIPTION_TAIL`) carries
  the model-facing digest of rules 1–2 and 5.
- Child isolation and toolset narrowing: `tools/AGENTS.md` § Delegation and
  [subagent-lifecycle-api.md](subagent-lifecycle-api.md).
- Verification gates for the parent's own changes: `omh-verification-gate` /
  `requesting-code-review` skills.
