"""
sink.py — where Phase 2 chunks go.

Both ingesters produce the same shape:

    Document = {
        "source_id":   str,   # stable id: thread id, or repo:path@commit
        "kb":          str,   # knowledge-base name
        "title":       str,   # display name shown in citations
        "rendered":    str,   # full human-readable rendering (markdown) — uploaded as the document
        "metadata":    dict,  # project/year/sender/repo/... → RAGFlow meta_fields (filterable)
        "chunks":      [ {"content": str, "keywords": [str], "questions": [str], "meta": dict}, ... ],
    }

RagflowSink uploads `rendered` as a document (so users can open the whole thread/file
from a citation), stamps metadata, then pushes every chunk through RAGFlow's
"add chunk" API so *our* chunk boundaries — one message, one method, one SQL
procedure — are exactly what gets embedded and retrieved. Parsing is never
triggered for these documents; the chunks are the content.

JsonlSink writes the same documents to a .jsonl file for inspection, for --dry-run,
or for loading into another store (Elasticsearch / pgvector) later.
"""
from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "phase1"))
from ragflow_client import RagflowClient, RagflowError  # noqa: E402


class Manifest:
    """JSONL of processed source_ids so reruns skip finished work."""

    def __init__(self, path: Path):
        self.path = path
        self.done: dict[str, dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self.done[rec["source_id"]] = rec

    def has(self, source_id: str) -> bool:
        return self.done.get(source_id, {}).get("status") == "ok"

    def record(self, rec: dict) -> None:
        self.done[rec["source_id"]] = rec
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class JsonlSink:
    def __init__(self, path: Path):
        self.path = path
        self.f = path.open("a", encoding="utf-8")
        self.docs = self.chunks = 0

    def write(self, doc: dict) -> dict:
        self.f.write(json.dumps(doc, ensure_ascii=False) + "\n")
        self.docs += 1; self.chunks += len(doc["chunks"])
        return {"status": "ok", "sink": "jsonl"}

    def close(self) -> None:
        self.f.close()


class RagflowSink:
    def __init__(self, base_url: str, api_key: str, embedding_model: str, chunk_method: str = "naive"):
        self.client = RagflowClient(base_url, api_key)
        self.embedding_model = embedding_model
        self.chunk_method = chunk_method
        self._kb_ids: dict[str, str] = {}
        self.docs = self.chunks = 0

    def kb_id(self, name: str) -> str:
        if name not in self._kb_ids:
            ds = self.client.get_or_create_dataset(name, self.chunk_method, self.embedding_model,
                                                   {"chunk_token_num": 512, "layout_recognize": False})
            self._kb_ids[name] = ds["id"]
        return self._kb_ids[name]

    def write(self, doc: dict) -> dict:
        ds_id = self.kb_id(doc["kb"])
        # upload the rendering as a .md document without parsing it
        blob = io.BytesIO(doc["rendered"].encode("utf-8"))
        files = [("file", (doc["title"][-200:], blob, "text/markdown"))]
        data = self.client._call("POST", f"/datasets/{ds_id}/documents", files=files, retries=2)
        d = data[0] if isinstance(data, list) else data
        doc_id = d["id"]
        meta = {k: str(v) for k, v in doc["metadata"].items() if v not in (None, "")}
        meta["source_id"] = doc["source_id"]
        try:
            self.client.set_metadata(ds_id, doc_id, meta)
        except RagflowError as e:  # metadata is nice-to-have; chunks are essential
            print(f"   metadata warning {doc['title']}: {e}", file=sys.stderr)
        n = 0
        for ch in doc["chunks"]:
            body = {"content": ch["content"]}
            if ch.get("keywords"):
                body["important_keywords"] = [k for k in ch["keywords"] if k][:30]
            if ch.get("questions"):
                body["questions"] = ch["questions"][:10]
            for attempt in range(3):
                try:
                    self.client._call("POST", f"/datasets/{ds_id}/documents/{doc_id}/chunks", json=body, retries=1)
                    n += 1; break
                except RagflowError as e:
                    if attempt == 2:
                        print(f"   chunk failed {doc['title']}: {e}", file=sys.stderr)
                    time.sleep(1 + attempt)
        self.docs += 1; self.chunks += n
        return {"status": "ok", "sink": "ragflow", "dataset_id": ds_id, "doc_id": doc_id, "chunks": n}

    def close(self) -> None:
        pass


def make_sink(cfg: dict, dry_run: bool, jsonl_path: Path | None):
    if dry_run or jsonl_path:
        return JsonlSink(jsonl_path or Path("phase2_chunks.jsonl"))
    rf = cfg["ragflow"]
    return RagflowSink(rf["base_url"], rf["api_key"], rf["embedding_model"])
