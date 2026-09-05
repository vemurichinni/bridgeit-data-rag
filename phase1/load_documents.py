#!/usr/bin/env python3
"""
load_documents.py — bulk-load the archive into RAGFlow, driven by the Phase 0 census.

Reads census_out/files.csv (from census_files.py), decides a knowledge base per
file from config.yaml, creates the KBs, uploads each file once (sha256 dedupe),
stamps metadata (project, year, ext, path, hash) for query-time filtering, and
kicks off parsing. Resumable: progress is written to a manifest (JSONL) and
already-loaded files are skipped on rerun.

Usage:
  python load_documents.py --config config.local.yaml --census ../phase0/census_out/files.csv
  python load_documents.py --config config.local.yaml --census files.csv --only "D:\\Projects\\ProjB_2016"
  python load_documents.py --config config.local.yaml --census files.csv --dry-run     # plan only
  python load_documents.py --config config.local.yaml --status                         # parse progress per KB
  python load_documents.py --config config.local.yaml --census files.csv --samples-only ../phase0/census_out/samples.csv

Load the samples first (--samples-only), check parsing quality in the RAGFlow UI, adjust
config.yaml chunking, then run the full load.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from ragflow_client import RagflowClient, RagflowError

TABLE_EXT = {"xlsx", "xls", "csv", "xlsm"}
EMAIL_EXT = {"eml", "msg"}


def slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-").lower()
    return s[:60] or "misc"


def kb_name_for(row: dict, cfg: dict) -> str:
    kb = cfg["knowledge_bases"]
    strat = kb.get("strategy", "top_folder")
    if strat == "single":
        base = kb.get("single_name", "archive-all")
    elif strat == "root":
        base = Path(row["root"]).name
    else:
        base = row.get("top_folder") or Path(row["root"]).name
    name = kb.get("name_prefix", "") + slug(base)
    ext = row["ext"].lower()
    if kb.get("split_tables", True) and ext in TABLE_EXT:
        name += "-tables"
    elif kb.get("split_email", True) and ext in EMAIL_EXT:
        name += "-mail"
    return name


def chunking_for(name: str, cfg: dict) -> tuple[str, dict]:
    ch = cfg["chunking"]
    if name.endswith("-tables"):
        return ch["tables"]["chunk_method"], ch["tables"].get("parser_config") or {}
    if name.endswith("-mail"):
        return ch["email"]["chunk_method"], ch["email"].get("parser_config") or {}
    return ch["documents"]["chunk_method"], ch["documents"].get("parser_config") or {}


def load_manifest(p: Path) -> dict[str, dict]:
    done = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line); done[rec["path"]] = rec
    return done


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--census", help="files.csv from census_files.py")
    ap.add_argument("--samples-only", help="samples.csv from census_files.py — load just those paths")
    ap.add_argument("--only", action="append", default=[], help="path prefix filter (repeatable)")
    ap.add_argument("--manifest", default="load_manifest.jsonl")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true", help="print parse status per KB and exit")
    ap.add_argument("--no-parse", action="store_true", help="upload only; trigger parsing later from the UI")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    rf = cfg["ragflow"]
    client = RagflowClient(rf["base_url"], rf["api_key"])

    if args.status:
        for d in client.list_datasets():
            if d["name"].startswith(cfg["knowledge_bases"].get("name_prefix", "")):
                print(f"{d['name']:40s} docs={d.get('document_count', '?'):>6} chunks={d.get('chunk_count', '?'):>8}  {client.parse_status(d['id'])}")
        return

    if not args.census:
        ap.error("--census is required unless --status")
    rows = list(csv.DictReader(Path(args.census).open(encoding="utf-8")))
    fcfg = cfg["files"]
    include = set(e.lower() for e in fcfg["include_ext"])
    max_bytes = fcfg.get("max_size_mb", 200) * 1024 * 1024
    prefixes = [p.rstrip("\\/") for p in (args.only or fcfg.get("only_prefixes") or [])]
    sample_paths = None
    if args.samples_only:
        sample_paths = {r["path"] for r in csv.DictReader(Path(args.samples_only).open(encoding="utf-8"))}

    manifest_p = Path(args.manifest)
    done = load_manifest(manifest_p)
    seen_hash: dict[str, str] = {r["sha256"]: r["path"] for r in done.values()
                                 if r.get("sha256") and r.get("status") == "uploaded"}
    census_dir = Path(args.census).resolve().parent

    plan: list[tuple[str, dict]] = []
    skipped = Counter()
    for r in rows:
        ext = r["ext"].lower()
        if ext not in include: skipped["ext"] += 1; continue
        if int(r["size_bytes"] or 0) == 0: skipped["empty"] += 1; continue
        if int(r["size_bytes"] or 0) > max_bytes: skipped["too-big"] += 1; continue
        if prefixes and not any(r["path"].startswith(p) for p in prefixes): skipped["prefix"] += 1; continue
        if sample_paths is not None and r["path"] not in sample_paths: skipped["not-sample"] += 1; continue
        if r["path"] in done: skipped["already-loaded"] += 1; continue
        if fcfg.get("skip_duplicates", True) and r.get("sha256"):
            if r["sha256"] in seen_hash:
                skipped["duplicate"] += 1
                if not args.dry_run:
                    with manifest_p.open("a", encoding="utf-8") as m:
                        m.write(json.dumps({"path": r["path"], "sha256": r["sha256"], "status": "duplicate-of",
                                            "duplicate_of": seen_hash[r["sha256"]]}) + "\n")
                continue
            seen_hash[r["sha256"]] = r["path"]
        plan.append((kb_name_for(r, cfg), r))

    by_kb = defaultdict(list)
    for name, r in plan:
        by_kb[name].append(r)
    print(f"{len(plan):,} files to load into {len(by_kb)} knowledge bases; skipped {dict(skipped)}")
    for name, items in sorted(by_kb.items()):
        cm, _ = chunking_for(name, cfg)
        print(f"  {name:40s} {len(items):>6} files  {sum(int(i['size_bytes']) for i in items)/1e9:6.2f} GB  chunk_method={cm}")
    if args.dry_run or not plan:
        return

    # create KBs
    ds_ids: dict[str, str] = {}
    for name in by_kb:
        cm, pc = chunking_for(name, cfg)
        try:
            ds = client.get_or_create_dataset(name, cm, rf["embedding_model"], pc)
        except RagflowError as e:
            print(f"!! cannot create KB {name}: {e}", file=sys.stderr); sys.exit(2)
        ds_ids[name] = ds["id"]

    meta_fields = cfg["metadata"]["fields"]
    t0 = time.time(); n_ok = n_fail = 0
    to_parse: dict[str, list[str]] = defaultdict(list)
    with manifest_p.open("a", encoding="utf-8") as m:
        for i, (name, r) in enumerate(plan, 1):
            p = Path(r["path"])
            if not p.is_absolute() and not p.exists():
                p = census_dir / p  # census was run with a relative root; resolve against its location
            rec = {"path": r["path"], "sha256": r.get("sha256", ""), "kb": name, "dataset_id": ds_ids[name]}
            try:
                # keep the relative path in the display name so duplicates of a file name stay distinguishable
                rel = p.relative_to(r["root"]) if r["path"].startswith(r["root"]) else p
                display = str(rel).replace("\\", "__").replace("/", "__")[-200:]
                doc = client.upload(ds_ids[name], p, display)
                doc_id = doc["id"]
                meta = {"project": r.get("top_folder") or Path(r["root"]).name, "year": r.get("year", ""),
                        "ext": r["ext"], "family": r.get("family", ""), "source_path": r["path"],
                        "sha256": r.get("sha256", ""), "root": r["root"]}
                client.set_metadata(ds_ids[name], doc_id, {k: str(v) for k, v in meta.items() if k in meta_fields})
                rec.update(status="uploaded", doc_id=doc_id)
                to_parse[ds_ids[name]].append(doc_id); n_ok += 1
            except (RagflowError, OSError) as e:
                rec.update(status="failed", error=str(e)[:300]); n_fail += 1
                print(f"!! {p}: {e}", file=sys.stderr)
            m.write(json.dumps(rec) + "\n"); m.flush()
            if i % 50 == 0 or i == len(plan):
                print(f"  {i}/{len(plan)} uploaded ({n_fail} failed) {time.time()-t0:.0f}s")
            # trigger parsing in batches so the parser works while uploads continue
            if not args.no_parse:
                for dsid, ids in list(to_parse.items()):
                    if len(ids) >= 50:
                        client.parse(dsid, ids); to_parse[dsid] = []
    if not args.no_parse:
        for dsid, ids in to_parse.items():
            if ids:
                client.parse(dsid, ids)
    print(f"\ndone: {n_ok} uploaded, {n_fail} failed, manifest {manifest_p}. Check progress with --status or in the UI.")


if __name__ == "__main__":
    main()
