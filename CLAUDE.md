# CLAUDE.md — Persistent Project Memory

> **READ THIS FIRST, every session, before doing anything.**
> This file is the single source of truth for project state. Chat history is
> assumed to disappear. If this file and your memory disagree, this file wins
> unless you have just verified otherwise against the live code/stack.
>
> Last updated: **2026-06-07** (low-RAM stability mode + smart model routing) · Maintainer: assistant (auto-maintained)

---

# Low-RAM / Stability Mode (HP EliteBook 840 G1) — PRODUCTION DEFAULT (2026-06-07)

> **This is now the shipped production profile.** Goal: STABILITY FIRST on an
> 8GB, 2C/4T, CPU-only laptop (the prior config OOM-killed `ai-brain` with Exit
> 137 and crashed the gateway). Verified live this session — see numbers below.

**What changed (all data/config + ai-brain logic; NO architecture change, NO
frozen file touched, safety boundary unchanged — human is still the only sender):**

1. **Dual-model strategy + smart routing** (`.env`, `worker.py`):
   - `MODEL=qwen2.5:3b` is the **base** model (messaging replies + general /
     recruiter / **buyer / sourcing** outreach). ~2.4GB loaded, ~1.4× faster.
   - `PREMIUM_MODEL=qwen3:4b-instruct-2507-q4_K_M` is used **only** for high-value
     profiles (**founder / CEO / director / owner / VP**) via `model_for()`, and
     **only when free RAM allows**. `qwen3:8b` stays INSTALLED but OFF.
   - `PREMIUM_ROUTING=true` toggles routing; `false` pins everything to base.
   - Routing rule (`is_premium_profile`): style `founder` OR senior-title keyword
     ⇒ premium; styles `recruiter/sourcing/sales` ALWAYS base (so a "Sourcing
     Director" stays base, not mis-promoted by the word "director").
2. **RAM watchdog / OOM prevention** (`worker.py`): `ram_pressure()` reads host
   `/proc/meminfo` `MemAvailable` (the signal that matters — ollama's load is
   host-wide, invisible to ai-brain's own cgroup). Below `RAM_MIN_FREE_MB` (default
   1200) the brain enters **DEFENSIVE MODE**: force base model, best-of-1, no
   length-regen, **unload the premium model** (`keep_alive:0`), and **pause the
   queue** `QUEUE_THROTTLE_SEC` between drafts. It degrades, never crashes.
3. **Never-crash generation**: `generate_with_fallback()` transparently retries on
   the base model if the premium model fails for ANY reason (not pulled / load
   error / OOM / timeout). The consumer loop already acks+continues per message.
4. **Explicit model unload on switch** (`ollama_unload`): with
   `OLLAMA_MAX_LOADED_MODELS=1` ollama evicts anyway, but we evict the old model
   FIRST so RAM is freed before the new (possibly larger) one loads — safer on 8GB.
5. **docker-compose hardening** (`version: "2.4"` so `mem_limit` is honored by
   compose v1 AND v2): `restart: unless-stopped` on every service (this IS the
   "restart ai-brain on Exit 137" watchdog — Redis consumer groups redeliver
   unacked drafts, so no work is lost); **`mem_limit`** ollama=6g (the key OOM
   guard — ollama can't grow big enough to trigger the host OOM-killer),
   ai-brain=1200m, gateway/redis=256m; healthchecks on ollama (`ollama ps`),
   ai-brain (redis ping), redis; `OLLAMA_KEEP_ALIVE=15m` (was 30m),
   `NUM_PARALLEL=1`, `MAX_LOADED_MODELS=1`.
6. **Observability**: `make doctor` (service health, model strategy, ollama
   loaded-model, host RAM/load, per-container usage, queue counts, recommended
   scan limit) + new read-only gateway `GET /stats` ({mode, fast_mode, base_model,
   premium_model, premium_routing, outreach/draft state counts}).

7. **SEQUENTIAL_MODE** (`SEQUENTIAL_MODE=true`, default ON; **FAST_MODE forces it**):
   strict ONE-profile-at-a-time generation. The consumer reads with `count=1`
   (`READ_COUNT`), so it fully finishes one profile (draft → grounding validation
   → `outreach.generated`) before pulling the next — there are **never two Ollama
   generations in flight**. A batch of N profiles is published to Redis at once
   and **drained sequentially** (profile #2 doesn't start until #1 is in its
   review state). Surfaced in `/config` + `/stats` (`sequential_mode`); the
   extension then defaults the scan limit to **1** and shows a "🐢 Sequential
   mode" hint. Routing rule sharpened: `BASE_FORCE_KW` keeps **buyer / sourcing /
   merchandising / procurement** on the base model **even with a senior title**
   ("Director of Sourcing" → base, not premium). Verified live: a 3-profile batch
   logged `[seq] start sq-1 → routing founder→premium → outreach.generated → [seq]
   start sq-2 → routing sourcing→base → …` with `ollama ps` never showing two
   models, restarts=0.

**Fallback behavior summary:** premium-model failure → base model (auto). RAM
pressure → base model + best-of-1 + premium unloaded + queue paused. ai-brain
killed → docker restarts it, Redis redelivers the in-flight draft. Concurrency →
strictly one generation at a time (SEQUENTIAL_MODE). Hallucination guard (grounding
validator + `claim_inventions` + grounded fallback) is UNCHANGED, so even
base-model best-of-1 can't ship a fabricated claim.

**Verified live 2026-06-07 (this 4-core/11.6GB dev box; the 840 G1 is ~1.5–2× slower):**
| Check | Result |
|---|---|
| Stack up, all healthy | redis/ollama/ai-brain `healthy`, gateway `{ok:true}` |
| mem_limits applied | ollama 6g, ai-brain 1.2g, gateway/redis 256m |
| Messaging reply (`make demo`, base) | OK, 25s cold |
| Single founder outreach (`make demo-outreach`) | **routed to premium 4b**, base unloaded first, grounding 1.0, 55s cold |
| Mixed fashion batch (4: 2 founder + buyer + sourcing) | **127s**, founders→premium / buyer+sourcing→base, clean model swaps |
| RAM during batch | ollama peak **3.17GiB / 6GiB cap**, host ~5.2GB free |
| **OOM / crashes** | **restarts=0 on every service, 0 OOM** |
| `approve_all` | all → READY_FOR_INJECTION |
| limit=1 batch | OK |
| Defensive path (forced pressure, deployed code) | founder → `qwen2.5:3b`, `defensive=True` |

**Before vs after (per the prior session + this one):**
| Config | 8GB-box behavior | Speed |
|---|---|---|
| OLD: `qwen3:4b-instruct` everywhere, no mem cap | **ai-brain OOM (Exit 137)**, gateway crash | 122s/5 warm when it ran |
| NEW: 3b base + 4b premium routing, ollama mem_limit 6g, restart policy | **0 OOM, 0 crashes, auto-recover** | base draft ~12–25s warm; mixed 4-batch 127s; premium founder ~55s cold |

**Rollback / tuning (one `.env` edit + `make restart`):** `PREMIUM_ROUTING=false`
(base only, max stability) · `MODEL=qwen3:4b-instruct-2507-q4_K_M` (4b everywhere)
· raise `RAM_MIN_FREE_MB` to 1500 on a busier box · `PREMIUM_MODEL=qwen3:8b` (OFF
by default; needs ~6GB+, risky on 8GB).

---

# Project Overview

**Local-first LinkedIn AI Executive Assistant.** A fully on-device copilot that
captures a LinkedIn message thread, drafts a reply with a **local** LLM (Ollama /
Qwen2.5), routes that draft through a **mandatory human approval gate**, and then
**injects** the approved text into LinkedIn's composer. **A human is the only
sender.** There is no auto-send, no LinkedIn API write path, and no send
capability anywhere in the system. Stack: Go gateway + state machine, Redis
Streams event bus, LangGraph/LangChain orchestration, Ollama local inference,
SQLite persistence, a Chrome/Brave extension (observe + inject only), all wired
together with Docker Compose.

**Core philosophy (non-negotiable):** AI drafts → Human approves → Human sends.
No autonomous outbound messaging. No hidden automation.

---

# Current Status

## 🟢 Working
- **Model stack upgraded to Qwen3 (NEW, 2026-06-06)**: production default is now
  **`qwen3:4b-instruct-2507-q4_K_M`** (latest stable Qwen3, non-thinking instruct),
  chosen by a real-pipeline benchmark over qwen2.5:3b for stronger grounding and
  near-zero hallucination. Rollback to qwen2.5:3b is one `.env` uncomment + `make
  restart`. Hybrid thinking qwen3:4b/8b were tested and rejected (unusable on
  CPU). All flows re-verified green. Full detail + numbers in **"# Model Stack"**.
- **Outreach GROUNDING fix (NEW, 2026-06-06)**: kills the hallucinated-context bug
  (drafts inventing industries/details not in the profile). ai-brain now has a
  deterministic grounding validator (`grounded_set` / `grounding_check` / acronym
  bleed guard), a hard grounding gate in best-of-N (`#ungrounded` is the primary
  score key after banned-phrases), a regeneration nudge that names the offending
  invented terms, and — when the best draft still invents ≥2 terms — a
  **grounded fallback that quotes the person's literal headline** (true +
  personalized). Each draft is generated independently (fresh graph state; no
  cross-profile carry-over) and the prompt forbids reusing the few-shot examples'
  facts. The brain emits `key_phrases` (grounded terms actually used) +
  `grounding` (0..1 confidence); the gateway persists+serves them; the extension
  shows a **debug preview** ("grounded on: …· grounding NN%", ⚠ if < 50%) beside
  each draft in the batch queue AND on the single-profile panel. Verified live on
  the exact reported cases (Gianluca/MLG CTO no longer invents finance; apparel
  profile no longer bleeds "GCC"); all browserless flows green, no regression.
- **Injection-flow hardening pass (NEW, 2026-06-06, extension-only)**: the live
  "Add a note → Inject" path is now modal-scoped and reason-coded. `inviteModal()`
  finds the OPEN, VISIBLE invite dialog (fixed-position-safe visibility check) and
  scopes the note-field lookup to it (no more grabbing an unrelated modal's
  textarea). `injectIntoConnectionNote(text, expectedName)` returns
  `{ok, reason, recipient}` with reasons `no-modal | no-textarea | wrong-profile
  | verify-failed | ok`, post-write VERIFIES the value actually took, and refuses
  to fill when the open invite's recipient (parsed from "Invite <Name> to
  connect") confidently mismatches the intended person (right note → right
  person; permissive when the name can't be read). Graceful, actionable status +
  a success/failure **toast** for every outcome; Approve/Inject buttons show a
  loading state. Batch queue gained emoji **state badges** (⏳ queued / ✍️
  drafting / 📝 review / ✅ approved / 📥 injected / 🤝 connected / ✕ rejected)
  and a per-item **live char counter** (120–180 target) so edit-before-approve is
  explicit. Backend/ai-brain/frozen-safety files UNTOUCHED; `node --check` clean;
  forbidden-API scan clean (still no click/submit/Enter/LinkedIn-fetch). All
  three browserless flows re-verified green (no regression). **Still pending: a
  real-LinkedIn DOM test** — the guards are coded but `inviteModal()` /
  `inviteModalRecipient()` selectors are best-effort against the live modal.
- **Phase 2 — Batch Outreach Queue (NEW, verified 2026-06-04)**: scan a LinkedIn
  search/connections/people-list page → AI bulk-drafts personalized connection
  notes for every visible profile → inbox-style **approval queue** with per-item
  Approve/Edit/Reject + Approve-All/Reject-All → approved items become
  `READY_FOR_INJECTION` → human opens each profile's *Add a note* → **Inject**
  (note text only) → human clicks Connect. **AI never sends, never clicks
  Connect.** Reuses the SAME event bus, state machine, and ai-brain — a pure
  queue layer. Browserless end-to-end verified with 5 profiles: all drafted
  (correct names, styles auto-detected), reject / approve-with-edit / approve-all
  / inject / observe lifecycle all confirmed, 409 guard on rejected items.
  Client-side: limit (5/10/20/50/custom), headline filters
  (recruiter/founder/hiring-manager/sales/custom), dedupe of already-processed
  profiles, and a daily cap (10/20/50) — all in the extension.
- **Phase 1 — AI Outreach Engine (verified 2026-06-04)**: full cold/new-
  thread connection-request path, reusing the existing event bus + state machine
  and the SAME mandatory approval gate. Browserless end-to-end confirmed
  (`make demo-outreach`): `/profile-ingest` → AI drafts a personalized connection
  note via Ollama (style auto-detected, e.g. `founder`) → `OUTREACH_READY`
  → `/approve-outreach` → returns `inject_text` for the connection NOTE field
  (never a send). Full lifecycle verified through `CONNECTION_SENT_BY_HUMAN`;
  reject path and the not-awaiting-approval guard (409) both confirmed. Existing
  messaging flow re-verified unbroken (no regression).
- **Full stack runs under Docker Compose** — `redis`, `ollama`, `gateway`,
  `ai-brain` all confirmed `Up` (verified 2026-06-04).
- **Gateway health**: `curl localhost:8080/health` → `{"ok":true}`.
- **End-to-end browserless flow** (`make demo`): `/ingest` → AI draft via Ollama
  → `PENDING_APPROVAL` → `/approve` → returns `inject_text` (never a send).
- **Go state machine + SQLite persistence**: threads + drafts tables, state
  spine RECEIVED → DRAFTING → PENDING_APPROVAL → APPROVED → READY_FOR_INJECTION
  → INJECTED → SENT_BY_HUMAN (+ REJECTED).
- **Redis Streams event bus**: gateway publishes to `stream:message_ingest`,
  ai-brain consumes it and publishes to `stream:cognition_result`, gateway
  consumes that. Consumer groups: `cg:gateway`, `cg:ai-brain`.
- **AI Brain (LangGraph)**: classify → retrieve_memory(stub) → generate_draft
  via Ollama. Has graceful fallbacks (inline nodes if LangGraph missing; raw
  `/api/generate` HTTP if `langchain-ollama` missing).
- **Chrome extension (MV3)**: panel UI, Capture, Get Suggestion, Approve→Inject,
  Reject, and passive human-send observation. Talks ONLY to `localhost:8080`.
- **Local model pulled**: `qwen2.5:3b` present in the Ollama volume.
- **Frozen safety spec corpus** authored in `docs/` and `safety/` (invariants,
  state-transition fixtures, forbidden-API list, tool allowlist, event schemas).

## 🟡 In Progress
- This `CLAUDE.md` persistent-memory file (created this session).
- Hardening the MVP stubs toward the frozen spec (see "MVP stubs" below).

## 🔴 Broken / Known Issues
- **Phase 1 outreach not yet tested on a live LinkedIn profile**: backend + the
  extension's outreach panel are fully wired and the browserless flow passes, but
  the profile-capture selectors and the connection-NOTE-field selector are
  best-effort against LinkedIn's (obfuscated, changing) DOM. The note injection
  requires the user to first open **Connect → Add a note** so the note `textarea`
  exists; if it isn't open, Approve reports "No note field found." Captured fields
  are always editable/pasteable in the panel as a fallback.
- **One outreach per profile**: re-ingesting a profile overwrites its draft/state
  (same MVP simplification as one-draft-per-thread on the messaging side).
- **Outreach length target is 120-180 chars**, enforced by best-of-3 scoring in
  `generate_outreach` (picks the draft closest to the band with the fewest banned
  phrases). The 3B model usually lands in-band now; 14B is more consistent.
- **3B outreach hallucination — now caught by a deterministic GROUNDING VALIDATOR
  (2026-06-06).** Previously the 3B model invented plausible-but-absent details
  ("sustainability initiatives", "Karachi's textile scene", "financial services /
  customer experience" on a CTO-only profile) and best-of-N couldn't detect it.
  Now `grounding_check()` in ai-brain compares every draft's significant
  content-words (incl. short ALL-CAPS acronyms, to catch few-shot bleed like
  "GCC") against the tokens that literally appear in the profile, scores a 0..1
  **grounding confidence**, and folds the #ungrounded terms into best-of-N scoring
  as the primary gate after banned-phrases. If the best draft still invents **≥2**
  terms, the brain ships a **deterministic grounded fallback** that quotes the
  person's literal headline instead (personalized + true, never a fabricated
  domain). A lone stray term ships but is surfaced (confidence < 1.0) in the
  approval-queue **debug preview** for the human to catch. Verified: the reported
  Gianluca/MLG and apparel/GCC cases no longer fabricate. Residual: a single
  generic over-reach (e.g. "tech" for a CTO) can still ship at ~0.75 confidence —
  visible, not silent. 14B grounds tighter still.
- **Model decision RESOLVED (2026-06-06)**: production default is now
  `qwen3:4b-instruct-2507-q4_K_M` (benchmarked winner — see "# Model Stack"). The
  old `.env=qwen2.5:3b` vs frozen-`14b` mismatch is retired; `.env` documents the
  rollback path. The hybrid thinking `qwen3:4b`/`qwen3:8b` are pulled but NOT used
  (unbounded reasoning on CPU — see Model Stack section). Pulled now: qwen2.5:3b,
  qwen3:4b-instruct-2507-q4_K_M (default), qwen3:4b, qwen3:8b, qwen3:4b-instruct.
- **Approval tokens are STUBBED**: the gate is enforced by *state* only, not yet
  by the Ed25519 single-use token described in the frozen specs.
- **Memory retrieval is a STUB** (returns a hardcoded note). No vector store yet.
- **Meeting / follow-up logic is a STUB** (intent is classified but not acted on).
- **Auth is fake/local**; one draft per thread (overwrites on re-ingest).
- **Send-observation is best-effort**: fires on the first DOM mutation after
  injection, which can be a false positive (not strictly the outbound bubble).
- **No automated tests / CI parity gate** wired yet despite spec calling for one.
- `docs/adr/` is **empty** — no ADRs written despite specs referencing them.

## ⛔ Blockers
- None hard-blocking. Largest open decision: 14B vs 3B model for the client demo
  (disk/latency tradeoff).

---

# Model Stack — Production Model Decision (Qwen3, 2026-06-06)

**PRODUCTION DEFAULT: `qwen3:4b-instruct-2507-q4_K_M`** (latest stable Qwen3,
NON-thinking instruct variant, ~2.5GB on disk). Set in `.env`, `.env.example`,
`docker-compose.yml` (`${MODEL:-…}`), `Makefile` (`pull-model`), and the
`worker.py` getenv default. **Rollback** is one uncomment away (see `.env`):
`qwen2.5:3b` (previous default) or `qwen2.5:7b`. The qwen2.5:3b weights are still
pulled, so rollback is instant: edit `.env` → `make restart`.

## Why NOT the hybrid thinking Qwen3 (the important finding)
The headline "latest Qwen3" models `qwen3:4b` and `qwen3:8b` on ollama are
**HYBRID THINKING** models. On this stack (ollama 0.30.0, CPU-only) their
reasoning is **not reliably suppressible**:
- `think:false` (API) → no `<think>` tags, but the model still reasons in PLAIN
  PROSE in the response body ("Okay, the user wants…"), burning the whole token
  budget without ever emitting the note.
- `/no_think` soft switch → works only NON-deterministically (one call stopped at
  44 tokens, an identical call ran away to 287+).
- Measured throughput ~1.4–2.4 tok/s on this 4-core box; a single thinking draft
  decoded **1,500–2,100+ tokens** → **minutes per draft**, with best-of-3 making
  it 5–30 min. Disqualifying for "outreach at scale".
The fix shipped in `worker.py` (`IS_QWEN3_THINKING`, `/no_think` + `think:false`
+ `QWEN3_NUM_PREDICT=220` cap + `strip_thinking()`) bounds the damage, but the
right call is to **use the non-thinking INSTRUCT variant**, which needs none of
that. NB: ollama has **no `qwen3:8b-instruct`** — the only small non-thinking
Qwen3 is the 4B instruct. (Instruct + Qwen2.5 take the clean code path unchanged,
so the default AND the rollback are byte-for-byte safe.)

## Benchmark (real `generate_outreach` pipeline; grounding scored by the
## production `grounding_check`; this 4-core / ~11.6GB-RAM / NO-GPU box)
5 realistic founder profiles (2 thin hallucination-traps + 3 rich), best-of-N on:

| Model | Avg latency | In-band 120-180 | Avg grounding | Real fabrications | Perfectly-clean notes |
|---|---|---|---|---|---|
| qwen2.5:3b (old default) | 77.6s | 3/5 | 0.61 | **invented** Cloudflare / agritech / finance on thin cards → forced fallbacks | 0/5 |
| **qwen3:4b-instruct-2507 (WINNER)** | 113s (≈84s warm; 229s incl. cold load) | 3/5 | **0.72** | **none** — stayed on real facts (thin CTO card → "CTO at MLG Group in Milan", no invented industry) | **2/5** |

**WINNER: `qwen3:4b-instruct-2507-q4_K_M`.** It wins on the use-case's primary
bar — strict grounding + near-zero hallucination: higher grounding (0.72 vs
0.61), 2/5 vs 0/5 perfectly-clean notes, and — critically — it **never fabricated
a company/industry on thin profiles**, the exact failure 3b exhibits (3b invented
"Cloudflare's Product Management team" for a bare "Product Manager" card). Same
in-band rate. Cost: ~45% slower warm (≈84s vs ≈58s/draft) and +~2.3GB RAM —
acceptable for a HUMAN-reviewed approval queue (drafts are never auto-sent).

## Resource usage (measured via `docker stats` on the ollama container)
| Model | Disk | Loaded (ollama ps) | Peak container RAM | CPU |
|---|---|---|---|---|
| qwen2.5:3b | 1.93 GB | 2.4 GB | ~2.2 GiB | ~2 cores (≈200%) |
| qwen3:4b-instruct-2507 | 2.50 GB | 3.4 GB | ~4.5 GiB | ~2 cores (≈200%) |
Both fit comfortably in 11.6 GiB. (14B was NOT benchmarked live — Q4 ≈9GB would
sit at the RAM ceiling beside the other containers; impractical on this box.)

## Verification on the new model (browserless, all green 2026-06-06)
- **Messaging** (`make demo`): draft → `PENDING_APPROVAL` → `/approve` →
  `inject_text` / `READY_FOR_INJECTION`. Clean direct draft, no reasoning leak.
- **Single outreach** (`make demo-outreach`): grounded note (grounding 0.88, all
  key_phrases real), `OUTREACH_READY` → `/approve-outreach` → `inject_text`.
- **Batch + approve_all** (`make demo-batch`): 3 drafts (Loom 1.0, Northwind 1.0,
  thin VP-Sales card → safe grounded fallback, no invention) → `approve_all` →
  all `READY_FOR_INJECTION`.
- **Injection flow**: batch `/batch-injected`→`INJECTED`→`/batch-observed`→
  `CONNECTED_BY_HUMAN`; single `/connection-injected`→`/connection-observed`→
  `CONNECTION_SENT_BY_HUMAN`. Human is still the only sender — unchanged.

## 10-founder production check (`bench/verify_founders.py`, new model)
10 founder profiles (varied richness + 2 deliberate thin/empty traps). Result:
**0 real fabrications across all 10** — the model never invented a company,
industry, or fact. 7/10 were clean model-authored grounded drafts (in-band,
grounding 0.8-1.0); 2/10 were the safe deterministic fallback on thin/empty cards
(by design — a true short note beats a false specific one); avg grounding **0.79
overall, ~0.94 on model-authored drafts**; 8/10 in the 120-180 band. Avg latency
184.6s (inflated by model-reload outliers; ≈84s warm). Every term the strict
harness flagged as "halluc" was a validator FALSE-POSITIVE on a generic word
(came, close, exactly, sits, structure, exchange) inside a fallback template —
NOT a model fabrication.
**Latent bug found & FIXED (data-only):** the deterministic grounded-fallback
template #3 shipped the literal phrase "let's connect", which is on the system's
own BANNED list (pre-existing; unrelated to the model swap). Rewrote it to
"— would genuinely value connecting."; both `_FALLBACK_TEMPLATES` and
`_FALLBACK_BARE` now scan clean against `BANNED_PHRASES`. Redeployed.

## FAST_MODE — low-resource CPU profile (2026-06-06, HP EliteBook 840 G1 class)
**Problem:** on an old 2C/4T Haswell laptop, ollama saturated all cores (load
avg ~9+) and a 5-profile batch took minutes. **Two root causes fixed:**
1. **CPU oversubscription** — ollama's default multi-slot parallelism. Fixed in
   `docker-compose.yml` ollama env: `OLLAMA_NUM_PARALLEL=1` (one generation at a
   time), `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_KEEP_ALIVE=30m` (keeps the model
   warm → kills the 200–390s reload-latency spikes). **Load avg dropped 9+ → ~2–4.**
2. **Per-draft cost** — best-of-3 + length-chasing regen + a ~666-token system
   prompt re-prefilled every call. Fixed with `FAST_MODE` (env, default false;
   `=true` in this `.env`). Generation is SEQUENTIAL (the consumer loop is serial;
   ollama pinned to 1 slot).

`FAST_MODE` behaviour (in `worker.py`):
- **best-of-N → 1** for every style EXCEPT `founder` (kept at **best-of-2** for
  quality, per requirement). `outreach_budget(style)` sets attempts + token cap.
- **No length-only regeneration** — a clean, grounded draft ships even if a bit
  short; a 2nd generation happens ONLY for a real CONTENT problem (spam / no real
  fact / hallucination), and only on the founder tier.
- **Token caps** (`num_predict`: 120 fast / 170 founder / 150 messaging) bound decode.
- **Compact prompt** `OUTREACH_SYSTEM_FAST` (~1/5 the tokens, no few-shots) — the
  big prompt's PREFILL was the dominant CPU cost. This alone cut 274s → 122s.
- **Grounding safety is UNCHANGED**: the deterministic validator + grounded
  fallback still backstop every draft, so a hallucination never ships even at
  best-of-1. Recruiter/sales/etc. fast-path; founder stays higher quality.

**Measured (this 4-core dev box, warm, 5 mixed drafts):**
| Config | 5-draft wall time | Peak load | Grounding |
|---|---|---|---|
| qwen3:4b-instruct, no FAST (cold) | 274s | — | 0.6–1.0 |
| **qwen3:4b-instruct + FAST** (current default) | **122s** | ~4 | 0.6–1.0, 0 fabrications |
| **qwen2.5:3b + FAST** | **82s** (~12–15s/draft warm) | ~3 | 0.6–1.0, all hallucinations caught by fallback |

**To hit the 30–90s target on the 840 G1** (≈1.5–2× slower than this box): pair
`FAST_MODE=true` with **`MODEL=qwen2.5:3b`** (the configured rollback) — 82s here,
fallback-protected. With qwen3:4b-instruct + FAST you get ~2 min/5 drafts (higher
native grounding, founder quality). Pick by priority: speed-to-target → 3b;
strict grounding/quality → 4b-instruct. Both flip via one line in `.env` +
`make restart`. (The async, human-reviewed queue means neither is "real-time".)

## FASHION MODE — apparel/fashion sourcing vertical (2026-06-06, `MODE=fashion`)
Specializes the outreach copilot for **UAE/GCC fashion manufacturing & apparel
sourcing** — fashion founders, brand owners, buyers, purchasing/sourcing/
merchandising leads. Set `MODE=fashion` (in `.env`; `general` = default). Wired
through `docker-compose.yml` `${MODE:-general}` → ai-brain env. Data-only; the
gateway, state machine, streams, and safety boundary are UNCHANGED.

What `MODE=fashion` changes in `worker.py`:
- **Voice**: `OUTREACH_SYSTEM_FASHION` (+ `_FAST`) — B2B apparel sourcing /
  manufacturing / partnership tone (private label, fashion supply chain, textile
  sourcing, production partnerships, buyer-manufacturer collaboration, scaling
  apparel sourcing). Bans generic networking ("would love to connect", "your
  profile aligns").
- **Style auto-detect** (`detect_style`): founders/brand owners → `founder`
  (high-quality tier, best-of-2); buyers/purchasing/sourcing/merchandising/
  procurement → new `sourcing` style (fast tier, best-of-1). This IS the
  requested DUAL MODE. Default style in fashion is `sourcing`, never generic
  networking.
- **Grounding (the hard part)** — two-layer, so notes are industry-specific AND
  strictly grounded:
  - `FASHION_SAFE_VOCAB` (industry + B2B sourcing framing: apparel, garment,
    textile, sourcing, private label, supply chain, production, operations,
    scalable, lead-time, …). A fashion word that IS in the profile is CREDITED as
    a real fact; the same word as generic framing is allowed, NOT flagged. (Gotcha
    fixed: do NOT merge this into SAFE_VOCAB — SAFE_VOCAB words are *skipped*, which
    stopped real fashion facts from counting as "used" and forced every draft to
    the fallback. It's a separate `_FRAMING_VOCAB` consulted only after the
    grounded check.)
  - `FASHION_CLAIM_BLOCKLIST` + `claim_inventions()` — specific-claim words
    (sustainability, organic, ethical, certified, luxury, award-winning, leading,
    iconic, …) MUST be grounded or the draft is rejected. In fashion mode the
    "hallucination" gate is this blocklist (ZERO tolerance), NOT the generic
    ungrounded-word count — because sourcing prose is naturally wordy and gating
    on every generic descriptor forced good drafts into the fallback. Net: invents
    a brand/sustainability/category → caught; ordinary sourcing language → ships.
- **Fashion fallbacks** (`_FALLBACK_TEMPLATES_FASHION`): when a draft is rejected,
  the deterministic fallback is itself sourcing-voiced ("…sits right next to my
  own in apparel sourcing — would value a quick conversation"), so even the safety
  net reads like a real sourcing note, never generic networking.

Extension (`content.js`): added the **`fashion` filter keyword set** (fashion,
apparel, garment, textile, buyer, merchandiser, sourcing, procurement, purchasing,
supply chain, head/director of sourcing, private label, wholesale) — checked by
default — and **3 one-click search presets** that open a LinkedIn people-search
(plain user-clicked navigation, NOT a fetch/automation): "Fashion Founders UAE",
"Fashion Buyers UAE", "Sourcing Decision Makers". A preset pre-checks the fashion
filter on the results page. Forbidden-API scan still clean (no click/submit/Enter/
LinkedIn-fetch; `location.href` navigation is not on the forbidden list).

Verified (MODE=fashion, FAST, qwen3:4b-instruct): a 6-persona batch (founder +
sourcing/buyer/procurement/merchandising) drafts grounded apparel-sourcing notes
(model draft when clean, sourcing-voiced fallback otherwise); style routing
correct (founder vs sourcing tier); `claim_inventions` unit-checked — "sustainable
award-winning" rejected, "private label garment sourcing" accepted. To revert to
general networking voice: `MODE=general` in `.env` + `make restart`.

## Scan-limit selector + low-CPU recommendation (2026-06-06)
The batch-queue **Limit** dropdown now allows **1, 2, 3, 5, 10, 15, 20** (was
5/10/20/50 — minimum was locked at 5). Minimum is **1** for slow CPUs and safer
manual review. The default is **mode-aware**, fetched from the gateway:
- New gateway endpoint **`GET /config`** → `{"mode": "...", "fast_mode": bool}`
  (reads the `MODE`/`FAST_MODE` env now also passed to the gateway service). Read-
  only, no state, no send capability.
- Extension (`content.js`, queue panel mount) calls it via the new background
  `config` action and sets the default Limit: **2 when `MODE=fashion`, 5 for
  general** (never overrides a choice the user already made this session).
- **Low-CPU helper text**: when `FAST_MODE=true` (the CPU-inference / HP EliteBook
  840 G1 profile — `FAST_MODE` is the proxy signal; true GPU-vs-CPU detection
  isn't available client-side), the panel shows an amber hint: *"FAST_MODE (CPU
  inference) — keep to 1–2 drafts per scan for speed and safer manual review."*
- **No backend floor**: the gateway never validated a minimum limit — `limit` is
  metadata (`batches.limit_n`); the real driver is `len(profiles)` (hard-capped at
  50, no minimum). The worker never reads `limit`. So `limit=1`/`limit=2` were
  always accepted; verified end-to-end (scan→queue→generate→approve_all→inject→
  observe) at both limit=1 and limit=2 on `MODE=fashion`+`FAST_MODE`.
- `selectedLimit()` unchanged in logic: custom number input (`min="1"`) wins if
  >0 (clamped to 50), else the dropdown value.

**Low-CPU usage recommendation (HP EliteBook 840 G1 / 2C-4T, no GPU):** run
`FAST_MODE=true` and scan **1–2 profiles at a time** (Limit 1 or 2). At ~12–25s/
draft warm that keeps a scan under ~30–50s and lets you review each note before
connecting. Daily cap still applies on top. For more throughput on the same box,
`MODEL=qwen2.5:3b` (rollback) is ~1.4× faster than the 4B default.

## Extension DOM-logic verification (`bench/dom_verify.js`, 2026-06-06)
Loads the REAL `services/extension/content.js` into jsdom (shipped file untouched;
a test-export hook is injected at load time) and exercises every selector + guard
against realistic LinkedIn fixtures. **31/31 PASS**: `scanProfiles` (reads
headline/location, mutual COUNT-only, dedupes within a scan, skips Connect/avatar/
"LinkedIn Member" anchors); `inviteModal` (picks the VISIBLE invite, ignores a
hidden unrelated modal); `inviteModalRecipient` parse; `connectionNoteField`
modal-scoping; `injectIntoConnectionNote` happy-path + **verify-after-write** +
non-empty; **wrong-profile guard** (refuses on recipient mismatch, leaves textarea
EMPTY, surfaces the real recipient); no-modal / no-textarea reasons; `nameMatches`
permissiveness; daily-cap accrual + dedupe persistence (chrome.storage stub);
`pageType` routing. Run: `node bench/dom_verify.js` (needs jsdom; installed at
`/tmp/lc-domtest`). **What this canNOT prove:** that the selectors match
LinkedIn's CURRENT obfuscated live class names — only a human on a real page can
confirm that last mile (LinkedIn changes these often). See the live runbook below.

## Live-LinkedIn last mile (still NOT auto-verifiable; human-required by design)
The full live flow (scan → queue → approve_all → open profile → Connect → Add a
note → Inject → human Connect) **cannot be run by the agent**: it needs an
authenticated LinkedIn session and it fires REAL invitations to REAL people, which
the frozen safety boundary (human is the only sender) and LinkedIn ToS forbid
automating. The selector/guard LOGIC is proven (above); the human runbook to close
the last mile in ~10 min is in the session notes (load unpacked extension → a
founder `search/results/people` page → Scan → Generate Queue → Approve All → open
each `/in/<slug>` → Connect → Add a note → click Inject → confirm the right note
lands + green toast + wrong-profile guard fires on the wrong person → human clicks
Connect → observe CONNECTED_BY_HUMAN). Watch DevTools `[LC] scan:` counters if a
scan queues 0.

## Bench artifacts
`bench/benchmark.py`, `bench/bench5.py` (head-to-head), `bench/verify_founders.py`
(10-profile production check), `bench/resource_probe.sh`, `bench/dom_verify.js`
(extension DOM-logic). Python ones run inside the ai-brain container:
`docker cp bench/<f> <ai-brain-cid>:/app/ && docker-compose exec ai-brain python <f>`.

# Implemented Features

| Feature | Status | % |
|---|---|---|
| LinkedIn thread capture (extension) | DONE | 100% |
| AI reply generation (local Qwen via Ollama) | DONE | 100% |
| Human approval gate (mandatory, state-enforced) | DONE | 90% (token unstubbed pending) |
| Inject-only workflow (composer text write, no send) | DONE | 100% |
| `SENT_BY_HUMAN` observation | PARTIAL | 70% (best-effort DOM mutation) |
| Local Ollama inference | DONE | 100% |
| Redis Streams event flow | DONE | 100% |
| Go state machine + SQLite persistence | DONE | 95% (subset of frozen spine) |
| Approval token (Ed25519 single-use) | TODO | 0% (stubbed) |
| Memory retrieval (vector) | TODO | 0% (stubbed) |
| Meeting arrangement / follow-ups | TODO | 0% (intent only) |
| **Outreach (cold/new-thread drafting)** | **DONE** | **95% (Phase 1; backend + extension UI verified, browser injection pending live LinkedIn DOM test)** |
| Outreach: profile capture (extension) | DONE | 92% (name from page title; mutual = COUNT only, names stripped) |
| Outreach: correct-name guarantee | DONE | 100% (explicit name thread + force_correct_greeting backstop) |
| Outreach: style auto-detect (7 styles) | DONE | 100% (heuristic + LLM steer) |
| Outreach: connection-NOTE injection (no Connect click) | DONE | 100% |
| **Phase 2: Batch Outreach Queue** | **DONE** | **90% (backend verified; live-page scan selectors pending real-DOM test)** |
| Batch: profile-card scan (search/connections list) | DONE | 85% (best-effort selectors; card-level fields only) |
| Batch: bulk draft generation | DONE | 100% (reuses per-profile ai-brain) |
| Batch: inbox approval queue (per-item + all) | DONE | 100% |
| Batch: limit / filters / dedupe / daily cap | DONE | 100% (client-side in extension) |
| Batch: per-item inject + human-send observe | DONE | 95% (inject needs modal open on that profile) |
| Follow-ups (FOLLOWUP_PENDING state) | TODO | 0% (status reserved; no sequencing yet) |

---

# Client Requirements

Priority order as given by client. Mark DONE / PARTIAL / TODO.

1. **AI outreach** — DONE (Phase 1 single-profile + Phase 2 batch/at-scale).
   Single: open a profile → Capture → Generate → Approve → inject note. Batch:
   open a search/connections list → Scan → Generate Outreach Queue → approve/
   reject in an inbox → inject per-profile. Human always clicks Connect. No send.
2. **AI replies** — DONE (local Qwen drafts replies to inbound messages).
3. **Follow-ups** — TODO (no scheduling/sequencing logic).
4. **Meeting arrangement** — TODO (intent="meeting" detected, no action taken).
5. **Human approval** — DONE (mandatory gate; `/approve` is the only path past
   `PENDING_APPROVAL`).
6. **Personal-assistant feel** — PARTIAL (warm-tone drafts; no persistent memory
   of user voice/preferences yet — retrieval is stubbed).

---

# Architecture Freeze

**Do NOT redesign these unless explicitly instructed.**

- **State-machine philosophy**: the **Go gateway is the single system authority**.
  All state transitions go through it and are persisted in SQLite. The AI Brain
  produces **data (draft text), never actions**. The extension produces
  **observations and one DOM write (composer text)**, never sends.
- **Safety invariants** (frozen): 22 formal invariants INV-001..022 in
  `safety/invariants.yaml` / `docs/safety-invariant.md`. Prose authority wins on
  conflict. Hand edits require an ADR + security sign-off + recomputed sha256 in
  the same PR.
- **No autonomous send — anywhere**: gateway links no LinkedIn client and exposes
  no send route; the only outbound-adjacent action is returning draft text for
  injection. AI Brain cannot send or approve. Extension has **no `.click()` on
  Send, no `form.submit()/requestSubmit()`, no synthetic Enter/keydown, no fetch
  to linkedin.com** (enforced spec: `safety/frozen/extension-forbidden-api.txt`).
- **Extension security boundary**: content script only reads thread DOM, writes
  composer text + dispatches `input`/`change`, and passively observes. All
  network calls go through the background service worker to `localhost:8080`
  ONLY. See `docs/extension-security-boundary.md`.
- **Approval model**: `PENDING_APPROVAL` is a hard gate. Only `/approve` advances
  to `READY_FOR_INJECTION`. Reject → `REJECTED`. `SENT_BY_HUMAN` is reachable
  ONLY from `INJECTED` (per INV-021).
- **Local-first**: Redis, Ollama, SQLite, gateway all run on-device. No cloud LLM.

---

# File Map

```
linkedin-copilot/
├── CLAUDE.md                       # ← this file (persistent project memory)
├── README.md                       # human setup/run guide (Phase 0 + Phase 1 MVP)
├── docker-compose.yml              # redis + ollama + gateway + ai-brain
├── Makefile                        # up/down/logs/pull-model/demo/ps/restart
├── .env / .env.example             # MODEL + internal addrs (NOTE: .env=3b)
│
├── services/
│   ├── gateway-go/                 # Go gateway = authority + state machine
│   │   ├── main.go                 #   HTTP API, SQLite, redis consumer, states
│   │   ├── go.mod / go.sum         #   redis/go-redis/v9, modernc.org/sqlite
│   │   └── Dockerfile
│   ├── ai-brain/                   # Python cognition (LangGraph + Ollama)
│   │   ├── worker.py               #   classify→retrieve(stub)→generate_draft
│   │   ├── requirements.txt        #   langgraph, langchain-core/-ollama, redis
│   │   └── Dockerfile
│   └── extension/                  # Chrome MV3 — observe + inject ONLY
│       ├── manifest.json           #   host perms: localhost:8080 + linkedin.com
│       ├── content.js              #   3 modes by URL: MESSAGING (reply), PROFILE_PAGE
│       │                           #   (single outreach), LIST_PAGE (Phase 2 batch queue)
│       └── background.js           #   fetches to local gateway only (msg/outreach/batch)
│
├── contracts/                      # event/envelope JSON Schemas (versioned)
│   ├── envelope.schema.json
│   ├── event-types.yaml / .sha256
│   └── events/*.v1.schema.json     #   message.received, draft.generated, etc.
│
├── safety/                         # frozen safety authority
│   ├── invariants.yaml / .sha256   #   22 invariants (machine-readable)
│   └── frozen/
│       ├── state-transitions.json (+.sha256)
│       ├── extension-forbidden-api.txt (+.sha256)
│       └── node-tool-allowlist.txt (+.sha256)
│
└── docs/                           # design authority (prose specs)
    ├── safety-invariant.md         #   §5 authoritative invariant text
    ├── go-state-machine.md         #   runtime assertions R1..R25
    ├── redis-stream-contracts.md
    ├── event-contracts.md
    ├── extension-security-boundary.md
    ├── invariants-registry.md
    ├── node-tool-governance.md
    ├── state-transition-fixture.md
    └── adr/                        #   EMPTY (no ADRs written yet)
```

---

# Current Demo State

**Verified working 2026-06-03.** Two equivalent demos:

**A) Browserless (`make demo`)** — fastest proof:
```
POST /ingest {thread_id:"demo", text:"..."}     # → DRAFTING, publish message.received
  → ai-brain consumes, Qwen drafts, publishes draft.generated
  → gateway: DRAFTING → PENDING_APPROVAL
GET /draft/demo                                  # poll until state=PENDING_APPROVAL
POST /approve {thread_id:"demo", decision:"approve"}  # → READY_FOR_INJECTION, returns inject_text
```

**B) In LinkedIn (full extension demo)**:
```
Load unpacked services/extension/ in chrome://extensions
  → open a LinkedIn messaging thread
  → panel appears bottom-right
  → Capture (or paste) → Get Suggestion
  → local Qwen drafts reply → shown in panel
  → edit if desired → Approve → Inject (text placed in composer)
  → HUMAN clicks LinkedIn Send (only sender)
  → extension observes new bubble → POST /observed → SENT_BY_HUMAN
```

**C) Phase 1 — AI Outreach, browserless (`make demo-outreach`)**:
```
POST /profile-ingest {profile_id, headline, about, profile_text, context}  # → OUTREACH_GENERATING, publish outreach.requested
  → ai-brain consumes, auto-detects style, Qwen drafts a connection note, publishes outreach.generated
  → gateway: OUTREACH_GENERATING → OUTREACH_READY
GET /outreach/{profile_id}                              # poll until state=OUTREACH_READY (returns note + style)
POST /approve-outreach {profile_id, decision:"approve"} # → READY_FOR_CONNECTION_INJECTION, returns inject_text (never a send)
POST /connection-injected {profile_id}                  # → CONNECTION_INJECTED (note filled in modal)
POST /connection-observed {profile_id}                  # → CONNECTION_SENT_BY_HUMAN (human clicked Connect)
```

