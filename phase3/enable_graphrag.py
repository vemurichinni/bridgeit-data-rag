#!/usr/bin/env python3
"""
enable_graphrag.py — turn on RAPTOR and/or GraphRAG for selected knowledge bases.

config.yaml already ships both switched off under chunking.documents.parser_config,
marked "phase 3 option" (docs/RAG-System-Recommendation.md section 5: "RAPTOR / GraphRAG
/ LightRAG — Phase 3, not now"). This script flips them on for existing datasets without
recreating them, for the specific KBs where cross-project "how did we handle X across
projects" questions actually come up — do this once hybrid exact retrieval is solid,
since both cost an LLM pass over the whole KB (re-indexing) and trade some exact-match
precision for cross-document synthesis.

Usage:
  python enable_graphrag.py --config ../phase1/config.local.yaml --list                       # inspect current state
  python enable_graphrag.py --config ../phase1/config.local.yaml --kb bt-projb_2016 --raptor
  python enable_graphrag.py --config ../phase1/config.local.yaml --pattern "bt-archive-*" \
         --graphrag --graphrag-method light --dry-run
  python enable_graphrag.py --config ../phase1/config.local.yaml --kb bt-archive-all --graphrag

After enabling, re-parse the KB's documents (phase1/load_documents.py, or the UI) —
RAPTOR/GraphRAG only run on documents parsed after the flag is on.
"""
from __future__ import annotations

import argparse
import fnmatch
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "phase1"))
from ragflow_client import RagflowClient, RagflowError  # noqa: E402


def matches(name: str, kb: list[str], pattern: str | None) -> bool:
    if kb:
        return name in kb
    if pattern:
        return fnmatch.fnmatch(name, pattern)
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="../phase1/config.local.yaml")
    ap.add_argument("--kb", action="append", default=[], help="exact KB name (repeatable)")
    ap.add_argument("--pattern", help="fnmatch glob against KB name, alternative to --kb")
    ap.add_argument("--list", action="store_true", help="print raptor/graphrag state for matching KBs and exit")
    ap.add_argument("--raptor", dest="raptor", action="store_true", default=None)
    ap.add_argument("--no-raptor", dest="raptor", action="store_false")
    ap.add_argument("--graphrag", dest="graphrag", action="store_true", default=None)
    ap.add_argument("--no-graphrag", dest="graphrag", action="store_false")
    ap.add_argument("--graphrag-method", default="light", choices=["light", "general"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.kb and not args.pattern:
        ap.error("--kb (repeatable) or --pattern is required")

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    rf = cfg["ragflow"]
    client = RagflowClient(rf["base_url"], rf["api_key"])
    prefix = cfg.get("knowledge_bases", {}).get("name_prefix", "")

    datasets = [d for d in client.list_datasets()
               if d["name"].startswith(prefix) and matches(d["name"], args.kb, args.pattern)]
    if not datasets:
        print("no matching knowledge bases", file=sys.stderr); sys.exit(1)

    for d in sorted(datasets, key=lambda d: d["name"]):
        pc = d.get("parser_config") or {}
        raptor_on = bool((pc.get("raptor") or {}).get("use_raptor"))
        graphrag_on = bool((pc.get("graphrag") or {}).get("use_graphrag"))
        print(f"{d['name']:40s} raptor={raptor_on!s:5s} graphrag={graphrag_on!s:5s}")

    if args.list:
        return
    if args.raptor is None and args.graphrag is None:
        ap.error("nothing to change: pass --raptor/--no-raptor and/or --graphrag/--no-graphrag, or --list")

    n_ok = n_fail = 0
    for d in datasets:
        pc = dict(d.get("parser_config") or {})
        if args.raptor is not None:
            pc["raptor"] = {**(pc.get("raptor") or {}), "use_raptor": args.raptor}
        if args.graphrag is not None:
            pc["graphrag"] = {**(pc.get("graphrag") or {}), "use_graphrag": args.graphrag,
                              "method": args.graphrag_method}
        if args.dry_run:
            print(f"  would update {d['name']}: parser_config={pc}")
            continue
        try:
            client.update_dataset(d["id"], parser_config=pc)
            n_ok += 1
        except RagflowError as e:
            print(f"!! {d['name']}: {e}", file=sys.stderr); n_fail += 1

    if not args.dry_run:
        print(f"\nupdated {n_ok} KB(s), {n_fail} failed. Re-parse each KB's documents to apply.")


if __name__ == "__main__":
    main()
