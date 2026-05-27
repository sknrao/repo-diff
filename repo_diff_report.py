#!/usr/bin/env python3
"""
repo_diff_report.py
Compare your forked/modified repo against an upstream repo and generate a
detailed Lines-of-Change HTML report.

Usage:
    python repo_diff_report.py \
        --current  https://github.com/your-org/ccsdk-oran \
        --upstream https://github.com/onap/ccsdk-oran \
        --pat       ghp_xxxxxxxxxxxxxxxxxxxx \
        --ref       2.3.0             # branch, tag, or commit SHA
        --output    report.html
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


# ── helpers ──────────────────────────────────────────────────────────────────

def inject_pat(url: str, pat: str) -> str:
    """Embed a PAT into a GitHub HTTPS clone URL."""
    parsed = urlparse(url)
    authed = parsed._replace(netloc=f"{pat}@{parsed.netloc}")
    return authed.geturl()


def run(cmd: list[str], cwd: str | None = None, silent: bool = False) -> str:
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True
    )
    if result.returncode != 0 and not silent:
        print(f"[ERROR] {' '.join(cmd)}\n{result.stderr}", file=sys.stderr)
    return result.stdout.strip()


def is_sha(ref: str) -> bool:
    """Return True if ref looks like a full (40-char) or short (7-8 char) commit SHA."""
    import re
    return bool(re.fullmatch(r"[0-9a-f]{7,40}", ref, re.IGNORECASE))


def clone_repo(url: str, dest: str, pat: str | None = None, ref: str = "master") -> bool:
    """
    Clone a repo at a specific git ref (branch, tag, or commit SHA).

    Strategy:
      • Branch / tag  → git clone --depth=1 --branch <ref>   (fast, shallow)
      • Commit SHA    → git clone (full), then git checkout <sha>
        (shallow clone cannot target an arbitrary commit directly)
    """
    clone_url = inject_pat(url, pat) if pat else url
    ref_type  = "commit" if is_sha(ref) else "branch/tag"
    print(f"  Cloning {url}  [{ref_type}: {ref}]  →  {dest}")

    if is_sha(ref):
        # Full clone then checkout — needed because --branch cannot accept a raw SHA
        result = subprocess.run(
            ["git", "clone", "--quiet", clone_url, dest],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"  [ERROR] clone failed:\n{result.stderr}", file=sys.stderr)
            return False
        result = subprocess.run(
            ["git", "checkout", ref],
            cwd=dest, capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"  [ERROR] checkout {ref} failed:\n{result.stderr}", file=sys.stderr)
            return False
        return True
    else:
        # Branch or tag — shallow clone is fine
        result = subprocess.run(
            ["git", "clone", "--depth=1", "--branch", ref, clone_url, dest],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            return True
        # Fallback: try without --branch (uses the repo default branch)
        print(f"  [WARN] --branch {ref!r} failed, retrying with default branch…")
        result = subprocess.run(
            ["git", "clone", "--depth=1", clone_url, dest],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"  [ERROR] clone failed:\n{result.stderr}", file=sys.stderr)
            return False
        return True


# ── diff logic ────────────────────────────────────────────────────────────────

SKIP_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
                   ".pdf", ".zip", ".tar", ".gz", ".jar", ".class",
                   ".bin", ".so", ".dll", ".exe", ".lock"}

def is_text_file(path: str) -> bool:
    ext = Path(path).suffix.lower()
    return ext not in SKIP_EXTENSIONS


def diff_repos(current_dir: str, upstream_dir: str) -> dict:
    """
    Run git diff --stat and --numstat between two local directories,
    treating upstream as the 'base' and current as the 'modified' version.
    We use git's diff-tree trick via a temp index.
    """
    print("\n  Running diff …")

    raw_diff = subprocess.run(
        ["git", "diff", "--no-index", "--numstat", upstream_dir, current_dir],
        capture_output=True, text=True
    )
    # exit code 1 means differences found (normal)
    numstat_output = raw_diff.stdout

    # Full patch for file-level analysis
    patch_proc = subprocess.run(
        ["git", "diff", "--no-index", "-U0", upstream_dir, current_dir],
        capture_output=True, text=True
    )
    patch = patch_proc.stdout

    return parse_numstat(numstat_output, upstream_dir, current_dir, patch)


def parse_numstat(numstat: str, upstream_dir: str, current_dir: str, patch: str) -> dict:
    files = []
    total_added = total_removed = 0

    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added_s, removed_s, filepath = parts

        # Strip leading dir prefix so we get repo-relative paths
        # git diff --no-index prefixes paths with b/ dir
        filepath = filepath.replace(current_dir, "").replace(upstream_dir, "")
        filepath = re.sub(r"^[ab]/", "", filepath).lstrip("/")

        if not is_text_file(filepath):
            continue

        # binary files show '-'
        if added_s == "-" or removed_s == "-":
            added, removed = 0, 0
            is_binary = True
        else:
            added, removed = int(added_s), int(removed_s)
            is_binary = False

        total_added += added
        total_removed += removed

        files.append({
            "path": filepath,
            "added": added,
            "removed": removed,
            "delta": added - removed,
            "is_binary": is_binary,
        })

    # Sort by total change descending
    files.sort(key=lambda f: f["added"] + f["removed"], reverse=True)

    # Extension breakdown
    ext_stats: dict[str, dict] = defaultdict(lambda: {"added": 0, "removed": 0, "count": 0})
    for f in files:
        ext = Path(f["path"]).suffix.lower() or "(no ext)"
        ext_stats[ext]["added"] += f["added"]
        ext_stats[ext]["removed"] += f["removed"]
        ext_stats[ext]["count"] += 1

    # Directory breakdown
    dir_stats: dict[str, dict] = defaultdict(lambda: {"added": 0, "removed": 0, "count": 0})
    for f in files:
        parts = Path(f["path"]).parts
        top_dir = parts[0] if len(parts) > 1 else "(root)"
        dir_stats[top_dir]["added"] += f["added"]
        dir_stats[top_dir]["removed"] += f["removed"]
        dir_stats[top_dir]["count"] += 1

    return {
        "files": files,
        "total_added": total_added,
        "total_removed": total_removed,
        "total_changed": total_added + total_removed,
        "net_delta": total_added - total_removed,
        "file_count": len(files),
        "ext_stats": dict(ext_stats),
        "dir_stats": dict(dir_stats),
    }


# ── HTML report ───────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Repo Diff Report</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Syne:wght@400;700;800&display=swap');

  :root {{
    --bg: #0b0f1a;
    --surface: #111827;
    --surface2: #1a2236;
    --border: #1f2d44;
    --accent-add: #22d3a5;
    --accent-rem: #f4545e;
    --accent-neu: #5b8dee;
    --text: #e2e8f0;
    --text-dim: #64748b;
    --font-mono: 'JetBrains Mono', monospace;
    --font-head: 'Syne', sans-serif;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: var(--font-mono); font-size: 13px; line-height: 1.6; }}

  .hero {{
    background: linear-gradient(135deg, #0b0f1a 0%, #0d1929 60%, #0f1f35 100%);
    border-bottom: 1px solid var(--border);
    padding: 48px 40px 36px;
    position: relative;
    overflow: hidden;
  }}
  .hero::before {{
    content: '';
    position: absolute; inset: 0;
    background: radial-gradient(ellipse 60% 80% at 80% 50%, rgba(91,141,238,0.08) 0%, transparent 70%);
    pointer-events: none;
  }}
  .hero h1 {{ font-family: var(--font-head); font-size: 2rem; font-weight: 800; letter-spacing: -0.03em; }}
  .hero h1 span {{ color: var(--accent-neu); }}
  .meta {{ margin-top: 12px; color: var(--text-dim); font-size: 11px; display: flex; gap: 24px; flex-wrap: wrap; }}
  .meta strong {{ color: var(--text); }}

  .wrapper {{ max-width: 1280px; margin: 0 auto; padding: 32px 40px; }}

  /* KPI cards */
  .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 36px; }}
  .kpi {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px 24px;
    position: relative;
    overflow: hidden;
  }}
  .kpi::after {{
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0; height: 3px;
    border-radius: 12px 12px 0 0;
  }}
  .kpi.add::after  {{ background: var(--accent-add); }}
  .kpi.rem::after  {{ background: var(--accent-rem); }}
  .kpi.neu::after  {{ background: var(--accent-neu); }}
  .kpi.tot::after  {{ background: #a855f7; }}
  .kpi.files::after {{ background: #f59e0b; }}
  .kpi-label {{ font-size: 10px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--text-dim); margin-bottom: 8px; }}
  .kpi-value {{ font-family: var(--font-head); font-size: 2rem; font-weight: 800; }}
  .kpi.add  .kpi-value {{ color: var(--accent-add); }}
  .kpi.rem  .kpi-value {{ color: var(--accent-rem); }}
  .kpi.neu  .kpi-value {{ color: var(--accent-neu); }}
  .kpi.tot  .kpi-value {{ color: #a855f7; }}
  .kpi.files .kpi-value {{ color: #f59e0b; }}

  /* Section headers */
  h2 {{ font-family: var(--font-head); font-size: 1rem; font-weight: 700; letter-spacing: 0.05em; color: var(--text-dim); text-transform: uppercase; margin-bottom: 16px; padding-bottom: 8px; border-bottom: 1px solid var(--border); }}

  /* Two-col layout */
  .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-bottom: 36px; }}
  @media (max-width: 900px) {{ .two-col {{ grid-template-columns: 1fr; }} }}

  .panel {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 24px;
  }}

  /* Bar chart rows */
  .bar-row {{ display: flex; align-items: center; gap: 12px; margin-bottom: 10px; font-size: 12px; }}
  .bar-label {{ width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-dim); flex-shrink: 0; }}
  .bar-track {{ flex: 1; height: 8px; background: var(--bg); border-radius: 4px; overflow: hidden; display: flex; }}
  .bar-add  {{ height: 100%; border-radius: 4px 0 0 4px; background: var(--accent-add); transition: width 0.4s; }}
  .bar-rem  {{ height: 100%; border-radius: 0 4px 4px 0; background: var(--accent-rem); transition: width 0.4s; }}
  .bar-nums {{ width: 80px; text-align: right; font-size: 11px; }}
  .bar-nums .a {{ color: var(--accent-add); }}
  .bar-nums .r {{ color: var(--accent-rem); }}

  /* File table */
  .file-table-wrap {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; margin-bottom: 36px; }}
  .table-header {{
    display: grid;
    grid-template-columns: 1fr 80px 80px 100px 160px;
    padding: 10px 20px;
    background: var(--surface2);
    border-bottom: 1px solid var(--border);
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-dim);
  }}
  .file-row {{
    display: grid;
    grid-template-columns: 1fr 80px 80px 100px 160px;
    padding: 8px 20px;
    border-bottom: 1px solid var(--border);
    align-items: center;
    transition: background 0.15s;
  }}
  .file-row:last-child {{ border-bottom: none; }}
  .file-row:hover {{ background: var(--surface2); }}
  .filepath {{ font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }}
  .added   {{ color: var(--accent-add); text-align: right; }}
  .removed {{ color: var(--accent-rem); text-align: right; }}
  .delta   {{ text-align: right; }}
  .delta.pos {{ color: var(--accent-add); }}
  .delta.neg {{ color: var(--accent-rem); }}
  .delta.zer {{ color: var(--text-dim); }}
  .inline-bar {{ display: flex; height: 6px; border-radius: 3px; overflow: hidden; width: 100%; }}
  .ib-add {{ background: var(--accent-add); }}
  .ib-rem {{ background: var(--accent-rem); }}

  .search-row {{ padding: 16px 20px; background: var(--surface2); border-bottom: 1px solid var(--border); }}
  input[type=text] {{
    width: 100%; max-width: 400px;
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    color: var(--text); font-family: var(--font-mono); font-size: 12px;
    padding: 8px 14px; outline: none;
  }}
  input[type=text]:focus {{ border-color: var(--accent-neu); }}

  .show-more {{ text-align: center; padding: 14px; cursor: pointer; color: var(--accent-neu); font-size: 12px; }}
  .show-more:hover {{ color: #fff; }}

  .footer {{ text-align: center; color: var(--text-dim); font-size: 11px; padding: 32px 0; border-top: 1px solid var(--border); margin-top: 24px; }}
</style>
</head>
<body>

<div class="hero">
  <h1>Repo <span>Diff</span> Report</h1>
  <div class="meta">
    <div><strong>Current&nbsp;&nbsp;</strong> {current_url}</div>
    <div><strong>Upstream</strong> {upstream_url}</div>
    <div><strong>Branch&nbsp;&nbsp;</strong> {branch}</div>
    <div><strong>Generated</strong> {generated}</div>
  </div>
</div>

<div class="wrapper">

  <!-- KPI cards -->
  <div class="kpis">
    <div class="kpi add"><div class="kpi-label">Lines Added</div><div class="kpi-value">{total_added}</div></div>
    <div class="kpi rem"><div class="kpi-label">Lines Removed</div><div class="kpi-value">{total_removed}</div></div>
    <div class="kpi neu"><div class="kpi-label">Net Delta</div><div class="kpi-value">{net_delta:+d}</div></div>
    <div class="kpi tot"><div class="kpi-label">Total Changes</div><div class="kpi-value">{total_changed}</div></div>
    <div class="kpi files"><div class="kpi-label">Files Changed</div><div class="kpi-value">{file_count}</div></div>
  </div>

  <!-- Breakdown charts -->
  <div class="two-col">
    <div class="panel">
      <h2>By File Extension</h2>
      {ext_bars}
    </div>
    <div class="panel">
      <h2>By Top-Level Directory</h2>
      {dir_bars}
    </div>
  </div>

  <!-- File table -->
  <div class="file-table-wrap">
    <div class="search-row">
      <input type="text" id="search" placeholder="Filter files…" oninput="filterTable()"/>
    </div>
    <div class="table-header">
      <div>File</div>
      <div style="text-align:right">+Added</div>
      <div style="text-align:right">−Removed</div>
      <div style="text-align:right">Δ Delta</div>
      <div>Change</div>
    </div>
    <div id="file-rows">
      {file_rows}
    </div>
  </div>

</div>

<div class="footer">Generated by repo_diff_report.py &nbsp;·&nbsp; {generated}</div>

<script>
function filterTable() {{
  const q = document.getElementById('search').value.toLowerCase();
  document.querySelectorAll('.file-row').forEach(r => {{
    r.style.display = r.querySelector('.filepath').textContent.toLowerCase().includes(q) ? '' : 'none';
  }});
}}
</script>
</body>
</html>
"""


