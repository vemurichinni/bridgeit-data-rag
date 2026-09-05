"""
code_chunker.py — split source files into retrieval units that match how people ask.

  Java / C# / TypeScript / JavaScript / Python   → tree-sitter AST: one chunk per method /
                                                   constructor / function; class-level chunk
                                                   for fields, annotations and small classes
  SQL (SQL Server / DB2)                         → one chunk per CREATE PROCEDURE / FUNCTION /
                                                   TRIGGER / VIEW / TABLE statement (regex)
  MyBatis mapper XML                             → one chunk per <select|insert|update|delete|
                                                   resultMap|sql> element (regex, keeps id)
  Spring/other XML, properties, YAML, JSON        → whole file, split by size
  Markdown / text                                → split on headings

Every chunk carries: kind, name (qualified where possible), start_line, end_line, and the
identifiers found in it (for RAGFlow important_keywords → exact-ID retrieval).
Oversized units are split at line boundaries with an overlap so nothing is lost.

Falls back to size-based splitting if tree-sitter is unavailable or a parse fails.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    from tree_sitter_language_pack import get_parser  # type: ignore
except Exception:  # pragma: no cover
    get_parser = None

MAX_CHARS = 3000       # chunk size ceiling (≈ 750 tokens)
OVERLAP_LINES = 8
MIN_CHARS = 40

TS_LANG = {"java": "java", "kt": "kotlin", "cs": "csharp", "ts": "typescript", "tsx": "tsx",
           "js": "javascript", "jsx": "javascript", "py": "python", "go": "go", "scala": "scala"}
UNIT_NODES = {
    "java": {"method_declaration", "constructor_declaration"},
    "kotlin": {"function_declaration"},
    "csharp": {"method_declaration", "constructor_declaration"},
    "typescript": {"method_definition", "function_declaration", "arrow_function"},
    "tsx": {"method_definition", "function_declaration"},
    "javascript": {"method_definition", "function_declaration"},
    "python": {"function_definition"},
    "go": {"function_declaration", "method_declaration"},
    "scala": {"function_definition"},
}
CONTAINER_NODES = {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration",
                   "class_body", "object_declaration", "class_definition", "abstract_class_declaration",
                   "namespace_declaration", "program", "module", "source_file", "compilation_unit"}
NAME_FIELDS = ("name", "declarator")
IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b")
KEYWORDS_SKIP = set("""public private protected static final void return class interface extends implements
import package throws throw new this super null true false else catch finally while switch case break
continue default abstract synchronized native transient volatile const function export async await
string number boolean select insert update delete from where into values begin end declare set
create alter procedure proc function trigger view table index type replace exists primary foreign
references constraint nocount transaction commit rollback exec execute print
""".split())


@dataclass
class Chunk:
    kind: str
    name: str
    start_line: int
    end_line: int
    text: str
    identifiers: list[str] = field(default_factory=list)


def identifiers(text: str, limit: int = 25) -> list[str]:
    seen, out = set(), []
    for m in IDENT_RE.findall(text):
        low = m.lower()
        if low in KEYWORDS_SKIP or m in seen:
            continue
        # prefer CamelCase / snake_case / UPPER_CASE names, which is what people search for
        if not (any(c.isupper() for c in m) or "_" in m or any(c.isdigit() for c in m)):
            continue
        seen.add(m); out.append(m)
        if len(out) >= limit:
            break
    return out


def split_big(kind: str, name: str, text: str, start_line: int) -> list[Chunk]:
    lines = text.splitlines()
    if len(text) <= MAX_CHARS:
        return [Chunk(kind, name, start_line, start_line + len(lines) - 1, text, identifiers(text))]
    out, i, part = [], 0, 1
    per = max(10, int(len(lines) * MAX_CHARS / max(1, len(text))))
    while i < len(lines):
        seg = lines[i:i + per]
        t = "\n".join(seg)
        out.append(Chunk(kind, f"{name} (part {part})", start_line + i, start_line + i + len(seg) - 1, t, identifiers(t)))
        if i + per >= len(lines):
            break
        i += max(1, per - OVERLAP_LINES); part += 1
    return out


# ---------------------------------------------------------------- tree-sitter
def _node_name(node) -> str:
    for f in NAME_FIELDS:
        n = node.child_by_field_name(f)
        if n is not None:
            if n.type in ("identifier", "name", "property_identifier", "type_identifier"):
                return n.text.decode("utf-8", "ignore")
            # declarator → identifier inside
            for c in n.children:
                if c.type in ("identifier", "property_identifier"):
                    return c.text.decode("utf-8", "ignore")
            return n.text.decode("utf-8", "ignore")[:80]
    # arrow functions assigned to a const: look at the parent variable_declarator
    p = node.parent
    if p is not None and p.type == "variable_declarator":
        n = p.child_by_field_name("name")
        if n is not None:
            return n.text.decode("utf-8", "ignore")
    return ""


def _enclosing(node) -> list[str]:
    names, p = [], node.parent
    while p is not None:
        if p.type in ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration",
                      "class_definition", "object_declaration", "abstract_class_declaration"):
            n = p.child_by_field_name("name")
            if n is not None:
                names.append(n.text.decode("utf-8", "ignore"))
        p = p.parent
    return list(reversed(names))


def chunk_with_tree_sitter(text: str, lang: str) -> list[Chunk] | None:
    if get_parser is None:
        return None
    try:
        parser = get_parser(lang)
        tree = parser.parse(text.encode("utf-8"))
    except Exception:
        return None
    src = text.encode("utf-8")
    units: list[tuple[int, int, str, str]] = []   # (start_byte, end_byte, kind, qualified name)
    unit_types = UNIT_NODES.get(lang, set())

    def walk(node):
        if node.type in unit_types:
            name = _node_name(node)
            if not name and node.type == "arrow_function":
                return
            qual = ".".join(_enclosing(node) + [name or "<anonymous>"])
            # include leading annotations/decorators/javadoc that sit directly above
            start = node.start_byte
            prev = node.prev_named_sibling
            while prev is not None and prev.type in ("annotation", "marker_annotation", "decorator", "block_comment",
                                                     "comment", "line_comment") and not src[prev.end_byte:start].strip():
                start = prev.start_byte; prev = prev.prev_named_sibling
            if node.end_byte - start < MIN_CHARS:
                return  # tiny unit: stays in the outline chunk instead of becoming its own
            units.append((start, node.end_byte, "method" if "method" in node.type or "constructor" in node.type else "function", qual))
            return  # do not descend into nested functions of a unit (they stay with it)
        for c in node.children:
            walk(c)

    walk(tree.root_node)
    if not units:
        return None
    units.sort()
    chunks: list[Chunk] = []
    # everything not inside a unit (package, imports, fields, class header, small inner classes) → "outline" chunks
    covered = 0
    outline_parts: list[tuple[int, int]] = []
    for s, e, _, _ in units:
        if s > covered:
            outline_parts.append((covered, s))
        covered = max(covered, e)
    if covered < len(src):
        outline_parts.append((covered, len(src)))
    outline_text = "\n".join(src[s:e].decode("utf-8", "ignore").strip() for s, e in outline_parts if src[s:e].strip())
    outline_text = re.sub(r"\n{3,}", "\n\n", outline_text)
    if len(outline_text) >= MIN_CHARS:
        chunks += split_big("outline", "file outline (imports, fields, class header)", outline_text, 1)
    for s, e, kind, qual in units:
        t = src[s:e].decode("utf-8", "ignore")
        if len(t) < MIN_CHARS:
            continue
        start_line = src[:s].count(b"\n") + 1
        chunks += split_big(kind, qual, t, start_line)
    return chunks


# ---------------------------------------------------------------- SQL
SQL_UNIT_RE = re.compile(
    r"^\s*CREATE\s+(?:OR\s+(?:REPLACE|ALTER)\s+)?(?:PROCEDURE|PROC|FUNCTION|TRIGGER|VIEW|TABLE|INDEX|TYPE)\b"
    r"|^\s*ALTER\s+(?:PROCEDURE|PROC|FUNCTION|TABLE)\b|^\s*GO\s*$|^\s*@\s*$", re.I | re.M)
SQL_NAME_RE = re.compile(r"(?:PROCEDURE|PROC|FUNCTION|TRIGGER|VIEW|TABLE|INDEX|TYPE)\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w\[\]\".$]+)", re.I)


def chunk_sql(text: str) -> list[Chunk]:
    starts = [m.start() for m in SQL_UNIT_RE.finditer(text) if not re.match(r"\s*(GO|@)\s*$", m.group(0), re.I)]
    if not starts:
        return split_big("sql-script", "script", text, 1)
    bounds = starts + [len(text)]
    chunks: list[Chunk] = []
    if starts[0] > 0 and text[:starts[0]].strip():
        chunks += split_big("sql-preamble", "preamble", text[:starts[0]].strip(), 1)
    for s, e in zip(bounds, bounds[1:]):
        body = text[s:e].rstrip()
        body = re.sub(r"\n\s*GO\s*$", "", body, flags=re.I)
        m = SQL_NAME_RE.search(body[:300])
        name = m.group(1).strip('[]"') if m else "statement"
        kind = re.match(r"\s*(?:CREATE|ALTER)\s+(?:OR\s+\w+\s+)?(\w+)", body, re.I)
        kind = ("sql-" + kind.group(1).lower()) if kind else "sql"
        chunks += split_big(kind, name, body, text[:s].count("\n") + 1)
    return chunks


# ---------------------------------------------------------------- MyBatis / XML
MYBATIS_EL_RE = re.compile(r"<(select|insert|update|delete|resultMap|sql)\b[^>]*\bid\s*=\s*\"([^\"]+)\"[^>]*>.*?</\1\s*>", re.S | re.I)
NAMESPACE_RE = re.compile(r"<mapper\b[^>]*namespace\s*=\s*\"([^\"]+)\"", re.I)


def chunk_mybatis(text: str) -> list[Chunk]:
    ns = NAMESPACE_RE.search(text)
    ns = ns.group(1) if ns else ""
    chunks: list[Chunk] = []
    last = 0
    head_end = MYBATIS_EL_RE.search(text)
    head = text[: head_end.start()] if head_end else text
    if len(head.strip()) >= MIN_CHARS:
        chunks += split_big("mybatis-header", f"{ns} (namespace, resultMaps preamble)", head.strip(), 1)
    for m in MYBATIS_EL_RE.finditer(text):
        body = m.group(0)
        name = f"{ns}.{m.group(2)}" if ns else m.group(2)
        pre = f"<!-- MyBatis {m.group(1)} id=\"{m.group(2)}\" namespace=\"{ns}\" -->\n"
        chunks += split_big(f"mybatis-{m.group(1).lower()}", name, pre + body, text[:m.start()].count("\n") + 1)
        last = m.end()
    return chunks or split_big("xml", "file", text, 1)


# ---------------------------------------------------------------- text / markdown
HEADING_RE = re.compile(r"^(#{1,6}\s.+|[A-Z][^\n]{2,80}\n[=-]{3,})$", re.M)


def chunk_markdown(text: str) -> list[Chunk]:
    starts = [m.start() for m in HEADING_RE.finditer(text)]
    if not starts:
        return split_big("text", "document", text, 1)
    bounds = ([0] if starts[0] > 0 else []) + starts + [len(text)]
    out: list[Chunk] = []
    for s, e in zip(bounds, bounds[1:]):
        seg = text[s:e].strip()
        if len(seg) < MIN_CHARS:
            continue
        title = seg.splitlines()[0].lstrip("# ").strip()[:80]
        out += split_big("section", title, seg, text[:s].count("\n") + 1)
    # a short document whose sections are all tiny still deserves one chunk
    return out or ([Chunk("text", "document", 1, text.count("\n") + 1, text.strip(), identifiers(text))]
                   if text.strip() else [])


# ---------------------------------------------------------------- dispatcher
def detect_kind(path: Path, text: str) -> str:
    ext = path.suffix.lower().lstrip(".")
    if ext in ("sql", "prc", "ddl", "sp", "psql", "tsql"):
        return "sql"
    if ext == "xml" and ("<mapper" in text[:3000] and ("mybatis" in text[:3000].lower() or "namespace=" in text[:3000])):
        return "mybatis"
    if ext in TS_LANG:
        return TS_LANG[ext]
    if ext in ("md", "markdown", "adoc", "txt", "rst"):
        return "markdown"
    return "plain"


def chunk_file(path: Path, text: str) -> tuple[str, list[Chunk]]:
    kind = detect_kind(path, text)
    if kind == "sql":
        return kind, chunk_sql(text)
    if kind == "mybatis":
        return kind, chunk_mybatis(text)
    if kind == "markdown":
        return kind, chunk_markdown(text)
    if kind in TS_LANG.values():
        ch = chunk_with_tree_sitter(text, kind)
        if ch:
            return kind, ch
        return kind, split_big("file", path.name, text, 1)
    return kind, split_big("file", path.name, text, 1)
