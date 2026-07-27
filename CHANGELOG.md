# Changelog: dynos-work

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to **Semantic Versioning**.

---

## [Unreleased]

---

## [7.5.16] - 2026-07-27
### Fixed
- dynos-work commands no longer fire without the user typing them.
  `/dynos-work:<command>` is the only invocation route. Command names are
  unchanged — `/dynos-work:resume`, `/dynos-work:status`, and so on.
- Root cause was not the slash namespace (the plugin prefix already supplies
  that) but the two surfaces that invoke a skill *without* a typed command:
  - **Skill descriptions.** A description is a trigger signal, and these were
    written as capability advertisements. `resume` said "Use after session
    restart or context compression", so restarting a session could fire it;
    `status` matched any question about status; `init` matched project-setup
    requests; `maintain` advertised that it periodically scans and opens pull
    requests. All 21 descriptions (and their 20 template mirrors) now state
    what the command does and scope invocation to the explicit
    `/dynos-work:<name>` form. `memory` and `execution` additionally keep
    their legitimate pipeline-invoked route. The `founder` command template,
    which has no `skills/` counterpart, is covered too.
  - **Agent prompts.** The same leak existed one level down: an agent
    description like "Implements API routes, services, business logic, and
    auth" reads as a standing offer, so the model could spawn an executor or
    auditor spontaneously outside any dynos-work task — a stray executor
    writes code. All 37 agents in `agents/` and their 34 mirrors under
    `cli/assets/templates/base/agents/` now name the `/dynos-work:<command>`
    that spawns them and forbid direct spawning.
  - **The SessionStart hook.** `hooks/session-start` injected
    "Use dynos-work start for new tasks." into every session — a standing
    instruction to enter the pipeline whenever a request looked task-shaped.
    It now states that commands are user-invoked only and that the pipeline
    must never be entered on the agent's own initiative.

