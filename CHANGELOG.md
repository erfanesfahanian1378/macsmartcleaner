# Changelog

## Unreleased

- **Auto-Protect** (`msc guard`, menu): hourly LaunchDaemon that rebuilds the Spotlight index above a
  limit and, when free space drops below a floor, deletes Time Machine local snapshots and runs the safe
  cleanup (skipping open apps); adjustable limits and an activity log in the menu
- **Startup animation**: block-letter logo drawn by a glowing scan beam with sparkles, typed tagline and a
  warm-up bar (any key skips, `MSC_NO_INTRO=1` turns it off); flowing colour wave on the menu logo

- **Spotlight Doctor** (`msc spotlight`): measures the real index, lists Search Privacy exclusions, flags
  folders that make Spotlight loop (cloud storage, dev/VM/model folders), **watches which folders
  Spotlight reads** (fs_usage), excludes folders safely (with a config backup), rebuilds, and an hourly
  **guard** that rebuilds if the index passes a limit. For indexes that grow to hundreds of GB again after
  being deleted
- **System Data breakdown** (`msc diagnose`): every APFS volume in the container (stuck Update volume),
  every snapshot (Time Machine and backup apps), indexes, swap, logs, caches and the biggest folders
- Menu: System Data Doctor

**Audit against CleanMyMac X (verified on a real Mac in CI)**
- **Uninstaller** (`msc uninstall`): apps with size and last-opened date. Removes an app with all its
  files (matched by bundle id) to the Trash; quits it first; admin items with your password
- **Space Lens** (`msc lens`): browse any folder or the whole disk by size, drill down, trash items
- **Optimize**: free up purgeable space, speed up Mail (compact its index)
- **System Status**: select a heavy process and quit / force quit it (core processes protected)
- **System items now ask for your password** and are cleaned in a separate admin step for exactly the
  items you picked, instead of failing with "needs sudo"
- New rules: Aerial wallpaper/screen saver videos (often 10-60 GB of System Data on macOS 14+), unused
  disk images anywhere in your folders (Trash), Deleted Users, Unreal Zen cache, system-wide simulator
  caches; explained entries for Command Line Tools, .NET SDKs, Android SDK and more
- Real-Mac fixes: Apple group containers (`groups.com.apple.*`, Shortcuts' `is.workflow.*`) are no longer
  flagged as leftovers, and every leftover now needs Spotlight to confirm no such app exists; copies of an
  app that share a bundle id (one Python Launcher per Python version) are labelled by folder and keep
  their shared settings when one copy is removed
- Smart Clean also cleans the per-user system cache, shows real app names for skipped apps, and offers
  to empty the Trash
- Fixes found on a real Mac: the Time Machine note showed another item's text; today's live system
  log could be deleted (rotated logs now need to be 2+ days old); the first scan could sit 30 s in
  "Checking tools" (slow checks now run in the background)
- Fixes found in review: log folders are emptied instead of deleted; items moved to the Trash are no
  longer reported as "freed"; files created under sudo are owned by you; the right user id under
  sudo; Trash name collisions keep the file extension; already-removed paths no longer report errors;
  the Trash is emptied before anything new is moved into it; Spotlight is only offered for rebuild
  when the index is 5 GB+ and unified logs above 1 GB; Intel/AMD GPU utilisation

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
