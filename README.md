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

## Quick start

```bash
pip install -r requirements.txt

# Phase 0 — run where the archive is visible
python phase0/census/census_files.py /path/to/archive --out census_out
python phase0/census/census_mbox.py  /path/to/Takeout/Mail --out census_out
python phase0/census/census_git.py   /path/to/repos --out census_out
python phase0/census/census_report.py --out census_out --gpu

# Phase 1 — after RAGFlow is up (see docs/Phase1-Runbook.md)
cp phase1/config.yaml phase1/config.local.yaml     # fill in base_url, api_key, model names
python phase1/load_documents.py --config phase1/config.local.yaml --census census_out/files.csv --dry-run
python phase1/load_documents.py --config phase1/config.local.yaml --census census_out/files.csv
python phase1/run_eval.py --config phase1/config.local.yaml --xlsx phase0/evaluation_set.xlsx --label baseline

# Phase 2 — email threads and source code (see docs/Phase2-Runbook.md)
python phase2/ingest_mbox.py --config phase1/config.local.yaml ~/Takeout/Mail/*.mbox \
       --account projects@bridgeit.com --attachments-dir /data/mail-attachments
python phase2/ingest_code.py --config phase1/config.local.yaml --repos-csv census_out/repos.csv
```

## Phases

0. **Census + evaluation set** — know the corpus, pick the worst files, write the 50 questions. `docs/Phase0-Runbook.md`
1. **RAGFlow stand-up + document load** — KB layout, sample bake-off, bulk load, tune against the eval set. `docs/Phase1-Runbook.md`
2. **Email threads + code** — mbox ingester with thread grouping, quote-stripping and attachment extraction; tree-sitter chunker for Java / TypeScript, plus SQL procedures, MyBatis statements and commit history. `docs/Phase2-Runbook.md`
3. **Hardening** — incremental sync, access control, MCP endpoint for IDE agents, GraphRAG for cross-project questions.

`config.local.yaml`, census outputs and load manifests are git-ignored: they contain API keys and archive paths.
