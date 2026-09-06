---
name: audit
description: "Run checkpoint audit, repair any findings, then reach DONE — all in one shot. Runs only when the user explicitly types /dynos-work:audit; never auto-triggered from conversation."
---

# dynos-work: Audit

Runs the full audit-to-done pipeline: audit → repair loop → DONE.

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

All repair-log persistence must go through `"$DYNOS" ctl write-repair-log ... --from ...`.
Do not hand-write `.dynos/task-{id}/repair-log.json`.

## Ruthlessness Standard

- Missing evidence is a finding.
- Partial compliance is non-compliance.
- Cosmetic fixes do not count as repairs.
- If a path is unverified, treat it as suspect.
- Prefer surfacing a real risk over preserving a comforting narrative.
- If two interpretations exist, audit the harsher one until disproved.
- Do not let a clean summary hide a dirty edge case.

## What you do

### Step 0 — Contract validation

After finding the active task, validate that all required inputs from the execute skill are present:

```text
"$DYNOS" ctl validate-contract --skill audit --task-dir .dynos/task-{id}
```

If validation fails with missing required inputs (evidence files, snapshot SHA), print the errors and stop.

### Step 1 — Find active task

Find the most recent active task in `.dynos/`. Read `manifest.json`.

Verify stage is `CHECKPOINT_AUDIT`. If not, print the current stage and what command to run instead.

### Step 2 — Audit Setup

Build the deterministic audit setup first:

```text
"$DYNOS" ctl run-audit-setup .dynos/task-{id}
```

This command reads `manifest.json`, derives the audit plan, writes `.dynos/task-{id}/audit-plan.json`, and computes diff scope from `snapshot.head_sha` (or `HEAD` fallback when needed). Use its JSON output directly. Do not hand-derive diff scope, task type, domains, fast-track, skip policy, or model selection in prompt logic.

### Step 3 — Audit (conditional auditor spawning with skip optimization)

When spawning auditors, tell them to attack the implementation, not narrate it. Favor findings with proof over summaries with tone.

## Auditor Prompt Discipline

`router.py audit-inject-prompt` deterministically appends a write-first `## Turn Budget Discipline` block to **every** auditor spawn, sized to the spawn's model/tier (15 / 20 / 25 tool calls). The orchestrator does not need to construct or inject these instructions by hand — they are guaranteed to be in the prompt the router prints. Each spawned auditor is therefore told to:

- Write its audit-report file (skeleton + a `## Progress Ledger` with `### Done`, `### In-Flight`, `### Next`, and `status="partial"`) as its FIRST or SECOND tool call, before reading the diff in depth, and update it incrementally.
- Emit only the canonical audit-report JSON envelope as its final message.
- When within 2 tool calls of its budget, stop investigating and finalize the report with `status="complete"` — a truncated-but-written report beats hitting the `maxTurns` cap with nothing on disk.

All 20 auditor agent files also carry these sections directly, so standalone spawns that bypass the injector get the same discipline. Partial reports (`status="partial"` with ledger content) are excluded from gating decisions but are surfaced to repair planning as advisory context.

> Note: there is no harness-level write-first watchdog enforcing the above. The discipline is prompt-level (injected + per-file). The previously documented `pre_tool_use.py` write-first watchdog was removed in 7.5.8 — it never fired (audit grants never carried `expected_artifact`/`budget`, and grants are not consumed under the shared-session resolution path), so it gave a false sense of enforcement.

For each auditor in the plan:
- If `action: "skip"`: log `{timestamp} [SKIP] {name} — {reason}` and do not spawn
- If `action: "spawn"`: spawn with the model returned by `"$DYNOS" ctl ensemble-next` — the plan `model` is only used for non-ensemble auditors (null = default)
- Log: `{timestamp} [ROUTE] {name} model={model} route={route_mode} source={route_source}`
- For the plan-auditor: For each AC in spec.md naming a function signature, verify plan.md's Component section names the same signature. Mismatch is a partial finding.

**Pre-load diff context (run once, write to sidecar, reference by path):** Using `diff_base` from the audit plan output, write the context ONCE to a sidecar file and reference its path in each auditor's base prompt. Do NOT capture the full content into a shell variable and append it to every prompt — that pattern multiplies (auditors × cascade-models × context-size) input tokens, which is exactly the regression the 2026-04-30 latency investigation flagged in CG-013 (the mislabeled "perf:" commit).

