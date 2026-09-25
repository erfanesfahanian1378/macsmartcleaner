# Contributing

Thanks for helping make Mac storage less mysterious. The most valuable contribution is
**another place where macOS or an app hides gigabytes**.

## Adding a rule

Rules live in [`macsmartcleaner/rules.py`](macsmartcleaner/rules.py) in `BUILTIN_RULES`:

```python
R("figma-cache", "Figma font & file cache", APPS, S, CON,
  paths=("~/Library/Application Support/Figma/DesktopProfile/*/Cache",),
  quit_apps=("Figma",),
  why="Cached file data from the Figma desktop app.",
  impact="Rebuilt automatically when you open files."),
```

| Field | Meaning |
|---|---|
| `id` | unique, kebab-case; users type it in `--only` / `--skip` |
| category | one of the constants at the top of the catalog (`SYS`, `APPS`, `XCODE`, `PKG`, `VM`, `AI`, `GAME`) |
| safety | `S` safe (pure cache/log), `C` caution (costly to rebuild), `V` review (may be user data) |
| action | `DEL` delete each match, `CON` empty the folder, `CMD` run the tool's own cleanup, `REP` report only |
| `paths` | `~`-relative or absolute; globs allowed |
| `commands` | for `CMD` rules, e.g. `(("brew", "cleanup"),)`; `{root}` expands to the matched path |
| `exclude` | child names to keep inside a `CON` folder (fnmatch patterns) |
| `min_age_days` | only remove items not modified for N days |
| `needs_root` | folder is only writable with `sudo` |
| `why` / `impact` | plain English, shown to users. Say what it is and what happens after deleting it |

Guidelines:

- **When in doubt, pick the safer tier.** Anything that could hold data a person made or downloaded on purpose is `review`.
- If a tool has its own cleanup command (`brew cleanup`, `docker system prune`, `pnpm store prune`), use `CMD`. Don't delete its internals.
- Overlap is fine. A specific rule inside a folder that a generic rule covers (e.g. `~/Library/Caches/Foo`) is carved out automatically.
- Paths deleted must pass [`safety.py`](macsmartcleaner/safety.py). If yours is refused, open an issue instead of loosening the deny-list.
- Add a test in `tests/` using the fake-home helpers (`write(self.h("..."))`).

## Development

```bash
git clone https://github.com/erfanesfahanian1378/macsmartcleaner && cd macsmartcleaner
python3 -m unittest discover -s tests -t .    # stdlib only, runs on macOS and Linux
./msc scan                                    # try your change for real (read-only)
./msc clean --dry-run
```

Constraints: **Python 3.9 compatible** (the version bundled with macOS Command Line Tools) and
**no third-party dependencies**.

## Reporting a problem

If `msc` ever deletes something it shouldn't have, please open an issue right away with the
rule id and path. The log is at `~/.local/state/macsmartcleaner/history.jsonl`.
