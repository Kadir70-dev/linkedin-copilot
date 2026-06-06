# LinkedIn Copilot — Local MVP (Phase 0 + Phase 1)

A fully local AI copilot skeleton that proves the safe flow:

```
ingest thread → AI draft (local Qwen) → MANDATORY approval → inject into composer → HUMAN clicks Send
```

**No auto-send. No LinkedIn API writes. No send capability anywhere. The human is the only sender.**

> 📌 **Working on this project? Read [`CLAUDE.md`](./CLAUDE.md) first.** It is the
> single source of truth for current status, architecture, what's done vs.
> stubbed, next priorities, and exact resume steps.

Stack (frozen): Go gateway + state machine · Redis Streams · LangGraph + Ollama (Qwen2.5:14B) · SQLite · Chrome extension (observe + inject only) · Docker Compose.

---

## Prerequisites
- Docker + Docker Compose
- Google Chrome (to load the unpacked extension)
- ~2.5 GB disk for the default Qwen3:4B-Instruct-2507 model (or set a different `MODEL`)

## 1. Start the stack

```bash
cp .env.example .env
make up            # builds + starts redis, ollama, gateway, ai-brain
```

Check the gateway:
```bash
curl localhost:8080/health      # -> {"ok":true}
```

## 2. Pull the local model (first run only)

```bash
make pull-model    # pulls BOTH the base (qwen2.5:3b) and premium (qwen3:4b-instruct-2507) models
```
> **Production default is a low-RAM dual-model setup** (see *Low-RAM mode* below):
> base `MODEL=qwen2.5:3b` for replies + general/recruiter/buyer/sourcing, and
> `PREMIUM_MODEL=qwen3:4b-instruct-2507-q4_K_M` auto-routed only for
> founder/CEO/director profiles when RAM allows. `qwen3:8b` is installed but OFF.
> Do NOT use the hybrid thinking `qwen3:4b`/`qwen3:8b` on CPU — see CLAUDE.md.

## 3a. Prove the flow WITHOUT a browser (fastest)

```bash
make demo
```
This `POST /ingest` → waits for the AI draft → `POST /approve`, printing the suggested reply. In the browser, that approved text is **injected** into the composer; a human still clicks Send.

Manual version:
```bash
curl -X POST localhost:8080/ingest -H 'Content-Type: application/json' \
  -d '{"thread_id":"demo","text":"Hi! Are you free Thursday to chat about the role?"}'

curl localhost:8080/draft/demo          # poll until "state":"PENDING_APPROVAL"

curl -X POST localhost:8080/approve -H 'Content-Type: application/json' \
  -d '{"thread_id":"demo","decision":"approve"}'   # returns inject_text — NOT a send
```

## 3b. Prove the flow IN LinkedIn (full demo)

1. Open `chrome://extensions`, enable **Developer mode**, click **Load unpacked**, select
   `services/extension/`.
2. Open a LinkedIn message thread: `https://www.linkedin.com/messaging/...`
3. A **LinkedIn Copilot** panel appears bottom-right.
4. Click **Capture** (or paste the thread text), then **Get Suggestion**.
5. The local model drafts a reply → it shows under **Suggested Reply**.
6. Edit if you like, then **Approve → Inject**. The text is placed in the composer.
7. **You** click LinkedIn's **Send**. The extension observes the new outbound bubble and
   records `SENT_BY_HUMAN`. The extension never sends.

## Flow & endpoints

```
extension --POST /ingest-->            gateway --message.received--> redis
                                          gateway <-draft.generated- ai-brain (LangGraph+Ollama)
extension --GET /draft/{id}-->         gateway (state: PENDING_APPROVAL, draft text)
extension --POST /approve-->           gateway (-> READY_FOR_INJECTION, returns inject_text)
extension  (injects text only)         gateway --POST /injected--> INJECTED
human clicks Send (only sender)        gateway --POST /observed--> SENT_BY_HUMAN
```

| Method | Path | Purpose |
|---|---|---|
| POST | `/ingest` | persist thread, publish `message.received` |
| GET | `/draft/{thread_id}` | current state + draft (extension polls) |
| POST | `/approve` | mandatory gate; returns `inject_text` (never a send) |
| POST | `/injected` | mark INJECTED |
| POST | `/observed` | record human send → SENT_BY_HUMAN |

## Safety guarantees in this MVP
- **Approval is mandatory:** `/approve` is the only path past `PENDING_APPROVAL`; no state reaches injection without it.
- **No send capability:** the gateway links no LinkedIn client; the extension has no `.click()` on Send, no `submit()`, no synthetic Enter, no LinkedIn `fetch`. Its only DOM write is composer text.
- **Local-first:** Redis, Ollama, SQLite, and the gateway all run on your machine. No cloud LLM.