```bash
AUDIT_CONTEXT_PATH=".dynos/task-{id}/audit-context.md"
"$DYNOS" hook build_prompt_context --diff {diff_base} --root . --sidecar "$AUDIT_CONTEXT_PATH"
```

<!-- pre-resolved-context-start -->

<!-- pre-resolved-context-end -->

In each auditor's base prompt, include a single line referencing the sidecar:

```
A pre-computed audit context (unified diff + current contents of changed files) is at `<AUDIT_CONTEXT_PATH>`. Read it ONCE at the start of your work via the Read tool. Do NOT re-read it for each finding, and do NOT call Read/Grep/Glob for files already covered there.
```

Read calls are cheap and parallel (per auditor, per session); prompt input tokens are billed per LLM call and multiply across the haiku→sonnet→opus cascade. The sidecar pattern keeps the per-prompt overhead at ~80 bytes regardless of diff size. If the sidecar file is empty (no changed files found, e.g. on first task or after a clean commit), the auditors proceed without it — the line in their prompt is harmless when the file is absent.

**Learned Auditor Injection (MANDATORY — deterministic via router):** Build each auditor's spawn prompt with `router.py audit-inject-prompt`. Pipe the base prompt over stdin and capture stdout as the prompt you pass to the Agent tool — do NOT read the learned agent file yourself or build the prompt by hand. The router does the frontmatter stripping, applies the learned-auditor block under the literal heading `## Learned Auditor Instructions`, computes the SHA-256 of the exact bytes it prints, and atomically writes the per-model sidecar at `.dynos/task-{id}/receipts/_injected-auditor-prompts/{auditor_name}-{model_used}.sha256` (with a companion `.txt` of the same bytes; when no model is specified the literal `default` is substituted in the filename).

**Parallelism note (task-20260430-011):** This sidecar pre-computation is per-auditor work, NOT a serial constraint between auditors. Issue all `router.py audit-inject-prompt` Bash commands in **a single message with multiple Bash tool calls** — they have no inter-dependencies and complete independently. Then issue the Agent-tool spawns themselves the same way: a single message with multiple Agent tool calls, one per spawned auditor. Sequential pre-step bash calls followed by sequential Agent calls is the slow path the latency investigation flagged; the harness already supports parallel tool calls in a single message and the pre-step has no ordering constraints. Wall-clock cost drops from `sum(per-auditor sidecar + spawn)` to `max(sidecar) + max(spawn)`.

```bash
echo "{base prompt for this auditor}" | "$DYNOS" hook router audit-inject-prompt \
  --root . \
  --task-type {task_type} \
  --audit-plan .dynos/task-{id}/audit-plan.json \
  --auditor-name {name} \
  --model {model}
```

The command logs `learned_auditor_applied`, `learned_auditor_missing`, or `learned_auditor_error` to `.dynos/events.jsonl` depending on whether the named auditor's `agent_path` exists and could be read. On any IO failure the command exits 1 and prints a JSON error to stderr — fix the cause (audit-plan path, auditor name, or `--task-type`) before retrying.

After each auditor spawn returns, you MUST write the audit receipt via the deterministic ctl wrapper below. Do NOT hand-write a Python `receipt_audit_done(...)` call. The wrapper derives `finding_count` and `blocking_count` from the on-disk report file so the model cannot mix one auditor/model's counts with another auditor/model's report. The captured digest must be the contents of the sidecar file the router just wrote — read it back rather than re-hashing:

The auditor's final text message IS the envelope JSON (a single bare JSON line). Capture the result content from the Agent tool spawn return and pass it verbatim as `--final-envelope`.

```bash
INJECTED_AGENT_SHA256=$(cat .dynos/task-{id}/receipts/_injected-auditor-prompts/{auditor_name}-{model_used}.sha256)
FINAL_ENVELOPE=$(... <extract last line of Agent tool return>)
"$DYNOS" ctl audit-receipt .dynos/task-{id} {auditor_name} \
  --model {model_used} \
  --report-path .dynos/task-{id}/audit-reports/{report_filename}.json \
  --tokens-used {tokens_used} \
  --route-mode {route_mode} \
  --agent-path {agent_path} \
  --injected-agent-sha256 "${INJECTED_AGENT_SHA256}" \
  --final-envelope "${FINAL_ENVELOPE}"
```

For `route_mode == "generic"` the `--final-envelope` argument may be omitted.

