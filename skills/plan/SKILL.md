---
name: plan
description: "Internal dynos-work skill. Re-run planning on an existing task — regenerates the plan after spec changes, or when the task was started externally. Runs PLANNING → PLAN_REVIEW → PLAN_AUDIT with deterministic artifact validation. Runs only when the user explicitly types /dynos-work:plan; never auto-triggered from conversation or by planning requests."
---

# dynos-work: Plan


## Command Funnel (applies to every command in this skill)

Every deterministic step below runs through the plugin CLI. Resolve it once at the start of the skill and substitute the ABSOLUTE path literally into each command you run (permission prefix-matching operates on literal command text):

```bash
PLUGIN_ROOT="${CODEX_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"
if [ -z "$PLUGIN_ROOT" ]; then
  echo "Set CODEX_PLUGIN_ROOT or CLAUDE_PLUGIN_ROOT to the dynos-work plugin root." >&2
  exit 2
fi
DYNOS="${PLUGIN_ROOT}/bin/dynos"   # resolve once; use the absolute path in every command
```

`"$DYNOS" ctl <subcommand>` wraps `hooks/ctl.py`; `"$DYNOS" hook <script> ...` wraps helper scripts (router, lib_tokens, build_prompt_context, ...) with PYTHONPATH handled internally. A permissions-ON user can allow the single `<plugin-root>/bin/dynos` prefix once instead of approving every call.

JSON payloads (classification, execution-graph, repair-log) are piped to the ctl wrapper over stdin with `--from -` and a heredoc — NEVER staged at `/tmp` or any raw filesystem path (the write policy denies those, by design). Temp files, when genuinely needed, belong in `.dynos/task-{id}/_scratch/`.

Re-runs the planning phase on an existing task. Use this when:
- You need to regenerate the plan after manual spec changes
- The task was initialized externally and skipped planning
- You want to replan after a major scope change

When done, the task is ready for `/dynos-work:execute`.

## Ruthlessness Standard

- Treat a vague plan as a delayed execution failure.
- Do not accept coverage by implication. Every acceptance criterion needs an explicit owner and mechanism.
- If the plan does not name failure modes, rollback impact, and verification strategy, it is incomplete.

## What you do

### Step 1 — Find active task

Find the most recent active task in `.dynos/` (manifest.json with stage not DONE/FAILED). If none, print "No active task. Start one with /dynos-work:start" and stop.

Read `manifest.json`, `spec.md`, and `design-decisions.md` (if it exists).

Verify `spec.md` exists. If not, print "No spec found. Run /dynos-work:start to generate one." and stop.

### Step 2 — Generate plan (PLANNING)

Transition the stage by running:

```text
"$DYNOS" ctl transition .dynos/task-{id} PLANNING
```

Append to execution log (transition_task already auto-logged the `[STAGE] → PLANNING` line; only emit the `[SPAWN]` line):
```
{timestamp} [SPAWN] planning — generate implementation plan
```

Spawn the `planning` agent with instruction: "Generate the implementation plan and execution graph. Read `spec.md`, `design-decisions.md` (if it exists), and `design-doc.md` (if it exists — §11 lists the intended segment shape your execution graph must match). Human design choices are binding. Write `plan.md` directly to `.dynos/task-{id}/plan.md`. For the execution graph, persist the final `.dynos/task-{id}/execution-graph.json` ONLY via `"$DYNOS" ctl write-execution-graph .dynos/task-{id} --from -`, piping the JSON payload over stdin with a heredoc (never stage it at /tmp or any raw path). Include: technical approach, module/component breakdown, data flow, error handling, failure modes, rollback or migration risk where relevant, test strategy, and explicit file ownership per segment. Do not leave any acceptance criterion covered only by implication. Do not hand-write `.dynos/task-{id}/execution-graph.json`."

Wait for completion. Finalize planning through the deterministic control-plane entrypoint. Run:

```text
"$DYNOS" ctl run-planning .dynos/task-{id}
```

