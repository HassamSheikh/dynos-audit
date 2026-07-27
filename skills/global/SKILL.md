---
name: global
description: "Internal dynos-work skill. Manage the global cross-project sweeper daemon. Runs only when the user explicitly types /dynos-work:global; never auto-triggered from conversation."
---

# dynos-work: Global

Manage the global daemon that sweeps all registered projects.

## Ruthlessness Standard

- Do not fake daemon health from stale assumptions.
- Report the exact status returned by the command.
- If the subcommand fails, surface the failure directly instead of printing a polite summary.

## Usage

```
/dynos-work:global start             # start sweeper
/dynos-work:global stop              # stop sweeper
/dynos-work:global status            # show health
/dynos-work:global run-once          # single sweep
/dynos-work:global logs              # sweep history
```

## What you do

Parse the user's arguments to determine the subcommand. Run the corresponding command:

### start
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/daemon.py" start
```

### stop
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/daemon.py" stop
```

### status
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/daemon.py" status
```

Print the JSON result in a human-readable format: running/stopped, PID, last sweep time, project count.

### run-once
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/daemon.py" run-once
```

### logs
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/daemon.py" logs
```

## Default

If no subcommand is given, show the status.