`"$DYNOS" ctl audit-receipt ...` calls `receipt_audit_done(...)`, which re-asserts the same sidecar exists at that exact path and that its contents match `injected_agent_sha256`. A mismatch raises `ValueError`. For `route_mode == "generic"` (no learned agent) the sidecar assertion is skipped and `injected_agent_sha256` may be `None`; `route_mode` and `agent_path` are still required keyword arguments. The wrapper derives counts from `--report-path`; when no report exists it writes literal zero findings only. `run-audit-setup` writes the `audit-routing` receipt from the deterministic audit plan before prompt injection; the per-auditor `audit-receipt` is the sidecar proof for spawned learned auditors.

For ensemble auditors, write one receipt per model run. Add `--ensemble-context --shard-step-name "{auditor_name}-{model_used}"` to each `audit-receipt` call so the file is named `receipts/audit-{auditor_name}-{model_used}.json`. Do not include the `audit-` prefix in `--shard-step-name`; `audit-{auditor_name}` collapsed receipts do not satisfy the DONE gate for ensemble accounting. `audit-receipt` also refuses a shard receipt that is out of cascade order — any tier other than the fast tier must be exactly the model `ensemble-next` reported — so a deep-tier receipt alone is rejected until the voting tiers have receipts.

The router handles fast-track reduction, skip policy, model policy, security floor enforcement, ensemble voting triggers, and learned agent routing in deterministic code. No prompt interpretation needed for these decisions. Do not re-derive skip thresholds, model assignments, or routing modes from markdown tables or retrospective files.

**Ensemble Voting (deterministic — computed by ctl, never by prompt logic):** The router marks auditors `"ensemble": true`. For those auditors the plan's `model` field is NOT the spawn model. Before every audit batch, run:

```bash
"$DYNOS" ctl ensemble-next .dynos/task-{id}
```

It evaluates every spawn-action auditor against the shard receipts on disk and returns, per auditor, one of:

- `"status": "spawn"` with the exact `model` and `shard_step_name` to use now (`reason` is `cascade_step` for the next voting tier or `escalate_on_findings` for the deep tier);
- `"status": "complete"` with `verdict` `pass` (every voting tier clean) or `escalated` (deep tier ran; its verdict is final and binding);
- `"status": "single"` for non-ensemble auditors, carrying the plan `model`.

The audit batch is a loop: spawn every auditor whose status is `spawn` or `single` (in parallel, at the returned `model`), write each receipt, then run `ensemble-next` again. Stop only when the output reports `"complete": true`. Do not decide by hand whether a tier is needed — the cascade (fast tier → balanced tier on zero findings → deep tier on any finding) is computed by ctl. Log each ensemble spawn as `{timestamp} [VOTE] {name} — {reason} → {model}`.

**Enforcement (there is no shortcut):** `router.py audit-inject-prompt` refuses to build a prompt for an ensemble auditor at any model other than the one `ensemble-next` returns; `audit-receipt` refuses a shard receipt written out of cascade order; `run-audit-summary` and the DONE gate refuse while any cascade is incomplete. A single deep-tier spawn does not satisfy an ensemble auditor. On null-model hosts the router sets `"ensemble": false` and the auditor spawns once at the plan model.

**Visual Audit Pass:** For tasks where `domains` includes `"ui"`, run a visual audit: start the dev server, use a browser subagent to screenshot modified screens, then evaluate with Claude 3.5 Sonnet against the planning-phase Design Decisions. Report visual findings as category `vision-finding`. Log: `{timestamp} [VISION] UI audit complete -- {N} visual bugs found`.

**Alongside mode deduplication:** When generic and learned auditors both produce findings for the same role, deduplicate by key `{file}:{line}:{category}`. Count duplicates once, preferring the learned version. The deduplicated set feeds into repair and retrospective counts. Track whether learned findings are a superset of generic findings (for promotion decisions by the learn step).

Append to log (transition_task already auto-logged the `[STAGE] → CHECKPOINT_AUDIT` line; only emit the `[SPAWN]` line):
```
{timestamp} [SPAWN] {N} auditors in parallel ({list of names})
```

**Role stamping (MANDATORY — BEFORE each Agent spawn):** Before each auditor's Agent tool invocation, stamp the role file via the deterministic ctl wrapper. The auditor subagent's `pre_tool_use.py` reads `active-segment-role` to resolve role; without this stamp the subagent runs as `execute-inline` and its `audit-reports/` writes are denied by `write_policy`. Issue all stamp-role Bash calls in a single message with multiple Bash tool uses (no inter-dependencies):

