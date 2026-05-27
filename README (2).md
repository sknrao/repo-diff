# Repo Diff Report

A small toolset to compare your forked/modified repositories against their upstream originals and generate detailed **Lines-of-Change (LOC) HTML reports** — one per repo, plus a combined summary dashboard across all repos.

Built for tracking changes like an ODL IETF NETCONF version upgrade across multiple ONAP CCSDK repositories.

---

## Files

| File | Purpose |
|------|---------|
| `repo_diff_report.py` | Core engine — clones two repos and produces a single HTML diff report |
| `run_all_diffs.sh` | Shell orchestrator — runs the engine for every pair in a config file, then generates a combined summary |
| `repos.conf` | Your list of repo pairs to compare |

---

## Requirements

- Python 3.10+
- `git` in your `PATH`
- No third-party Python packages needed (stdlib only)
- GNU `parallel` *(optional, only needed for `--jobs > 1`)*

---

## Quick Start

### 1. Edit `repos.conf`

Add one repo pair per line. The ref column accepts a **branch name, tag, or commit SHA**:

```
# CURRENT_URL   UPSTREAM_URL   CURRENT_REF       UPSTREAM_REF
https://github.com/your-org/ccsdk-oran      https://github.com/onap/ccsdk-oran      my-netconf-fix  2.3.0
https://github.com/your-org/ccsdk-features  https://github.com/onap/ccsdk-features  main            jakarta-release
```

### 2. Run

```bash
chmod +x run_all_diffs.sh

./run_all_diffs.sh --pat ghp_xxxxxxxxxxxxxxxxxxxx
```

### 3. Open the reports

```
diff_reports/
├── summary.html            ← combined dashboard (start here)
├── ccsdk-oran_diff.html
├── ccsdk-features_diff.html
└── ...
```

Open `summary.html` in your browser. Each repo row links to its detailed report.

---

## Choosing a Git Ref — Branch, Tag, or SHA?

The toolset accepts any git ref. Here's when to use each:

| Ref type | Example | Use when |
|----------|---------|----------|
| **Tag** ✅ recommended | `2.3.0`, `jakarta-release` | Pinning upstream to a known release. Tags are immutable — the diff is reproducible forever. |
| **Branch** | `master`, `main` | Quick comparisons where reproducibility doesn't matter. Note: upstream `master` moves, so two runs may give different results even if *you* changed nothing. |
| **Commit SHA** | `a3f9c21` | Maximum precision when no tag exists for the exact commit you forked from. Short (7-char) or full (40-char) SHAs both work. |

> **Recommendation:** use a **tag** for the upstream ref whenever the upstream project cuts releases. This gives you a human-readable, stable anchor (`2.3.0` tells a reader far more than `a3f9c21`). Use a **branch** for your own fork's ref since your branch name is meaningful to you and tracks your work.

---

## `repos.conf` Format

Columns separated by whitespace. Lines starting with `#` are comments.

| Columns | Meaning |
|---------|---------|
| 2 | Both sides use the default ref (`--ref` flag, default: `master`) |
| 3 | Both sides use the same ref |
| 4 | Each side uses its own ref — the most flexible form |

```bash
# Same ref both sides (branch)
https://github.com/your-org/ccsdk-oran     https://github.com/onap/ccsdk-oran     master

# Your fork on a feature branch, upstream pinned to a release tag
https://github.com/your-org/ccsdk-features https://github.com/onap/ccsdk-features my-netconf-fix  2.3.0

# Upstream pinned to a specific commit SHA
https://github.com/your-org/ccsdk-sli-core https://github.com/onap/ccsdk-sli-core main  a3f9c21d
```

---

## Private Repositories

Yes — private repos work, as long as your PAT has the `repo` scope.

The PAT is embedded into the HTTPS clone URL at runtime (`https://<PAT>@github.com/...`) and is never written to disk.

### Providing the PAT

**Option A — flag:**
```bash
./run_all_diffs.sh --pat ghp_xxxxxxxxxxxxxxxxxxxx
```

**Option B — environment variable (recommended):**
```bash
export GITHUB_PAT=ghp_xxxxxxxxxxxxxxxxxxxx
./run_all_diffs.sh
```

**Option C — `.env` file:**
```bash
echo "GITHUB_PAT=ghp_xxxxxxxxxxxxxxxxxxxx" > .env
source .env && ./run_all_diffs.sh
```

> ⚠️ Never commit your PAT to version control. Add `.env` to your `.gitignore`.

---

## `run_all_diffs.sh` Options

