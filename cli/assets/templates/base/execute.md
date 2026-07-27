---
name: execute
description: "Execute the approved plan. Orchestrates execution graph segments through specialized executor agents, including dependency management and error recovery. Runs only when the user explicitly types /dynos-work:execute; never auto-triggered from conversation."
---

# dynos-work: Execute Skill

Manages the parallel execution of the implementation plan via the execution graph. Handles dependency ordering, executor spawning, and final integration.

## What you do

### Step 0 — Contract validation

Validate that all required inputs from the start skill are present:

```text
python3 "{{HOOKS_PATH}}/ctl.py" validate-contract --skill execute --task-dir .dynos/task-{id}
```

If validation fails with missing required inputs, print the errors and stop. Do not proceed with execution.

### Step 1 — Review the Plan

Read `spec.md`, `plan.md`, and `execution-graph.json`. Ensure you understand the dependency chain.

### Step 2 — Snapshot the Base

Before any modification starts:
1. Capture the current HEAD SHA.
2. Create a temporary git branch `dynos/task-{id}-snapshot`.

Append to log:
```
{timestamp} [DECISION] snapshot created — branch dynos/task-{id}-snapshot at {head_sha}
```

### Step 3 — Execute segments (Optimized Scheduler)

Run deterministic execute setup first:

```text
python3 "{{HOOKS_PATH}}/ctl.py" run-execute-setup .dynos/task-{id}
```

This command runs execution preflight validation, advances `PRE_EXECUTION_SNAPSHOT -> EXECUTION`, builds the executor plan, and writes the `executor-routing` receipt. Use its JSON output directly.

`run-execute-setup` already performed deterministic preflight validation and stage advancement. If it failed, repair the plan first; do not patch around a broken plan during execution.

Immediately compute the authoritative execution schedule:

```bash
python3 "{{HOOKS_PATH}}/ctl.py" run-execution-batch-plan .dynos/task-{id}
```

This command is authoritative for:
- segment state: `completed | cached | pending`
- cache eligibility and cache rejection reasons
- dependency depth / critical path
- next runnable batch and remaining batches

Do NOT re-derive dependency depth, cache reuse, or runnable batches in prompt logic. Consume the JSON output and follow it.

If `cached_segments` is non-empty, treat those segments as already satisfied and reuse their evidence files. Append to log: `{timestamp} [CACHE] {segment-id} — skipping (inputs unchanged)`.

If `next_batch` is empty while `pending_segments` is non-empty, stop and fail the task as blocked. That means dependency resolution or receipt state is inconsistent; do not guess.

Progressive auditing remains allowed, but only after a segment is deterministically known complete or cached.

**Deterministic routing (MANDATORY):** Use the `segments` payload returned by `run-execute-setup` as the authoritative executor plan. Do not rebuild routing in prompt logic. The `executor-routing` receipt has already been written.

Do NOT read project_rules.md tables manually. The router handles model policy, agent routing, and security floors deterministically.

**Learned Agent Injection (MANDATORY — NOT OPTIONAL):** For every segment, you MUST build the executor prompt using the deterministic prompt builder. This is not a suggestion. This is an enforcement gate. For each segment in the executor plan:

```bash
echo "{your base prompt for this segment}" | PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/router.py" inject-prompt --root . --task-type {task_type} --graph .dynos/task-{id}/execution-graph.json --segment-id {seg-id}
```

This command:
1. Reads the base prompt from stdin
2. Looks up the routing decision for this segment (model, route_mode, agent_path)
3. If `route_mode` is `replace` or `alongside`, reads the learned agent `.md` file and appends its rules to the prompt
4. Appends prevention rules from the project's history
5. Logs a `learned_agent_applied` event to `.dynos/events.jsonl`
6. Prints the complete prompt to stdout