**D) Phase 1 — AI Outreach, in LinkedIn (full extension)**:
```
Open a LinkedIn profile (linkedin.com/in/<slug>) → extension detects PROFILE_PAGE
  → outreach panel appears bottom-right
  → Capture Profile (name/headline/location/about/experience/mutual) → editable
  → Generate Outreach → local Qwen drafts a personalized note (style auto-detected)
  → edit if desired → click LinkedIn's Connect → Add a note
  → Approve & Inject (note text placed in the connection modal's note field ONLY)
  → HUMAN clicks LinkedIn Connect (only sender)
  → extension observes "invitation sent / pending" → POST /connection-observed → CONNECTION_SENT_BY_HUMAN
```

**E) Phase 2 — Batch Outreach Queue, browserless (`make demo-batch`)**:
```
POST /batch-ingest {batch_id, limit, filters, profiles:[{profile_id,name,headline,company,location,mutual},...]}
  → each profile: DISCOVERED → PROFILE_CAPTURED → OUTREACH_GENERATING, publish outreach.requested
  → ai-brain drafts each (per-profile, unchanged), publishes outreach.generated
  → gateway: batch items → OUTREACH_GENERATED (single-profile items still → OUTREACH_READY)
GET /batch/{batch_id}                                    # poll the queue: items + states + drafts
POST /batch-approve {batch_id, profile_id, decision:"approve", edited_text?}   # → READY_FOR_INJECTION (+inject_text)
POST /batch-approve {batch_id, decision:"approve_all"|"reject_all"}            # bulk gate
POST /batch-injected {profile_id}                        # → INJECTED (note filled in that profile's modal)
POST /batch-observed {profile_id}                        # → CONNECTED_BY_HUMAN (human clicked Connect)
```

