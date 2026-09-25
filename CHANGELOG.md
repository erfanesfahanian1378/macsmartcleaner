# Changelog

## Unreleased

- **Interactive browser**: `msc` now opens a full-screen list after scanning. Move with the arrow keys,
  select with space, read what each item is, reveal it in Finder, and delete with a confirmation.
  `msc scan --report` keeps the text report.
- **Live progress** for scans and cleanups: animated spinner, gradient percentage bar, live
  file/GB counters, elapsed time and the current folder, plus a ✔ checklist of finished stages
- Fix: a folder whose listing fails midway (e.g. OneDrive timing out) no longer aborts the scan
- Cloud-synced folders (~/Library/CloudStorage, iCloud Drive) are no longer walked

## 0.1.0 - 2026-09-25

First public release.

- `msc scan`: about 60 rules covering Time Machine snapshots, app and Electron caches, logs, iOS backups,
  Xcode and simulators, package managers, Docker and VMs, AI model caches and game-engine caches
- Discovery of uncovered big folders (likely-junk / orphaned / stale / data / system) and of
  project build artifacts (node_modules, Unity Library, Unreal Intermediate, .venv, target, ...)
- `msc clean` with safety tiers, dry-run, per-item confirmation, deny-list and history log
- HTML and JSON reports, `msc trash`, weekly `msc schedule`, `msc doctor`
- One-line installer
