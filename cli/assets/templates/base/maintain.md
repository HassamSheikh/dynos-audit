---
name: maintain
description: "Internal dynos-work skill. Autonomous maintenance worker. Scans the repo for technical debt, security anti-patterns, and architectural drift; on a finding it creates a repair branch, implements the fix, verifies with audits/tests, and opens a Pull Request. Runs only when the user explicitly types /dynos-work:maintain; never auto-triggered from conversation."
---

# dynos-work: Maintain (The Autonomous Backend)

Maintains the long-term health of the repository by proactively identifying and resolving issues before they become blocking findings.

## What you do

### Step 1 -- Autonomous Debt Polling (The Trigger)

Every 24 hours (or when invoked), the system performs a **Background Meta-Audit**:
1. Scan the codebase using the **Proactive Meta-Auditor**.
2. Focus on:
   - Dependency vulnerabilities.
   - Architectural drift (deviations from current "Gold Standards").
   - Recurring finding categories from the last 10 tasks.
   - Code-smell clusters identified by complexity metrics.

### Step 2 -- Severity Threshold Gate

For each discovered finding, determine if it meets the **Autonomous Fix Threshold**:
- **Critical (e.g. Auth/Infra):** Immediately proceed to Step 3.
- **High (e.g. Security/Performance):** Proceed to Step 3.
- **Medium/Low:** Append to `.dynos/proactive-findings.json` and wait for the next manual task.

### Step 3 -- The Autonomous Fix Pipeline

If a finding meets the threshold, the system starts an **Auto-Task**:
1. **Branching:** Create a branch `dynos/auto-fix-{finding-id}`.
2. **Implementation:** Use a specialized **Refactor Executor** to implement the fix based on the current "Gold Standard" patterns.
3. **Verification:**
   - Run the full test suite.
   - Spawn a **Security Auditor** (Opus) to verify the fix.
4. **Failure Recovery:** If audits/tests fail, discard the branch and log the failure to `.dynos/maintenance-log.json`. Do not retry more than once.

### Step 4 -- The Hand-off (Pull Request)

If the fix passes ALL audits and tests:
1. **PR Creation:** Open a Pull Request (via `gh pr create` or similar).
2. **Description:** Automatically generate a detailed PR description including:
   - The original finding from the meta-audit.
   - The "Gold Standard" pattern it followed.
   - The audit-pass certificate.
3. **Notify User:** Print:
   ```
   {timestamp} [MAINTAIN] Autonomous PR created: {pr-url} -- fixed {finding-description}
   ```

### Step 5 -- Architectural Strategy Proposals (The Strategist)

If the **Meta-Auditor** identifies a "Debt Cluster" (a module with > 20% of the project's historical findings):

1. **Strategic Refactor Design:** Instead of fixing individual bugs, the maintainer spawns a **Lead Architect (Opus)** to design a clean-slate refactor of the entire module.
2. **Simulation Sandbox Test:**
   - Implement the refactored version in the **Autonomous Simulation Sandbox**.
   - Compare the Quality Score and Audit Pass rate of the *old* logic vs the *new* proposed logic.
3. **The Strategic PR:** If the new version is significantly cleaner/safer, open an **Architectural Proposal PR**. 
   - Report the "Architecture Score" improvement.
   - Include a visual breakdown of the new module's hierarchy.

### Step 6 -- Autonomous Task Discovery (The Manager)

In addition to technical debt, the maintainer now polls your **Issue Trackers** (e.g. `gh issue list` or Jira API):

1. **Bug Detection:** Identify new issues labeled `priority:high` or `bug`.
2. **Auto-Assignment:** If a bug is within the maintainer's domain (Backend/Infra), it autonomously triggers a **Task Start**.
3. **Draft PR:** It builds the fix and opens a Draft PR with the title `[AUTO] Fix for Issue #{id}`.

### Step 7 -- The Zero-Input Auto-Merge Policy

To achieve a 100% "Set-and-Forget" backend, the maintainer can **Self-Merge** specific refactors:

1. **Eligibility:** Only "Technical Debt Refactors" (discovered via meta-audit) that touch < 5 files.
2. **The 1.0 Quality Bar:**
   - Must pass all Unit and Integration tests.
   - Must achieve a **1.0 Quality Score** and **1.0 Security Score** from the Opus Ensemble auditors.
3. **The Final Action:** If the above are met, the maintainer merges the branch directly into `main` and sends a notification.

### Step 8 -- The Architectural Tournament (Recursive Benchmarking)

To ensure the "Gold Standard" is perpetually optimized, the maintainer performs a **Search-Space Exploration**:

1. **Variant Generation:** For a high-debt module, the maintainer drafts 3 **Architectural Branches** in the sandbox (e.g. `Variant A: Functional`, `Variant B: OOP`, `Variant C: Utility-driven`).
2. **Playout Simulations:**
   - **Performance:** instruction count, cyclomatic complexity check.
   - **ROI:** token-cost of the implementation vs the potential debt reduction.
   - **Quality:** composite finding-density score.
3. **Winner Selection:** The variant with the highest **ROI/Efficiency Score** is selected as the **"Candidate for Push"**.
4. **Final Proposal:** Present the user with the "Winner"'s data and the two rejected alternatives for comparison.

## Hard Rules
- **Winner-Take-All:** Never present a refactor proposal unless a **Simulation Benchmark** has been completed for at least two alternate branches.
- **ROI-First:** If a refactor's token-cost is > 50% of the anticipated maintenance savings over a 12-month horizon (estimated), abort the tournament ($ \mathcal{L} = \frac{\Delta \text{Debt}}{\text{Cost}} $).
- **Safe-Merging:** Never auto-merge a "Tournament Winner" unless the human operator has explicitly verified the architectural philosophy (via human insight gate).
