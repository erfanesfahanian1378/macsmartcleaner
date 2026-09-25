# Changelog

## 0.1.0 - 2026-09-25

First public release.

- `msc scan`: about 60 rules covering Time Machine snapshots, app and Electron caches, logs, iOS backups,
  Xcode and simulators, package managers, Docker and VMs, AI model caches and game-engine caches
- Discovery of uncovered big folders (likely-junk / orphaned / stale / data / system) and of
  project build artifacts (node_modules, Unity Library, Unreal Intermediate, .venv, target, ...)
- `msc clean` with safety tiers, dry-run, per-item confirmation, deny-list and history log
- HTML and JSON reports, `msc trash`, weekly `msc schedule`, `msc doctor`
- One-line installer
