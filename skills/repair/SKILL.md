---
name: repair
description: "Internal dynos-work skill. Manually repair one specific finding without running the full audit loop (/dynos-work:audit handles repair automatically). Runs only when the user explicitly types /dynos-work:repair; never auto-triggered from conversation."
---

# dynos-work: Repair

Manually repair a specific finding from an audit report. Use this when you need to fix a precise issue without running the full audit pipeline.

## Ruthlessness Standard

- A repair that cannot survive re-audit is not a repair.
- Fix the mechanism, not the screenshot of the symptom.
- If the instruction is vague, stop and sharpen it before spawning an executor.

## What you do

1. Find the active task in `.dynos/`
2. Read the latest audit reports
3. If the user specifies a finding ID, repair that finding only
4. If no finding specified, show all open blocking findings and ask which to repair
5. Spawn the appropriate executor subagent with the precise repair instruction
6. After repair, re-run only the auditor(s) that reported the finding (plus always spec-completion and security)
7. Report new audit result

## Usage

```
/dynos-work:repair                    — shows all open findings, ask which to fix
/dynos-work:repair sec-003            — repairs finding sec-003
/dynos-work:repair --all              — repairs all open findings in parallel (where file-safe)
```

## Executor selection

Use the finding's `assigned_executor` from the audit report. If it is missing, stop and surface the gap instead of inferring ownership from file extension or guessing.

## Hard rules

- Always re-audit after repair — do not assume the fix worked
- Always include spec-completion and security in the re-audit
- Do not hand-write `repair-log.json`; that file belongs to the deterministic audit repair loop
