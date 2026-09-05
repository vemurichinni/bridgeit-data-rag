#!/usr/bin/env python3
"""
load_jsonl.py — load Phase 2 JSONL chunk files into RAGFlow, on a different machine
than the one that produced them.

Why: ingest_mbox.py and ingest_code.py do all the real work locally — thread grouping,
quote-stripping, AST chunking — and, run with --dry-run --jsonl out.jsonl, need no
network access to RAGFlow at all. That is the "prepare everything on an offline
machine (attached hard disk, local mail export), export the result, handle it in the
cloud" workflow: run the ingesters fully offline, copy the small JSONL file(s) they
produce to wherever RAGFlow actually runs, then use this script there to push the
chunks in. Nothing here re-chunks anything; it replays exactly the documents the
ingesters already built through the same RagflowSink they would have used directly.

Usage
  # on the offline machine (attached disk / local Takeout export, no RAGFlow reachable):
  python ingest_code.py --config ../phase1/config.local.yaml /mnt/archive/orders-service \
         --dry-run --jsonl code.jsonl
  python ingest_mbox.py --config ../phase1/config.local.yaml ~/Takeout/Mail/*.mbox \
         --account ops@bridgeit.com --dry-run --jsonl mail.jsonl --attachments-dir ./attachments

  # copy code.jsonl, mail.jsonl (and the attachments folder, separately, if any) to the
  # cloud machine, then, pointed at the real RAGFlow instance:
  python load_jsonl.py --config ../phase1/config.local.yaml code.jsonl mail.jsonl

Resumable: uses the same Manifest as the live ingesters, keyed by source_id with a
version (chunk count) — so re-running after re-preparing a changed source only
re-uploads documents that actually changed, and an interrupted load resumes cleanly.

Note: attachments referenced by mail.jsonl are not embedded in the JSONL — they were
written to --attachments-dir as their own files with an attachments.csv. Copy that
folder across too and load it separately with phase1/load_documents.py --census
attachments.csv, same as a same-machine run would.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sink import Manifest, RagflowSink  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("jsonl", nargs="+", help="JSONL file(s) produced by ingest_mbox.py/ingest_code.py --jsonl")
    ap.add_argument("--config", required=True)
    ap.add_argument("--manifest", default="jsonl_load_manifest.jsonl")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    rf = cfg["ragflow"]
    sink = RagflowSink(rf["base_url"], rf["api_key"], rf["embedding_model"])
    manifest = Manifest(Path(args.manifest))

    n_docs = n_chunks = n_skipped = n_failed = 0
    for jp in args.jsonl:
        path = Path(jp)
        if not path.exists():
            print(f"!! not found: {path}", file=sys.stderr); n_failed += 1; continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            doc = json.loads(line)
            version = len(doc["chunks"])
            if manifest.has(doc["source_id"], version=version):
                n_skipped += 1
                continue
            try:
                res = sink.write(doc)
                manifest.record({"source_id": doc["source_id"], "kb": doc["kb"], "version": version,
                                 "chunks": len(doc["chunks"]), **res})
                n_docs += 1; n_chunks += len(doc["chunks"])
            except Exception as e:
                manifest.record({"source_id": doc["source_id"], "status": "failed", "error": str(e)[:300]})
                n_failed += 1
                print(f"!! {doc.get('title', doc['source_id'])}: {e}", file=sys.stderr)
            if (n_docs + n_failed) % 50 == 0:
                print(f"  {n_docs} loaded, {n_skipped} skipped, {n_failed} failed so far")

    print(f"\ndone: {n_docs} document(s) loaded ({n_chunks} chunks), {n_skipped} skipped (already loaded), "
         f"{n_failed} failed. manifest: {args.manifest}")


if __name__ == "__main__":
    main()
