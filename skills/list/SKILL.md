---
name: list
description: "Internal dynos-work skill. List all registered dynos-work projects. Runs only when the user explicitly types /dynos-work:list; never auto-triggered from conversation."
---

# dynos-work: List

Show all projects registered with dynos-work.

## Ruthlessness Standard

- Do not invent project status or last-active data.
- Print what the registry actually returns.
- If the registry read fails, report that failure instead of showing a guessed empty list.

## Usage

```
/dynos-work:list
```

## What you do

```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/registry.py" list
```

Print the results in a human-readable format showing: project path, status (active/paused/archived), last active timestamp.
