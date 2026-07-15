# Spawn-budget backstop: re-base off clean-audit counting (PR 3)

## Problem

The spawn-budget backstop pauses a task when its "wasted spawn" count crosses a
learned threshold. "Wasted" was defined as an **audit report with empty
findings** — a *clean* audit (`compute_spawn_budget_status` in `hooks/ctl.py`
and the retrospective builder in `hooks/lib_validate.py` both used
`len(findings) == 0`). That is inverted: a clean audit is a **passing
dimension**, the goal — not waste. With a default threshold of 2, any healthy
task whose two-plus audit dimensions pass cleanly marched toward a hard pause
(`wasted_spawns_exceeded`) that halted the task and required a human
`ctl spawn-resume`. Priority is finishing the work; the backstop must fire only
on **genuine non-convergence**, never on clean passes.

## New signal: repair non-convergence

A repair *converges* when the fix sticks. `repair-log.json` already tracks a
per-finding `retry_count`, and `retry_count >= 2` already triggers deep-tier
escalation (build-repair-log). We reuse that as the backstop signal:

    wasted_spawns := count of distinct findings whose max retry_count >= 2

i.e. findings that were repaired and re-flagged repeatedly — the repair loop
churning without resolving them. A task that never needed repair, or whose
repairs stuck, scores 0. Implemented once as
`lib_validate.count_nonconverging_repairs(task_dir)` and used by **both** the
runtime pause decision and the retrospective, so the signal has a single
definition.

## What this removes

The ensemble-cascade dedup and `exempt_auditors` logic in
`compute_spawn_budget_status` existed **only** to soften the clean-audit count
(dedup ensemble tiers so one auditor isn't counted N times; exempt auditors
with long zero-finding streaks). Under the repair-convergence signal they are
obsolete and are removed. `auditor_zero_finding_streaks` stays in the
retrospective (other consumers use it) but no longer feeds a spawn-budget
exemption.

## Migration: cold-start the learned thresholds

`policy_engine._build_spawn_budget_policy_data` learns per-task-class thresholds
from the distribution of historical `wasted_spawns`. Those observations were
clean-audit counts — incomparable to the new signal. Mixing them would poison
the baseline. Cold-start cleanly by **tagging the signal version**:

* Retrospectives now carry `wasted_spawns_signal_version = 2`.
* `_build_spawn_budget_policy_data` aggregates **only** retrospectives at the
  current signal version; pre-migration retrospectives (no tag / v1) are skipped
  for spawn-budget learning. The emitted policy is stamped `version: 2`.
* Until a task class accumulates >= 3 v2 observations, it has no learned entry
  and the `global_fallback` threshold (2) governs. With the new signal, count 2
  means two findings each stuck in repair — a reasonable pause point.

`circuit_breaker.WASTED_SPAWN_ABORT_THRESHOLD` (9) is unchanged in value; it now
means nine non-converging repairs — still a valid hard-abort runaway guard.

## Blast radius

`hooks/ctl.py` (runtime counter), `hooks/lib_validate.py` (retrospective +
helper), `memory/policy_engine.py` (cold-start filter), `hooks/circuit_breaker.py`
(docs), plus test migration for `test_spawn_budget_policy.py`,
`test_circuit_breaker.py`, and any suite asserting the clean-audit semantics.
`wasted_spawns` stays an int on the retrospective and dashboard, so downstream
KPI/dashboard consumers keep working — only its meaning changes.
