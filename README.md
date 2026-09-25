# macsmartcleaner (`msc`)

Finds out **why macOS "System Data" is huge** and cleans it safely. It has no
dependencies and runs on the `python3` that ships with macOS / Xcode Command Line Tools (3.9+).

```bash
git clone https://github.com/erfanesfahanian1378/macsmartcleaner && cd macsmartcleaner
./msc doctor                      # check permissions first
./msc scan --html ~/Desktop/storage.html   # read-only: see what is using space
./msc clean --dry-run             # see what the safe cleanup would do
./msc clean                       # do it (asks before deleting)
```

Optional: `pip3 install --user .` puts `msc` on your PATH.

> **Grant Full Disk Access first**: System Settings > Privacy & Security > Full Disk Access >
> add Terminal (or iTerm / VS Code / whatever you run it from), then restart that app.
> Without it macOS hides large parts of `~/Library` and the numbers come out too low.
> Run `sudo ./msc scan` to also measure `/Library` and `/private/var`.

## Where 300+ GB of "System Data" usually hides

| Suspect | Typical size | Rule |
|---|---|---|
| Time Machine **local snapshots** (APFS hides their size) | 20-200 GB | `tm-snapshots` |
| Docker Desktop / OrbStack virtual disk | 20-150 GB | `docker`, `orbstack` |
| Xcode DerivedData, device support, simulators & runtimes | 10-100 GB | `xcode-*`, `simulator-*` |
| AI models: Hugging Face, Ollama, LM Studio, torch | 10-200 GB | `huggingface-*`, `ollama`, `lmstudio`, `torch-hub` |
| Package caches: npm, pnpm, yarn, pip, uv, conda, Gradle, Cargo, Go, CocoaPods, Homebrew | 5-60 GB | one rule each |
| Game dev: Unreal DDC and vault, Unity caches & editors, Godot templates | 5-100 GB | `unreal-*`, `unity-*`, `godot` |
| iPhone backups and firmware | 10-200 GB | `ios-backups`, `ios-updates` |
| App caches and Electron apps (Slack, Discord, VS Code, Teams) | 2-30 GB | `user-caches`, `electron-caches` |
| Leftovers from uninstalled apps in `Containers` / `Application Support` | varies | found by discovery |
| Project build output: `node_modules`, Unity `Library/`, Unreal `Intermediate/`, `.venv`, Rust `target/` | 5-100 GB | found by discovery |

## How it works

```
 rules.py  ── catalog of ~60 known junk locations: path/glob, safety tier, how to clean, why
    │
 scanner.py ── expand globs & probes (tmutil, simctl, getconf) ─► measure in parallel
    │           (real allocated blocks, hard links once, never crosses volumes)
    │           ─► de-duplicate: a specific rule (~/Library/Caches/Homebrew) is carved out
    │              of a generic one (~/Library/Caches) so nothing is counted or deleted twice
    │
 discover.py ── heuristics for what no rule knows:
    │             • big folders in ~/Library, dot-dirs, /Library, /private/var
    │               classified as likely-junk / orphaned (no installed app matches) /
    │               stale (untouched for 6-12+ months) / data / system
    │             • project build artifacts, detected by marker files
    │               (package.json, Assets+ProjectSettings, *.uproject, Cargo.toml, pyvenv.cfg…)
    │
 cleaner.py ── select by tier ─► safety.check() every path ─► delete / run tool's own cleaner
    │           ─► re-measure what was actually freed ─► log to ~/.local/state/macsmartcleaner/
 safety.py  ── hard deny-list: home, Documents, Photos, Keychains, iCloud, /System… and any
                parent of them; symlinked parents are resolved so nothing can escape
```

### Safety tiers

| Tier | Meaning | Cleaned by |
|---|---|---|
| **safe** | Pure caches and logs. Regenerated automatically | `msc clean` |
| **caution** | Regenerable but costly (re-downloads, slow rebuilds), local TM snapshots, Trash | `msc clean --tier caution` |
| **review** | Could be real data: device backups, AI models, Xcode archives, emulators | only `msc clean --only <id>` |
| info | Shown so you know, but cleaned through the owning app (VMs, Ollama, simulator runtimes) | the app itself |

Folders the discovery step flags are never deleted automatically. After you look
at one, `msc trash <path>` moves it to the Trash so you can undo it.

## Commands

```bash
msc scan [--projects ~/code ~/Unity] [--json f] [--html f] [--no-discover]
msc clean                                   # safe tier
msc clean --tier caution --skip gradle-dists
msc clean --only tm-snapshots               # delete local Time Machine snapshots (asks for sudo)
msc clean --only huggingface-models -i      # confirm each model one by one
msc clean --projects ~/code --older-than 60 # node_modules/Library/target… of idle projects
msc clean --min-age 7                       # keep cache items used in the last week
msc rules / msc explain docker
msc trash "~/Library/Application Support/SomeOldApp"
msc schedule install                        # weekly safe cleanup via launchd (Sundays 11:00)
```

### Your own rules

Add to `~/.config/macsmartcleaner/rules.json`:

```json
[{"id": "my-renders", "name": "Blender render cache", "category": "Custom",
  "safety": "safe", "action": "delete-contents", "paths": ["~/renders/cache"]}]
```

## Things `msc` deliberately won't touch

- `/private/var/vm` (swap and sleep image). A restart shrinks it.
- `/private/var/db` and the Spotlight index. If they're huge, a restart or a Safe Mode boot trims them.
- Mail, Messages, Photos and iCloud Drive. Use their own settings (Optimize Mac Storage, Messages > Keep for 1 year).

## Development

```bash
python3 -m unittest discover -s tests -t .
```

The tests build a fake Mac home directory in a temp folder, so they run on macOS and Linux.