### Added
- Guard test (`tests/test_explicit_invocation_only.py`, 160 cases) covering
  every invocable surface: each skill and command template names its typed
  command and declares itself never auto-triggered; each agent names the
  command that spawns it and forbids direct spawning; no description on
  either surface contains auto-trigger phrasing ("use after", "runs
  automatically", "periodically", "proactively", "whenever"); template
  mirrors have not drifted; skill names stay bare so the command is singly
  namespaced; and the SessionStart hook does not reintroduce the standing
  invocation order.

---

## [7.5.15] - 2026-07-25
### Added
- Frontier tier (`TIER_FRONTIER` → `fable`) as a fourth rung above `deep` in
  `lib_models.TIER_TO_MODEL`, plus `TIER_RANK` as the single authority for
  tier ordering. Codex maps it to `None` like every other tier.
- Role tier ceilings (`lib_models.ROLE_TIER_CEILINGS`, `max_tier_for_role`,
  `clamp_model_to_role_ceiling`). `router.resolve_model` is now a thin wrapper
  that clamps the inner selector's result, so no selection path — explicit
  policy override, epsilon-greedy exploration, UCB winner, benchmark
  selection, learned history, or default — can put an executor role above
  `deep`. A clamp emits `router_model_ceiling_clamp` and preserves the
  pre-clamp pick on `uncapped_model`.
- Guard test (`tests/test_tier_ceiling_invariant.py`) driving the ceiling
  through the production `resolve_model` entry point rather than asserting on
  constants.

### Changed
- `planning` default tier moves `balanced` → `frontier`; `agents/planning.md`
  frontmatter follows. Plan quality sets the cost of every downstream executor
  spawn, which is where the spend actually is.
- Ensemble escalation model moves `deep` → `frontier`. Auditors keep their
  existing default tiers, so a clean first pass never pays frontier rates —
  only a split between the fast/balanced voting arms spends a frontier spawn.
- Security floor in `router.resolve_model` and the `security-auditor`
  monotonicity check in `policy_engine.derive_model_policy` are now rank
  comparisons. As equality checks against the deep-tier literal, both would
  have classified a frontier pick as *below* floor and downgraded it.
- `circuit_breaker._deep_tier_zero_yield_count` counts spawns at the deep tier
  **or above**, so an auditor escalated to frontier that returns no findings
  still trips the breaker. The `model=None` identity match under codex is
  preserved.
- `tests/test_model_literal_guard.py` regex extended with `fable`; without it
  the new literal could leak outside `lib_models.py` unnoticed.

---

## [7.5.14] - 2026-07-15
### Added
- Guard test (`tests/test_domain_registry_invariant.py`) asserting
  `lib_core.VALID_DOMAINS` and the `domain_conditional` keys in
  `router._DEFAULT_AUDITOR_REGISTRY` stay a set-equal, closed vocabulary.
  `build_audit_plan` selects domain-scoped auditors via `dict.get(domain, [])`,
  so a domain present in one but not the other silently drifts: a new valid
  domain would run only the `always` roster, and a typo'd registry key would be
  a dead entry that never fires. The test fails on either drift and documents
  that an intentionally `always`-covered domain must carry an explicit
  empty-list entry rather than be omitted.

---

## [7.5.13] - 2026-07-15
### Changed
- Re-based the spawn-budget backstop off clean-audit counting. "Wasted spawns"
  previously meant *empty-findings audit reports* — a clean audit is a passing
  dimension, so with a default threshold of 2 any healthy task whose dimensions
  passed would trip a hard pause requiring a human `spawn-resume`. The signal is
  now **repair non-convergence**: distinct findings the repair loop keeps
  re-flagging (max `retry_count >= 2` in `repair-log.json`), computed once in
  `lib_validate.count_nonconverging_repairs` and used by both the runtime pause
  (`compute_spawn_budget_status`) and the retrospective. The obsolete
  ensemble-dedup and exempt-auditor machinery that only existed to soften the
  clean-audit count is removed. `circuit_breaker.WASTED_SPAWN_ABORT_THRESHOLD`
  (9) is unchanged in value; it now counts non-converging repairs.
- Cold-started the learned thresholds: retrospectives carry
  `wasted_spawns_signal_version = 2`, `policy_engine` aggregates only current-
  version observations (pre-migration clean-audit counts are skipped) and stamps
  the policy `version: 2`, and `compute_spawn_budget_status` honors learned
  per-task-class thresholds only under a v2+ policy — falling back to
  `global_fallback` until the new signal re-learns. Design:
  `docs/spawn-budget-convergence-design.md`.

---

## [7.5.12] - 2026-07-15
### Added
- Segment continuation loop so execution finishes the work instead of stalling
  when an executor runs out of turns mid-segment. New `ctl next-continuation`
  reports every incomplete segment (reusing the finish gate's own authority:
  pending segments + evidence/`files_expected` verification) together with a
  resume seed — role, model, `files_expected`, `criteria_ids`, and the exact
  `incomplete_reasons` — so a continuation executor picks up the partial work on
  disk rather than cold-restarting. The execute skill now loops on it until
  `complete`; budget is never the stop condition. The only automatic halt is a
  genuine stall — two consecutive continuations that move nothing (no cleared
  reason, no evidence growth, no new `files_expected` on disk) — reported as
  status `stalled` (exit 3) for escalation instead of spinning forever.
  `continuation-state.json` (stall fingerprints/attempt counts) is ctl-owned and
  denied to agent roles in the write policy. Adds `tests/test_next_continuation.py`.

---

## [7.5.11] - 2026-07-15
### Fixed
- Project-root detection no longer treats the framework home (`~/.dynos` /
  `$DYNOS_HOME`) or a stray orphan `.dynos` as a project control plane. The
  `.dynos` name is overloaded — it is both the framework home and each
  project's control plane — so the old bare-name ancestor walk resolved `$HOME`
  (which always contains `~/.dynos`) as one giant project, pulling every
  unrelated repository under it into governance and producing spurious
  write-policy denials in folders that are not dynos projects. A new shared
  discriminator (`hooks/lib_dynos_root.is_project_dynos_dir`) rejects the
  framework home outright and requires a candidate `.dynos` to be a registered
  project root (or to carry on-disk project state) before it can govern its
  parent. Applied to every ancestor walk in `write_policy` and `pre_tool_use`,
  and the out-of-scope write path now decides from the target location instead
  of governing-when-unsure. Adds `tests/test_dynos_root_scope.py`.
- Cleared six pre-existing red tests on `main`. Five were stale fixtures that
  built task directories without a `manifest.json` and so were dropped by the
  stale-binding hardening (7.5.10) before role/scope resolution ran; their
  helpers now write a non-terminal manifest as a real task dir always would
  (`test_role_file_fallback`, `test_bash_destination_extraction`). The sixth
  was a genuine model-literal lint violation: an escalation comment in
  `hooks/lib_core.py` named vendor model tiers directly; it now uses the
  cheap/mid/deep-tier vocabulary already used elsewhere in the file.

---

## [7.5.10] - 2026-06-23
### Fixed
- Terminal task transitions now clear per-session task bindings, so a completed
  dynos-work task no longer leaves the pinned orchestrator session governed by
  stale write-policy state. Stale `DYNOS_TASK_DIR` values and session-task
  bindings are also ignored or pruned when their manifest is already terminal.

---

## [7.5.9] - 2026-06-22
### Fixed
- Ensemble DONE gate now binds escalation on `finding_count`, not just
  `blocking_count`. The cascade protocol escalates to the deep tier (opus) on
  **any** voting-model finding — a non-blocking nit at haiku still warrants opus
  confirmation — but `_check_ensemble_voting` (`lib_core.py`) previously accepted
  a clean-ish ensemble whenever every voting receipt was zero-*blocking*. That
  left a seam: an auditor with non-blocking haiku findings could be run at sonnet
  (or skip escalation entirely) and still pass the gate, silently dropping the
  protocol-required opus shard while the prose rule was satisfied only on paper.
  The gate now requires an opus escalation receipt whenever any voting-model
  receipt reports `finding_count != 0`, so the documented cascade is enforced by
  the harness instead of left to the spawning agent's discretion under token
  pressure. Added regression coverage for the non-blocking-finding path
  (`test_gate_done_ensemble.py`).

---

## [7.5.8] - 2026-06-22
### Fixed
- Auditors no longer run out of turns before writing their report. The auditor
  prompt builder (`router.py audit-inject-prompt`) now deterministically
  appends a write-first "Turn Budget Discipline" block to **every** auditor
  spawn — sized to the selected model/tier (15 / 20 / 25 tool calls) —
  instructing the auditor to write its report skeleton plus a Progress Ledger
  before reading the diff and to finalize a (possibly truncated) report rather
  than hit the `maxTurns` cap with nothing on disk. Previously only the
  executor prompt builder injected this; auditors relied on a per-file section
  that 16 of 20 auditor agents were missing, so sonnet/haiku auditors
  (architecture, test-strategy, docs-accuracy, …) routinely died before
  writing and had to be re-spawned.
- Normalized all 20 auditor agent files to carry the `## Turn Budget
  Discipline` and `## Progress Ledger` sections, so standalone spawns that
  bypass the injector get the same discipline.

### Removed
- Deleted the dead per-session write-first watchdog (`pre_tool_use.py
  ::_run_watchdog` and `_watchdog_*` helpers) and the orphaned `ctl spawn-prep`
  command (plus its constants, argparser, and the `clear-role` attempt-stripping
  block). The watchdog could never fire: the audit path arms grants via `ctl
  stamp-role`, which never sets `expected_artifact`/`budget`, and grants are not
  consumed under the shared-session actor-resolution path, so it always
  fail-opened. `spawn-prep` (the only writer of those grant fields and the
  report skeleton) had zero callers. Simplified `actor_identity.append_grant`
  to drop the now-unused `expected_artifact`/`attempt`/`budget` kwargs, and
  updated `skills/audit/SKILL.md` to stop documenting a safety net that did not
  exist. Removed the obsolete `test_watchdog.py`, `test_spawn_prep.py`,
  `test_ctl_spawn_prep.py`, and `test_pre_tool_use.py`.

---

## [7.5.7] - 2026-06-22
### Fixed
- Task-id allocation is now collision-resistant across concurrent worktrees.
  The previous `task-{YYYYMMDD}-{seq}` scheme derived the sequence by globbing
  the local, root-relative `.dynos/`, so worktrees that branched at once each
  saw an empty set, all landed on `seq=001`, and produced identical ids —
  causing last-writer-wins clobbering in the shared persistent project store
  (keyed by task_id). Ids now carry a 32-bit CSPRNG entropy suffix
  (`task-{YYYYMMDD}-{seq:03d}-{hex}`) via a single shared
  `lib_core.allocate_task_id` helper used by both `ctl.py` and
  `manual_pipeline.py`, which also unifies the date segment on UTC and claims
  the task dir atomically with retry. Strict `\d{3}$` task-id matchers in
  `lib_tokens_hook.py` and the dashboard vite-plugin were widened to accept the
  optional suffix while still rejecting path traversal.

---

## [7.5.6] - 2026-06-21
### Fixed
- Enforce per-model ensemble audit receipt accounting by requiring `audit-{auditor}-{model}` shard receipts, rejecting pre-prefixed `audit-*` shard names, and documenting the required audit skill invocation.

### Plugin / Distribution
- Bump package and plugin metadata to `7.5.6`.

---

## [7.5.5] - 2026-06-21
### Fixed
- Prevent concurrent dynos-work sessions from sharing project-global active-task and orchestrator-session state by adding session-scoped task bindings and multi-session orchestrator pins.
- Resolve hook task context from absolute tool target paths before falling back to `cwd`, preventing linked-worktree auditors from inheriting the main worktree's role state when Claude reports an ambiguous subagent cwd.
- Harden spawn-log and web-tool telemetry hooks so ambiguous multi-task checkouts skip instead of logging to the highest-numbered task, while session bindings and explicit task paths route to the intended worktree task.
- Avoid token attribution to the wrong task when multiple tasks are active; unbound ambiguous sessions now preserve usage in the orphan token ledger.

### Plugin / Distribution
- Bump package and plugin metadata to `7.5.5`.

---

## [7.5.4] - 2026-06-19
### Changed
- Inject ruthless self-implicating audit briefs into dynos auditor prompts.

### Plugin / Distribution
- Bump package and plugin metadata to `7.5.4`.

---

## [7.5.3] - 2026-06-19
### Changed
- Expand foundry auditor and executor role coverage for architecture, threat modeling, contracts, accessibility, privacy, supply chain, infrastructure, observability, release, and data integrity work.

### Plugin / Distribution
- Bump package and plugin metadata to `7.5.3`.

---

## [7.5.2] - 2026-06-17
### Changed
- Add repo-level release hygiene automation.

### Plugin / Distribution
- Bump package and plugin metadata to `7.5.2`.

---

## [7.5.1] - 2026-06-15
### "Claude Subagent Compatibility": Stamped Roles Work Where Subagents Share a Session

Claude Code spawns subagents that share the orchestrator's `session_id` and `transcript_path` with no distinguishing field (claude-code #7881), so the D3 per-session actor model resolved every planner/executor/auditor subagent as `orchestrator`. Their role grants went unconsumed and `write_policy` denied their role-scoped writes — the planning and execute pipelines could only complete in fast-track inline mode. (#212)

### Fixed
- **Orchestrator role adoption:** a new `subagent_isolation` capability flag (`.dynos/config/policy.json`, default `false`) lets the pinned orchestrator session adopt the stamped `active-segment-role` on harnesses that do not isolate subagent sessions (Claude Code), making the existing `ctl stamp-role` calls effective. Setting `subagent_isolation: true` restores strict D3 (the orchestrator never adopts a stamped role).
- Forgery defenses unchanged: `control-plane.json` and the orchestrator-session pin remain denied to all roles; `audit-reports/` writes remain cross-checked against `spawn-log.jsonl` at receipt time.
- New diagnostic event `orchestrator_role_adopted` allowlisted in `hooks/lib_log.py`.

### Plugin / Distribution
- Bump `.claude-plugin/plugin.json`, `package.json`, `.claude-plugin/marketplace.json`, and `.codex-plugin/plugin.json` to `7.5.1`.

---

## [7.5.0] - 2026-06-12
### "Selection, Durability, Enforcement, Scope": Four-Layer Foundation for Audited Execution

This release completes the execution-audit-repair pipeline's foundation across selection truth, durability contracts, per-session enforcement, and compute budgets (task-20260612-001: 9-segment graph, TDD-first build, 4-layer validation, full receipting).

### Added
- **L1 Selection Truth:** receipt-backed `select_eligible_reports` with attempt isolation (`{auditor}-{model}-attempt-{n}.json` filenames), status/receipt/stage triple-check gate path, and permissive repair-planning path. `receipt_audit_done` gains `shard_step_name` and `stage` fields; audit-report schema introduces `status` (lifecycle) and `verdict` fields. `_collect_latest_audit_reports` removed.
- **L2 Durability:** `ctl spawn-prep` attempt isolation and continuation support; `append_grant` now carries `expected_artifact`, `attempt`, and `budget`. Durability Protocol and Progress Ledger added to all executor and auditor agents. `execute` skill mandates two-phase A/B writes in all entry points.
- **L3 Enforcement:** per-session write-first watchdog in `pre_tool_use.py` tracking tool-call-counters in `.json`, deny-once with K=5 cooldown, preventing runaway tool use mid-stream.
- **L4 Scope:** LOC-weighted `compute_segment_budget` (files_loc, LOC_BASE/LOC_SLOPE=400/1) with audit sharding (AUDIT_SHARD_FILE_THRESHOLD=30 files, LOC_THRESHOLD=8000), per-directory cluster briefs plus mandatory cross-cutting brief for large segments.

### Changed
- Receipt schema version bumped to match spawn-prep/execution flow changes; spawn receipts auto-compute attempt and stage fields.
- All executor agents (specification, execution, repair-coordinator, security-auditor, code-quality-auditor, performance-auditor) updated to include Durability Protocol and Progress Ledger in task narratives.
- Audit sharding logic applied to all audit segments; cluster briefs standardize large-file aggregation behavior.

### Plugin / Distribution
- Bump `.claude-plugin/plugin.json`, `package.json`, `.claude-plugin/marketplace.json`, and `.codex-plugin/plugin.json` to `7.5.0`.

---

## [7.4.2] - 2026-06-11
### "Codex Compatibility": Same Foundry, Second Host

This release adds Codex plugin packaging while preserving the existing dynos-work philosophy: human-approved spec and plan gates, deterministic control-plane writes, audited execution, repair loops, and project learning stay intact. Claude Code remains supported by the existing plugin metadata.

### Added
- **Codex plugin manifest** at `.codex-plugin/plugin.json`, including interface metadata and the existing `skills/` directory as the Codex-discoverable skill surface.
- **Execution grouping skill** at `skills/execution/SKILL.md` so Codex skill discovery accepts the executor grouping directory without changing the specialized executor flow.

### Changed
- Skill command funnels and hook commands now resolve the plugin root via `CODEX_PLUGIN_ROOT` first, falling back to `CLAUDE_PLUGIN_ROOT`, so the same `bin/dynos` control-plane path works under either host.
- Session-start context uses host-neutral dynos-work wording while keeping the same routing and start/execute/audit guidance.
- README now documents Claude Code and Codex install/use paths.
- Prose-policy test substitutions understand the new Codex/Claude root fallback expression.

### Fixed
- `telemetry/global_dashboard.py` no longer embeds escaped HTML inside an f-string expression, restoring Python compile compatibility for pre-3.12 interpreters.
- `run-audit-setup` now writes the required `audit-routing` receipt from the deterministic audit plan, so the DONE gate can enumerate routed auditors instead of failing on a missing receipt.

### Host-Aware Model Tier Abstraction (also in this release; task-20260611-001, built end-to-end by the foundry pipeline)

Built end-to-end by the foundry pipeline itself (task-20260611-001: spec 28 criteria, 9-segment graph, TDD-first RED suite, 2 repair cycles, ensemble re-audits — all receipted in-task). Model orchestration no longer speaks vendor names outside one module.

### Added
- **`hooks/lib_models.py`** (leaf): ordered tiers `fast < balanced < deep`, `TIER_TO_MODEL` per host (claude: haiku/sonnet/opus; codex: all null initially), `ROLE_DEFAULT_TIERS` (18 roles), `resolve_model_for_tier` / `model_to_tier` / `valid_models_for_host`. Guard test (`tests/test_model_literal_guard.py`) holds vendor literals to this module (`# noqa: model-literal` escape for prose) — burned 87 violations to 0.
- **`hooks/lib_host.py`** (leaf): `detect_host` (CODEX_PLUGIN_ROOT → CLAUDE_PLUGIN_ROOT → claude), persisted-host read/write for `.dynos/control-plane.json` — which is hook-owned in write_policy (agent writes denied; it is load-bearing for receipt anti-forgery).
- **`memory/lib_migrate_host.py`**: idempotent, receipt-guarded one-time backfill stamping learning records `host=claude` + `model_tier`; auto-triggered on the next policy run.
- **Receipts v7**: spawn receipts self-compute `{host, tier, resolved_model}`; writers REFUSE models invalid for the active host (claiming a claude model under codex fails at write time); `validate_receipt_model_field`; `RECEIPT_CONTRACT_VERSION` 6→7 with `spawn-*` floor.
- **Fail-closed wiring under null-mapping hosts**: `floor_unmet: true` recorded when a floor (security=deep) is unsatisfiable; ensemble voting disabled with `reason: host_null_mapping`; retry escalation records `escalation_unavailable` (router helper wired through ctl's escalation path); token capture marks `host_unsupported` instead of silently defaulting (entry path resolves the persisted host).
- 110+ new tests across 10 `tests/test_hostmodel_*.py` files plus the literal guard.

### Changed
- Claude behavior is byte-identical: the 18-role routing table resolves exactly as before (parametrized test); full-suite failure set byte-identical to the pre-task baseline.
- Q-learning arms re-keyed to tiers internally; emitted `model_override` resolves through the host (opus under claude — unchanged); tool budgets, validators, postmortem-improve proposals, and ctl escalation all consume `lib_models`.
- `agents/repair-coordinator.md` and `skills/audit/SKILL.md` prose use tier language with host-qualified examples; agents frontmatter intentionally untouched.
- 6 new prevention rules minted by the task's own postmortem (production-caller requirements, signature locks, write-first executor discipline).

### Plugin / Distribution
- Bump `package.json`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, and `.codex-plugin/plugin.json` to `7.4.2`.

---

## [7.4.1] - 2026-06-11
### "Permissions-ON": The Plugin Stops Denying Its Own Pipeline

A real permissions-on run (no `--dangerously-skip-permissions`, no blanket allow) showed the plugin denying actions its own skills prescribe: `/tmp` payload staging, `2>/dev/null` redirects, `rm active-segment-role`, orchestrator writes blocked by whatever role was last stamped for a subagent, and `run-execution-verify-evidence` hard-erroring after `run-execution-finish`. 7.4.1 fixes the causes per `docs/permissions-on-design.md` while tightening every guardrail it touches. Design doc: `docs/permissions-on-design.md`; spec addendum: `docs/write-boundary-spec.md`.

### Added
- **Per-actor role resolution (D3):** SessionStart pins the orchestrator session (`.dynos/orchestrator-session.json`, hook-owned); the main session always resolves to the new `orchestrator` role and never reads role files. Subagents consume single-use grants (`ctl grant-role`, ledger at `role-grants.json`, wrapper-required) bound immutably per session (`role-bindings.json`, hook-owned). `ctl clear-role` replaces the policy-denied `rm active-segment-role`. Legacy role-file resolution is preserved for unpinned sessions.
- **Verification-evidence runner (D6):** execution-graph segments may declare `verify_commands`; `ctl run-verification-evidence` executes them and captures exit codes + output into ctl-owned `evidence/verification/{seg}.json` (executors are policy-denied from writing it), hash-bound by a receipt. `run-execution-finish` requires a passing record for every declaring segment; the spec-completion auditor cites the record instead of trusting executor narrative — execution-based acceptance criteria are now machine-verified.
- **Task scratch namespace (D2):** `.dynos/task-*/_scratch/` is sanctioned temp space for recognized actor roles; CI asserts no gate/receipt/validator ever reads it.
- **stdin payloads (D1):** `write-classification` / `write-execution-graph` / `write-repair-log` accept `--from -`; skills pipe LLM-returned payloads via heredoc instead of staging files.
- **`ctl run-start-init`** replaces start Step 0's six hand-run actions (manifest moves from agent-write to ctl-write); **`ctl tdd-receipt`** replaces the inline-python receipt snippet; **`dynos hook <script>`** funnels helper scripts without `PYTHONPATH=` incantations.
- **Self-modification guard (H1):** agent roles (including executors) can no longer write the installed plugin directory, `~/.claude/plugins/`, `~/.claude/settings*.json`, or `~/.dynos/` — closing the "agent edits its own guardrails" hole. Developer mode (plugin source repo) exempts the plugin-root check only.
- **Prose↔policy contract test** (`tests/test_skill_prose_policy_contract.py`): extracts every fenced command block from the pipeline skills, runs the production destination extractor + `decide_write`, and fails CI on any prescribed-but-denied write, forbidden token (`/tmp`, `PYTHONPATH=`, `rm active-segment-role`, repo-relative hook calls), unregistered ctl subcommand, or read-only agent instructed to write files.
- **Investigator dossier-only enforcement (D7-3):** the pre-tool-use hook denies investigator Grep/Glob and restricts Read to `.dynos/investigations/` + dossier-referenced files; `triage.py finalize` validates structure + citations and persists report + markdown (the read-only agent now RETURNS its JSON). Classifier gains `ci-failure` / `config-error` types.

### Changed
- **Bash write detection is quote-aware (D5):** shlex tokenization with heredoc stripping replaces raw-string regexes — `>` inside quoted prompts/JSON no longer produces false write destinations; `/dev/null` and friends are recognized sinks. Legacy regexes remain only as the fail-closed fallback for unparseable commands.
- **Denials are self-explaining:** every write-policy denial names itself as a plugin guardrail (not a Claude Code permission), the role, the path, and the sanctioned alternative (wrapper command or `_scratch/`), with a degraded-actor diagnostic when relevant.
- **`run-execution-finish` verifies evidence inside the stage gate** (evidence files, `files_expected`, declared verifications); `run-execution-verify-evidence` is legal at EXECUTION *and* TEST_EXECUTION (it previously hard-errored after finish — the execute Step 5 dead-end).
- **`files_expected` supports directories (`src/pkg/`) and path-aware globs (`src/**/*.py`)** across the diff-membership check, evidence existence, ownership, and the cross-segment exclusivity rule (which got stricter: containment counts as overlap).
- **Validator errors are self-correcting:** enum violations name the valid set with did-you-mean hints (`'ci'` → `'infra'` — `ci` is intentionally NOT a domain; `'backend'` → `'backend-executor'`). Plan Reference-Code checks only slash-containing tokens and honors a "Files to be created" section.
- **Skill prose rewritten to the `bin/dynos` funnel:** all `python3 hooks/ctl.py` / `PYTHONPATH=...` invocations in the pipeline skills route through `"$DYNOS" ctl` / `"$DYNOS" hook`; a permissions-ON user can allow the single `<plugin-root>/bin/dynos` prefix once. `hooks.json` SessionStart matcher gains `resume` so the pin survives resumed sessions.
- Conformance fixes: `execute/SKILL.md`'s false "audit-* roles cannot be stamped" claim corrected; repair-coordinator (no Write/Bash) now returns its payload for the orchestrator to persist via stdin; security-auditor description no longer claims "Read-only" while prescribing its report write.

### Plugin / Distribution
- Bump `.claude-plugin/plugin.json` and `package.json` to `7.4.1`.

---

## [7.4.0] - 2026-05-05
### "Wider Drain": Residual Queue Captures More Sources

The 7.3.0 producer admitted only `claude-md-auditor` and `dead-code-auditor` non-blocking findings — narrow on purpose, but narrow enough that running the queue against itself excluded most of the actionable follow-ups it surfaced. 7.4.0 broadens the producer to three more auditors and adds a second source surface so postmortem prevention rules with concrete enforcement (test, lint, static-check, ci-gate, runtime-guard) land in the same queue rather than only `prevention-rules.json`.

### Added
- Producer now admits non-blocking findings from `security-auditor`, `performance-auditor`, and `code-quality-auditor` (in addition to `claude-md-auditor` and `dead-code-auditor`). Severity-`info` and category skip-list rules still apply.
- New `lib_residuals.ingest_prevention_rules(root, rules)` helper that ingests postmortem prevention rules with actionable enforcement (test / lint / static-check / ci-gate / runtime-guard) into the residual queue. Advisory and review-checklist rules are deliberately excluded.
- `postmortem_analysis.apply_analysis` now calls the new helper after writing prevention rules, so engineering-actionable rules surface as queue items alongside their write to `prevention-rules.json`.

### Plugin / Distribution
- Bump `package.json` and `.claude-plugin/plugin.json` to `7.4.0`.

---

## [7.3.0] - 2026-05-04
### "Residual Drain": Non-Blocking Findings Become an Overnight Backlog

The theme of 7.3.0 is giving non-blocking audit findings somewhere to go. Instead of disappearing into the audit report, advisory-grade findings now flow into a persistent backlog that can be inspected on demand and drained autonomously while you sleep.

### Added
- **Producer hook in audit-finish**: every audit cycle now appends its non-blocking findings to `proactive-findings.json`, so advisory-grade signals survive past the task they were found in.
- **`/dynos-work:residual` skill**: new user-facing skill with `list` and `run-next` subcommands for inspecting the residual queue and draining one item at a time.
- **Overnight drain pattern**: pair the residual skill with the `/loop` runner (`/loop 1h /dynos-work:residual run-next`) to autonomously chip away at the backlog between active sessions.

### Plugin / Distribution
- Bump `package.json` and `.claude-plugin/plugin.json` to `7.3.0`.

---

## [7.2.0] - 2026-04-21
### "Write Boundaries": Control-Plane Ownership, Wrapper Persistence, Task Diagnostics

The theme of 7.2.0 is putting a hard write boundary between LLM-authored work artifacts and framework-owned control-plane state. The runtime now refuses direct agent writes to control-plane files, moves hybrid artifacts behind deterministic ctl wrappers, and exposes per-task write-boundary diagnostics so violations are visible instead of implicit.

### Added
- **Central write policy**: new `hooks/write_policy.py` defines role-based write allowlists, control-plane classification, wrapper-required decisions, and structured `write_policy_allowed` / `write_policy_wrapper_required` / `write_policy_denied` events.
- **Wrapper persistence for hybrid artifacts**: `ctl.py` now owns deterministic persistence for `execution-graph.json`, `repair-log.json`, and `classification.json`, including normalization, validation, and atomic writes.
- **Per-task write-boundary diagnostics**: dashboard API and task detail UI now surface write-policy counts, top denied paths, top wrapper-required paths, and recent policy events.
- **Write-boundary enforcement tests**: added policy-matrix, wrapper, prompt-prose, and enforcement coverage for control-plane writes and router sidecar paths.
- **Spec and workflow docs**: added `docs/write-boundary-spec.md` plus supporting workflow and terminal-pipeline notes.

### Changed
- **Classification persistence moved behind ctl**: planner/start flows now write classification payloads through `write-classification` instead of mutating `manifest.json` directly.
- **Planner and repair prompts aligned to wrappers**: planning and audit/repair prose now instruct models to emit payloads to temp JSON and persist them only through ctl wrapper commands.
- **Executor ownership checks hardened**: patch/apply validation now rejects executor writes to protected task artifacts and reserves evidence/audit/control-plane paths by role.
- **Router sidecar writes are policy-checked**: injected prompt sidecars now go through write-policy validation instead of bypassing the new boundary.

### Fixed
- **Prompt drift no longer silently bypasses control-plane ownership** for `manifest.json`, `receipts/**`, `handoff-*.json`, `external-solution-gate.json`, wrapper-owned graph/repair-log/classification artifacts, and related task state.
- **Dashboard observability gap** around write-boundary denials and wrapper-required attempts.
- **Version metadata drift**: package and plugin release surfaces are now aligned on `7.2.0`.

### Plugin / Distribution
- Bump `package.json`, `.claude-plugin/plugin.json`, and `.claude-plugin/marketplace.json` to `7.2.0`.
- Bump `hooks/dashboard-ui/package.json` and `hooks/dashboard-ui/package-lock.json` to `0.2.0`.

## [7.1.0] - 2026-04-19
### "Close the Anti-Pattern": Receipt-Driven State Machine, Trust-Me-Bro Elimination, Modular Runtime

The theme of 7.1.0 is moving the state machine from pull-based (LLM decides when to advance) toward receipt-driven (scheduler advances when proofs are on disk), and closing the trust-me-bro anti-pattern across every control-plane surface. Receipt writers self-compute machine-derivable fields — callers can no longer supply counts, hashes, or scores. The contract version is bumped from 2 → 4 in two waves.

### Added
- **Receipt-driven scheduler POC**: new `hooks/scheduler.py` exports a pure `compute_next_stage(task_dir) -> (next_stage | None, list[missing_proofs])` and an I/O-capable `handle_receipt_written(...)` dispatcher. `write_receipt` in `lib_receipts.py` now synchronously invokes the scheduler after the atomic write, so writing the `human-approval-SPEC_REVIEW` receipt advances the task to `PLANNING` without any caller running `ctl.py transition`. Scope ceiling is explicit: `compute_next_stage` returns `(None, [])` for every non-SPEC_REVIEW stage. `<!-- scheduler-owned: X -> Y -->` marker in skill prose opts a transition out of the LLM-must-mention-the-ctl-command linter. New `receipt_scheduler_refused` writer + `scheduler_transition_refused` / `scheduler_transition_race` diagnostic events.
- **Deterministic rules engine** (#125): 6 prevention-rule templates (`pattern_must_not_appear`, `co_modification_required`, `signature_lock`, `caller_count_required`, `import_constant_only`, `every_name_in_X_satisfies_Y`, `advisory`) with structured params. Rules from postmortem analysis are normalized to these templates and enforced by `run_checks` at `REPAIR_EXECUTION → TEST_EXECUTION` / pre-DONE gates via `receipt_rules_check_passed`.
- **`rules-check-passed` receipt + transition gate**: error-severity violations refuse to write the pass receipt, blocking DONE transition. Prevention-rules file hash is pinned to the receipt; drift after the check forces a re-run.
- **LLM-powered postmortem analysis**: `postmortem_analysis.py build-prompt` / `apply` pipeline. `opus` analyst reads deterministic postmortem + findings and returns structured JSON with root_causes, prevention_rules, repair_failures. Rules merge into `prevention-rules.json` via the template normalizer. `postmortem_rule_promoted`, `postmortem_rule_dropped`, `postmortem_rule_promotion_dropped` events.
- **Hierarchical Q-learning**: per-category action spaces (3 Q-tables) replace the flat executor×model table. `ctl.py repair-plan` surfaces Q-derived assignments; `repair-update` writes outcomes back. No-op when learning is disabled.
- **Auditor ensemble voting**: router-plan entries can set `ensemble: true` with `ensemble_voting_models` (haiku + sonnet) and `ensemble_escalation_model` (opus). Both voters zero → pass; either voter non-zero → escalate to opus (binding). Per-model injected-prompt sidecars disambiguate ensemble call paths.
- **CI linters** (+5): `test_ci_event_emit_consume` (every `log_event` name must be consumed or in `DIAGNOSTIC_ONLY_EVENTS`), `test_ci_value_error_coverage` (every `raise ValueError` in production has an adversarial `pytest.raises(match=…)`), `test_ci_receipt_selfverify_parity` (every receipt writer self-verifies or is allowlisted), `test_skill_stage_references::test_no_skill_prose_advises_manual_stage_edit` (scans for `manually set` within 80 bytes of `"stage"` — catches DONE-escape-hatch regressions), `test_skill_stage_references::test_scheduler_owned_transitions_are_exempt_from_transition_prose_requirement` (caps scheduler-owned markers at `SPEC_REVIEW -> PLANNING`).
- **Plug-and-play modularity**: auditor registry, executor discovery, action spaces, ensemble wiring, handler discovery — all opt-in via registry lookups rather than hardcoded lists.
- **`bus` CLI subcommands** on `ctl.py`: `emit`, `drain`, `status`, `handlers`.
- **`router-cache-status`**: reports freshness of the per-task executor-plan cache (see Performance).
- **External-solution gate in start skill**: structured JSON artifact at `.dynos/task-{id}/external-solution-gate.json` captures when the planner consulted external docs and which candidate was chosen. Includes untrusted-content contract (paraphrase-not-quote, URL allowlist, instruction-shaped-content rejection, body caps).
- **Global dashboard live updates** (#121): regen-on-fetch + JS polling. Dashboard reflects current task state without manual refresh.
- **Stage-aware artifact validation**: `validate_task_artifacts` no longer false-fails on early stages (pre-spec, pre-plan) by conditioning required artifacts on current stage.
- **Contract version 4** (#127, #132): receipts embed `contract_version: 4`. `MIN_VERSION_PER_STEP` enforces per-receipt minimums. Writers refuse to emit below-floor; readers refuse to consume below-floor.

### Changed
- **Receipt writers self-compute machine-derivable fields** (#123, #127, #130, #131, #132, #133): `receipt_retrospective`, `receipt_plan_validated`, `receipt_plan_audit`, `receipt_audit_done`, `receipt_postmortem_generated` no longer accept caller-supplied `quality_score`, `cost_score`, `efficiency_score`, `total_tokens`, `segment_count`, `criteria_coverage`, `validation_passed`, or `finding_count`. The writers re-read the artifacts themselves and raise `TypeError` on any legacy kwarg. Closes the trust-me-bro input-trust hole.
- **`receipt_audit_done` TOCTOU fixed** (#133 / MA-005): when `report_path is None`, `finding_count` and `blocking_count` MUST both be zero. Auditors with real findings materialize a report file, and the writer re-reads and cross-checks.
- **`receipt_planner_spawn` enforces sidecar**: `injected_prompt_sha256` is required (no-sidecar legacy path removed). The sidecar file at `receipts/_injected-planner-prompts/{phase}.{sha256,txt}` pins the exact prompt bytes.
- **`receipt_executor_done` enforces sidecar**: per-segment injected prompt sidecar must exist and match the supplied digest.
- **`write_receipt` chokepoint dispatch**: every receipt write triggers `scheduler.handle_receipt_written` after the atomic write + existing `log_event("receipt_written", ...)`. Exception in the scheduler dispatch is swallowed to stderr — never rolls back the durable receipt.
- **`cmd_approve_stage` stops calling `transition_task`**: exits 0 after the receipt is durably on disk; the scheduler drives the resulting stage advance asynchronously in-process. Docstring updated to match.
- **Post-completion drain is async** (#117): `task-completed` handler no longer blocks the execute skill's return. Improve / policy_engine / dashboard / registry refresh run after the skill completes.
- **`evolve.py` → `calibrate.py`**: rename. The shell alias `dynos calibration` routes to `memory/agent_generator.py auto`. Old `hooks/calibrate.py` / `hooks/generate.py` wrappers removed; functionality lives in `agent_generator.py init-registry` / `register-agent` / `auto` subcommands.
- **`patterns.py` → `policy_engine.py`**: rename. `patterns.md` table data split into JSON (`project_rules.json`) so the LLM-facing prose (`project_rules.md`) no longer drifts against the structured data.
- **Eventbus flattened**: 4 handlers, 1 event (`task-completed`), 1 drain loop. `receipt_written` and `stage_transition` remain as orthogonal JSONL-only observability events.
- **`agent_generator` refreshes existing learned agents** (#115): every run regenerates agent `.md` files from the latest retrospective patterns, not just new ones. Bias / format / rule-filtering fixes.
- **Benchmark scheduler wired as auto-discovered eventbus handler**.
- **Daemon stripped to trajectory-only**: removed duplicate seeding; calibration CLI added; dormant modules cleared.
- **Test migration** (#108): 7 unittest files → pytest; 3 dyno-prefixed tests renamed.

### Performance / Determinism
- **Router executor-plan cache** (#113, #114): `executor-plan` writes a fingerprinted plan to `.dynos/task-{id}/router-cache/executor-plan.json`. Per-segment `inject-prompt` calls reuse the cached plan instead of rebuilding it. Critically, this eliminates re-rolled epsilon-greedy exploration dice between executor-plan and inject-prompt, so the model the executor was spawned under matches the model the prompt was injected for. Cache fingerprint covers graph, policy, effectiveness scores, retrospectives, learned registry, benchmark history, and prevention rules — any drift forces a live rebuild. New `router-cache-status` subcommand. New `router_cache_lookup` / `router_cache_write` events.
- **`_benchmark_model_for_agent` honors `RouterContext`**: helper no longer re-reads learned registry and benchmark history when called from a path that already has both on its context.
- **Gap analysis cached + decoupled** (#119): `plan_gap_analysis` no longer runs inside `validate_task_artifacts`; callers invoke it explicitly. Handler work deduplicated.
- **Worktree slug normalization** (#122): `~/.dynos/projects/{slug}/` derived from the main-repo path, not the worktree path — retrospectives, benchmarks, and learned agents now flow back to the same persistent dir regardless of which worktree executed the task. Safe `migrate` CLI consolidates legacy duplicate slugs.

### Fixed
- **27 silently-vacuous gates** (#132): receipt/transition gates that checked the wrong field or returned True on empty input. Contract v4 adds explicit non-empty constraints.
- **10 remaining trust-me-bro LLM surfaces** (#127): contract v3 closes caller-supplied counts on auditor receipts and plan receipts.
- **8 initial trust-me-bro control-plane gates** (#123): sha256-bound sidecars, hash-drift refusal, explicit kwarg requirements.
- **6 framework enforcement gaps** (#126) + 4 non-blocking followups (#128): stage-transition + receipt-parity + auditor-registry cross-checks.
- **G1–G4 postmortem-skip structural guardrails** (#130): `.dynos/deferred-findings.json` TTL registry + `check_deferred_findings` CLI. Removed `quality-above-threshold` from `_POSTMORTEM_SKIP_REASONS`.
- **Postmortem rule-promotion parity** (#131): `postmortem_rule_promoted` event fires per rule added; `postmortem_rule_dropped` fires per rule dropped. Closes the PR-#130 silent-drop pattern.
- **MA-005 / MA-007 meta-audit** (#133): `receipt_audit_done` rejects `report_path=None` with non-zero counts; `apply_analysis` emits `postmortem_rule_promoted` per rule on successful promotion (was silent).
- **Three pipeline regressions** (#116): registry data loss, fast-track stage walk, post-completion receipt gap.
- **`performance_check`** (#111, #112): stopped flagging `dict.get()` as N+1 queries; indent-stack-based quadratic-loop detection.
- **Execute skill stage transitions** (#117): post-completion drain dispatched async so `/dynos-work:execute` returns promptly.
- **Dead code removal** (#109): `cli/assets/hooks`, `cli/assets/memory`, `cli/assets/bin`, `cli/assets/telemetry` subtrees; dormant sandbox modules (`sweeper`, `dream`, `state`, `founder-skill`, stale copies).

### Removed
- **`no-sidecar legacy path` in `receipt_planner_spawn`**: the pre-#124 `injected_prompt_sha256=None` branch is gone.
- **Caller-supplied score / count fields** on `receipt_retrospective` / `receipt_plan_validated` / `receipt_plan_audit` / `receipt_audit_done` (also listed under Changed for breaking-change discoverability — legacy kwargs raise `TypeError`).
- **`quality-above-threshold`** as a valid value in `_POSTMORTEM_SKIP_REASONS` (#130).
- **`hooks/calibrate.py`** / **`hooks/generate.py`** shell wrappers: functionality lives in `memory/agent_generator.py`.
- **MCTS / dreamer modules**: extracted to `sandbox/` — out of the main pipeline.
- **Optional learning modules**: sandboxed; `memory/` is now 1,318 lines. Q-learning / postmortem_improve / agent_generator restored from sandbox when needed.

### Plugin / Distribution
- Bump `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` to `7.1.0`.
- Reinstall via `/plugin update dynos-work` to pick up updated skills. Cached-v7.0.0 installations will continue to serve stale skill prompts (notably a stale 5-arg `receipt_retrospective` signature from an intermediate commit) until the update runs.

---

## [7.0.0] - 2026-04-16
### "Verified Foundry": Tool-Grounded Verification, Compliance, Least Privilege

The foundry moves from LLM-reviewing-LLM to deterministic-tool-checking-LLM across every verification surface. Autofix is extracted to a separate repository. The learning layer becomes explicitly optional.

### Added
- **Compliance auditing**: security-auditor flags GPL/AGPL deps, generates SBOM via cyclonedx-bom/syft, verifies dependency provenance via Sigstore, checks for missing privacy code (data export, account deletion). New `compliance` category with `comp-` prefix.
- **Conditional plan sections**: `## API Contracts` required when domains include backend/ui/security; `## Data Model` required when domains include db.
- **Plan gap analysis**: deterministic hook cross-references API Contracts and Data Model tables against actual route definitions (10+ frameworks) and model/schema definitions (8+ ORMs). Plans can't claim endpoints or tables that don't exist.
- **Doc-accuracy auditing**: code-quality-auditor runs `validate_docs_accuracy.py` on tasks touching `.md` files.
- **Per-agent tool boundaries**: all 17 agents declare minimum tool sets in YAML frontmatter. Auditors cannot Write/Edit. Planners cannot Bash.
- **LEARNING_ENABLED policy flag**: `dynos config set learning_enabled false` disables the entire learning layer. Foundry-only mode is now a single config flip.
- **Phase-labeled start.md**: 6 phase headers make structure visible. Zero behavior change.
- **DORA metrics**: `lead_time_seconds`, `change_failure`, `recovery_time_seconds` in retrospectives. `dynos stats dora` aggregator.
- **Usage telemetry**: module-level dormancy detection. `dynos stats usage` CLI.
- **`dynos config` CLI**: get/set project policy without hand-editing JSON.
- **`hooks/compliance_check.py`**: license scanning, SBOM, Sigstore provenance, privacy checks across 10 ecosystems.
- **`hooks/plan_gap_analysis.py`**: deterministic plan verification against codebase.
- **`hooks/lib_usage_telemetry.py`**: append-only JSONL telemetry for dormancy detection.

### Removed
- **Autofix system** (6,327 LOC): `hooks/proactive.py` (3,592 lines), `skills/autofix/`, 5 test files, all autofix constants, Q-table functions, daemon integration, `--autofix` CLI flags. Extracted to `dynos-fit/autofix`.
- `DYNOS_AUTOFIX_WORKTREE` environment variable.
- `"autofix"` from `VALID_PIPELINES`.

### Changed
- `security-auditor` covers `security` + `compliance` categories.
- `code-quality-auditor` covers `code-quality` + `doc-accuracy` categories.
- `repair-coordinator` routes compliance and doc-accuracy findings.
- Audit report schema gains optional `category` field.
- `validate_task_artifacts` enforces conditional headings + gap analysis.
- `compute_reward` includes DORA fields.
- Router gated on `is_learning_enabled()`.
- Event bus skips learning handlers when disabled.
- `maintain.py` stripped of all autofix code; daemon preserved for non-autofix maintenance.
- `start/SKILL.md` gains phase labels; trajectory/learned skill injection gated on learning flag.
- README rewritten (no autofix, DORA metrics, least privilege, tool-grounded verification).

---

## [6.0.0] - 2026-04-03
### "Runtime Control Plane": Deterministic Foundry, Live Dashboard, Maintainer Daemon

This release turns the plugin from a primarily prompt-defined foundry into a runtime-backed adaptive control system. The workflow remains human-directed, but key guarantees now live in code: artifact validation, route gating, benchmark-driven promotion, freshness blocking, lineage, and persistent maintenance automation.

### Added
- Deterministic control runtime:
  - `hooks/dynoslib.py`
  - `hooks/dynosctl.py`
  - `hooks/validate_task_artifacts.py`
- Task artifact enforcement for:
  - manifest validation
  - spec/plan structure
  - execution-graph coverage, ownership, and cycle detection
  - repair-log and retrospective validation
- RL-inspired adaptive runtime components:
  - `hooks/dynostate.py`
  - `hooks/dynostrajectory.py`
  - `hooks/dynosdream.py`
- Learned component lifecycle tooling:
  - `hooks/dynoevolve.py`
  - `hooks/dynoeval.py`
  - `hooks/dynogenerate.py`
  - `hooks/dynofixture.py`
- Benchmark and rollout harnesses:
  - `hooks/dynobench.py`
  - `hooks/dynorollout.py`
  - `hooks/dynochallenge.py`
- Live routing and automation:
  - `hooks/dynoroute.py`
  - `hooks/dynoauto.py`
- Observability and traceability:
  - `hooks/dynoreport.py`
  - `hooks/dynolineage.py`
  - `hooks/dynodashboard.py`
- Persistent maintainer runtime:
  - `hooks/dynomaintain.py`
  - background daemon mode
  - manual invoke mode
  - maintenance status and PID tracking
- New contributor and internals docs:
  - `UNDER_THE_HOOD.md`
  - `ARCHITECTURE.md`
- Automated live dashboard generation and refresh from hooks
- Fixture synthesis from completed task retrospectives
- Benchmark index and lineage graph for task -> component -> fixture -> run traceability

### Changed
- README rewritten from scratch to be user-facing instead of runtime-internal
- Founder mode retained as an advisory design-review layer inside the start flow
- Learned routing now resolves from the live registry, not markdown tables
- Promotion and rollback now depend on benchmark evidence plus must-pass category checks
- Route resolution now blocks stale learned components by freshness policy
- TaskCompleted hook now runs learn, automation, and dashboard refresh automatically
- SessionStart hook now refreshes dashboard state and can ensure the maintainer daemon is running
- `/dynos-work:maintain` is now a clearer user-facing manual maintainer path

### Fixed
- Eliminated several documentation/runtime mismatches where skills described behavior that was not enforced in code
- Added regression tests for route resolution, auto benchmarking, fixture synthesis, lineage, dashboard generation, challenger rollout, and maintainer cycles

### Security
- Learned components can no longer silently remain active after benchmark regression or staleness
- Promotion remains blocked unless challenger evidence clears configured policy thresholds


## [5.0.0] - 2026-04-03
### "Foundry Intelligence": Decision Transformer Architecture

The system evolves from a self-learning platform into a **Trajectory-Driven, Human-Directed Autonomous Software Foundry** with a full Decision Transformer (DT) memory layer.

### Added
- **`trajectory` Skill:** Manages State-Action-Reward (SAR) sequences. Reconstructs the full trajectory of every task and stores it in `.dynos/trajectories.json` for future retrieval.
- **`state-encoder` Agent:** Produces a structured State Signature ($) from a module's AST, Dependency Graph, and Finding Entropy. Powers the DT retrieval step.
- **DT-Informed Discovery:** The `start` skill now spawns the `state-encoder` at the beginning of every task and retrieves the 3 most similar successful past trajectories. Discovery questions are generated from known failure points in those trajectories.
- **MCTS/Dreaming as a Consultant Service:** The `founder` skill is refactored from an independent entry point into a strategic simulation service. It runs Sandbox Playouts for hard/critical design options and returns a **Design Certificate** (PASS/FAIL, Security Score, Performance Metrics, Recommendation) before the user chooses.
- **Unified Foundry Start:** Merged the old "Phase 0 Founder Mode" shortcut into the Standard Discovery pipeline. Every task — regardless of prompt size — now follows the full pipeline ending in mandatory human-approval gates.

### Changed
- **`start` Skill:** Removed the "Phase 0" bypass path. All tasks now begin with RL-informed discovery (Step 1), MCTS Dreaming for hard/critical subtasks (Step 2), and mandatory Spec Review + Plan Review gates for all tasks.
- **`founder` Skill:** Demoted from independent entry point to a strategic "Dreaming Engine." Its MCTS and Sandbox Simulation logic is now called as a service by `start` for design option vetting only.
- **Spec Review & Plan Review Gates:** Now explicitly enforced as Hard Rules for every task. No skip paths exist.

### Security
- **Zero-Trust Founding:** Founder's sandbox simulations are now audited by an Opus-level auditor before Design Certificates are issued to the user.

---

## [4.0.0] - 2026-04-03
### "God-Mode" Evolution: The Autonomous Software Foundry

This is a major architectural overhaul, transforming the system from a task-based plugin into a **Self-Learning, High-Performance Autonomous Engineering Platform.**

### Added
- **`founder` Skill:** A new strategic entry point for minimal prompts. Uses **MCTS** and **Dreaming** (Sandbox Simulation) to bootstrap full production-grade systems.
- **`maintain` Skill:** The Autonomous Backend worker. Performs proactive self-audits, identifies technical debt clusters, and opens automated PRs.
- **`evolve` Skill:** Manages agent lifecycle through **Shadow Mode** and **Simulation Benchmarking** at `/tmp`.
- **Ensemble Voting:** High-risk audits now utilize a multi-model consensus (Haiku/Sonnet) before escalating to Opus for 58% cost efficiency.
- **Critical Path Scheduler:** An optimized execution engine that prioritizes the most "blocking" segments and runs audits in parallel with code implementation.
- **Incremental Caching:** Skips executor spawns for unchanged segments, reducing token costs by up to 80% on re-runs.
- **Web Dashboard:** A premium, visual Engineering Control Center (`.dynos/dashboard.html`) with finding density heatmaps and ROI charts.
- **RAG-Lite Docs Refresh:** Automated fetching of official library documentation during the learning phase.
- **Global Pattern Syncing:** Cross-project intelligence sharing via external memory paths.

### Changed
- **Planning Agent:** Upgraded to **Hierarchical Planning** (Master/Worker) to handle high-complexity tasks.
- **`start` Skill:** Integrated **Founder Mode** (Phase 0) and strategic interrogation loops.
- **`execute` Skill:** Implemented **Progressive Pipelining** for real-time background auditing.
- **`learn` Skill:** Decoupled from agent management; now focused on high-density pattern extraction and "Human Insight" gates.
- **Model Policies:** Transitioned to dynamic, EMA-based model routing (ROI tracking).

### Security
- **Multi-Modal Visual Audit:** Integrated browser-based vision checks for UI-domain tasks.
- **Simulation Isolation:** All candidate agents are now verified in a **Security Sandbox** at `/tmp` before promotion.
- **Downtime Shield:** Automated auto-merge policy is strictly prohibited from merging if any tests are failing.

---

## [3.0.0] - 2026-04-03

### Added
- Learned agents system: dynos-work generates project-specific executors and auditors that improve over time
- Agent generation in learn step: analyzes codebase patterns and repair history, rate-limited to every 3 tasks, passive (no extra spawns)
- Agent Routing table in `dynos_patterns.md`: routes to learned agents when they outperform generics
- Alongside/replace mode for learned auditors: both run for 3-task proving window, then replace when proven
- Self-pruning: learned agents soft-deleted to `.archive/` after 3 consecutive tasks of underperformance
- EMA quad key: (role, model, task_type, source) tracks generic vs learned agent effectiveness separately
- Finding-overlap tracking for alongside auditor evaluation
- Path validation on learned agent file reads (defense-in-depth)
- Sanitization on agent generation instructions (prompt injection prevention)
- Security-auditor replace protection (can never be replaced by learned agent)
- Priority-stack composite weights: 0.6 quality + 0.25 efficiency + 0.15 cost

### Changed
- `skills/learn/SKILL.md` gains Steps 6-9: agent generation, routing table, pruning, mode transitions
- `skills/execute/SKILL.md` reads Agent Routing table, routes to learned executors when composite is higher
- `skills/audit/SKILL.md` reads Agent Routing table, supports alongside/replace mode, tracks agent_source and finding overlap in retrospective
- EMA effectiveness tracking extended from triple to quad key

---

## [2.12.0] - 2026-04-03

### Added
- `/dynos-work:dry-run` skill with declarative contract validation: each pipeline skill has a `contract.json` sidecar declaring input/output schemas; dry-run validates the chain
- `/dynos-work:dashboard` skill: hybrid data source rendering policy state from `dynos_patterns.md` plus time-series trends (token cost, model distribution, quality score) from retrospectives
- UCB exploration bonus in model selection: `ucb_score = composite + 0.5 * sqrt(ln(total) / sample_count)` replaces pure composite, naturally exploring under-observed model/role/task_type triples
- Prevention rule aging with FIFO eviction: rules carry `created_task_id`, oldest evicted first at 15-rule cap, 3-task eviction exemption prevents bulk flushing
- Baseline Policy reconstruction from best 3-task quality window when no baseline exists
- TaskCompleted hook activation: auto-triggers learn after task completion, conditional auto-commit gated on `dynos_auto_commit` in project settings.json
- 12 `contract.json` sidecar files across all skill directories
- Rule sanitization in learn skill to prevent prompt injection via finding descriptions

### Changed
- Token tracking extraction now specifies exact field path (`total_tokens` from Agent tool usage summary)
- `classification.type` / `task_type` naming normalized with explicit mapping notes across audit, execute, and repair-coordinator
- Auto-commit uses `git add -u` (tracked files only) instead of `git add -A` to prevent staging secrets
- Prevention rules table gains `Created` column for age tracking

### Removed
- `agents/execution-coordinator.md` (orphaned, functionality absorbed by planning agent)

---

## [2.11.0] - 2026-04-02

### Added
- Actor-critic inspired adaptive model selection: EMA effectiveness scores tracked per (role, model, task_type) triple
- Model Policy table in `dynos_patterns.md`: recommends optimal model for each agent based on observed quality, cost, and efficiency
- Skip Policy table: learned skip thresholds per auditor replace hardcoded value of 3
- Effectiveness Scores table: raw EMA data driving policy derivation
- Baseline Policy: rolling snapshot for regression detection and revert
- Reward vector computation in reflect step: quality_score, cost_score, efficiency_score
- Real token tracking: `token_usage_by_agent`, `total_token_usage`, `model_used_by_agent` in retrospective
- Meta-validator: bounds checking, monotonicity constraints (security-auditor always Opus), regression detection with rolling baseline blend-back
- Cold-start gate: hardcoded defaults for first 5 tasks, then adaptive
- Policy readers in audit skill, execute skill, and repair-coordinator with fallback to defaults
- Per-task-type baseline token budgets for cost_score (feature: 50k, refactor: 30k, bugfix: 20k, other: 40k)

### Changed
- `skills/learn/SKILL.md` gains Step 5 (Policy Update) with EMA, policy derivation, meta-validation, and baseline management
- `skills/audit/SKILL.md` Step 3 reads Skip Policy and Model Policy; Step 5 Reflect captures tokens and computes reward vector
- `skills/execute/SKILL.md` Step 3 reads Model Policy for executor model selection
- `agents/repair-coordinator.md` reads Model Policy for retry 0-1 findings; retry >= 2 always Opus (non-negotiable)
- `dynos_patterns.md` gains four new sections: Model Policy, Skip Policy, Effectiveness Scores, Baseline Policy
- README updated with adaptive model selection and actor-critic self-improvement details

---

## [2.10.0] - 2026-04-02

### Added
- Eager two-phase repair: repair starts on first auditor findings (phase 1) while slower auditors continue running; late results feed into phase 2
- Short-circuit on critical spec failure: spec-completion-auditor critical findings trigger immediate phase 1 repair
- Parallel repair batch execution: non-overlapping batches run concurrently, only file-conflicting batches are serialized
- Model escalation on retry: findings failing twice (retry >= 2) automatically upgrade executor to Opus
- Auditor skip on zero-finding streak: skip-eligible auditors (dead-code, ui, db-schema) auto-skipped after 3+ consecutive zero-finding tasks
- Per-auditor zero-finding streak tracking in task retrospective (replaces old single-integer field)
- Late-finding conflict resolution: all late auditor findings queue for phase 2, no interruption of in-progress repairs
- Cross-phase retry continuity: max_retries (3) applies across both phases combined per finding

### Changed
- `skills/audit/SKILL.md` Steps 3, 4, 5 rewritten for two-phase pipeline
- `agents/repair-coordinator.md` updated with phase awareness, `model_override` field, and cross-phase retry rules
- `auditor_zero_finding_streak` (integer) replaced by `auditor_zero_finding_streaks` (object map) in retrospective schema
- Repair-coordinator now sets `parallel` field on batches and `model_override` on escalated tasks
- README updated with new sync optimization details

---

## [2.9.0] - 2026-03-30

### Added
- Self-improving token efficiency with prevention rules injected into executor spawn instructions
- Incremental re-audit scoping: re-audit after repair inspects only repair-modified files
- Spec-completion auditor retains full scope during re-audit for overall requirement coverage
- Prevention rules from `dynos_patterns.md` filtered by executor type and injected at spawn time

### Changed
- Audit skill reflect step computes spawn efficiency metrics (subagent_spawn_count, wasted_spawns, zero-finding streaks)

---

## [2.8.0] - 2026-03-30

### Added
- Debug skill and forensic investigator agent for deep root cause analysis
- Token consumption optimizations across agents, skills, and hooks
- Workflow optimization reducing subagent spawns by ~22%

### Changed
- Removed non-Claude platform configs (Cursor, Gemini CLI, OpenCode, Codex)
- Session hook and config files updated for v2.8.0

---

Versions prior to 2.8.0 predate this changelog.

[7.2.0]: https://github.com/dynos-fit/dynos-work/compare/v7.1.0...v7.2.0
[7.1.0]: https://github.com/dynos-fit/dynos-work/compare/v7.0.0...v7.1.0
[5.0.0]: https://github.com/dynos-fit/dynos-work/compare/v4.0.0...v5.0.0
[4.0.0]: https://github.com/dynos-fit/dynos-work/compare/v3.0.0...v4.0.0
[3.0.0]: https://github.com/dynos-fit/dynos-work/compare/v2.12.0...v3.0.0
[2.12.0]: https://github.com/dynos-fit/dynos-work/compare/v2.11.0...v2.12.0
[2.11.0]: https://github.com/dynos-fit/dynos-work/compare/v2.10.0...v2.11.0
[2.10.0]: https://github.com/dynos-fit/dynos-work/compare/v2.9.0...v2.10.0
[2.9.0]: https://github.com/dynos-fit/dynos-work/compare/v2.8.0...v2.9.0
[2.8.0]: https://github.com/dynos-fit/dynos-work/compare/v2.7.0...v2.8.0
[7.0.0]: https://github.com/dynos-fit/dynos-work/compare/v6.0.0...v7.0.0
[6.0.0]: https://github.com/dynos-fit/dynos-work/compare/v5.0.0...v6.0.0