```bash
"$DYNOS" ctl stamp-role .dynos/task-{id} --role "audit-{name-without-suffix}"
```

The role string strips the `-auditor` suffix from the agent file name: `spec-completion-auditor` → `audit-spec-completion`, `security-auditor` → `audit-security`, `claude-md-auditor` → `audit-claude-md`. The wrapper validates against an allowlist (`hooks/ctl.py::_STAMP_ROLE_ALLOWLIST`) and fails with exit 1 if the resolved string is unknown — a fail-fast guard that catches typos before the spawn.

When auditors run in parallel, the role file is overwritten on each stamp; this is fine because each subagent reads the file at its own first tool call within its own session. The post-spawn cleanup (delete `active-segment-role`) happens after the entire audit batch, not per auditor.

**Spawn budget check (MANDATORY — BEFORE every batch):**

Before issuing each parallel auditor batch (this includes the first batch AND every subsequent re-audit batch — the check runs once per spawn cycle, before EACH batch, not only the first), run:

```bash
"$DYNOS" ctl check-spawn-budget .dynos/task-{id}
```

Parse the JSON output. The command emits a single-line JSON object with keys `status`, `count`, `threshold`, `exempt_count`, and `task_class`. If `status` is `'paused'` or `'already_paused'`:

1. Write `escalation.md` in the task directory containing the full JSON output and a human-readable explanation that the spawn budget was exceeded — name the non-converging-repair `count`, the threshold, the `task_class`, and instruct the operator to run `"$DYNOS" ctl spawn-resume .dynos/task-{id} --reason "<≥20-char rationale>"` after diagnosing why repairs are not converging.
2. Append a line beginning with `[BUDGET-PAUSE]` to `execution-log.md` that records the timestamp, count, and threshold.
3. Exit non-zero.

Do not proceed with spawning auditors. The pause is intentional — the policy is calibrated from this project's retrospective history, and the wasted-spawn count has crossed the learned threshold.

Spawn the determined auditors simultaneously, passing for each auditor the model returned by `ensemble-next` (never the plan `model` for an ensemble auditor) in the subagent spawn configuration. For alongside-mode auditors, this means two spawns for that role (generic + learned), both counted in {N}.

Each auditor writes its own report to `.dynos/task-{id}/audit-reports/{auditor}-checkpoint-{timestamp}.json`. Every auditor agent has a write capability sufficient for this: auditors with `Bash` use a heredoc; `spec-completion-auditor` has the `Write` tool scoped via `write_policy.py` to its own report path. The role file stamped above is what unlocks the `audit-reports/` write rule for these subagents.

**The orchestrator MUST NOT materialize audit-report files.** Direct `Write`/`Edit`/Bash redirection from the orchestrator (role=`execute-inline`) to `audit-reports/` is denied by `write_policy.decide_write` because the role is not `audit-*`. The orchestrator also cannot self-elevate by stamping the role file directly — `active-segment-role` is wrapper-required (`ctl stamp-role`) and only this skill's prose tells it what to stamp. The only legitimate path for an audit-report file to land on disk is the auditor subagent's own write call. If the auditor returns text instead of writing the file, treat the audit as **failed for this auditor** and re-spawn — do NOT backfill from the auditor's text response. Backfilling is the exact mechanism behind the 2026-04-30 audit-chain forgery incident: 7 of 8 ensemble auditors were synthesized by the orchestrator after one truncation, and the receipt chain validated as clean because there was no harness-level cross-check. The Agent-tool spawn-log hook (`hooks/agent-spawn-log`) now provides that cross-check; the absence of a matching `agent_spawn_post` entry for an auditor whose report is on disk is a hard fail at receipt time (`receipt_audit_done`).

**Token & event capture (applies to all events in Steps 3-5):** After each subagent spawn AND each deterministic check, record the event:

**For LLM subagent spawns** (auditors, repair executors):
```bash
"$DYNOS" hook lib_tokens record \
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
"$DYNOS" hook lib_tokens record \
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
- Step 3: router audit-plan (type=deterministic, detail="Router decided: spawn X, skip Y")
- Step 3: Each auditor spawn (type=spawn, phase=audit)
- Step 4: repair-log build (type=deterministic, phase=repair)
- Step 4: Each repair executor spawn (type=spawn, phase=repair, include --segment)
- Step 4: ctl check-ownership after repair (type=deterministic, phase=repair)
- Step 4: Re-audit spawns (type=spawn, phase=audit, detail="Re-audit after repair cycle N")
- Step 5: compute_reward / validate_retrospective_scores (type=deterministic, phase=audit, detail="Computed retrospective scores")

**Audit repair gate (deterministic):**

Decide whether audit proceeds to repair or reflect through ctl:

```bash
"$DYNOS" ctl run-audit-findings-gate .dynos/task-{id}
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
"$DYNOS" ctl run-audit-repair-cycle-plan .dynos/task-{id}
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