**F) Phase 2 — Batch Outreach Queue, in LinkedIn (full extension)**:
```
Open a People search / Connections / My-Network page → extension detects LIST_PAGE
  → Outreach Queue panel appears bottom-right
  → set Limit (5/10/20/50/custom), Daily cap (10/20/50), Filters (recruiter/founder/hiring-mgr/sales/custom kw)
  → Scan Profiles (READ-ONLY card scan; dedupes already-processed; applies filter + limit + remaining daily cap)
  → Generate Outreach Queue → bulk drafts appear as an inbox; poll until each is OUTREACH_GENERATED
  → review each: edit text inline, [Approve] / [Reject], or [Approve All] / [Reject All]
  → approved → READY_FOR_INJECTION
  → go to a profile, open Connect → Add a note, click [Inject] in the queue row (note text only)
  → HUMAN clicks LinkedIn Connect (only sender) → extension observes "invitation sent/pending" → CONNECTED_BY_HUMAN
```

---

# Phase 2 — Batch Outreach Queue (architecture notes)

**Reused, not redesigned.** No new services, no new streams. Batch profiles are
published as the SAME `outreach.requested` event on `stream:message_ingest`; the
ai-brain is unchanged (it drafts per-profile and knows nothing about batches).
Batch rows live in the SAME `outreach` SQLite table, tagged by `batch_id`. The
gateway consumer routes a generated draft to the Phase 2 vocabulary iff the row
has a `batch_id`, else the Phase 1 vocabulary — so all three flows coexist.

