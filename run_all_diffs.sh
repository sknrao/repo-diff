#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# run_all_diffs.sh
# Run repo_diff_report.py for every repo pair in a config file, then produce
# a combined HTML summary dashboard across all repos.
#
# Usage:
#   ./run_all_diffs.sh [OPTIONS]
#
# Options:
#   -c, --config   FILE    Repo-pair config file       (default: repos.conf)
#   -p, --pat      TOKEN   GitHub Personal Access Token (default: $GITHUB_PAT)
#   -b, --ref      REF     Default git ref for both repos (default: master)
#   -o, --outdir   DIR     Output directory            (default: ./diff_reports)
#   -s, --script   FILE    Path to repo_diff_report.py (default: auto-detect)
#   -j, --jobs     N       Parallel jobs               (default: 1)
#   -q, --qualitative      Also run qualitative (AI) reports (needs --anthropic-key)
#   -a, --anthropic-key K  Anthropic API key (default: $ANTHROPIC_API_KEY)
#   -h, --help             Show this help
#
# Config file format (repos.conf):
#   CURRENT_URL  UPSTREAM_URL  [BRANCH]
#   # lines starting with # are comments
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

log()  { echo -e "${CYAN}[INFO]${RESET}  $*"; }
ok()   { echo -e "${GREEN}[ OK ]${RESET}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
err()  { echo -e "${RED}[FAIL]${RESET}  $*" >&2; }
sep()  { echo -e "${BOLD}────────────────────────────────────────────────${RESET}"; }

# ── defaults ──────────────────────────────────────────────────────────────────
CONFIG_FILE="repos.conf"
PAT="${GITHUB_PAT:-}"
DEFAULT_REF="master"
OUT_DIR="./diff_reports"
JOBS=1
QUALITATIVE=0
ANTHROPIC_KEY="${ANTHROPIC_API_KEY:-}"

# Auto-detect repo_diff_report.py next to this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="${SCRIPT_DIR}/repo_diff_report.py"

# ── arg parsing ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--config)  CONFIG_FILE="$2"; shift 2 ;;
    -p|--pat)     PAT="$2";         shift 2 ;;
    -b|--ref)     DEFAULT_REF="$2";    shift 2 ;;
    -o|--outdir)  OUT_DIR="$2";     shift 2 ;;
    -s|--script)  PY_SCRIPT="$2";   shift 2 ;;
    -j|--jobs)    JOBS="$2";        shift 2 ;;
    -q|--qualitative)  QUALITATIVE=1;          shift ;;
    -a|--anthropic-key) ANTHROPIC_KEY="$2";   shift 2 ;;
    -h|--help)
      grep '^#' "$0" | grep -v '^#!/' | sed 's/^# \?//'
      exit 0 ;;
    *) err "Unknown option: $1"; exit 1 ;;
  esac
done

# ── pre-flight checks ─────────────────────────────────────────────────────────
sep
echo -e "${BOLD}  Multi-Repo Diff Runner${RESET}"
sep

[[ -f "$CONFIG_FILE" ]] || { err "Config file not found: $CONFIG_FILE"; exit 1; }
[[ -f "$PY_SCRIPT"   ]] || { err "repo_diff_report.py not found: $PY_SCRIPT"; exit 1; }
command -v python3 &>/dev/null || { err "python3 not found in PATH"; exit 1; }
command -v git     &>/dev/null || { err "git not found in PATH"; exit 1; }

mkdir -p "$OUT_DIR"
JSON_DIR="${OUT_DIR}/.summaries"
mkdir -p "$JSON_DIR"
LOG_FILE="${OUT_DIR}/run_all.log"
> "$LOG_FILE"  # truncate

log "Config  : $CONFIG_FILE"
log "Output  : $OUT_DIR"
log "Script  : $PY_SCRIPT"
log "Ref     : $DEFAULT_REF (default)"
log "Jobs    : $JOBS"
[[ -n "$PAT" ]] && log "PAT     : ****${PAT: -4}" || warn "No PAT set — only public repos will work"
[[ $QUALITATIVE -eq 1 ]] && log "Mode    : quantitative + qualitative (AI)" || log "Mode    : quantitative only"
sep

