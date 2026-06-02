#!/usr/bin/env python3
"""
qualitative_report.py
─────────────────────
Generates a qualitative HTML report explaining the *additions* in your forked
repos versus upstream, using:
  • git log  — commit messages (the "why")
  • git diff — actual added code/config (the "what")
  • Claude API — narrative summaries at repo level and per-file level

Usage:
    python3 qualitative_report.py \
        --current  https://github.com/your-org/ccsdk-oran \
        --upstream https://github.com/onap/ccsdk-oran \
        --pat      ghp_xxxxxxxxxxxxxxxxxxxx \
        --current-ref  my-netconf-fix \
        --upstream-ref 2.3.0 \
        --anthropic-key sk-ant-xxxxxxxxxxxxxxxxxx \
        --output   ccsdk-oran-qualitative.html

    # Or set env vars:
    export GITHUB_PAT=ghp_...
    export ANTHROPIC_API_KEY=sk-ant-...
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# ── constants ─────────────────────────────────────────────────────────────────

CLAUDE_MODEL      = "claude-sonnet-4-20250514"
MAX_TOKENS        = 1500
MAX_DIFF_CHARS    = 6000   # chars of diff sent per file
PER_FILE_THRESHOLD = 5     # min added lines to warrant per-file analysis

SKIP_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf",
    ".zip", ".tar", ".gz", ".jar", ".class", ".bin", ".so",
    ".dll", ".exe", ".lock", ".sum",
}

CHANGE_CATEGORIES = {
    "feat":     ("✨", "#22d3a5", "Feature"),
    "fix":      ("🐛", "#f4545e", "Bug Fix"),
    "refactor": ("♻️",  "#5b8dee", "Refactor"),
    "chore":    ("🔧", "#94a3b8", "Chore"),
    "docs":     ("📝", "#f59e0b", "Docs"),
    "test":     ("🧪", "#a855f7", "Test"),
    "build":    ("📦", "#ec4899", "Build"),
    "other":    ("💡", "#64748b", "Other"),
}

# ── git helpers ───────────────────────────────────────────────────────────────

def inject_pat(url, pat):
    parsed = urlparse(url)
    return parsed._replace(netloc=f"{pat}@{parsed.netloc}").geturl()


def run(cmd, cwd=None, silent=False):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0 and not silent:
        print(f"  [WARN] {' '.join(cmd[:4])}  rc={r.returncode}", file=sys.stderr)
    return r.returncode, r.stdout, r.stderr


def is_sha(ref):
    return bool(re.fullmatch(r"[0-9a-f]{7,40}", ref, re.IGNORECASE))


def clone_repo(url, dest, pat=None, ref="master"):
    clone_url = inject_pat(url, pat) if pat else url
    ref_type  = "commit" if is_sha(ref) else "branch/tag"
    print(f"  Cloning [{ref_type}: {ref}]  {url}")

    if is_sha(ref):
        rc, _, _ = run(["git", "clone", "--quiet", clone_url, dest])
        if rc != 0:
            return False
        rc, _, _ = run(["git", "checkout", ref], cwd=dest)
        return rc == 0
    else:
        rc, _, _ = run(["git", "clone", "--depth=1", "--branch", ref, clone_url, dest])
        if rc == 0:
            return True
        print(f"  [WARN] --branch {ref!r} failed, retrying with default branch…")
        rc, _, _ = run(["git", "clone", "--depth=1", clone_url, dest])
        return rc == 0


def get_commits(current_dir, upstream_dir):
    _, up_sha, _ = run(["git", "rev-parse", "HEAD"], cwd=upstream_dir, silent=True)
    up_sha = up_sha.strip()

    _, merge_base, _ = run(
        ["git", "merge-base", "HEAD", up_sha], cwd=current_dir, silent=True
    )
    merge_base = merge_base.strip()
    log_range  = f"{merge_base}..HEAD" if merge_base else "HEAD~20..HEAD"

    fmt = "--pretty=format:%H\x1f%s\x1f%b\x1f%an\x1f%ad"
    _, log_out, _ = run(
        ["git", "log", log_range, fmt, "--date=short"], cwd=current_dir, silent=True
    )

    commits = []
    for line in log_out.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("\x1f")
        if len(parts) < 5:
            continue
        sha, subject, body, author, date = parts[:5]
        commits.append({
            "sha":      sha.strip()[:8],
            "subject":  subject.strip(),
            "body":     body.strip(),
            "author":   author.strip(),
            "date":     date.strip(),
            "category": classify_commit(subject),
        })
    return commits


def classify_commit(subject):
    s = subject.lower()
    if any(s.startswith(p) for p in ("feat", "feature", "add ", "added", "new ")):
        return "feat"
    if any(s.startswith(p) for p in ("fix", "bug", "hotfix", "patch")):
        return "fix"
    if any(s.startswith(p) for p in ("refactor", "rework", "restructure", "move", "rename", "clean")):
        return "refactor"
    if any(s.startswith(p) for p in ("doc", "readme", "comment")):
        return "docs"
    if any(s.startswith(p) for p in ("test", "spec", "coverage")):
        return "test"
    if any(s.startswith(p) for p in ("chore", "bump", "upgrade", "update dep", "merge")):
        return "chore"
    if any(s.startswith(p) for p in ("build", "ci", "cd", "pipeline", "makefile", "pom", "gradle")):
        return "build"
    return "other"


def get_additions_diff(current_dir, upstream_dir):
    _, numstat, _ = run(
        ["git", "diff", "--no-index", "--numstat", upstream_dir, current_dir],
        silent=True
    )
    _, patch, _ = run(
        ["git", "diff", "--no-index", "-U3", upstream_dir, current_dir],
        silent=True
    )

    # Split patch into per-file chunks
    file_patches = {}
    current_file = None
    current_chunk = []

    for line in patch.splitlines():
        if line.startswith("diff --git"):
            if current_file and current_chunk:
                file_patches[current_file] = "\n".join(current_chunk)
            current_chunk = [line]
            current_file = None
        elif line.startswith("+++ ") and current_file is None:
            raw = line[4:]
            for prefix in (current_dir, upstream_dir, "b/", "a/"):
                raw = raw.replace(prefix, "")
            current_file = raw.lstrip("/") or "(unknown)"
            current_chunk.append(line)
        else:
            current_chunk.append(line)

    if current_file and current_chunk:
        file_patches[current_file] = "\n".join(current_chunk)

    results = []
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added_s, _, filepath = parts
        if added_s == "-":
            continue
        added = int(added_s)
        if added < PER_FILE_THRESHOLD:
            continue

        for prefix in (current_dir, upstream_dir, "b/"):
            filepath = filepath.replace(prefix, "")
        filepath = filepath.lstrip("/")

        if Path(filepath).suffix.lower() in SKIP_EXTENSIONS:
            continue

        diff_text = file_patches.get(filepath, "")
        added_lines = "\n".join(
            l for l in diff_text.splitlines()
            if l.startswith("+") and not l.startswith("+++")
        )
        if len(added_lines) > MAX_DIFF_CHARS:
            added_lines = added_lines[:MAX_DIFF_CHARS] + "\n... [truncated]"

        results.append({"path": filepath, "added": added, "diff_text": added_lines})

    results.sort(key=lambda x: x["added"], reverse=True)
    return results


# ── Claude API ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a senior software engineer writing a technical change report. "
    "Your audience is engineering leads reviewing a codebase migration. "
    "Be precise, concise, and technical. Use plain English, no marketing language. "
    "Always respond in valid HTML fragments (no <html>/<body> tags). "
    "Use <p>, <ul>, <li>, <code>, <strong> only. No markdown."
)


def call_claude(prompt, api_key):
    payload = {
        "model":      CLAUDE_MODEL,
        "max_tokens": MAX_TOKENS,
        "system":     SYSTEM_PROMPT,
        "messages":   [{"role": "user", "content": prompt}],
    }
    body = json.dumps(payload).encode()
    req  = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            return data["content"][0]["text"].strip()
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"  [ERROR] Claude API {e.code}: {err[:200]}", file=sys.stderr)
        return f"<p>[API error {e.code} — check your ANTHROPIC_API_KEY]</p>"
    except Exception as e:
        print(f"  [ERROR] Claude API: {e}", file=sys.stderr)
        return f"<p>[API error: {e}]</p>"


def summarise_repo(repo_name, commits, file_additions, api_key):
    commit_block = "\n".join(
        f"- [{c['category'].upper()}] {c['subject']}" +
        (f"\n  {c['body'][:200]}" if c["body"] else "")
        for c in commits[:40]
    ) or "(no unique commits — changes may be uncommitted)"

    top_files = "\n".join(
        f"- {f['path']} (+{f['added']} lines)"
        for f in file_additions[:15]
    ) or "(none above threshold)"

    prompt = (
        f"Repository: {repo_name}\n\n"
        f"COMMIT MESSAGES (your fork vs upstream):\n{commit_block}\n\n"
        f"TOP CHANGED FILES (additions):\n{top_files}\n\n"
        "Write a 4-6 sentence executive summary of what was changed in this "
        "repository and why, based on the commit messages and file list above. "
        "Focus on: purpose of the changes, which components are affected, "
        "and any notable patterns (e.g. protocol version upgrades, new APIs, "
        "dependency changes). Output as HTML <p> tags."
    )
    print("    → Repo-level summary…")
    return call_claude(prompt, api_key)


def summarise_file(repo_name, filepath, added, diff_text, api_key):
    prompt = (
        f"Repository: {repo_name}\n"
        f"File: {filepath}\n"
        f"Lines added: {added}\n\n"
        f"ADDED LINES (from git diff):\n```\n{diff_text}\n```\n\n"
        "In 2-4 sentences, explain what these additions do. "
        "Be specific to the code/config shown. Mention: "
        "what the new code/config introduces or changes, "
        "any protocol, standard, version, or API references visible in the diff, "
        "and the likely purpose or impact of the change. "
        "Output as HTML <p> tags only."
    )
    return call_claude(prompt, api_key)


# ── HTML ──────────────────────────────────────────────────────────────────────

CSS = """
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Syne:wght@400;700;800&display=swap');
:root{--bg:#0b0f1a;--surface:#111827;--surface2:#1a2236;--border:#1f2d44;
  --add:#22d3a5;--rem:#f4545e;--neu:#5b8dee;--warn:#f59e0b;--purple:#a855f7;
  --text:#e2e8f0;--dim:#64748b;
  --mono:'JetBrains Mono',monospace;--head:'Syne',sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:13px;line-height:1.7}