def make_bar_rows(stats: dict, max_total: int) -> str:
    html = ""
    for label, s in sorted(stats.items(), key=lambda x: x[1]["added"] + x[1]["removed"], reverse=True):
        total = s["added"] + s["removed"]
        if total == 0:
            continue
        pct_add = (s["added"] / max_total) * 100 if max_total else 0
        pct_rem = (s["removed"] / max_total) * 100 if max_total else 0
        html += f"""
        <div class="bar-row">
          <div class="bar-label" title="{label}">{label}</div>
          <div class="bar-track">
            <div class="bar-add" style="width:{pct_add:.1f}%"></div>
            <div class="bar-rem" style="width:{pct_rem:.1f}%"></div>
          </div>
          <div class="bar-nums"><span class="a">+{s['added']}</span> <span class="r">-{s['removed']}</span></div>
        </div>"""
    return html


def make_file_rows(files: list) -> str:
    html = ""
    for f in files:
        total = f["added"] + f["removed"]
        if total == 0:
            pct_add = pct_rem = 0
        else:
            pct_add = (f["added"] / total) * 100
            pct_rem = (f["removed"] / total) * 100

        delta = f["delta"]
        delta_cls = "pos" if delta > 0 else ("neg" if delta < 0 else "zer")
        delta_str = f"{delta:+d}" if delta != 0 else "0"

        html += f"""
        <div class="file-row">
          <div class="filepath" title="{f['path']}">{f['path']}</div>
          <div class="added">+{f['added']}</div>
          <div class="removed">-{f['removed']}</div>
          <div class="delta {delta_cls}">{delta_str}</div>
          <div class="inline-bar">
            <div class="ib-add" style="width:{pct_add:.1f}%"></div>
            <div class="ib-rem" style="width:{pct_rem:.1f}%"></div>
          </div>
        </div>"""
    return html


