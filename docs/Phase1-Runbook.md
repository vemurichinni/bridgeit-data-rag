# BridgeIT-Data — Phase 1 runbook: RAGFlow stand-up and document load

*2026-09-05. Phase 1 turns the Phase 0 census into a working, measured retrieval system for the document part of the archive (PDF, Word, Excel, PowerPoint, text, single `.eml` files). Email threads from mbox and source code are Phase 2 — they go through the custom ingestion pipeline, not this loader.*

## What is in this kit

```
phase1/
├── config.yaml          all knobs: RAGFlow URL/key, embedding + reranker names, KB layout rules,
│                        chunking per family, file filters, evaluation defaults
├── ragflow_client.py    thin wrapper over RAGFlow's HTTP API (datasets, upload, metadata, parse, retrieve)
├── load_documents.py    bulk loader driven by census_out/files.csv — dedupes by hash, stamps metadata,
│                        resumable via load_manifest.jsonl, --dry-run / --samples-only / --status
├── run_eval.py          scores the evaluation set against RAGFlow and writes a run block into
│                        evaluation_set.xlsx (Recall@5, MRR, exact-match), plus a CSV per run
└── PHASE1-RUNBOOK.md    this file
```

Requirements on the machine that runs the scripts: Python 3.10+, `pip install requests pyyaml openpyxl`, network access to the RAGFlow host and read access to the archive paths recorded in `files.csv`.

## Step 1 — host and install RAGFlow (day 1)

RAGFlow's stated floor is 4 cores / 16 GB RAM / 50 GB disk; for this corpus take the sizing from `CENSUS_REPORT.md` and give the host 32–64 GB and SSD of at least twice the estimated index size. A Linux VM (or WSL2/Docker Desktop for a first trial) with Docker ≥ 24 and Docker Compose ≥ 2.26.

```bash
sudo sysctl -w vm.max_map_count=262144           # Elasticsearch requirement; persist it in /etc/sysctl.conf
git clone https://github.com/infiniflow/ragflow.git
cd ragflow/docker
git checkout v0.26.4                             # pin the release; the recommendation doc was written against 0.26.x
# optional: edit .env — DOC_ENGINE stays elasticsearch (that is the full-text + vector store we want)
docker compose -f docker-compose.yml up -d
docker logs -f docker-ragflow-cpu-1              # wait for "Running on all addresses (0.0.0.0)"
```

Open `http://<host>` (port 80), register the first account (it becomes admin), then **avatar → API → API Key → Create** and put the key into `config.local.yaml`.

Ports to expose internally only: 80 (UI/API), 9380 (API direct), 1200/9200 (Elasticsearch — keep firewalled). Back up the `docker/` volumes (`esdata01`, `minio_data`, `mysql_data`, `redis_data`) before any upgrade.

## Step 2 — models (day 1)