.hero{background:linear-gradient(135deg,#0b0f1a,#0d1929 60%,#101e30);border-bottom:1px solid var(--border);padding:48px 40px 32px;position:relative;overflow:hidden}
.hero::before{content:'';position:absolute;inset:0;background:radial-gradient(ellipse 60% 80% at 85% 40%,rgba(91,141,238,.09) 0%,transparent 70%);pointer-events:none}
.hero h1{font-family:var(--head);font-size:2rem;font-weight:800;letter-spacing:-.03em}
.hero h1 span{color:var(--neu)}
.meta{margin-top:10px;color:var(--dim);font-size:11px;display:flex;gap:24px;flex-wrap:wrap}
.meta strong{color:var(--text)}
.wrap{max-width:1100px;margin:0 auto;padding:36px 40px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:14px;margin-bottom:36px}
.kpi{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:18px 20px;position:relative;overflow:hidden}
.kpi::after{content:'';position:absolute;top:0;left:0;right:0;height:3px;border-radius:12px 12px 0 0}
.kpi.k1::after{background:var(--add)}.kpi.k2::after{background:var(--neu)}
.kpi.k3::after{background:var(--warn)}.kpi.k4::after{background:var(--purple)}
.kpi-label{font-size:10px;text-transform:uppercase;letter-spacing:.12em;color:var(--dim);margin-bottom:6px}
.kpi-value{font-family:var(--head);font-size:1.8rem;font-weight:800}
.kpi.k1 .kpi-value{color:var(--add)}.kpi.k2 .kpi-value{color:var(--neu)}
.kpi.k3 .kpi-value{color:var(--warn)}.kpi.k4 .kpi-value{color:var(--purple)}
h2{font-family:var(--head);font-size:.85rem;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--dim);margin-bottom:14px;padding-bottom:8px;border-bottom:1px solid var(--border)}
.taxonomy{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:32px}
.pill{display:flex;align-items:center;gap:7px;background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:6px 14px;font-size:11px}
.pill-count{font-family:var(--head);font-weight:800;font-size:1rem}
.commit-list{display:flex;flex-direction:column;gap:6px;margin-bottom:32px}
.commit{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:10px 14px;display:flex;gap:12px;align-items:flex-start}
.commit-cat{font-size:10px;padding:2px 8px;border-radius:10px;font-weight:600;white-space:nowrap;margin-top:1px;flex-shrink:0}
.commit-sha{font-size:10px;color:var(--dim);flex-shrink:0;margin-top:2px}
.commit-subject{flex:1;font-size:12px;color:var(--text)}
.commit-body{font-size:11px;color:var(--dim);margin-top:4px}
.commit-meta{font-size:10px;color:var(--dim);flex-shrink:0;text-align:right;white-space:nowrap;display:flex;flex-direction:column;gap:2px}
.ai-box{background:var(--surface2);border:1px solid var(--border);border-left:3px solid var(--neu);border-radius:10px;padding:20px 22px;margin-bottom:28px}
.ai-label{font-size:10px;text-transform:uppercase;letter-spacing:.12em;color:var(--neu);margin-bottom:10px}
.ai-box p,.file-ai p{color:var(--text);font-size:13px;line-height:1.75;margin-bottom:8px}
.ai-box p:last-child,.file-ai p:last-child{margin-bottom:0}
.ai-box ul,.file-ai ul{padding-left:18px;color:var(--text);font-size:13px;line-height:1.75;margin-bottom:8px}
.ai-box li,.file-ai li{margin-bottom:4px}
.ai-box code,.file-ai code{background:rgba(255,255,255,.07);border-radius:4px;padding:1px 5px;font-size:11px}
.ai-box strong,.file-ai strong{color:var(--add)}
.file-cards{display:flex;flex-direction:column;gap:12px;margin-bottom:32px}
.file-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.file-card-header{background:var(--surface2);padding:10px 16px;display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--border);cursor:pointer;user-select:none}
.file-card-header:hover{background:#1e2d42}
.file-path{flex:1;font-size:12px;color:var(--add);font-family:var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.file-added{font-size:11px;color:var(--add);white-space:nowrap;flex-shrink:0}
.file-ext{font-size:10px;background:rgba(91,141,238,.15);color:var(--neu);border-radius:4px;padding:2px 7px;flex-shrink:0}
.toggle-icon{color:var(--dim);font-size:12px;flex-shrink:0;width:16px;text-align:center}
.file-card-body{padding:16px;display:none}
.file-card-body.open{display:block}
.file-ai{border-left:3px solid var(--add);padding-left:14px}
.footer{text-align:center;color:var(--dim);font-size:11px;padding:28px 0;border-top:1px solid var(--border);margin-top:8px}
"""

CAT_STYLES = {
    "feat":     "background:rgba(34,211,165,.15);color:#22d3a5",
    "fix":      "background:rgba(244,84,94,.15);color:#f4545e",
    "refactor": "background:rgba(91,141,238,.15);color:#5b8dee",
    "chore":    "background:rgba(148,163,184,.12);color:#94a3b8",
    "docs":     "background:rgba(245,158,11,.15);color:#f59e0b",
    "test":     "background:rgba(168,85,247,.15);color:#a855f7",
    "build":    "background:rgba(236,72,153,.15);color:#ec4899",
    "other":    "background:rgba(100,116,139,.15);color:#64748b",
}


def render_taxonomy(commits):
    counts = {}
    for c in commits:
        counts[c["category"]] = counts.get(c["category"], 0) + 1
    if not counts:
        return ""
    html = '<div class="taxonomy">'
    for cat, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        icon, color, label = CHANGE_CATEGORIES[cat]
        html += (f'<div class="pill"><span>{icon}</span>'
                 f'<span class="pill-count" style="color:{color}">{cnt}</span>'
                 f'<span style="color:var(--dim)">{label}</span></div>')
    return html + "</div>"


def render_commits(commits):
    if not commits:
        return '<p style="color:var(--dim);font-style:italic">No unique commits found — changes may be uncommitted.</p>'
    html = '<div class="commit-list">'
    for c in commits:
        cat   = c["category"]
        icon, _, label = CHANGE_CATEGORIES[cat]
        style = CAT_STYLES[cat]
        body_html = f'<div class="commit-body">{c["body"][:300]}</div>' if c["body"] else ""
        html += (f'<div class="commit">'
                 f'<span class="commit-cat" style="{style}">{icon} {label}</span>'
                 f'<div style="flex:1"><div class="commit-subject">{c["subject"]}</div>{body_html}</div>'
                 f'<div class="commit-meta"><span class="commit-sha">{c["sha"]}</span>'
                 f'<span>{c["date"]}</span><span>{c["author"]}</span></div>'
                 f'</div>')
    return html + "</div>"


def render_file_cards(file_summaries):
    if not file_summaries:
        return '<p style="color:var(--dim);font-style:italic">No files above the analysis threshold.</p>'
    html = '<div class="file-cards">'
    for i, fs in enumerate(file_summaries):
        ext  = Path(fs["path"]).suffix.lower() or "?"
        body = fs.get("summary", "<p>Analysis not available.</p>")
        html += (f'<div class="file-card">'
                 f'<div class="file-card-header" onclick="toggle({i})">'
                 f'<span class="file-ext">{ext}</span>'
                 f'<span class="file-path" title="{fs["path"]}">{fs["path"]}</span>'
                 f'<span class="file-added">+{fs["added"]} lines</span>'
                 f'<span class="toggle-icon" id="icon-{i}">▶</span>'
                 f'</div>'
                 f'<div class="file-card-body" id="body-{i}">'
                 f'<div class="file-ai">{body}</div>'
                 f'</div></div>')
    return html + "</div>"


def build_html(repo_name, current_url, upstream_url, current_ref, upstream_ref,
               commits, repo_summary, file_summaries, total_added):
    ref_label = (current_ref if current_ref == upstream_ref
                 else f"{current_ref} → {upstream_ref}")
    n_files   = len(file_summaries)
    n_commits = len(commits)
    cat_counts = {}
    for c in commits:
        cat_counts[c["category"]] = cat_counts.get(c["category"], 0) + 1
    dominant   = max(cat_counts, key=cat_counts.get) if cat_counts else "other"
    dom_icon, _, dom_label = CHANGE_CATEGORIES[dominant]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Qualitative Report — {repo_name}</title>
<style>{CSS}</style>
</head>
<body>
<div class="hero">
  <h1><span>Qualitative</span> Change Report</h1>
  <div class="meta">
    <div><strong>Repo</strong> {repo_name}</div>
    <div><strong>Current</strong> {current_url}</div>
    <div><strong>Upstream</strong> {upstream_url}</div>
    <div><strong>Ref</strong> {ref_label}</div>
    <div><strong>Generated</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
  </div>
</div>
<div class="wrap">
  <div class="kpis">
    <div class="kpi k1"><div class="kpi-label">Lines Added</div><div class="kpi-value">{total_added:,}</div></div>
    <div class="kpi k2"><div class="kpi-label">Commits</div><div class="kpi-value">{n_commits}</div></div>
    <div class="kpi k3"><div class="kpi-label">Files Analysed</div><div class="kpi-value">{n_files}</div></div>
    <div class="kpi k4"><div class="kpi-label">Dominant Type</div><div class="kpi-value" style="font-size:1.2rem">{dom_icon} {dom_label}</div></div>
  </div>

  <h2>Executive Summary</h2>
  <div class="ai-box">
    <div class="ai-label">⬡ AI-Generated · Claude {CLAUDE_MODEL}</div>
    {repo_summary}
  </div>

  <h2>Commit Taxonomy</h2>
  {render_taxonomy(commits)}

  <h2>Commit Log</h2>
  {render_commits(commits)}

  <h2>Per-File Analysis <span style="font-weight:400;font-size:.85em;color:var(--dim)">(≥{PER_FILE_THRESHOLD} added lines)</span></h2>
  {render_file_cards(file_summaries)}
</div>
<div class="footer">Generated by qualitative_report.py using Claude API &nbsp;·&nbsp; {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
<script>
function toggle(i){{
  var b=document.getElementById('body-'+i);
  var ic=document.getElementById('icon-'+i);
  var open=b.classList.toggle('open');
  ic.textContent=open?'▼':'▶';
}}
window.addEventListener('DOMContentLoaded',function(){{if(document.querySelector('.file-card-body'))toggle(0);}});
</script>
</body>
</html>"""


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate a qualitative HTML report explaining additions using Claude AI."
    )
    parser.add_argument("--current",        required=True)
    parser.add_argument("--upstream",       required=True)
    parser.add_argument("--pat",            default=os.environ.get("GITHUB_PAT"))
    parser.add_argument("--ref",            default="master")
    parser.add_argument("--current-ref",    default=None)
    parser.add_argument("--upstream-ref",   default=None)
    parser.add_argument("--anthropic-key",  default=os.environ.get("ANTHROPIC_API_KEY"))
    parser.add_argument("--output",         default="qualitative_report.html")
    parser.add_argument("--file-threshold", type=int, default=5,
                        help="Min added lines to analyse a file (default: 5)")
    parser.add_argument("--max-files",      type=int, default=20,
                        help="Max files to send for per-file AI analysis (default: 20)")
    parser.add_argument("--json-summary",   default=None)
    args = parser.parse_args()

    global PER_FILE_THRESHOLD
    PER_FILE_THRESHOLD = args.file_threshold

    if not args.anthropic_key:
        print("[ERROR] Anthropic API key required. Use --anthropic-key or ANTHROPIC_API_KEY.", file=sys.stderr)
        sys.exit(1)

    current_ref  = args.current_ref  or args.ref
    upstream_ref = args.upstream_ref or args.ref
    repo_name    = args.current.rstrip("/").split("/")[-1]

    tmpdir       = tempfile.mkdtemp(prefix="qual_diff_")
    current_dir  = os.path.join(tmpdir, "current")
    upstream_dir = os.path.join(tmpdir, "upstream")

    try:
        print(f"\n{'─'*54}\n  Qualitative Report: {repo_name}\n{'─'*54}")

        print("\n📦 Cloning repositories…")
        if not clone_repo(args.current,  current_dir,  args.pat, current_ref):
            print("[ERROR] Failed to clone current repo.", file=sys.stderr); sys.exit(1)
        if not clone_repo(args.upstream, upstream_dir, args.pat, upstream_ref):
            print("[ERROR] Failed to clone upstream repo.", file=sys.stderr); sys.exit(1)

        print("\n📋 Extracting commit log…")
        commits = get_commits(current_dir, upstream_dir)
        print(f"   {len(commits)} unique commit(s)")

        print("\n🔍 Extracting additions diff…")
        file_additions = get_additions_diff(current_dir, upstream_dir)
        total_added    = sum(f["added"] for f in file_additions)
        print(f"   {len(file_additions)} file(s) above threshold, {total_added:,} lines added")

        files_to_analyse = file_additions[:args.max_files]
        total_calls      = len(files_to_analyse) + 1

        print(f"\n🤖 Claude API calls: {total_calls} (1 repo summary + {len(files_to_analyse)} files)…")
        repo_summary = summarise_repo(repo_name, commits, file_additions, args.anthropic_key)

        file_summaries = []
        for idx, finfo in enumerate(files_to_analyse):
            print(f"    [{idx+2}/{total_calls}] {finfo['path']} (+{finfo['added']} lines)…")
            summary = summarise_file(
                repo_name, finfo["path"], finfo["added"],
                finfo["diff_text"], args.anthropic_key
            )
            file_summaries.append({
                "path":    finfo["path"],
                "added":   finfo["added"],
                "summary": summary,
            })

        print(f"\n📝 Writing → {args.output}")
        html = build_html(
            repo_name, args.current, args.upstream,
            current_ref, upstream_ref,
            commits, repo_summary, file_summaries, total_added
        )
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(html)

        if args.json_summary:
            with open(args.json_summary, "w", encoding="utf-8") as jf:
                json.dump({
                    "repo": repo_name, "current_url": args.current,
                    "upstream_url": args.upstream,
                    "current_ref": current_ref, "upstream_ref": upstream_ref,
                    "report_file": args.output, "commits": len(commits),
                    "files_analysed": len(file_summaries),
                    "total_added": total_added, "status": "ok",
                }, jf, indent=2)

        print("✅ Done!")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