def generate_html(stats: dict, current_url: str, upstream_url: str, branch: str) -> str:
    max_ext = max((v["added"] + v["removed"] for v in stats["ext_stats"].values()), default=1)
    max_dir = max((v["added"] + v["removed"] for v in stats["dir_stats"].values()), default=1)

    return HTML_TEMPLATE.format(
        current_url=current_url,
        upstream_url=upstream_url,
        branch=branch,
        generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        total_added=f"{stats['total_added']:,}",
        total_removed=f"{stats['total_removed']:,}",
        net_delta=stats["net_delta"],
        total_changed=f"{stats['total_changed']:,}",
        file_count=f"{stats['file_count']:,}",
        ext_bars=make_bar_rows(stats["ext_stats"], max_ext),
        dir_bars=make_bar_rows(stats["dir_stats"], max_dir),
        file_rows=make_file_rows(stats["files"]),
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Diff two GitHub repos and generate a LOC change report."
    )
    parser.add_argument("--current",         required=True, help="URL of YOUR modified repo")
    parser.add_argument("--upstream",        required=True, help="URL of the upstream (original) repo")
    parser.add_argument("--pat",             default=None,  help="Personal Access Token (optional, for private repos)")
    parser.add_argument("--ref",             default="master", help="Shared git ref (branch, tag, or commit SHA) for both repos (default: master)")
    parser.add_argument("--current-ref",     default=None,  help="Git ref for YOUR repo (branch/tag/SHA) — overrides --ref")
    parser.add_argument("--upstream-ref",    default=None,  help="Git ref for upstream repo (branch/tag/SHA) — overrides --ref")
    parser.add_argument("--output",          default="diff_report.html", help="Output HTML file path")
    parser.add_argument("--json-summary",    default=None,  help="Optional path to write a JSON summary (for aggregation)")
    args = parser.parse_args()

    current_ref  = args.current_ref  or args.ref
    upstream_ref = args.upstream_ref or args.ref

    tmpdir = tempfile.mkdtemp(prefix="repo_diff_")
    current_dir  = os.path.join(tmpdir, "current")
    upstream_dir = os.path.join(tmpdir, "upstream")

    try:
        print("\n📦 Cloning repositories…")
        ok1 = clone_repo(args.current,  current_dir,  args.pat, current_ref)
        ok2 = clone_repo(args.upstream, upstream_dir, args.pat, upstream_ref)

        if not ok1 or not ok2:
            print("[ERROR] Cloning failed. Check URLs, PAT, and ref (branch/tag/SHA).", file=sys.stderr)
            sys.exit(1)

        print("\n🔍 Analysing differences…")
        stats = diff_repos(current_dir, upstream_dir)

        print(f"\n📊 Summary:")
        print(f"   Files changed : {stats['file_count']}")
        print(f"   Lines added   : {stats['total_added']:,}")
        print(f"   Lines removed : {stats['total_removed']:,}")
        print(f"   Net delta     : {stats['net_delta']:+,}")

        print(f"\n📝 Generating HTML report → {args.output}")
        ref_label = current_ref if current_ref == upstream_ref else f"{current_ref} → {upstream_ref}"
        html = generate_html(stats, args.current, args.upstream, ref_label)
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(html)

        # Emit JSON summary for the shell-level aggregator
        if args.json_summary:
            import json
            repo_name = args.current.rstrip("/").split("/")[-1]
            summary = {
                "repo":          repo_name,
                "current_url":   args.current,
                "upstream_url":  args.upstream,
                "current_ref": current_ref, "upstream_ref": upstream_ref,
                "report_file":   args.output,
                "files_changed": stats["file_count"],
                "lines_added":   stats["total_added"],
                "lines_removed": stats["total_removed"],
                "net_delta":     stats["net_delta"],
                "total_changed": stats["total_changed"],
                "status":        "ok",
            }
            with open(args.json_summary, "w", encoding="utf-8") as jf:
                json.dump(summary, jf, indent=2)
            print(f"📄 JSON summary → {args.json_summary}")

        print(f"✅ Done!  Open {args.output} in your browser.")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
