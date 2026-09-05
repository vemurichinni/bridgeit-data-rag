# BridgeIT-Data — Phase 2 runbook: email threads and source code

*Phase 1 covered the file archive through RAGFlow's own parsers. Phase 2 adds the two sources those parsers handle badly: Gmail history and the code of every application. Both go through ingesters we control, land in the same RAGFlow instance, and are measured with the same evaluation set.*

## Why these two need custom ingestion

RAGFlow can open a single `.eml` and it can read a `.java` file as text, but neither gives you exact retrieval at 17-year scale:

- **Email** arrives as one giant mbox, not as files. The unit people ask about is a *thread* ("who approved the go-live"), while the unit that must be embedded is a *message*. And roughly half of all body text in a mail archive is quoted history — index it and the same paragraph appears twenty times, pushing the real answer out of the top 5.
- **Code** has natural boundaries — a method, a stored procedure, a MyBatis statement — that a fixed 512-token window cuts straight through. Splitting `usp_ApplyCreditNote` in half means neither half answers "what does it call afterwards".

So Phase 2 does the splitting itself and pushes finished chunks into RAGFlow through its add-chunk API. The document uploaded alongside them is a readable rendering (the whole thread, or the whole file) that citations point at, so a user can always open the full context.

## What is in this kit

```
phase2/
├── ingest_mbox.py           Gmail Takeout .mbox → thread documents + per-message chunks
├── ingest_code.py           Git repos → per-file documents + AST-level chunks + commit history
├── sink.py                  shared output: RAGFlow (upload + add-chunk + metadata) or JSONL, plus resume manifests
├── chunkers/
│   ├── email_clean.py       quoted-history, signature and disclaimer stripping; HTML → text
│   └── code_chunker.py      tree-sitter AST chunking; SQL / MyBatis / Markdown chunkers
└── PHASE2-RUNBOOK.md        this file
```

Install: `pip install -r requirements.txt` at the repo root (adds `tree-sitter` and `tree-sitter-language-pack` on top of Phase 1). Both ingesters read the **same `phase1/config.local.yaml`** — RAGFlow URL, API key, embedding model and the KB name prefix.

## Step 1 — email

```bash
cd phase2

# dry run first: writes JSONL, touches nothing in RAGFlow
python ingest_mbox.py --config ../phase1/config.local.yaml \
       ~/Takeout/Mail/*.mbox --account projects@bridgeit.com \
       --dry-run --jsonl mail.jsonl --attachments-dir /data/mail-attachments

# inspect a few threads, then load for real
python ingest_mbox.py --config ../phase1/config.local.yaml \
       ~/Takeout/Mail/*.mbox --account projects@bridgeit.com \
       --attachments-dir /data/mail-attachments --skip-labels Spam,Trash,Promotions
```

What it does, in order:

1. **Threads.** Messages are grouped by Gmail's `X-GM-THRID`, falling back to `References`/`In-Reply-To` chains and then to normalised subject, so mail exported from other clients still groups correctly.
2. **Cleaning.** Every body loses quoted history (`On … wrote:`, `-----Original Message-----`, `>` lines, Outlook `From:/Sent:/To:` blocks), signatures, mobile footers and legal disclaimers; HTML-only messages are converted to text. Only what the author typed is indexed.
3. **Attachments.** Extracted to `--attachments-dir`, de-duplicated by SHA-256 (the plan mailed five times is stored once), and listed in `attachments.csv` **in Phase 0 census format** — so the Phase 1 loader ingests them as ordinary parsed documents:
   ```bash
   python ../phase1/load_documents.py --config ../phase1/config.local.yaml \
          --census /data/mail-attachments/attachments.csv
   ```
4. **Chunks.** One per message (split further only if very long), each prefixed with thread subject, sender, date, recipients and attachment names, so a chunk retrieved alone still identifies itself. Sender addresses, subject words, file names and identifiers (`INV-2001`, `ORD-88123`) become RAGFlow keywords — this is what makes exact-ID lookups land.

Knowledge base: `<prefix>mail-<account>`. Metadata per thread: `account, thread_id, subject, year, first_date, last_date, message_count, participants, domains, labels, attachment_count` — all filterable at query time.

Useful flags: `--since 2012` to skip prehistory, `--limit 500` for a trial run, `--kb` to force a KB name. Reruns skip threads already in `mbox_manifest.jsonl`, so an interrupted load resumes and a monthly top-up only adds new threads.

## Step 2 — code

```bash
# every repo below a folder, or the repos Phase 0 already found
python ingest_code.py --config ../phase1/config.local.yaml /src/repos --dry-run --jsonl code.jsonl
python ingest_code.py --config ../phase1/config.local.yaml --repos-csv ../phase0/census_out/repos.csv \
       --project "Billing platform"
```