**Queue status spine (per item):**
```
DISCOVERED → PROFILE_CAPTURED → OUTREACH_GENERATED → APPROVED
  → READY_FOR_INJECTION → INJECTED → CONNECTED_BY_HUMAN → (FOLLOWUP_PENDING, reserved)
                                   ↘ OUTREACH_REJECTED
```
(`PROFILE_CAPTURED` is transient: ingest holds the captured context and goes
straight to `OUTREACH_GENERATING`, which the consumer flips to
`OUTREACH_GENERATED`. `FOLLOWUP_PENDING` is a reserved status — no sequencing yet.)

**Schema (additive, defensive).** `outreach` gained `batch_id, name, headline,
company, location, mutual` via `ALTER TABLE ADD COLUMN` (the DB is on the
persistent `gwdata` volume, so `CREATE TABLE IF NOT EXISTS` can't add them; the
`addColumn` helper ignores duplicate-column errors on re-run). New `batches`
table records `{batch_id, limit_n, filters, count, created_at}` for audit.
**(2026-06-06)** also added `key_phrases` + `grounding` columns (display-only
grounding signal from ai-brain; written by `setOutreachMeta` on `outreach.generated`,
returned by `/batch` and `/outreach`; never gates approval).

**Extension scan selectors (best-effort, `content.js: scanProfiles`).** Reads
`a[href*="/in/"]` anchors (one per card), derives `profile_id` from the
`/in/<slug>` href, takes the name from the anchor's `span[aria-hidden="true"]`,
then walks up to the card container (`li.reusable-search__result-container`,
`div.entity-result`, `.mn-connection-card`, or nearest `li`) to read:
- headline ← `.entity-result__primary-subtitle` / `.mn-connection-card__occupation` / `[class*="primary-subtitle"]`
- location ← `.entity-result__secondary-subtitle`
- mutual   ← any card text matching `/mutual connection/i`, **count only** (names stripped)
Non-name anchors (avatars, Message/Connect/Follow buttons, "LinkedIn Member")
are skipped. **`about` and `company` are usually NOT on list cards** — they come
through only when the user pastes/edits or on single-profile capture.

