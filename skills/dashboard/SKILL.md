---
name: dashboard
description: "Internal dynos-work skill. Start the SPA dashboard server showing all registered projects. Runs only when the user explicitly types /dynos-work:dashboard; never auto-triggered from conversation."
---

# dynos-work: Dashboard

Start the SPA dashboard server. Shows all registered projects in a unified web UI.

## Ruthlessness Standard

- Do not invent server state, URLs, or process health.
- Report the exact command outcome. If startup fails, surface the failure plainly.
- If the server is already running or broken, say that directly instead of implying success.

## Usage

```
/dynos-work:dashboard              # start server at :8765
/dynos-work:dashboard stop         # stop server
/dynos-work:dashboard restart      # restart server
```

## What you do

### serve (default)

```bash
python3 "${REPO_DIR}/telemetry/dashboard.py" serve
```

Print the URL: `http://127.0.0.1:8765`

### stop

```bash
python3 "${REPO_DIR}/telemetry/dashboard.py" kill
```

### restart

```bash
python3 "${REPO_DIR}/telemetry/dashboard.py" restart
```

## Notes

The dashboard runs as a background HTTP server. It survives after the conversation ends. Use `stop` to shut it down.

The dashboard shows:
- Global daemon status and aggregate stats
- Per-project cards (click to expand full detail)
- Active routes, recent tasks, benchmark runs, findings
- Autofix PRs per project
- Maintenance cycle history
