#!/usr/bin/env python3
"""
census_mbox.py — inventory Gmail (Google Takeout .mbox) exports for RAG planning.

For each .mbox (or a folder of them) it counts messages, threads, years,
attachments by type, top sender domains, Gmail labels, quoted-reply weight
(how much of the body text is quoted history) and exact-duplicate attachments.
No message bodies are written out — only counts and a sample list.

Outputs (into --out, default ./census_out):
  mbox_messages.csv   one row per message (headers + attachment summary, no body)
  mbox_summary.json   the numbers you need for sizing
  mbox_samples.csv    long threads / big attachments / odd encodings to test parsing on

Usage:
  python census_mbox.py "~/Takeout/Mail/All mail Including Spam and Trash.mbox"
  python census_mbox.py ~/Takeout/Mail --out census_out --limit 50000   # first N messages only

How to get the .mbox: https://takeout.google.com -> deselect all -> Mail -> export.
Standard library only.
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
from collections import Counter, defaultdict
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

QUOTE_RE = re.compile(r"^\s*>", re.M)
ON_WROTE_RE = re.compile(r"^On .{5,120} wrote:\s*$", re.M)
ORIGINAL_RE = re.compile(r"^-{2,}\s*(Original Message|Forwarded message)\s*-{2,}", re.M | re.I)
SIG_RE = re.compile(r"^--\s*$", re.M)


def hdr(msg, name: str) -> str:
    v = msg.get(name)
    if not v:
        return ""
    try:
        return str(make_header(decode_header(v)))
    except Exception:
        return str(v)


def body_stats(msg) -> tuple[int, int, bool]:
    """Return (chars_total, chars_quoted, has_html_only)."""
    text, has_plain, has_html = "", False, False
    for part in msg.walk():
        ct = part.get_content_type()
        if part.get_content_disposition() == "attachment":
            continue
        if ct == "text/plain":
            has_plain = True
            try:
                text += part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:
                pass
        elif ct == "text/html":
            has_html = True
    total = len(text)
    quoted = 0
    m = ON_WROTE_RE.search(text) or ORIGINAL_RE.search(text)
    if m:
        quoted = total - m.start()
    else:
        quoted = sum(len(l) + 1 for l in text.splitlines() if l.startswith(">"))
    return total, quoted, (has_html and not has_plain)


def attachments(msg) -> list[dict]:
    out = []
    for part in msg.walk():
        if part.get_content_disposition() != "attachment" and not part.get_filename():
            continue
        fn = part.get_filename() or ""
        try:
            fn = str(make_header(decode_header(fn)))
        except Exception:
            pass
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            payload = b""
        out.append({
            "filename": fn,
            "ext": Path(fn).suffix.lower().lstrip("."),
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest() if payload else "",
        })
    return out


def iter_mbox_files(paths: list[str]):
    for p in paths:
        pp = Path(p).expanduser()
        if pp.is_dir():
            yield from sorted(pp.rglob("*.mbox"))
        elif pp.exists():
            yield pp
        else:
            print(f"!! not found: {p}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help=".mbox files or folders containing them")
    ap.add_argument("--out", default="census_out")
    ap.add_argument("--limit", type=int, default=0, help="stop after N messages (0 = all)")
    args = ap.parse_args()
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    att_hashes: Counter = Counter()
    att_by_ext = defaultdict(lambda: {"count": 0, "bytes": 0})
    threads: Counter = Counter()
    senders: Counter = Counter()
    labels: Counter = Counter()
    years: Counter = Counter()
    bad_dates = html_only = 0
    total_chars = quoted_chars = 0
    t0 = time.time()

    for mbox_path in iter_mbox_files(args.paths):
        print(f"reading {mbox_path} ...", file=sys.stderr)
        mb = mailbox.mbox(str(mbox_path))
        for i, msg in enumerate(mb):
            if args.limit and len(rows) >= args.limit:
                break
            date_s = hdr(msg, "Date")
            try:
                dt = parsedate_to_datetime(date_s)
                year = dt.year
                date_iso = dt.strftime("%Y-%m-%d")
            except Exception:
                year, date_iso = 0, ""
                bad_dates += 1
            thread = hdr(msg, "X-GM-THRID") or hdr(msg, "In-Reply-To") or hdr(msg, "Message-ID")
            threads[thread] += 1
            from_addr = parseaddr(hdr(msg, "From"))[1].lower()
            domain = from_addr.split("@")[-1] if "@" in from_addr else ""
            senders[domain] += 1
            for lab in [l.strip() for l in hdr(msg, "X-Gmail-Labels").split(",") if l.strip()]:
                labels[lab] += 1
            years[year] += 1
            tot, quo, honly = body_stats(msg)
            total_chars += tot; quoted_chars += quo; html_only += honly
            atts = attachments(msg)
            for a in atts:
                att_by_ext[a["ext"] or "(none)"]["count"] += 1
                att_by_ext[a["ext"] or "(none)"]["bytes"] += a["size"]
                if a["sha256"]:
                    att_hashes[a["sha256"]] += 1
            rows.append({
                "mbox": mbox_path.name, "index": i, "message_id": hdr(msg, "Message-ID"),
                "thread_id": thread, "date": date_iso, "year": year,
                "from_domain": domain, "subject": hdr(msg, "Subject")[:200],
                "labels": hdr(msg, "X-Gmail-Labels")[:200],
                "body_chars": tot, "quoted_chars": quo, "html_only": "yes" if honly else "no",
                "attachment_count": len(atts),
                "attachment_bytes": sum(a["size"] for a in atts),
                "attachment_exts": ";".join(sorted({a["ext"] for a in atts if a["ext"]})),
            })
            if len(rows) % 5000 == 0:
                print(f"  {len(rows):,} messages, {time.time()-t0:.0f}s", file=sys.stderr)

    if not rows:
        print("no messages found", file=sys.stderr); sys.exit(1)

    with (out / "mbox_messages.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

    thread_sizes = sorted(threads.values(), reverse=True)
    dup_att = sum(c - 1 for c in att_hashes.values() if c > 1)
    summary = {
        "messages": len(rows),
        "threads": len(threads),
        "avg_messages_per_thread": round(len(rows) / max(1, len(threads)), 2),
        "largest_threads": thread_sizes[:10],
        "year_range": [min(y for y in years if y), max(years)] if any(years) else None,
        "by_year": dict(sorted(years.items())),
        "messages_with_bad_dates": bad_dates,
        "html_only_messages": html_only,
        "body_chars_total": total_chars,
        "quoted_chars_total": quoted_chars,
        "quoted_ratio": round(quoted_chars / max(1, total_chars), 3),
        "attachments_total": sum(v["count"] for v in att_by_ext.values()),
        "attachment_bytes_total": sum(v["bytes"] for v in att_by_ext.values()),
        "attachments_by_ext": dict(sorted(att_by_ext.items(), key=lambda kv: -kv[1]["count"])[:40]),
        "duplicate_attachments": dup_att,
        "unique_attachments": len(att_hashes),
        "top_sender_domains": senders.most_common(30),
        "top_labels": labels.most_common(30),
    }
    (out / "mbox_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # samples: longest threads, biggest attachment messages, html-only, bad dates
    sample_rows = []
    biggest_threads = {t for t, _ in threads.most_common(10)}
    seen = set()
    for r in rows:
        why = []
        if r["thread_id"] in biggest_threads and r["thread_id"] not in seen:
            why.append("long-thread"); seen.add(r["thread_id"])
        if r["attachment_bytes"] > 5_000_000: why.append("big-attachment")
        if r["html_only"] == "yes" and len([s for s in sample_rows if "html-only" in s["reason"]]) < 5: why.append("html-only")
        if not r["date"] and len([s for s in sample_rows if "bad-date" in s["reason"]]) < 5: why.append("bad-date")
        if r["quoted_chars"] > 0 and r["body_chars"] and r["quoted_chars"] / r["body_chars"] > 0.9 and \
                len([s for s in sample_rows if "mostly-quoted" in s["reason"]]) < 5: why.append("mostly-quoted")
        if why:
            sample_rows.append({"mbox": r["mbox"], "index": r["index"], "message_id": r["message_id"],
                                "date": r["date"], "subject": r["subject"], "reason": ",".join(why),
                                "parser_result": "", "notes": ""})
    with (out / "mbox_samples.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["mbox", "index", "message_id", "date", "subject", "reason", "parser_result", "notes"])
        w.writeheader(); w.writerows(sample_rows[:60])

    print(f"\n{summary['messages']:,} messages in {summary['threads']:,} threads, years {summary['year_range']}")
    print(f"quoted text ratio {summary['quoted_ratio']:.0%}  html-only {html_only:,}  bad dates {bad_dates:,}")
    print(f"attachments {summary['attachments_total']:,} ({summary['attachment_bytes_total']/1e9:.2f} GB), "
          f"{summary['unique_attachments']:,} unique, {dup_att:,} duplicates")
    print("top attachment types:", ", ".join(f"{k}={v['count']}" for k, v in list(summary["attachments_by_ext"].items())[:10]))
    print(f"wrote {out/'mbox_messages.csv'}, {out/'mbox_summary.json'}, {out/'mbox_samples.csv'} in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