**Use the OUTPUT of this command as the prompt for the Agent tool spawn.** Do NOT construct the executor prompt yourself. Do NOT skip this step. If you spawn an executor without running inject-prompt first, the learned agent is silently ignored and the whole self-learning system is broken.

**Sidecar capture (MANDATORY).** `cmd_inject_prompt` writes the SHA-256 of the exact bytes it printed to stdout to `.dynos/task-{id}/receipts/_injected-prompts/{seg-id}.sha256` (single-line lowercase hex digest, no trailing newline) plus a companion `.txt` of the same bytes. Both files are written atomically via tempfile + `os.replace`, so a retry overwrites cleanly. Immediately after `inject-prompt` returns, read the sidecar:

```bash
INJECTED_PROMPT_SHA256=$(cat .dynos/task-{id}/receipts/_injected-prompts/{seg-id}.sha256)
```

You MUST pass this captured digest to `receipt_executor_done(...)` below as `injected_prompt_sha256`. The receipt writer asserts the same sidecar exists and matches; a mismatch raises `ValueError` with the literal substrings `injected_prompt_sha256 sidecar missing` or `injected_prompt_sha256 mismatch`.

If `inject-prompt` fails or is unavailable, stop and fix the deterministic routing path. Do NOT manually read `agent_path` or hand-build the learned-agent prompt.

**Model selection:** Pass the `model` field from the executor plan as the model parameter when spawning the agent. If `model` is null, use default (omit the model parameter).

**TDD-First Awareness:** Check if `.dynos/task-{id}/evidence/tdd-tests.md` exists. If it does, include this instruction in the base prompt (before piping through inject-prompt): "A TDD test suite has already been committed. Your implementation must make those tests pass. Do NOT write new tests or modify existing test files."

Spawn the `next_batch` executor agents in parallel.

Executor agents by type:
- `ui-executor`, `backend-executor`, `ml-executor`, `db-executor`, `refactor-executor`, `testing-executor`, `integration-executor`, `docs-executor`, `infra-executor`, `security-executor`, `data-executor`, `observability-executor`, `release-executor`.

The base prompt for each executor (before inject-prompt) must include:
1. Its specific segment object from `execution-graph.json`
2. The full text of each acceptance criterion referenced by the segment's `criteria_ids` field, extracted from `spec.md`
3. Evidence files from dependency segments: for each segment ID in the executor's `depends_on` list, read `.dynos/task-{id}/evidence/{dependency-segment-id}.md` and include its contents
4. Instruction to write evidence to `.dynos/task-{id}/evidence/{segment-id}.md`

Do NOT pass the full `spec.md` or `plan.md` to executors.

**Segment finalization (MANDATORY):** After each executor completes, finalize the segment through ctl instead of hand-writing receipt / ownership / manifest logic:

```bash
python3 "{{HOOKS_PATH}}/ctl.py" run-execution-segment-done \
  .dynos/task-{id} \
  "{seg-id}" \
  --injected-prompt-sha256 "{INJECTED_PROMPT_SHA256 captured from sidecar}" \
  --model "{model from plan or null}" \
  --agent-name "{agent_name from plan or None}" \
  --executor-type "{executor from plan}" \
  --evidence-path ".dynos/task-{id}/evidence/{seg-id}.md" \
  --tokens-used {total_tokens from Agent result or None}
```

This command deterministically:
- verifies segment ownership against `execution-graph.json`
- verifies the evidence file exists
- writes `receipt_executor_done(...)`
- updates `manifest.json.execution_progress` from deterministic execution state

After each batch (or cached resolution) completes, record events and verify:

- **Token capture (executor spawn):** After each executor returns:
  ```bash
  PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/lib_tokens.py" record \
    --task-dir .dynos/task-{id} \
    --agent "{executor_name}-{segment-id}" \
    --model "{model_name}" \
    --input-tokens {input_tokens} \
    --output-tokens {output_tokens} \
    --phase execution \
    --stage "EXECUTION" \
    --type spawn \
    --segment "{segment-id}" \
    --detail "{what the executor implemented}"
  ```
