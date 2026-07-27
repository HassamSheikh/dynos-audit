---
name: init
description: "Internal dynos-work skill. Set up the current project: register with dynos-work and start the local daemon. Runs only when the user explicitly types /dynos-work:init; never auto-triggered from conversation or by project-setup requests."
---

# dynos-work: Init

Register the current project and start the local maintenance daemon.

## Ruthlessness Standard

- Do not claim initialization succeeded unless registration and daemon startup both succeeded.
- If the daemon is already running, say so plainly and stop.
- If any setup step fails, stop immediately and report the exact failing step.

## Usage

```
/dynos-work:init
```

## What you do

1. Register the project:
```bash
python3 "${PLUGIN_HOOKS}/registry.py" register "$(pwd)"
```

2. Create the `.dynos` directory:
```bash
mkdir -p .dynos
```

3. Check if a daemon is already running:
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/daemon.py" status --root .
```

If `running` is `true` in the JSON output, print "Daemon already running" and stop.

4. Start the daemon:
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/daemon.py" start --root .
```

5. Print the result:
```
Project registered and daemon started.
Use /dynos-work:start to begin a task.
Use /dynos-work:local status to check daemon health.
```
