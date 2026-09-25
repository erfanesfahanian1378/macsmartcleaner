"""The catalog: every known source of macOS "System Data" bloat.

Each rule says *where* the junk lives, *how* to clean it, and *how safe* that is:

  safe     - pure caches/logs; regenerated automatically. Cleaned by default.
  caution  - regenerable but costly (re-downloads, slow rebuilds) or you lose
             something minor (local Time Machine restore points, Trash).
  review   - may be real data (device backups, AI models, VMs). Only cleaned
             when you name the rule explicitly with --only.

Actions:
  delete           - remove each matched path entirely
  delete-contents  - empty the matched directory but keep the folder itself
  command          - let the owning tool clean itself (brew, docker, tmutil...)
  report-only      - just show it; cleaning needs a tool/GUI we shouldn't drive
"""
from __future__ import annotations

import enum
import glob
import json
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .context import Context


class Safety(str, enum.Enum):
    SAFE = "safe"
    CAUTION = "caution"
    REVIEW = "review"


class Action(str, enum.Enum):
    DELETE = "delete"
    DELETE_CONTENTS = "delete-contents"
    COMMAND = "command"
    REPORT = "report-only"


@dataclass
class ProbeResult:
    roots: List[str] = field(default_factory=list)
    note: str = ""
    virtual: bool = False  # true when size can't be measured from paths (e.g. APFS snapshots)
    present: bool = False  # for virtual results: is there anything to clean?


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    category: str
    safety: Safety
    action: Action
    paths: Tuple[str, ...] = ()
    exclude: Tuple[str, ...] = ()  # fnmatch patterns for child names (delete-contents)
    commands: Tuple[Tuple[str, ...], ...] = ()
    command_needs_root: bool = False
    min_age_days: int = 0
    needs_root: bool = False
    quit_apps: Tuple[str, ...] = ()
    trash: bool = False  # move to the Trash instead of deleting (reversible)
    keep_dirs: bool = False  # empty sub-folders instead of removing them (log folders apps expect)
    min_size: int = 0  # only worth offering above this size (e.g. rebuilding Spotlight)
    probe_async: bool = False  # slow probe (simctl): run in the background while the scan continues
    handler: Optional[Callable[[Context, bool], Tuple[bool, str]]] = field(default=None, compare=False, hash=False)
    why: str = ""
    impact: str = ""
    probe: Optional[Callable[[Context], ProbeResult]] = field(default=None, compare=False, hash=False)


# --------------------------------------------------------------------------- probes

def _probe_tm_snapshots(ctx: Context) -> ProbeResult:
    res = ctx.run(["tmutil", "listlocalsnapshots", "/"], timeout=60, as_user=False)
    if res is None or res.returncode != 0:
        return ProbeResult(virtual=True, note="tmutil unavailable")
    snaps = [ln for ln in res.stdout.splitlines() if "com.apple.TimeMachine" in ln]
    return ProbeResult(
        virtual=True,
        present=bool(snaps),
        note=f"{len(snaps)} local snapshot(s) - size hidden by APFS, often tens of GB",
    )


def _tm_snapshot_count(ctx: Context) -> int:
    res = ctx.run(["tmutil", "listlocalsnapshots", "/"], timeout=60, as_user=False)
    if res is None or res.returncode != 0:
        return -1
    return sum(1 for ln in res.stdout.splitlines() if "com.apple.TimeMachine" in ln)


def _delete_tm_snapshots(ctx: Context, dry_run: bool) -> Tuple[bool, str]:
    """Delete all local snapshots: one call on macOS 12+, date by date on older systems."""
    before = _tm_snapshot_count(ctx)
    sudo = [] if ctx.is_root else ["sudo", "-n"]
    res = ctx.run(sudo + ["tmutil", "deletelocalsnapshots", "/"], timeout=1800, as_user=False)
    if res is None or res.returncode != 0 or _tm_snapshot_count(ctx) > 0:
        dates = ctx.run(["tmutil", "listlocalsnapshotdates", "/"], timeout=60, as_user=False)
        for line in (dates.stdout.splitlines() if dates is not None else []):
            if re.match(r"^\d{4}-\d{2}-\d{2}-\d{6}$", line.strip()):
                ctx.run(sudo + ["tmutil", "deletelocalsnapshots", line.strip()], timeout=600, as_user=False)
    after = _tm_snapshot_count(ctx)
    if after == 0:
        return True, f"deleted {before} local snapshot(s); macOS releases the space within a few minutes"
    if after < 0:
        return False, "could not read snapshots (tmutil failed)"
    return False, f"{after} snapshot(s) could not be deleted (one may be in use by a running backup)"


