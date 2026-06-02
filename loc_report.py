#!/usr/bin/env python3
"""
loc_report.py  –  Lines-of-Code analyser across multiple GitHub repos
======================================================================
Clones repos (in parallel), counts real code lines, and writes a rich
HTML report + optional JSON export.

Usage
-----
# Repos via config file:
    python3 loc_report.py --config repos.conf --pat ghp_xxx

# Repos inline (no config file needed):
    python3 loc_report.py --repos https://github.com/org/a https://github.com/org/b \
                          --pat ghp_xxx --output report.html

# PAT from environment:
    export GITHUB_PAT=ghp_xxx
    python3 loc_report.py --repos https://github.com/org/repo

repos.conf format  (one repo per line, optional ref after whitespace):
    https://github.com/your-org/ccsdk-oran
    https://github.com/your-org/ccsdk-features   main
    https://github.com/your-org/old-service        v2.3.0
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# ── language registry ─────────────────────────────────────────────────────────
# ext → (display_name, single_comment_prefixes, block_comment or None)
LANG_MAP: dict[str, tuple[str, list[str], tuple[str, str] | None]] = {
    ".py":         ("Python",        ["#"],          None),
    ".java":       ("Java",          ["//"],         ("/*", "*/")),
    ".js":         ("JavaScript",    ["//"],         ("/*", "*/")),
    ".jsx":        ("JavaScript",    ["//"],         ("/*", "*/")),
    ".ts":         ("TypeScript",    ["//"],         ("/*", "*/")),
    ".tsx":        ("TypeScript",    ["//"],         ("/*", "*/")),
    ".c":          ("C",             ["//"],         ("/*", "*/")),
    ".h":          ("C Header",      ["//"],         ("/*", "*/")),
    ".cpp":        ("C++",           ["//"],         ("/*", "*/")),
    ".cc":         ("C++",           ["//"],         ("/*", "*/")),
    ".cxx":        ("C++",           ["//"],         ("/*", "*/")),
    ".go":         ("Go",            ["//"],         ("/*", "*/")),
    ".rs":         ("Rust",          ["//"],         ("/*", "*/")),
    ".sh":         ("Shell",         ["#"],          None),
    ".bash":       ("Bash",          ["#"],          None),
    ".zsh":        ("Shell",         ["#"],          None),
    ".yaml":       ("YAML",          ["#"],          None),
    ".yml":        ("YAML",          ["#"],          None),
    ".xml":        ("XML",           [],             ("<!--", "-->")),
    ".html":       ("HTML",          [],             ("<!--", "-->")),
    ".htm":        ("HTML",          [],             ("<!--", "-->")),
    ".yang":       ("YANG",          ["//"],         ("/*", "*/")),
    ".json":       ("JSON",          [],             None),
    ".toml":       ("TOML",          ["#"],          None),
    ".ini":        ("INI",           ["#", ";"],     None),
    ".cfg":        ("Config",        ["#", ";"],     None),
    ".conf":       ("Config",        ["#"],          None),
    ".properties": ("Properties",    ["#", "!"],     None),
    ".groovy":     ("Groovy",        ["//"],         ("/*", "*/")),
    ".kt":         ("Kotlin",        ["//"],         ("/*", "*/")),
    ".scala":      ("Scala",         ["//"],         ("/*", "*/")),
    ".rb":         ("Ruby",          ["#"],          None),
    ".md":         ("Markdown",      [],             None),
    ".txt":        ("Text",          [],             None),
    ".sql":        ("SQL",           ["--"],         ("/*", "*/")),
    ".tf":         ("Terraform",     ["#"],          ("/*", "*/")),
    ".proto":      ("Protobuf",      ["//"],         ("/*", "*/")),
    ".gradle":     ("Gradle",        ["//"],         ("/*", "*/")),
    ".swift":      ("Swift",         ["//"],         ("/*", "*/")),
    ".dart":       ("Dart",          ["//"],         ("/*", "*/")),
    ".lua":        ("Lua",           ["--"],         ("--[[", "]]")),
    ".r":          ("R",             ["#"],          None),
    ".R":          ("R",             ["#"],          None),
    ".cs":         ("C#",            ["//"],         ("/*", "*/")),
    ".php":        ("PHP",           ["//", "#"],    ("/*", "*/")),
    ".ex":         ("Elixir",        ["#"],          None),
    ".exs":        ("Elixir",        ["#"],          None),
}

SKIP_EXTENSIONS: set[str] = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".jar", ".class",
    ".bin", ".so", ".dll", ".exe", ".lock", ".sum",
    ".woff", ".woff2", ".ttf", ".eot",
    ".mp4", ".mp3", ".wav", ".ogg",
    ".pyc", ".pyo", ".pyd",
    ".db", ".sqlite", ".sqlite3",
    ".min.js",  # minified
}

SKIP_DIRS: set[str] = {
    ".git", ".svn", ".hg",
    "node_modules", "__pycache__", ".tox", ".pytest_cache",
    "target", "build", "dist", "out",
    ".idea", ".vscode", ".eclipse",
    "vendor",          # Go / PHP
    ".terraform",
}

# ── git helpers ───────────────────────────────────────────────────────────────

def _inject_pat(url: str, pat: str) -> str:
    p = urlparse(url)
    return p._replace(netloc=f"{pat}@{p.netloc}").geturl()


def _is_sha(ref: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{7,40}", ref, re.IGNORECASE))


def clone_repo(url: str, dest: str, pat: str | None, ref: str) -> tuple[bool, str]:
    """Clone *url* at *ref* into *dest*.  Returns (success, message)."""
    clone_url = _inject_pat(url, pat) if pat else url

    if _is_sha(ref):
        r = subprocess.run(["git", "clone", "--quiet", clone_url, dest],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return False, r.stderr.strip()[:300]
        r2 = subprocess.run(["git", "checkout", ref], cwd=dest,
                            capture_output=True, text=True)
        return r2.returncode == 0, r2.stderr.strip()[:300] if r2.returncode != 0 else "ok"

    # Try the requested branch/tag first, then fall back to default branch
    for branch_args in (["--branch", ref], []):
        cmd = ["git", "clone", "--depth=1"] + branch_args + [clone_url, dest]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return True, "ok"
        if dest and Path(dest).exists():
            shutil.rmtree(dest, ignore_errors=True)
        if branch_args:
            # First attempt failed; try again without --branch (default branch)
            continue

    return False, r.stderr.strip()[:300]


# ── LOC counter ───────────────────────────────────────────────────────────────

def count_loc_file(filepath: str | Path) -> tuple[int, int, int, int]:
    """Return (total, code, blank, comment) for one file."""
    ext  = Path(filepath).suffix.lower()
    info = LANG_MAP.get(ext)

    try:
        with open(filepath, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return 0, 0, 0, 0

    total   = len(lines)
    blank   = 0
    comment = 0
    in_block = False

    single_prefixes: list[str] = info[1] if info else []
    block_delim: tuple[str, str] | None = info[2] if info else None

    for raw in lines:
        line = raw.strip()
        if not line:
            blank += 1
            continue

        if block_delim:
            bopen, bclose = block_delim
            if in_block:
                comment += 1
                if bclose in line:
                    in_block = False
                continue
            if bopen in line:
                comment += 1
                tail_start = line.find(bopen) + len(bopen)
                if bclose not in line[tail_start:]:
                    in_block = True
                continue

        if single_prefixes and any(line.startswith(p) for p in single_prefixes):
            comment += 1
            continue

    code = total - blank - comment
    return total, max(code, 0), blank, comment


def count_repo(repo_dir: str | Path) -> dict:
    """Walk a cloned repo; return structured LOC data."""
    repo_dir  = Path(repo_dir)
    files_data: list[dict] = []

    for root, dirs, filenames in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

        for fname in filenames:
            fpath = Path(root) / fname
            ext   = fpath.suffix.lower()

            if ext in SKIP_EXTENSIONS or fname.endswith(".min.js"):
                continue
            if not fpath.is_file():
                continue

            rel_path = str(fpath.relative_to(repo_dir))
            total, code, blank, cmt = count_loc_file(fpath)

            if total == 0:
                continue

            lang = LANG_MAP.get(ext, (ext or "other", [], None))[0]
            files_data.append({
                "path":    rel_path,
                "ext":     ext or "(none)",
                "lang":    lang,
                "total":   total,
                "code":    code,
                "blank":   blank,
                "comment": cmt,
            })

    files_data.sort(key=lambda x: x["code"], reverse=True)

    # Aggregate by language
    by_lang: dict[str, dict] = defaultdict(
        lambda: {"total": 0, "code": 0, "blank": 0, "comment": 0, "files": 0}
    )
    for f in files_data:
        for k in ("total", "code", "blank", "comment"):
            by_lang[f["lang"]][k] += f[k]
        by_lang[f["lang"]]["files"] += 1

    # Aggregate by top-level directory
    by_dir: dict[str, dict] = defaultdict(
        lambda: {"total": 0, "code": 0, "blank": 0, "comment": 0, "files": 0}
    )
    for f in files_data:
        parts   = Path(f["path"]).parts
        top_dir = parts[0] if len(parts) > 1 else "(root)"
        for k in ("total", "code", "blank", "comment"):
            by_dir[top_dir][k] += f[k]
        by_dir[top_dir]["files"] += 1

    totals = {
        "total":   sum(f["total"]   for f in files_data),
        "code":    sum(f["code"]    for f in files_data),
        "blank":   sum(f["blank"]   for f in files_data),
        "comment": sum(f["comment"] for f in files_data),
        "files":   len(files_data),
    }
    avg_file_loc = round(totals["code"] / totals["files"], 1) if totals["files"] else 0
    comment_ratio = round(totals["comment"] / (totals["code"] + totals["comment"]) * 100, 1) \
                    if (totals["code"] + totals["comment"]) else 0

    return {
        "files":          files_data,
        "by_lang":        dict(by_lang),
        "by_dir":         dict(by_dir),
        "totals":         totals,
        "avg_file_loc":   avg_file_loc,
        "comment_ratio":  comment_ratio,
    }


# ── config parser ─────────────────────────────────────────────────────────────

def parse_config(config_file: str, default_ref: str = "main") -> list[dict]:
    repos = []
    with open(config_file) as fh:
        for line in fh:
            line = line.split("#")[0].strip()
            if not line:
                continue
            parts = line.split()
            url   = parts[0]
            ref   = parts[1] if len(parts) > 1 else default_ref
            name  = url.rstrip("/").split("/")[-1]
            repos.append({"url": url, "ref": ref, "name": name})
    return repos


# ── HTML rendering helpers ────────────────────────────────────────────────────

def _fmt(n: int | float) -> str:
    return f"{int(n):,}"


def _pct(part: int | float, total: int | float) -> float:
    return round(part / total * 100, 1) if total else 0.0


def _stack_bars(items: dict, max_code: int, show: int = 15) -> str:
    html = ""
    for name, s in sorted(items.items(), key=lambda x: -x[1]["code"])[:show]:
        pct_code = _pct(s["code"],    max_code) if max_code else 0
        pct_cmt  = _pct(s["comment"], max_code) if max_code else 0
        html += (
            f'<div class="stack-row">'
            f'<div class="stack-label" title="{name}">{name}</div>'
            f'<div class="stack-track">'
            f'<div class="stack-code"    style="width:{pct_code:.1f}%"></div>'
            f'<div class="stack-comment" style="width:{pct_cmt:.1f}%"></div>'
            f'</div>'
            f'<div class="stack-nums">'
            f'<span class="c">{_fmt(s["code"])}</span>'
            f'<span class="m">{_fmt(s["comment"])}</span>'
            f'</div>'
            f'</div>'
        )
    return html


def _lang_table(by_lang: dict, grand_code: int) -> str:
    rows = sorted(by_lang.items(), key=lambda x: -x[1]["code"])
    html = '<div class="tbl-wrap"><table><thead><tr>'
    html += '<th>Language</th><th>Files</th><th>Code</th><th>Comments</th><th>Blank</th><th>Total</th><th>Code %</th></tr></thead><tbody>'
    for lang, s in rows:
        gcp = _pct(s["code"], grand_code)
        html += (
            f'<tr><td>{lang}</td>'
            f'<td>{_fmt(s["files"])}</td>'
            f'<td class="td-code">{_fmt(s["code"])}'
            f'<span class="pct-bar"><span class="pb-code" style="width:{gcp:.1f}%"></span></span></td>'
            f'<td class="td-comment">{_fmt(s["comment"])}</td>'
            f'<td class="td-blank">{_fmt(s["blank"])}</td>'
            f'<td>{_fmt(s["total"])}</td>'
            f'<td class="td-pct">{_pct(s["code"], s["total"] or 1):.1f}%</td></tr>'
        )
    html += "</tbody></table></div>"
    return html


def _dir_table(by_dir: dict, grand_code: int) -> str:
    rows = sorted(by_dir.items(), key=lambda x: -x[1]["code"])
    html = '<div class="tbl-wrap"><table><thead><tr>'
    html += '<th>Directory</th><th>Files</th><th>Code</th><th>Comments</th><th>Blank</th><th>Total</th><th>Code %</th></tr></thead><tbody>'
    for d, s in rows:
        gcp = _pct(s["code"], grand_code)
        html += (
            f'<tr><td><code style="font-size:11px">{d}</code></td>'
            f'<td>{_fmt(s["files"])}</td>'
            f'<td class="td-code">{_fmt(s["code"])}'
            f'<span class="pct-bar"><span class="pb-code" style="width:{gcp:.1f}%"></span></span></td>'
            f'<td class="td-comment">{_fmt(s["comment"])}</td>'
            f'<td class="td-blank">{_fmt(s["blank"])}</td>'
            f'<td>{_fmt(s["total"])}</td>'
            f'<td class="td-pct">{_pct(s["code"], s["total"] or 1):.1f}%</td></tr>'
        )
    html += "</tbody></table></div>"
    return html


def _file_table(files: list[dict], repo_idx: int, grand_code: int) -> str:
    html = (
        f'<div class="search-row">'
        f'<input type="text" id="fsearch-{repo_idx}" placeholder="Filter files…" '
        f'oninput="filterFiles({repo_idx})"/>'
        f'</div>'
        f'<div class="tbl-wrap"><table id="ftable-{repo_idx}">'
        f'<thead><tr><th>File</th><th>Lang</th><th>Code</th><th>Comments</th>'
        f'<th>Blank</th><th>Total</th><th>Code %</th></tr></thead><tbody>'
    )
    for f in files:
        gcp = _pct(f["code"], grand_code)
        html += (
            f'<tr class="frow">'
            f'<td style="font-size:11px;max-width:420px;overflow:hidden;text-overflow:ellipsis" title="{f["path"]}">{f["path"]}</td>'
            f'<td style="color:var(--dim)">{f["lang"]}</td>'
            f'<td class="td-code">{_fmt(f["code"])}'
            f'<span class="pct-bar"><span class="pb-code" style="width:{gcp:.1f}%"></span></span></td>'
            f'<td class="td-comment">{_fmt(f["comment"])}</td>'
            f'<td class="td-blank">{_fmt(f["blank"])}</td>'
            f'<td>{_fmt(f["total"])}</td>'
            f'<td class="td-pct">{_pct(f["code"], f["total"] or 1):.1f}%</td>'
            f'</tr>'
        )
    html += "</tbody></table></div>"
    return html


# ── CSS ───────────────────────────────────────────────────────────────────────

CSS = """
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Syne:wght@400;700;800&display=swap');
:root{
  --bg:#0b0f1a;--surface:#111827;--surface2:#1a2236;--border:#1f2d44;
  --code:#22d3a5;--comment:#5b8dee;--blank:#64748b;--warn:#f59e0b;--purple:#a855f7;
  --text:#e2e8f0;--dim:#64748b;
  --mono:'JetBrains Mono',monospace;--head:'Syne',sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:13px;line-height:1.7}

