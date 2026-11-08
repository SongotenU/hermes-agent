# evals/ — behaviour probes a unit test cannot judge

An eval runs the real code against a real model (or a recorded/scripted provider) and scores what
comes back. Use one exactly where assertions cannot reach: **model-facing text and model behaviour**
— system/agent instructions, tool descriptions and schemas, skill texts, generated titles,
compression summaries, classifier prompts. A unit test proves plumbing; an eval proves the text.

The scripts in this directory are standalone probes run by hand (`python evals/<name>.py` from the
repo root with the test environment active); they are not part of `scripts/run_tests.sh`. Existing
shapes to copy: `approval_deny_dispatch.py` (policy → dispatch), `acp_empty_session_wire.py` (wire
contract), `audit_catalog.py` under `plugin_isolation/` (catalogue re-measurement).

## When to write one

- You changed a prompt, a tool schema/description, a skill that steers tool use, or any text a model
  reads and acts on.
- A model-behaviour bug appeared (wrong tool picked, wrong routing, a title naming the wrong thing):
  add the case that fails on the old text BEFORE the fix, and keep it after.
- Not for: logic, data, state, protocol, persistence — those get ordinary tests in `tests/`.

## Rules for cases

- **Cases are data, not prose.** One file/dict per eval: `id`, where it came from (issue, PR,
  session), and its check or rubric. Adding a case must not require editing the runner.
- **Import the code under test; never copy its text.** The prompt, schema or skill loads from its
  real module at run time, so the eval always tests what ships. A copied prompt is a second source
  of truth that drifts silently.
- **Fail before the fix, pass after.** A case you cannot reproduce on the old text is not a case.
  If you must change a case later, it must still fail against the text it was written for.
- **Add controls.** Include cases where the correct behaviour is ordinary work; they catch a fix
  that overcorrects (a guardrail prompt that starts refusing everything).
- **Sample and compare.** Model output varies: run each case N times (N ≥ 3), a case fails when at
  least half its samples fail, and compare against a baseline run on the same model — never an
  absolute score.
- **No user data.** No real paths, names or content from the user's machine; fixtures use
  `/home/user`, `acme/app` and synthetic data. Read every captured fixture before committing it.
- **Cost is real.** Default to the cheapest model that exercises the behaviour; run only touched
  cases while iterating; one full run per model before merge. Record cost/model in the run summary.
- **A self-grading evaluator is plain logic** — it gets its own unit test in `tests/` and runs in
  CI; a human-graded run must hide which sample is which.

## Where the code under test lives

- Agent/system prompt assembly: `agent/prompt_builder.py`, `agent/context_*.py`.
- Tool schemas and descriptions: `tools/registry.py` registrations (`schema=` argument).
- Skills: `skills/<category>/<name>/SKILL.md` (frontmatter `description` is model-facing too).
- Titles: `agent/title_generator.py`. Compression: `agent/conversation_compression.py`.