# ── parse config into arrays ──────────────────────────────────────────────────
declare -a CURRENT_URLS UPSTREAM_URLS CURRENT_REFS UPSTREAM_REFS REPO_NAMES

while IFS= read -r line; do
  # strip comments and blank lines
  line="${line%%#*}"
  line="${line//[$'\t']/ }"   # tabs → spaces
  line="$(echo "$line" | xargs)"   # trim
  [[ -z "$line" ]] && continue

  # Columns: CURRENT  UPSTREAM  [SHARED_REF | CURRENT_REF  UPSTREAM_REF]
  # REF may be a branch name, a tag (e.g. 2.3.0), or a commit SHA
  read -r current upstream col3 col4 <<< "$line"
  if [[ -n "$col4" ]]; then
    # 4-column: separate refs for each side
    cur_ref="$col3"
    up_ref="$col4"
  else
    # 3-column: shared ref; 2-column: use default
    cur_ref="${col3:-$DEFAULT_REF}"
    up_ref="${col3:-$DEFAULT_REF}"
  fi
  repo_name="$(basename "$current" .git)"

  CURRENT_URLS+=("$current")
  UPSTREAM_URLS+=("$upstream")
  CURRENT_REFS+=("$cur_ref")
  UPSTREAM_REFS+=("$up_ref")
  REPO_NAMES+=("$repo_name")
done < "$CONFIG_FILE"