/* hero */
.hero{background:linear-gradient(135deg,#0b0f1a,#0d1929 60%,#101e30);border-bottom:1px solid var(--border);padding:48px 40px 32px;position:relative;overflow:hidden}
.hero::before{content:'';position:absolute;inset:0;background:radial-gradient(ellipse 60% 80% at 85% 40%,rgba(34,211,165,.07) 0%,transparent 70%);pointer-events:none}
.hero h1{font-family:var(--head);font-size:2rem;font-weight:800;letter-spacing:-.03em}
.hero h1 span{color:var(--code)}
.meta{margin-top:10px;color:var(--dim);font-size:11px;display:flex;gap:24px;flex-wrap:wrap}
.meta strong{color:var(--text)}

/* layout */
.wrap{max-width:1280px;margin:0 auto;padding:36px 40px}
h2{font-family:var(--head);font-size:.85rem;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--dim);margin:28px 0 14px;padding-bottom:8px;border-bottom:1px solid var(--border)}
h3{font-family:var(--head);font-size:1rem;font-weight:700;color:var(--text);margin-bottom:12px}

/* KPI grid */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:14px;margin-bottom:36px}
.kpi{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:18px 22px;position:relative;overflow:hidden}
.kpi::after{content:'';position:absolute;top:0;left:0;right:0;height:3px;border-radius:12px 12px 0 0}
.kpi.kcode::after{background:var(--code)}   .kpi.kcomment::after{background:var(--comment)}
.kpi.kblank::after{background:#334155}       .kpi.ktotal::after{background:var(--purple)}
.kpi.krepos::after{background:var(--warn)}   .kpi.kfiles::after{background:#ec4899}
.kpi.kavg::after{background:#06b6d4}         .kpi.kratio::after{background:#f97316}
.kpi-label{font-size:10px;text-transform:uppercase;letter-spacing:.12em;color:var(--dim);margin-bottom:6px}
.kpi-value{font-family:var(--head);font-size:1.8rem;font-weight:800}
.kpi.kcode .kpi-value{color:var(--code)}     .kpi.kcomment .kpi-value{color:var(--comment)}
.kpi.kblank .kpi-value{color:#475569}        .kpi.ktotal .kpi-value{color:var(--purple)}
.kpi.krepos .kpi-value{color:var(--warn)}    .kpi.kfiles .kpi-value{color:#ec4899}
.kpi.kavg .kpi-value{color:#06b6d4}          .kpi.kratio .kpi-value{color:#f97316}

/* stacked bar */
.stack-row{display:flex;align-items:center;gap:12px;margin-bottom:8px;font-size:12px}
.stack-label{width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text);flex-shrink:0}
.stack-track{flex:1;height:10px;background:var(--bg);border-radius:5px;overflow:hidden;display:flex}
.stack-code   {height:100%;background:var(--code)}
.stack-comment{height:100%;background:var(--comment)}
.stack-blank  {height:100%;background:#1e293b}
.stack-nums{width:110px;text-align:right;font-size:11px;display:flex;gap:6px;justify-content:flex-end;flex-shrink:0}
.stack-nums .c{color:var(--code)}
.stack-nums .m{color:var(--comment)}

/* two-col */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin-bottom:32px}
@media(max-width:900px){.two-col{grid-template-columns:1fr}}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:22px}

/* repo sections */
.repo-block{background:var(--surface);border:1px solid var(--border);border-radius:14px;margin-bottom:28px;overflow:hidden}
.repo-block-header{background:var(--surface2);padding:16px 22px;display:flex;align-items:center;gap:14px;cursor:pointer;user-select:none;border-bottom:1px solid var(--border)}
.repo-block-header:hover{background:#1a2840}
.repo-name-big{font-family:var(--head);font-size:1.1rem;font-weight:800;color:var(--text);flex:1}
.repo-ref-badge{font-size:10px;color:var(--dim);background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:3px 10px}
.repo-code-total{font-family:var(--head);font-size:1.3rem;font-weight:800;color:var(--code)}
.repo-code-label{font-size:10px;color:var(--dim);margin-left:4px}
.toggle-icon{color:var(--dim);font-size:12px;flex-shrink:0;width:16px;text-align:center}
.repo-block-body{padding:22px;display:none}
.repo-block-body.open{display:block}
.repo-error{padding:14px 22px;color:#f87171;font-size:12px;background:#1a0f0f;border-top:1px solid #3f1111}

/* tables */
.tbl-wrap{overflow-x:auto;margin-bottom:24px}
table{width:100%;border-collapse:collapse;font-size:12px}
thead tr{background:var(--surface2)}
th{padding:8px 12px;text-align:right;font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);white-space:nowrap}
th:first-child{text-align:left}
td{padding:7px 12px;border-top:1px solid var(--border);text-align:right;white-space:nowrap}
td:first-child{text-align:left;color:var(--text)}
tr:hover td{background:var(--surface2)}
.td-code{color:var(--code);font-weight:600}
.td-comment{color:var(--comment)}
.td-blank{color:#475569}
.td-pct{color:var(--dim);font-size:11px}

/* pct bar inside table */
.pct-bar{display:inline-flex;height:5px;border-radius:3px;overflow:hidden;width:60px;vertical-align:middle;margin-left:6px;background:var(--bg)}
.pb-code{height:100%;background:var(--code)}

/* search */
.search-row{padding:10px 16px;background:var(--surface2);border-bottom:1px solid var(--border)}
input[type=text]{width:100%;max-width:360px;background:var(--bg);border:1px solid var(--border);
  border-radius:8px;color:var(--text);font-family:var(--mono);font-size:12px;
  padding:7px 12px;outline:none}
input[type=text]:focus{border-color:var(--code)}

/* legend */
.legend{display:flex;gap:18px;margin-bottom:18px;font-size:11px}
.legend-dot{width:10px;height:10px;border-radius:2px;display:inline-block;margin-right:5px}

/* warning badge */
.badge-warn{display:inline-block;background:#78350f;color:#fcd34d;font-size:10px;
  border-radius:4px;padding:1px 7px;margin-left:8px;vertical-align:middle}

.footer{text-align:center;color:var(--dim);font-size:11px;padding:28px 0;border-top:1px solid var(--border);margin-top:8px}
"""


# ── HTML builder ──────────────────────────────────────────────────────────────

def build_html(all_repos: list[dict], failed_repos: list[dict], generated: str) -> str:
    ok = [r for r in all_repos]

    grand_code    = sum(r["data"]["totals"]["code"]    for r in ok)
    grand_comment = sum(r["data"]["totals"]["comment"] for r in ok)
    grand_blank   = sum(r["data"]["totals"]["blank"]   for r in ok)
    grand_total   = sum(r["data"]["totals"]["total"]   for r in ok)
    grand_files   = sum(r["data"]["totals"]["files"]   for r in ok)
    n_repos       = len(ok) + len(failed_repos)
    avg_file_loc  = round(grand_code / grand_files, 1) if grand_files else 0
    grand_ratio   = _pct(grand_comment, grand_code + grand_comment)

    # Grand lang / dir aggregation
    grand_lang: dict = defaultdict(lambda: {"code": 0, "comment": 0, "blank": 0, "total": 0, "files": 0})
    for r in ok:
        for lang, s in r["data"]["by_lang"].items():
            for k in ("code", "comment", "blank", "total", "files"):
                grand_lang[lang][k] += s[k]

    grand_dir: dict = defaultdict(lambda: {"code": 0, "comment": 0, "blank": 0, "total": 0, "files": 0})
    for r in ok:
        for d, s in r["data"]["by_dir"].items():
            key = f"{r['name']}/{d}"
            for k in ("code", "comment", "blank", "total", "files"):
                grand_dir[key][k] += s[k]

    max_lang = max((s["code"] for s in grand_lang.values()), default=1)
    max_dir  = max((s["code"] for s in grand_dir.values()),  default=1)

    lang_bars = _stack_bars(dict(grand_lang), max_lang)
    dir_bars  = _stack_bars(dict(grand_dir),  max_dir)

    # Failed repos banner
    failed_html = ""
    if failed_repos:
        rows = "".join(
            f'<tr><td>{r["name"]}</td><td>{r["url"]}</td><td style="color:#f87171">{r["error"]}</td></tr>'
            for r in failed_repos
        )
        failed_html = f"""
        <div style="background:#1a0f0f;border:1px solid #3f1111;border-radius:10px;padding:18px 22px;margin-bottom:28px">
          <h3 style="color:#f87171;margin-bottom:10px">⚠ {len(failed_repos)} repo(s) failed to clone</h3>
          <div class="tbl-wrap"><table>
            <thead><tr><th>Repo</th><th>URL</th><th>Error</th></tr></thead>
            <tbody>{rows}</tbody>
          </table></div>
        </div>"""

    # Per-repo sections
    repo_sections = ""
    for idx, r in enumerate(ok):
        d  = r["data"]
        t  = d["totals"]
        ml = max((s["code"] for s in d["by_lang"].values()), default=1)
        md = max((s["code"] for s in d["by_dir"].values()),  default=1)
        ratio_badge = (
            f'<span class="badge-warn">high comments {d["comment_ratio"]:.0f}%</span>'
            if d["comment_ratio"] > 40 else ""
        )

        repo_sections += f"""
        <div class="repo-block">
          <div class="repo-block-header" onclick="toggleRepo({idx})">
            <span class="repo-name-big">{r['name']}{ratio_badge}</span>
            <span class="repo-ref-badge">{r['ref']}</span>
            <span class="repo-code-total">{_fmt(t['code'])}</span>
            <span class="repo-code-label">code lines</span>
            <span style="color:var(--dim);font-size:11px;margin-left:8px">{_fmt(t['files'])} files</span>
            <span class="toggle-icon" id="ricon-{idx}">▶</span>
          </div>
          <div class="repo-block-body" id="rbody-{idx}">
            <div style="display:flex;gap:20px;flex-wrap:wrap;margin-bottom:20px">
              <div><span style="color:var(--code);font-family:var(--head);font-size:1.4rem;font-weight:800">{_fmt(t['code'])}</span> <span style="color:var(--dim);font-size:11px">code</span></div>
              <div><span style="color:var(--comment);font-family:var(--head);font-size:1.4rem;font-weight:800">{_fmt(t['comment'])}</span> <span style="color:var(--dim);font-size:11px">comments</span></div>
              <div><span style="color:#475569;font-family:var(--head);font-size:1.4rem;font-weight:800">{_fmt(t['blank'])}</span> <span style="color:var(--dim);font-size:11px">blank</span></div>
              <div><span style="color:var(--purple);font-family:var(--head);font-size:1.4rem;font-weight:800">{_fmt(t['total'])}</span> <span style="color:var(--dim);font-size:11px">total</span></div>
              <div><span style="color:#06b6d4;font-family:var(--head);font-size:1.4rem;font-weight:800">{d['avg_file_loc']}</span> <span style="color:var(--dim);font-size:11px">avg LOC/file</span></div>
            </div>

            <div class="two-col">
              <div class="panel"><h3>By Language</h3>{_stack_bars(d['by_lang'], ml)}</div>
              <div class="panel"><h3>By Directory</h3>{_stack_bars(d['by_dir'], md)}</div>
            </div>

            <h2>Language Breakdown</h2>
            {_lang_table(d['by_lang'], t['code'])}

            <h2>Directory Breakdown</h2>
            {_dir_table(d['by_dir'], t['code'])}

            <h2>File Breakdown</h2>
            {_file_table(d['files'], idx, t['code'])}
          </div>
        </div>"""

    # Repo comparison table
    repo_rows = "".join(
        f"<tr>"
        f"<td style='font-weight:600'>{r['name']}</td>"
        f"<td style='color:var(--dim);font-size:11px'>{r['ref']}</td>"
        f"<td>{_fmt(r['data']['totals']['files'])}</td>"
        f"<td class='td-code'>{_fmt(r['data']['totals']['code'])}"
        f"<span class='pct-bar'><span class='pb-code' style='width:{_pct(r['data']['totals']['code'], grand_code):.1f}%'></span></span></td>"
        f"<td class='td-comment'>{_fmt(r['data']['totals']['comment'])}</td>"
        f"<td class='td-blank'>{_fmt(r['data']['totals']['blank'])}</td>"
        f"<td>{_fmt(r['data']['totals']['total'])}</td>"
        f"<td class='td-pct'>{_pct(r['data']['totals']['code'], r['data']['totals']['total']):.1f}%</td>"
        f"<td style='color:#06b6d4'>{r['data']['avg_file_loc']}</td>"
        f"</tr>"
        for r in sorted(ok, key=lambda x: -x["data"]["totals"]["code"])
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>LOC Report</title>
<style>{CSS}</style>
</head>
<body>

<div class="hero">
  <h1>Lines of <span>Code</span> Report</h1>
  <div class="meta">
    <div><strong>Repos analysed</strong> {len(ok)} of {n_repos}</div>
    <div><strong>Counting</strong> non-blank, non-comment lines</div>
    <div><strong>Generated</strong> {generated}</div>
  </div>
</div>

<div class="wrap">

  <!-- Grand KPIs -->
  <div class="kpis">
    <div class="kpi krepos"><div class="kpi-label">Repos</div><div class="kpi-value">{len(ok)}</div></div>
    <div class="kpi kcode"><div class="kpi-label">Code Lines</div><div class="kpi-value">{_fmt(grand_code)}</div></div>
    <div class="kpi kcomment"><div class="kpi-label">Comment Lines</div><div class="kpi-value">{_fmt(grand_comment)}</div></div>
    <div class="kpi kblank"><div class="kpi-label">Blank Lines</div><div class="kpi-value">{_fmt(grand_blank)}</div></div>
    <div class="kpi ktotal"><div class="kpi-label">Total Lines</div><div class="kpi-value">{_fmt(grand_total)}</div></div>
    <div class="kpi kfiles"><div class="kpi-label">Files</div><div class="kpi-value">{_fmt(grand_files)}</div></div>
    <div class="kpi kavg"><div class="kpi-label">Avg LOC / File</div><div class="kpi-value">{avg_file_loc}</div></div>
    <div class="kpi kratio"><div class="kpi-label">Comment Ratio</div><div class="kpi-value">{grand_ratio:.1f}%</div></div>
  </div>

  <!-- Legend -->
  <div class="legend">
    <span><span class="legend-dot" style="background:var(--code)"></span>Code</span>
    <span><span class="legend-dot" style="background:var(--comment)"></span>Comments</span>
    <span><span class="legend-dot" style="background:#1e293b"></span>Blank</span>
  </div>

  <!-- Summary charts -->
  <div class="two-col">
    <div class="panel"><h3>By Language (all repos)</h3>{lang_bars}</div>
    <div class="panel"><h3>By Directory (all repos)</h3>{dir_bars}</div>
  </div>

  <!-- Failed repos -->
  {failed_html}

  <!-- Repo comparison table -->
  <h2>Repo Comparison</h2>
  <div class="tbl-wrap">
    <table>
      <thead><tr>
        <th>Repo</th><th>Ref</th><th>Files</th>
        <th>Code</th><th>Comments</th><th>Blank</th><th>Total</th><th>Code %</th><th>Avg LOC</th>
      </tr></thead>
      <tbody>
        {repo_rows}
        <tr style="background:var(--surface2);font-weight:600;border-top:2px solid var(--border)">
          <td>TOTAL</td><td></td>
          <td>{_fmt(grand_files)}</td>
          <td class="td-code">{_fmt(grand_code)}</td>
          <td class="td-comment">{_fmt(grand_comment)}</td>
          <td class="td-blank">{_fmt(grand_blank)}</td>
          <td>{_fmt(grand_total)}</td>
          <td class="td-pct">{_pct(grand_code, grand_total):.1f}%</td>
          <td style="color:#06b6d4">{avg_file_loc}</td>
        </tr>
      </tbody>
    </table>
  </div>

  <!-- Per-repo detail -->
  <h2>Per-Repo Detail</h2>
  {repo_sections}

</div>

<div class="footer">Generated by loc_report.py &nbsp;·&nbsp; {generated}</div>

<script>
function toggleRepo(i) {{
  var b  = document.getElementById('rbody-' + i);
  var ic = document.getElementById('ricon-' + i);
  var open = b.classList.toggle('open');
  ic.textContent = open ? '▼' : '▶';
}}
function filterFiles(i) {{
  var q   = document.getElementById('fsearch-' + i).value.toLowerCase();
  var tbl = document.getElementById('ftable-' + i);
  tbl.querySelectorAll('tr.frow').forEach(function(row) {{
    row.style.display = row.cells[0].textContent.toLowerCase().includes(q) ? '' : 'none';
  }});
}}
window.addEventListener('DOMContentLoaded', function() {{
  var first = document.querySelector('.repo-block-body');
  if (first) toggleRepo(0);
}});
</script>
</body>
</html>"""


# ── parallel clone + count ────────────────────────────────────────────────────

def _process_one(repo: dict, tmpdir: str, pat: str | None) -> dict:
    dest = os.path.join(tmpdir, repo["name"])
    ok, msg = clone_repo(repo["url"], dest, pat, repo["ref"])
    if not ok:
        return {**repo, "success": False, "error": msg}
    data = count_repo(dest)
    t    = data["totals"]
    return {**repo, "success": True, "data": data, "error": None,
            "_summary": f"{_fmt(t['code'])} code | {_fmt(t['comment'])} comments | {_fmt(t['files'])} files"}


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Count LOC across multiple GitHub repos and generate an HTML report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--config",  metavar="FILE",
                     help="Config file with repo URLs, one per line (optional ref after whitespace)")
    src.add_argument("--repos",   metavar="URL", nargs="+",
                     help="One or more repo URLs directly on the command line")
    parser.add_argument("--pat",  default=os.environ.get("GITHUB_PAT"),
                        help="GitHub PAT (default: $GITHUB_PAT env var)")
    parser.add_argument("--ref",  default="main",
                        help="Default git ref when not in config (default: main)")
    parser.add_argument("--output", default="loc_report.html",
                        help="Output HTML file (default: loc_report.html)")
    parser.add_argument("--json", metavar="FILE",
                        help="Also write a JSON summary to this file")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel clone workers (default: 4)")
    args = parser.parse_args()

    # Build repo list
    if args.repos:
        repos = [{"url": u, "ref": args.ref, "name": u.rstrip("/").split("/")[-1]}
                 for u in args.repos]
    elif args.config:
        if not os.path.isfile(args.config):
            sys.exit(f"[ERROR] Config file not found: {args.config}")
        repos = parse_config(args.config, default_ref=args.ref)
    else:
        sys.exit("[ERROR] Provide --repos URLs or --config FILE.")

    if not repos:
        sys.exit("[ERROR] No repos found.")

    bar = "─" * 56
    print(f"\n{bar}\n  LOC Report  —  {len(repos)} repo(s)\n{bar}\n")

    tmpdir     = tempfile.mkdtemp(prefix="loc_report_")
    all_repos  : list[dict] = []
    failed     : list[dict] = []

    try:
        with ThreadPoolExecutor(max_workers=min(args.workers, len(repos))) as ex:
            futures = {ex.submit(_process_one, r, tmpdir, args.pat): r for r in repos}
            done    = 0
            for fut in as_completed(futures):
                done += 1
                result = fut.result()
                name   = result["name"]
                if result["success"]:
                    print(f"  [{done}/{len(repos)}] ✓ {name}  →  {result['_summary']}")
                    all_repos.append(result)
                else:
                    print(f"  [{done}/{len(repos)}] ✗ {name}  →  {result['error']}", file=sys.stderr)
                    failed.append(result)

        if not all_repos:
            sys.exit("[ERROR] All repos failed. Nothing to report.")

        generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # HTML report
        print(f"\n📝 Writing HTML → {args.output}")
        html = build_html(all_repos, failed, generated)
        Path(args.output).write_text(html, encoding="utf-8")

        # Optional JSON export
        if args.json:
            print(f"📄 Writing JSON → {args.json}")
            export = {
                "generated": generated,
                "repos": [
                    {
                        "name":    r["name"],
                        "url":     r["url"],
                        "ref":     r["ref"],
                        "totals":  r["data"]["totals"],
                        "avg_file_loc":  r["data"]["avg_file_loc"],
                        "comment_ratio": r["data"]["comment_ratio"],
                        "by_lang": r["data"]["by_lang"],
                        "by_dir":  r["data"]["by_dir"],
                    }
                    for r in all_repos
                ],
                "failed": [{"name": r["name"], "url": r["url"], "error": r["error"]}
                           for r in failed],
            }
            Path(args.json).write_text(json.dumps(export, indent=2), encoding="utf-8")

        grand_code  = sum(r["data"]["totals"]["code"]  for r in all_repos)
        grand_files = sum(r["data"]["totals"]["files"] for r in all_repos)
        print(f"\n{bar}")
        print(f"  ✅  Done!  ({len(all_repos)} OK, {len(failed)} failed)")
        print(f"  Total code lines : {_fmt(grand_code)}")
        print(f"  Total files      : {_fmt(grand_files)}")
        print(f"  Report           : {args.output}")
        if args.json:
            print(f"  JSON export      : {args.json}")
        print(f"{bar}\n")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
