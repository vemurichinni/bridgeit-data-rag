# BridgeIT-Data — Phase 3 runbook: hardening and reach

*Phases 0–2 built the archive: census, RAGFlow document load, email-thread and code
ingestion. Phase 3 is "ongoing" by design (docs/RAG-System-Recommendation.md section 7)
— it keeps the archive current, tightens who can see what, exposes retrieval to IDE
agents, and turns on cross-project reasoning where it earns its cost. Nothing here
re-ingests or re-chunks anything; it operates on the KBs Phase 1/2 already built.*

## What is in this kit

```
phase3/
├── incremental_sync.py     nightly entry point: git pull + ingest_code.py, gmail_sync.py + ingest_mbox.py
├── gmail_sync.py            Gmail API incremental pull (history.list) -> .mbox for ingest_mbox.py
├── access_control.py        apply a permission policy (me/team) across knowledge bases
├── access_policy.yaml       example policy — copy to access_policy.local.yaml and edit
├── enable_graphrag.py       flip RAPTOR/GraphRAG on for specific existing KBs
├── mcp_server.py            MCP server exposing search_archive/list_knowledge_bases/list_documents
└── hooks/post-merge         git hook template: re-index a repo after every `git pull`
```

Install: `pip install -r requirements.txt` (adds `mcp`, and the optional Gmail API client
libraries). Everything here reads the same `phase1/config.local.yaml` as Phases 1–2.

## 1. Incremental sync

Phase 2 already made re-running its ingesters cheap: `ingest_code.py` keys files by
content hash, so a rerun after `git pull` only re-embeds what changed, and (fixed in
this phase) `ingest_mbox.py`'s manifest now re-embeds a thread when its message count
grows instead of treating it as permanently done. `incremental_sync.py` is just the
orchestration around that:

```bash
cd phase3
python incremental_sync.py --config ../phase1/config.local.yaml \
       --repos-csv ../phase0/census_out/repos.csv \
       --gmail-credentials gmail_credentials.json \
       --mail-account projects@bridgeit.com --mail-account ops@bridgeit.com \
       --attachments-dir /data/mail-attachments \
       --log sync.log
```

It runs, in order: `git pull` on every repo in `--repos-csv` (and any `--repo` given
directly), one `ingest_code.py` pass over all of them, then for each `--mail-account`
either a Gmail API incremental pull (if `--gmail-credentials` is set) or ingestion of
whatever `--mail-glob` points at. It exits non-zero if any step failed, so a cron
wrapper can alert:

```cron
0 2 * * * cd /opt/bridgeit-data-rag/phase3 && python3 incremental_sync.py \
    --config ../phase1/config.local.yaml --repos-csv ../phase0/census_out/repos.csv \
    --gmail-credentials gmail_credentials.json --mail-account projects@bridgeit.com \
    --log sync.log >> sync.cron.log 2>&1
```

### Git hooks (event-driven code sync)

For repos where "current after the next pull" beats "current by 2am", install the
provided hook instead of (or alongside) the cron pass:

```bash
cp phase3/hooks/post-merge /path/to/each-repo/.git/hooks/post-merge
chmod +x /path/to/each-repo/.git/hooks/post-merge
```

It calls `incremental_sync.py --repo <this repo> --skip-mail` in the background after
every `git pull`, so the pull itself is not slowed down. Set `BRIDGEIT_RAG_HOME` in the
environment if this repo isn't cloned at `~/bridgeit-data-rag`.

### Gmail API incremental pull

`gmail_sync.py` replaces "export a fresh Takeout periodically" with a proper
incremental pull: it stores the Gmail API's `historyId` per account in
`gmail_sync_state.json` and, on each run, fetches only messages added since then via
`users.history.list`, writing them to a fresh `.mbox` that `ingest_mbox.py` reads
exactly like a Takeout export. First run for an account (no state, or an expired
`historyId` — Google keeps history for about a week) backfills `--since-days` (default
30) instead of the whole mailbox; use Takeout for anything older, once, as already
described in Phase 2.

Setup: enable the Gmail API in Google Cloud Console, create an OAuth Desktop-app client,
download it as `gmail_credentials.json`. The first run opens a browser for consent and
caches a token; every run after that is unattended, so it's safe to call from cron via
`incremental_sync.py --gmail-credentials ...`.

### The Phase 2 thread-growth fix

`phase2/sink.py`'s `Manifest.has()` now takes an optional `version`; `ingest_mbox.py`
passes the message count. A thread that gained a reply since the last run keeps its
`thread_id` (so citations stay stable) but is re-embedded because its version changed —
no more manually deleting manifest lines to pick up growing threads.

## 2. Access control

RAGFlow's open-source HTTP API controls visibility at the **dataset** level only:
`permission` is `"me"` (creator/owner account only) or `"team"` (everyone on the same
RAGFlow team). There is no per-user or per-group ACL in the OSS API — real role-based
access needs the Enterprise edition, or an authorization layer in front of retrieval.

Given that ceiling, `access_control.py` keeps every KB's permission in line with a
small policy file, so sensitive archives default to `"me"` instead of silently staying
team-visible:

```bash
cp phase3/access_policy.yaml phase3/access_policy.local.yaml   # edit patterns for your org
python access_control.py --config ../phase1/config.local.yaml \
       --policy access_policy.local.yaml --status              # inspect only
python access_control.py --config ../phase1/config.local.yaml \
       --policy access_policy.local.yaml --dry-run              # preview changes
python access_control.py --config ../phase1/config.local.yaml \
       --policy access_policy.local.yaml                        # apply
```