## Low-RAM mode (HP EliteBook 840 G1 / 8GB, CPU-only)

This is the **shipped production profile** — stability first on an 8GB, 2C/4T,
CPU-only laptop (the old "4B everywhere" config OOM-killed the AI worker).

**Recommended `.env`:**
```ini
MODEL=qwen2.5:3b                              # base: replies + general/recruiter/buyer/sourcing
PREMIUM_MODEL=qwen3:4b-instruct-2507-q4_K_M   # premium: founder/CEO/director/owner/VP only
PREMIUM_ROUTING=true                          # set false to pin everything to base (max stability)
FAST_MODE=true                                # best-of-1 + token caps + compact prompts
SEQUENTIAL_MODE=true                          # strict one-profile-at-a-time (never 2 generations at once)
RAM_MIN_FREE_MB=1200                          # below this free RAM -> defensive mode
MODE=general                                  # or: fashion (apparel sourcing vertical)
```

**How it stays stable**
- **Sequential generation** (`SEQUENTIAL_MODE=true`, forced on by `FAST_MODE`):
  profiles are drafted strictly **one at a time** — profile #2 doesn't start until
  #1 is fully done (draft → grounding → review state). There are never two Ollama
  generations in flight, so the CPU is never oversubscribed (no hallucination
  spikes from an overloaded box). A scan of N queues them and they drain in order.
- **Persona/style detection** (priority ladder): Founder/CEO → Recruiter/Talent/
  Advisory → Buyer/Sourcing → generic networking. A founder or talent/advisory
  profile is **never** mis-tagged as apparel sourcing, and `MODE=fashion` only
  applies the apparel voice to genuinely apparel/sourcing profiles. Each draft
  shows its **Detected style + confidence (high/medium/low)** in the queue.
- **Smart routing**: premium profiles (founder/CEO/director/owner) use the
  stronger 4B; **buyer/sourcing/merchandising and everyone else** use the lighter,
  ~1.4× faster 3B (even a "Director of Sourcing" stays on base). Only one model is
  in RAM at a time (`OLLAMA_MAX_LOADED_MODELS=1`); switching evicts the other.
- **RAM watchdog**: when free host RAM drops below `RAM_MIN_FREE_MB`, the AI worker
  enters *defensive mode* — forces the base model, drops to best-of-1, unloads the
  premium model, and briefly pauses the queue. It degrades instead of OOM-ing.
- **Auto-recovery**: every service is `restart: unless-stopped`; `ollama` is capped
  at `mem_limit: 6g` so it can never trigger the host OOM-killer. If the worker is
  ever killed, Docker restarts it and Redis redelivers the in-flight draft (no loss).
- **Grounding is untouched** — even base-model best-of-1 can't ship a fabricated
  claim (the deterministic validator + grounded fallback still backstop every draft).

**Expected drafting times** (warm; the 840 G1 is ~1.5–2× slower than a modern box):
| Draft | Model | ~Time |
|---|---|---|
| Messaging reply / general outreach | base 3B | ~12–25s |
| Founder/CEO outreach | premium 4B | ~30–60s (incl. model swap) |
| Mixed batch of 4 (2 founder + 2 buyer/sourcing) | both | ~2 min |

**Recommendation:** scan **1–2 profiles per batch** on this hardware (`make doctor`
prints a recommended limit for your box). For more speed, set `PREMIUM_ROUTING=false`.

**Health check anytime:** `make doctor` shows service health, the active model
strategy, which model ollama holds in RAM, host memory/load, per-container usage,
queue counts, and a recommended scan limit.

## Useful commands
```bash
make doctor    # health + model strategy + RAM/load + queue + recommended scan limit
make logs      # tail gateway + ai-brain
make ps        # service status
make restart   # restart gateway + ai-brain (after an .env change)
make down      # stop everything
```
> First run / after a code change: `make up` (`docker-compose up --build -d`). If
> you hit `KeyError: 'ContainerConfig'` (a compose-v1 recreate quirk), run
> `docker-compose rm -sf && docker-compose build && docker-compose up -d`.

## MVP stubs (intentional, see `safety/` frozen specs for the full design)
- Approval tokens (Ed25519 single-use) are stubbed; the gate is enforced by state, not yet by token.
- Memory retrieval and meeting logic are stubs.
- Auth is fake/local. One draft per thread.
- Send-observation is best-effort (first DOM mutation after injection).
