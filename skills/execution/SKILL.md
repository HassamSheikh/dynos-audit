---
name: execution
description: "Internal dynos-work executor skill group. Routes execution graph segments to the specialized executor skills. Invoked by /dynos-work:execute as part of the pipeline, or when the user explicitly types /dynos-work:execution; never auto-triggered from conversation."
---

# dynos-work: Execution Skill Group

This directory is the container for specialized executor skills:

- `backend-executor`
- `data-executor`
- `db-executor`
- `infra-executor`
- `integration-executor`
- `ml-executor`
- `observability-executor`
- `release-executor`
- `refactor-executor`
- `security-executor`
- `testing-executor`
- `ui-executor`

Do not invoke this grouping skill directly for implementation work. The `execute` skill owns routing, prompt injection, role stamping, spawn-budget checks, and receipt validation, then dispatches each execution graph segment to the matching specialized executor.

If this skill is selected directly, stop and run the `execute` skill instead.
