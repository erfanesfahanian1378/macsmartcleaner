# Changelog

## Unreleased

- **Faster scans**: folders are measured by a pool of helper processes in parallel (2x faster even on a
  4-core machine with warm caches, more on cold disks), and a folder is never walked twice in one scan.
  Results are identical to the sequential walker, including hard links
- **25 new rules (85 total)**: Spotlight index (rebuilt properly), unified logs, rotated logs, `/cores`,
  macOS update leftovers, old "Install macOS" apps, external drives' trash, uninstalled-app leftovers
  (moved to Trash), saved window state, old installers in Downloads, Adobe media cache, Spotify cache,
  Telegram media, Steam caches, simulator app caches, browser profile caches (Chrome/Brave/Edge/Arc), and more
- **Large & old files** tab in Deep Scan (Spotlight-powered)
- With `sudo`, discovery also explains `/System/Volumes/Data` hidden folders and `/private/var/db`
- Root-only folders show "?" with "run with sudo to measure" instead of a misleading 0

- **Home menu**: `msc` opens an animated menu with a live CPU/RAM/disk strip
- **Smart Clean** (`msc smart`): one-key cleanup of safe caches and junk that skips the caches of apps
  that are currently open
- **Startup Items** (`msc startup`): login items, launch agents and daemons, with running/enabled status,
  leftover detection and advice; enable, disable or remove them (moved to the Trash)
- **Optimize** (`msc optimize`): flush DNS, purge memory, maintenance scripts, Quick Look, Open With,
  fonts, Dock/Finder restart, disk check, Spotlight rebuild, each with an animated spinner
- **System Status** (`msc status`): live dashboard with per-core CPU (P/E cores), GPU, memory breakdown and
  pressure, swap, disk and I/O, network speed, battery, thermal state and top processes
- **Rescan**: `r` in the Deep Scan list (or after a cleanup) re-runs the scan and rebuilds the list

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
