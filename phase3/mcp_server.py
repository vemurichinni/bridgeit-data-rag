#!/usr/bin/env python3
"""
mcp_server.py — expose the archive's hybrid retrieval as an MCP server for IDE agents.

This is the "Expose retrieval as an MCP server so Copilot/Claude Code can query the
archive from the IDE" item from docs/RAG-System-Recommendation.md section 7. It is a
thin stdio wrapper over the same RagflowClient used by phase1/run_eval.py — no new
retrieval logic, just tools an agent can call:

  list_knowledge_bases()                         — every KB with doc/chunk counts
  search_archive(question, kb, project, top_k)   — hybrid BM25+vector retrieve, reranked
  list_documents(kb, keywords, limit)            — browse a KB's documents by name

Every search_archive hit includes its document title and metadata (source_path /
thread_id / repo+path / etc., whatever the KB stamped) so a citation is always
traceable back to the original file, thread or commit — never a paraphrase.

Setup
  pip install -r requirements.txt   # adds the `mcp` SDK (Phase 3 section)
  export BRIDGEIT_RAG_CONFIG=/path/to/phase1/config.local.yaml   # else phase1/config.local.yaml is used

Register with an MCP-capable client (Claude Code's .mcp.json, VS Code Copilot's
mcp.json, etc.) as a stdio server:
  {
    "mcpServers": {
      "bridgeit-archive": {
        "command": "python3",
        "args": ["/path/to/bridgeit-data-rag/phase3/mcp_server.py"],
        "env": {"BRIDGEIT_RAG_CONFIG": "/path/to/phase1/config.local.yaml"}
      }
    }
  }

Access control note: RAGFlow's OSS API has no per-caller ACL, so every KB this server's
RAGFlow API key can see is queryable by anyone who can reach this process. Restrict the
API key to a team-scoped RAGFlow account, or filter `kb` server-side (see the ALLOWED_KB
pattern below) before deploying this where untrusted users can invoke it.
"""
from __future__ import annotations

import fnmatch
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "phase1"))
from ragflow_client import RagflowClient, RagflowError  # noqa: E402

try:
    from mcp.server.fastmcp import FastMCP  # mcp SDK 1.x
except ModuleNotFoundError:
    from mcp.server.mcpserver import MCPServer as FastMCP  # mcp SDK 2.x — FastMCP renamed

CONFIG_ENV = "BRIDGEIT_RAG_CONFIG"
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "phase1" / "config.local.yaml"
# Optional hard allow-list of KB name globs this server will ever query, regardless of
# what the API key can see — set via BRIDGEIT_RAG_ALLOWED_KB="bt-*,-bt-hr-*" (comma list,
# a leading "-" excludes). Empty = everything the API key can see (no extra restriction).
ALLOWED_KB = [p.strip() for p in os.environ.get("BRIDGEIT_RAG_ALLOWED_KB", "").split(",") if p.strip()]

CONTENT_CHARS = 1500  # keep tool output small enough for an agent's context window


def _load_cfg() -> dict:
    path = Path(os.environ.get(CONFIG_ENV, DEFAULT_CONFIG))
    if not path.exists():
        raise RuntimeError(f"config not found: {path} (set {CONFIG_ENV} or create phase1/config.local.yaml)")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


_cfg = _load_cfg()
_rf = _cfg["ragflow"]
_client = RagflowClient(_rf["base_url"], _rf["api_key"])
_prefix = _cfg.get("knowledge_bases", {}).get("name_prefix", "")

mcp = FastMCP("bridgeit-archive")


def _allowed(name: str) -> bool:
    if not ALLOWED_KB:
        return True
    verdict = False
    for pat in ALLOWED_KB:
        neg = pat.startswith("-")
        p = pat[1:] if neg else pat
        if fnmatch.fnmatch(name, p):
            verdict = not neg
    return verdict


def _known_datasets() -> list[dict]:
    return [d for d in _client.list_datasets() if d["name"].startswith(_prefix) and _allowed(d["name"])]


