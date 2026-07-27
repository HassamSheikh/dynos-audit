---
name: start
description: "Start a new dynos-work task: handles discovery, spec, plan, execution, and audit. Runs only when the user explicitly types /dynos-work:start; never auto-triggered from conversation or by task-shaped requests."
---

# dynos-work: Unified Foundry Start

You are the entry point for dynos-work. You own all human-in-the-loop gates before execution. When done, the task is ready for `/dynos-work:execute`.

There is one pipeline for all tasks. There are no shortcuts. Historical memory may inform discovery and design review, but it is advisory only. Human approval and deterministic artifact checks decide readiness.

---

## MANDATORY: Token Recording After Every Agent Spawn

After EVERY Agent tool call in this skill (planner, spec-completion auditor, testing-executor), you MUST write a receipt that records token usage. Read `total_tokens` from the Agent tool result's usage summary and run:

Before EVERY planner receipt write, you MUST first write a per-phase injected-prompt sidecar by piping the planner prompt body into `hooks/router.py planner-inject-prompt`. Capture the printed sha256 digest and pass it back through as the `injected_prompt_sha256=<digest>` kwarg on the receipt call. The receipt will raise `ValueError` if the sidecar is missing or its contents do not match — that is the proof-of-injection gate.

```bash
# Discovery planner — write sidecar, capture digest:
DISCOVERY_DIGEST=$(printf '%s' "$DISCOVERY_PROMPT" | python3 "{{HOOKS_PATH}}/router.py" planner-inject-prompt --task-id {id} --phase discovery)

# Spec planner — write sidecar, capture digest:
SPEC_DIGEST=$(printf '%s' "$SPEC_PROMPT" | python3 "{{HOOKS_PATH}}/router.py" planner-inject-prompt --task-id {id} --phase spec)

# Plan planner — write sidecar, capture digest:
PLAN_DIGEST=$(printf '%s' "$PLAN_PROMPT" | python3 "{{HOOKS_PATH}}/router.py" planner-inject-prompt --task-id {id} --phase plan)
```

Then, after each planner subagent returns, write the matching receipt with the captured digest:

```python
from dynoslib_receipts import receipt_planner_spawn, receipt_plan_audit

# After planner spawn (discovery/design/classification):
receipt_planner_spawn(
    task_dir, "discovery",
    tokens_used=TOTAL_TOKENS,
    injected_prompt_sha256=DISCOVERY_DIGEST,
)

# After planner spawn (spec normalization):
receipt_planner_spawn(
    task_dir, "spec",
    tokens_used=TOTAL_TOKENS,
    injected_prompt_sha256=SPEC_DIGEST,
)

# After planner spawn (plan generation):
receipt_planner_spawn(
    task_dir, "plan",
    tokens_used=TOTAL_TOKENS,
    injected_prompt_sha256=PLAN_DIGEST,
)

# After spec-completion auditor (plan audit; task-007 A-006 restricts the
# writer to high/critical risk only — for low/medium risk, skip and log
# plan_audit_skipped_by_risk instead):
# The writer re-hashes spec.md / plan.md / execution-graph.json from disk
# at write time (SEC-004: no caller-supplied hashes, no TOCTOU window).
# The PLAN_AUDIT exit gate re-hashes these artifacts at transition time and
# refuses to advance when any has drifted since the audit was recorded.
# finding_count was removed from the v4 signature (task-007 B-006 / AC 14).
receipt_plan_audit(task_dir, tokens_used=TOTAL_TOKENS)

```

Each receipt auto-records tokens to `token-usage.json`. If you skip this, the retrospective will show 0 tokens and the effectiveness scores will be wrong. This is the same enforcement pattern as the execute skill's receipts.

---

## Step 0 — Metadata & Initialization