Self-hosted default: run [Ollama](https://ollama.com) on the RAGFlow host or a GPU box, `ollama pull bge-m3`, and add it in RAGFlow under **Model providers → Ollama → embedding model `bge-m3`** with the Ollama base URL (`http://host.docker.internal:11434` from inside Docker, or the host's IP). For the reranker, run `bge-reranker-v2-m3` through Xinference or any OpenAI-compatible rerank endpoint and add it as a rerank model; if that is a day-1 hassle, skip reranking for the baseline run and add it as the second experiment — the evaluation harness has a flag for exactly that comparison. Chat model: whatever you already have access to (an OpenAI-compatible endpoint, Claude, or a local Qwen/Llama via Ollama); it only affects answers, not retrieval, and every metric in Phase 1 is a retrieval metric.

Set the model names in `config.yaml` exactly as RAGFlow displays them (`bge-m3:latest@Ollama` style — the suffix is the provider). Set the same embedding model as the system default in **System model settings** so KBs created from the UI match the ones the loader creates.

## Step 3 — knowledge-base layout (decide before loading)

The loader creates KBs from `files.csv` using the rules in `config.yaml`:

| `strategy` | result | use when |
|---|---|---|
| `top_folder` (default) | one KB per first-level folder under each census root — i.e. per project | the archive is already organised by project/client, which is what the metadata filter `project` and the answer citations need |
| `root` | one KB per census root | roots are already per domain |
| `single` | one KB | fewer than ~5,000 files, or you want to test cross-project search first |

Two rules are fixed regardless of strategy, because RAGFlow chunk methods are per KB: spreadsheets go to `<kb>-tables` (chunk method `table`, one chunk per row, headers carried into every row — this is what makes "what rate did we agree for tier 2" answerable verbatim), and `.eml` files go to `<kb>-mail` (chunk method `email`). Everything else uses `naive` with DeepDoc layout recognition, 512-token chunks and parent-child enabled, so small chunks match and the enclosing section is what gets returned.

Every document is stamped with `project, year, ext, family, source_path, sha256, root` as metadata, which is what query-time `metadata_condition` filters use (`--filter-by-project` in the harness shows the effect). The display name keeps the relative path (`specs__AS400_OrderSync_IF_v3.pdf`) so two files with the same name in different folders stay distinguishable in citations.

Run `python load_documents.py --config config.local.yaml --census files.csv --dry-run` and read the KB list it prints; if a project folder produces a KB with 20,000 files, split it by subfolder with `--only` runs into differently named prefixes, or accept it — RAGFlow copes, the UI just gets slower to browse.

## Step 4 — load the samples first (day 2)

```bash
python load_documents.py --config config.local.yaml --census ../phase0/census_out/files.csv \
       --samples-only ../phase0/census_out/samples.csv
python load_documents.py --config config.local.yaml --status
```

Open each sample KB in the UI, click a document, and look at the chunks. This is the parser bake-off from Phase 0, sheet **Parser tests**: did the table on page 12 come through as a table, is the scanned PDF OCR'd, is the text verbatim. Where RAGFlow's DeepDoc loses (typically complex Excel and badly scanned pages), the fix is to convert those files with Docling to Markdown first (`docling file.pdf --to md`) and load the Markdown — the loader takes any path in `files.csv`, so add a converted folder to the census and rerun. Adjust `chunk_token_num`, `layout_recognize`, `html4excel` in `config.yaml` as you learn; changing a KB's chunk config in the UI and re-parsing is also fine.

## Step 5 — baseline measurement, then bulk load (day 2–3)

With samples loaded and at least the sample-related questions in `evaluation_set.xlsx`, take a baseline:

```bash
python run_eval.py --config config.local.yaml --xlsx ../phase0/evaluation_set.xlsx --label "baseline samples"
```

Then the full load. Start it in a `tmux`/`screen` session; it is resumable (`load_manifest.jsonl` records every path, so rerunning skips what is done and retries failures):

```bash
python load_documents.py --config config.local.yaml --census ../phase0/census_out/files.csv
python load_documents.py --config config.local.yaml --status      # UNSTART / RUNNING / DONE / FAIL per KB
```

Throughput is parse-bound, not upload-bound: DeepDoc with layout recognition on CPU manages roughly 1–3 PDF pages per second per worker; a 100k-page corpus is a day or two on CPU and hours on a GPU (`DEVICE=gpu` in `.env` before `docker compose up`). Watch `docker stats` — if Elasticsearch is memory-starved, raise `MEM_LIMIT` in `.env`. Failed parses (`FAIL`) are listed per document in the UI with the reason; the usual ones are encrypted PDFs and corrupt legacy Office files that the census already flagged.

## Step 6 — tune retrieval against the evaluation set (day 3–5)

Each experiment is one `run_eval.py` invocation with a new `--label`; each writes a fresh block into sheet **Runs** and a CSV with the top-5 documents and the top chunk per question, so a miss can be diagnosed in seconds.

| experiment | flags | what it tells you |
|---|---|---|
| baseline | `--label baseline` | hybrid search, keyword boost on, no reranker |
| reranker | `--label "hybrid+rerank" --rerank` | usually the single biggest gain on exact-ID and table questions |
| semantic-heavy | `--vector-weight 0.7` | if this *beats* baseline on Exact-ID questions something is wrong with tokenisation of your identifiers |
| keyword-heavy | `--vector-weight 0.1` | expect exact-match to rise, semantic questions to fall |
| project filter | `--filter-by-project` | the ceiling when the user tells you which project; shows how much noise the 17-year corpus adds |

The number that matters is **Exact** (the expected snippet appears verbatim in a top-5 chunk); Recall@5 tells you whether the right document was found, and the gap between the two tells you whether chunking is cutting answers in half. Typical Phase 1 targets on a 50-question set: Recall@5 ≥ 85%, Exact ≥ 70%. Below that on a specific query type, fix the source of that type (chunk method for tables, OCR for scans, `chunk_token_num` for long specs) rather than the global weights.

## Step 7 — hand-over to users and Phase 2

Create a **Chat assistant** in the UI over the project KBs with "Show citation" on and the similarity threshold from your best run, and give it to two or three colleagues with the instruction to report every wrong or missing answer — those become new rows in the evaluation set. Phase 2 (mbox threads, Git repos, stored procedures via the custom pipeline) starts once the document baseline is stable, and it writes into the same Elasticsearch cluster and the same evaluation workbook.

## Exit criteria for Phase 1

All census document families loaded with `FAIL` under 2% and every failure explained; the evaluation set at ≥ 50 questions with at least three scored runs in sheet **Runs**; a written note of the chosen `vector_similarity_weight`, reranker on/off and per-family chunk settings; a chat assistant in use by real colleagues.
