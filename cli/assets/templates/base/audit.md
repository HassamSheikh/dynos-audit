---
name: audit
description: "Run checkpoint audit, repair any findings, then reach DONE — all in one shot. Runs only when the user explicitly types /dynos-work:audit; never auto-triggered from conversation."
---

# dynos-work: Audit

Runs the full audit-to-done pipeline: audit → repair loop → DONE.

## What you do

### Step 0 — Contract validation

After finding the active task, validate that all required inputs from the execute skill are present:

```text
python3 "{{HOOKS_PATH}}/ctl.py" validate-contract --skill audit --task-dir .dynos/task-{id}
```

If validation fails with missing required inputs (evidence files, snapshot SHA), print the errors and stop.

### Step 1 — Find active task

Find the most recent active task in `.dynos/`. Read `manifest.json`.

Verify stage is `CHECKPOINT_AUDIT`. If not, print the current stage and what command to run instead.

### Step 2 — Audit Setup

Build the deterministic audit setup first:

```text
python3 "{{HOOKS_PATH}}/ctl.py" run-audit-setup .dynos/task-{id}
```

This command reads `manifest.json`, derives the audit plan, writes `.dynos/task-{id}/audit-plan.json`, and computes diff scope from `snapshot.head_sha` (or `HEAD` fallback when needed). Use its JSON output directly. Do not hand-derive diff scope, task type, domains, fast-track, skip policy, or model selection in prompt logic.

### Step 3 — Audit (conditional auditor spawning with skip optimization)

For each auditor in the plan:
- If `action: "skip"`: log `{timestamp} [SKIP] {name} — {reason}` and do not spawn
- If `action: "spawn"`: spawn with the model returned by `python3 "{{HOOKS_PATH}}/ctl.py" ensemble-next` — the plan `model` is only used for non-ensemble auditors (null = default)
- Log: `{timestamp} [ROUTE] {name} model={model} route={route_mode} source={route_source}`

**Learned Auditor Injection (MANDATORY — deterministic via router):** Build the auditor's spawn prompt with `router.py audit-inject-prompt`. Pipe the base prompt over stdin and capture stdout as the prompt you pass to the Agent tool — do NOT read the learned agent file yourself or build the prompt by hand. The router does the frontmatter stripping, applies the learned-auditor block under the literal heading `## Learned Auditor Instructions`, computes the SHA-256 of the exact bytes it prints, and atomically writes the per-model sidecar at `.dynos/task-{id}/receipts/_injected-auditor-prompts/{auditor_name}-{model_used}.sha256` (with a companion `.txt` of the same bytes; when no model is specified the literal `default` is substituted in the filename).

```bash
echo "{base prompt for this auditor}" | PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/router.py" audit-inject-prompt \
  --root . \
  --task-type {task_type} \
  --audit-plan .dynos/task-{id}/audit-plan.json \
  --auditor-name {name} \
  --model {model}
```

The command logs `learned_auditor_applied`, `learned_auditor_missing`, or `learned_auditor_error` to `.dynos/events.jsonl` depending on whether the named auditor's `agent_path` exists and could be read. On any IO failure the command exits 1 and prints a JSON error to stderr — fix the cause (audit-plan path, auditor name, or `--task-type`) before retrying.

After each auditor spawn returns, you MUST write the audit receipt via the deterministic ctl wrapper below. Do NOT hand-write a Python `receipt_audit_done(...)` call. The wrapper derives `finding_count` and `blocking_count` from the on-disk report file so the model cannot mix one auditor/model's counts with another auditor/model's report. The captured digest must be the contents of the sidecar file the router just wrote — read it back rather than re-hashing:

```bash
INJECTED_AGENT_SHA256=$(cat .dynos/task-{id}/receipts/_injected-auditor-prompts/{auditor_name}-{model_used}.sha256)
python3 "{{HOOKS_PATH}}/ctl.py" audit-receipt .dynos/task-{id} {auditor_name} \
  --model {model_used} \
  --report-path .dynos/task-{id}/audit-reports/{report_filename}.json \
  --tokens-used {tokens_used} \
  --route-mode {route_mode} \
  --agent-path {agent_path} \
  --injected-agent-sha256 "${INJECTED_AGENT_SHA256}"
```

`python3 "{{HOOKS_PATH}}/ctl.py" audit-receipt ...` calls `receipt_audit_done(...)`, which re-asserts the same sidecar exists at that exact path and that its contents match `injected_agent_sha256`. A mismatch raises `ValueError`. For `route_mode == "generic"` (no learned agent) the sidecar assertion is skipped and `injected_agent_sha256` may be `None`; `route_mode` and `agent_path` are still required keyword arguments. The wrapper derives counts from `--report-path`; when no report exists it writes literal zero findings only. The `receipt_audit_routing` writer also enforces these fields per-entry, so any auditor entry missing `injected_agent_sha256` (when non-generic) or `agent_path` will hard-fail at the routing-receipt write.

