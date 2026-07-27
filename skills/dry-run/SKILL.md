---
name: dry-run
description: "Internal dynos-work skill. Validate contract.json chaining across the skill pipeline. Checks that output_schema of each pipeline stage covers the required input_schema fields of the next stage. Runs only when the user explicitly types /dynos-work:dry-run; never auto-triggered from conversation."
---

# dynos-work: Dry Run

Validate that the contract.json files across all skills form a valid pipeline chain. This skill reads every `skills/*/contract.json`, determines the pipeline order, and verifies that each stage's outputs satisfy the next stage's required inputs.

## Ruthlessness Standard

- Treat missing or incompatible contract fields as real pipeline breakage, not paperwork.
- Do not infer compatibility from naming similarity alone.
- If a downstream stage depends on behavior the upstream contract does not guarantee, call it broken.

## What you do

### Step 1 -- Discover contracts

1. Scan all directories under `skills/` in the project root.
2. For each directory, check if a `contract.json` file exists.
3. If any skill directory lacks a `contract.json`, emit a warning that names every specific directory:
   ```
   WARNING: Missing contract.json in: skills/foo/, skills/bar/
   ```
4. Parse each discovered `contract.json` file. If any file contains invalid JSON, report it as an error and skip that contract.

### Step 2 -- Determine pipeline order

The linear pipeline chain is fixed:

```
start -> plan -> execute -> audit -> learn
```

Skills that are NOT part of the linear pipeline (utility skills) are:
- status
- investigate
- resume
- repair
- execution
- dashboard
- dry-run

Utility skills have contracts but do not participate in linear chain validation. They should still be loaded and listed in the report.

### Step 3 -- Validate chain links

For each consecutive pair in the pipeline (stage N -> stage N+1):

1. Read the `output_schema` of stage N.
2. Read the `input_schema` of stage N+1.
3. For every field in stage N+1's `input_schema` where `required` is `true`:
   - Check if a field with a matching key exists in stage N's `output_schema`.
   - If the field exists, check that the `type` values are compatible (exact match or both resolve to the same base type).
   - If the field is missing from stage N's `output_schema`, record a FAIL with details.
   - If the field exists but the type does not match, record a FAIL with details.
4. For optional fields (where `required` is `false`), check availability but only emit an INFO-level note if missing, not a FAIL.

**Important:** Some inputs have `source` values that reference things outside the immediate predecessor (e.g., user prompt, filesystem paths, or outputs from earlier stages). When an input field's `source` explicitly references a specific origin other than the previous pipeline stage, note it but do not count it as a chain break. The chain validation focuses on whether the pipeline as a whole produces what downstream stages need.

### Step 4 -- Generate report

Print a structured report with the following sections:

```
=== dynos-work Dry Run: Contract Chain Validation ===

Pipeline: start -> plan -> execute -> audit -> learn

--- Contracts Loaded ---
  [OK]   skills/start/contract.json
  [OK]   skills/plan/contract.json
  ...
  [WARN] skills/dashboard/ -- missing contract.json

--- Chain Validation ---

start -> plan:
  [PASS] manifest.json (object -> object)
  [PASS] spec.md (string -> string)
  [FAIL] design-decisions.md: present in plan input_schema (required: false) but missing from start output_schema [INFO]
  ...

plan -> execute:
  [PASS] manifest.json (object -> object)
  ...

execute -> audit:
  ...

audit -> learn:
  ...

--- Summary ---
Pipeline stages validated: 4
Total required fields checked: NN
Passed: NN
Failed: NN
Warnings: NN

Result: PASS (or FAIL if any required field is missing/mismatched)
```

### Step 5 -- Runtime validation (optional)

If an active or completed task exists in `.dynos/`, run the runtime contract validator against it to verify real artifacts match the declared contracts:

```text
"${CODEX_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}/bin/dynos" ctl validate-contract --skill start --task-dir .dynos/task-{id} --direction both --strict
"${CODEX_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}/bin/dynos" ctl validate-contract --skill execute --task-dir .dynos/task-{id} --direction both --strict
"${CODEX_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}/bin/dynos" ctl validate-contract --skill audit --task-dir .dynos/task-{id} --direction both --strict
```

Report any mismatches between declared contracts and actual artifacts. This catches drift between what contracts promise and what skills actually produce.

### Step 6 -- Exit

If all required chain links pass, print:
```
Dry run PASSED. All pipeline contracts are compatible.
```

If any required chain link fails, print:
```
Dry run FAILED. See mismatches above.
```

## Contract Schema Reference

Every skill should have a `contract.json` file in its skill directory (`skills/<name>/contract.json`). The schema is:

```json
{
  "skill": "<skill-name>",
  "description": "<what this skill does>",
  "input_schema": {
    "<field-name>": {
      "type": "<string | object | array | boolean>",
      "required": true | false,
      "source": "<where this input comes from, e.g. '.dynos/task-{id}/manifest.json'>"
    }
  },
  "output_schema": {
    "<field-name>": {
      "type": "<string | object | array | boolean>",
      "description": "<what this output contains>"
    }
  }
}
```

### Field definitions

| Field | Required | Description |
|-------|----------|-------------|
| `skill` | Yes | The skill name, must match the directory name |
| `description` | Yes | Human-readable description of what the skill does |
| `input_schema` | Yes | Map of field names to input descriptors |
| `input_schema.<field>.type` | Yes | Data type: `string`, `object`, `array`, or `boolean` |
| `input_schema.<field>.required` | Yes | Whether this input is mandatory for the skill to run |
| `input_schema.<field>.source` | Yes | Where the input originates (file path pattern, user prompt, etc.) |
| `output_schema` | Yes | Map of field names to output descriptors |
| `output_schema.<field>.type` | Yes | Data type of the output |
| `output_schema.<field>.description` | Yes | Human-readable description of the output content |

### Pipeline vs. utility skills

- **Pipeline skills** (start, plan, execute, audit, learn) form a linear chain. The output_schema of each stage must cover the required input_schema fields of the next stage.
- **Utility skills** (status, investigate, resume, repair, execution, dashboard, dry-run) operate independently. They still need contracts for documentation and tooling, but they are not validated as part of the linear chain.

### Tips for skill authors

1. Keep field names consistent across stages. If `start` outputs `manifest.json`, then `plan` should reference `manifest.json` as an input, not `task_manifest`.
2. Use the `source` field to document where the data actually lives on disk or comes from at runtime.
3. Mark fields as `required: false` if the skill can operate without them (graceful degradation).
4. The `type` field should match across producer and consumer. If `start` outputs `manifest.json` as type `object`, then `plan` should expect type `object` for `manifest.json`.
