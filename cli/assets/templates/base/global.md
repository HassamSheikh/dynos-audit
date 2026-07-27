---
name: global
description: "Internal dynos-work skill. Manage the global cross-project sweeper daemon. Runs only when the user explicitly types /dynos-work:global; never auto-triggered from conversation."
---

# dynos-work: Global

Show status across all registered projects and manage per-project daemons.

## Usage

```
/dynos-work:global             # list all registered projects and their daemon status
/dynos-work:global start       # start daemon for every active registered project
/dynos-work:global stop        # stop daemon for every active registered project
/dynos-work:global status      # show daemon health for each project
```

## What you do

### status (default)

List all registered projects and their daemon health:

```bash
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/registry.py" list
```

For each project in the output, show whether its local daemon is running:

```bash
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/daemon.py" status --root "<project_path>"
```

### start

Start the local daemon for every active registered project:

```bash
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/registry.py" list
```

Then for each active project path:

```bash
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/daemon.py" start --root "<project_path>"
```

### stop

Stop the local daemon for every active registered project:

```bash
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/daemon.py" stop --root "<project_path>"
```

## Default

If no subcommand is given, show the status of all registered projects.
