#!/usr/bin/env python3
"""
ingest_code.py — Git repositories → per-file documents + AST-level chunks → RAGFlow.

For every source file in every repo it finds (or the repos listed in Phase 0 repos.csv):
  • the file is uploaded as the citation target (rendered as Markdown with a header
    naming repo, path, language, last commit, author, date — then the code in a fence)
  • chunks are one per method / constructor / function (tree-sitter), one per SQL
    procedure / table / view, one per MyBatis statement, one per Markdown section —
    each prefixed with "repo / path / kind / qualified name / lines a–b" so a chunk
    read on its own still says where it lives ("contextual retrieval")
  • identifiers in the chunk become RAGFlow important_keywords → exact-name lookups
  • commit messages (last --commits per repo) go into one extra document per repo,
    one chunk per commit, because that is where the "why" lives

Knowledge base: one per repo, named <prefix>code-<repo>. Metadata per document:
repo, path, language, kind, last_commit, last_author, last_date, year, loc.

Usage
  python ingest_code.py --config ../phase1/config.local.yaml /src/repos              # find every .git below
  python ingest_code.py --config cfg.yaml --repos-csv ../phase0/census_out/repos.csv   # repos found by Phase 0
  python ingest_code.py --config cfg.yaml /src/orders-service --dry-run              # JSONL only
  python ingest_code.py --config cfg.yaml /src/repos --commits 500 --max-file-kb 500

Resumable: files are keyed repo:path@blob-sha in --manifest; unchanged files are skipped on rerun,
so re-running after a `git pull` re-indexes only what changed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chunkers.code_chunker import chunk_file, TS_LANG  # noqa: E402
from sink import Manifest, make_sink  # noqa: E402

SOURCE_EXT = set(TS_LANG) | {"sql", "prc", "ddl", "sp", "xml", "properties", "yml", "yaml", "json", "md", "adoc", "txt",
                             "html", "jsp", "ftl", "gradle", "sh", "bat", "ps1", "rpg", "rpgle", "sqlrpgle", "clle", "cbl"}
VENDORED = {"node_modules", "target", "build", "dist", "out", "bin", ".gradle", "vendor", ".idea", ".vscode",
            "coverage", ".angular", "__pycache__", "bower_components", ".git", ".svn"}
LANG_NAME = {"java": "Java", "kt": "Kotlin", "cs": "C#", "ts": "TypeScript", "tsx": "TypeScript", "js": "JavaScript",
             "jsx": "JavaScript", "py": "Python", "sql": "SQL", "prc": "SQL", "ddl": "SQL", "sp": "SQL", "xml": "XML",
             "properties": "Properties", "yml": "YAML", "yaml": "YAML", "json": "JSON", "md": "Markdown", "adoc": "AsciiDoc",
             "txt": "Text", "html": "HTML", "jsp": "JSP", "ftl": "Template", "gradle": "Gradle", "sh": "Shell",
             "bat": "Batch", "ps1": "PowerShell", "rpg": "RPG", "rpgle": "RPG", "sqlrpgle": "RPG", "clle": "CL", "cbl": "COBOL"}


def sh(args: list[str], cwd: Path) -> str:
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=120, errors="replace").stdout
    except Exception:
        return ""


def find_repos(roots: list[str], max_depth: int = 4) -> list[Path]:
    repos = []
    for root in roots:
        rp = Path(root).expanduser().resolve()
        if (rp / ".git").exists():
            repos.append(rp); continue
        for dirpath, dirnames, _ in os.walk(rp):
            d = Path(dirpath)
            if (d / ".git").exists():
                repos.append(d); dirnames[:] = []; continue
            if len(d.relative_to(rp).parts) >= max_depth:
                dirnames[:] = []
            dirnames[:] = [x for x in dirnames if x not in VENDORED]
        if not any(r == rp or rp in r.parents for r in repos) and rp.is_dir():
            repos.append(rp)  # plain source tree without .git
    return sorted(set(repos))


def git_file_info(repo: Path, is_git: bool) -> dict[str, tuple[str, str, str]]:
    """path → (commit, author, date) of the last change, in one git call."""
    if not is_git:
        return {}
    out = sh(["git", "log", "--name-only", "--format=%x1e%h%x1f%an%x1f%as", "--no-merges"], repo)
    info: dict[str, tuple[str, str, str]] = {}
    for block in out.split("\x1e")[1:]:
        head, _, files = block.partition("\n")
        parts = head.split("\x1f")
        if len(parts) != 3:
            continue
        for f in files.splitlines():
            f = f.strip()
            if f and f not in info:
                info[f] = (parts[0], parts[1], parts[2])
    return info


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()[:12]


def iter_source_files(repo: Path, max_bytes: int):
    for dirpath, dirnames, filenames in os.walk(repo):
        d = Path(dirpath)
        if set(d.relative_to(repo).parts) & VENDORED:
            dirnames[:] = []; continue
        dirnames[:] = [x for x in dirnames if x not in VENDORED]
        for fn in filenames:
            p = d / fn
            ext = p.suffix.lower().lstrip(".")
            if ext not in SOURCE_EXT and fn.lower() not in ("dockerfile", "makefile", "pom.xml"):
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size == 0 or size > max_bytes:
                continue
            yield p, ext


def build_file_document(repo: Path, p: Path, ext: str, kb: str, info: dict, prefix_meta: dict) -> dict | None:
    data = p.read_bytes()
    if b"\x00" in data[:4000]:
        return None  # binary
    text = data.decode("utf-8", "replace")
    rel = str(p.relative_to(repo)).replace("\\", "/")
    kind, chunks = chunk_file(p, text)
    if not chunks:
        return None
    commit, author, date = info.get(rel, ("", "", ""))
    lang = LANG_NAME.get(ext, ext.upper())
    header = (f"# {repo.name}/{rel}\n\n"
              f"Repository: {repo.name} · Language: {lang} · Kind: {kind} · {text.count(chr(10))+1} lines"
              + (f" · Last commit {commit} by {author} on {date}" if commit else "") + "\n\n")
    fence = "```" + {"Java": "java", "TypeScript": "ts", "JavaScript": "js", "SQL": "sql", "XML": "xml",
                     "Python": "python", "C#": "csharp", "YAML": "yaml", "JSON": "json"}.get(lang, "") + "\n"
    rendered = header + (text if kind == "markdown" else fence + text + "\n```\n")
    out_chunks = []
    for c in chunks:
        ctx = (f"Repo: {repo.name} | File: {rel} | {c.kind}: {c.name} | lines {c.start_line}-{c.end_line}"
               + (f" | last change {date} by {author} ({commit})" if commit else "") + "\n\n")
        out_chunks.append({
            "content": ctx + c.text,
            "keywords": list(dict.fromkeys([c.name.split(".")[-1], c.name, p.name, repo.name] + c.identifiers))[:30],
            "questions": [],
            "meta": {"kind": c.kind, "name": c.name, "start_line": c.start_line, "end_line": c.end_line},
        })
    return {
        "source_id": f"{repo.name}:{rel}@{blob_sha(data)}",
        "kb": kb,
        "title": f"{repo.name}__{rel.replace('/', '__')}.md"[-200:],
        "rendered": rendered,
        "metadata": {"repo": repo.name, "path": rel, "language": lang, "kind": kind, "ext": ext,
                     "last_commit": commit, "last_author": author, "last_date": date,
                     "year": date[:4] if date else "", "loc": text.count("\n") + 1, "family": "code", **prefix_meta},
        "chunks": out_chunks,
    }


def build_commits_document(repo: Path, kb: str, n: int) -> dict | None:
    out = sh(["git", "log", f"-{n}", "--format=%x1e%H%x1f%h%x1f%an%x1f%as%x1f%s%x1f%b", "--stat=120", "--no-merges"], repo)
    blocks = [b for b in out.split("\x1e") if b.strip()]
    if not blocks:
        return None
    chunks, lines = [], [f"# {repo.name} — commit history (last {len(blocks)})", ""]
    for b in blocks:
        parts = b.split("\x1f")
        if len(parts) < 6:
            continue
        full, short, author, date, subject, rest = parts[:6]
        body, _, stat = rest.partition("\n\n") if "\n\n" in rest else ("", "", rest)
        files = [l.strip().split("|")[0].strip() for l in stat.splitlines() if "|" in l][:25]
        text = (f"Commit {short} — {date} — {author}\n{subject}\n" + (f"\n{body.strip()}\n" if body.strip() else "")
                + (f"\nFiles: {', '.join(files)}" if files else ""))
        lines += [f"## {short} {date} {author}", "", subject, body.strip(), ""]
        chunks.append({"content": f"Repo: {repo.name} | commit {short}\n\n{text}",
                       "keywords": [short, author, repo.name] + [Path(f).name for f in files[:10]],
                       "questions": [], "meta": {"commit": full, "date": date, "author": author}})
    return {"source_id": f"{repo.name}:__commits__@{blocks[0][:12]}", "kb": kb, "title": f"{repo.name}__commit-history.md",
            "rendered": "\n".join(lines), "metadata": {"repo": repo.name, "kind": "commit-history", "family": "code"},
            "chunks": chunks}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roots", nargs="*", help="folders containing repositories (or a single repo)")
    ap.add_argument("--repos-csv", help="repos.csv from phase0/census_git.py (uses its 'path' column)")
    ap.add_argument("--config", required=True)
    ap.add_argument("--kb-prefix", help="KB name prefix (default <config prefix>code-)")
    ap.add_argument("--project", default="", help="project/domain label stored in metadata for all repos in this run")
    ap.add_argument("--commits", type=int, default=300, help="commit messages to index per repo (0 = none)")
    ap.add_argument("--max-file-kb", type=int, default=400)
    ap.add_argument("--manifest", default="code_manifest.jsonl")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--jsonl")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    prefix = args.kb_prefix or (cfg.get("knowledge_bases", {}).get("name_prefix", "bt-") + "code-")
    manifest = Manifest(Path(args.manifest))
    sink = make_sink(cfg, args.dry_run, Path(args.jsonl) if args.jsonl else None)
    repos: list[Path] = []
    if args.repos_csv:
        repos += [Path(r["path"]) for r in csv.DictReader(Path(args.repos_csv).open(encoding="utf-8"))]
    if args.roots:
        repos += find_repos(args.roots)
    repos = sorted({r.resolve() for r in repos if r.exists()})
    if not repos:
        print("no repositories found", file=sys.stderr); sys.exit(1)

    t0 = time.time(); stats = Counter()
    for repo in repos:
        is_git = (repo / ".git").exists()
        kb = prefix + "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in repo.name).lower()
        info = git_file_info(repo, is_git)
        n_files = n_chunks = n_skip = 0
        for p, ext in iter_source_files(repo, args.max_file_kb * 1024):
            try:
                doc = build_file_document(repo, p, ext, kb, info, {"project": args.project} if args.project else {})
            except Exception as e:
                print(f"!! {p}: {e}", file=sys.stderr); stats["errors"] += 1; continue
            if doc is None:
                continue
            if manifest.has(doc["source_id"]):
                n_skip += 1; continue
            try:
                res = sink.write(doc)
                manifest.record({"source_id": doc["source_id"], "kb": kb, "chunks": len(doc["chunks"]), **res})
                n_files += 1; n_chunks += len(doc["chunks"])
                stats[doc["metadata"]["kind"]] += len(doc["chunks"])
            except Exception as e:
                manifest.record({"source_id": doc["source_id"], "status": "failed", "error": str(e)[:300]})
                print(f"!! {p}: {e}", file=sys.stderr); stats["errors"] += 1
        if is_git and args.commits:
            cd = build_commits_document(repo, kb, args.commits)
            if cd and not manifest.has(cd["source_id"]):
                res = sink.write(cd)
                manifest.record({"source_id": cd["source_id"], "kb": kb, "chunks": len(cd["chunks"]), **res})
                n_chunks += len(cd["chunks"]); stats["commits"] += len(cd["chunks"])
        print(f"  {repo.name:40s} → {kb:40s} {n_files:>5} files {n_chunks:>7} chunks ({n_skip} unchanged) {time.time()-t0:.0f}s")
        stats["files"] += n_files; stats["chunks_total"] += n_chunks
    sink.close()
    print(f"\ndone: {len(repos)} repos, {stats['files']:,} files, {stats['chunks_total']:,} chunks, {stats['errors']} errors")
    print("chunks by kind:", ", ".join(f"{k}={v:,}" for k, v in stats.most_common() if k not in ("files", "chunks_total", "errors")))
    if hasattr(sink, "path"):
        print(f"→ {sink.path}")


if __name__ == "__main__":
    main()
