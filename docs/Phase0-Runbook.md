# BridgeIT-Data — Phase 0 runbook: corpus census and evaluation set

*2026-09-05. Goal of Phase 0: know exactly what the archive contains, find the files that will break parsers, and write down the questions the system must answer — before any RAG software is installed.*

## What is in this kit

```
phase0/
├── census/
│   ├── census_files.py    inventory folders of PDFs/Office/text; detects scanned & encrypted PDFs,
│   │                      legacy .doc/.xls, multi-sheet/formula workbooks, exact duplicates
│   ├── census_mbox.py     inventory Gmail Takeout .mbox: messages, threads, years, attachments,
│   │                      quoted-reply ratio, duplicate attachments (never writes bodies out)
│   ├── census_git.py      inventory Git repos: languages, commits, authors, MyBatis mappers,
│   │                      stored procedures, DDL, Angular components, controllers/services
│   └── census_report.py   merges the three into CENSUS_REPORT.md with chunk/index/embedding-time estimates
├── evaluation_set.xlsx    the 50–100 question gold set (Questions, Coverage, Runs, Parser tests)
└── PHASE0-RUNBOOK.md      this file
```

Python 3.10+ and the standard library are enough. `pypdf` and `openpyxl` (both `pip install`-able) switch on the PDF and Excel hazard checks; `git` on PATH switches on commit history.

## Where to run it

The census has to run on the machine that can see the archive — a file server, the NAS, a workstation with the share mounted, or wherever the repos are cloned. Nothing leaves that machine: the outputs are CSV/JSON of paths, sizes, hashes and header fields only. The Google Drive connected to this workspace is a personal account, not the company archive, so it was not inventoried.

## Step 1 — export Gmail (start this first; it takes hours)

1. Sign in to the mailbox that holds the project history and open https://takeout.google.com.
2. Deselect all, tick **Mail**, choose *All Mail data included* (or only the labels you want), export as `.zip`, 10 GB parts.
3. Unzip; you get `Takeout/Mail/*.mbox`. Attachments are embedded in the mbox — nothing else to download.
4. If there are several mailboxes, do this for each one; keep the files under `mail/<account>/`.

## Step 2 — run the three inventories

```bash
# documents: every share / folder that holds project material
python census/census_files.py "D:\Projects" "\\\\nas\\archive\\clients" --out census_out

# email: the Takeout folder(s)
python census/census_mbox.py mail --out census_out

# code: the folder(s) where the repos are cloned (finds every .git up to 4 levels deep)
python census/census_git.py "C:\src" "D:\legacy-repos" --out census_out

# merge + sizing (use --gpu if you will embed on a GPU box)
python census/census_report.py --out census_out --gpu
```

Runtime: `census_files.py` hashes every file, so budget roughly 1 minute per 5 GB on a local SSD, slower on a network share (`--no-hash` skips it but then you lose duplicate detection). `census_mbox.py` reads about 1 GB of mbox per minute. `census_git.py` is fast.

Outputs in `census_out/`:

| file | use |
|---|---|
| `CENSUS_REPORT.md` | the one-page picture: counts, sizes, years, hazards, chunk and hardware estimate |
| `files_summary.json` → `by_top_folder` | decide knowledge-base boundaries (per project / domain / year) |
| `samples.csv`, `mbox_samples.csv`, `code_samples.csv` | the worst-case files to test parsers and chunkers on — fill in `parser_result` |
| `files.csv`, `mbox_messages.csv`, `code_files.csv`, `repos.csv` | full inventories for later filtering and for the ingestion manifest |

## Step 3 — collect the questions

Open `evaluation_set.xlsx`, sheet **Questions**. Yellow cells are yours; the five blue rows are examples to overwrite. For each question record where the answer *actually lives* (path + page/sheet/row, repo + file + method, or thread + sender + date) and the verbatim snippet that proves it. The **Coverage** sheet counts query types as you go — aim for a quarter of questions to be exact-identifier lookups (project codes, procedure names, invoice numbers), since exact retrieval is the stated requirement.

Good places to harvest questions: the last ten things you personally had to dig for; onboarding questions from the newest team member; "does anyone remember…" emails; support tickets that needed archaeology; the business-logic knowledge base work already under way (every `BR-SEQ-*` rule is a candidate "where is this implemented?" question).

## Step 4 — parser bake-off on the samples

For each row in the three sample CSVs, run Docling (`pip install docling`; `docling file.pdf --to md`) and, once RAGFlow is up in Phase 1, its DeepDoc parser. Log results in sheet **Parser tests**: did tables survive, is the text verbatim, which one wins. This is what decides whether RAGFlow's built-in parsing is enough or Docling goes in front of it for some families.

## Exit criteria for Phase 0

Phase 0 is done when `CENSUS_REPORT.md` exists for every source, the sample CSVs have a `parser_result` for every row, the evaluation set has at least 50 questions with sources, and knowledge-base boundaries are written down. Then Phase 1 (RAGFlow stand-up and bulk document load) starts from measured numbers instead of guesses.

## Scoring later phases

Sheet **Runs** has three column blocks (baseline, hybrid + rerank, custom chunkers). For each run mark Hit@5, rank and exact-match per question; Recall@5, MRR and exact-match rate compute at the bottom. Add a block per experiment. A change ships only when it does not lower exact-match rate.