The router handles fast-track reduction, skip policy, model policy, security floor enforcement, ensemble voting triggers, and learned agent routing in deterministic code. No prompt interpretation needed for these decisions. Do not re-derive skip thresholds, model assignments, or routing modes from markdown tables or retrospective files.

**Ensemble Voting (deterministic — computed by ctl, never by prompt logic):** The router marks auditors `"ensemble": true`. For those auditors the plan's `model` field is NOT the spawn model. Before every audit batch, run:

```bash
python3 "{{HOOKS_PATH}}/ctl.py" ensemble-next .dynos/task-{id}
```

It evaluates every spawn-action auditor against the shard receipts on disk and returns, per auditor, one of:

- `"status": "spawn"` with the exact `model` and `shard_step_name` to use now (`reason` is `cascade_step` for the next voting tier or `escalate_on_findings` for the deep tier);
- `"status": "complete"` with `verdict` `pass` (every voting tier clean) or `escalated` (deep tier ran; its verdict is final and binding);
- `"status": "single"` for non-ensemble auditors, carrying the plan `model`.

The audit batch is a loop: spawn every auditor whose status is `spawn` or `single` (in parallel, at the returned `model`), write each receipt, then run `ensemble-next` again. Stop only when the output reports `"complete": true`. Do not decide by hand whether a tier is needed — the cascade (fast tier → balanced tier on zero findings → deep tier on any finding) is computed by ctl. Log each ensemble spawn as `{timestamp} [VOTE] {name} — {reason} → {model}`.

**Enforcement (there is no shortcut):** `router.py audit-inject-prompt` refuses to build a prompt for an ensemble auditor at any model other than the one `ensemble-next` returns; `audit-receipt` refuses a shard receipt written out of cascade order; `run-audit-summary` and the DONE gate refuse while any cascade is incomplete. A single deep-tier spawn does not satisfy an ensemble auditor. On null-model hosts the router sets `"ensemble": false` and the auditor spawns once at the plan model.

**Visual Audit Pass:** For tasks where `domains` includes `"ui"`, run a visual audit: start the dev server, use a browser subagent to screenshot modified screens, then evaluate with Claude 3.5 Sonnet against the planning-phase Design Decisions. Report visual findings as category `vision-finding`. Log: `{timestamp} [VISION] UI audit complete -- {N} visual bugs found`.

**Alongside mode deduplication:** When generic and learned auditors both produce findings for the same role, deduplicate by key `{file}:{line}:{category}`. Count duplicates once, preferring the learned version. The deduplicated set feeds into repair and retrospective counts. Track whether learned findings are a superset of generic findings (for promotion decisions by the learn step).

Append to log (the `[STAGE] → CHECKPOINT_AUDIT` line is auto-written by `transition_task`; the skill writes only the `[SPAWN]` line):
```
{timestamp} [SPAWN] {N} auditors in parallel ({list of names})
```

Spawn the determined auditors simultaneously, passing for each auditor the model returned by `ensemble-next` (never the plan `model` for an ensemble auditor) in the subagent spawn configuration. For alongside-mode auditors, this means two spawns for that role (generic + learned), both counted in {N}.

Each writes its report to `.dynos/task-{id}/audit-reports/{auditor}-checkpoint-{timestamp}.json`.

**Token & event capture (applies to all events in Steps 3-5):** After each subagent spawn AND each deterministic check, record the event:

**For LLM subagent spawns** (auditors, repair executors):
```bash
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/lib_tokens.py" record \
  --task-dir .dynos/task-{id} \
  --agent "{agent_name}" \
  --model "{model_name}" \
  --input-tokens {input_tokens} \
  --output-tokens {output_tokens} \
  --phase audit \
  --stage "AUDITING" \
  --type spawn \
  --detail "{what the agent did}"
```

**For deterministic steps** (router decisions, retrospective computation, repair-log validation):
```bash
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/lib_tokens.py" record \
  --task-dir .dynos/task-{id} \
  --agent "{tool_name}" \
  --model "none" \
  --input-tokens 0 \
  --output-tokens 0 \
  --phase audit \
  --stage "AUDITING" \
  --type deterministic \
  --detail "{result summary}"
```

**For repair executor spawns**, use `--phase repair` and include `--segment {segment-id}` if the repair targets a specific segment.