1. Ensure `.dynos/` exists: `mkdir -p .dynos`. Then auto-register this project with the global registry (silent, idempotent): run `python3 "{{HOOKS_PATH}}/registry.py" register "$(pwd)" 2>/dev/null || true`. This creates `~/.dynos/projects/{slug}/` and adds the project to `~/.dynos/registry.json` if not already registered. No user action needed. Then ensure the local maintenance daemon is running (silent, idempotent): run `PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/daemon.py" start --root "$(pwd)" 2>/dev/null || true`. This starts the daemon without autofix. Autofix must be explicitly enabled by the user via `dynos init --autofix` or `dynos local start --autofix`. If already running, it is a no-op.
2. Generate a task ID in the format `task-YYYYMMDD-NNN`.
3. Create the task directory: `.dynos/task-{id}/`.
3. Write `raw-input.md` with the full task description exactly as given.
4. Initialize `manifest.json` with at least:

```json
{
  "task_id": "task-20260403-001",
  "created_at": "ISO timestamp",
  "title": "First 80 characters of task description",
  "raw_input": "Full task description as provided by user",
  "input_type": "text | prd | wireframe | mixed",
  "stage": "FOUNDRY_INITIALIZED",
  "classification": null,
  "retry_counts": {},
  "blocked_reason": null,
  "completed_at": null
}
```

5. Initialize `.dynos/task-{id}/execution-log.md`.
6. Before writing `raw-input.md`, inspect the user input for:
   - A file path ending in `.prd.md`, `.pdf`, or `.txt` and treat it as a primary spec source.
   - A URL to a Figma link, screenshot, or wireframe and note it as a design artifact.
   - An attached screenshot or image and note it as a visual spec artifact.
7. Deterministically verify that `manifest.json` parses as valid JSON and that `task_id`, `created_at`, `raw_input`, and `stage` are present before continuing. If available in this repo, use:

```text
python3 "{{HOOKS_PATH}}/ctl.py" validate-task .dynos/task-{id}
```
8. Print: `dynos-work: Foundry Task Initialized: task-YYYYMMDD-NNN`

---

## Token & Event Capture (applies to ALL events in this skill)

After every subagent spawn AND every deterministic validation, record the event:

**For LLM subagent spawns** (planner, founder, testing-executor, spec-completion-auditor):
```bash
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/lib_tokens.py" record \
  --task-dir .dynos/task-{id} \
  --agent "{agent_name}" \
  --model "{model_name}" \
  --input-tokens {input_tokens} \
  --output-tokens {output_tokens} \
  --phase planning \
  --stage "{current_manifest_stage}" \
  --type spawn \
  --detail "{what the agent did}"
```

**For deterministic Python validations** (validate_task_artifacts, ctl validate-task, spec heading check, etc.):
```bash
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/lib_tokens.py" record \
  --task-dir .dynos/task-{id} \
  --agent "{validation_tool_name}" \
  --model "none" \
  --input-tokens 0 \
  --output-tokens 0 \
  --phase planning \
  --stage "{current_manifest_stage}" \
  --type deterministic \
  --detail "{validation result summary}"
```

Where:
- `input_tokens`/`output_tokens` come from the Agent tool result's usage summary (pass `0` if unavailable)
- `model_name` is the model used (e.g. "opus", "sonnet", "haiku")
- `current_manifest_stage` is the current stage from manifest.json (e.g. DISCOVERY, SPEC_NORMALIZATION, PLANNING)
- `detail` is a short description of what happened (e.g. "Discovery + Design + Classification", "Validated spec.md headings — 0 errors")

This writes to `.dynos/task-{id}/token-usage.json` with a chronological event log. The same hook is used by execute and audit skills — events accumulate across all phases.

**Specific events to record in this skill:**
- Step 2: Planner spawn (phase=planning, stage=DISCOVERY)
- Step 2: Founder simulation spawns (phase=planning, stage=DISCOVERY)
- Step 2: Classification validation (type=deterministic, stage=DISCOVERY)
- Step 2b: Fast-track gate check (type=deterministic, stage=DISCOVERY)
- Step 3: Planner spec normalization spawn (phase=planning, stage=SPEC_NORMALIZATION)
- Step 3: Spec heading/criteria validation (type=deterministic, stage=SPEC_NORMALIZATION)
- Step 5: Planner plan generation spawn (phase=planning, stage=PLANNING)
- Step 5: validate_task_artifacts run (type=deterministic, stage=PLANNING)
- Step 7: spec-completion-auditor plan audit spawn (phase=audit, stage=PLAN_AUDIT)
- Step 8: testing-executor TDD spawn (phase=tdd, stage=PLAN_AUDIT)
- Step 8: TDD criteria coverage validation (type=deterministic, stage=PLAN_AUDIT)