def _probe_unavailable_sims(ctx: Context) -> ProbeResult:
    res = ctx.run(["xcrun", "simctl", "list", "devices", "-j"], timeout=60)
    if res is None or res.returncode != 0:
        return ProbeResult()
    try:
        data = json.loads(res.stdout)
    except ValueError:
        return ProbeResult()
    base = ctx.path("~/Library/Developer/CoreSimulator/Devices")
    roots = [
        os.path.join(base, dev["udid"])
        for devices in data.get("devices", {}).values()
        for dev in devices
        if not dev.get("isAvailable", True) and dev.get("udid")
    ]
    return ProbeResult(roots=roots, note=f"{len(roots)} simulator(s) for runtimes no longer installed")


def _probe_darwin_user_cache(ctx: Context) -> ProbeResult:
    res = ctx.run(["getconf", "DARWIN_USER_CACHE_DIR"], timeout=10)
    if res is None or res.returncode != 0 or not res.stdout.strip():
        return ProbeResult()
    return ProbeResult(roots=[res.stdout.strip().rstrip("/")])


LEFTOVER_PARENTS = ("~/Library/Containers", "~/Library/Group Containers", "~/Library/Application Scripts",
                    "~/Library/Saved Application State", "~/Library/HTTPStorages", "~/Library/WebKit",
                    "~/Library/Application Support")


def _probe_leftovers(ctx: Context) -> ProbeResult:
    """Library folders named after apps that are no longer installed."""
    from .apps import belongs_to_installed, bundle_id_of, installed_apps, installed_somewhere, is_apple

    ids, _names = installed_apps(ctx)
    if len(ids) < 5:  # can't see /Applications (tests, odd setups): don't guess
        return ProbeResult()
    roots = []
    for parent in LEFTOVER_PARENTS:
        base = ctx.path(parent)
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            bid = bundle_id_of(name)
            if bid and not is_apple(bid) and not belongs_to_installed(bid, ids):
                roots.append((bid, os.path.join(base, name)))
    # second opinion from Spotlight, which knows apps and helpers installed anywhere on the Mac
    verdict: Dict[str, bool] = {}
    for bid, _p in roots:
        if bid not in verdict:
            verdict[bid] = installed_somewhere(bid, ctx)
    found = [p for bid, p in roots if not verdict[bid]]
    return ProbeResult(roots=found, note=f"{len(found)} folder(s) from apps that are no longer installed")


def _probe_disk_images(ctx: Context) -> ProbeResult:
    """Disk images in the home folder (not Downloads - that has its own rule) via Spotlight, else a shallow look."""
    roots: List[str] = []
    home = ctx.home
    from .apps import spotlight_ok
    res = ctx.run(["mdfind", "-onlyin", home, "kMDItemContentType == 'com.apple.disk-image-udif' || "
                   "kMDItemFSName == '*.dmg'"], timeout=30, as_user=True) if spotlight_ok(ctx) else None
    if res is not None and res.returncode == 0:
        roots = [p for p in res.stdout.splitlines() if p.lower().endswith((".dmg", ".sparseimage"))]
    else:
        for folder in ("Desktop", "Documents", "Movies", "Music", "Pictures", "Public"):
            for depth in ("*", "*/*"):
                roots += glob.glob(os.path.join(home, folder, depth + ".dmg"))
    skip = (os.path.join(home, "Downloads") + os.sep, os.path.join(home, "Library") + os.sep,
            os.path.join(home, ".Trash") + os.sep)
    roots = [p for p in dict.fromkeys(roots)
             if not p.startswith(skip) and os.path.isfile(p) and ".app/" not in p and ".photoslibrary/" not in p]
    return ProbeResult(roots=roots)


def _probe_volume_trash(ctx: Context) -> ProbeResult:
    roots = glob.glob(os.path.join(ctx.path("/Volumes"), "*", ".Trashes", str(ctx.uid)))
    return ProbeResult(roots=[r for r in roots if not os.path.islink(os.path.dirname(os.path.dirname(r)))])


# --------------------------------------------------------------------------- catalog

SYS = "System & backups"
APPS = "App caches & logs"
XCODE = "Xcode & Apple dev"
PKG = "Package managers"
VM = "Docker, VMs & emulators"
AI = "AI / ML models"
GAME = "Game dev"

R = Rule
S, C, V = Safety.SAFE, Safety.CAUTION, Safety.REVIEW
DEL, CON, CMD, REP = Action.DELETE, Action.DELETE_CONTENTS, Action.COMMAND, Action.REPORT

ELECTRON_CACHE_DIRS = (
    "Cache", "Code Cache", "GPUCache", "CachedData", "DawnCache", "DawnGraphiteCache",
    "DawnWebGPUCache", "GrShaderCache", "ShaderCache", "Crashpad/completed",
)

# cache folders inside browser/Electron profiles (Chrome/Brave/Edge/Arc "Default", "Profile 1"...)
PROFILE_CACHE_DIRS = ("Code Cache", "GPUCache", "Service Worker/CacheStorage", "Service Worker/ScriptCache",
                      "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache", "GrShaderCache", "ShaderCache")

