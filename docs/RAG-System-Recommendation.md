# BridgeIT-Data — RAG System Recommendation

*Prepared 2026-09-05. Scope: one retrieval system over everything the company has produced since 2009 — Gmail mail (with attachments), PDFs, Word/Excel files, and the source code of every application — with exact, citable retrieval.*

---

## 1. Verdict in one paragraph

The `rag-cookbook-2026` repo you pointed at is a good **textbook**, not a **system**. It is 40 Jupyter notebooks (Apache-2.0, ~27 stars) that each teach one technique — late chunking, contextual retrieval, hybrid BM25+dense, ColBERT, RAPTOR, GraphRAG, RAGAS evaluation — on four toy documents with an in-memory Qdrant. Nothing in it ingests Gmail, walks a Git repo, or runs as a service. Use it as the reference for *which* techniques to switch on; do not try to build the archive on top of it. What actually fits your requirements is a **two-layer design**: a self-hosted **RAGFlow** instance as the ready-made engine for documents and email (deep PDF/Excel parsing, hybrid search, grounded citations, web UI, out of the box), plus a small **custom ingestion pipeline** that you own — Docling for hard files, an mbox/Gmail-API loader for mail, and tree-sitter AST chunking for code — feeding a single **Elasticsearch/OpenSearch index that stores full text alongside vectors**. Full text next to vectors is the non-negotiable part: "exact content retrieval" is a BM25/keyword problem as much as an embedding problem, and any system that only stores vectors will fail you on invoice numbers, class names, error strings and SQL identifiers.

## 2. What "exact content retrieval" actually demands

Semantic (dense-vector) search finds things that *mean* the same thing. Your archive is full of things that must be found by *literal* match: a project code, a customer name spelled one way, a stored-procedure name, a Java exception, a cell value in a 2014 Excel sheet, a subject line. Nearly every failed corporate RAG project fails here — vectors return "something similar" and the user wanted "that one".

So the system must have, at minimum:

1. **Hybrid retrieval** — BM25 keyword index and dense vector index queried together, fused with Reciprocal Rank Fusion, then re-ranked by a cross-encoder. This is the single highest-value technique in the cookbook (recipe "hybrid dense+BM25") and is what RAGFlow, Onyx and Haystack all ship by default.
2. **Verbatim chunk storage with provenance** — every chunk keeps the original text unchanged plus `source_path`, `page`/`sheet`/`row`, `line range`, `commit`, `message-id`, `thread-id`, `date`, `project`. The answer layer must quote the chunk and show the citation; it must never paraphrase from memory.
3. **Metadata filtering before ranking** — "only project X", "only 2015–2017", "only *.java", "only mail from customer Y". Without this, a 17-year corpus drowns every query in noise.
4. **Structure-aware chunking** — tables stay tables (Excel rows as key:value text with sheet + row id), code is split at method/class boundaries, email is chunked per message with the thread kept as a unit. Fixed 512-token windows destroy exactly the things you want to find.
5. **Lossless originals** — keep the source file (or a rendered PDF/Markdown of it) in object storage and link every chunk back to it, so a user can open the actual document after the answer.

## 3. Candidates reviewed

