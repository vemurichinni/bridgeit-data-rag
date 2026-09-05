#!/usr/bin/env python3
"""
census_git.py — inventory source-code repositories for RAG planning.

Given a folder that contains Git repositories (or plain source trees), it
records per repo: languages by file count and lines, commit count and date
range, number of authors, largest source files, generated/vendored folders,
and counts of the artefacts that matter for a Java/SQL shop — MyBatis mapper
XMLs, stored-procedure scripts, Spring config, Angular components, README/docs.

Outputs (into --out, default ./census_out):
  repos.csv           one row per repository
  code_files.csv      one row per source file (repo, path, language, lines)
  git_summary.json    totals
  code_samples.csv    files worth testing the AST chunker on (huge classes, big mappers, long procs)

Usage:
  python census_git.py /archive/repos --out census_out
  python census_git.py /archive/repos /other/repos --max-depth 3

Standard library + the `git` executable (optional; without it you still get file stats).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

LANG = {
    "java": "Java", "kt": "Kotlin", "groovy": "Groovy", "scala": "Scala",
    "sql": "SQL", "prc": "SQL", "sp": "SQL", "ddl": "SQL", "rpg": "RPG", "rpgle": "RPG", "sqlrpgle": "RPG", "clle": "CL", "clp": "CL",
    "ts": "TypeScript", "js": "JavaScript", "html": "HTML", "css": "CSS", "scss": "SCSS",
    "xml": "XML", "properties": "Properties", "yml": "YAML", "yaml": "YAML", "json": "JSON",
    "cs": "C#", "py": "Python", "sh": "Shell", "bat": "Batch", "ps1": "PowerShell",
    "jsp": "JSP", "jspx": "JSP", "ftl": "Template", "vm": "Template",
    "md": "Markdown", "txt": "Text", "adoc": "AsciiDoc",
    "gradle": "Gradle", "pom": "Maven", "dockerfile": "Docker",
    "cbl": "COBOL", "cob": "COBOL", "pl": "Perl", "php": "PHP", "rb": "Ruby", "go": "Go",
}
VENDORED = {"node_modules", "target", "build", "dist", "out", "bin", ".gradle", "vendor", "lib", "libs",
            ".idea", ".vscode", "coverage", ".angular", "__pycache__", "bower_components"}
SKIP = {".git", ".svn"}


def sh(args: list[str], cwd: Path) -> str:
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=120).stdout.strip()
    except Exception:
        return ""


def is_repo(p: Path) -> bool:
    return (p / ".git").exists()


def find_repos(roots: list[str], max_depth: int) -> list[Path]:
    repos = []
    for root in roots:
        rp = Path(root).expanduser()
        if not rp.exists():
            print(f"!! not found: {root}", file=sys.stderr); continue
        if is_repo(rp):
            repos.append(rp); continue
        for dirpath, dirnames, _ in os.walk(rp):
            d = Path(dirpath)
            depth = len(d.relative_to(rp).parts)
            if is_repo(d):
                repos.append(d); dirnames[:] = []; continue
            if depth >= max_depth:
                dirnames[:] = []
            dirnames[:] = [x for x in dirnames if x not in SKIP and x not in VENDORED]
        # plain source trees with no .git at top level count as one "repo" each if they hold code
        if not any(r == rp or rp in r.parents for r in repos):
            repos.append(rp)
    return sorted(set(repos))


def count_lines(p: Path) -> int:
    try:
        with p.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def classify(rel: str, ext: str, p: Path) -> str:
    """Tag artefacts that need special chunking."""
    name = p.name.lower()
    if ext == "xml":
        try:
            head = p.read_text("utf-8", "ignore")[:2000].lower()
        except OSError:
            head = ""
        if "mybatis" in head or "<mapper" in head:
            return "mybatis-mapper"
        if "pom.xml" == name:
            return "maven-pom"
        if "beans" in head and "springframework" in head:
            return "spring-xml"
        return "xml"
    if ext in ("sql", "prc", "sp", "ddl"):
        try:
            head = p.read_text("utf-8", "ignore")[:4000].lower()
        except OSError:
            head = ""
        if "create procedure" in head or "create proc" in head or "create or replace procedure" in head:
            return "stored-procedure"
        if "create table" in head or "alter table" in head:
            return "ddl"
        return "sql-script"
    if ext == "ts" and name.endswith(".component.ts"):
        return "angular-component"
    if ext == "java":
        try:
            head = p.read_text("utf-8", "ignore")[:3000]
        except OSError:
            head = ""
        if "@RestController" in head or "@Controller" in head: return "java-controller"
        if "@Service" in head: return "java-service"
        if "@Mapper" in head or "@Repository" in head: return "java-repository"
        if "@Entity" in head: return "java-entity"
        if name.endswith("test.java") or name.endswith("tests.java"): return "java-test"
        return "java"
    if ext in ("md", "adoc", "txt") or "readme" in name or "/docs/" in rel.replace("\\", "/").lower():
        return "docs"
    return LANG.get(ext, "other").lower()


def scan_repo(repo: Path) -> tuple[dict, list[dict]]:
    files, langs, kinds = [], Counter(), Counter()
    lines_by_lang: Counter = Counter()
    vendored_bytes = 0
    for dirpath, dirnames, filenames in os.walk(repo):
        d = Path(dirpath)
        parts = set(d.relative_to(repo).parts)
        if parts & SKIP:
            dirnames[:] = []; continue
        if parts & VENDORED:
            for fn in filenames:
                try: vendored_bytes += (d / fn).stat().st_size
                except OSError: pass
            dirnames[:] = []; continue
        for fn in filenames:
            p = d / fn
            ext = p.suffix.lower().lstrip(".") or ("dockerfile" if fn.lower() == "dockerfile" else "")
            lang = LANG.get(ext)
            if not lang:
                continue
            try: size = p.stat().st_size
            except OSError: continue
            if size > 5_000_000:  # generated / minified / data
                continue
            rel = str(p.relative_to(repo))
            n = count_lines(p)
            kind = classify(rel, ext, p)
            files.append({"repo": repo.name, "path": rel, "language": lang, "kind": kind, "lines": n, "bytes": size})
            langs[lang] += 1; lines_by_lang[lang] += n; kinds[kind] += 1

    git_info = {"commits": "", "first_commit": "", "last_commit": "", "authors": "", "branches": "", "remote": ""}
    if is_repo(repo):
        git_info["commits"] = sh(["git", "rev-list", "--all", "--count"], repo)
        git_info["first_commit"] = sh(["git", "log", "--reverse", "--format=%ad", "--date=short", "--all"], repo).split("\n")[0]
        git_info["last_commit"] = sh(["git", "log", "-1", "--format=%ad", "--date=short", "--all"], repo)
        git_info["authors"] = str(len(set(sh(["git", "log", "--all", "--format=%ae"], repo).split("\n")) - {""}))
        git_info["branches"] = str(len(sh(["git", "branch", "-a"], repo).split("\n")))
        git_info["remote"] = sh(["git", "remote", "get-url", "origin"], repo)

    top = sorted(langs.items(), key=lambda kv: -lines_by_lang[kv[0]])
    row = {
        "repo": repo.name, "path": str(repo), "is_git": "yes" if is_repo(repo) else "no",
        **git_info,
        "source_files": len(files), "source_lines": sum(f["lines"] for f in files),
        "primary_language": top[0][0] if top else "",
        "languages": ";".join(f"{l}:{lines_by_lang[l]}" for l, _ in top[:6]),
        "java_files": langs.get("Java", 0), "sql_files": langs.get("SQL", 0), "ts_files": langs.get("TypeScript", 0),
        "mybatis_mappers": kinds.get("mybatis-mapper", 0), "stored_procedures": kinds.get("stored-procedure", 0),
        "ddl_files": kinds.get("ddl", 0), "angular_components": kinds.get("angular-component", 0),
        "controllers": kinds.get("java-controller", 0), "services": kinds.get("java-service", 0),
        "repositories": kinds.get("java-repository", 0), "entities": kinds.get("java-entity", 0),
        "tests": kinds.get("java-test", 0), "doc_files": kinds.get("docs", 0),
        "has_readme": "yes" if any(f["path"].lower().startswith("readme") for f in files) else "no",
        "vendored_bytes_skipped": vendored_bytes,
        "largest_file": max(files, key=lambda f: f["lines"])["path"] if files else "",
        "largest_file_lines": max((f["lines"] for f in files), default=0),
    }
    return row, files


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roots", nargs="+")
    ap.add_argument("--out", default="census_out")
    ap.add_argument("--max-depth", type=int, default=4, help="how deep to look for .git folders")
    args = ap.parse_args()
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    repos = find_repos(args.roots, args.max_depth)
    print(f"found {len(repos)} repositories", file=sys.stderr)
    repo_rows, all_files = [], []
    for r in repos:
        row, files = scan_repo(r)
        repo_rows.append(row); all_files.extend(files)
        print(f"  {row['repo']:40s} {row['source_files']:>6} files {row['source_lines']:>9,} lines  {row['primary_language']}", file=sys.stderr)
    if not repo_rows:
        print("nothing found", file=sys.stderr); sys.exit(1)

    with (out / "repos.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(repo_rows[0].keys())); w.writeheader(); w.writerows(repo_rows)
    if all_files:
        with (out / "code_files.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(all_files[0].keys())); w.writeheader(); w.writerows(all_files)

    lang_lines, lang_files, kinds = Counter(), Counter(), Counter()
    for fl in all_files:
        lang_lines[fl["language"]] += fl["lines"]; lang_files[fl["language"]] += 1; kinds[fl["kind"]] += 1
    years = sorted({r["first_commit"][:4] for r in repo_rows if r["first_commit"]} | {r["last_commit"][:4] for r in repo_rows if r["last_commit"]})
    summary = {
        "repositories": len(repo_rows),
        "git_repositories": sum(1 for r in repo_rows if r["is_git"] == "yes"),
        "source_files": len(all_files),
        "source_lines": sum(f["lines"] for f in all_files),
        "total_commits": sum(int(r["commits"] or 0) for r in repo_rows),
        "commit_year_range": [years[0], years[-1]] if years else None,
        "lines_by_language": dict(lang_lines.most_common()),
        "files_by_language": dict(lang_files.most_common()),
        "files_by_kind": dict(kinds.most_common()),
        "repos_without_readme": [r["repo"] for r in repo_rows if r["has_readme"] == "no"],
        "vendored_bytes_skipped": sum(r["vendored_bytes_skipped"] for r in repo_rows),
        "files_over_2000_lines": sum(1 for f in all_files if f["lines"] > 2000),
    }
    (out / "git_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # samples for the AST chunker: biggest per kind + a spread
    samples = []
    by_kind = defaultdict(list)
    for f in all_files:
        by_kind[f["kind"]].append(f)
    for kind, items in sorted(by_kind.items()):
        items.sort(key=lambda f: -f["lines"])
        for f in items[:3]:
            samples.append({**f, "reason": "largest-of-kind", "chunker_result": "", "notes": ""})
        if len(items) > 6:
            mid = items[len(items) // 2]
            samples.append({**mid, "reason": "typical-of-kind", "chunker_result": "", "notes": ""})
    with (out / "code_samples.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(samples[0].keys()) if samples else ["path"]); w.writeheader(); w.writerows(samples)

    print(f"\n{summary['repositories']} repos, {summary['source_files']:,} source files, {summary['source_lines']:,} lines, "
          f"{summary['total_commits']:,} commits, years {summary['commit_year_range']}")
    print("lines by language:", ", ".join(f"{k}={v:,}" for k, v in list(summary['lines_by_language'].items())[:8]))
    print("special artefacts:", ", ".join(f"{k}={v}" for k, v in kinds.most_common() if k in
          ("mybatis-mapper", "stored-procedure", "ddl", "angular-component", "java-controller", "java-service", "docs")))
    print(f"wrote {out/'repos.csv'}, {out/'code_files.csv'}, {out/'git_summary.json'}, {out/'code_samples.csv'} in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