---

## Step 1 — Discovery Intake

1. Build discovery context from:
   - `raw-input.md`
   - relevant existing code in the repo
   - optional trajectory memory from `.dynos/trajectories.json` if available
2. If trajectory memory exists, use it only to surface likely ambiguity or failure patterns. Do not treat it as ground truth and do not copy prior solutions blindly.
3. Generate up to 5 targeted questions that materially reduce implementation risk.
4. Present the questions to the user using `AskUserQuestion`.
5. Write `discovery-notes.md` with the Q&A.

---

## Step 2 — Discovery + Design + Classification

There is no direct-classify shortcut here. Always use the planner for Discovery + Design + Classification. Do NOT skip the planner spawn and do NOT infer classification directly from the task input in prompt logic.

**Learned Planning Skill Injection (MANDATORY):** Before spawning the planner, check if a learned planning skill exists for this task type:

```bash
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 -c "from pathlib import Path; from router import resolve_route; r = resolve_route(Path('.'), 'plan-skill', '{task_type}'); print(r['agent_path'] or '')" 
```

If a non-empty path is returned AND the file exists, read it, strip frontmatter, and append its contents to the planner's instruction below under a `## Learned Planning Rules` heading. This injects project-specific planning patterns (e.g., tighter acceptance criteria, better segment sizing) derived from past task retrospectives. Log: `{timestamp} [ROUTE] plan-skill route={mode} agent={agent_name}`.

Spawn the Planner subagent (`dynos-work:planning`) with instruction:

```text
Phase: Discovery + Design + Classification (combined).
Read raw-input.md and discovery-notes.md if present. Also read any attached trajectory context as advisory prior history only.

Perform three things in one pass:
1. Discovery: generate only the highest-value unresolved questions.
2. Design Options: break the task into subtasks. For any subtask rated hard complexity or critical value, generate 2-3 design options with pros and cons. For easy or medium subtasks, decide directly.
3. Classification: produce type, domains, risk_level, and notes.

Return three sections:
- Questions
- Design Options
- Classification (JSON)
```

Present any remaining discovery questions to the user and append answers to `discovery-notes.md`.

If hard or critical design options were returned:
1. Run the Founder skill in simulation mode for each high-risk design option.
2. Treat simulation output as advisory evidence, not automatic approval.
3. Present each option to the user with the simulation evidence and record the chosen design in `design-decisions.md`.

If no high-risk design options were returned, write `design-decisions.md` with the autonomous design choices and rationale.

Write the returned classification object to `/tmp/classification-{id}.json`, then run `python3 hooks/ctl.py write-classification .dynos/task-{id} --from /tmp/classification-{id}.json`.

Deterministic validation before proceeding:
1. The wrapper enforces `classification.type`.
2. The wrapper enforces `classification.risk_level`.
3. The wrapper enforces `classification.domains`.
4. If `write-classification` fails, stop and correct the payload before moving on.

Transition the stage by running:
Finalize classification through the deterministic control-plane entrypoint:

```text
python3 "{{HOOKS_PATH}}/ctl.py" run-start-classification .dynos/task-{id}
```

`run-start-classification` validates the classification payload, applies fast-track + `tdd_required`, and advances the manifest to `SPEC_NORMALIZATION` when the task is ready.

---

## Step 2b — Fast-Track Gate (conditional)

After classification, determine fast-track eligibility **deterministically** by running:

```text
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 -c "from lib import apply_fast_track; from pathlib import Path; print(apply_fast_track(Path('.dynos/task-{id}')))"
```

This checks: `risk_level == "low"` AND exactly 1 domain. It writes `"fast_track": true` or `"fast_track": false` to `manifest.json`. If the command is not available, abort. Fast-track eligibility is a deterministic gate; manual fallback is forbidden.