TOTAL=${#CURRENT_URLS[@]}
[[ $TOTAL -eq 0 ]] && { err "No repo pairs found in $CONFIG_FILE"; exit 1; }
log "Found $TOTAL repo pair(s) to process"
sep

# ── worker function ───────────────────────────────────────────────────────────
run_one() {
  local idx="$1"
  local current="${CURRENT_URLS[$idx]}"
  local upstream="${UPSTREAM_URLS[$idx]}"
  local cur_ref="${CURRENT_REFS[$idx]}"
  local up_ref="${UPSTREAM_REFS[$idx]}"
  local repo_name="${REPO_NAMES[$idx]}"
  local num=$(( idx + 1 ))

  local report_file="${OUT_DIR}/${repo_name}_diff.html"
  local json_file="${JSON_DIR}/${repo_name}.json"
  local log_prefix="[${num}/${TOTAL}] ${repo_name}"

  echo "" >> "$LOG_FILE"
  echo "=== $log_prefix ===" >> "$LOG_FILE"

  echo -e "\n${BOLD}${CYAN}[${num}/${TOTAL}]${RESET} ${BOLD}${repo_name}${RESET}"
  echo "  current  : $current"
  echo "  upstream : $upstream"
  echo "  ref      : ${cur_ref} (current)  /  ${up_ref} (upstream)"

  local pat_arg=()
  [[ -n "$PAT" ]] && pat_arg=(--pat "$PAT")

  if python3 "$PY_SCRIPT" \
        --current          "$current" \
        --upstream         "$upstream" \
        "${pat_arg[@]}" \
        --current-ref   "$cur_ref" \
        --upstream-ref  "$up_ref" \
        --output   "$report_file" \
        --json-summary "$json_file" \
        >> "$LOG_FILE" 2>&1; then
    ok "$repo_name  →  $(basename "$report_file")"
  if [[ $QUALITATIVE -eq 1 ]]; then
    local qual_file="${OUT_DIR}/${repo_name}_qualitative.html"
    local qual_args=(--current "$current" --upstream "$upstream" \
                     --current-ref "$cur_ref" --upstream-ref "$up_ref" \
                     --output "$qual_file")
    [[ -n "$PAT" ]]           && qual_args+=(--pat "$PAT")
    [[ -n "$ANTHROPIC_KEY" ]] && qual_args+=(--anthropic-key "$ANTHROPIC_KEY")
    if python3 "${SCRIPT_DIR}/qualitative_report.py" "${qual_args[@]}" >> "$LOG_FILE" 2>&1; then
      ok "$repo_name  qualitative →  $(basename "$qual_file")"
    else
      warn "$repo_name  qualitative FAILED (see $LOG_FILE)"
    fi
  fi
    echo "OK" > "${JSON_DIR}/${repo_name}.status"
  else
    err "$repo_name  FAILED (see $LOG_FILE)"
    # write a failure stub so the summary can still show it
    python3 - <<PYSTUB
import json, sys
with open("$json_file", "w") as f:
    json.dump({
        "repo": "$repo_name",
        "current_url": "$current",
        "upstream_url": "$upstream",
        "branch": "$branch",
        "report_file": "$report_file",
        "files_changed": 0,
        "lines_added": 0,
        "lines_removed": 0,
        "net_delta": 0,
        "total_changed": 0,
        "status": "failed"
    }, f, indent=2)
PYSTUB
    echo "FAILED" > "${JSON_DIR}/${repo_name}.status"
  fi
}

export -f run_one
export CURRENT_URLS UPSTREAM_URLS CURRENT_REFS UPSTREAM_REFS REPO_NAMES TOTAL QUALITATIVE ANTHROPIC_KEY
export OUT_DIR JSON_DIR LOG_FILE PAT PY_SCRIPT

# ── run (sequential or parallel) ─────────────────────────────────────────────
START_TIME=$(date +%s)
INDICES=()
for (( i=0; i<TOTAL; i++ )); do INDICES+=("$i"); done

if [[ $JOBS -gt 1 ]] && command -v parallel &>/dev/null; then
  log "Running with GNU parallel ($JOBS jobs)"
  printf '%s\n' "${INDICES[@]}" | parallel -j "$JOBS" run_one {}
elif [[ $JOBS -gt 1 ]]; then
  warn "--jobs > 1 requested but GNU parallel not found; running sequentially"
  for i in "${INDICES[@]}"; do run_one "$i"; done
else
  for i in "${INDICES[@]}"; do run_one "$i"; done
fi

END_TIME=$(date +%s)
ELAPSED=$(( END_TIME - START_TIME ))

# ── generate combined summary dashboard ──────────────────────────────────────
sep
log "Generating combined summary dashboard…"

SUMMARY_HTML="${OUT_DIR}/summary.html"

python3 - "$JSON_DIR" "$SUMMARY_HTML" "$ELAPSED" <<'PYEOF'
import sys, json, os
from pathlib import Path
from datetime import datetime

json_dir   = sys.argv[1]
out_html   = sys.argv[2]
elapsed    = int(sys.argv[3])

records = []
for jf in sorted(Path(json_dir).glob("*.json")):
    with open(jf) as f:
        records.append(json.load(f))

records.sort(key=lambda r: r["total_changed"], reverse=True)

grand_added   = sum(r["lines_added"]   for r in records)
grand_removed = sum(r["lines_removed"] for r in records)
grand_changed = sum(r["total_changed"] for r in records)
grand_delta   = sum(r["net_delta"]     for r in records)
grand_files   = sum(r["files_changed"] for r in records)
ok_count      = sum(1 for r in records if r["status"] == "ok")
fail_count    = len(records) - ok_count

max_changed   = max((r["total_changed"] for r in records), default=1)

def fmt(n): return f"{n:,}"
def pct(a, b): return (a / b * 100) if b else 0

repo_rows = ""
for r in records:
    status_badge = (
        '<span class="badge ok">✓ ok</span>'   if r["status"] == "ok"
        else '<span class="badge fail">✗ failed</span>'
    )
    bar_add = pct(r["lines_added"],   max_changed)
    bar_rem = pct(r["lines_removed"], max_changed)
    dval    = r["net_delta"]
    dcls    = "pos" if dval > 0 else ("neg" if dval < 0 else "zer")
    dstr    = f"{dval:+,}" if dval != 0 else "0"
    report_link = (
        f'<a href="{Path(r["report_file"]).name}" target="_blank">view →</a>'
        if r["status"] == "ok" else "—"
    )
    repo_rows += f"""
    <tr class="{'failed-row' if r['status'] != 'ok' else ''}">
      <td class="repo-name">{r['repo']}</td>
      <td class="added">+{fmt(r['lines_added'])}</td>
      <td class="removed">-{fmt(r['lines_removed'])}</td>
      <td class="delta {dcls}">{dstr}</td>
      <td class="files">{fmt(r['files_changed'])}</td>
      <td>
        <div class="mini-bar">
          <div class="mb-add" style="width:{bar_add:.1f}%"></div>
          <div class="mb-rem" style="width:{bar_rem:.1f}%"></div>
        </div>
      </td>
      <td>{status_badge}</td>
      <td class="report-link">{report_link}</td>
    </tr>"""

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Multi-Repo Diff Summary</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Syne:wght@400;700;800&display=swap');
  :root {{
    --bg:#0b0f1a; --surface:#111827; --surface2:#1a2236; --border:#1f2d44;
    --add:#22d3a5; --rem:#f4545e; --neu:#5b8dee; --warn:#f59e0b;
    --text:#e2e8f0; --dim:#64748b;
    --mono:'JetBrains Mono',monospace; --head:'Syne',sans-serif;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:13px;line-height:1.6}}

  .hero{{background:linear-gradient(135deg,#0b0f1a,#0d1929 60%,#0f1f35);border-bottom:1px solid var(--border);padding:48px 40px 32px;position:relative;overflow:hidden}}
  .hero::before{{content:'';position:absolute;inset:0;background:radial-gradient(ellipse 70% 90% at 90% 40%,rgba(34,211,165,.07) 0%,transparent 70%);pointer-events:none}}
  .hero h1{{font-family:var(--head);font-size:2rem;font-weight:800;letter-spacing:-.03em}}
  .hero h1 span{{color:var(--add)}}
  .meta{{margin-top:10px;color:var(--dim);font-size:11px;display:flex;gap:24px;flex-wrap:wrap}}
  .meta strong{{color:var(--text)}}

  .wrap{{max-width:1280px;margin:0 auto;padding:32px 40px}}

  .kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:16px;margin-bottom:36px}}
  .kpi{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px 22px;position:relative;overflow:hidden}}
  .kpi::after{{content:'';position:absolute;top:0;left:0;right:0;height:3px;border-radius:12px 12px 0 0}}
  .kpi.add::after{{background:var(--add)}} .kpi.rem::after{{background:var(--rem)}}
  .kpi.neu::after{{background:var(--neu)}} .kpi.tot::after{{background:#a855f7}}
  .kpi.files::after{{background:var(--warn)}} .kpi.repos::after{{background:#ec4899}}
  .kpi-label{{font-size:10px;text-transform:uppercase;letter-spacing:.12em;color:var(--dim);margin-bottom:8px}}
  .kpi-value{{font-family:var(--head);font-size:1.9rem;font-weight:800}}
  .kpi.add .kpi-value{{color:var(--add)}} .kpi.rem .kpi-value{{color:var(--rem)}}
  .kpi.neu .kpi-value{{color:var(--neu)}} .kpi.tot .kpi-value{{color:#a855f7}}
  .kpi.files .kpi-value{{color:var(--warn)}} .kpi.repos .kpi-value{{color:#ec4899}}

  h2{{font-family:var(--head);font-size:.9rem;font-weight:700;letter-spacing:.06em;color:var(--dim);text-transform:uppercase;margin-bottom:14px;padding-bottom:8px;border-bottom:1px solid var(--border)}}

  .table-wrap{{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden;margin-bottom:32px}}
  table{{width:100%;border-collapse:collapse}}
  thead tr{{background:var(--surface2)}}
  th{{padding:10px 16px;text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--dim);white-space:nowrap}}
  td{{padding:10px 16px;border-top:1px solid var(--border);vertical-align:middle}}
  tr:hover td{{background:var(--surface2)}}
  .failed-row td{{opacity:.55}}

  .repo-name{{font-weight:600;color:var(--text);white-space:nowrap}}
  .added{{color:var(--add);text-align:right;white-space:nowrap}}
  .removed{{color:var(--rem);text-align:right;white-space:nowrap}}
  .files{{text-align:right;white-space:nowrap;color:var(--dim)}}
  .delta{{text-align:right;white-space:nowrap}}
  .delta.pos{{color:var(--add)}} .delta.neg{{color:var(--rem)}} .delta.zer{{color:var(--dim)}}

  .mini-bar{{display:flex;height:8px;border-radius:4px;overflow:hidden;width:120px;background:var(--bg)}}
  .mb-add{{background:var(--add)}} .mb-rem{{background:var(--rem)}}

  .badge{{display:inline-block;padding:2px 8px;border-radius:20px;font-size:10px;font-weight:600}}
  .badge.ok{{background:rgba(34,211,165,.15);color:var(--add)}}
  .badge.fail{{background:rgba(244,84,94,.15);color:var(--rem)}}

  .report-link a{{color:var(--neu);text-decoration:none;font-size:11px}}
  .report-link a:hover{{color:#fff;text-decoration:underline}}

  tfoot td{{background:var(--surface2);font-weight:600;border-top:2px solid var(--border)}}
  tfoot .added{{color:var(--add)}} tfoot .removed{{color:var(--rem)}}

  .footer{{text-align:center;color:var(--dim);font-size:11px;padding:28px 0;border-top:1px solid var(--border);margin-top:8px}}
</style>
</head>
<body>
<div class="hero">
  <h1>Multi-Repo <span>Diff</span> Summary</h1>
  <div class="meta">
    <div><strong>Repos processed</strong> {len(records)}</div>
    <div><strong>Success / Failed</strong> {ok_count} / {fail_count}</div>
    <div><strong>Elapsed</strong> {elapsed}s</div>
    <div><strong>Generated</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
  </div>
</div>

<div class="wrap">
  <div class="kpis">
    <div class="kpi repos"><div class="kpi-label">Repos</div><div class="kpi-value">{len(records)}</div></div>
    <div class="kpi add"><div class="kpi-label">Lines Added</div><div class="kpi-value">{fmt(grand_added)}</div></div>
    <div class="kpi rem"><div class="kpi-label">Lines Removed</div><div class="kpi-value">{fmt(grand_removed)}</div></div>
    <div class="kpi neu"><div class="kpi-label">Net Delta</div><div class="kpi-value">{grand_delta:+,}</div></div>
    <div class="kpi tot"><div class="kpi-label">Total Changed</div><div class="kpi-value">{fmt(grand_changed)}</div></div>
    <div class="kpi files"><div class="kpi-label">Files Changed</div><div class="kpi-value">{fmt(grand_files)}</div></div>
  </div>

  <h2>Per-Repo Breakdown</h2>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Repo</th><th style="text-align:right">+Added</th><th style="text-align:right">−Removed</th>
        <th style="text-align:right">Δ Delta</th><th style="text-align:right">Files</th>
        <th>Change</th><th>Status</th><th>Report</th>
      </tr></thead>
      <tbody>{repo_rows}</tbody>
      <tfoot><tr>
        <td><strong>TOTAL ({len(records)} repos)</strong></td>
        <td class="added">+{fmt(grand_added)}</td>
        <td class="removed">-{fmt(grand_removed)}</td>
        <td class="delta {'pos' if grand_delta>=0 else 'neg'}">{grand_delta:+,}</td>
        <td class="files">{fmt(grand_files)}</td>
        <td></td><td></td><td></td>
      </tr></tfoot>
    </table>
  </div>
</div>
<div class="footer">Generated by run_all_diffs.sh &nbsp;·&nbsp; {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
</body></html>"""

with open(out_html, "w", encoding="utf-8") as f:
    f.write(html)
print(f"  Written: {out_html}")
PYEOF

sep
echo ""
echo -e "${BOLD}  ✅ All done in ${ELAPSED}s${RESET}"
echo ""
ok_count=$(grep -rl '"status": "ok"' "$JSON_DIR" 2>/dev/null | wc -l | tr -d " ")
fail_count=$(( TOTAL - ok_count ))
echo -e "  ${BOLD}Repos OK     :${RESET} ${GREEN}${ok_count}${RESET} / ${TOTAL}"
[[ $fail_count -gt 0 ]] && echo -e "  ${BOLD}Repos FAILED :${RESET} ${RED}${fail_count}${RESET} / ${TOTAL}"
echo ""
echo -e "  ${BOLD}Summary dash :${RESET} ${CYAN}${SUMMARY_HTML}${RESET}"
echo -e "  ${BOLD}Full log     :${RESET} ${LOG_FILE}"
echo ""
sep
