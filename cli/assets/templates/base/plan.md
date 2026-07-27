---
name: plan
description: "Internal dynos-work skill. Re-run planning on an existing task — regenerates the plan after spec changes, or when the task was started externally. Runs PLANNING → PLAN_REVIEW → PLAN_AUDIT with deterministic artifact validation. Runs only when the user explicitly types /dynos-work:plan; never auto-triggered from conversation or by planning requests."
---

# dynos-work: Plan

Re-runs the planning phase on an existing task. Use this when:
- You need to regenerate the plan after manual spec changes
- The task was initialized externally and skipped planning
- You want to replan after a major scope change

When done, the task is ready for `/dynos-work:execute`.

## What you do

### Step 1 — Find active task

Find the most recent active task in `.dynos/` (manifest.json with stage not DONE/FAILED). If none, print "No active task. Start one with /dynos-work:start" and stop.

Read `manifest.json`, `spec.md`, and `design-decisions.md` (if it exists).

Verify `spec.md` exists. If not, print "No spec found. Run /dynos-work:start to generate one." and stop.

### Step 2 — Generate plan (PLANNING)

Transition the stage to `PLANNING` (transition_task auto-writes the `[STAGE] → PLANNING` log line):

```text
python3 "{{HOOKS_PATH}}/ctl.py" transition .dynos/task-{id} PLANNING
```

Append the spawn line to the execution log:
```
{timestamp} [SPAWN] planning — generate implementation plan
```

Spawn the `planning` agent with instruction: "Generate the implementation plan and execution graph. Read `spec.md` and `design-decisions.md` (if it exists). Human design choices are binding. Write `plan.md` directly to `.dynos/task-{id}/plan.md`. For the execution graph, pipe the JSON payload over stdin and persist the final `.dynos/task-{id}/execution-graph.json` ONLY via `python3 hooks/ctl.py write-execution-graph .dynos/task-{id} --from -`. Include: technical approach, module/component breakdown, data flow, error handling, test strategy, and explicit file ownership per segment. Do not hand-write `.dynos/task-{id}/execution-graph.json`."

Wait for completion. Finalize planning through the deterministic control-plane entrypoint:

```text
python3 "{{HOOKS_PATH}}/ctl.py" run-planning .dynos/task-{id}
```

`run-planning` owns full deterministic validation, writes the `plan-validated` receipt, and advances `PLANNING -> PLAN_REVIEW` when the artifacts are sound.

Append to log:
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

- If **approved**: run the `approve-stage` ctl command below. It hashes the current `plan.md`, writes the `human-approval-PLAN_REVIEW` receipt with that hash, then transitions PLAN_REVIEW → PLAN_AUDIT in one atomic step. The hash is computed from the CURRENT `plan.md` content **at transition time** (the gate re-hashes the file and compares it to `receipt.artifact_sha256`), so an approval that races against a manual edit will be refused with the literal substrings `human-approval-PLAN_REVIEW` and `hash mismatch`. Do NOT add a manual `[HUMAN]` log line — the receipt is the audit trail. Then proceed to Step 4.

  ```text
  python3 "{{HOOKS_PATH}}/ctl.py" approve-stage .dynos/task-{id} PLAN_REVIEW
  ```

  Exit code 0 means success; exit code 1 means the gate refused (stderr identifies the cause). Do not bypass with `transition --force`.
- If **changes requested**: append `{timestamp} [HUMAN] PLAN_REVIEW — changes requested: {summary}` to log. Spawn planning agent again with the feedback. Re-present the updated plan. Repeat until approved. Do NOT call `approve-stage` against a stale plan.
- If **rejected**: run `python3 hooks/ctl.py transition .dynos/task-{id} FAILED`, append `[FAILED] Plan rejected by user`. Stop. Do not edit `manifest.json` directly.

### Step 4 — Spec coverage audit (PLAN_AUDIT)

The `approve-stage` call in Step 3 has already advanced the manifest to `PLAN_AUDIT` — do NOT call `transition .dynos/task-{id} PLAN_AUDIT` here (the state machine would refuse it as `PLAN_AUDIT → PLAN_AUDIT`).

Run the deterministic controller first:

```text
python3 "{{HOOKS_PATH}}/ctl.py" run-plan-audit .dynos/task-{id}
```

If it returns `llm_audit_required`, run the auditor and finalize with:

```text
python3 "{{HOOKS_PATH}}/ctl.py" run-plan-audit .dynos/task-{id} --report-path .dynos/task-{id}/audit-reports/plan-audit-{timestamp}.json --tokens-used {TOTAL_TOKENS} --model {MODEL_USED}
```

If either call returns `replan_required`, repair the plan and rerun the controller. If it returns `passed`, proceed.

### Step 5 — Done

Transition the stage to `PRE_EXECUTION_SNAPSHOT`:

```text
python3 "{{HOOKS_PATH}}/ctl.py" transition .dynos/task-{id} PRE_EXECUTION_SNAPSHOT
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
