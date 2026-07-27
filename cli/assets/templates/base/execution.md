---
name: execution
description: "Internal dynos-work executor skill group. Routes execution graph segments to the specialized executor skills. Invoked by /dynos-work:execute as part of the pipeline, or when the user explicitly types /dynos-work:execution; never auto-triggered from conversation."
---

# dynos-work: Execution Container

The `execution` skill is a container for the following executor sub-agents, each invoked during `/dynos-work:execute` based on the segment's declared executor type:

- `backend-executor` — backend code segments
- `data-executor` — ETL, analytics, backfill, reconciliation, and data-quality segments
- `db-executor` — database-only segments
- `docs-executor` — documentation updates and accuracy fixes
- `infra-executor` — infrastructure, deployment, CI/CD, and environment segments
- `integration-executor` — cross-system wiring
- `ml-executor` — ML/data-pipeline segments
- `observability-executor` — logs, metrics, traces, alerts, and reliability instrumentation
- `release-executor` — version, changelog, rollout, rollback, and release-operation segments
- `refactor-executor` — restructuring with no behavior change
- `security-executor` — auth, authorization, secret-handling, validation, and vulnerability remediation
- `testing-executor` — test-suite additions and TDD
- `ui-executor` — UI-only segments

Inputs and outputs are defined in this skill's `contract.json`. Each executor reads:
- the segment (from `execution-graph.json`)
- matched acceptance criteria text (from `spec.md`)
- dependency evidence (previously generated `evidence/{dep}.md` files)
- prevention rules filtered from `project_rules.md`

Each executor writes `evidence/{segment-id}.md` documenting modified files, integration points, failure cases handled, required config, and the acceptance criteria it satisfied.