Build the repair log through ctl — Q-learning assignments are computed inside this command from `repair-cycle-plan.json`; do NOT construct or pipe a separate findings payload:

```text
"$DYNOS" ctl run-repair-log-build .dynos/task-{id}
```

This command reads `repair-cycle-plan.json`, calls Q-learning itself, derives `files_to_modify`, writes `.dynos/task-{id}/repair-log.json`, and validates the result. Do NOT ask an LLM to draft `repair-log.json`.

Wait for completion, then finalize repair execution readiness through the control plane:

```text
"$DYNOS" ctl run-repair-execution-ready .dynos/task-{id}
```

This command validates `repair-log.json` and advances `REPAIR_PLANNING -> REPAIR_EXECUTION`. Use its JSON output directly.

Build the repair execution groups through ctl:

```text
"$DYNOS" ctl run-repair-batch-plan .dynos/task-{id}
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
"$DYNOS" ctl run-audit-reaudit-plan .dynos/task-{id}
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
"$DYNOS" ctl run-repair-q-update .dynos/task-{id}
```

This command derives outcomes from `repair-log.json` plus the current blocking audit findings and updates the Q-tables deterministically. Do NOT build `outcomes` JSON by hand.

- If `status == "clear"`: proceed to Step 5.
- If `status == "repair_required"`: let the control plane decide whether another repair cycle is legal:

  ```text
  "$DYNOS" ctl run-repair-retry .dynos/task-{id}
  ```

  Interpret the JSON result:
  - `status == "repair_retry_ready"`: another repair cycle is allowed. Re-run `run-audit-repair-cycle-plan` and continue with the returned queue.
  - `status == "escalation_required"`: the retry cap blocked another loop. Write `.dynos/task-{id}/escalation.md`, transition to `FAILED`, and stop.

### Step 5 — Gate to DONE

Write the deterministic audit summary:

```bash
"$DYNOS" ctl run-audit-summary .dynos/task-{id}
```

This command writes `audit-summary.json` from the on-disk audit reports. Do not aggregate counts by hand.

**Reflect (deterministic reward computation):**

Generate the retrospective through the control plane:

```text
"$DYNOS" ctl run-audit-reflect .dynos/task-{id}
```

This command computes `task-retrospective.json` and writes the `retrospective` receipt deterministically. Use its JSON output directly.

The written retrospective is complete. Do NOT reopen it to patch `model_used_by_agent`, `agent_source`, `alongside_overlap`, `auditor_zero_finding_streaks`, or `executor_zero_repair_streak` from prompt logic.

**agent_source cross-check:** Retrospective claims of `agent_source[role] = "learned:X"` are cross-checked by `memory/policy_engine.py::_extract_quads` against `.dynos/events.jsonl`. Claims without a matching `learned_agent_applied` event (same `task_id`, same `agent_name`, same `segment_id` — or `segment_id` matching `role.removeprefix("audit-")` for auditor roles) are reclassified to `"generic"` and an `agent_source_reclassified` event is emitted. Auditors must continue to populate `agent_source` honestly — the cross-check is verification, not a substitute for honest reporting. Retrospectives are still accepted when unmatched; only the EMA attribution is downgraded, and the `agent_source_reclassified` events are the audit trail a reviewer can use to spot systemic drift.

Append to log:
```
{timestamp} [DONE] reflect — task-retrospective.json written
```

**Deterministic Postmortem (Step 5a):** Generate the deterministic postmortem report before the LLM analysis so the LLM has access to anomaly detection, recurring patterns, and similar task comparisons.

```bash
"$DYNOS" hook postmortem generate --root . --task-id {task-id}
```

`memory/postmortem.py:generate_postmortem` (called by `postmortem.py generate`) now writes its own receipt internally as part of the same call. You do NOT need to write a receipt from this skill:

