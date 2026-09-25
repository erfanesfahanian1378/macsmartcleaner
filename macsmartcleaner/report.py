"""Terminal, JSON and HTML reports."""
from __future__ import annotations

import html
import json
import os
import shutil
import sys
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Sequence

from .context import Context
from .discover import Hog, _display
from .rules import Action, Safety
from .scanner import Finding
from .sizes import human

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


SAFETY_STYLE = {Safety.SAFE: "32", Safety.CAUTION: "33", Safety.REVIEW: "35"}
VERDICT_STYLE = {"likely-junk": "32", "orphaned": "33", "stale": "33", "data": "35", "system": "36",
                 "unknown": "2"}


def size_str(f: Finding) -> str:
    return "?" if not f.size_known else human(f.size)


def disk_summary(ctx: Context) -> Dict[str, int]:
    du = shutil.disk_usage(ctx.home)
    return {"total": du.total, "used": du.used, "free": du.free}


def tier_totals(findings: Sequence[Finding]) -> Dict[str, int]:
    out = {"safe": 0, "caution": 0, "review": 0, "report-only": 0}
    for f in findings:
        if f.rule.action == Action.REPORT:
            out["report-only"] += f.size
        else:
            out[f.rule.safety.value] += f.size
    return out


def _path(ctx: Context, f: Finding) -> str:
    if f.root:
        return _display(ctx, f.root)
    return f.note


def print_report(ctx: Context, findings: Sequence[Finding], hogs: Sequence[Hog], projects: Sequence[Finding],
                 min_size: int, out=sys.stdout) -> None:
    w = out.write
    d = disk_summary(ctx)
    w("\n" + c("macsmartcleaner", "1") + f"  -  disk {human(d['used'])} used of {human(d['total'])}, "
      f"{human(d['free'])} free\n")

    groups: "OrderedDict[str, List[Finding]]" = OrderedDict()
    for f in findings:
        if f.size >= min_size or not f.size_known:
            groups.setdefault(f.rule.category, []).append(f)
    for cat in sorted(groups, key=lambda k: -sum(x.size for x in groups[k])):
        items = groups[cat]
        w("\n" + c(f"{cat}  ({human(sum(x.size for x in items))})", "1;4") + "\n")
        for f in items:
            tag = f.rule.safety.value if f.rule.action != Action.REPORT else "info"
            style = SAFETY_STYLE[f.rule.safety] if f.rule.action != Action.REPORT else "36"
            extra = []
            if f.note and f.root:
                extra.append(f.note)
            if f.skipped_young:
                extra.append(f"{f.skipped_young} recently-used item(s) kept")
            if f.errors:
                extra.append(f"{f.errors} unreadable")
            w(f"  {size_str(f):>9}  {c(f'{tag:<7}', style)}  {f.rule.id:<24} {_path(ctx, f)}"
              + (c("  [" + "; ".join(extra) + "]", "2") if extra else "") + "\n")

    if projects:
        total = sum(p.size for p in projects)
        w("\n" + c(f"Project build artifacts  ({human(total)})", "1;4") + "\n")
        for p in projects[:25]:
            w(f"  {human(p.size):>9}  {p.note:<16}  {p.rule.name:<28} "
              f"{_display(ctx, p.root or '')}\n")
        if len(projects) > 25:
            w(c(f"  ... and {len(projects) - 25} more (see --json/--html)\n", "2"))

    if hogs:
        w("\n" + c("Other big folders no rule covers (review these yourself)", "1;4") + "\n")
        for h in hogs[:30]:
            w(f"  {human(h.usage.bytes):>9}  {c(f'{h.verdict:<11}', VERDICT_STYLE.get(h.verdict, '0'))} "
              f"{h.display}\n{'':>24}{c(h.reason, '2')}\n")

    t = tier_totals(findings)
    pt = sum(p.size for p in projects)
    snaps = [f for f in findings if not f.size_known]
    w("\n" + c("Summary", "1;4") + "\n")
    w(f"  {c('safe', '32')}     {human(t['safe']):>9}   msc clean                    (caches, logs - regenerated automatically)\n")
    w(f"  {c('caution', '33')}  {human(t['caution']):>9}   msc clean --tier caution     (re-downloads / slower rebuilds)\n")
    w(f"  {c('review', '35')}   {human(t['review']):>9}   msc clean --only <rule-id>   (backups, models, archives - your call)\n")
    if pt:
        w(f"  projects {human(pt):>9}   msc clean --projects --older-than 30\n")
    if snaps:
        w(c(f"\n  ! Time Machine: {snaps[0].note}.\n"
            "    This is very often the biggest hidden chunk of 'System Data'. "
            "Clean with: msc clean --only tm-snapshots\n", "33"))
    w(c("\n  Tip: run `msc` (without --report) to browse, select and delete these interactively.\n", "36"))
    if not ctx.is_root:
        w(c("\n  Tip: some system folders need `sudo` to measure/clean, and Terminal needs\n"
            "  Full Disk Access (System Settings > Privacy & Security) to see everything.\n", "2"))
    w("\n")


# --------------------------------------------------------------------------- json / html

def to_dict(ctx: Context, findings: Sequence[Finding], hogs: Sequence[Hog], projects: Sequence[Finding]) -> dict:
    def fd(f: Finding) -> dict:
        return {
            "rule": f.rule.id, "name": f.rule.name, "category": f.rule.category,
            "safety": f.rule.safety.value, "action": f.rule.action.value,
            "path": _display(ctx, f.root) if f.root else None, "size": f.size if f.size_known else None,
            "files": f.files, "note": f.note, "why": f.rule.why, "impact": f.rule.impact,
            "last_modified_days": None if not f.newest_mtime else round(ctx.days_since(f.newest_mtime)),
        }
    return {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "disk": disk_summary(ctx),
        "totals": tier_totals(findings),
        "findings": [fd(f) for f in findings],
        "projects": [fd(p) for p in projects],
        "hogs": [{"path": h.display, "size": h.usage.bytes, "verdict": h.verdict, "reason": h.reason,
                  "last_modified_days": round(ctx.days_since(h.usage.newest_mtime))
                  if h.usage.newest_mtime else None} for h in hogs],
    }


