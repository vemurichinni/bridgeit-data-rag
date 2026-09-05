# BridgeIT-Data — deployment profiles: local vs. cloud

*Keep both configured. Switching between them is a config/UI change, not a rebuild —
use the fully-local profile day to day, and flip to a cloud model (or a cloud-hosted
RAGFlow instance) whenever you specifically need it.*

## The three things that can each be local or cloud, independently

RAGFlow itself (Elasticsearch, MySQL, Redis, MinIO, the RAGFlow app) is always
self-hosted — there is no "RAGFlow cloud" to rent, you run its `docker-compose`
yourself, on your laptop or on a rented VM, the compose file is identical either way.
What actually varies is three model dependencies:

| dependency | configured in | fully-local option | cloud option |
|---|---|---|---|
| **Embedding model** (chunks/questions → vectors) | `phase1/config.local.yaml` → `ragflow.embedding_model` | Ollama serving `bge-m3` | any provider RAGFlow's Model Providers page supports (OpenAI, Voyage, Jina, Cohere, ...) |
| **Reranker** | `phase1/config.local.yaml` → `ragflow.rerank_model` | Xinference serving `bge-reranker-v2-m3`, or leave empty to disable | a hosted reranker provider, or leave empty |
| **Generation LLM** (writes the final cited answer) | **RAGFlow's own web UI** — Model Providers, then each Chat Assistant's model dropdown. Not in this repo's config at all. | Ollama serving a local chat model (Qwen, Llama, ...) | Claude / OpenAI / any provider RAGFlow supports |

That last row is the one people expect to find in `config.yaml` and won't — this repo's
config only drives the ingestion/retrieval scripts (`load_documents.py`, `run_eval.py`,
the Phase 2/3 ingesters, `mcp_server.py`). The chat-answer model is a RAGFlow concept,
set once in its UI, and every script here is unaffected by which one you pick.

## Profile A — fully local (nothing leaves the machine)

1. Deploy RAGFlow itself (its own repo, `docker-compose up -d`) — see `docs/Phase1-Runbook.md`.
2. Bring up the local model server:
   ```bash
   docker compose -f deploy/docker-compose.ollama.yml up -d
   docker exec bridgeit-ollama ollama pull bge-m3
   docker exec bridgeit-ollama ollama pull qwen2.5:14b-instruct   # or a size that fits your RAM/VRAM
   ```
3. In RAGFlow's UI → Model Providers → add Ollama, base URL `http://host.docker.internal:11434`
   (put both containers on the same Docker network and use the service name if that
   hostname doesn't resolve on your platform). Register `bge-m3` as an embedding model
   and `qwen2.5:14b-instruct` as a chat model.
4. `phase1/config.local.yaml`:
   ```yaml
   ragflow:
     embedding_model: "bge-m3:latest@Ollama"
     rerank_model: ""     # or run Xinference for bge-reranker-v2-m3 the same way
   ```
5. In each Chat Assistant (RAGFlow UI), pick the Ollama chat model as its LLM.

Every request — embedding, retrieval, reranking, generation — now runs on your
hardware. This is the right default for a founder-era archive with everyone's email
and every project's source in it.

## Profile B — local RAGFlow, cloud generation only

Same as Profile A through step 4, except in the Chat Assistant's LLM dropdown pick a
hosted provider (add its API key under Model Providers first — Anthropic for Claude,
OpenAI, etc.) instead of the Ollama chat model. Only the chunks retrieved for a given
question are sent to that provider per query — embeddings, storage and retrieval stay
local. Reach for this when you want stronger answer quality than a local model gives
you and are comfortable with just the cited snippets leaving the machine.

## Profile C — fully cloud

Run the identical RAGFlow `docker-compose` on a rented VM instead of your laptop
(sizing per `docs/RAG-System-Recommendation.md` §8 / the follow-up sizing discussion
for your ~50 GB corpus), and use either local-to-that-VM models or hosted ones. This
is the same "prepare locally, load in the cloud" split already documented in
`docs/Phase2-Runbook.md`'s "Offline prep, cloud load" section — relevant once you want
an always-on, shared instance rather than something that only runs while your laptop
is on.

## Switching later

Nothing above is one-way. Because the embedding model is only referenced by name in
`config.local.yaml` and RAGFlow's provider registry, you can register both an Ollama
and a cloud provider at the same time and just change which name a knowledge base or
chat assistant points at — no re-ingestion is needed to switch the *generation* model,
since it only reads the already-computed chunks/vectors at query time. Switching the
*embedding* model, however, does require re-parsing (RAGFlow re-embeds with whatever
model the knowledge base is currently set to) — plan an embedding-model change as a
deliberate, measured step (`phase1/run_eval.py` before/after), not a casual toggle.