When fast-tracked (`fast_track: true`), apply these simplifications throughout the remaining steps:
- **Spec (Step 3):** The planner should produce a concise spec. The `Implicit Requirements Surfaced` and `Risk Notes` sections can contain a single line each if no significant risks exist.
- **Planning (Step 5):** The execution graph should contain a **single segment** (no multi-segment decomposition). A single executor handles the entire change.
- **Audit (handled by audit skill):** When `fast_track: true` in the manifest, spawn only `spec-completion-auditor` and `security-auditor`. Skip all other auditors regardless of streak or domain.

If any condition is not met, proceed normally (no fast-track). Do not ask the user — this is a deterministic gate.

---

## Step 2c — External Solution Gate (always runs)

Before inventing a solution from scratch, run the deterministic external-solution gate. The control plane writes `.dynos/task-{id}/external-solution-gate.json`; do NOT hand-write this artifact in prompt logic.

```bash
python3 "{{HOOKS_PATH}}/ctl.py" run-external-solution-gate .dynos/task-{id}
```

This command deterministically inspects the task input and classification, then writes the gate artifact with:
- `search_recommended`: whether external research is likely worth the time
- `search_used`: always `false` at gate time
- `query_reason`: short deterministic explanation
- `candidates`: `[]`
- `recommended_choice`: `null`
- `decision_basis`: matched trigger signals for auditability

If `search_recommended` is `true`, you may perform bounded external research to inform planning, but treat that research as planner input only. Do NOT rewrite `external-solution-gate.json` by hand after searching.

If `search_recommended` is `false`, proceed directly to Step 3.

Append exactly one line to the execution log:

`{timestamp} [GATE] external-solution — recommended: {true|false}`

---

## Step 3 — Spec Normalization

Spawn the Planner subagent with instruction:

```text
Phase: Spec Normalization.
Read raw-input.md, discovery-notes.md, and design-decisions.md.
Also read the actual implementation files referenced in the task (e.g., the files that will be modified). Verify runtime semantics directly from the code — do not assume template engines, escaping conventions, or generation mechanisms without reading the relevant functions. Include specific function signatures, data flow paths, and module boundaries in the spec.
Write spec.md.
```

After `spec.md` is written, run deterministic spec validation:
1. The file must contain the headings `Task Summary`, `User Context`, `Acceptance Criteria`, `Implicit Requirements Surfaced`, `Out of Scope`, `Assumptions`, and `Risk Notes`.
2. Acceptance criteria must be a numbered list starting at `1` and incrementing by `1` with no gaps.
3. Every acceptance criterion must be concrete and independently testable.
4. Any assumption that affects behavior must be labeled `needs confirmation` or `safe assumption`.

If any rule fails, send the Planner back to fix `spec.md` before presenting it.

Finalize spec readiness through the deterministic control-plane entrypoint:

```text
python3 "{{HOOKS_PATH}}/ctl.py" run-spec-ready .dynos/task-{id}
```

`run-spec-ready` validates `spec.md`, writes the `spec-validated` receipt, and advances `SPEC_NORMALIZATION -> SPEC_REVIEW` when the artifact is sound.

---

## Step 4 — Spec Review

This gate always runs. There is no skip path.

Present `spec.md` to the user and ask for approval.