`run-planning` owns full deterministic artifact validation, writes the `plan-validated` receipt, and advances `PLANNING -> PLAN_REVIEW` when the artifacts are sound. If it exits non-zero, the JSON payload tells you exactly why replanning is required.

Append to log (the ctl command above may already advance to `PLAN_REVIEW`; only emit the `[DONE]` line):
```
{timestamp} [DONE] planning — plan.md written
```

### Step 3 — Human review (PLAN_REVIEW)

Read `plan.md`. Present to the user using **AskUserQuestion**:

```
=== Plan Review ===

[contents of plan.md]

---
Approve this plan? (yes / no + what to change)
```

- If **approved**: run the `approve-stage` ctl command below. It hashes the current `plan.md`, writes the `human-approval-PLAN_REVIEW` receipt with that hash, then transitions PLAN_REVIEW → PLAN_AUDIT in one atomic step. The hash is computed from the CURRENT `plan.md` content **at transition time** (the `transition_task` gate re-hashes the file and compares it to `receipt.artifact_sha256`), so an approval that races against a manual edit to `plan.md` after the receipt is written will be refused with the literal substrings `human-approval-PLAN_REVIEW` and `hash mismatch`. Do NOT add a manual `[HUMAN]` log line — the receipt is the audit trail. Then proceed to Step 4.

  ```text
  "$DYNOS" ctl approve-stage .dynos/task-{id} PLAN_REVIEW
  ```

  Exit code 0 means success; exit code 1 means the gate refused (stderr identifies the cause: missing artifact, hash drift, illegal transition). Do not bypass with `transition --force`.
- If **changes requested**: append `{timestamp} [HUMAN] PLAN_REVIEW — changes requested: {summary}` to log. Spawn planning agent again with the feedback. Re-present the updated plan. Repeat until approved. Do NOT call `approve-stage` against a stale plan — the next time you call it, the live `plan.md` content (and therefore its hash) MUST match the version the user just approved, otherwise the transition will be refused.
- If **rejected**: run `"$DYNOS" ctl transition .dynos/task-{id} FAILED`, append `[FAILED] Plan rejected by user`. Stop. Do not edit `manifest.json` directly.

### Step 4 — Spec coverage audit (PLAN_AUDIT)

The `approve-stage` call in Step 3 has already advanced the manifest to `PLAN_AUDIT` and auto-logged the `[STAGE] → PLAN_AUDIT` transition line — do NOT call `transition .dynos/task-{id} PLAN_AUDIT` here (the state machine would refuse it as `PLAN_AUDIT → PLAN_AUDIT`).

Run the deterministic plan-audit controller first:

```text
"$DYNOS" ctl run-plan-audit .dynos/task-{id}
```

Interpret the JSON result:

- `status == "replan_required"`: deterministic gap analysis found fake or weak claims. Replan before any LLM audit.
- `status == "passed"` with `mode == "deterministic_only"`: low/medium-risk task. No LLM plan-audit is needed.
- `status == "llm_audit_required"`: high/critical-risk task. Spawn `spec-completion-auditor`, then finalize with:

  ```text
  "$DYNOS" ctl run-plan-audit .dynos/task-{id} --report-path .dynos/task-{id}/audit-reports/plan-audit-{timestamp}.json --tokens-used {TOTAL_TOKENS} --model {MODEL_USED}
  ```

If the final `run-plan-audit` call returns `status == "replan_required"`, repair the plan and rerun it. If it returns `status == "passed"`, proceed.

### Step 5 — Done

Transition the stage by running:

```text
"$DYNOS" ctl transition .dynos/task-{id} PRE_EXECUTION_SNAPSHOT
```

Append to log:
```
{timestamp} [ADVANCE] PLAN_AUDIT → PRE_EXECUTION_SNAPSHOT
```

Print:
```
Plan approved and audited. All acceptance criteria covered.

Next: /dynos-work:execute
```
