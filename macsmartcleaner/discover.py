"""Heuristic discovery for things no rule knows about.

* ``find_space_hogs``   - big folders in ~/Library, dot-dirs, /Library, /private/var
                          that aren't covered by a rule, classified by name/age and
                          by whether the app that created them is still installed.
* ``find_project_artifacts`` - regenerable build output inside your projects
                          (node_modules, Unity Library, Unreal Intermediate, .venv...).
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .apps import installed_apps
from .context import Context
from .rules import Action, Rule, Safety
from .scanner import Finding, Target, measure_all
from .sizes import Usage
from .ui import NULL, Reporter

# --------------------------------------------------------------------------- space hogs

HOG_PARENTS = [
    "~/Library", "~/Library/Application Support", "~/Library/Containers", "~/Library/Group Containers",
    "~/Library/Developer", "~", "/Library", "/Library/Application Support",
    "/private/var", "/private/var/db", "/System/Volumes/Data", "/Users/Shared",
]
# measured by drilling into them instead of as one blob
DRILL = {"~/Library/Application Support", "~/Library/Containers", "~/Library/Group Containers",
         "~/Library/Caches", "~/Library/Developer", "/Library/Application Support"}
# Cloud-synced folders: walking them can force downloads or hang on the file
# provider, and their content is the user's files, not System Data junk.
NEVER_WALK = {"~/Library/CloudStorage", "~/Library/Mobile Documents"}
USER_DATA = {"Documents", "Desktop", "Downloads", "Pictures", "Movies", "Music", "Public",
             "Applications", "Library", "Sites", "Parallels"}

CACHE_WORDS = re.compile(r"cache|caches|tmp|temp|shadercache|gpucache|crashpad|blob_storage|deriveddata", re.I)
LOG_WORDS = re.compile(r"(^|[._ -])(logs?|diagnostic|crash ?reports?)($|[._ -])", re.I)
BIG_DATA_WORDS = re.compile(r"backup|models?|datasets?|vm|virtual|simulator|\.raw$|\.img$|\.dmg$", re.I)

EXPLAIN: Dict[str, str] = {
    "/private/var/vm": "Swap & sleep image - managed by macOS, shrinks after a restart.",
    "/private/var/folders": "Per-user temp/caches - reboot clears much of it; `msc` cleans the safe part.",
    "/private/var/db": "System databases (Spotlight, updates, diagnostics) - leave alone; "
                       "if huge, a restart or Safe Mode boot trims it.",
    "/private/var/db/diagnostics": "Unified system logs - `msc` can erase them (rule unified-logs).",
    "/private/var/db/uuidtext": "Symbol data for the unified logs - shrinks together with them.",
    "/private/var/db/Spotlight-V100": "Spotlight's system index - rebuild it via Optimize > Rebuild Spotlight.",
    "/private/var/db/dyld": "Shared library cache - managed by macOS.",
    "/private/var/db/oah": "Rosetta 2 translation cache - macOS rebuilds it; it shrinks after a restart.",
    "/System/Volumes/Data/.Spotlight-V100": "Spotlight index - use rule spotlight-index to rebuild (never delete by hand).",
    "/System/Volumes/Data/.DocumentRevisions-V100": "Document version history - managed by macOS.",
    "/System/Volumes/Data/.fseventsd": "File-change journal for Time Machine/Spotlight - leave alone.",
    "/System/Volumes/Data/.MobileBackups": "Old-style local Time Machine data - thin snapshots via tm-snapshots.",
    "~/Library/Mail": "Mail messages & attachments - reduce by deleting big mails or 'Download attachments: None'.",
    "~/Library/Messages": "iMessage attachments - Settings > General > Storage > Messages to review.",
    "~/Library/Mobile Documents": "iCloud Drive local copies - enable 'Optimize Mac Storage'.",
    "~/Library/CloudStorage": "Dropbox/OneDrive/Google Drive local copies - switch to online-only files.",
    "~/Library/Photos": "Photos library data.",
    "~/Library/Metadata": "Spotlight/CoreSpotlight index - `sudo mdutil -E /` rebuilds it smaller if bloated.",
}


@dataclass
class Hog:
    path: str
    display: str
    usage: Usage
    verdict: str   # likely-junk | orphaned | stale | data | system | unknown
    reason: str


def _looks_orphaned(name: str, parent: str, ids: Set[str], names: Set[str]) -> bool:
    n = name.lower()
    if n.startswith(("com.apple.", "group.com.apple.", "apple")) or not ids:
        return False
    if parent.endswith(("Containers", "Group Containers")):
        # bundle id (or TEAMID.group.bundle.id); match on any installed id sharing the vendor+product
        core = re.sub(r"^[a-z0-9]{10}\.", "", n)
        core = core.replace("group.", "")
        return not any(core.startswith(i) or i.startswith(core) for i in ids) and \
            not any(part in names for part in core.split(".")[1:] if len(part) > 3)
    if parent.endswith("Application Support"):
        if "." in n:  # bundle-id-style folder
            return not any(n.startswith(i) or i.startswith(n) for i in ids)
        return n not in names and not any(n in x or x in n for x in names if len(x) > 3)
    return False


def classify(name: str, parent: str, u: Usage, ctx: Context, ids: Set[str], names: Set[str]) -> Tuple[str, str]:
    age = ctx.days_since(u.newest_mtime)
    if CACHE_WORDS.search(name):
        return "likely-junk", "name says it's a cache/temp folder"
    if LOG_WORDS.search(name):
        return "likely-junk", "name says it's logs/diagnostics"
    if _looks_orphaned(name, parent, ids, names):
        return "orphaned", "no installed app matches it - probably left over from an uninstalled app"
    if age > 365:
        return "stale", f"nothing inside changed for {int(age)} days"
    if BIG_DATA_WORDS.search(name):
        return "data", "looks like backups/models/VM data - review before deleting"
    if age > 180:
        return "stale", f"nothing inside changed for {int(age)} days"
    return "unknown", "in active use"


def find_space_hogs(ctx: Context, findings: Sequence[Finding], min_size: int = 1_000_000_000,
                    workers: Optional[int] = None, reporter: Reporter = NULL) -> List[Hog]:
    covered = [os.path.normpath(f.root) for f in findings if f.root] + [
        os.path.normpath(t.path) for f in findings for t in f.targets]

    candidates: List[Tuple[str, str]] = []  # (path, parent pattern)
    for parent in HOG_PARENTS:
        base = ctx.path(parent)
        try:
            entries = list(os.scandir(base))
        except OSError:
            continue
        for e in entries:
            try:
                if not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            rel = parent + "/" + e.name if parent != "~" else "~/" + e.name
            if rel in DRILL or rel in HOG_PARENTS or rel in NEVER_WALK:
                continue
            if parent == "~" and (e.name in USER_DATA or not e.name.startswith(".")):
                continue  # only dot-dirs in home; your own folders aren't "System Data"
            if parent == "~" and e.name in (".Trash",):
                continue
            if parent == "/System/Volumes/Data" and not e.name.startswith("."):
                continue  # everything else there is the same data seen via /Users, /Library...
            if parent == "/private/var" and e.name == "db":
                continue  # drilled into separately
            candidates.append((e.path, parent))

    reporter.begin("hogs", "Hunting for unknown big folders", total=len(candidates))
    measured = measure_all([c[0] for c in candidates], reporter, workers)
    usages = [measured[c[0]] for c in candidates]

    ids, names = installed_apps(ctx)
    hogs: List[Hog] = []
    for (path, parent), u in zip(candidates, usages):
        if u.bytes < min_size:
            continue
        norm = os.path.normpath(path)
        covered_bytes = 0
        for f in findings:
            for t in f.targets or ([Target(f.root, Usage(bytes=f.size))] if f.root else []):
                tp = os.path.normpath(t.path)
                if tp == norm or tp.startswith(norm + os.sep):
                    covered_bytes += t.usage.bytes
        if any(norm == c or norm.startswith(c + os.sep) for c in covered):
            continue  # entirely inside something a rule already handles
        u.bytes -= covered_bytes  # report only what rules don't already account for
        if u.bytes < min_size:
            continue
        display = _display(ctx, path)
        if display in EXPLAIN:
            verdict, reason = "system", EXPLAIN[display]
        elif parent.startswith(("/private/var", "/System/Volumes")):
            verdict, reason = "system", "macOS system data - don't delete by hand"
        else:
            verdict, reason = classify(os.path.basename(path), ctx.path(parent), u, ctx, ids, names)
        hogs.append(Hog(path, display, u, verdict, reason))
    hogs.sort(key=lambda h: h.usage.bytes, reverse=True)
    reporter.end(f"{len(hogs)} big folder(s) worth a look")
    return hogs


def _display(ctx: Context, path: str) -> str:
    home = ctx.home.rstrip("/")
    if path == home or path.startswith(home + "/"):
        return "~" + path[len(home):]
    if ctx.root != "/" and path.startswith(ctx.root):
        return "/" + os.path.relpath(path, ctx.root)
    return path


# --------------------------------------------------------------------------- project artifacts

@dataclass(frozen=True)
class ArtifactKind:
    dirname: str
    label: str
    markers: Tuple[str, ...]  # sibling files/dirs (glob) that must exist in the project root
    inside: Tuple[str, ...] = ()  # files that must exist inside the artifact dir


ARTIFACTS: List[ArtifactKind] = [
    ArtifactKind("node_modules", "Node.js dependencies", ("package.json",)),
    ArtifactKind(".next", "Next.js build cache", ("package.json",)),
    ArtifactKind(".nuxt", "Nuxt build cache", ("package.json",)),
    ArtifactKind(".turbo", "Turborepo cache", ("package.json",)),
    ArtifactKind(".parcel-cache", "Parcel cache", ("package.json",)),
    ArtifactKind(".angular", "Angular cache", ("angular.json",)),
    ArtifactKind(".svelte-kit", "SvelteKit build", ("package.json",)),
    ArtifactKind(".venv", "Python virtualenv", (), ("pyvenv.cfg",)),
    ArtifactKind("venv", "Python virtualenv", (), ("pyvenv.cfg",)),
    ArtifactKind("env", "Python virtualenv", (), ("pyvenv.cfg",)),
    ArtifactKind("target", "Rust build output", ("Cargo.toml",)),
    ArtifactKind("Library", "Unity import cache (Library/)", ("Assets", "ProjectSettings")),
    ArtifactKind("Temp", "Unity temp", ("Assets", "ProjectSettings")),
    ArtifactKind("obj", "Unity/.NET intermediates", ("Assets", "ProjectSettings")),
    ArtifactKind("Intermediate", "Unreal intermediates", ("*.uproject",)),
    ArtifactKind("DerivedDataCache", "Unreal project DDC", ("*.uproject",)),
    ArtifactKind(".godot", "Godot import cache", ("project.godot",)),
    ArtifactKind("Pods", "CocoaPods", ("Podfile",)),
    ArtifactKind("build", "Gradle build output", ("build.gradle", "build.gradle.kts")),
    ArtifactKind(".gradle", "Gradle project cache", ("settings.gradle", "settings.gradle.kts")),
    ArtifactKind(".dart_tool", "Dart tool cache", ("pubspec.yaml",)),
    ArtifactKind("DerivedData", "Xcode local DerivedData", ("*.xcodeproj", "*.xcworkspace")),
]
_BY_NAME: Dict[str, List[ArtifactKind]] = {}
for _k in ARTIFACTS:
    _BY_NAME.setdefault(_k.dirname, []).append(_k)

PROJECT_ROOT_GUESSES = ["~/Developer", "~/Projects", "~/projects", "~/Code", "~/code", "~/src", "~/dev",
                        "~/Documents", "~/Desktop", "~/Downloads", "~/workspace", "~/Workspace",
                        "~/Unity", "~/Unity Projects", "~/Documents/Unreal Projects", "~/GitHub",
                        "~/repos", "~/git"]
SKIP_DIRS = {".git", ".hg", ".svn", "Library", "Applications", ".Trash", "node_modules", "site-packages", "Pods"}


def _match_artifact(project: str, name: str) -> Optional[ArtifactKind]:
    for kind in _BY_NAME.get(name, []):
        if kind.markers and not any(glob.glob(os.path.join(glob.escape(project), m)) for m in kind.markers):
            continue
        if kind.inside and not all(os.path.exists(os.path.join(project, name, f)) for f in kind.inside):
            continue
        return kind
    return None


def _project_last_touched(project: str, artifact_names: Set[str]) -> float:
    newest = 0.0
    try:
        with os.scandir(project) as it:
            for e in it:
                if e.name in artifact_names or e.name == ".DS_Store":
                    continue
                try:
                    newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
                except OSError:
                    pass
    except OSError:
        pass
    return newest


def find_project_artifacts(ctx: Context, roots: Optional[Iterable[str]] = None, max_depth: int = 7,
                           workers: Optional[int] = None, reporter: Reporter = NULL) -> List[Finding]:
    roots = [ctx.path(r) for r in (roots or PROJECT_ROOT_GUESSES)]
    seen_roots: Set[str] = set()
    hits: List[Tuple[str, str, ArtifactKind]] = []  # (project dir, artifact path, kind)
    reporter.begin("projects", "Finding project build folders (node_modules, Library, .venv...)")
    for i, root in enumerate(roots):
        reporter.fraction(0.5 * i / max(len(roots), 1))
        real = os.path.realpath(root)
        if not os.path.isdir(real) or any(real == s or real.startswith(s + "/") for s in seen_roots):
            continue
        seen_roots.add(real)
        base_depth = real.rstrip("/").count("/")
        for dirpath, dirnames, _files in os.walk(real, followlinks=False):
            reporter.current(dirpath)
            if dirpath.count("/") - base_depth >= max_depth:
                dirnames[:] = []
                continue
            keep = []
            for d in dirnames:
                kind = _match_artifact(dirpath, d)
                if kind:
                    hits.append((dirpath, os.path.join(dirpath, d), kind))
                elif d not in SKIP_DIRS and not d.startswith(".") and not d.endswith((".app", ".photoslibrary")):
                    keep.append(d)
            dirnames[:] = keep

    reporter.fraction(0.5)
    measured = _measure_fraction([h[1] for h in hits], reporter, workers)
    usages = [measured[h[1]] for h in hits]

    findings: List[Finding] = []
    names = {k.dirname for k in ARTIFACTS}
    for (project, path, kind), u in zip(hits, usages):
        if u.bytes < 1_000_000:
            continue
        rule = Rule(
            id="project:" + kind.dirname.lstrip(".").lower(), name=kind.label, category="Project build artifacts",
            safety=Safety.CAUTION, action=Action.DELETE,
            why="Regenerable build output / dependencies inside one of your projects.",
            impact="Recreated by reinstalling dependencies or rebuilding the project.",
        )
        f = Finding(rule, path, u.bytes, u.files, u.newest_mtime, [Target(path, u)], errors=u.errors)
        f.note = f"project idle {_fmt_days(ctx.days_since(_project_last_touched(project, names)))}"
        f.newest_mtime = _project_last_touched(project, names)
        findings.append(f)
    findings.sort(key=lambda f: f.size, reverse=True)
    reporter.end(f"{len(findings)} found")
    return findings


def _measure_fraction(paths: List[str], reporter: Reporter, workers: Optional[int]) -> Dict[str, Usage]:
    """measure_all, mapping completed paths onto the second half of the stage."""
    done = [0]

    class Half(Reporter):
        def step(self, n=1, current=None):
            done[0] += n
            reporter.fraction(0.5 + 0.5 * done[0] / max(len(paths), 1))

        def count(self, files=0, nbytes=0):
            reporter.count(files, nbytes)

        def current(self, text):
            reporter.current(text)

    return measure_all(paths, Half(), workers)


def _fmt_days(d: float) -> str:
    if d == float("inf"):
        return "unknown"
    return f"{int(d)}d"


# --------------------------------------------------------------------------- large & old files

PACKAGE_EXTS = (".photoslibrary", ".app", ".fcpbundle", ".musiclibrary", ".tvlibrary", ".imovielibrary",
                ".logicx", ".band", ".aplibrary", ".lrdata", ".vmwarevm", ".pvm", ".utm", ".sparsebundle",
                ".xcarchive", ".bundle", ".framework")
SKIP_LARGE = {"Library", ".Trash", "node_modules", ".git", ".cache", ".npm", ".gradle", ".cargo", ".rustup",
              ".ollama", ".lmstudio", ".docker", ".colima", ".lima", ".android"}
OLD_DAYS = 180


def _inside_package(path: str) -> bool:
    return any(part.endswith(PACKAGE_EXTS) for part in path.split(os.sep)[:-1])


def find_large_files(ctx: Context, findings: Sequence[Finding] = (), min_size: int = 500_000_000,
                     reporter: Reporter = NULL) -> List[Hog]:
    """Big files in your home folder (outside Library), marked 'old' if untouched for 6+ months.

    Uses Spotlight (instant) when available, otherwise walks the home folder.
    """
    reporter.begin("large", "Finding large & old files")
    home = ctx.home
    claimed = {os.path.normpath(t.path) for f in findings for t in f.targets}
    candidates: List[str] = []
    from .apps import spotlight_ok
    res = ctx.run(["mdfind", "-onlyin", home, f"kMDItemFSSize >= {min_size}"], timeout=60, as_user=True) \
        if spotlight_ok(ctx) else None
    if res is not None and res.returncode == 0:
        candidates = [ln for ln in res.stdout.splitlines() if ln.strip()]
    else:
        stack = [home]
        while stack:
            d = stack.pop()
            reporter.current(d)
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                try:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if e.name in SKIP_LARGE or e.name.endswith(PACKAGE_EXTS) or \
                                        (e.name.startswith(".") and d == home):
                                    continue
                                stack.append(e.path)
                            elif e.is_file(follow_symlinks=False) and e.stat(follow_symlinks=False).st_size >= min_size:
                                candidates.append(e.path)
                        except OSError:
                            continue
                except OSError:
                    continue
    out: List[Hog] = []
    lib = os.path.join(home, "Library") + os.sep
    for path in candidates:
        if path.startswith(lib) or _inside_package(path) or os.path.normpath(path) in claimed:
            continue
        rel = path[len(home) + 1:] if path.startswith(home + os.sep) else path
        if rel.split(os.sep)[0] in SKIP_LARGE:
            continue
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not os.path.isfile(path) or os.path.islink(path):
            continue
        size = getattr(st, "st_blocks", 0) * 512 or st.st_size
        if size < min_size:
            continue
        last = max(st.st_mtime, st.st_atime)
        days = int(ctx.days_since(last))
        u = Usage(bytes=size, files=1, newest_mtime=last)
        verdict = "old" if days >= OLD_DAYS else "large"
        reason = f"not opened or changed for {days} days" if verdict == "old" else f"last used {days} days ago"
        out.append(Hog(path, _display(ctx, path), u, verdict, reason))
    out.sort(key=lambda h: h.usage.bytes, reverse=True)
    reporter.end(f"{len(out)} file(s) over {min_size // 1_000_000} MB")
    return out