- If approved: run the `approve-stage` ctl command below. It hashes the current `spec.md`, writes the `human-approval-SPEC_REVIEW` receipt with that hash, then transitions SPEC_REVIEW → PLANNING in one atomic step. Do NOT add a manual `[HUMAN]` log line — `approve-stage` is the only path that satisfies the receipt-gate in `transition_task` (which compares the receipt's `artifact_sha256` against the live `spec.md` at transition time and refuses with `human-approval-SPEC_REVIEW` / `hash mismatch` substrings on drift).
- If changes are requested: append the feedback, respawn the Planner in Spec Normalization mode, re-run deterministic spec validation, write a new `receipt_spec_validated`, and present the updated spec again. Do NOT call `approve-stage` until the user re-approves the regenerated spec.
- If rejected outright: run `python3 hooks/ctl.py transition .dynos/task-{id} FAILED`, append `[FAILED] Spec rejected by user`, and stop. Do not edit `manifest.json` directly.

When approved:

```text
python3 "{{HOOKS_PATH}}/ctl.py" approve-stage .dynos/task-{id} SPEC_REVIEW
```

Exit code 0 means the receipt was written and the stage advanced to PLANNING. Exit code 1 means the gate refused — the stderr text identifies the cause. Do not bypass with `transition --force`.

---

## Step 5 — Generate Plan + Execution Graph

(transition_task auto-appends the `[STAGE] → PLANNING` log line; do not write it manually.)

Choose planning mode through ctl:

```bash
python3 "{{HOOKS_PATH}}/ctl.py" run-planning-mode .dynos/task-{id}
```

Use the JSON output as authoritative:
- `planning_mode == "fast_track_combined"`: use the fast-track combined flow
- `planning_mode == "hierarchical"`: use hierarchical planning
- `planning_mode == "standard"`: use standard planning

Do NOT re-derive fast-track, risk-based escalation, or acceptance-criteria thresholds in prompt logic.

Hierarchical flow:
1. Spawn Master Planner (Opus) for strategic boundaries.
2. Spawn Worker Planners in parallel for non-overlapping subsystems.
3. Merge outputs into final `plan.md` and an execution-graph payload, then persist the final graph ONLY via `python3 hooks/ctl.py write-execution-graph .dynos/task-{id} --from -` using stdin.

Standard flow:
1. Spawn Planner (Opus) with instruction to generate `plan.md` and an execution-graph payload, then persist the final graph ONLY via `python3 hooks/ctl.py write-execution-graph .dynos/task-{id} --from -` using stdin.

After generation, run deterministic artifact validation before any human review. If available in this repo, run:

```text
python3 hooks/validate_task_artifacts.py .dynos/task-{id}
```

The command is the source of truth for artifact validation. Use the rules below to explain and repair failures:

For `plan.md`:
1. It must contain `Technical Approach`, `Reference Code`, `Components / Modules`, `Data Flow`, `Error Handling Strategy`, `Test Strategy`, `Dependency Graph`, and `Open Questions`.
2. Every component or module section must list exact files.
3. `Reference Code` paths must exist in the repo unless explicitly marked as to-be-created.

For `execution-graph.json`:
1. It must parse as valid JSON.
2. Every segment must have unique `id`.
3. Every segment must declare exactly one valid executor.
4. No file may appear in more than one segment's `files_expected`.
5. Every `depends_on` reference must point to an existing segment.
6. The dependency graph must be acyclic.
7. Every `criteria_id` must map to a real acceptance criterion in `spec.md`.
8. Every acceptance criterion in `spec.md` must be covered by at least one segment.

If any validation fails, respawn planning and fix the artifacts before continuing.

Append to the execution log (transition_task auto-appends the `[STAGE] → PLAN_REVIEW` line — only the `[DONE]` line is the skill's responsibility):

```text
{timestamp} [DONE] planning — final plan.md and execution-graph.json written (mode: {hierarchical|standard})
```

Transition the stage by running:

```text
python3 "{{HOOKS_PATH}}/ctl.py" transition .dynos/task-{id} PLAN_REVIEW
```

---

## Step 6 — Plan Review

This gate always runs. There is no skip path.

Present `plan.md` to the user and ask for approval.

- If approved: run `python3 "{{HOOKS_PATH}}/ctl.py" approve-stage .dynos/task-{id} PLAN_REVIEW`. This hashes the current `plan.md`, writes the `human-approval-PLAN_REVIEW` receipt with that hash, and atomically advances PLAN_REVIEW → PLAN_AUDIT. Exit code 0 means success; exit code 1 means the gate refused (stderr identifies the cause). Do not bypass with `transition --force`. Do NOT add a manual `[HUMAN]` log line — the receipt is the audit trail.
- If changes are requested: append the feedback, respawn planning, re-run deterministic artifact validation, and present the updated plan again. Do NOT call `approve-stage` against a stale plan.
- If rejected outright: run `python3 hooks/ctl.py transition .dynos/task-{id} FAILED`, append `[FAILED] Plan rejected by user`, and stop. Do not edit `manifest.json` directly.

---

## Step 7 — Plan Audit

**Fast-track skip:** If `manifest.json` has `"fast_track": true`, check the project policy at `~/.dynos/projects/{slug}/policy.json` for `"fast_track_skip_plan_audit": true`. If both are true, skip the plan audit entirely and proceed to Step 8. Log: `{timestamp} [SKIP] plan audit — fast_track_skip_plan_audit policy`. This policy is set automatically by the improvement engine when low-risk tasks consistently pass without repair.

**Normal path:**

1. Spawn `spec-completion-auditor` to verify that `plan.md` and `execution-graph.json` cover all acceptance criteria in `spec.md`.
2. If the auditor finds gaps, route back to planning, repair the gaps, and rerun both deterministic artifact validation and the plan audit.
3. Create a git branch safety net: `dynos/task-{id}-snapshot`.

---

## Step 8 — TDD-First Gate

This gate always runs. There is no skip path.

1. Spawn `testing-executor` with instruction:

```text
TDD-First Mode.
Read spec.md and plan.md.
Write a complete test suite covering every acceptance criterion.
Do not implement production code.
Write only test files and evidence to .dynos/task-{id}/evidence/tdd-tests.md.
```

2. Deterministically validate the generated tests before user review:
   - Every acceptance criterion from `spec.md` must be mapped to at least one test case in the evidence summary.
   - Test file paths must be under the repository root.
   - No production source files may be modified in this step.
3. Present the test file paths and summary to the user.
4. If changes are requested, rerun the testing executor and the same deterministic validation.
5. When validation passes (and before asking for human approval), write the TDD receipt:

   ```python
   from pathlib import Path
   from dynoslib_receipts import receipt_tdd_tests, hash_file

   evidence_path = Path(".dynos/task-{id}/evidence/tdd-tests.md")
   receipt_tdd_tests(
       task_dir=Path(".dynos/task-{id}"),
       test_file_paths=[...],
       tests_evidence_sha256=hash_file(evidence_path),
       tokens_used=TOTAL_TOKENS,
       model_used="opus",
   )
   ```

6. When the user approves the test suite, transition out of TDD_REVIEW via the `approve-stage` ctl command. This hashes `evidence/tdd-tests.md`, writes the `human-approval-TDD_REVIEW` receipt with that hash, and advances TDD_REVIEW → PRE_EXECUTION_SNAPSHOT in one atomic step. Do NOT append a manual `[HUMAN]` log line:

   ```text
   python3 "{{HOOKS_PATH}}/ctl.py" approve-stage .dynos/task-{id} TDD_REVIEW
   ```

   Exit code 0 means success; exit code 1 means the gate refused (stderr text identifies the cause). Do not bypass with `transition --force`.

7. Commit the approved tests to the snapshot branch before any production code is written.

---

## Step 9 — Done

Transition the stage by running:

```text
python3 "{{HOOKS_PATH}}/ctl.py" transition .dynos/task-{id} PRE_EXECUTION_SNAPSHOT
```

Append to the execution log:

```text
{timestamp} [ADVANCE] PLAN_AUDIT → PRE_EXECUTION_SNAPSHOT
```

Print:

```text
Foundry Ready to Execute.

Task:   {task_id}
Spec:   {N} acceptance criteria (human approved)
Plan:   approved, validated, and audited (mode: {standard|hierarchical})
Memory: advisory only

Next: /dynos-work:execute
```

---

## What you do NOT do

- You do not execute production code.
- You do not audit production code.
- You do not decide when the task is done.
- You do not skip discovery, spec review, plan review, plan audit, or TDD review.
- You do not let historical memory or the Founder override human approval or deterministic validation.

---

## Hard Rules

- **No stealth paths:** all tasks follow Steps 0-9.
- **Memory is advisory:** trajectory retrieval may inform questions or design review, but it never overrides the current repo or user instructions.
- **Founder is a consultant:** simulation output informs design decisions but never replaces review gates.
- **Validation before trust:** do not present `spec.md`, `plan.md`, `execution-graph.json`, or the generated test suite for approval until deterministic validation passes.
- **Stop on ambiguity:** if a blocking ambiguity remains unresolved after discovery, flag it explicitly instead of silently choosing.