BUILTIN_RULES: List[Rule] = [
    # ---------------------------------------------------------------- system
    R("tm-snapshots", "Time Machine local snapshots", SYS, C, CMD,
      commands=(("tmutil", "deletelocalsnapshots", "/"),), command_needs_root=True,
      probe=_probe_tm_snapshots, handler=_delete_tm_snapshots,
      why="macOS keeps hourly APFS snapshots on the internal disk while your backup drive is away. "
          "They are counted as System Data and are the #1 cause of a huge System Data number.",
      impact="Removes local restore points only. Backups on your Time Machine drive are untouched."),
    R("user-logs", "User logs & crash reports", APPS, S, CON, keep_dirs=True,
      paths=("~/Library/Logs",), exclude=("macsmartcleaner*",),
      why="Application logs, crash and diagnostic reports.",
      impact="None; apps create new logs as needed."),
    R("system-logs", "System logs & diagnostic reports", APPS, S, CON, keep_dirs=True, min_age_days=1,
      paths=("/Library/Logs", "/private/var/log/DiagnosticMessages"),
      needs_root=True, why="System-wide crash and diagnostic reports.", impact="None."),
    R("system-caches", "System-wide caches", APPS, C, CON,
      paths=("/Library/Caches",), needs_root=True, min_age_days=3,
      why="Caches shared by all users; macOS and installers write here.",
      impact="Rebuilt on demand; first launch of some apps may be slower."),
    R("ios-updates", "iPhone/iPad firmware downloads", SYS, S, CON,
      paths=("~/Library/iTunes/iPhone Software Updates", "~/Library/iTunes/iPad Software Updates"),
      why="Old .ipsw firmware files downloaded by Finder/iTunes.", impact="Re-downloaded when needed."),
    R("ios-backups", "iPhone/iPad backups", SYS, V, DEL,
      paths=("~/Library/Application Support/MobileSync/Backup/*",),
      why="Full local device backups; each one can be 10-200 GB.",
      impact="You lose that backup. Keep the newest one, or back up to iCloud instead."),
    R("mail-downloads", "Mail attachment previews", SYS, S, CON,
      paths=("~/Library/Containers/com.apple.mail/Data/Library/Mail Downloads",),
      why="Copies of attachments you opened from Mail.", impact="Attachments stay in Mail."),
    R("trash", "Trash", SYS, C, CON, paths=("~/.Trash",),
      why="Files you already deleted.", impact="Permanently deleted."),
    R("installers", "Leftover installers in /Library/Updates", SYS, V, REP,
      paths=("/Library/Updates",), needs_root=True,
      why="Staged macOS updates.", impact="Let Software Update manage this; install or cancel pending updates."),

    # ---------------------------------------------------------------- app caches
    R("user-caches", "App caches (~/Library/Caches)", APPS, S, CON,
      paths=("~/Library/Caches",),
      exclude=("com.apple.bird", "CloudKit", "com.apple.nsurlsessiond", "com.apple.containermanagerd",
               "com.apple.HomeKit", "com.apple.homed", "com.apple.findmy*", "FamilyCircle",
               "com.apple.akd", "macsmartcleaner*"),
      why="Per-app caches: browsers, Spotify, Slack, Adobe, build tools and more.",
      impact="Rebuilt automatically. Quit big apps first for the cleanest result."),
    R("sandbox-caches", "Sandboxed app caches", APPS, C, CON,
      paths=("~/Library/Containers/*/Data/Library/Caches",),
      why="Caches of App Store / sandboxed apps (inside ~/Library/Containers).",
      impact="Rebuilt automatically. macOS may ask Terminal for permission to touch other apps' data."),
    R("electron-caches", "Electron/Chromium app caches (Slack, Discord, VS Code, Teams...)", APPS, C, CON,
      paths=tuple(f"~/Library/Application Support/*/{d}" for d in ELECTRON_CACHE_DIRS)
      + tuple(f"~/Library/Application Support/*/{lvl}{d}" for lvl in ("*/", "*/*/") for d in PROFILE_CACHE_DIRS)
      + ("~/Library/Application Support/*/Service Worker/CacheStorage",
         "~/Library/Application Support/*/Partitions/*/Cache",
         "~/Library/Application Support/*/Partitions/*/Service Worker/CacheStorage"),
      quit_apps=("Slack", "Discord", "Code", "Cursor", "Microsoft Teams", "Notion", "Figma", "Spotify"),
      why="Chromium-based apps keep large HTTP, GPU and service-worker caches outside ~/Library/Caches.",
      impact="Rebuilt automatically. Quit those apps before cleaning."),
    R("editor-vsix", "VS Code / Cursor cached extension installers", APPS, S, CON,
      paths=("~/Library/Application Support/Code/CachedExtensionVSIXs",
             "~/Library/Application Support/Cursor/CachedExtensionVSIXs"),
      why="Old .vsix packages kept after installing extensions.", impact="None."),
    R("xdg-cache", "Generic ~/.cache", APPS, C, CON, paths=("~/.cache",),
      why="Cross-platform tools store caches here (the specific AI/model ones are listed separately).",
      impact="Rebuilt/re-downloaded by the tools that use it."),
    R("darwin-user-cache", "Per-user system cache (/var/folders/.../C)", APPS, C, CON,
      probe=_probe_darwin_user_cache, min_age_days=3,
      why="Hidden per-user cache dir used by macOS and compilers (clang module cache, Metal shaders...).",
      impact="Rebuilt automatically; only items untouched for 3+ days are removed."),

    # ---------------------------------------------------------------- xcode
    R("xcode-deriveddata", "Xcode DerivedData", XCODE, S, CON,
      paths=("~/Library/Developer/Xcode/DerivedData",), quit_apps=("Xcode",),
      why="Build intermediates and indexes for every project you've opened.",
      impact="Next build of each project is a full build."),
    R("xcode-device-support", "Xcode device support files", XCODE, S, DEL,
      paths=("~/Library/Developer/Xcode/iOS DeviceSupport/*",
             "~/Library/Developer/Xcode/watchOS DeviceSupport/*",
             "~/Library/Developer/Xcode/tvOS DeviceSupport/*",
             "~/Library/Developer/Xcode/visionOS DeviceSupport/*"),
      why="Symbol caches (2-5 GB each) for every iOS version of every device you've plugged in.",
      impact="Re-created the next time you connect that device."),
    R("xcode-archives", "Xcode archives", XCODE, V, DEL,
      paths=("~/Library/Developer/Xcode/Archives/*/*.xcarchive",),
      why="App builds you archived for distribution.",
      impact="You lose the dSYMs needed to symbolicate crashes for those builds."),
    R("xcode-misc", "Xcode logs, previews & doc cache", XCODE, S, CON,
      paths=("~/Library/Developer/Xcode/iOS Device Logs", "~/Library/Developer/Xcode/UserData/Previews",
             "~/Library/Developer/Xcode/DocumentationCache", "~/Library/Developer/Xcode/Products"),
      why="Device logs, SwiftUI preview simulators, docs cache.", impact="Rebuilt when needed."),
    R("simulator-caches", "Simulator caches", XCODE, S, CON,
      paths=("~/Library/Developer/CoreSimulator/Caches",),
      why="dyld shared caches built for simulator runtimes.", impact="Rebuilt on next simulator boot."),
    R("simulator-unavailable", "Orphaned simulators", XCODE, S, CMD,
      commands=(("xcrun", "simctl", "delete", "unavailable"),), probe=_probe_unavailable_sims, probe_async=True,
      why="Simulator devices whose iOS runtime is no longer installed - they can never boot again.",
      impact="None."),
    R("simulator-devices", "All simulator devices", XCODE, V, REP,
      paths=("~/Library/Developer/CoreSimulator/Devices",),
      why="Every simulator's disk (apps, data). Grows with every app you run in the simulator.",
      impact="Run `xcrun simctl erase all` to reset them, or delete unused ones in Xcode > Devices."),
    R("simulator-runtimes", "Simulator runtimes", XCODE, V, REP,
      paths=("/Library/Developer/CoreSimulator/Images", "/Library/Developer/CoreSimulator/Cryptex"),
      why="Each installed iOS/watchOS/visionOS runtime is 5-10 GB.",
      impact="Remove old ones in Xcode > Settings > Components, or `xcrun simctl runtime delete <id>`."),

    # ---------------------------------------------------------------- package managers
    R("homebrew", "Homebrew downloads & old versions", PKG, S, CMD,
      paths=("~/Library/Caches/Homebrew",), commands=(("brew", "cleanup", "--prune=all", "-s"),),
      why="Downloaded bottles and outdated formula versions.", impact="None."),
    R("npm", "npm cache", PKG, S, CON, paths=("~/.npm/_cacache", "~/.npm/_npx"),
      why="Every package tarball npm ever downloaded.", impact="Re-downloaded on next install."),
    R("yarn", "Yarn cache", PKG, S, CON, paths=("~/Library/Caches/Yarn", "~/.yarn/berry/cache"),
      why="Yarn's offline package mirror.", impact="Re-downloaded on next install."),
    R("pnpm", "pnpm store", PKG, S, CMD, paths=("~/Library/pnpm/store",),
      commands=(("pnpm", "store", "prune"),),
      why="Content-addressed package store.", impact="Only unreferenced packages are removed."),
    R("bun", "Bun cache", PKG, S, CON, paths=("~/.bun/install/cache",),
      why="Bun's package cache.", impact="Re-downloaded on next install."),
    R("pip", "pip / Poetry / uv caches", PKG, S, CON,
      paths=("~/Library/Caches/pip", "~/.cache/pip", "~/Library/Caches/pypoetry", "~/.cache/uv",
             "~/Library/Caches/pipenv"),
      why="Downloaded wheels and built packages.", impact="Re-downloaded on next install."),
    R("conda", "Conda package cache", PKG, S, CMD,
      paths=("~/miniconda3/pkgs", "~/anaconda3/pkgs", "~/miniforge3/pkgs", "~/mambaforge/pkgs",
             "~/opt/anaconda3/pkgs", "~/opt/miniconda3/pkgs"),
      commands=(("{root}/../bin/conda", "clean", "--all", "--yes"),),
      why="Every package tarball conda downloaded for every environment.",
      impact="Environments keep working; unused tarballs and packages are removed."),
    R("cocoapods", "CocoaPods cache", PKG, S, CON, paths=("~/Library/Caches/CocoaPods",),
      why="Downloaded pod specs and sources.", impact="Re-downloaded on next pod install."),
    R("gradle", "Gradle caches", PKG, S, CON, paths=("~/.gradle/caches",),
      why="Android/Java/Kotlin build caches and dependencies.", impact="Re-downloaded on next build."),
    R("gradle-dists", "Gradle wrapper distributions", PKG, C, CON, paths=("~/.gradle/wrapper/dists",),
      why="One full Gradle install per version your projects used.", impact="Re-downloaded per project."),
    R("maven", "Maven repository", PKG, C, CON, paths=("~/.m2/repository",),
      why="Local Maven artifact cache.", impact="Re-downloaded on next build."),
    R("cargo", "Cargo registry cache", PKG, S, CON,
      paths=("~/.cargo/registry/cache", "~/.cargo/registry/src", "~/.cargo/git/checkouts"),
      why="Downloaded crate sources.", impact="Re-downloaded on next build."),
    R("go", "Go build & module caches", PKG, C, CMD,
      paths=("~/Library/Caches/go-build", "~/go/pkg/mod"),
      commands=(("go", "clean", "-cache", "-modcache"),),
      why="Compiled package cache and downloaded modules.", impact="Re-downloaded/rebuilt on next build."),
    R("pub", "Dart/Flutter pub cache", PKG, C, CON, paths=("~/.pub-cache",),
      why="Downloaded Dart/Flutter packages.", impact="Run `flutter pub get` again in each project."),
    R("composer", "Composer cache", PKG, S, CON,
      paths=("~/Library/Caches/composer", "~/.composer/cache"),
      why="PHP package cache.", impact="Re-downloaded on next install."),
    R("browsers-for-testing", "Playwright / Puppeteer browsers", PKG, C, CON,
      paths=("~/Library/Caches/ms-playwright", "~/.cache/puppeteer", "~/.cache/selenium"),
      why="Full browser builds downloaded for end-to-end tests (hundreds of MB each version).",
      impact="Re-run `npx playwright install` when needed."),

    # ---------------------------------------------------------------- docker & vms
    R("docker", "Docker Desktop disk image", VM, C, CMD,
      paths=("~/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw",),
      commands=(("docker", "system", "prune", "--all", "--force"), ("docker", "builder", "prune", "--all", "--force")),
      why="Every image, stopped container and build cache lives in this one virtual disk. "
          "macOS reports it as System Data.",
      impact="Removes unused images, stopped containers and build cache (not volumes). "
             "Docker must be running. You can also cap the disk size in Docker Desktop > Settings > Resources."),
    R("orbstack", "OrbStack data", VM, V, REP,
      paths=("~/Library/Group Containers/HUGAGRAYAR.dev.orbstack/data",),
      why="OrbStack's container/VM disk.", impact="Use `docker system prune -a` or `orb delete <machine>`."),
    R("colima-lima", "Colima / Lima VMs", VM, V, REP, paths=("~/.colima", "~/.lima"),
      why="Linux VM disks for Colima/Lima.", impact="Use `colima delete` / `limactl delete` for unused VMs."),
    R("virtual-machines", "Virtual machines (Parallels, UTM, VMware)", VM, V, REP,
      paths=("~/Parallels", "~/Virtual Machines.localized",
             "~/Library/Containers/com.utmapp.UTM/Data/Documents"),
      why="Full VM disk images.", impact="Delete VMs you don't use from the VM app."),
    R("android-avd", "Android emulator images", VM, V, DEL, paths=("~/.android/avd/*.avd",),
      why="Each Android emulator device (2-10 GB each).", impact="Recreate in Android Studio > Device Manager."),
    R("android-sdk-images", "Android SDK system images", VM, V, REP,
      paths=("~/Library/Android/sdk/system-images",),
      why="Emulator OS images per API level.", impact="Remove old API levels in Android Studio > SDK Manager."),
    R("android-cache", "Android build cache", VM, S, CON, paths=("~/.android/cache", "~/.android/build-cache"),
      why="Android tooling caches.", impact="None."),

    # ---------------------------------------------------------------- ai / ml
    R("huggingface-models", "Hugging Face models", AI, V, DEL,
      paths=("~/.cache/huggingface/hub/models--*",),
      why="Model weights downloaded by transformers/diffusers. Often 5-50 GB each.",
      impact="Re-downloaded the next time a script loads that model. "
             "`huggingface-cli delete-cache` lets you pick interactively."),
    R("huggingface-datasets", "Hugging Face datasets", AI, V, DEL,
      paths=("~/.cache/huggingface/hub/datasets--*", "~/.cache/huggingface/datasets/*"),
      why="Datasets downloaded by the datasets library.", impact="Re-downloaded when needed."),
    R("huggingface-xet", "Hugging Face download/xet cache", AI, S, CON,
      paths=("~/.cache/huggingface/xet", "~/.cache/huggingface/hub/.locks"),
      why="Transfer chunk cache.", impact="None."),
    R("ollama", "Ollama models", AI, V, REP, paths=("~/.ollama/models",),
      why="Local LLM weights pulled with `ollama pull`.", impact="Use `ollama list` then `ollama rm <model>`."),
    R("lmstudio", "LM Studio models", AI, V, REP,
      paths=("~/.lmstudio/models", "~/.cache/lm-studio/models"),
      why="Local LLM weights.", impact="Delete unused models from LM Studio's My Models tab."),
    R("torch-hub", "PyTorch hub / torchvision weights", AI, V, CON, paths=("~/.cache/torch",),
      why="Pretrained weights downloaded by torch.hub / torchvision.", impact="Re-downloaded when needed."),
    R("ml-misc", "Whisper / Keras / other model caches", AI, V, CON,
      paths=("~/.cache/whisper", "~/.keras/models", "~/.keras/datasets", "~/.cache/clip",
             "~/.cache/ultralytics", "~/.u2net"),
      why="Model weights and datasets cached by ML libraries.", impact="Re-downloaded when needed."),
    R("wandb-cache", "Weights & Biases cache", AI, C, CON,
      paths=("~/.cache/wandb", "~/Library/Application Support/wandb"),
      why="Cached W&B artifacts.", impact="Artifacts stay in the cloud; re-downloaded on use."),

    # ---------------------------------------------------------------- game dev
    R("unity-cache", "Unity package & asset caches", GAME, S, CON,
      paths=("~/Library/Unity/cache", "~/Library/Application Support/Unity/cache"),
      why="Downloaded Unity packages and GI/shader caches shared across projects.",
      impact="Re-downloaded/rebuilt when you open a project."),
    R("unity-asset-store", "Unity Asset Store downloads", GAME, V, REP,
      paths=("~/Library/Unity/Asset Store-5.x",),
      why="Every Asset Store package you downloaded.", impact="Delete old packages in Package Manager > My Assets."),
    R("unity-editors", "Unity editor installs", GAME, V, REP,
      paths=("/Applications/Unity/Hub/Editor/*",),
      why="Each Unity editor version is 5-15 GB with platform modules.",
      impact="Uninstall versions you no longer use from Unity Hub > Installs."),
    R("unreal-ddc", "Unreal Engine shared DerivedDataCache", GAME, C, CON,
      paths=("~/Library/Application Support/Epic/UnrealEngine/Common/DerivedDataCache",
             "~/Library/Application Support/Epic/UnrealEngine/*/DerivedDataCache",
             "~/Library/Caches/com.epicgames.UnrealEngine"),
      why="Compiled shaders and cooked assets shared across Unreal projects. Can reach 50+ GB.",
      impact="Rebuilt when you open projects (first open is slow)."),
    R("unreal-vault", "Epic Launcher vault cache", GAME, C, CON,
      paths=("/Users/Shared/UnrealEngine/Launcher/VaultCache",),
      why="Marketplace/Fab downloads kept after adding them to projects.",
      impact="Re-downloaded if you add the asset to another project."),
    R("godot", "Godot export templates", GAME, V, REP,
      paths=("~/Library/Application Support/Godot/export_templates",),
      why="Export templates for every Godot version (~1 GB each).",
      impact="Remove old versions from Editor > Manage Export Templates."),
    R("steam-shadercache", "Steam shader cache", GAME, S, CON,
      paths=("~/Library/Application Support/Steam/steamapps/shadercache",),
      why="Pre-compiled shaders for games.", impact="Rebuilt when you launch the game."),
    R("steam-caches", "Steam app & download caches", GAME, S, CON,
      paths=("~/Library/Application Support/Steam/appcache", "~/Library/Application Support/Steam/depotcache",
             "~/Library/Application Support/Steam/logs"),
      quit_apps=("steam_osx",), why="Steam's store/library cache, partial download chunks and logs.",
      impact="Rebuilt by Steam. Games are not touched."),

    # ---------------------------------------------------------------- system level (sudo)
    R("spotlight-index", "Spotlight index (.Spotlight-V100)", SYS, V, CMD,
      paths=("/System/Volumes/Data/.Spotlight-V100",), commands=(("mdutil", "-E", "/"),),
      command_needs_root=True, needs_root=False, min_size=5_000_000_000,
      why="Spotlight's search index. It can bloat to tens of GB after big file moves or a corrupted index. "
          "Never delete this folder by hand: that can break Spotlight and Mail search.",
      impact="Erases and rebuilds the index (the right way). Spotlight search is incomplete and the Mac is "
             "busy while it re-indexes, from minutes to hours."),
    R("unified-logs", "System diagnostic logs (/var/db/diagnostics)", SYS, C, CMD,
      paths=("/private/var/db/diagnostics",), commands=(("log", "erase", "--all"),), command_needs_root=True,
      min_size=1_000_000_000,
      why="macOS unified logging store. Usually capped, but logging profiles or crashes can grow it to many GB.",
      impact="Old system logs are gone (only matters when troubleshooting). macOS keeps logging normally."),
    R("rotated-logs", "Old rotated system logs", APPS, S, DEL, needs_root=True, min_age_days=2,
      paths=("/private/var/log/*.gz", "/private/var/log/*.bz2", "/private/var/log/*/*.gz",
             "/private/var/log/*/*.bz2", "/private/var/log/asl/*.asl"),
      why="Compressed, already-rotated system log archives.", impact="None."),
    R("core-dumps", "Crash core dumps (/cores)", SYS, S, DEL, paths=("/cores/core.*",), needs_root=True,
      why="Full memory dumps written when a process crashes with core dumps enabled. Often several GB each.",
      impact="None, unless you were about to debug that crash."),
    R("macos-install-leftovers", "Leftovers from macOS updates", SYS, C, DEL, needs_root=True,
      paths=("/macOS Install Data", "/System/Volumes/Data/macOS Install Data"),
      why="Staging folder left behind by an interrupted or finished macOS update (often 10+ GB).",
      impact="If an update is waiting to install, it will download again."),
    R("macos-installers", "Old 'Install macOS' apps", SYS, V, DEL, trash=True,
      paths=("/Applications/Install macOS *.app",),
      why="Full macOS installer apps (12-15 GB each), kept after upgrading or making a USB installer.",
      impact="Moved to the Trash. Download it again from the App Store if you need to make a boot USB."),
    R("document-versions", "Document version history (.DocumentRevisions-V100)", SYS, V, REP,
      paths=("/System/Volumes/Data/.DocumentRevisions-V100",),
      why="Old versions of documents saved by apps (File > Revert To > Browse All Versions).",
      impact="Managed by macOS. It shrinks when the documents are deleted. Never delete it by hand."),
    R("fsevents", "File system event log (.fseventsd)", SYS, V, REP, paths=("/System/Volumes/Data/.fseventsd",),
      why="Change journal used by Time Machine, Spotlight and backup apps.",
      impact="Managed by macOS. Leave it alone."),
    R("sleep-image", "Sleep image & swap", SYS, V, REP, paths=("/private/var/vm",),
      why="RAM contents saved for hibernation, plus swap files when memory is full.",
      impact="Managed by macOS. A restart shrinks swap, and closing memory-hungry apps prevents it."),
    R("volume-trash", "Trash on external drives", SYS, C, CON, probe=_probe_volume_trash,
      why="Files you deleted from USB/external drives stay in a hidden .Trashes folder on that drive.",
      impact="Permanently deleted from those drives."),

    # ---------------------------------------------------------------- more user junk
    R("app-leftovers", "Leftovers of uninstalled apps", APPS, V, DEL, trash=True, probe=_probe_leftovers,
      why="Settings, containers and data folders named after apps that are no longer installed "
          "(what 'uninstaller' tools remove).",
      impact="Moved to the Trash, so you can put anything back. If you reinstall the app, it starts fresh."),
    R("saved-app-state", "Saved window state", APPS, C, CON, paths=("~/Library/Saved Application State",),
      min_age_days=7, why="Window positions apps restore when reopened. Grows with apps you no longer use.",
      impact="Apps reopen without their previous windows. Items used in the last week are kept."),
    R("old-installers", "Old installers in Downloads (.dmg/.pkg/.xip)", SYS, C, DEL, trash=True, min_age_days=14,
      paths=("~/Downloads/*.dmg", "~/Downloads/*.pkg", "~/Downloads/*.mpkg", "~/Downloads/*.xip"),
      why="Disk images and installers you already used. Apps are installed; the installers are not needed.",
      impact="Moved to the Trash. Only files older than 2 weeks."),
    R("adobe-media-cache", "Adobe media cache (Premiere, After Effects)", APPS, C, CON,
      paths=("~/Library/Application Support/Adobe/Common/Media Cache Files",
             "~/Library/Application Support/Adobe/Common/Media Cache",
             "~/Library/Application Support/Adobe/Common/Peak Files"),
      quit_apps=("Adobe Premiere Pro", "After Effects", "Adobe Media Encoder"),
      why="Conformed audio and indexed media for every project you've edited. Commonly 20-100 GB.",
      impact="Rebuilt when you open a project (first open is slower)."),
    R("spotify-cache", "Spotify streaming cache", APPS, C, CON,
      paths=("~/Library/Application Support/Spotify/PersistentCache",), quit_apps=("Spotify",),
      why="Songs Spotify cached while streaming (can reach 10 GB).",
      impact="Downloaded (offline) playlists must be downloaded again."),
    R("telegram-media", "Telegram media cache", APPS, C, CON,
      paths=("~/Library/Group Containers/*.ru.keepcoder.Telegram/*/account-*/postbox/media",
             "~/Library/Application Support/Telegram Desktop/tdata/user_data"),
      quit_apps=("Telegram",), why="Photos and videos Telegram cached from your chats.",
      impact="Media is still in the cloud and re-downloads when you open a chat."),
    R("simulator-app-caches", "Caches inside simulators", XCODE, S, CON,
      paths=("~/Library/Developer/CoreSimulator/Devices/*/data/Library/Caches",),
      why="Caches of apps you ran in the iOS simulator.", impact="Rebuilt by those apps."),
    R("configurator-firmware", "Apple Configurator firmware", SYS, S, CON,
      paths=("~/Library/Group Containers/K36BKF7T3D.group.com.apple.configurator/Library/Caches/Firmware",),
      why="Device firmware downloaded by Apple Configurator.", impact="Re-downloaded when needed."),
    R("old-ios-apps", "Old iOS app backups (.ipa)", SYS, C, CON,
      paths=("~/Music/iTunes/iTunes Media/Mobile Applications",),
      why="iPhone apps iTunes kept from the old days of syncing apps.", impact="None; apps come from the App Store."),
    R("messages-attachments", "Messages attachments", SYS, V, REP, paths=("~/Library/Messages/Attachments",),
      why="Photos, videos and files from your conversations.",
      impact="Review in System Settings > General > Storage > Messages, or set Keep messages: 1 Year."),
    R("nvm-cache", "nvm download cache", PKG, S, CON, paths=("~/.nvm/.cache",),
      why="Node.js tarballs nvm downloaded.", impact="Re-downloaded when installing a Node version."),
    R("rustup-toolchains", "Rust toolchains", PKG, V, REP, paths=("~/.rustup/toolchains",),
      why="Every Rust toolchain you installed (~1 GB each).",
      impact="Use `rustup toolchain list` and `rustup toolchain uninstall <name>`."),
    R("unused-disk-images", "Unused disk images (.dmg) in your folders", SYS, C, DEL, trash=True,
      probe=_probe_disk_images, min_age_days=30,
      why="Disk images anywhere in your home folder that haven't been opened for a month "
          "(installers you already used, old backups).",
      impact="Moved to the Trash. Open one again before emptying the Trash if you're unsure."),
    R("deleted-users", "Deleted user accounts", SYS, V, DEL, needs_root=True, paths=("/Users/Deleted Users/*.dmg",),
      why="Home folders of deleted accounts that macOS kept as disk images.",
      impact="That account's files are gone for good."),
    R("unreal-zen", "Unreal Engine Zen cache", GAME, C, CON,
      paths=("~/Library/Application Support/Epic/Zen/Data",),
      why="Unreal Engine 5.4+ local shared cache server data (cooked assets, shaders).",
      impact="Rebuilt when you open projects (first open is slower)."),
    R("gem-cache", "Ruby gem cache", PKG, S, CON, paths=("~/.gem/ruby/*/cache", "~/.gem/specs"),
      why="Downloaded .gem files.", impact="Re-downloaded when installing gems."),
]