**Anti-ban rules / smart limits (extension, `chrome.storage.local`).**
- **Limit** per scan: 5/10/20/50/custom (hard-capped at 50; gateway backstops at 50/batch too).
- **Daily cap**: 10/20/50 — `lc_daily = {date, count}`; effective queue size = `min(limit, cap − today's count, fresh-after-dedupe)`. Cap reached ⇒ scan queues 0 and says so.
- **Dedupe**: `lc_processed` (Set of profile_ids, capped at 3000) — already-queued profiles are skipped across sessions.
- **Pacing**: jittered delay before batch submit; the scan is READ-ONLY (no navigation, no LinkedIn calls), so inherent ban-risk is low — the real throttle is that a human still clicks every Connect.

**Known limitations (Phase 2).**
- Live-page scan selectors are best-effort against LinkedIn's obfuscated,
  changing DOM — **not yet exercised on a real search page**; expect to tune
  `scanProfiles` selectors. Card-level fields only (no about/company on lists).
- Injection is **per-profile-when-the-Add-a-note-modal-is-open** — there is no
  cross-page auto-fill. Approve in the queue, then visit each profile to Inject.
  (A nice enhancement: on a PROFILE_PAGE, auto-offer that profile's approved
  batch draft. Not built.)
- Daily cap / dedupe state is per-browser-profile (chrome.storage.local), not
  server-side. The gateway records batches but does not enforce the daily cap.
- `FOLLOWUP_PENDING` is a reserved status only; no follow-up sequencing yet.
- 3B model occasionally writes a faintly redundant clause; 14B is cleaner.

HTTP API surface (gateway):
| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness |
| POST | `/ingest` | persist thread, publish message.received → DRAFTING |
| GET | `/draft/{thread_id}` | current state + draft (extension polls) |
| POST | `/approve` | mandatory gate; returns `inject_text` (never a send) |
| POST | `/injected` | mark INJECTED |
| POST | `/observed` | record human send → SENT_BY_HUMAN |
| POST | `/profile-ingest` | **(Phase 1)** persist captured profile, publish outreach.requested → OUTREACH_GENERATING |
| GET | `/outreach/{profile_id}` | **(Phase 1)** current outreach state + drafted note + detected style (extension polls) |
| POST | `/approve-outreach` | **(Phase 1)** mandatory gate; returns `inject_text` for the connection NOTE (never a send) |
| POST | `/connection-injected` | **(Phase 1)** mark CONNECTION_INJECTED |
| POST | `/connection-observed` | **(Phase 1)** record human-sent connection → CONNECTION_SENT_BY_HUMAN |
| POST | `/batch-ingest` | **(Phase 2)** persist N scanned profiles, request bulk drafts → OUTREACH_GENERATING each |
| GET | `/batch/{batch_id}` | **(Phase 2)** the whole approval queue: every item + state + draft (queue polls) |
| POST | `/batch-approve` | **(Phase 2)** per-item or `approve_all`/`reject_all`; approve → READY_FOR_INJECTION (+ inject_text). Never a send |
| POST | `/batch-injected` | **(Phase 2)** mark a queue item INJECTED |
| POST | `/batch-observed` | **(Phase 2)** record human-sent connection → CONNECTED_BY_HUMAN |

---

# Next Priorities (Top 5, ordered)

0. **Live-LinkedIn test of Phase 1 + Phase 2 on real pages** (the only pieces not
   yet exercised in-browser). Phase 1: open a real `/in/<slug>`, Capture →
   Generate → Connect → Add a note → Approve, confirm the note fills AND that the
   new green "Note injected" toast fires. Phase 2: open a People-search /
   Connections page, Scan → Generate Outreach Queue → approve/reject → open a
   card's Connect → Add a note → Inject, confirm the right note lands and the
   **wrong-profile guard** fires if you open the wrong person's invite. Selectors
   to validate/tune against the current (obfuscated) LinkedIn DOM:
   `scanProfiles` cards, `captureProfile` fields, and **the new `inviteModal()` /
   `inviteModalRecipient()` / `connectionNoteField()`** (the invite dialog
   `div[role="dialog"]`, the `#custom-message`/`textarea[name="message"]` field,
   and the "Invite <Name> to connect" header text the recipient guard parses).
1. **Decide & lock the demo model** (14B vs 3B). If 14B: `make pull-model` and
   set `.env MODEL=qwen2.5:14b`; else document 3B as the intentional MVP choice.
2. **Unstub the approval token** (Ed25519 single-use mint on approve / consume on
   inject) to enforce the gate cryptographically, per frozen spec.
3. **Tighten send-observation** so `SENT_BY_HUMAN` only fires on a genuine
   outbound bubble (reduce false positives).
4. **Implement real memory retrieval** (replace the hardcoded stub) — start of
   the "personal-assistant feel" requirement.
