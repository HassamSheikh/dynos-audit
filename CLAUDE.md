# CLAUDE.md

Guidance for Claude Code and other Claude-based agents working in this repository.

## Release Hygiene

Every PR that changes dynos-work ships as exactly one release. **The version number is computed by CI — do not edit it by hand.**

### What CI does

`.github/workflows/release-hygiene.yml` runs on every PR open, synchronize, reopen, and label change. It calls `scripts/bump_version.py`, which:

- reads the current version from the PR's **base ref** (not from your branch),
- applies one bump — `patch` by default, or `major`/`minor`/`none` if the PR carries a `release:major` / `release:minor` / `release:none` label,
- writes the result to all four manifests (`package.json`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `.codex-plugin/plugin.json`),
- adds a stub `CHANGELOG.md` section only if no entry for that version exists,
- and pushes a `chore: update release metadata` commit to your branch.

It is idempotent: re-running produces the same version, so a PR gets one version no matter how many commits it contains.

### What you do

- **Write the `CHANGELOG.md` entry.** CI's stub is a fallback, not a substitute — it only records the PR title. Describe the behavior change, fix, or maintenance work concretely.
- **Leave the four manifest version fields alone.** Hand-editing them is overwritten by CI, and the bot's push to your branch will reject your next `git push` until you rebase.
- **One entry per PR, not per change.** If a PR bundles several logical changes, they still ship under one version. Writing an entry per change produces orphan entries for versions no manifest ever carried.

If you need to know the version your PR will land as, it is the base branch's version plus one patch (or whatever your `release:*` label selects). `scripts/bump_version.py` has no dry-run mode — it writes the manifests in place — so read the number off `git show origin/main:package.json` rather than running the script to find out.

Do not open or prepare a PR with code changes while leaving `CHANGELOG.md` unchanged.
