# macsmartcleaner

**Find out why "System Data" is eating your Mac's disk, and safely get the space back.**

Open *System Settings > General > Storage* on almost any Mac and you'll find a grey
**System Data** bar that can reach 100, 200 or 300+ GB. macOS won't tell you what's in it.
`macsmartcleaner` (`msc`) will. It is a free, open-source command-line tool:

- **Scans** about 60 known sources of hidden bloat plus anything big it doesn't recognise
- **Explains** each item in plain English: what it is and what happens if you delete it
- **Cleans** only what is safe, asks before deleting, and has a dry-run mode
- **Can't delete your stuff**: Documents, Photos, iCloud, Mail, Keychains and system folders are hard-blocked

No sign-up, no background app, no telemetry, no dependencies. Nothing leaves your Mac.

```
$ msc scan

Xcode & Apple dev  (41.8 GB)
     28.1 GB  safe     xcode-deriveddata        ~/Library/Developer/Xcode/DerivedData
     11.2 GB  safe     xcode-device-support     ~/Library/Developer/Xcode/iOS DeviceSupport/17.5 (21F79)
      2.5 GB  safe     simulator-unavailable    6 simulator(s) for runtimes no longer installed

Docker, VMs & emulators  (38.0 GB)
     38.0 GB  caution  docker                   ~/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw

Summary
  safe        54.3 GB   msc clean                    (caches, logs - regenerated automatically)
  caution     61.0 GB   msc clean --tier caution     (re-downloads / slower rebuilds)
  review      72.4 GB   msc clean --only <rule-id>   (backups, models, archives - your call)

  ! Time Machine: 14 local snapshot(s) - size hidden by APFS, often tens of GB.
```
<sub>(example output)</sub>

---

## Install

Requires macOS 11 or newer. Paste this into **Terminal** (Applications > Utilities > Terminal):

```bash
curl -fsSL https://raw.githubusercontent.com/erfanesfahanian1378/macsmartcleaner/main/install.sh | sh
```

If your Mac asks to install the **Command Line Tools**, click Install and run the command again.
They provide the Python that `msc` runs on.

<details>
<summary>Other ways to install</summary>

```bash
# with pipx / pip
pipx install git+https://github.com/erfanesfahanian1378/macsmartcleaner
# or run straight from a clone, no install
git clone https://github.com/erfanesfahanian1378/macsmartcleaner && cd macsmartcleaner && ./msc scan
```
To update, run the installer again. To uninstall: `curl -fsSL https://raw.githubusercontent.com/erfanesfahanian1378/macsmartcleaner/main/install.sh | sh -s -- --uninstall`
</details>

### Give Terminal "Full Disk Access" (important)

macOS hides a lot of `~/Library` from Terminal, which makes the numbers come out too low.
Open **System Settings > Privacy & Security > Full Disk Access**, turn on **Terminal**
(or iTerm, VS Code, whatever you use), and restart that app. Then run `msc doctor` to confirm.

## Use it in 3 steps

```bash
msc scan                 # 1. look: shows everything, changes nothing
msc clean --dry-run      # 2. preview: what the safe cleanup would delete
msc clean                # 3. clean: shows the plan and asks "Delete X GB now? [y/N]"
```

Want a nicer view? `msc scan --html ~/Desktop/storage.html && open ~/Desktop/storage.html`
creates a report page with an explanation for every item.

### How safe is "safe"?

Every item has a safety level:

| Level | What it means | Cleaned by |
|---|---|---|
| 🟢 **safe** | Caches and logs. Apps rebuild them automatically | `msc clean` |
| 🟡 **caution** | Rebuildable but costly: re-downloads, slow rebuilds, Trash, local Time Machine snapshots | `msc clean --tier caution` |
| 🟣 **review** | Might be something you want: iPhone backups, AI models, app archives | only when you name it: `msc clean --only ios-backups` |
| 🔵 **info** | Shown so you know. Cleaned from the app that owns it (VMs, Ollama models…) | the app itself |

Big folders that no rule knows about are listed with a best guess (**likely-junk**,
**orphaned** = its app is gone, **stale** = untouched for months, **data**, **system**) but
are **never deleted automatically**. If you decide one should go,
`msc trash "<path>"` moves it to the Trash so you can still undo it.

## What it finds