@mcp.tool()
def list_knowledge_bases() -> list[dict]:
    """List every archive knowledge base this server can query, with size and chunk counts."""
    return [
        {
            "name": d["name"],
            "id": d["id"],
            "description": d.get("description", ""),
            "document_count": d.get("document_count"),
            "chunk_count": d.get("chunk_count"),
            "chunk_method": d.get("chunk_method"),
        }
        for d in sorted(_known_datasets(), key=lambda d: d["name"])
    ]


@mcp.tool()
def search_archive(
    question: str,
    kb: list[str] | None = None,
    project: str | None = None,
    top_k: int = 5,
    use_rerank: bool = True,
) -> list[dict]:
    """Hybrid BM25+vector search over the BridgeIT-Data archive (docs, email threads, code, commits).

    Args:
        question: natural-language question or exact identifier (invoice #, class name, error string).
        kb: knowledge base names to search (from list_knowledge_bases); omit to search every KB
            this server is allowed to see.
        project: optional metadata filter — matches the 'project' field stamped at ingest time.
        top_k: number of ranked chunks to return (RAGFlow reranks internally over a larger pool).
        use_rerank: apply the configured cross-encoder reranker (config.yaml's rerank_model);
            turn off only to compare raw hybrid ranking.

    Returns a list of hits, each with the source document title, the verbatim chunk text
    (never paraphrased), a similarity score, and whatever metadata the ingester stamped
    (source_path, thread_id/subject, repo/path/kind, commit, etc.) so the citation is
    traceable back to the original file, thread or commit.
    """
    datasets = _known_datasets()
    if kb:
        wanted = set(kb)
        datasets = [d for d in datasets if d["name"] in wanted]
    if not datasets:
        return []
    ds_ids = [d["id"] for d in datasets]
    id_to_name = {d["id"]: d["name"] for d in datasets}
    ev = _cfg.get("eval", {})
    rerank_id = _rf.get("rerank_model") if use_rerank and _rf.get("rerank_model") else None
    metadata_condition = None
    if project:
        metadata_condition = {"logic": "and", "conditions": [
            {"name": "project", "comparison_operator": "contains", "value": project}
        ]}
    try:
        data = _client.retrieve(
            question, ds_ids, page_size=top_k,
            similarity_threshold=ev.get("similarity_threshold", 0.1),
            vector_similarity_weight=ev.get("vector_similarity_weight", 0.3),
            top_k=ev.get("top_k", 1024), rerank_id=rerank_id, keyword=ev.get("keyword", True),
            metadata_condition=metadata_condition,
        )
    except RagflowError as e:
        return [{"error": str(e)}]
    hits = []
    for c in data.get("chunks", []):
        content = c.get("content", "")
        hits.append({
            "document": c.get("document_keyword") or c.get("docnm_kwd") or "",
            "kb": id_to_name.get(c.get("dataset_id", ""), ""),
            "score": c.get("similarity"),
            "content": content[:CONTENT_CHARS] + ("…" if len(content) > CONTENT_CHARS else ""),
            "metadata": {k: v for k, v in c.items()
                        if k not in {"content", "similarity", "highlight", "document_keyword", "docnm_kwd", "vector"}},
        })
    return hits


@mcp.tool()
def list_documents(kb: str, keywords: str | None = None, limit: int = 20) -> list[dict]:
    """List documents in one knowledge base by name, optionally filtered by keyword substring."""
    datasets = {d["name"]: d for d in _known_datasets()}
    ds = datasets.get(kb)
    if not ds:
        return [{"error": f"unknown or disallowed knowledge base: {kb}"}]
    docs = _client.list_documents(ds["id"], keywords=keywords)[:limit]
    return [{"id": d.get("id"), "name": d.get("name"), "run_status": d.get("run"),
             "chunk_count": d.get("chunk_count")} for d in docs]


if __name__ == "__main__":
    mcp.run()