- On successful generation it calls `receipt_postmortem_generated(task_dir, postmortem_json_path)`; the writer itself opens the JSON, counts anomalies / recurring_patterns, and hashes both the JSON and its sibling markdown (task-007 B-001 self-compute contract). Writes `.dynos/task-{id}/receipts/postmortem-generated.json`.
- When the deterministic engine determines there is literally nothing to learn, it short-circuits the postmortem write and instead emits `receipt_postmortem_skipped(task_dir, reason, retrospective_sha256, subsumed_by)` with `reason` from the enum `{"clean-task", "no-findings"}` and `subsumed_by` as a required list argument. The decision is deterministic: (a) `reason="clean-task"`, `subsumed_by=[]` when ≥2 auditors ran AND every auditor reported `findings: []`; (b) `reason="no-findings"`, `subsumed_by=[]` when exactly 1 auditor ran AND it reported `findings: []`; (c) in every other case — any auditor reporting ≥1 finding — the engine does NOT skip; it runs the LLM postmortem and writes `receipt_postmortem_generated` instead. There is no skip path for high-quality repairs: any task with findings must run the full LLM postmortem. The `task-retrospective.json` is hashed at receipt-emission time.
- A receipt-write failure is logged as `postmortem_receipt_failed` / `postmortem_skip_receipt_failed` and does NOT corrupt the postmortem files themselves; the script returns its normal result so the audit pipeline keeps moving.

This writes `postmortems/{task-id}.json` and `postmortems/{task-id}.md` to the persistent project directory. Append to log:
```
{timestamp} [DONE] postmortem — anomalies={N}, recurring_patterns={N}
```

**LLM Postmortem Analysis (Step 5b):** After the deterministic postmortem, run LLM-powered failure analysis. This step is skipped for clean tasks (no findings, no repairs, quality >= 0.8).

1. Build the analysis prompt:
```bash
"$DYNOS" hook postmortem_analysis build-prompt .dynos/task-{id}
```

2. If the result has `"has_findings": true`, spawn an **opus** agent with the prompt from the `"prompt"` field. Instruct the agent to respond with ONLY the JSON object described in the prompt — no markdown, no explanation.

3. Parse the agent's JSON response and apply it:
```bash
echo '${AGENT_JSON_OUTPUT}' | "$DYNOS" hook postmortem_analysis apply .dynos/task-{id}
```

This writes `postmortem-analysis.json` to the task dir and merges new prevention rules into `prevention-rules.json`. These rules are automatically included in `project_rules.md` by the policy engine on the next task.

`memory/postmortem_analysis.py:apply_analysis` (called by `postmortem_analysis.py apply`) emits its own receipt internally — do NOT write one from this skill:

- When the agent JSON is empty or non-dict (the orchestrator passed nothing to apply), it emits `receipt_postmortem_skipped(task_dir, "no-findings", retrospective_sha256, subsumed_by=[])`.
- When the sanitized analysis yields zero usable rules, it emits `receipt_postmortem_analysis` internally, passing `analysis_path` and `rules_path` as keyword file-path arguments; hashes are derived from the on-disk files — no caller-supplied hashes are accepted. The receipt records `rules_added=0` and the current on-disk hash of `prevention-rules.json` (or 64 zeros when the file does not exist).
- When the merge appends rules, the same receipt is emitted AFTER the fcntl lock is released so the recorded hash captures the on-disk state visible to other readers. Do not call `receipt_postmortem_analysis` directly from this skill — it is emitted internally by `postmortem_analysis.py apply`.

Append to log:
```
{timestamp} [DONE] postmortem-analysis — {N} prevention rules added
```

If `has_findings` is false, skip this step and append:
```
{timestamp} [SKIP] postmortem-analysis — clean task, nothing to analyze
```

**Post-completion processing:** Improve, policy engine, dashboard, and registry refresh are handled automatically by the `task-completed` hook via the event bus. Do not run them inline. The hook fires after this skill completes and the task reaches DONE.

Finalize completion through ctl:

```text
"$DYNOS" ctl run-audit-finish .dynos/task-{id}
```

This command writes `completion.json` and advances `CHECKPOINT_AUDIT|FINAL_AUDIT -> DONE` deterministically. Do NOT call `transition_task(...)` directly from prompt logic.

Print verbatim the contents of `.dynos/task-{id}/completion.json::user_summary`.
Do NOT paraphrase, reorder, or add commentary. The orchestrator's user-facing
final output for this skill is constrained to a literal echo of that text.

---

## Standalone use (no active task)

If no active task is found, run the 5 universal auditors on `git diff --name-only HEAD`. Skip Step 5 (no DONE state to write). Print results and stop.