5. **Begin AI outreach** (client priority #1): new-thread/cold-draft path that
   still routes through the same mandatory approval gate.

---

# Tomorrow Resume Instructions

**Exact steps to continue — no guessing:**
1. `cat CLAUDE.md` (this file) first.
2. `docker-compose ps` — confirm redis/ollama/gateway/ai-brain are `Up`. If not:
   `make up` (from `/home/kadir70/linkedin-copilot`).
3. `curl localhost:8080/health` → expect `{"ok":true}`.
4. `curl -s localhost:11434/api/tags` → confirm which model is pulled (currently
   `qwen2.5:3b`).
5. Run `make demo` to confirm the end-to-end flow still works.
6. Pick up at **Next Priorities #1** (model decision) then #2 (approval token).
7. Any change to a frozen file under `safety/` or `contracts/` requires an ADR in
   `docs/adr/` + recomputed `.sha256` in the same change.

---

# Decisions Log (chronological, append-only)

- **2026-06-03** — Stack frozen: Go gateway, Redis Streams, LangGraph+LangChain,
  Ollama, Qwen2.5, SQLite, Chrome/Brave extension (observe+inject), Docker
  Compose. *Why:* local-first, no-cloud, auditable safety boundary.
- **2026-06-03** — Gateway is the **single authority**; AI Brain emits data only;
  extension emits observations + one composer write only. *Why:* keep the "human
  is the only sender" invariant structurally true, not just by convention.
- **2026-06-03** — Authored frozen safety corpus (22 invariants, state-transition
  fixtures, forbidden-API list, tool allowlist, event schemas with sha256 digests).
  *Why:* make safety machine-verifiable and tamper-evident.
- **2026-06-03** — MVP ships approval token / memory / meeting logic as **stubs**;
  gate enforced by state. *Why:* prove the safe flow end-to-end first.
- **2026-06-03** — `.env` overridden to `MODEL=qwen2.5:3b` for a faster demo on
  this machine (frozen default remains 14b). *Why:* disk/latency on dev box.
- **2026-06-03** — Created `CLAUDE.md` as persistent project memory.
- **2026-06-04** — **Phase 1 AI Outreach Engine built on top of the MVP — NO
  redesign.** Reused the existing Redis Streams bus (new `type`s on the SAME
  `stream:message_ingest` / `stream:cognition_result` streams) and the gateway
  state machine. Added an `outreach` SQLite table; an outreach spine
  PROFILE_CAPTURED → OUTREACH_GENERATING → OUTREACH_READY → OUTREACH_APPROVED →
  READY_FOR_CONNECTION_INJECTION → CONNECTION_INJECTED → CONNECTION_SENT_BY_HUMAN
  (+ OUTREACH_REJECTED); endpoints `/profile-ingest`, `/outreach/{id}`,
  `/approve-outreach`, `/connection-injected`, `/connection-observed`. AI Brain
  gained a second LangGraph (heuristic style auto-detect over 7 styles →
  world-class-outreach system prompt → Qwen). Extension now routes by page type
  (MESSAGING vs PROFILE_PAGE), adds a profile-capture + outreach panel, and
  injects into the connection NOTE field via `HTMLTextAreaElement.value`
  (ALLOWED `composer_text_injection`) ONLY. *Why:* deliver client priority #1
  (outreach) while keeping every frozen safety guarantee — no send route, no
  LinkedIn write, human is still the only sender (no Connect click anywhere).
- **2026-06-04** — **Outreach quality pass (premium, founder-to-founder)** —
  ai-brain only. (1) Mutual connections are stripped from the profile BEFORE the
  model sees it (`strip_mutual`) and the system prompt forbids ever implying one
  — we only have a count, never a verified name. This also fixed every real-scan
  profile classifying as `warm_intro` (old `detect_style` ran on text containing
  the "Mutual:" line; now it runs on the stripped profile). (2) Tightened length
  120-220 → **120-180**. (3) Rewrote the system prompt: premium founder-to-founder
  voice, ground ONLY in real profile facts (headline/about/company/location), zero
  hallucination, with a BANNED-phrase list ("noticed your", "would love to
  connect", "came across", "your work on", "great work", "reaching out", "hope
  you're doing well", …) and two premium few-shot examples. (4) Added best-of-3
  generation: each draft is scored by `(banned-phrase count, distance from the
  120-180 band)` and the cleanest is kept; one regeneration is nudged with the
  rejected draft. Verified across 5 drafts (batch + single-profile): all 120-180
  chars, zero banned phrases, zero mutual mentions, real-context references.
  **Caveat:** the 3B model still produces occasional MILD hallucination (a detail
  adjacent to but not in the profile); 14B grounds noticeably tighter. No gateway/
  extension change — single-profile and batch share `generate_outreach`.
- **2026-06-04** — **Batch queue filter fix** (extension-only). Symptom: scan
  "Found 27" but "queuing 0" with the founder filter on. Two causes: (a) the
  headline selector misses on the live search DOM, so `headline` was empty and
  no keyword could match; (b) founder keywords were too strict (missed "founding
  partner"/"entrepreneur"). Fixes in `content.js`: lowercase `includes()`
  matching now falls back to the card's full text when the headline selector
  misses (`scanProfiles` attaches a `cardText` field, stripped before send);
  founder keywords broadened to `found|ceo|owner|entrepreneur|co-found|cofound|
  founding partner` (so founder, co-founder, "ceo & founder", startup founder,
  founding partner, entrepreneur all match). Added debug counters
  (scanned/matched_filter/queued/rejected) shown in the panel status AND a
  per-profile rejection reason logged to the DevTools console (filtered-out vs
  already-processed vs over-limit/cap), so a "queuing 0" is always explainable.
  No gateway/ai-brain change; reload the unpacked extension to pick it up.
- **2026-06-04** — **Phase 2 Batch Outreach Queue built as a pure queue layer —
  NO redesign.** Reused the SAME services, streams, and ai-brain. Batch profiles
  publish the SAME `outreach.requested` event; batch rows live in the SAME
  `outreach` table tagged by `batch_id` (added columns `batch_id, name, headline,
  company, location, mutual` via defensive `ALTER TABLE`; new `batches` audit
  table). Added gateway endpoints `/batch-ingest`, `/batch/{id}`,
  `/batch-approve` (per-item + `approve_all`/`reject_all`), `/batch-injected`,
  `/batch-observed`, and the queue status spine DISCOVERED → PROFILE_CAPTURED →
  OUTREACH_GENERATED → APPROVED → READY_FOR_INJECTION → INJECTED →
  CONNECTED_BY_HUMAN (+ reserved FOLLOWUP_PENDING, + OUTREACH_REJECTED). The
  gateway consumer routes a generated draft to the Phase 2 vocabulary iff the row
  has a `batch_id`, else Phase 1 — so messaging, single-profile, and batch flows
  coexist. ai-brain UNCHANGED. Extension adds a LIST_PAGE mode: read-only
  `scanProfiles` over search/connections cards, headline filters, dedupe
  (`lc_processed`), daily cap (`lc_daily`), and an inbox approval queue with
  per-item + bulk approve/reject and per-profile Inject. *Why:* deliver
  outreach-at-scale while keeping every safety guarantee — AI NEVER sends, never
  clicks Connect; only DOM write is the note field VALUE. Verified browserless
  (5- and 3-profile batches): bulk draft, reject, approve-with-edit, approve_all,
  inject, observe → CONNECTED_BY_HUMAN, 409 guard; no regression to Phase 0/1.
- **2026-06-04** — **Outreach personalization quality fix** (no architecture
  change). Symptom: greeted the wrong person ("Hi Yessica" on Saima Shaikh's
  profile), too long, robotic. Root cause: (a) the mutual-connection label
  ("Yessica Bianchi and N mutual connections") leaked into capture and got used
  as the target name; (b) the bare `h1` name fallback could match a non-name
  heading; (c) the prompt didn't pin the exact name or length. Fixes: extension
  now derives the owner name from the page `<title>` first (top-card `h1`
  fallback, never a bare `h1`), and captures mutual connections as a COUNT only
  (names stripped). The exact `name` is threaded explicitly profile-ingest →
  `target_name` on the stream → brain. Outreach system prompt rewritten (use the
  EXACT name, 120-220 chars, reference real profile context, no fluff/sales/“hope
  you’re doing well”, no mutual names, with good-example shots); temp 0.6→0.5. A
  deterministic `force_correct_greeting()` backstop rewrites ONLY the greeting to
  the correct first name if the model slips. *Why:* the human-is-only-sender
  boundary is untouched; this is pure draft-quality. Verified on Saima Shaikh
  ("Hi Saima, noticed your expertise in brand positioning for UAE apparel
  brands…", 128 chars) and an adversarial profile with a mutual name embedded —
  no leak, correct greeting.
- **2026-06-04** — Outreach injection targets the **connection NOTE field only**
  and requires the human to open "Add a note" first; the extension never clicks
  Connect/Send. New events `outreach.requested/.generated`,
  `connection.inject.requested`, `human.connection.sent.observed` carried on the
  existing streams. *Why:* no new infra, no new manifest permissions (`/in/`
  pages already under the `linkedin.com/*` host permission), boundary unchanged.
- **2026-06-06** — **Injection-flow hardening + UX pass (extension `content.js`
  ONLY; NO architecture change, NO backend change).** Goal: polish the live
  Connect→Add-a-note→Inject path and make every failure explainable. Changes:
  (1) `inviteModal()` locates the OPEN, VISIBLE invite dialog using a
  fixed-position-safe `isVisible()` (box dims + computed style, NOT `offsetParent`,
  which is null for `position:fixed` modals) and the note-field lookup is now
  SCOPED to that modal first — eliminating the prior `.artdeco-modal textarea`
  global grab that could hit an unrelated modal. (2) `injectIntoConnectionNote`
  now takes an `expectedName` and returns `{ok, reason, recipient}`; reasons:
  `no-modal | no-textarea | wrong-profile | verify-failed | ok`. It VERIFIES the
  textarea value took after the native-setter write, and **refuses to fill on a
  confident recipient mismatch** (parsed from the modal's "Invite <Name> to
  connect"), guaranteeing right-note→right-person in both single-profile and
  batch; the guard is permissive (never blocks) when the name can't be read.
  (3) `injectMsg(reason)` → actionable copy for each failure; new `toast()`
  success/failure notifications; Approve/Inject buttons show a loading state
  (disabled + "…"). (4) Batch queue: emoji `stateBadge()` per item + a live
  120–180 char counter under each editable draft (edit-before-approve made
  explicit; Approve still sends `ta.value`). Messaging Approve also got
  toast+loading (composer path otherwise unchanged). **Safety:** the only DOM
  write is still the note/composer VALUE (+input/change); the name guard only
  READS modal text (`dom_read`). Forbidden-API scan clean (no click/submit/
  Enter/LinkedIn fetch), `node --check` passes, no frozen file touched (no ADR
  required). Verified: all 3 browserless flows (`make demo` / `demo-outreach` /
  `demo-batch`) green, approve_all → READY_FOR_INJECTION confirmed, messaging
  reply re-verified (no regression). **NOT yet verified: real-LinkedIn in-browser
  injection** (no browser in this env) — the modal/recipient selectors remain
  best-effort and are the #0 thing to exercise on a live page.
- **2026-06-06** — **Outreach GROUNDING-quality fix (ai-brain core; gateway +
  extension passthrough only; NO architecture change).** Bug: 3B drafts
  hallucinated profile context (e.g. "financial services / customer experience"
  on Gianluca Orsini, CTO MLG Group, whose card carried only role+company; and a
  "GCC" bleed from the prompt's few-shot example onto an apparel profile). Root
  causes: (a) thin batch cards (name+headline+location only) gave the model little,
  so it invented; (b) best-of-N scored only banned-phrases+length — **no grounding
  check**; (c) few-shot example facts bled into drafts. Fix, all in
  `services/ai-brain/worker.py` unless noted: (1) rewrote `OUTREACH_SYSTEM` with an
  ABSOLUTE grounding rule (reference ONLY words literally in the profile; thin
  profile → write about the role, invent nothing) and an explicit "the examples
  are tone/length only — do NOT reuse their companies/industries" clause. (2) Added
  a deterministic validator: `grounded_set()` (significant profile tokens, field
  labels stripped), `grounding_check()` (flags significant draft words — incl.
  short ALL-CAPS **acronyms** like GCC to catch example-bleed — not present in the
  profile; tolerant prefix-match for inflection; a generous SAFE_VOCAB of generic
  business/networking words so only true domain claims flag), returning
  `(used, ungrounded, confidence)`. (3) `score_note` now = `(#banned, #ungrounded,
  length-dist)` — grounding is the primary gate; the regeneration nudge names the
  exact invented terms and tells the model to drop them. (4) HARD GUARANTEE: if the
  best draft still invents ≥2 terms, ship `grounded_fallback()` which quotes the
  person's LITERAL headline ("your work as <headline> caught my eye — …"), so the
  worst case is still personalized + true, never fabricated, never an identical
  bland line. (5) Each profile is a fresh `OUTREACH_GRAPH.invoke` (no state
  carry-over) — template-bleed fix is structural + acronym-guard. (6) Brain emits
  `key_phrases` + `grounding`; gateway adds `key_phrases`/`grounding` columns
  (`addColumn`), `setOutreachMeta`, and returns them from `/batch` + `/outreach`;
  extension renders a **debug preview** beside each draft (grounded-on terms +
  NN% confidence, ⚠ red < 50%) in the batch queue and the single-profile panel.
  **Safety untouched** (data-only change; gateway still the sole authority; human
  still the only sender; no frozen file edited → no ADR). Verified live: Gianluca →
  grounded fallback (no finance invention); apparel → no "GCC" bleed; rich profiles
  (Cloudify/Northwind) ship specific grounded model drafts; thin profiles get
  headline-quoting fallbacks; `make demo` / `demo-outreach` / `demo-batch` all green;
  approve_all → READY_FOR_INJECTION intact; messaging reply unchanged.

- **2026-06-06** — **Model stack upgraded to Qwen3; production default set to
  `qwen3:4b-instruct-2507-q4_K_M` (NO architecture change, data-only).** Pulled
  qwen3:4b, qwen3:8b, qwen3:4b-instruct-2507. Empirically established that the
  hybrid THINKING qwen3:4b/8b are unusable on this CPU box (reasoning not reliably
  suppressible via `think:false` or `/no_think` on ollama 0.30; 1.5–2.1k tokens/
  draft at ~2 tok/s = minutes/draft). Added a Qwen3 compat shim in `worker.py`
  `ollama_generate` (`IS_QWEN3_THINKING` → `/no_think` + `think:false` +
  `num_predict=220` cap + `strip_thinking()`); instruct + Qwen2.5 take the clean
  path unchanged. Benchmarked qwen2.5:3b vs qwen3:4b-instruct-2507 on 5 founder
  profiles via the real `generate_outreach` pipeline (grounding scored by the
  production `grounding_check`): the instruct model wins on grounding (0.72 vs
  0.61), perfectly-clean notes (2/5 vs 0/5), and — decisively — never fabricated a
  company/industry on thin cards (3b invented "Cloudflare"/"agritech"/finance),
  at ~45% higher warm latency (≈84s vs ≈58s/draft) and +~2.3GB RAM, both fine on
  11.6GB. Applied rollback-safe config across `.env` (+commented qwen2.5:3b/7b
  rollbacks), `.env.example`, `docker-compose.yml`, `Makefile`, `README.md`, and
  the `worker.py` getenv default. Re-verified all browserless flows green on the
  new model: messaging, single outreach, batch + approve_all, and the full
  injection lifecycle (→ CONNECTED_BY_HUMAN / CONNECTION_SENT_BY_HUMAN). Safety
  boundary untouched — data-only change, human still the only sender, no frozen
  file edited (no ADR). *Why:* "upgrade to latest stable Qwen" while honoring the
  use-case's hard bar (strict grounding, near-zero hallucination) on real hardware.

- **2026-06-06** — **FAST_MODE low-resource CPU profile added (data-only; no
  architecture change).** Fixes ollama saturating all cores (load ~9+) and slow
  5-profile batches on old 2C/4T laptops (HP EliteBook 840 G1). (1) `docker-compose`
  ollama env: `OLLAMA_NUM_PARALLEL=1` + `OLLAMA_MAX_LOADED_MODELS=1` +
  `OLLAMA_KEEP_ALIVE=30m` → load 9+→~2-4, no reload spikes. (2) `FAST_MODE` env
  (default false; true in this `.env`): best-of-1 for all styles except `founder`
  (best-of-2), no length-only regen, token caps, and a compact `OUTREACH_SYSTEM_FAST`
  prompt (~1/5 the prefill tokens). Cut a warm 5-draft batch 274s→122s on the dev
  box; grounding safety untouched (validator + fallback still backstop every draft,
  so no hallucination ships even at best-of-1). Measured: qwen3:4b-instruct+FAST
  122s/5, qwen2.5:3b+FAST 82s/5 (hits the 30-90s target; fallback-protected).
  Sequential generation throughout (serial consumer + 1 ollama slot). *Why:* make
  the copilot usable on the client's actual low-end hardware without weakening the
  grounding guarantees or the safety boundary.

- **2026-06-06** — **FASHION vertical added (`MODE=fashion`; data-only, no
  architecture change).** Specializes outreach for UAE/GCC apparel sourcing
  (founders, brand owners, buyers, sourcing/purchasing/merchandising leads). New
  fashion system prompts (full + FAST), `sourcing` style + dual-tier routing
  (founder=best-of-2, sourcing/buyer=best-of-1), a fashion FRAMING vocab (real
  fashion facts credited, generic industry framing allowed) and a CLAIM BLOCKLIST
  (sustainability/brand/category/award claims must be grounded — zero tolerance),
  sourcing-voiced fallbacks, plus extension fashion filter keywords + 3 LinkedIn
  search presets. Key gotcha found & fixed: merging fashion vocab into SAFE_VOCAB
  *skipped* those words so real fashion facts stopped counting as "used" → every
  draft fell to the fallback; fixed by a separate `_FRAMING_VOCAB` + a blocklist
  hallucination gate (instead of the generic ungrounded-word count, which over-
  rejected wordy sourcing prose). Verified: grounded apparel-sourcing drafts ship,
  invented sustainability/brand claims caught, style routing correct, safety
  boundary + forbidden-API scan unchanged. Revert with `MODE=general`. *Why:*
  client pivot to fashion/apparel sourcing outreach at scale.

- **2026-06-06** — **Scan-limit selector min 5→1 + mode-aware default (clean impl).**
  Limit dropdown now 1/2/3/5/10/15/20 (min 1 for slow CPUs / safer manual review).
  Added gateway `GET /config` ({mode, fast_mode} from env, now also passed to the
  gateway service) so the extension sets a mode-aware default (2 fashion / 5
  general) and shows a low-CPU helper ("FAST_MODE — keep to 1–2 drafts/scan") when
  FAST_MODE is on. Confirmed there was NO backend min-limit validation (gateway
  `limit` is metadata `limit_n`; count = len(profiles), capped 50, no floor; worker
  ignores limit), so limit=1/2 already passed — verified end-to-end (scan→queue→
  generate→approve_all→inject→observe) at limit=1 and limit=2. Files: gateway
  main.go (+/config, strings import), docker-compose (MODE/FAST_MODE on gateway),
  background.js (config action), content.js (dropdown values, mode-aware default,
  helper text). Safety boundary unchanged; forbidden-API scan clean.

- **2026-06-07** — **Low-RAM PRODUCTION STABILITY pass + smart dual-model routing
  (data/config + ai-brain logic; NO architecture change, NO frozen file edited,
  safety boundary untouched — human is still the only sender).** Problem: on 8GB
  CPU-only hardware the `qwen3:4b-instruct`-everywhere config OOM-killed `ai-brain`
  (Exit 137) and crashed the gateway (exit 2). Fixes: (1) **Dual model** — base
  `MODEL=qwen2.5:3b` for replies + general/recruiter/buyer/sourcing; premium
  `PREMIUM_MODEL=qwen3:4b-instruct-2507-q4_K_M` for founder/CEO/director/owner/VP
  via `model_for()`/`is_premium_profile()` (`PREMIUM_ROUTING` toggle); `qwen3:8b`
  kept installed but OFF. (2) **RAM watchdog** `ram_pressure()` (host
  `/proc/meminfo MemAvailable` < `RAM_MIN_FREE_MB`=1200) → DEFENSIVE mode: force
  base, best-of-1, no length-regen, `ollama_unload()` premium (`keep_alive:0`),
  pause queue `QUEUE_THROTTLE_SEC`. (3) **Never-crash** `generate_with_fallback()`
  (premium failure → base auto-retry). (4) **Explicit unload-on-switch** (lower
  peak RAM with `MAX_LOADED_MODELS=1`). (5) **docker-compose** `version:"2.4"` +
  `restart: unless-stopped` (auto-recover Exit137; Redis groups redeliver unacked)
  + `mem_limit` ollama=6g/ai-brain=1200m/gateway+redis=256m (the host-OOM guard) +
  healthchecks + `KEEP_ALIVE=15m`. (6) **Observability** `make doctor` + gateway
  `GET /stats`. Per-model Qwen3 thinking shim refactored from a module global to
  per-call (`is_thinking_model`/`think_cap`) so routing can mix models safely.
  Files: `.env`, `.env.example`, `docker-compose.yml`, `Makefile` (doctor +
  pull-model both models), `services/ai-brain/worker.py`, `services/gateway-go/main.go`
  (+/stats). **Verified live**: 0 OOM / 0 restarts; founder→premium (base unloaded
  first, grounding 1.0); mixed 4-profile fashion batch 127s with correct
  founder→premium / buyer+sourcing→base routing; ollama peaked 3.17/6GiB; messaging
  + single + batch + approve_all + limit=1 all green; defensive path confirmed in
  the deployed container (forced pressure → founder routed to qwen2.5:3b,
  defensive=True). Hallucination guard unchanged (grounding validator +
  claim_inventions + grounded fallback still backstop base-model best-of-1). *Why:*
  make the copilot production-stable on the client's actual 8GB CPU-only laptop
  without weakening grounding or the safety boundary.

- **2026-06-07** — **CRITICAL routing/persona bug fix: priority style detection +
  never-sourcing guard + per-profile fashion voice + confidence (ai-brain core;
  gateway + extension passthrough; NO architecture change, safety boundary
  untouched).** Bug: "Lara Matibaeva — Founder of Hillbridge Partners | Talent
  Advisory" was tagged `style=sourcing` and got the apparel-sourcing voice. Two
  root causes: (a) fashion-mode `detect_style` fell through to a blind
  `return "sourcing"` for anything not matching founder/buyer keywords, so a
  talent/advisory profile (or a profile whose headline the extension failed to
  capture → name-only) defaulted to sourcing; (b) the apparel VOICE was applied
  globally whenever `IS_FASHION`, regardless of the actual profile. Fixes in
  `worker.py`: rewrote `detect_style` as a strict PRIORITY ladder — Founder/CEO >
  Recruiter/Talent/Advisory > Buyer/Sourcing > Hiring/Partnership/Sales >
  apparel-without-function > generic networking — returning `(style, confidence)`
  (high/medium/low); added `NEVER_SOURCING_KW` (founder/ceo/talent/recruit/
  recruitment/advisory/staffing/executive search/people consulting) that HARD-
  BLOCKS the sourcing style/voice; generic no-signal profiles now → `networking`
  (low confidence), never sourcing. Added `is_apparel_profile()` and a per-profile
  `use_fashion_voice = IS_FASHION and (style=="sourcing" or apparel-signal)` that
  gates the fashion system prompt, the sourcing hint, the claim-gate, AND the
  grounded-fallback template choice (`grounded_fallback(..., fashion=)`), so the
  fashion checkbox no longer blindly forces sourcing — a talent-advisory founder
  gets the general networking voice. `recruiter` STYLE_GUIDANCE rewritten to peer-
  to-talent (no candidate pitch, no apparel talk). Confidence threaded out:
  `worker` emits `style_confidence`; gateway adds a `style_confidence` column +
  `setOutreachMeta` arg + returns it from `/outreach` and `/batch`; extension
  renders **"Detected style: X · confidence: Y"** (amber ⚠ on `low`) on the
  single-profile panel and every batch-queue row. **Verified live**: Lara full
  headline → `founder/high` (premium model, talent-advisory networking note, NOT
  apparel); "Talent Advisory" only → `recruiter/high`; name-only → `networking/low`
  (the real-world bug case, now flagged not mis-routed); regression check — genuine
  "Senior Buyer at GulfTex Garments" → `sourcing/high` fashion_voice=True and
  "Founder at Noor Apparel | Private Label" → `founder/high` fashion_voice=True
  (apparel voice preserved). *Why:* correct persona/voice is the foundation of
  outreach quality — wrong routing = bad outreach at scale.

- **2026-06-07** — **SEQUENTIAL_MODE (strict one-profile-at-a-time) + routing
  sharpen (data/config + ai-brain; NO architecture change, safety boundary
  untouched).** Goal: on low-CPU systems never run two generations at once.
  Added `SEQUENTIAL_MODE` (default true; `FAST_MODE` forces it) → consumer
  `xreadgroup count=READ_COUNT` (1 when sequential) so each profile fully
  completes (draft → grounding → `outreach.generated`) before the next is pulled;
  added a `[seq] start profile=…` log. The batch of N is still published to Redis
  at once and **drained sequentially** (no gateway change). Routing: new
  `BASE_FORCE_KW` forces buyer/sourcing/merchandising/procurement/purchasing to
  the **base** model even under a senior title (e.g. "Director of Sourcing" →
  base), so premium is reserved for true founder/CEO/owner/CPO leads. Surfaced
  `sequential_mode` in gateway `/config` + `/stats`; extension defaults the scan
  limit to 1 and shows a "🐢 Sequential mode" hint when on. Files: `.env`,
  `.env.example`, `docker-compose.yml` (SEQUENTIAL_MODE on both services),
  `services/ai-brain/worker.py`, `services/gateway-go/main.go`,
  `services/extension/content.js`. **Verified live**: 3-profile batch drained
  strictly in order (logs show #1 `outreach.generated` BEFORE `[seq] start` #2),
  `ollama ps` never held two models, routing correct (founder→4b, Director-of-
  Sourcing & Merchandiser→3b), approve_all → all READY_FOR_INJECTION, restarts=0
  (no OOM, no crash), `node --check` + forbidden-API scan clean. *Why:* predictable
  single-profile flow + zero CPU oversubscription = stability over speed, no
  hallucination spikes from an overloaded box.

---

# Lessons / Gotchas

- **`docker-compose` vs `docker compose`**: this project's Makefile uses the
  hyphenated **`docker-compose`** (v1 style). The running stack reports v1-style
  container names (`linkedin-copilot_gateway_1`). Stick with `docker-compose` to
  match the Makefile.
- **MODEL is now `qwen3:4b-instruct-2507-q4_K_M`** (benchmarked winner, 2026-06-06).
  Always check `curl localhost:11434/api/tags` before judging draft quality, and
  `docker-compose logs ai-brain | grep consuming` to confirm which model the brain
  actually loaded (it echoes `model=…` on boot).
- **NEVER use the hybrid thinking `qwen3:4b` / `qwen3:8b` on this CPU box.** Their
  reasoning is not reliably disabled (neither `think:false` nor `/no_think` stops
  it deterministically on ollama 0.30) → 1,500–2,100+ tokens/draft at ~2 tok/s =
  minutes per draft. Symptom in `docker-compose logs ollama`: `n_decoded` climbing
  into the thousands for a short note. Use the `-instruct` variant. There is NO
  `qwen3:8b-instruct` on ollama — 4B-instruct is the only small non-thinking Qwen3.
- **After `docker-compose build ai-brain` + recreate, the new container is FRESH**
  — anything you `docker cp`'d into the old container (e.g. `bench/*.py`) is gone.
  Re-`docker cp` before re-running bench/verify scripts. Also: `docker-compose up
  -d --build` can throw `KeyError: 'ContainerConfig'` (compose-v1 + newer engine)
  during recreate — workaround: `docker-compose build X` then `stop`/`rm -sf`/`up -d X`.
- **`load avg ~9+` on a 4-thread box = ollama PARALLELISM, not the model.** The
  fix is `OLLAMA_NUM_PARALLEL=1` (+ `OLLAMA_MAX_LOADED_MODELS=1`) in the ollama
  service env, NOT thread-count tuning. Add `OLLAMA_KEEP_ALIVE=30m` to stop the
  model being evicted between drafts (the cause of the 200–390s latency outliers).
- **On CPU, the biggest per-draft cost is PROMPT PREFILL, not generation.** A
  ~666-token system prompt re-prefilled every call dominated latency; the compact
  `OUTREACH_SYSTEM_FAST` (used when `FAST_MODE`) cut a 5-draft batch 274s→122s by
  itself. When optimizing CPU latency, shrink the prompt before anything else.
- **`FAST_MODE=true` never weakens grounding.** It only trims best-of-N and the
  cosmetic length-chasing regen; the deterministic grounding validator + fallback
  still catch hallucinations, so even best-of-1 can't ship a fabricated note.
  `founder` style keeps best-of-2. To actually hit 30-90s/5-drafts on a 2C/4T
  laptop, also set `MODEL=qwen2.5:3b` (82s vs 122s for 4b-instruct).
- **Qwen3 thinking-mode shim lives in `ollama_generate`** (`IS_QWEN3_THINKING`):
  thinking models get `/no_think` + `think:false` + a `num_predict` cap + a
  `<think>` stripper; instruct/Qwen2.5 take the clean path untouched, so the
  default and the qwen2.5:3b rollback are both behavior-identical to before.
- **LinkedIn messaging page required**: the extension works on a **messaging
  thread** URL (`/messaging/...`). A profile page does NOT work — no composer.
- **Extension is inject-only by design**: never add `.click()` on Send,
  `submit()`, synthetic Enter, or any fetch to linkedin.com. This is a frozen
  safety boundary, not a TODO.
- **Stream names ≠ event-type names**: Redis streams are
  `stream:message_ingest` / `stream:cognition_result`; the message `type` fields
  are `message.received` / `draft.generated`. Don't conflate them.
- **Ollama generation can be slow on CPU**: `make demo` polls up to ~2 min. GPU
  reservation is commented out in `docker-compose.yml` (uncomment for NVIDIA).
- **SQLite serialized writes**: gateway sets `MaxOpenConns(1)` to avoid
  `SQLITE_BUSY` in the MVP. Keep that in mind before adding concurrency.
- **One draft per thread**: re-ingesting a thread overwrites its draft/state.
- **Phase 2 batch reuses the `outreach` table with TWO state vocabularies.**
  Single-profile rows (no `batch_id`) use OUTREACH_READY → READY_FOR_CONNECTION_
  INJECTION → CONNECTION_INJECTED → CONNECTION_SENT_BY_HUMAN. Batch rows (with a
  `batch_id`) use OUTREACH_GENERATED → READY_FOR_INJECTION → INJECTED →
  CONNECTED_BY_HUMAN. They're just strings in one column; the gateway consumer
  picks the vocabulary by checking `outreachBatchID(pid)`. Don't "unify" them
  without touching both the consumer and both sets of endpoints.
- **Batch schema columns are added by `ALTER TABLE` at boot**, not by CREATE TABLE
  (the DB is on the persistent `gwdata` volume). `addColumn` ignores duplicate-
  column errors, so re-runs are safe — but a brand-new column you add must go
  through `addColumn`, not the CREATE statement, or existing DBs won't get it.
- **"queuing 0" after a good scan? Open the DevTools console.** The Scan handler
  logs a collapsed `[LC] scan: …` group with a per-profile ✓/✗ and the reason
  (filtered-out + which keywords / already-processed dedupe / over limit-or-cap),
  plus a final `counters` object (scanned/matched_filter/queued/rejected). The
  panel status shows the same four counters. Filter matching is lowercase
  `includes()` against the headline, falling back to the card's full text when
  the headline selector misses — so an empty-headline DOM no longer silently
  drops everyone. If matched_filter is 0 with a filter on, the keywords didn't
  appear in the visible card text (tune `FILTER_KEYWORDS` or the card selectors).
- **The extension content script now has THREE modes** routed by URL in
  `pageType()`: MESSAGING (`/messaging/`), LIST_PAGE (`/search/results/people|all`,
  `/mynetwork/`), PROFILE_PAGE (`/in/`). An SPA-nav watcher re-mounts the right
  panel on client-side navigation. Only one `[data-lc-panel]` exists at a time.
- **`detect_style` is a strict PRIORITY ladder, not first-keyword-wins, and it
  returns `(style, confidence)`.** Order: Founder/CEO > Recruiter/Talent/Advisory
  > Buyer/Sourcing > Hiring/Partnership/Sales > apparel-without-function > generic
  networking. `NEVER_SOURCING_KW` hard-blocks the `sourcing` style/voice for any
  founder/talent/advisory/recruiting/staffing profile (the Lara bug). A no-signal
  profile → `networking/low`, NEVER `sourcing`. The apparel VOICE is a PER-PROFILE
  decision (`use_fashion_voice = IS_FASHION and (style=="sourcing" or
  is_apparel_profile)`), NOT global `IS_FASHION` — so `MODE=fashion` no longer
  forces apparel copy onto a talent-advisory founder. If you add a style, update
  BOTH `detect_style` and `STYLE_GUIDANCE`, and decide whether it should be in
  `NON_PREMIUM_STYLES` / `BASE_FORCE_KW` (routing) and whether it implies apparel.
- **Outreach: never trust a bare `h1` for the target name, and never feed mutual-
  connection text to the model.** Mutual labels read like "Yessica Bianchi and N
  mutual connections" — that name leaked in and the model greeted "Hi Yessica" on
  Saima's profile. The name comes from the page `<title>` (top-card `h1`
  fallback); mutuals are captured as a COUNT only; the exact name is threaded as
  `target_name`; and `force_correct_greeting()` in the brain is the deterministic
  backstop that rewrites the greeting to the right first name.

---

# Commands Cheat Sheet (verified)

```bash
# from /home/kadir70/linkedin-copilot
make up                      # docker-compose up --build -d (redis, ollama, gateway, ai-brain)
make pull-model              # pull MODEL into ollama (default qwen3:4b-instruct-2507, ~2.5GB)
make demo                    # browserless reply flow: ingest -> draft -> approve
make demo-outreach           # browserless Phase 1 outreach: profile-ingest -> generate -> approve
make demo-batch              # browserless Phase 2 batch: batch-ingest (3) -> bulk draft -> approve_all
make logs                    # tail gateway + ai-brain
make ps                      # service status
make restart                 # restart gateway + ai-brain
make doctor                  # health + model strategy + RAM/load + queue + recommended scan limit
make down                    # stop everything

curl localhost:8080/health                       # -> {"ok":true}
curl -s localhost:8080/stats                     # model strategy {base/premium/routing} + queue state counts
curl -s localhost:11434/api/tags                 # which Ollama models are pulled
docker-compose exec ollama ollama ps             # which model ollama holds in RAM right now
docker-compose ps                                # container states

# Low-RAM gotcha: `make pull-model` now pulls BOTH the base (qwen2.5:3b) and the
# premium (qwen3:4b-instruct-2507) models. qwen3:8b stays OFF unless you set it.
# `docker-compose up --build -d` can throw KeyError 'ContainerConfig' (compose-v1
# recreate bug) -> workaround: docker-compose rm -sf && docker-compose build && up -d.

# manual flow
curl -X POST localhost:8080/ingest -H 'Content-Type: application/json' \
  -d '{"thread_id":"demo","text":"Hi! Are you free Thursday to chat about the role?"}'
curl localhost:8080/draft/demo                   # poll until PENDING_APPROVAL
curl -X POST localhost:8080/approve -H 'Content-Type: application/json' \
  -d '{"thread_id":"demo","decision":"approve"}' # returns inject_text (NOT a send)

# manual outreach flow (Phase 1)
curl -X POST localhost:8080/profile-ingest -H 'Content-Type: application/json' \
  -d '{"profile_id":"p1","headline":"Founder & CEO at Acme","about":"Building robots","profile_text":"Name: Jane","context":"met at RoboConf"}'
curl localhost:8080/outreach/p1                  # poll until OUTREACH_READY (note + detected style)
curl -X POST localhost:8080/approve-outreach -H 'Content-Type: application/json' \
  -d '{"profile_id":"p1","decision":"approve"}'  # returns inject_text for the connection NOTE (NOT a send)
```

---

## Maintenance rules for this file
1. Update after every major milestone (and at session end).
2. Keep concise but complete. Assume future sessions have zero memory.
3. **Append** to Decisions Log — never delete history.
4. Add timestamps to milestones. Preserve previous decisions.
5. Re-verify "Working" claims against the live stack before trusting them.
