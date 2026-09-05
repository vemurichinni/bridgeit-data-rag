"""
ragflow_client.py — thin wrapper over RAGFlow's HTTP API (v0.2x) used by the Phase 1 scripts.

Only the endpoints we need; every call returns the `data` part of RAGFlow's
{"code": 0, "data": ..., "message": ...} envelope or raises RagflowError.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests


class RagflowError(RuntimeError):
    pass


class RagflowClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 120):
        self.base = base_url.rstrip("/") + "/api/v1"
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {api_key}"
        self.timeout = timeout

    # ---- internal -------------------------------------------------------
    def _call(self, method: str, path: str, retries: int = 3, **kw) -> Any:
        url = self.base + path
        for attempt in range(retries):
            try:
                r = self.s.request(method, url, timeout=self.timeout, **kw)
            except requests.RequestException as e:
                if attempt == retries - 1:
                    raise RagflowError(f"{method} {path}: {e}") from e
                time.sleep(2 ** attempt); continue
            if r.status_code >= 500 and attempt < retries - 1:
                time.sleep(2 ** attempt); continue
            try:
                body = r.json()
            except ValueError:
                raise RagflowError(f"{method} {path}: HTTP {r.status_code} non-JSON: {r.text[:200]}")
            if body.get("code", 0) != 0:
                raise RagflowError(f"{method} {path}: code={body.get('code')} {body.get('message')}")
            return body.get("data")
        raise RagflowError(f"{method} {path}: retries exhausted")

    # ---- datasets -------------------------------------------------------
    def list_datasets(self) -> list[dict]:
        out, page = [], 1
        while True:
            data = self._call("GET", "/datasets", params={"page": page, "page_size": 100}) or []
            out.extend(data)
            if len(data) < 100:
                return out
            page += 1

    def get_or_create_dataset(self, name: str, chunk_method: str, embedding_model: str,
                              parser_config: dict | None = None, description: str = "") -> dict:
        for d in self.list_datasets():
            if d["name"] == name:
                return d
        body = {"name": name, "chunk_method": chunk_method, "embedding_model": embedding_model,
                "description": description or f"BridgeIT-Data archive — {name}"}
        if parser_config:
            body["parser_config"] = parser_config
        return self._call("POST", "/datasets", json=body)

    # ---- documents ------------------------------------------------------
    def upload(self, dataset_id: str, path: Path, display_name: str | None = None) -> dict:
        with path.open("rb") as f:
            files = [("file", (display_name or path.name, f))]
            data = self._call("POST", f"/datasets/{dataset_id}/documents", files=files, retries=2)
        return data[0] if isinstance(data, list) else data

    def set_metadata(self, dataset_id: str, doc_id: str, meta: dict) -> None:
        self._call("PATCH", f"/datasets/{dataset_id}/documents/{doc_id}", json={"meta_fields": meta})

    def parse(self, dataset_id: str, doc_ids: list[str]) -> None:
        for i in range(0, len(doc_ids), 50):
            self._call("POST", f"/datasets/{dataset_id}/chunks", json={"document_ids": doc_ids[i:i + 50]})

    def list_documents(self, dataset_id: str, keywords: str | None = None) -> list[dict]:
        out, page = [], 1
        while True:
            params = {"page": page, "page_size": 100}
            if keywords:
                params["keywords"] = keywords
            data = self._call("GET", f"/datasets/{dataset_id}/documents", params=params) or {}
            docs = data.get("docs", data if isinstance(data, list) else [])
            out.extend(docs)
            if len(docs) < 100:
                return out
            page += 1

    def parse_status(self, dataset_id: str) -> dict:
        """Counts by run status: UNSTART / RUNNING / DONE / FAIL / CANCEL."""
        counts: dict[str, int] = {}
        for d in self.list_documents(dataset_id):
            counts[d.get("run", "?")] = counts.get(d.get("run", "?"), 0) + 1
        return counts

    # ---- retrieval ------------------------------------------------------
    def retrieve(self, question: str, dataset_ids: list[str], page_size: int = 5,
                 similarity_threshold: float = 0.1, vector_similarity_weight: float = 0.3,
                 top_k: int = 1024, rerank_id: str | None = None, keyword: bool = True,
                 highlight: bool = False, metadata_condition: dict | None = None,
                 document_ids: list[str] | None = None) -> dict:
        body: dict[str, Any] = {
            "question": question, "dataset_ids": dataset_ids, "page": 1, "page_size": page_size,
            "similarity_threshold": similarity_threshold, "vector_similarity_weight": vector_similarity_weight,
            "top_k": top_k, "keyword": keyword, "highlight": highlight,
        }
        if rerank_id:
            body["rerank_id"] = rerank_id
        if metadata_condition:
            body["metadata_condition"] = metadata_condition
        if document_ids:
            body["document_ids"] = document_ids
        return self._call("POST", "/retrieval", json=body) or {}
