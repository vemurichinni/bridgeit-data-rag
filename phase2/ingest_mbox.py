#!/usr/bin/env python3
"""
ingest_mbox.py — Gmail Takeout (.mbox) → thread documents + per-message chunks → RAGFlow.

What it does
  1. Reads one or more .mbox files, groups messages into threads (Gmail's X-GM-THRID,
     falling back to References/In-Reply-To chains, then to normalised subject).
  2. Cleans every message body: quoted history, signatures and disclaimers removed,
     HTML converted to text — so each paragraph is indexed once, not once per reply.
  3. Extracts attachments to --attachments-dir, de-duplicated by SHA-256, and writes an
     attachments.csv in Phase 0 files.csv format so phase1/load_documents.py can load them
     (they become normal parsed documents linked back by message id in their metadata).
  4. Renders each thread as Markdown (uploaded as the citation target) and pushes one
     chunk per message — prefixed with thread/sender/date context ("contextual retrieval")
     — with sender, subject words, attachment names and identifiers as keywords.

Usage
  python ingest_mbox.py --config ../phase1/config.local.yaml ~/Takeout/Mail/*.mbox --account projects@bridgeit.com
  python ingest_mbox.py --config cfg.yaml mail.mbox --dry-run                 # writes phase2_chunks.jsonl, no RAGFlow
  python ingest_mbox.py --config cfg.yaml mail.mbox --since 2012 --skip-labels Spam,Trash,Promotions
  python ingest_mbox.py --config cfg.yaml mail.mbox --limit 500               # first 500 messages, for a trial

Resumable: threads recorded in --manifest are skipped on rerun; a thread that gained
new messages since its last run (same thread_id, higher message count) is re-embedded,
which is what makes a nightly incremental sync (see phase3/) safe to run on a growing mailbox.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import mailbox
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chunkers.email_clean import clean_body  # noqa: E402
from sink import Manifest, make_sink  # noqa: E402

IDENT_RE = re.compile(r"\b(?:[A-Z]{2,}[-_/]?\d{2,}[\w-]*|\d{3,}[-/]\d{2,}[-/\d]*|[A-Z][A-Z0-9_]{3,}\d[A-Z0-9_]*)\b")
STOP = set("re fw fwd the and for with from that this your our are was were you have has will not".split())
CHUNK_CHARS = 1800


def hdr(msg, name: str) -> str:
    v = msg.get(name)
    if not v:
        return ""
    try:
        return str(make_header(decode_header(v)))
    except Exception:
        return str(v)


def norm_subject(s: str) -> str:
    s = re.sub(r"^\s*((re|fw|fwd|aw|wg|tr)\s*:\s*)+", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip().lower()


def msg_date(msg):
    try:
        return parsedate_to_datetime(hdr(msg, "Date"))
    except Exception:
        return None


def body_and_attachments(msg, att_dir: Path | None, seen: dict) -> tuple[str, bool, list[dict]]:
    plain, html_body, atts = [], [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fn = part.get_filename()
        disp = part.get_content_disposition()
        ct = part.get_content_type()
        if fn or disp == "attachment":
            try:
                fn = str(make_header(decode_header(fn or "attachment.bin")))
            except Exception:
                fn = fn or "attachment.bin"
            try:
                payload = part.get_payload(decode=True) or b""
            except Exception:
                payload = b""
            if not payload:
                continue
            sha = hashlib.sha256(payload).hexdigest()
            rec = {"filename": fn, "size": len(payload), "sha256": sha, "path": ""}
            if att_dir is not None:
                safe = re.sub(r"[^\w.\-() ]+", "_", fn)[:120]
                if sha in seen:
                    rec["path"] = seen[sha]
                else:
                    sub = att_dir / sha[:2]; sub.mkdir(parents=True, exist_ok=True)
                    p = sub / f"{sha[:12]}_{safe}"
                    if not p.exists():
                        p.write_bytes(payload)
                    seen[sha] = str(p); rec["path"] = str(p)
            atts.append(rec)
            continue
        try:
            text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if ct == "text/plain":
            plain.append(text)
        elif ct == "text/html":
            html_body.append(text)
    if plain:
        return "\n".join(plain), False, atts
    return "\n".join(html_body), True, atts


def group_threads(records: list[dict]) -> dict[str, list[dict]]:
    """Union-find over Gmail thread ids, References/In-Reply-To, then subject."""
    parent: dict[str, str] = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for r in records:
        key = r["message_id"] or f"idx:{r['index']}"
        r["_key"] = key; find(key)
        if r["gm_thrid"]:
            union(f"gm:{r['gm_thrid']}", key)
        for ref in r["refs"]:
            union(ref, key)
    # subject fallback for messages with no linkage at all
    by_subject: dict[str, str] = {}
    for r in records:
        if not r["gm_thrid"] and not r["refs"] and r["subject_norm"]:
            if r["subject_norm"] in by_subject:
                union(by_subject[r["subject_norm"]], r["_key"])
            else:
                by_subject[r["subject_norm"]] = r["_key"]
    threads: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        threads[find(r["_key"])].append(r)
    def ts(r: dict) -> float:
        d = r["date"]
        if not d:
            return 0.0
        try:
            return d.timestamp()
        except (OverflowError, ValueError):
            return 0.0

    for t in threads.values():
        t.sort(key=ts)
    return threads


def thread_id_of(msgs: list[dict]) -> str:
    if msgs[0]["gm_thrid"]:
        return f"gm-{msgs[0]['gm_thrid']}"
    return "th-" + hashlib.sha1((msgs[0]["message_id"] or msgs[0]["subject_norm"] or str(msgs[0]["index"])).encode()).hexdigest()[:16]


def keywords_for(m: dict, subject: str) -> list[str]:
    kws = set()
    if m["from_addr"]: kws.add(m["from_addr"])
    if m["from_name"]: kws.add(m["from_name"])
    kws.update(w for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", subject) if w.lower() not in STOP)
    kws.update(IDENT_RE.findall(m["clean"])[:20])
    kws.update(a["filename"] for a in m["atts"])
    return sorted(kws)[:30]


def split_paragraphs(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        if len(cur) + len(para) + 2 > limit and cur:
            out.append(cur.strip()); cur = ""
        cur += para + "\n\n"
    if cur.strip():
        out.append(cur.strip())
    return out


def build_document(msgs: list[dict], account: str, kb: str) -> dict:
    tid = thread_id_of(msgs)
    subject = next((m["subject"] for m in msgs if m["subject"]), "(no subject)")
    dates = [m["date"] for m in msgs if m["date"]]
    first, last = (min(dates), max(dates)) if dates else (None, None)
    participants = sorted({m["from_addr"] for m in msgs if m["from_addr"]} |
                          {a for m in msgs for a in m["to_addrs"]})
    domains = sorted({p.split("@")[-1] for p in participants if "@" in p})
    labels = sorted({l for m in msgs for l in m["labels"]})
    n_att = sum(len(m["atts"]) for m in msgs)

    lines = [f"# {subject}", "",
             f"Thread {tid} · {len(msgs)} message(s) · {first:%Y-%m-%d} → {last:%Y-%m-%d}" if first else f"Thread {tid} · {len(msgs)} message(s)",
             f"Participants: {', '.join(participants[:20])}", ""]
    chunks = []
    for i, m in enumerate(msgs, 1):
        when = m["date"].strftime("%Y-%m-%d %H:%M") if m["date"] else "unknown date"
        who = f"{m['from_name']} <{m['from_addr']}>" if m["from_name"] else m["from_addr"]
        lines += [f"## Message {i} — {who} — {when}", ""]
        if m["to_addrs"]:
            lines.append(f"To: {', '.join(m['to_addrs'][:10])}")
        if m["atts"]:
            lines.append("Attachments: " + ", ".join(f"{a['filename']} ({a['size']//1024} KB)" for a in m["atts"]))
        lines += ["", m["clean"] or "(empty after quote removal)", ""]
        prefix = (f"Email thread: {subject}\nFrom: {who}\nDate: {when}\nTo: {', '.join(m['to_addrs'][:5])}"
                  + (f"\nAttachments: {', '.join(a['filename'] for a in m['atts'])}" if m["atts"] else "")
                  + f"\nMessage {i} of {len(msgs)}\n\n")
        body = m["clean"] or "(no text; attachments only)"
        parts = split_paragraphs(body, CHUNK_CHARS - len(prefix))
        for j, part in enumerate(parts, 1):
            chunks.append({
                "content": prefix + part,
                "keywords": keywords_for(m, subject),
                "questions": [],
                "meta": {"message_id": m["message_id"], "part": j, "of": len(parts), "date": when, "from": m["from_addr"]},
            })
    return {
        "source_id": f"{account}:{tid}",
        "kb": kb,
        "title": f"{first:%Y-%m-%d} {subject}"[:180] + ".md" if first else f"{subject}"[:180] + ".md",
        "rendered": "\n".join(lines),
        "metadata": {
            "account": account, "thread_id": tid, "subject": subject,
            "year": first.year if first else "", "first_date": first.strftime("%Y-%m-%d") if first else "",
            "last_date": last.strftime("%Y-%m-%d") if last else "", "message_count": len(msgs),
            "participants": ";".join(participants[:20]), "domains": ";".join(domains[:10]),
            "labels": ";".join(labels[:10]), "attachment_count": n_att, "family": "email",
        },
        "chunks": chunks,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mbox", nargs="+", help=".mbox files (or folders containing them)")
    ap.add_argument("--config", required=True, help="phase1 config.local.yaml (RAGFlow URL/key/models)")
    ap.add_argument("--account", help="mailbox label used in KB name and metadata (default: mbox file stem)")
    ap.add_argument("--kb", help="knowledge base name (default: <prefix>mail-<account>)")
    ap.add_argument("--attachments-dir", default="attachments", help="where extracted attachments go ('' to skip)")
    ap.add_argument("--since", type=int, default=0, help="ignore messages before this year")
    ap.add_argument("--skip-labels", default="Spam,Trash", help="comma list of Gmail labels to ignore")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--manifest", default="mbox_manifest.jsonl")
    ap.add_argument("--dry-run", action="store_true", help="write JSONL instead of pushing to RAGFlow")
    ap.add_argument("--jsonl", help="also/only write documents to this JSONL path")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    prefix = cfg.get("knowledge_bases", {}).get("name_prefix", "bt-")
    skip_labels = {l.strip().lower() for l in args.skip_labels.split(",") if l.strip()}
    att_dir = Path(args.attachments_dir) if args.attachments_dir else None
    manifest = Manifest(Path(args.manifest))
    sink = make_sink(cfg, args.dry_run, Path(args.jsonl) if args.jsonl else None)

    files: list[Path] = []
    for p in args.mbox:
        pp = Path(p).expanduser()
        files += sorted(pp.rglob("*.mbox")) if pp.is_dir() else [pp]
    t0 = time.time()
    seen_att: dict[str, str] = {}
    att_rows: list[dict] = []
    total_msgs = total_docs = total_chunks = skipped_threads = 0

    for mbox_path in files:
        account = args.account or re.sub(r"[^\w.@-]+", "_", mbox_path.stem)
        kb = args.kb or f"{prefix}mail-{re.sub(r'[^A-Za-z0-9._-]+', '-', account).lower()}"
        print(f"reading {mbox_path} → KB {kb}", file=sys.stderr)
        records: list[dict] = []
        for i, msg in enumerate(mailbox.mbox(str(mbox_path))):
            if args.limit and i >= args.limit:
                break
            labels = [l.strip() for l in hdr(msg, "X-Gmail-Labels").split(",") if l.strip()]
            if skip_labels & {l.lower() for l in labels}:
                continue
            d = msg_date(msg)
            if args.since and d and d.year < args.since:
                continue
            raw, is_html, atts = body_and_attachments(msg, att_dir, seen_att)
            cleaned = clean_body(raw, is_html)
            name, addr = parseaddr(hdr(msg, "From"))
            to_addrs = [a.lower() for _, a in getaddresses([hdr(msg, "To"), hdr(msg, "Cc")]) if a]
            refs = [r for r in re.findall(r"<[^>]+>", hdr(msg, "References") + " " + hdr(msg, "In-Reply-To"))]
            mid = hdr(msg, "Message-ID").strip()
            records.append({
                "index": i, "message_id": mid, "gm_thrid": hdr(msg, "X-GM-THRID").strip(), "refs": refs,
                "subject": hdr(msg, "Subject").strip(), "subject_norm": norm_subject(hdr(msg, "Subject")),
                "date": d, "from_name": name.strip(), "from_addr": addr.lower(), "to_addrs": to_addrs,
                "labels": labels, "clean": cleaned["text"], "atts": atts,
            })
            for a in atts:
                if a["path"]:
                    att_rows.append({"root": str(att_dir.resolve()), "path": str(Path(a["path"]).resolve()),
                                     "name": a["filename"], "ext": Path(a["filename"]).suffix.lower().lstrip("."),
                                     "family": "email-attachment", "size_bytes": a["size"],
                                     "modified": d.strftime("%Y-%m-%d") if d else "", "year": d.year if d else "",
                                     "depth": 2, "top_folder": f"mail-{account}", "sha256": a["sha256"],
                                     "pdf_pages": "", "pdf_encrypted": "", "pdf_scanned": "",
                                     "xlsx_sheets": "", "xlsx_max_rows": "", "xlsx_has_formulas": "",
                                     "message_id": mid, "thread_subject": hdr(msg, "Subject")[:120]})
            if len(records) % 2000 == 0:
                print(f"  {len(records):,} messages read, {time.time()-t0:.0f}s", file=sys.stderr)
        total_msgs += len(records)
        threads = group_threads(records)
        print(f"  {len(records):,} messages → {len(threads):,} threads", file=sys.stderr)
        for n, msgs in enumerate(threads.values(), 1):
            doc = build_document(msgs, account, kb)
            if manifest.has(doc["source_id"], version=len(msgs)):
                skipped_threads += 1; continue
            try:
                res = sink.write(doc)
                manifest.record({"source_id": doc["source_id"], "kb": kb, "messages": len(msgs),
                                 "version": len(msgs), "chunks": len(doc["chunks"]), **res})
                total_docs += 1; total_chunks += len(doc["chunks"])
            except Exception as e:
                manifest.record({"source_id": doc["source_id"], "status": "failed", "error": str(e)[:300]})
                print(f"!! thread {doc['source_id']}: {e}", file=sys.stderr)
            if n % 200 == 0:
                print(f"  {n:,}/{len(threads):,} threads pushed, {time.time()-t0:.0f}s", file=sys.stderr)

    sink.close()
    if att_rows and att_dir:
        # de-duplicate rows by sha256 (first message wins) — the loader dedupes again anyway
        seen = set(); rows = []
        for r in att_rows:
            if r["sha256"] in seen: continue
            seen.add(r["sha256"]); rows.append(r)
        out = att_dir / "attachments.csv"
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
        print(f"\n{len(rows):,} unique attachments extracted → {out}  "
              f"(load with: python ../phase1/load_documents.py --config <cfg> --census {out})")
    print(f"\ndone: {total_msgs:,} messages, {total_docs:,} threads pushed ({skipped_threads:,} already done), "
          f"{total_chunks:,} chunks, {time.time()-t0:.0f}s"
          + (f"  → {sink.path}" if hasattr(sink, 'path') else ""))


if __name__ == "__main__":
    main()