| Where the space hides | Typical size | Who has it |
|---|---|---|
| **Time Machine local snapshots** (their size isn't shown anywhere) | 20-200 GB | anyone with Time Machine |
| iPhone/iPad backups and firmware downloads | 10-200 GB | anyone with an iPhone/iPad |
| App caches: browsers, Spotify, Slack, Discord, Teams, VS Code, Adobe… | 2-30 GB | everyone |
| Logs, crash reports, Mail attachment copies, Trash | 1-10 GB | everyone |
| Leftovers from apps you already deleted | varies | everyone |
| Xcode DerivedData, device support, simulators & runtimes | 10-100 GB | iOS/macOS developers |
| Docker / OrbStack / Colima disks, VMs, Android emulators | 20-150 GB | developers |
| npm, pnpm, yarn, bun, pip, uv, conda, Homebrew, Gradle, Maven, Cargo, Go, CocoaPods, Flutter caches | 5-60 GB | developers |
| Hugging Face, Ollama, LM Studio, PyTorch, Whisper models | 10-200 GB | AI/ML folks |
| Unreal DerivedDataCache & vault, Unity caches & editors, Godot templates, Steam shader cache | 5-100 GB | game developers |
| `node_modules`, Unity `Library/`, Unreal `Intermediate/`, `.venv`, Rust `target/`… in idle projects | 5-100 GB | developers |

Run `msc rules` for the full list, or `msc explain <rule-id>` for the details of one rule.

## All commands

```bash
msc scan [--html FILE] [--json FILE]        # full report
msc scan --projects ~/code ~/Unity          # tell it where your projects live
msc clean                                   # safe tier
msc clean --tier caution --skip maven       # include "caution" items, except Maven
msc clean --only tm-snapshots               # remove local Time Machine snapshots (asks for your password)
msc clean --only huggingface-models -i      # confirm each item one by one
msc clean --projects --older-than 60        # build folders of projects untouched for 60+ days
msc clean --min-age 7                       # keep cache items used in the last week
msc trash "<path>"                          # move a reviewed folder to the Trash
msc schedule install                        # automatic weekly "safe" cleanup (Sundays 11:00)
msc schedule remove
msc rules | msc explain <id> | msc doctor
sudo msc scan                               # also measures /Library and /private/var
```

## FAQ

**Will this break my apps?** The *safe* tier only removes caches and logs, which apps are
designed to rebuild. An app may be a little slower on its first launch afterwards. For the
cleanest result, quit big apps (Xcode, Slack, VS Code…) first; `msc` warns you if they're running.

**My System Data didn't drop right away.** macOS updates that number lazily. Wait a minute,
or restart. Space freed from Time Machine snapshots can take a few minutes to show up.

**What won't it touch?** Your personal folders, Photos, Mail, Messages, iCloud Drive,
Keychains, `/System`, swap (`/private/var/vm`) and macOS databases. A restart clears swap and
many temporary files on its own.

**Is it a replacement for CleanMyMac etc.?** It focuses on one thing, explaining and shrinking
System Data, and it shows you exactly what it will do. It's free and the code is open.

## How it works

```
rules.py    catalog of known junk: location, safety level, how to clean, plain-English explanation
scanner.py  expands locations, measures real disk usage in parallel, never counts anything twice
discover.py heuristics for the unknown: big uncovered folders, orphaned app data, project build output
cleaner.py  selects by safety level -> safety check on every path -> delete or run the tool's own
            cleaner (brew cleanup, docker prune, tmutil…) -> re-measures what was actually freed
safety.py   hard deny-list: home, Documents, Photos, Keychains, iCloud, /System and any parent
            of them; symlinks can't be used to escape
```

Every cleanup is logged to `~/.local/state/macsmartcleaner/history.jsonl`.

### Add your own rules

Create `~/.config/macsmartcleaner/rules.json`:

```json
[{"id": "blender-cache", "name": "Blender render cache", "category": "Custom",
  "safety": "safe", "action": "delete-contents", "paths": ["~/renders/cache"]}]
```

## Contributing

Know another folder that bloats System Data? Please
[open an issue](../../issues/new?template=new-junk-location.yml) or send a pull request.
Adding a rule is usually 5 lines. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Disclaimer

This tool deletes files. It is built to be careful: dry-run, confirmation, safety tiers and a
hard deny-list. Still, keep a backup, as you should anyway. Provided under the [MIT License](LICENSE),
without warranty.
