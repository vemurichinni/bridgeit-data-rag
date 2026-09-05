#!/usr/bin/env python3
"""
census_report.py — merge the three census outputs into one Markdown report
with sizing estimates (chunks, embedding time, index size) for the RAG build.

Usage:
  python census_report.py --out census_out            # reads *_summary.json in that folder
  python census_report.py --out census_out --gpu      # estimate embedding time on a GPU

Assumptions used for estimates (edit ESTIMATE below if you know better):
  ~1 chunk per 1,500 characters of text; PDFs ~3,000 chars/page; Excel rows ~1 chunk each;
  code ~1 chunk per 60 lines; email ~1 chunk per message after quote-stripping;
  BGE-M3 (1024-dim float32) ~4 KB vector + ~2 KB text/metadata per chunk in Elasticsearch;
  embedding throughput ~40 chunks/s on CPU, ~800 chunks/s on a single consumer GPU.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

ESTIMATE = {
    "chars_per_chunk": 1500,
    "chars_per_pdf_page": 3000,
    "pdf_pages_per_mb_if_unknown": 30,
    "chars_per_office_mb": 400_000,
    "lines_per_code_chunk": 60,
    "bytes_per_chunk_in_index": 6 * 1024,
    "chunks_per_sec_cpu": 40,
    "chunks_per_sec_gpu": 800,
}


def load(out: Path, name: str) -> dict | None:
    p = out / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def fmt_gb(b: float) -> str:
    return f"{b/1e9:.2f} GB"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="census_out")
    ap.add_argument("--gpu", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    files, mbox, git = load(out, "files_summary.json"), load(out, "mbox_summary.json"), load(out, "git_summary.json")

    L = [f"# BridgeIT-Data corpus census — {datetime.now():%Y-%m-%d}", ""]
    chunks = {}

    if files:
        fam = files["by_family"]
        pdf_pages_known = 0
        L += ["## Documents", "",
              f"{files['total_files']:,} files, {fmt_gb(files['total_bytes'])}, modified {files['year_range'][0]}–{files['year_range'][1]}. "
              f"Exact duplicates: {files['duplicates']['duplicate_files']:,} files ({fmt_gb(files['duplicates']['duplicate_bytes'])}) — these index once.", "",
              "| family | files | size |", "|---|---:|---:|"]
        for k, v in fam.items():
            L.append(f"| {k} | {v['files']:,} | {fmt_gb(v['bytes'])} |")
        hz = files["hazards"]
        L += ["", "Parsing hazards found: " + ", ".join(f"{k.replace('_', ' ')} = {v:,}" for k, v in hz.items() if v), ""]
        L += ["Files per year (modified time — treat as a proxy, older projects may have been copied later):", "",
              "| year | files |", "|---|---:|"] + [f"| {y} | {v['files']:,} |" for y, v in files["by_year"].items()] + [""]
        # chunk estimate
        pdf_b = fam.get("pdf", {}).get("bytes", 0)
        office_b = sum(fam.get(k, {}).get("bytes", 0) for k in ("word", "word-legacy", "ppt", "ppt-legacy", "text"))
        excel_b = sum(fam.get(k, {}).get("bytes", 0) for k in ("excel", "excel-legacy"))
        E = ESTIMATE
        chunks["pdf"] = int(pdf_b / 1e6 * E["pdf_pages_per_mb_if_unknown"] * E["chars_per_pdf_page"] / E["chars_per_chunk"])
        chunks["office/text"] = int(office_b / 1e6 * E["chars_per_office_mb"] / E["chars_per_chunk"])
        chunks["excel (row-level)"] = int(excel_b / 1e6 * 4000)  # ~4k rows per MB of xlsx
        chunks["code-in-doc-folders"] = int(fam.get("code", {}).get("bytes", 0) / 1e6 * 25_000 / E["lines_per_code_chunk"])

    if mbox:
        L += ["## Email (Gmail mbox)", "",
              f"{mbox['messages']:,} messages in {mbox['threads']:,} threads "
              f"({mbox['avg_messages_per_thread']} per thread; largest {mbox['largest_threads'][:3]}), years {mbox['year_range']}.",
              f"Quoted-reply text is {mbox['quoted_ratio']:.0%} of all body text — strip it or the index roughly "
              f"{'doubles' if mbox['quoted_ratio'] > 0.4 else 'inflates'}. HTML-only messages: {mbox['html_only_messages']:,}. "
              f"Undated: {mbox['messages_with_bad_dates']:,}.",
              f"Attachments: {mbox['attachments_total']:,} ({fmt_gb(mbox['attachment_bytes_total'])}), "
              f"{mbox['unique_attachments']:,} unique after hashing ({mbox['duplicate_attachments']:,} duplicates).", "",
              "| attachment type | count | size |", "|---|---:|---:|"]
        for k, v in list(mbox["attachments_by_ext"].items())[:15]:
            L.append(f"| {k} | {v['count']:,} | {fmt_gb(v['bytes'])} |")
        L += ["", "Top sender domains: " + ", ".join(f"{d} ({n:,})" for d, n in mbox["top_sender_domains"][:10]), ""]
        L += ["Messages per year:", "", "| year | messages |", "|---|---:|"] + \
             [f"| {y} | {n:,} |" for y, n in mbox["by_year"].items() if y != "0"] + [""]
        net_chars = mbox["body_chars_total"] - mbox["quoted_chars_total"]
        chunks["email bodies"] = max(mbox["messages"], int(net_chars / ESTIMATE["chars_per_chunk"]))
        chunks["email attachments"] = int(mbox["attachment_bytes_total"] / 1e6 * 30 * 3000 / ESTIMATE["chars_per_chunk"] *
                                          (mbox["unique_attachments"] / max(1, mbox["attachments_total"])))

    if git:
        L += ["## Code", "",
              f"{git['repositories']} repositories ({git['git_repositories']} with Git history), {git['source_files']:,} source files, "
              f"{git['source_lines']:,} lines, {git['total_commits']:,} commits, {git['commit_year_range']}. "
              f"Vendored/build output skipped: {fmt_gb(git['vendored_bytes_skipped'])}.", "",
              "| language | lines | files |", "|---|---:|---:|"]
        for k, v in list(git["lines_by_language"].items())[:12]:
            L.append(f"| {k} | {v:,} | {git['files_by_language'].get(k, 0):,} |")
        special = {k: v for k, v in git["files_by_kind"].items() if k in
                   ("mybatis-mapper", "stored-procedure", "ddl", "angular-component", "java-controller",
                    "java-service", "java-repository", "java-entity", "java-test", "docs", "spring-xml")}
        L += ["", "Artefacts needing dedicated chunkers: " + ", ".join(f"{k} = {v:,}" for k, v in special.items()),
              f"Files over 2,000 lines (chunk at method level, not file level): {git['files_over_2000_lines']:,}.",
              f"Repos without a README (nothing to give 'contextual retrieval' a summary from): {len(git['repos_without_readme'])}.", ""]
        chunks["code (AST)"] = int(git["source_lines"] / ESTIMATE["lines_per_code_chunk"])
        chunks["commit messages"] = git["total_commits"]

    if chunks:
        total = sum(chunks.values())
        rate = ESTIMATE["chunks_per_sec_gpu" if args.gpu else "chunks_per_sec_cpu"]
        L += ["## Sizing estimate", "",
              "Rough, order-of-magnitude — the point is to choose hardware and a batch plan, not to be exact.", "",
              "| source | est. chunks |", "|---|---:|"] + [f"| {k} | {v:,} |" for k, v in chunks.items()] + \
             [f"| **total** | **{total:,}** |", "",
              f"Index size at ~6 KB/chunk (BGE-M3 1024-d + text + metadata): **{fmt_gb(total * ESTIMATE['bytes_per_chunk_in_index'])}** "
              f"(Elasticsearch; add ~1.5× for replicas/merges).",
              f"Embedding time at ~{rate} chunks/s ({'GPU' if args.gpu else 'CPU'}): **{total / rate / 3600:.1f} hours**"
              f"{'' if args.gpu else ' — rerun with --gpu to see the GPU figure'}.",
              "RAGFlow host: 4 cores / 16 GB is the floor; for this corpus plan 32–64 GB RAM and SSD ≥ 2× index size plus originals.", ""]

    L += ["## Next steps (Phase 0 → Phase 1)", "",
          "1. Open `samples.csv`, `mbox_samples.csv`, `code_samples.csv`; run Docling and RAGFlow's parser on each; record pass/fail in `parser_result`.",
          "2. Fill `evaluation_set.xlsx` with 50 real questions and their known-answer locations.",
          "3. Decide knowledge-base boundaries (per project / per domain / per year) from the `by_top_folder` breakdown in `files_summary.json`.",
          "4. Stand up RAGFlow and load the sample set first; measure against the evaluation set before bulk load.", ""]

    report = out / "CENSUS_REPORT.md"
    report.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))
    print(f"\nwrote {report}")


if __name__ == "__main__":
    main()