Run this after EVERY event. The hook writes to `.dynos/task-{id}/token-usage.json` with a chronological event log plus aggregated totals. The retrospective's token fields are populated from this file.

**Specific events to record in this skill:**
- Step 3: dynorouter audit-plan (type=deterministic, detail="Router decided: spawn X, skip Y")
- Step 3: Each auditor spawn (type=spawn, phase=audit)
- Step 4: repair-log build (type=deterministic, phase=repair)
- Step 4: Each repair executor spawn (type=spawn, phase=repair, include --segment)
- Step 4: ctl check-ownership after repair (type=deterministic, phase=repair)
- Step 4: Re-audit spawns (type=spawn, phase=audit, detail="Re-audit after repair cycle N")
- Step 5: compute_reward / validate_retrospective_scores (type=deterministic, phase=audit, detail="Computed retrospective scores")

**Audit repair gate (deterministic):**

Decide whether audit proceeds to repair or reflect through ctl:

```bash
python3 "{{HOOKS_PATH}}/ctl.py" run-audit-findings-gate .dynos/task-{id}
```

Use the JSON output as authoritative:
- `status == "repair_required"`: proceed to Step 4
- `status == "clear"`: skip Step 4 and proceed to Step 5

Do NOT re-count blocking findings, infer critical spec failure, or decide repair-vs-reflect from prompt logic.

### Step 4 — Deterministic repair loop

This step runs only when blocking findings exist.

**If `run-audit-findings-gate` returns `status == "clear"`:** skip this step entirely. Proceed to Step 5.

Build the repair queue through ctl:

```bash
python3 "{{HOOKS_PATH}}/ctl.py" run-audit-repair-cycle-plan .dynos/task-{id}
```

Use this JSON output as authoritative:
- `phase` is the current repair-cycle label (`phase_1`, `phase_2`, or `repair_cycle_N`)
- `repair_cycle` is the exact cycle number the coordinator must write to `repair-log.json`
- `blocking_findings` is the exact finding set to repair now
- each finding's `retry_count` is authoritative
- each finding's optional `model_override` is authoritative
- `critical_spec_finding_ids` must be repaired first
- `transitioned_to_repair_planning == true` means ctl already advanced `CHECKPOINT_AUDIT|FINAL_AUDIT -> REPAIR_PLANNING`

Do NOT collect early findings, late findings, queued findings, or increment retry counts in prompt logic.

Append to log:
```
{timestamp} [REPAIR] {phase} cycle={repair_cycle} findings={list of finding IDs}
```

**Q-learning repair plan (deterministic):** Before spawning the repair coordinator, get executor and model assignments from the Q-learning planner:

```bash
echo '{"findings": [{finding objects}]}' | python3 "{{HOOKS_PATH}}/ctl.py" repair-plan --root . --task-type {task_type}
```

If the response has `"source": "q-learning"`, ctl will use those assignments when building `repair-log.json`.

If `"source": "default"` (Q-learning disabled), ctl uses deterministic fallback executor selection.

Log: `{timestamp} [REPAIR-PLAN] source={source} assignments={N}`

Build the repair log through ctl:

```text
python3 "{{HOOKS_PATH}}/ctl.py" run-repair-log-build .dynos/task-{id}
```

This command reads `repair-cycle-plan.json`, calls Q-learning itself, derives `files_to_modify`, writes `.dynos/task-{id}/repair-log.json`, and validates the result. Do NOT ask an LLM to draft `repair-log.json`.

Wait for completion, then finalize repair execution readiness through the control plane:

```text
python3 "{{HOOKS_PATH}}/ctl.py" run-repair-execution-ready .dynos/task-{id}
```

This command validates `repair-log.json` and advances `REPAIR_PLANNING -> REPAIR_EXECUTION`.

Build the repair execution groups through ctl:

```text
python3 "{{HOOKS_PATH}}/ctl.py" run-repair-batch-plan .dynos/task-{id}
```

Use this JSON output as authoritative:
- `execution_groups` is the exact execution order
- groups with `parallel == true` may be spawned concurrently
- groups with `parallel == false` must be serialized
- `model_overrides` inside each batch are authoritative

Do NOT infer parallelism, shared-file conflicts, or execution order from prompt logic.

For each batch, spawn executor agents as assigned in `repair-log.json`:
- `ui-executor`, `backend-executor`, `ml-executor`, `db-executor`, `refactor-executor`, `testing-executor`, `integration-executor`, `docs-executor`, `infra-executor`, `security-executor`, `data-executor`, `observability-executor`, `release-executor`

