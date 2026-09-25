# macsmartcleaner

**Get your disk space back, see what's slowing your Mac down, and keep it lean, all from one
friendly terminal app.**

Open *System Settings > General > Storage* on almost any Mac and you'll find a grey
**System Data** bar that can reach 100, 200 or 300+ GB. macOS won't tell you what's in it.
`macsmartcleaner` (`msc`) will. It's a free, open-source toolkit with an animated terminal interface:

| | Tool | What it does |
|---|---|---|
| ✦ | **Smart Clean** | One key clears caches and junk. It **skips the caches of apps you have open**, so nothing glitches |
| ◧ | **Space Lens** | Browse any folder, or the whole disk including System Data, sorted by size. Drill down and send anything to the Trash |
| ⌫ | **Uninstaller** | Removes an app **together with everything it left behind**: settings, caches, containers, agents. All of it goes to the Trash |
| ◎ | **Deep Scan** | Finds everything using space (about 60 known junk sources plus anything big it doesn't recognise). Browse it, read what each item is, pick what goes |
| ↑ | **Startup Items** | Every app and helper that launches automatically. Disable or remove them, and spot leftovers from deleted apps |
| ⚙ | **Optimize** | Flush DNS, free inactive memory, fix a stuck Finder/Dock, rebuild Spotlight and "Open With", and more |
| ◔ | **System Status** | A live dashboard: CPU per core, GPU, memory pressure, swap, disk, network, battery, thermal state. Quit heavy apps from it |

It **can't delete your stuff**: Documents, Photos, iCloud, Mail, Keychains and system folders are
hard-blocked, and everything asks before it acts. No sign-up, no background app, no telemetry,
no dependencies. Nothing leaves your Mac.

```
                                      ┏┳┓┏━┓┏━╸   macsmartcleaner
                                      ┃┃┃┗━┓┃     keep your Mac lean
                                      ╹ ╹┗━┛┗━╸   Apple M2 Pro

                    ▶ ✦  Smart Clean     Clear caches & junk, skipping open apps        1
                      ◎  Deep Scan       See everything using space, pick what goes     2
                      ◧  Space Lens      Browse any folder by size, drill down, trash   3
                      ⌫  Uninstaller     Remove apps with all their leftovers           4
                      ↑  Startup Items   Apps & helpers that launch automatically       5
                      ⚙  Optimize        DNS, memory, Finder/Dock, Spotlight & more     6
                      ◔  System Status   Live CPU, GPU, memory, disk, network           7
                      ×  Quit                                                           q

       CPU  ███········· 23%          RAM  ████████···· 11.8/16 GB     Disk ███████████· 188 GB free
```

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

## Use it

Just type:

```bash
msc
```

The animated home screen opens. Choose a tool with the arrow keys and Enter, or press its number.
Every tool is also a direct command (`msc smart`, `msc scan`, `msc startup`, `msc optimize`, `msc status`).

### ✦ Smart Clean: `msc smart`

The quick, safe option. It scans only the known-safe caches and logs, and checks which apps are running.
Anything that belongs to an open app (Chrome, Slack, Spotify, Xcode…) is **left alone** and listed,
so you can quit those apps and run it again. Then it shows a size chart of what it will clean, asks once
(`Clean it now? [Y/n]`), and cleans with a live progress bar.

### ◎ Deep Scan: `msc scan`

**1. It scans, with live progress.** You can always see it working: an animated spinner, a
percentage bar, files counted, GB scanned, elapsed time, and the folder it's reading right now.
Nothing is deleted during a scan.

```
  ◆ macsmartcleaner  scanning your Mac - nothing is deleted during a scan

  ✔ Checking Time Machine, simulators & system tools  Time Machine snapshots found · 0:01
  ✔ Measuring known junk locations  1,204 places checked · 0:48
  ⠹ Finding project build folders (node_modules, Library, .venv...)                  3/4
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╸━━━━━━━━━━━━━━━━━━━━━━━━━━━━  61%
    1,842,310 files · 212.4 GB scanned · 1:07 · ~/Projects/game/Library/Artifacts
```

**2. You browse the results and choose.** A full-screen list opens in your terminal:

```
 ◆ macsmartcleaner                              ████████████████······ 812 GB used · 188 GB free
                                                                  Selected: 104.2 GB in 23 item(s)
  Junk & caches (147.6 GB)   Projects (18.3 GB)   Big folders (31.0 GB)
 ─────────────────────────────────────────────────────────────────────────────────────────────────
 ▸ [x]   28.1 GB             safe      Xcode DerivedData             ~/Library/Developer/Xcode/…
   [x]   22.4 GB ████████··  safe      App caches (~/Library/Caches) ~/Library/Caches
   [ ]   14 snaps            caution   Time Machine local snapshots
   [ ]   18.0 GB ██████····  caution   Docker Desktop disk image     ~/Library/Containers/com.do…
 ─────────────────────────────────────────────────────────────────────────────────────────────────
 Xcode DerivedData · safe
  Build intermediates and indexes for every project you've opened.
  After cleaning: Next build of each project is a full build.
 ─────────────────────────────────────────────────────────────────────────────────────────────────
  ↑↓ move  space select  tab lists  a safe  c +caution  n none  o Finder  d delete  q quit
```

| Key | Does |
|---|---|
| `↑` `↓` (or `j` `k`), `PgUp` `PgDn` | move through the list; the panel below explains the highlighted item |
| `space` | select / unselect |
| `tab` or `1` `2` `3` | switch between **Junk & caches**, **Projects** and **Big folders** |
| `a` / `c` / `n` | select all *safe* / *safe + caution* / nothing in this list |
| `o` | show the folder in Finder so you can look inside |
| `d` | delete what's selected. Shows exactly what will happen and asks `y`/`n` first |
| `q` | quit |

*Safe* items start out selected. Anything marked *review* or found under *Big folders* is only
selected if you pick it yourself, and big folders go to the Trash so you can undo it.

**3. It cleans, with live progress,** then shows how much space you got back
(`✨ Freed 104.2 GB  free space 188 GB → 292 GB`) and takes you back to the list.

**Refresh any time:** press `r` in the list to rescan everything, or answer `r` after a cleanup.
The list is rebuilt from a fresh scan, so you see the real current state.

### ◧ Space Lens: `msc lens [folder]`

Starts in your home folder and lists what's inside, biggest first, with a bar and a percentage.
`Enter` opens a folder, `←` goes back, `d` moves the highlighted item to the Trash (protected
places are refused), and `o` shows it in Finder. Press **`/`** to look at the **whole disk**,
which is the fastest way to find where "System Data" really is. Run `sudo msc lens /` so that
macOS lets it look inside every folder. Folders you've already opened are remembered, so going back is instant.

### ⌫ Uninstaller: `msc uninstall`

Lists every non-Apple app with its size and when you last opened it. Sort by size or by "not used
for months" (`s`). Selecting an app finds everything that belongs to it, matched by the app's own
bundle id: Application Support, caches, containers, group containers, preferences, saved state,
web data, logs, launch agents, and (with your password) launch daemons and privileged helpers.
Running apps are quit first, and everything is moved to the Trash so you can undo it.
From scripts: `msc uninstall --list`, `msc uninstall "App Name" --dry-run`.

### ↑ Startup Items: `msc startup`

Lists everything that starts automatically: your **Login Items**, your **Launch Agents**,
agents installed for **all users**, and **system daemons**. Each one shows whether it's running,
enabled or disabled, with advice:

- **broken**: the app it belongs to is gone. A leftover, safe to remove.
- **auto-updater**: optional. Most apps still update themselves when you open them.
- **system daemon**: keep it if you use the app (VPNs, drivers, security tools).

`space` enables or disables the highlighted item, `x` removes it (the launch file goes to the Trash,
so you can undo it), `o` shows it in Finder and `r` refreshes the list. System items ask for your password.
Apple's own services are never listed or touched.

### ⚙ Optimize: `msc optimize`

A checklist of maintenance tasks, each explained, each using Apple's own tools. The recommended
ones are pre-selected:

| Task | Fixes |
|---|---|
| Flush DNS cache | websites not loading after network changes |
| Free up inactive memory | apps waiting for RAM (`purge`) |
| Run macOS maintenance scripts | log rotation and temp cleanup that normally runs overnight |
| Reset Quick Look thumbnails | wrong or missing previews in Finder |
| Rebuild the "Open With" list | duplicate or deleted apps in right-click > Open With |
| Clear font caches | garbled or missing fonts |
| Restart Dock, Finder & menu bar | a stuck Dock, frozen Finder, unresponsive menu bar icons |
| Check the startup disk | read-only file system check |
| Rebuild Spotlight index | Spotlight not finding files, or a bloated index |
| Free up purgeable space | macOS holding on to purgeable space (snapshots etc.) you need now |
| Speed up Mail | slow Mail search and scrolling (compacts Mail's index, with Mail closed) |

Press Enter and each task runs with its own animated spinner and a ✔/✖ result.
From scripts: `msc optimize --list` and `msc optimize --run dns,quicklook` (or `--run recommended`).

### ◔ System Status: `msc status`

A live dashboard that refreshes every second:

- **CPU**: total, a history sparkline, a bar for every core (labelled **P**/**E** on Apple Silicon) and load averages
- **Memory**: used of total, split into app, wired, compressed and cached. It shows **memory pressure**
  (green, yellow or red, like Activity Monitor) and swap
- **GPU**: utilisation with history, renderer load and GPU memory (Apple Silicon and most Intel Macs)
- **Storage**: used and free, plus live disk throughput
- **Network**: live download and upload speed with sparklines
- **Battery & thermal**: charge, charging state, time left, and whether the Mac is being throttled
- **Heaviest processes** by CPU (`c`) or memory (`m`). Select one and press `k` to quit it (`K` to force quit).
  Core macOS processes are protected

No admin password needed. `msc status --once` prints a one-off snapshot.

Prefer plain commands, for scripts or a quick check?

```bash
msc scan --report                  # print a text report instead of the browser
msc scan --html ~/Desktop/storage.html && open ~/Desktop/storage.html   # shareable web report
msc clean --dry-run                # what the safe cleanup would delete
msc clean                          # do it (asks first)
```

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
| Logs, crash reports, Mail attachment copies, Trash (including external drives' trash) | 1-10 GB | everyone |
| **Leftovers of uninstalled apps**: containers, group containers, saved state, web data | 1-50 GB | everyone |
| **Large & old files** in your home folder: videos, disk images, archives you haven't opened in months | varies | everyone |
| Old installers in Downloads (`.dmg`, `.pkg`, `.xip`), old "Install macOS" apps, macOS update leftovers | 5-40 GB | everyone |
| Adobe media cache, Spotify streaming cache, Telegram media, Steam caches | 5-100 GB | creators, gamers |
| **System level** (with `sudo`): Spotlight index (`.Spotlight-V100`), unified logs, rotated logs, `/cores` crash dumps, `/Library/Caches` | 2-50 GB | everyone |
| Xcode DerivedData, device support, simulators & runtimes | 10-100 GB | iOS/macOS developers |
| Docker / OrbStack / Colima disks, VMs, Android emulators | 20-150 GB | developers |
| npm, pnpm, yarn, bun, pip, uv, conda, Homebrew, Gradle, Maven, Cargo, Go, CocoaPods, Flutter caches | 5-60 GB | developers |
| Hugging Face, Ollama, LM Studio, PyTorch, Whisper models | 10-200 GB | AI/ML folks |
| Unreal DerivedDataCache & vault, Unity caches & editors, Godot templates, Steam shader cache | 5-100 GB | game developers |
| `node_modules`, Unity `Library/`, Unreal `Intermediate/`, `.venv`, Rust `target/`… in idle projects | 5-100 GB | developers |

Run `msc rules` for the full list of 85 rules, or `msc explain <rule-id>` for the details of one rule.
Run **`sudo msc`** once in a while to include the system-level locations. Without it they're shown with
a "?" size, because macOS only lets an admin look inside them.

### Compared with CleanMyMac X

| | CleanMyMac X | msc |
|---|---|---|
| System junk (caches, logs, Xcode, broken downloads) | ✔ | ✔ plus 30+ developer, AI and game-dev caches it doesn't know |
| Time Machine local snapshots | ✔ | ✔ and it tells you they're there even though macOS hides their size |
| Large & old files | ✔ | ✔ (Spotlight-powered, instant) |
| Uninstaller (app + all its files) | ✔ | ✔ matched by bundle id, all to the Trash |
| Uninstalled-app leftovers | ✔ | ✔ moved to the Trash, so it's reversible |
| Space Lens (browse by size) | ✔ | ✔ including the whole disk / System Data with `sudo` |
| Heavy consumers (quit apps) | ✔ | ✔ from System Status, with core processes protected |
| Startup items / login items | ✔ | ✔ including launch daemons, with "broken leftover" detection |
| Maintenance (DNS, RAM, purgeable space, Mail, Spotlight, Launch Services…) | ✔ | ✔ |
| System items that need admin rights | helper tool | asks for your password once, only for those items |
| Live system monitor | menu bar app | ✔ full dashboard: per-core CPU, GPU, memory pressure, network, battery |
| Skips caches of apps you have open | partly | ✔ Smart Clean checks every running app |
| Explains every item, dry-run, no hidden actions | – | ✔ |
| Price, account, background process | subscription | free, none, none |

**Things we deliberately don't do.** Some cleaners remove app **language files** and "thin" **universal
binaries**. On modern macOS that breaks apps' code signatures: apps can refuse to launch or stop
updating, and it saves little. We also never delete `.Spotlight-V100`, `.DocumentRevisions-V100` or
`.fseventsd` by hand. Spotlight is rebuilt properly with `mdutil -E`, and the other two are managed by macOS.

## All commands

```bash
msc                                         # animated home menu
msc smart [--dry-run] [--yes] [--empty-trash]  # Smart Clean
msc scan                                    # Deep Scan + interactive browser (r = rescan)
msc scan --report [--html FILE] [--json FILE]  # text/web/JSON report instead
msc lens [folder] [--list]                  # Space Lens (`/` = whole disk)
msc uninstall [--list] [APP…] [--dry-run]   # remove apps with their leftovers
msc startup [--list]                        # startup items
msc optimize [--list] [--run IDS]           # maintenance tasks
msc status [--once]                         # live system dashboard
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
sudo msc                                    # include system-level junk (Spotlight index, system logs, /cores…)
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

**How is scanning so fast?** Folders are split into sub-folders and measured by several helper
processes at once (one per CPU core), streaming progress back. A folder measured once is never walked
again in the same scan, and large files come straight from the Spotlight index.

**Will Optimize make my Mac faster?** Honestly: macOS manages memory and caches well on its own.
These tasks fix specific problems (stale DNS, broken previews, a stuck Finder, a bloated
Spotlight index) and give a quick boost when memory is tight. Disabling startup items you don't
need is what makes the most lasting difference to startup time and free memory.

**Why does Startup Items ask to control "System Events"?** That's how macOS lets apps read your
Login Items list. Allow it once, or deny it and you'll still see all the launch agents and daemons.

**Is it a replacement for CleanMyMac etc.?** It focuses on one thing, explaining and shrinking
System Data, and it shows you exactly what it will do. It's free and the code is open.

## How it works

```
rules.py    catalog of known junk: location, safety level, how to clean, plain-English explanation
scanner.py  expands locations, measures real disk usage in parallel, never counts anything twice
discover.py heuristics for the unknown: big uncovered folders, orphaned app data, project build output
cleaner.py  selects by safety level -> safety check on every path -> delete or run the tool's own
            cleaner (brew cleanup, docker prune, tmutil…) -> re-measures what was actually freed
smart.py    Smart Clean: safe rules only, skips caches of running apps
uninstall.py  finds an app's files by bundle id; quits, unloads agents, moves everything to the Trash
apptools.py Space Lens and Uninstaller screens
sizes.py    parallel folder measuring (helper processes, results identical to a plain walk)
startup.py  login items + launch agents/daemons: detect, explain, enable/disable/remove
optimize.py maintenance tasks built on Apple's own tools (dscacheutil, purge, qlmanage, mdutil…)
sysinfo.py  live metrics: mach per-core CPU, vm_stat, sysctl, ioreg (GPU), pmset, netstat, iostat
app.py      the animated home menu, status dashboard, startup and optimize screens
ui.py       live progress: spinner, gradient percentage bar, live counters (plain lines when not a terminal)
tui.py      the interactive curses browser: select, explain, reveal in Finder, delete
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
