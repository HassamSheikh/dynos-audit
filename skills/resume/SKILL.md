---
name: resume
description: "Resume an interrupted dynos-work task from .dynos/ state. Runs only when the user explicitly types /dynos-work:resume; never auto-triggered from conversation, by session restart, by context compression, or by the word 'resume' appearing in a request."
---

# dynos-work: Resume

Resume a dynos-work task that was interrupted.

## Ruthlessness Standard

- Do not guess the next command from vibes; derive it from the actual task stage.
- Missing or corrupt artifacts are not "probably fine". Call them out.
- If multiple active tasks exist, do not silently pick the wrong one.

## What you do

1. List all tasks in `.dynos/` that are not DONE or FAILED (read each manifest.json). If available in this repo, use `"${CODEX_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}/bin/dynos" ctl active-task`.
2. If one active task: resume it automatically
3. If multiple active tasks: show list and ask which to resume
4. Read `manifest.json` to determine current stage
5. Tell the user which command to run next based on the stage:

| Stage | Run this |
|---|---|
| SPEC_NORMALIZATION, SPEC_REVIEW | `/dynos-work:start` |
| PLANNING, PLAN_REVIEW, PLAN_AUDIT | `/dynos-work:plan` |
| PRE_EXECUTION_SNAPSHOT, EXECUTION, TEST_EXECUTION | `/dynos-work:execute` |
| CHECKPOINT_AUDIT, REPAIR_PLANNING, REPAIR_EXECUTION, FINAL_AUDIT | `/dynos-work:audit` |

## Output on resume

```
dynos-work: Resuming task-20260327-001
Title: [task title]
Current stage: CHECKPOINT_AUDIT

Run: /dynos-work:audit
```

## Edge cases

- If manifest.json is corrupt or unreadable: report the error and suggest starting fresh with `/dynos-work:start`
- If execution-graph has segments with no evidence files: `/dynos-work:execute` will re-run those segments
- If audit-reports exist from before interruption: `/dynos-work:audit` will read them and determine if re-audit is needed

## When to use

- After a session restart during a long task
- After Claude Code context was compressed mid-task
- After a network interruption
- After manually investigating the task state with `/dynos-work:status`
