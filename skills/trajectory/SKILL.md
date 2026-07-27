---
name: trajectory
description: "Internal dynos-work skill. Sequence memory manager. Stores compact task traces and retrieves similar prior tasks to inform discovery and design review. Runs only when the user explicitly types /dynos-work:trajectory; never auto-triggered from conversation."
---

# dynos-work: Trajectory (Sequence Memory)

This is the memory layer for prior task traces. It is retrieval support, not an autonomous policy engine.

## Ruthlessness Standard

- Retrieved history is evidence of recurrence, not permission to cargo-cult old solutions.
- If current code contradicts memory, trust the repo and call the memory mismatch out.
- Do not store or retrieve traces so loosely that they become decorative noise.

If available in this repo, the deterministic runtime for this skill is:

```text
python3 hooks/trajectory.py rebuild --root .
python3 hooks/trajectory.py search query.json --root . --limit 3
```

## What you do

### Step 1 -- Task Trace Extraction (PUSH)
Immediately after a successful task completion and `/dynos-work:learn`:
1. **Trace Reconstruction:** Reconstruct the sequence of (State, Action, Reward) for the entire task.
2. **Update Registry:** Push the sequence into the versioned store at `.dynos/trajectories.json`.
3. **Outcome Summary:** Record enough metadata to help future retrieval find similar successful tasks.

### Step 2 -- Behavioral Retrieval (PULL)
During the "Founder Phase" or "Planning Phase":
1. **State Signature Search:** Call the `state-encoder` to get the current module's state signature ($).
2. **Trajectory Search:** Use the deterministic similarity search in `hooks/trajectory.py` to find the 3 most similar successful trajectories in `trajectories.json`.

### Step 3 -- Advisory Use
When a similar prior task is found:
1. Use it to surface likely failure modes, test gaps, and architecture risks.
2. Never copy its implementation plan or decision path blindly.
3. If current repo evidence conflicts with retrieved history, trust current repo evidence.

## Hard Rules
- **Trajectory Integrity:** Never modify a past trajectory; they are immutable ground truth.
- **Anonymization:** Strip all local paths and IDs from the trajectories before they are indexed for search.
- **Advisory Only:** Retrieval may influence prioritization, but it never overrides hard validation or human approval.
- **Schema First:** Trajectories must be written in the versioned store format. Do not append ad hoc JSON blobs.
