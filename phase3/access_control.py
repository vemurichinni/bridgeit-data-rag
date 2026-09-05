#!/usr/bin/env python3
"""
access_control.py — keep RAGFlow dataset `permission` in line with a policy file.

RAGFlow's open-source HTTP API controls visibility at the dataset level only:
`permission` is "me" (only the creator/owning account can see or query the KB) or
"team" (everyone on the same RAGFlow team can). There is no per-user or per-group ACL
in the OSS API — real role-based access needs the Enterprise edition, or an
authorization layer in front of retrieval (phase3/mcp_server.py's ALLOWED_KB pattern
is a coarse version of that: a KB-name allow-list applied before /retrieval is called).

Given that ceiling, this script does the one thing the API supports well: make sure
every KB matching a sensitive-name pattern (HR, legal, a client's confidential project)
is "me" instead of silently staying "team"-visible to the whole org, while everything
else can default to open for broad retrieval once vetted.

Usage:
  python access_control.py --config ../phase1/config.local.yaml --policy access_policy.local.yaml --status
  python access_control.py --config ../phase1/config.local.yaml --policy access_policy.local.yaml --dry-run
  python access_control.py --config ../phase1/config.local.yaml --policy access_policy.local.yaml
"""
from __future__ import annotations

import argparse
import fnmatch
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "phase1"))
from ragflow_client import RagflowClient, RagflowError  # noqa: E402


def desired_permission(name: str, policy: dict) -> str:
    for rule in policy.get("rules", []):
        if fnmatch.fnmatch(name, rule["pattern"]):
            return rule["permission"]
    return policy.get("default", "team")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="../phase1/config.local.yaml")
    ap.add_argument("--policy", default="access_policy.local.yaml")
    ap.add_argument("--status", action="store_true", help="print current vs. desired permission and exit")
    ap.add_argument("--dry-run", action="store_true", help="print the changes that would be made, apply none")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    policy = yaml.safe_load(Path(args.policy).read_text(encoding="utf-8"))
    rf = cfg["ragflow"]
    client = RagflowClient(rf["base_url"], rf["api_key"])
    prefix = cfg.get("knowledge_bases", {}).get("name_prefix", "")

    datasets = sorted((d for d in client.list_datasets() if d["name"].startswith(prefix)), key=lambda d: d["name"])
    if not datasets:
        print("no knowledge bases found", file=sys.stderr); sys.exit(1)

    changes = []
    for d in datasets:
        current = d.get("permission", "?")
        want = desired_permission(d["name"], policy)
        flag = "  " if current == want else "->"
        print(f"{d['name']:40s} current={current:5s} {flag} desired={want}")
        if current != want:
            changes.append((d, want))

    if args.status:
        return
    if not changes:
        print("\nnothing to change"); return
    if args.dry_run:
        print(f"\n{len(changes)} dataset(s) would change (dry run, nothing applied)")
        return

    n_ok = n_fail = 0
    for d, want in changes:
        try:
            client.update_dataset(d["id"], permission=want)
            n_ok += 1
        except RagflowError as e:
            print(f"!! {d['name']}: {e}", file=sys.stderr); n_fail += 1
    print(f"\napplied {n_ok} change(s), {n_fail} failed")


if __name__ == "__main__":
    main()
