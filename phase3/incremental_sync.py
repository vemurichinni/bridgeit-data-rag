#!/usr/bin/env python3
"""
incremental_sync.py — one nightly entry point for keeping code and mail current.

Phase 2 already made both ingesters cheap to re-run: ingest_code.py keys files by
content hash so a rerun after `git pull` only re-embeds what changed, and (as of
Phase 3) ingest_mbox.py re-embeds a thread when its message count grows instead of
skipping it forever. This script is just the orchestration Step 4 of
docs/Phase2-Runbook.md described running by hand:

  1. `git pull` every repo in --repos-csv, then run ingest_code.py once over all of them.
  2. For each --mail-account with Gmail API credentials configured, run gmail_sync.py to
     pull only new messages into a fresh .mbox, then run ingest_mbox.py on it.
  3. For each --mail-glob (e.g. a fresh Takeout export dropped on a schedule), run
     ingest_mbox.py directly — safe to repoint at the same account repeatedly.

Every step's exit code is recorded; the script exits non-zero if anything failed, so a
cron wrapper can alert on it. Nothing here duplicates ingestion logic — it only calls
the existing scripts as subprocesses with the flags docs/Phase2-Runbook.md already
recommends for incremental runs.

Usage
  python incremental_sync.py --config ../phase1/config.local.yaml \
         --repos-csv ../phase0/census_out/repos.csv \
         --gmail-credentials gmail_credentials.json \
         --mail-account projects@bridgeit.com --mail-account ops@bridgeit.com \
         --attachments-dir /data/mail-attachments \
         --log sync.log

  # code only, e.g. from a per-repo post-merge hook (see hooks/post-merge):
  python incremental_sync.py --config ../phase1/config.local.yaml --repo /src/repos/orders-service

  # mail only, from a fresh Takeout export dropped by a scheduled task:
  python incremental_sync.py --config ../phase1/config.local.yaml \
         --mail-glob "/data/takeout-latest/Mail/*.mbox" --mail-account projects@bridgeit.com

Cron example (runs nightly at 02:00):
  0 2 * * * cd /opt/bridgeit-data-rag/phase3 && python3 incremental_sync.py \
      --config ../phase1/config.local.yaml --repos-csv ../phase0/census_out/repos.csv \
      --gmail-credentials gmail_credentials.json --mail-account projects@bridgeit.com \
      --log sync.log >> sync.cron.log 2>&1
"""
from __future__ import annotations

import argparse
import csv
import glob
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PHASE1 = HERE.parent / "phase1"
PHASE2 = HERE.parent / "phase2"


def run(cmd: list[str], label: str) -> bool:
    print(f"--- {label}: {' '.join(cmd)}", file=sys.stderr)
    t0 = time.time()
    proc = subprocess.run(cmd)
    ok = proc.returncode == 0
    print(f"--- {label}: {'ok' if ok else 'FAILED'} ({time.time()-t0:.0f}s)", file=sys.stderr)
    return ok


def sync_code(args, results: list[tuple[str, bool]]) -> None:
    repos = list(args.repo)
    if args.repos_csv:
        repos += [r["path"] for r in csv.DictReader(Path(args.repos_csv).open(encoding="utf-8"))]
    if not repos:
        return
    for r in repos:
        p = Path(r)
        if (p / ".git").exists():
            ok = run(["git", "-C", str(p), "pull", "--quiet"], f"git pull {p.name}")
            results.append((f"git pull {p.name}", ok))
    cmd = [sys.executable, str(PHASE2 / "ingest_code.py"), "--config", args.config,
          "--manifest", args.code_manifest]
    if args.repos_csv:
        cmd += ["--repos-csv", args.repos_csv]
    cmd += list(args.repo)
    if args.project:
        cmd += ["--project", args.project]
    results.append(("ingest_code.py", run(cmd, "ingest_code.py")))


def sync_mail_account(args, account: str, results: list[tuple[str, bool]]) -> None:
    mbox_files: list[str] = []
    if args.gmail_credentials:
        sync_dir = Path(args.gmail_sync_dir)
        cmd = [sys.executable, str(HERE / "gmail_sync.py"), "--credentials", args.gmail_credentials,
              "--token-file", f"gmail_token_{account}.json", "--account", account,
              "--out", str(sync_dir), "--state-file", args.gmail_state_file,
              "--since-days", str(args.since_days)]
        ok = run(cmd, f"gmail_sync {account}")
        results.append((f"gmail_sync {account}", ok))
        if ok:
            mbox_files += sorted(str(p) for p in sync_dir.glob(f"{account}_*.mbox"))
    for pattern in args.mail_glob:
        mbox_files += sorted(glob.glob(pattern))
    if not mbox_files:
        print(f"{account}: nothing new to ingest", file=sys.stderr)
        return
    cmd = [sys.executable, str(PHASE2 / "ingest_mbox.py"), "--config", args.config,
          *mbox_files, "--account", account, "--manifest", args.mail_manifest]
    if args.attachments_dir:
        cmd += ["--attachments-dir", args.attachments_dir]
    if args.skip_labels:
        cmd += ["--skip-labels", args.skip_labels]
    results.append((f"ingest_mbox {account}", run(cmd, f"ingest_mbox {account}")))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(PHASE1 / "config.local.yaml"))
    ap.add_argument("--repo", action="append", default=[], help="repo path to sync (repeatable)")
    ap.add_argument("--repos-csv", help="repos.csv from phase0/census_git.py")
    ap.add_argument("--project", help="passed through to ingest_code.py --project")
    ap.add_argument("--code-manifest", default=str(PHASE2 / "code_manifest.jsonl"))
    ap.add_argument("--mail-account", action="append", default=[], dest="mail_accounts")
    ap.add_argument("--gmail-credentials", help="OAuth client secret JSON; enables Gmail API incremental pull")
    ap.add_argument("--gmail-sync-dir", default=str(HERE / "gmail_incoming"))
    ap.add_argument("--gmail-state-file", default=str(HERE / "gmail_sync_state.json"))
    ap.add_argument("--since-days", type=int, default=30, help="gmail_sync.py backfill window with no prior state")
    ap.add_argument("--mail-glob", action="append", default=[], help="glob of a fresh .mbox drop (repeatable)")
    ap.add_argument("--mail-manifest", default=str(PHASE2 / "mbox_manifest.jsonl"))
    ap.add_argument("--attachments-dir")
    ap.add_argument("--skip-labels", default="Spam,Trash")
    ap.add_argument("--skip-code", action="store_true")
    ap.add_argument("--skip-mail", action="store_true")
    ap.add_argument("--log", help="append a one-line run summary to this file")
    args = ap.parse_args()

    results: list[tuple[str, bool]] = []
    if not args.skip_code:
        sync_code(args, results)
    if not args.skip_mail:
        for account in args.mail_accounts:
            sync_mail_account(args, account, results)

    n_ok = sum(1 for _, ok in results if ok)
    n_fail = len(results) - n_ok
    summary = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} steps={len(results)} ok={n_ok} failed={n_fail} " \
             + ",".join(f"{name}:{'ok' if ok else 'FAIL'}" for name, ok in results)
    print(summary)
    if args.log:
        with Path(args.log).open("a", encoding="utf-8") as f:
            f.write(summary + "\n")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
