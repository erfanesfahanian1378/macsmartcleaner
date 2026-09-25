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
import json
import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

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

BUILTIN_RULES: List[Rule] = [
    # ---------------------------------------------------------------- system
    R("tm-snapshots", "Time Machine local snapshots", SYS, C, CMD,
      commands=(("tmutil", "deletelocalsnapshots", "/"),), command_needs_root=True,
      probe=_probe_tm_snapshots,
      why="macOS keeps hourly APFS snapshots on the internal disk while your backup drive is away. "
          "They are counted as System Data and are the #1 cause of a huge System Data number.",
      impact="Removes local restore points only. Backups on your Time Machine drive are untouched."),
    R("user-logs", "User logs & crash reports", APPS, S, CON,
      paths=("~/Library/Logs",), exclude=("macsmartcleaner*",),
      why="Application logs, crash and diagnostic reports.",
      impact="None; apps create new logs as needed."),
    R("system-logs", "System logs & diagnostic reports", APPS, S, CON,
      paths=("/Library/Logs/DiagnosticReports", "/private/var/log/DiagnosticMessages"),
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
      + ("~/Library/Application Support/*/Service Worker/CacheStorage",
         "~/Library/Application Support/*/*/Service Worker/CacheStorage",
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
      commands=(("xcrun", "simctl", "delete", "unavailable"),), probe=_probe_unavailable_sims,
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