| Flag | Default | Description |
|------|---------|-------------|
| `-c`, `--config FILE` | `repos.conf` | Repo-pair config file |
| `-p`, `--pat TOKEN` | `$GITHUB_PAT` | GitHub Personal Access Token |
| `-b`, `--ref REF` | `master` | Default git ref when not specified in config |
| `-o`, `--outdir DIR` | `./diff_reports` | Directory for all output files |
| `-s`, `--script FILE` | auto-detect | Path to `repo_diff_report.py` |
| `-j`, `--jobs N` | `1` | Parallel jobs (requires GNU `parallel`) |
| `-h`, `--help` | | Show help |

---

## `repo_diff_report.py` Options

Use this directly when you only need to diff a single pair of repos.

| Flag | Default | Description |
|------|---------|-------------|
| `--current URL` | *(required)* | URL of your modified/forked repo |
| `--upstream URL` | *(required)* | URL of the original upstream repo |
| `--pat TOKEN` | none | GitHub PAT for private repos |
| `--ref NAME` | `master` | Shared git ref for both repos |
| `--current-ref REF` | `--ref` | Ref for your repo — branch, tag, or SHA (overrides `--ref`) |
| `--upstream-ref REF` | `--ref` | Ref for upstream repo — branch, tag, or SHA (overrides `--ref`) |
| `--output FILE` | `diff_report.html` | Output HTML report path |
| `--json-summary FILE` | none | Also write a JSON summary (used by the shell script for aggregation) |

**Example — fork on a branch, upstream pinned to a release tag:**
```bash
python3 repo_diff_report.py \
  --current      https://github.com/your-org/ccsdk-oran \
  --upstream     https://github.com/onap/ccsdk-oran \
  --pat          ghp_xxxxxxxxxxxxxxxxxxxx \
  --current-ref  my-netconf-fix \
  --upstream-ref 2.3.0 \
  --output       ccsdk-oran-diff.html
```

**Example — upstream pinned to a commit SHA:**
```bash
python3 repo_diff_report.py \
  --current      https://github.com/your-org/ccsdk-oran \
  --upstream     https://github.com/onap/ccsdk-oran \
  --current-ref  main \
  --upstream-ref a3f9c21d \
  --output       ccsdk-oran-diff.html
```

---

## How Refs Are Cloned

| Ref type | Clone strategy |
|----------|---------------|
| Branch / tag | `git clone --depth=1 --branch <ref>` — fast shallow clone |
| Commit SHA | `git clone` (full) then `git checkout <sha>` — necessary because `--branch` does not accept bare SHAs |

If cloning with `--branch` fails (e.g. branch name typo), the script retries against the repo's default branch and warns you in the log.

---

## What the Reports Show

### Per-repo report (`*_diff.html`)
- **KPI cards** — lines added, removed, net delta, total changed, files changed
- **By file extension** — bar chart of change volume per extension (`.yang`, `.java`, `.xml`, …)
- **By top-level directory** — bar chart grouped by the first path component
- **Per-file table** — every changed file with added/removed/delta counts, inline visual bar, and a live filter box

### Summary dashboard (`summary.html`)
- Grand totals across all repos
- Per-repo table sorted by change volume, with status badges and direct links to each report
- Failed repos shown greyed-out so nothing silently disappears

### What is counted / excluded
- **Counted:** all text-based source files
- **Excluded:** binary formats — `.png`, `.jpg`, `.svg`, `.jar`, `.class`, `.so`, `.dll`, `.zip`, `.tar`, `.gz`, `.pdf`, `.lock`

---

## Parallel Execution

```bash
# macOS
brew install parallel

# Ubuntu/Debian
sudo apt install parallel

# Run 4 repos at a time
./run_all_diffs.sh --jobs 4 --pat ghp_xxxxxxxxxxxxxxxxxxxx
```

---

## Output Structure

```
diff_reports/
├── summary.html               ← combined dashboard across all repos
├── ccsdk-oran_diff.html
├── ccsdk-features_diff.html
├── ccsdk-sli-core_diff.html
├── ccsdk-apps_diff.html
├── run_all.log                ← full stdout/stderr from every run
└── .summaries/                ← internal JSON files used to build summary.html
    ├── ccsdk-oran.json
    ├── ccsdk-features.json
    └── ...
```

---

## Tips

- **Re-running** is safe — output files are overwritten on each run.
- **Pin your upstream ref to a tag** for reproducible, meaningful diffs.
- **Checking failures** — `run_all.log` has the full error output for any failed repo.
- **SHA clones are slower** — they require a full clone rather than a shallow one, so expect them to take longer on large repos.
- **Finding the right upstream tag** — run `git tag -l` in the cloned repo, or browse the GitHub Releases page.
