---
name: founder
description: "Internal dynos-work skill. Foundry strategic design-review service. Uses sandbox simulations and targeted checks to vet high-risk architecture options before human approval. Runs only when the user explicitly types /dynos-work:founder; never auto-triggered from conversation."
---

# dynos-work: Founder (Design Review Engine)

The Founder is the design-review service for the Foundry. It uses sandbox trials to compare high-risk options and returns evidence for a human decision.

If available in this repo, the deterministic runtime for this skill is:

```text
python3 hooks/dynostate.py --root . --target <path>
python3 hooks/dynosdream.py design-options.json --root .
```

## **Service 1 — Strategic Interrogation (Insight Generation)**

When called by `skills/start/SKILL.md` during discovery:
1.  **Trajectory Search**: If available, use `dynostate.py` to produce the current state signature and `dynostrajectory.py search` to retrieve a small set of similar past tasks from `trajectories.json`.
2.  **Gaps Search**: Identify the uncertain parts of the prompt based on current repo context first, then use past failures only as advisory prompts.
3.  **Insight Output**: Provide the `start` skill with 3-5 targeted questions that will uncover hidden complexity.

## **Service 2 — Sandbox Architecture Playouts**

When called to vet a **High-Risk Design Option**:
1.  **The Sandbox**: Initialize a simulation environment at `/tmp/dynos-dream-{id}/`.
2.  **Playout**: Use `dynosdream.py` to run deterministic MCTS-lite playouts across the provided design options.
3.  **Autonomous Audit**: Score each option for complexity, maintainability, security exposure, and trajectory support.
4.  **Comparison Output**: Record the strengths, weaknesses, and concrete failure modes observed in simulation.

## **Service 3 — The Design Certificate**

For every vetted option, output a **Design Certificate** to be presented in the `start` skill's design-decisions gate:
*   **Result**: PASS/FAIL.
*   **Performance Metrics**: Instruction density, cyclomatic complexity, and dependency breadth.
*   **Security Score**: Number of findings generated in the sandbox.
*   **Recommendation**: "Preferred", "Acceptable", or "High Risk."

## **Hard Rules**

- **Supportive, Not Bypassing**: The Founder never bypasses the Spec Review. It provides the **evidence** the user needs to approve the spec.
- **Evidence-First**: Never recommend an architecture in the design phase unless a sandbox playout has been completed.
- **History Is Advisory**: Past trajectories may inform what to inspect first, but the current repo and current task always win.
- **Structured Output Only**: Design review must end in machine-readable Design Certificates, not free-form prose only.