| System | What it is | Fits your case | Gaps |
|---|---|---|---|
| **rag-cookbook-2026** (FareedKhan-dev) | 40 educational notebooks; Qdrant, LiteLLM, LangGraph, RAGAS | Reference for techniques and for building an eval set | No ingestion connectors, no service, no UI, toy corpus, tiny community |
| **RAGFlow** (infiniflow) | Full open-source RAG engine, Apache-2.0, ~88k stars, v0.26.x, Docker | Deep document understanding (layout, tables, scanned PDFs), Word/Excel/PPT/images, template-based chunking with visual inspection, hybrid search on Elasticsearch, grounded citations, agent/MCP, multi-tenant knowledge bases | Has an `Email` chunk method for single `.eml` files and a `Table` method for Excel, but no mbox/thread grouping or quote-stripping; code is plain text to it; needs 16 GB RAM/4 cores minimum; you customise via its API, not by editing its internals |
| **Onyx** (ex-Danswer) | Open-source enterprise search + chat, MIT community edition, ~32k stars, v3.x | 50+ connectors (Gmail, Google Drive, GitHub, file upload), hybrid index, citations, permissions, Docker/K8s | Document parsing is generic — weaker than RAGFlow/Docling on complex PDFs and Excel; connector-centric, less control over chunking |
| **Docling** (IBM) | Parsing library, MIT | Best-in-class PDF layout + table extraction, OCR, DOCX/XLSX/PPTX/HTML/EML/MSG/images, exports Markdown/JSON, integrates with LangChain/LlamaIndex/Haystack, runs as `docling-serve` API | Only parsing — no index, no retrieval |
| **Haystack / LlamaIndex / LangChain** | Python frameworks | Maximum control; every technique in the cookbook is available as a component | You build and operate everything; no UI; best when you already have a team owning Python services |
| **LightRAG / GraphRAG** | Graph-augmented RAG | Good for "how does X relate to Y across projects" questions | Expensive to build (LLM entity extraction over the whole corpus), weaker on exact-match lookups; add later for cross-project reasoning, not as the base |
| **Spring AI + pgvector** | Java-native RAG (ETL pipeline, VectorStore abstraction; Docling reader available via the Arconia add-on) | Natural for your team; can be the *query/serving* layer inside an existing Spring Boot app | Ingestion ecosystem (parsers, OCR, tree-sitter, mbox) is much richer in Python; hybrid search needs extra work (ParadeDB or manual BM25) |
| **RAG-Mail** (ManiAm) | Small project: Gmail API/mbox → thread-aware chunks → Qdrant + Postgres | Proves the email pattern: group by thread, re-embed the thread on new mail, extract attachments (PDF/DOCX/EML/ZIP/images with OCR) | Personal-scale (17 stars); borrow the design, not the code |

## 4. Recommended architecture

```
                 ┌──────────────── INGESTION (Python, you own it) ────────────────┐
 Gmail Takeout   │  mbox/Gmail-API loader → per-thread docs → attachments split   │
 (.mbox)         │                                                                │
 PDF/DOCX/XLSX   │  Docling (layout, tables, OCR) → Markdown/JSON + page/sheet ids │
 PPTX/images     │                                                                │
 Git repos       │  tree-sitter AST chunker (Java, SQL, TS/Angular, XML mappers) │
                 │  + README/ADR/docs as text; commit + path + line metadata      │
 SQL Server/DB2  │  DDL, stored procs, MyBatis XML as code chunks                 │
                 └──────────────────────────┬─────────────────────────────────────┘
                                            │ normalized chunk = {text, embedding, metadata, source_uri}
                                            ▼
      ┌──────────── STORE ────────────┐    ┌──────────── ORIGINALS ────────────┐
      │ Elasticsearch / OpenSearch    │    │ MinIO / S3 (or NAS): the file      │
      │ • BM25 full-text field        │    │ itself + rendered Markdown         │
      │ • dense_vector field (kNN)    │    └───────────────────────────────────┘
      │ • keyword fields for filters  │
      └───────────────┬───────────────┘
                      ▼
      ┌──────────── RETRIEVAL ───────────┐
      │ metadata filter → BM25 + kNN →   │
      │ RRF fusion → cross-encoder rerank│
      │ (bge-reranker-v2-m3) → top-k     │
      └───────────────┬──────────────────┘
                      ▼
      ┌──────────── ANSWER / UI ─────────┐
      │ RAGFlow chat UI + API (citations) │
      │ or Spring Boot service using      │
      │ Spring AI for internal apps       │
      └───────────────────────────────────┘
```

### Why RAGFlow as the engine

It is the only mature open-source system that already does deep parsing of the document types you have (including scanned PDFs and multi-sheet Excel), lets a human *see* how each file was chunked and fix the template per knowledge base, runs hybrid search on Elasticsearch, and returns answers with clickable chunk-level citations. That covers the bulk of a 2009–2026 document archive on day one. Its default store is Elasticsearch, which is precisely the full-text-plus-vector store you need, and you can point your own ingestion at the same cluster.

### Why a custom pipeline for email and code

