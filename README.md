# BridgeIT-Data RAG

Company-wide retrieval system over the project archive since 2009 — documents, Gmail, spreadsheets and source code — with exact, citable retrieval.

Design: **RAGFlow** as the engine (deep PDF/Excel parsing, hybrid BM25 + vector search on Elasticsearch, grounded citations) plus a small custom ingestion pipeline for email threads and code. Full rationale in [docs/RAG-System-Recommendation.md](docs/RAG-System-Recommendation.md).

## Layout

| path | what |
|---|---|
| `docs/` | architecture recommendation and the per-phase runbooks |
| `phase0/census/` | inventory scripts: documents (`census_files.py`), Gmail mbox (`census_mbox.py`), Git repos (`census_git.py`), merged report with sizing (`census_report.py`) |
| `phase0/evaluation_set.xlsx` | the gold question set — Questions, Coverage, Runs (Recall@5 / MRR / exact-match), Parser tests |
| `phase1/` | RAGFlow bulk loader (`load_documents.py`), retrieval evaluation harness (`run_eval.py`), API wrapper, `config.yaml` |
| `phase2/` | email and code ingesters: `ingest_mbox.py` (threads, quote-stripping, attachments), `ingest_code.py` (tree-sitter AST chunks, SQL procs, MyBatis, commit history), shared `sink.py` and `chunkers/` |
| `phase3/` | hardening: `incremental_sync.py` (nightly code+mail sync), `gmail_sync.py` (Gmail API incremental pull), `access_control.py` (per-KB permission policy), `enable_graphrag.py` (RAPTOR/GraphRAG toggle), `mcp_server.py` (retrieval as an MCP server for IDE agents), `hooks/post-merge` |
| `deploy/` | `docker-compose.ollama.yml` — local model serving (embedding + chat) for the fully-local deployment profile |

## Quick start

```bash
pip install -r requirements.txt

# Step 0 — stand up RAGFlow (its own repo/compose — see docs/Phase1-Runbook.md) and,
# for a fully-local deployment, local models alongside it (see docs/Deployment-Options.md):
docker compose -f deploy/docker-compose.ollama.yml up -d
docker exec bridgeit-ollama ollama pull bge-m3
docker exec bridgeit-ollama ollama pull qwen2.5:14b-instruct   # optional local generation model
cp phase1/config.yaml phase1/config.local.yaml     # fill in base_url, api_key, model names

# Phase 0 — run where the archive is visible (an attached disk works fine)
python phase0/census/census_files.py /path/to/archive --out census_out
python phase0/census/census_mbox.py  /path/to/Takeout/Mail --out census_out
python phase0/census/census_git.py   /path/to/repos --out census_out
python phase0/census/census_report.py --out census_out --gpu

# Phase 1 — bulk-load documents, then measure (see docs/Phase1-Runbook.md)
python phase1/load_documents.py --config phase1/config.local.yaml --census census_out/files.csv --dry-run
python phase1/load_documents.py --config phase1/config.local.yaml --census census_out/files.csv
python phase1/run_eval.py --config phase1/config.local.yaml --xlsx phase0/evaluation_set.xlsx --label baseline

# Phase 2 — email threads and source code, straight into RAGFlow (see docs/Phase2-Runbook.md)
python phase2/ingest_mbox.py --config phase1/config.local.yaml ~/Takeout/Mail/*.mbox \
       --account projects@bridgeit.com --attachments-dir /data/mail-attachments
python phase2/ingest_code.py --config phase1/config.local.yaml --repos-csv census_out/repos.csv

# Phase 2 alternate — prepare offline (no RAGFlow reachable), load from wherever it runs:
python phase2/ingest_code.py --config phase1/config.local.yaml /mnt/archive/some-repo \
       --dry-run --jsonl code.jsonl
python phase2/load_jsonl.py --config phase1/config.local.yaml code.jsonl   # run where RAGFlow is reachable

# Phase 3 — hardening: incremental sync, access control, MCP, GraphRAG (see docs/Phase3-Runbook.md)
python phase3/incremental_sync.py --config phase1/config.local.yaml --repos-csv census_out/repos.csv \
       --mail-account projects@bridgeit.com --log phase3/sync.log
python phase3/access_control.py --config phase1/config.local.yaml --policy phase3/access_policy.local.yaml --status
BRIDGEIT_RAG_CONFIG=phase1/config.local.yaml python phase3/mcp_server.py
```

## Phases

0. **Census + evaluation set** — know the corpus, pick the worst files, write the 50 questions. `docs/Phase0-Runbook.md`
1. **RAGFlow stand-up + document load** — KB layout, sample bake-off, bulk load, tune against the eval set. `docs/Phase1-Runbook.md`
2. **Email threads + code** — mbox ingester with thread grouping, quote-stripping and attachment extraction; tree-sitter chunker for Java / TypeScript, plus SQL procedures, MyBatis statements and commit history. `docs/Phase2-Runbook.md`
3. **Hardening** — incremental sync (nightly + git hooks + Gmail API), per-KB access control, an MCP endpoint for IDE agents, GraphRAG/RAPTOR for cross-project questions. `docs/Phase3-Runbook.md`

Every model in the stack (embedding, reranker, the generation LLM that writes answers)
can be run fully local or swapped for a cloud provider independently, and RAGFlow itself
can run on your own machine or a rented VM — see `docs/Deployment-Options.md` for the
local/cloud profiles and how to switch between them.

`config.local.yaml`, census outputs, load manifests, and Phase 3's Gmail OAuth tokens/credentials, sync state, logs and local access policy are git-ignored: they contain API keys, tokens and archive paths.