**Model escalation:** Use the `model_override` values returned by `run-repair-batch-plan` / `repair-log.json`. Do NOT recompute escalation from prose rules.

Each executor receives: the specific finding, the file(s) to fix, and the relevant acceptance criteria text from `spec.md`.

After all batches complete, append to log:
```
{timestamp} [DONE] repair-execution-{phase} — all fixes applied
```

**Phase 1 re-audit (domain-aware, incremental scope):** Build the re-audit plan through ctl:

```bash
python3 "{{HOOKS_PATH}}/ctl.py" run-audit-reaudit-plan .dynos/task-{id}
```

Use its JSON output as authoritative:
- `modified_files` is the scoped re-audit file set
- `auditors_to_spawn` is the exact auditor list
- `full_scope_auditors` always receive the original broad scope
- `scoped_auditors` receive only `modified_files`

Do NOT hand-compute repair-modified files or choose the re-audit auditor set in prompt logic.

After re-audit, run `run-audit-findings-gate` again.

Update Q-learning outcomes through ctl:

```text
python3 "{{HOOKS_PATH}}/ctl.py" run-repair-q-update .dynos/task-{id}
```

This command derives outcomes from `repair-log.json` plus the current blocking audit findings and updates the Q-tables deterministically. Do NOT build `outcomes` JSON by hand.

- If `status == "clear"`: proceed to Step 5.
- If `status == "repair_required"`: let the control plane decide whether another repair cycle is legal:

  ```text
  python3 "{{HOOKS_PATH}}/ctl.py" run-repair-retry .dynos/task-{id}
  ```

  If it returns `repair_retry_ready`, re-run `run-audit-repair-cycle-plan` and continue. If it returns `escalation_required`, write `escalation.md`, transition to `FAILED`, and stop.

### Step 5 — Gate to DONE

Write the deterministic audit summary:

```bash
python3 "{{HOOKS_PATH}}/ctl.py" run-audit-summary .dynos/task-{id}
```

This command writes `audit-summary.json` from the on-disk audit reports. Do not aggregate counts by hand.

**Reflect (deterministic reward computation):**

Generate the retrospective through the control plane:

```text
python3 "{{HOOKS_PATH}}/ctl.py" run-audit-reflect .dynos/task-{id}
```

This command computes `task-retrospective.json` and writes the `retrospective` receipt deterministically.

The written retrospective is complete. Do NOT reopen it to patch `model_used_by_agent`, `agent_source`, `alongside_overlap`, `auditor_zero_finding_streaks`, or `executor_zero_repair_streak` from prompt logic.

Append to log:
```
{timestamp} [DONE] reflect — task-retrospective.json written
```

**Postmortem skip decision (deterministic):** Before the LLM postmortem runs, the deterministic engine decides whether a skip receipt may be written. The decision is:

- ≥2 auditors ran AND every auditor reported `findings: []` → emit `receipt_postmortem_skipped(task_dir, reason="clean-task", retrospective_sha256=..., subsumed_by=[])`.
- Exactly 1 auditor ran AND it reported `findings: []` → emit `receipt_postmortem_skipped(task_dir, reason="no-findings", retrospective_sha256=..., subsumed_by=[])`.
- Any auditor reported ≥1 finding → do NOT skip; run the LLM postmortem and write `receipt_postmortem_generated(...)`.

If ANY auditor reports ≥1 finding (blocking or not), the LLM postmortem runs — no "quality-above-threshold" bypass. The `subsumed_by` argument is required on every skip receipt write; the empty list `[]` is valid only for `reason="clean-task"` or `reason="no-findings"`.

**Post-completion processing:** Learn, trajectory rebuild, evolve, postmortems, and dashboard refresh are handled automatically by the `task-completed` hook via the event bus. Do not run them inline. The hook fires after this skill completes and the task reaches DONE.

Finalize completion through ctl:

```text
python3 "{{HOOKS_PATH}}/ctl.py" run-audit-finish .dynos/task-{id}
```

This command writes `completion.json` and advances `CHECKPOINT_AUDIT|FINAL_AUDIT -> DONE` deterministically. Do NOT call `transition_task(...)` directly from prompt logic.

Print (listing only auditors that were actually spawned, not skipped):
```
Audit complete — ALL PASSED

  {auditor-name}:  PASS
  ... (one line per spawned auditor)

Task complete. Snapshot branch dynos/task-{id}-snapshot can be deleted if desired.
```

---

## Standalone use (no active task)

If no active task is found, run the 4 universal auditors on `git diff --name-only HEAD`. Skip Step 5 (no DONE state to write). Print results and stop.