Rules match dataset names by glob, top to bottom, first match wins; anything unmatched
gets the policy's `default`. Run `--status` after every new KB (a new project folder, a
new mail account, a new repo) lands, since new KBs default to whatever RAGFlow's UI
default is until this script has run.

For finer control than "me"/"team", front retrieval with `mcp_server.py`'s
`BRIDGEIT_RAG_ALLOWED_KB` allow-list (below) or an equivalent check in any other client
of the RAGFlow API — a KB-name allow-list per caller is the practical substitute for
per-user ACL until (if ever) RAGFlow Enterprise is adopted.

## 3. MCP endpoint for IDE agents

`mcp_server.py` exposes the same hybrid retrieval `run_eval.py` scores against, as an
MCP server any MCP-capable IDE agent (Claude Code, VS Code Copilot Agent Mode, etc.) can
call directly — this is the "ties directly into your existing Copilot Agent Mode
workflow" item from the recommendation doc.

```bash
export BRIDGEIT_RAG_CONFIG=/path/to/phase1/config.local.yaml
python phase3/mcp_server.py   # run manually to sanity-check it starts; normally launched by the MCP client
```

Register it as a stdio server, e.g. in a project's `.mcp.json`:

```json
{
  "mcpServers": {
    "bridgeit-archive": {
      "command": "python3",
      "args": ["/opt/bridgeit-data-rag/phase3/mcp_server.py"],
      "env": { "BRIDGEIT_RAG_CONFIG": "/opt/bridgeit-data-rag/phase1/config.local.yaml" }
    }
  }
}
```

Tools exposed:

| tool | purpose |
|---|---|
| `list_knowledge_bases()` | every KB the server can see, with doc/chunk counts |
| `search_archive(question, kb, project, top_k, use_rerank)` | hybrid BM25+vector retrieve, reranked, with verbatim chunks and metadata |
| `list_documents(kb, keywords, limit)` | browse a KB's documents by name |

`search_archive` never paraphrases — it returns the chunk text RAGFlow stored plus
whatever metadata the ingester stamped (`source_path`, `thread_id`/`subject`,
`repo`/`path`/`kind`, `commit`, ...), so an agent's answer stays traceable to the
original file, thread or commit, per the "verbatim chunk storage with provenance"
requirement in the recommendation doc.

Set `BRIDGEIT_RAG_ALLOWED_KB` (comma-separated globs, a leading `-` excludes) to
restrict which KBs this particular server instance will ever query, independent of
what the configured RAGFlow API key can see — use this to run one server per
sensitivity tier if `access_control.py`'s `"me"`/`"team"` split isn't enough on its own.

## 4. GraphRAG / RAPTOR for cross-project questions

Per the recommendation doc's phased technique list, RAPTOR and GraphRAG were left off
in Phase 1's `config.yaml` (`parser_config.raptor.use_raptor: false`,
`parser_config.graphrag.use_graphrag: false`) until exact hybrid retrieval was proven
out. `enable_graphrag.py` flips them on for specific existing KBs without recreating
them — do this for the archive-wide or cross-project KBs where "how did we handle X
across projects" questions actually come up, not everywhere (both cost an LLM pass over
the whole KB and re-embedding, and trade some exact-match precision for synthesis):

```bash
python enable_graphrag.py --config ../phase1/config.local.yaml --pattern "bt-*" --list   # inspect current state
python enable_graphrag.py --config ../phase1/config.local.yaml --kb bt-archive-all --graphrag --graphrag-method light
python enable_graphrag.py --config ../phase1/config.local.yaml --kb bt-projb_2016 --raptor
```

After enabling, re-parse the KB's documents (`phase1/load_documents.py --status` to
check, then re-parse from the UI or by re-running the loader) — RAPTOR/GraphRAG only
run on documents parsed after the flag is on. Re-measure with `run_eval.py` before and
after on the cross-project questions in the evaluation set; if recall on exact-ID
questions in the same KB drops, that KB was a bad candidate — turn it back off with
`--no-graphrag`/`--no-raptor`.

## 5. Internal Spring Boot apps

Unchanged from the recommendation doc: if an internal Java service needs the archive,
call the RAGFlow API directly (`ragflow_client.py`'s `retrieve()` is a 20-line
reference for the request/response shape) or read the same Elasticsearch index through
Spring AI's `VectorStore` abstraction. Nothing in Phase 3 blocks either path — both are
just additional callers of the same store `access_control.py` is governing.

## Exit criteria for Phase 3

Nightly `incremental_sync.py` (or the per-repo hook) running with under 2% step
failures over a week; every KB's permission matching `access_policy.local.yaml`
(`access_control.py --status` shows no diffs); `mcp_server.py` reachable from at least
one IDE agent and returning cited results for a sample of the evaluation set's
questions; and, if GraphRAG was enabled anywhere, a measured before/after on the
affected KB's cross-project questions.

## Known limitations

RAGFlow OSS access control tops out at "me"/"team" — anything closer to real RBAC is
either RAGFlow Enterprise or a bespoke authorization layer in front of `/retrieval`
(the `BRIDGEIT_RAG_ALLOWED_KB` pattern in `mcp_server.py` is a starting point, not a
complete one — it trusts whatever launches the process). `gmail_sync.py` needs a
human to complete the OAuth consent screen once per account; there is no service-account
path for personal Gmail. GraphRAG/RAPTOR indexing cost scales with KB size and is not
incremental — re-enabling after adding many new documents means another full re-parse.
