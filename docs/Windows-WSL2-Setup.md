# BridgeIT-Data — Windows/WSL2 local setup

*Step-by-step for running the fully-local profile (`docs/Deployment-Options.md`, Profile A)
on a Windows machine with Docker Desktop + WSL2 — RAGFlow, local models via Ollama, and
this repo's ingestion pipeline, all on your own hardware. Includes fixes for the specific
failures encountered setting this up the first time, so the next person (or your future
self, on a fresh machine) doesn't have to re-discover them.*

## 0. Before you start

**Work entirely inside your Linux home directory (`~`), never under `/mnt/c/...`.**
Two reasons: WSL's pass-through to the Windows filesystem (`drvfs`) doesn't map Linux
file permissions cleanly — Docker's own `mkdir`/`chmod` calls can fail there in ways
that look like random permission errors — and it's much slower for the kind of small,
frequent file I/O Elasticsearch does. The one exception is your archive/hard disk data
itself: reading from `/mnt/c/...` or `/mnt/e/...` etc. is fine (the census and ingest
scripts only read), it's Docker writing there that's a problem. If a WSL terminal opens
with its working directory already inside `/mnt/c/...` (this happens by default from
some elevated Command Prompt/PowerShell/Run-dialog launches, and has been seen landing
directly in `C:\WINDOWS\system32` — a protected system directory that denies writes
outright), `cd ~` before doing anything else.

## 1. Install WSL2

```powershell
# PowerShell, as Administrator
wsl --install
```
Restart when prompted. This installs WSL2 and an Ubuntu distro by default.

## 2. Install Docker Desktop and enable WSL integration

Install **Docker Desktop for Windows**, then in its Settings:
- General → enable "Use the WSL 2 based engine"
- Resources → WSL Integration → enable it for your Ubuntu distro

## 3. Confirm the GPU is reachable from Docker (if you have one)

Make sure you're on a current NVIDIA driver, then from the Ubuntu WSL shell:
```bash
docker run --rm hello-world                                                    # sanity check Docker itself first
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi       # then the GPU specifically
```
If `hello-world` fails with **permission denied**: your user isn't in the `docker`
group yet. Toggle WSL Integration off/on for your distro in Docker Desktop, then:
```powershell
wsl --shutdown        # PowerShell — forces a fresh session so group membership takes effect
```
reopen your terminal and retry. If it's still denied, add yourself manually:
```bash
sudo usermod -aG docker $USER
newgrp docker
```

If GPU passthrough specifically fails (but `hello-world` works): check Docker Desktop
→ Settings → Resources → GPU is enabled, and that your driver version is current — this
matters more on very new GPU generations.

**If Docker Desktop itself reports "WSL integration with distro 'Ubuntu' unexpectedly
stopped"** with an error like `relocating proxy binary: no exec-capable directory found`
/ `install: No such file or directory`: this is Docker Desktop's internal WSL distros
being unable to place their own binaries — usually one of:
1. **Low disk space** on the Windows drive hosting `%LOCALAPPDATA%\Docker\wsl\` — check
   with `Get-PSDrive C` in PowerShell before anything else.
2. Stale WSL/Docker state — update and fully restart both:
   ```powershell
   wsl --update
   wsl --shutdown
   ```
   then fully quit Docker Desktop (tray icon → Quit, not just close the window) and
   reopen it.
3. Still broken — recreate Docker's internal distros (safe; Docker rebuilds them):
   ```powershell
   wsl --unregister docker-desktop
   wsl --unregister docker-desktop-data
   ```
   then restart Docker Desktop.
4. Still broken — try toggling `systemd=false` in your Ubuntu distro's `/etc/wsl.conf`
   to rule out a systemd/Docker Desktop interaction bug, then `wsl --shutdown` and retest.

## 4. Python environment

Modern Ubuntu (Debian's PEP 668 "externally managed environment") blocks
`pip install` outside a virtual environment. Always use one for this repo:
```bash
sudo apt install -y python3-venv python3-full git
cd ~
git clone https://github.com/vemurichinni/bridgeit-data-rag.git
cd bridgeit-data-rag
python3 -m venv .venv
source .venv/bin/activate       # do this every time you open a new terminal for this repo
pip install --upgrade pip
pip install -r requirements.txt
```

## 5. Stand up RAGFlow

Clone it into your Linux home directory — **not** under `/mnt/c/...` (see §0):
```bash
cd ~
git clone https://github.com/infiniflow/ragflow.git
cd ragflow/docker
docker compose up -d
```
First startup pulls several images and can take a few minutes. Then open
`http://localhost` in a browser and create the admin account.

## 6. Local models (Ollama)

From the `bridgeit-data-rag` repo:
```bash
cd ~/bridgeit-data-rag
docker compose -f deploy/docker-compose.ollama.yml up -d
docker exec bridgeit-ollama ollama pull bge-m3
docker exec bridgeit-ollama ollama pull qwen2.5:14b-instruct   # size this to your GPU's VRAM — see docs/Deployment-Options.md
```

**Validate it actually works before moving on** — a container that's "Up" doesn't mean
the model runs; test the embedding endpoint directly:
```bash
curl http://localhost:11434/api/embeddings -d '{"model": "bge-m3", "prompt": "test sentence"}'
```
This should return a JSON object with a 1024-number `"embedding"` array. If instead you
get `{"error":"llama-server process has terminated: signal: segmentation fault"}`, this
is llama.cpp's CUDA backend crashing on an unsupported GPU architecture — seen on very
new GPU generations (e.g. RTX 50-series) where the CUDA kernels Ollama shipped with
don't yet recognize the card's compute capability. Force CPU-only until an Ollama
release with confirmed support for your GPU lands, by adding this to the `ollama`
service in `deploy/docker-compose.ollama.yml`:
```yaml
services:
  ollama:
    environment:
      - CUDA_VISIBLE_DEVICES=
```
then `docker compose -f deploy/docker-compose.ollama.yml down && ... up -d` (a full
`down`+`up`, not just `up -d`, to guarantee the container is recreated — `docker exec
bridgeit-ollama env | grep -i cuda` should show `CUDA_VISIBLE_DEVICES=` before you
retest). At the corpus sizes this repo targets, CPU-only embedding is entirely
workable — see the sizing discussion in `docs/RAG-System-Recommendation.md` — it's the
chat/generation model that will feel slow on CPU, so prefer a smaller one (e.g.
`qwen2.5:7b-instruct`) until GPU support is confirmed working.

In RAGFlow's UI → Model Providers → add Ollama, base URL `http://host.docker.internal:11434`
(this resolves correctly on Docker Desktop for Windows). Register `bge-m3` as an
embedding model and your chosen chat model, then create an API key (avatar → API →
Create).

## 7. Configure this repo

```bash
cd ~/bridgeit-data-rag
cp phase1/config.yaml phase1/config.local.yaml
```
Fill in `base_url` (`http://localhost`), the API key from step 6, and confirm
`embedding_model` matches exactly what RAGFlow's Model Providers page shows.

## 8. From here

Continue with the README's Quick start (census → load → evaluate → email/code
ingestion → Phase 3 hardening), all from inside this same WSL shell with the venv
activated. `docs/Phase1-Runbook.md` onward isn't Windows-specific — the setup above is
what gets you to a working RAGFlow + local models; everything after runs the same as on
Linux/Mac.