Neither RAGFlow nor Onyx chunks email threads or source code the way exact retrieval needs (RAGFlow's `Email` method takes one `.eml` at a time and its `Table` method handles Excel well, so the gap is upstream of it). Email needs thread grouping, header metadata (from/to/date/subject/message-id) as filterable fields, and attachments peeled off and parsed as their own documents but linked back to the message. Code needs AST-boundary chunks (a whole method, a whole MyBatis `<select>`, a whole stored procedure) with file path and line numbers, plus an "expanded context" step that pulls the enclosing class or the imports at query time. Both are a few hundred lines of Python with Docling, `mailbox`, and `tree-sitter-languages`, and they write into the same Elasticsearch index through RAGFlow's document API (or directly, if you keep a separate index for code).

### Store: one Elasticsearch/OpenSearch cluster

Start with the Elasticsearch that RAGFlow provisions. If you later want a single Postgres for everything, ParadeDB (BM25 + pgvector in Postgres) is the credible alternative and works with Spring AI. Avoid pure vector stores (Chroma, plain Qdrant) as the *primary* store — you would have to bolt BM25 on separately.

### Models (all self-hostable)

Embeddings: **BGE-M3** (multilingual, 8k context, dense+sparse in one model — also used in the cookbook) or **Qwen3-Embedding**; for code chunks, the same model is acceptable, or **jina-code-embeddings** if code queries dominate. Reranker: **bge-reranker-v2-m3**. Generation: any OpenAI-compatible endpoint — local (vLLM/Ollama with Qwen3 or Llama) or hosted (Claude via API). Keep the LLM swappable; the value is in the index.

## 5. Techniques from the cookbook worth switching on (and when)

| Cookbook recipe | Use it? | Where |
|---|---|---|
| Hybrid dense + BM25, cross-encoder rerank | **Yes, day one** | Retrieval layer (RAGFlow does this) |
| Metadata-filtered auto-retrieval | **Yes, day one** | Project / year / type / sender filters |
| Contextual retrieval (prepend doc summary to each chunk) | **Yes** | Big win for email and code where the chunk alone is ambiguous |
| Late chunking | Yes, if embedding model supports long context (BGE-M3 does) | Long PDFs and threads |
| Sentence-window / parent-child chunks | **Yes** | Index small, return the enclosing section/method/thread |
| Multi-query fusion, HyDE, step-back | Optional, query-time only | Improves recall on vague questions; costs latency |
| RAPTOR / GraphRAG / LightRAG | **Phase 3, not now** | Cross-project "how did we handle X" questions |
| ColPali page-as-image | Phase 3 | Only for scanned drawings/forms Docling can't read |
| RAGAS + pytest CI, Phoenix tracing | **Yes, from phase 1** | Build a 50–100 question gold set from real staff questions and measure before every change |

## 6. Ingestion details per source

**Gmail.** Export via Google Takeout (one `.mbox` per label, attachments embedded) for the historical bulk; add a Gmail API OAuth read-only sync for ongoing mail. Parse with Python `mailbox` + `email`; strip quoted replies and signatures (the `talon` or `mail-parser` libraries handle this) so the same paragraph is not indexed 40 times across a thread; one document per thread, one chunk per message; attachments extracted, hashed (dedupe across threads), parsed with Docling, indexed as separate documents with `parent_message_id`.

**PDF / Word / Excel / PowerPoint.** Docling (or RAGFlow's built-in DeepDoc parser — try both on 20 of your worst files and keep the better). Excel: every sheet becomes a table; every row becomes a chunk written as `Sheet: X | ColA: v1 | ColB: v2 …` so a value lookup hits; also keep the whole sheet as Markdown for context. Enable OCR for scanned PDFs (older projects will have many).

**Code.** One indexer per repo: walk the tree with `.gitignore` respected, skip vendored/binary files, chunk by AST node (class/method/interface, SQL procedure, MyBatis statement, Angular component), attach `repo, path, language, start_line, end_line, last_commit, author, date`. Also index commit messages and PR descriptions as text — they are where the "why" lives. Re-index incrementally on push.

**Databases.** Export DDL, stored procedures (`sys.sql_modules`, DB2 `QSYS2.SYSROUTINES`) and MyBatis mapper XML as code documents; they are the authoritative business logic and pair naturally with the knowledge base you are already building under `/docs/business-logic/`.

**Deduplication and versioning.** Hash content at file and chunk level; a PDF mailed five times is indexed once with five source references. Keep `version`/`superseded_by` so old spec versions can be found but are ranked below current ones.

## 7. Delivery plan

**Phase 0 — corpus census (1 week).** Count files, sizes, types, years, repos, mailboxes. Pick 20 nasty samples per type. Collect 50 real questions staff actually ask ("where is the interface spec for the 2016 AS/400 sync?"). This becomes your evaluation set.

**Phase 1 — documents in RAGFlow (2–3 weeks).** Stand up RAGFlow with Docker (Elasticsearch, MinIO, Redis, MySQL come with it). Create one knowledge base per project or per business domain. Bulk-load PDFs/Office files. Tune chunk templates per KB. Turn on hybrid search + reranker. Measure against the question set.

**Phase 2 — email and code (3–4 weeks).** Build the Python ingestion service (Docling + mbox loader + tree-sitter chunker) writing into RAGFlow via API or directly into Elasticsearch with the same mapping. Add filters for sender, date, repo, language. Re-measure.

**Phase 3 — hardening and reach (ongoing).** Incremental sync (Gmail API, Git hooks). Access control per KB. Expose retrieval as an MCP server so Copilot/Claude Code can query the archive from the IDE — this ties directly into your existing Copilot Agent Mode workflow. Consider LightRAG for cross-project questions once exact retrieval is solid. If internal Spring Boot apps need the archive, call the RAGFlow API or read the same Elasticsearch index through Spring AI.

## 8. Sizing and cost (self-hosted)

RAGFlow's stated minimum is 4 cores / 16 GB / 50 GB; for a 17-year corpus plan on one machine with 32–64 GB RAM and 500 GB–1 TB SSD for Elasticsearch plus originals in MinIO or an existing NAS. Embedding a few million chunks with BGE-M3 on a single consumer GPU takes hours, not days; CPU-only works but is roughly 10× slower. Everything above is Apache-2.0 / MIT; the only recurring cost is the generation LLM if you use a hosted one.

## 9. Risks to watch

Excel and scanned PDFs are where parsing quality varies most — test early. Email volume duplicates heavily (quoted replies, forwarded attachments) — dedupe or the index doubles. A single shared knowledge base across 17 years will need metadata filters exposed in the UI or people will not trust results. And keep the evaluation set alive: every parser or model change should be a measured before/after, which is the one lesson from the cookbook's "Evaluation & Production" section that matters most.

---

### Sources

- [rag-cookbook-2026 (FareedKhan-dev)](https://github.com/FareedKhan-dev/rag-cookbook-2026)
- [RAGFlow](https://github.com/infiniflow/ragflow) · [Docling](https://github.com/docling-project/docling) · [Onyx](https://github.com/onyx-dot-app/onyx) · [RAG-Mail](https://github.com/ManiAm/RAG-Mail)
- [15 Best Open-Source RAG Frameworks in 2026 (Firecrawl)](https://www.firecrawl.dev/blog/best-open-source-rag-frameworks) · [Best Open Source RAG Frameworks 2026 (Olostep)](https://www.olostep.com/blog/open-source-rag-frameworks) · [LangChain vs LlamaIndex vs Haystack vs RAGFlow](https://langcopilot.com/posts/2025-09-18-best-rag-frameworks-2026)
- [Hybrid Search and Re-ranking in Production RAG 2026](https://appscale.blog/en/blog/hybrid-search-and-reranking-production-rag-bm25-dense-cross-encoder-2026) · [Hybrid Search in PostgreSQL (ParadeDB)](https://www.paradedb.com/blog/hybrid-search-in-postgresql-the-missing-manual)
- [cAST: Structural Code Chunking via AST](https://arxiv.org/html/2506.15655v1) · [Reliable Graph-RAG for Codebases](https://arxiv.org/abs/2601.08773) · [Index a codebase with tree-sitter and CocoIndex](https://cocoindexio.substack.com/p/index-codebase-with-tree-sitter-and)
- [Extracting Emails from Gmail with Google Takeout (mbox)](https://www.cloudmailin.com/blog/extracting-emails-from-gmail-with-google-takeout-and-mbox)
- [Spring AI ETL Pipeline](https://docs.spring.io/spring-ai/reference/api/etl-pipeline.html) · [RAG with Docling, Java and Spring AI](https://www.thomasvitale.com/rag-docling-java-spring-ai/) · [Advanced RAG with Spring AI: hybrid search and re-ranking](https://springdevpro.com/spring-ai/advanced-rag-spring-ai-hybrid-search-reranking/)