def write_json(path: str, data: dict) -> None:
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)


def write_html(path: str, data: dict) -> None:
    rows = []
    biggest = max([f["size"] or 0 for f in data["findings"] + data["projects"]] +
                  [h["size"] for h in data["hogs"]] + [1])

    def bar(n: Optional[int]) -> str:
        pct = 0 if not n else max(1, int(100 * n / biggest))
        return f'<div class="bar"><span style="width:{pct}%"></span></div>'

    def e(x) -> str:
        return html.escape(str(x if x is not None else ""))

    for f in data["findings"]:
        tag = "info" if f["action"] == "report-only" else f["safety"]
        rows.append(
            f'<tr class="{e(tag)}"><td class="num">{e(human(f["size"]) if f["size"] is not None else "?")}'
            f'{bar(f["size"])}</td><td><span class="tag {e(tag)}">{e(tag)}</span></td>'
            f'<td><b>{e(f["name"])}</b><br><code>{e(f["rule"])}</code></td>'
            f'<td><code>{e(f["path"] or f["note"])}</code><details><summary>what is this?</summary>'
            f'<p>{e(f["why"])}</p><p><i>After cleaning:</i> {e(f["impact"])}</p></details></td>'
            f'<td>{e(f["category"])}</td></tr>')
    proj_rows = "".join(
        f'<tr><td class="num">{e(human(p["size"]))}{bar(p["size"])}</td><td>{e(p["note"])}</td>'
        f'<td>{e(p["name"])}</td><td><code>{e(p["path"])}</code></td></tr>' for p in data["projects"])
    hog_rows = "".join(
        f'<tr><td class="num">{e(human(h["size"]))}{bar(h["size"])}</td>'
        f'<td><span class="tag v-{e(h["verdict"])}">{e(h["verdict"])}</span></td>'
        f'<td><code>{e(h["path"])}</code></td><td>{e(h["reason"])}</td></tr>' for h in data["hogs"])
    t, d = data["totals"], data["disk"]
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Mac Storage Report</title>
<style>
:root{{--bg:#fafaf9;--fg:#1c1917;--mut:#78716c;--card:#fff;--line:#e7e5e4;--safe:#15803d;--caution:#b45309;
--review:#7e22ce;--info:#0e7490;--bar:#3b82f6}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1917;--fg:#f5f5f4;--mut:#a8a29e;--card:#292524;--line:#44403c;
--safe:#4ade80;--caution:#fbbf24;--review:#c084fc;--info:#22d3ee;--bar:#60a5fa}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,system-ui,sans-serif;margin:0;padding:24px 16px}}
main{{max-width:1100px;margin:auto}} h1{{margin:0 0 4px}} .mut{{color:var(--mut)}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:20px 0}}
.tile{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}}
.tile b{{font-size:24px;display:block}} table{{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:10px;overflow:hidden;margin-bottom:28px}}
td,th{{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}}
.num{{white-space:nowrap;width:110px;font-variant-numeric:tabular-nums}}
.bar{{height:4px;background:var(--line);border-radius:2px;margin-top:4px}}.bar span{{display:block;height:4px;
background:var(--bar);border-radius:2px}} code{{font-size:12px;word-break:break-all}}
.tag{{font-size:12px;padding:1px 8px;border-radius:99px;border:1px solid currentColor}}
.safe{{color:var(--safe)}}.caution{{color:var(--caution)}}.review{{color:var(--review)}}.info{{color:var(--info)}}
tr.safe,tr.caution,tr.review,tr.info{{color:inherit}} .v-likely-junk{{color:var(--safe)}}
.v-orphaned,.v-stale{{color:var(--caution)}}.v-data{{color:var(--review)}}.v-system{{color:var(--info)}}
details summary{{cursor:pointer;color:var(--mut);font-size:13px}} .scroll{{overflow-x:auto}}
</style></head><body><main>
<h1>Mac Storage Report</h1><div class="mut">Generated {e(data["generated"])} &middot;
{e(human(d["used"]))} used of {e(human(d["total"]))} &middot; {e(human(d["free"]))} free</div>
<div class="tiles">
<div class="tile"><span class="safe">Safe to clean</span><b>{e(human(t["safe"]))}</b><code>msc clean</code></div>
<div class="tile"><span class="caution">With caution</span><b>{e(human(t["caution"]))}</b><code>msc clean --tier caution</code></div>
<div class="tile"><span class="review">Needs review</span><b>{e(human(t["review"]))}</b><code>msc clean --only &lt;id&gt;</code></div>
<div class="tile"><span class="info">Info only</span><b>{e(human(t["report-only"]))}</b><span class="mut">clean via the owning app</span></div>
</div>
<h2>Known junk &amp; caches</h2><div class="scroll"><table>{"".join(rows)}</table></div>
<h2>Project build artifacts</h2><div class="scroll"><table>{proj_rows or '<tr><td>None found</td></tr>'}</table></div>
<h2>Other big folders (not covered by a rule)</h2><div class="scroll"><table>{hog_rows or '<tr><td>None</td></tr>'}</table></div>
</main></body></html>"""
    with open(path, "w") as fh:
        fh.write(doc)