def load_rules(extra_file: Optional[str] = None) -> List[Rule]:
    """Built-in rules plus optional user rules from a JSON file.

    User rule format (list of objects)::

      [{"id": "my-tool", "name": "My tool cache", "category": "Custom",
        "safety": "safe", "action": "delete-contents", "paths": ["~/.mytool/cache"]}]
    """
    rules = list(BUILTIN_RULES)
    if extra_file and os.path.exists(extra_file):
        with open(extra_file) as fh:
            for raw in json.load(fh):
                rules.append(Rule(
                    id=raw["id"], name=raw.get("name", raw["id"]), category=raw.get("category", "Custom"),
                    safety=Safety(raw.get("safety", "review")), action=Action(raw.get("action", "report-only")),
                    paths=tuple(raw.get("paths", ())), exclude=tuple(raw.get("exclude", ())),
                    commands=tuple(tuple(c) for c in raw.get("commands", ())),
                    min_age_days=int(raw.get("min_age_days", 0)), why=raw.get("why", ""),
                    impact=raw.get("impact", ""),
                ))
    ids = [r.id for r in rules]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate rule ids: {sorted(dupes)}")
    return rules


def default_user_rules_path(ctx: Context) -> str:
    return ctx.path("~/.config/macsmartcleaner/rules.json")