Chunk boundaries by file type:

| file type | one chunk per | named as |
|---|---|---|
| Java, Kotlin, C#, TypeScript, JavaScript, Python, Go | method / constructor / function (tree-sitter AST), plus one "outline" chunk holding package, imports, fields and the class header | `OrderController.getOrder` |
| SQL (`.sql`, `.prc`, `.ddl`) | `CREATE PROCEDURE / FUNCTION / TRIGGER / VIEW / TABLE` statement | `dbo.usp_ApplyCreditNote` |
| MyBatis mapper XML | `<select> <insert> <update> <delete> <resultMap> <sql>` element | `com.bridgeit.OrderMapper.findById` |
| Markdown / AsciiDoc / text | heading section | the heading |
| other XML, properties, YAML, JSON | whole file, size-split | file name |

Annotations and Javadoc immediately above a method travel with it. Every chunk is prefixed with `Repo | File | kind: qualified name | lines a–b | last change <date> by <author> (<commit>)`, and the identifiers inside it become keywords, so `usp_RecalcInvoiceTotals` or `SessionTimeoutInterceptor` is findable by name alone. Vendored and build folders (`node_modules`, `target`, `build`, `dist`, `.gradle`, …) are skipped.

**Commit history** is indexed too — one chunk per commit (subject, body, changed files), up to `--commits` per repo, because the reason a rule exists is usually in a commit message rather than the code. This is also where `BR-SEQ-*` references surface.

Knowledge base: `<prefix>code-<repo>`. Metadata: `repo, path, language, kind, last_commit, last_author, last_date, year, loc`, plus `--project` if you pass one. Files are keyed by content hash in `code_manifest.jsonl`, so re-running after a `git pull` re-indexes only what actually changed — that is the incremental sync mechanism, and it is cheap enough to run nightly from cron.

**Database objects.** Stored procedures and DDL exported from SQL Server (`sys.sql_modules`) and DB2 (`QSYS2.SYSROUTINES`) into `.sql` files are ingested by exactly the same path — point `ingest_code.py` at the export folder. Those files pair with the `/docs/business-logic/` knowledge base already in progress: the `BR-SEQ-*` rules describe the behaviour, these chunks are the implementation.

## Step 3 — measure

Phase 2 content is scored with the Phase 1 harness, unchanged:

```bash
python ../phase1/run_eval.py --config ../phase1/config.local.yaml \
       --xlsx ../phase0/evaluation_set.xlsx --label "phase2 mail+code"
```

Add rows to the evaluation set for the new sources before you run it — "who approved X and when" (Email), "which procedure does Y" (Code), "which MyBatis statement updates Z" (Exact-ID). For email questions the **Expected source** cell should name the thread in quotes (`Gmail, thread 'Release 2.3 go-live confirmation', message from j.doe@client.com`); for code, the repo and path (`repo orders-service, db/usp_ApplyCreditNote.sql`). The harness matches both forms against Phase 2 document titles.

Expect email and code to behave differently from documents: code questions are keyword-dominant, so if raising `--vector-weight` improves them something is wrong with how identifiers are being tokenised. Email questions benefit most from the reranker, because many threads look alike to an embedding model.

## Step 4 — keep it current

Once the backfill is done, both ingesters are incremental by design. A nightly job is enough:

```bash
# code: re-run after fetching; unchanged files are skipped by content hash
git -C /src/repos/<each> pull --quiet && python ingest_code.py --config ... --repos-csv ...
# email: export a fresh Takeout periodically, or add a Gmail API sync; threads already loaded are skipped
python ingest_mbox.py --config ... /data/takeout-latest/Mail --account projects@bridgeit.com
```

Note that a thread which gained a reply keeps its `thread_id` but gets a new set of messages; the manifest currently treats a known thread as done, so to refresh growing threads either delete their manifest lines or (simpler) let the next full Takeout land in a new account label. Fixing this properly — re-embedding a thread when its message count changes — is a small change to `Manifest.has()` and worth doing once mail volume settles.

## Exit criteria for Phase 2

Every mailbox and every repository ingested with under 2% errors; attachments extracted, de-duplicated and loaded through the Phase 1 loader; the evaluation set extended with at least 15 email and code questions and scored in a run block; and the nightly incremental job running for code.

## Known limitations

`.pst` and `.msg` Outlook exports are not handled — convert to mbox (`readpst`) or let RAGFlow's `email` chunk method take single `.msg` files. Encrypted or password-protected attachments are extracted but will fail parsing downstream, and appear in the Phase 1 loader's failures. RPG, CL and COBOL source is indexed as whole files split by size, since no tree-sitter grammar ships for them — acceptable, because those files are usually short and searched by name.