- **Token capture (deterministic checks):** After file ownership and evidence verification:
  ```bash
  PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/lib_tokens.py" record \
    --task-dir .dynos/task-{id} \
    --agent "ctl-check-ownership" \
    --model "none" \
    --input-tokens 0 \
    --output-tokens 0 \
    --phase execution \
    --stage "EXECUTION" \
    --type deterministic \
    --segment "{segment-id}" \
    --detail "Verified file ownership and evidence — {pass|fail}"
  ```
- Also record the **router decision** before each batch:
  ```bash
  PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/lib_tokens.py" record \
    --task-dir .dynos/task-{id} \
    --agent "router-executor-plan" \
    --model "none" \
    --input-tokens 0 \
    --output-tokens 0 \
    --phase execution \
    --stage "EXECUTION" \
    --type deterministic \
    --detail "Router: {executor}={model} route={route_mode}"
  ```
- Use `run-execution-segment-done` as the authoritative completion gate.
- Append to log: `{timestamp} [DONE] {segment-id} — complete`.
- Re-run `run-execution-batch-plan` to get the next authoritative batch.

**For inline fast-track execution** (no subagent spawn), record with `--type inline`:
```bash
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/lib_tokens.py" record \
  --task-dir .dynos/task-{id} \
  --agent "inline-executor" \
  --model "none" \
  --input-tokens 0 \
  --output-tokens 0 \
  --phase execution \
  --stage "EXECUTION" \
  --type inline \
  --segment "seg-1" \
  --detail "Fast-track inline execution (no subagent spawn)"
```

**Test execution** (Step 4): Record test runner events with `--phase execution --stage TEST_EXECUTION`.

When `run-execution-batch-plan` reports no pending segments, advance through ctl:

```bash
python3 "{{HOOKS_PATH}}/ctl.py" run-execution-finish .dynos/task-{id}
```

This command refuses the transition if any segment is still pending. Do not manually decide that execution is complete.

### Step 4 — Run tests

Read `plan.md` Test Strategy. Run the specified tests. Use incremental testing if the framework supports it (e.g. `jest --onlyChanged`).

If an approved TDD suite exists, run those tests first as the blocking contract suite. If they pass, run any broader regression suite required by `plan.md`.

If tests pass:
- Append to log: `{timestamp} [DONE] tests — passed`
- Continue to Audit

If tests fail:
- Append to log: `{timestamp} [FAIL] tests — failed. Repairs required.`
- Update `manifest.json` stage to `REPAIR`
- Trigger the `repair` skill

### Step 5 — Verify completion

After successfully completing execution and tests (and any repairs triggered by tests or audit), verify all referenced `criteria_ids` from `execution-graph.json` are represented in the finalized code. 

Deterministically verify before completion:
- Every segment has an evidence file or a valid cached status.
- Every approved test file still exists unless the plan explicitly superseded it.
- No segment modified files outside its declared ownership without corresponding repair evidence.

Append to log:
```
{timestamp} [DONE] execute — all segments complete and tested
```

### Handoff — Write handoff record

After Step 5, run:

```text
python3 hooks/ctl.py write-execute-handoff .dynos/task-{id}
```

This writes `.dynos/task-{id}/handoff-execute-audit.json` deterministically from the live manifest stage. Do NOT hand-write the handoff JSON in prompt logic.

## Hard Rules
- **No speculative implementation:** Executors must stay strictly within their segment's `files_expected` and `criteria_ids`.
- **Atomic evidence:** Evidence files must be written only after the segment's implementation is complete.
- **Dependency discipline:** Never spawn an executor before its dependency evidence files are available.
- **Caching Discipline:** Never skip a segment if its `files_expected` have any uncommitted changes not reflected in the existing evidence.
- **Ownership discipline:** A segment that edits files outside `files_expected` is invalid until repaired and re-evidenced.
