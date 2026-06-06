# Redis Stream Contracts

**Document ID:** REDIS-STREAM-CONTRACTS-001
**Status:** AUTHORITATIVE — normative for all event transport. CI validates schemas and topology against this document.
**Owner:** Architecture / Distributed Systems
**Applies to:** `gateway` (Go), `ai-brain` (Python/LangGraph), `extension` (browser), `pwa`, `redis`, `persistence` (SQLite→Postgres)
**Companion:** [`safety-invariant.md`](./safety-invariant.md) (SAFETY-INVARIANT-001) — this document MUST NOT weaken any invariant defined there. Where the two touch (INV-014, INV-016, INV-018, INV-020, INV-021), the safety document wins.
**Change control:** Changes to Sections 1, 3, or 4 require an ADR in `docs/adr/` and a corresponding update to the machine-readable contracts in `contracts/` (Section 10) in the same PR.

---

## 0. Purpose and reading guide

This document defines the **complete event backbone** of the system: every Redis Stream, every event type, every payload schema, the ordering and idempotency rules, the approval lifecycle as it appears on the wire, and the dead-letter / replay-protection machinery.

It establishes one transport-level principle that the rest of the document elaborates:

> ### TRANSPORT PRINCIPLE
> **Streams transport facts; they never confer authority.** A message on a stream is a *claim*. Authority lives exclusively in the Go gateway (the state authority of SAFETY-INVARIANT-001, Layer 2). No stream message — including a well-formed `approval.granted` — changes the state of record until the gateway validates it (schema, signature, ordering, idempotency, token consume). Redis is a durable, ordered, replayable **log of claims**, not a command bus.

This principle is what makes it safe to use an at-least-once, untrusted transport for a security-critical workflow: the dangerous decisions (mint token, advance to `APPROVED`, command injection) are gated by the gateway's verification, not by message delivery.

### 0.1 Terminology (delta over the safety doc)

| Term | Meaning |
|---|---|
| **Stream** | A Redis Stream (`XADD`/`XREADGROUP`), an append-only, ID-ordered log. |
| **Entry ID** | The Redis-assigned `<ms>-<seq>` id of a stream entry. Monotonic **per stream**. |
| **Consumer group** | A named `XGROUP` providing at-least-once delivery, per-consumer PEL (pending entries list), and `XACK`. |
| **`event_id`** | Application-level ULID, the **idempotency key**. Globally unique per logical event. |
| **`seq`** | Application-level, **per-thread** monotonic integer. The ordering authority for a conversation (INV-018). |
| **`correlation_id`** | Stable id shared by every event in one end-to-end workflow (one inbound message → its draft → its approval → its injection → its observed send). |
| **`causation_id`** | The `event_id` of the single event that directly caused this one. Forms the causal DAG. |
| **PEL** | Pending Entries List — Redis-tracked, delivered-but-unacked entries per consumer group. |
| **DLQ** | Dead-letter queue: `stream:dead_letter`. |

### 0.2 Relationship to the state machine

This document does not redefine the draft lifecycle state machine — that is frozen in SAFETY-INVARIANT-001 §2 (Layer 2). Streams **carry the triggers** that the gateway's single transition function consumes. The mapping is explicit in Section 5. Every stream event that can drive a state transition names the transition it requests; the gateway remains free to reject it (forbidden transition → `SAFETY_VIOLATION`).

---

## 1. Stream topology

### 1.1 Design rules

1. **One writer of record per stream direction.** A stream has exactly one *authoritative* producer role. (Clients may be multiple physical devices, but they share one role, e.g. "a client".)
2. **The gateway is on one end of every safety-relevant stream.** Cognition and clients never talk to each other directly; the gateway brokers.
3. **Streams are typed by stage, not by event.** A stream carries a small, related family of event types for one pipeline stage. Event `type` (Section 2) disambiguates within a stream.
4. **Consumers use consumer groups, never raw `XREAD`.** This gives at-least-once delivery, crash-safe PEL recovery, and explicit `XACK`. (Exception: the PWA/extension may *tail* `stream:approval` read-only via `XREAD` for live UI, but the authoritative consumer is always a consumer group on the gateway.)
5. **`stream:audit` is a mirror, not a fork.** It is append-only and never consumed for control flow; it feeds the persistence worker and the forensic verifier (SAFETY-INVARIANT-001 §7).

### 1.2 Stream catalogue

| Stream | Owner | Producer (role) | Consumer group(s) | Carries | Retention |
|---|---|---|---|---|---|
| `stream:message_ingest` | Gateway | Extension | `cg:gateway-ingest` | `message.received`, `thread.updated` | `MAXLEN ~ 50_000`, ~14d |
| `stream:cognition_request` | Gateway | Gateway (dispatcher) | `cg:ai-brain` | `draft.requested`, `memory.retrieve.requested` | `MAXLEN ~ 20_000`, ~7d |
| `stream:cognition_result` | Gateway | AI Brain | `cg:gateway-cognition` | `draft.generated` | `MAXLEN ~ 20_000`, ~7d |
| `stream:approval` | Gateway | Gateway (requests) **+** Clients (decisions) | `cg:gateway-approval` (+ read-only client tails) | `approval.requested`, `approval.granted`, `approval.rejected` | `MAXLEN ~ 20_000`, ~30d |
| `stream:injection` | Gateway | Gateway (commands) **+** Extension (results) | `cg:gateway-injection`, `cg:extension-injection` | `injection.requested`, `injection.completed` | `MAXLEN ~ 20_000`, ~30d |
| `stream:observed_send` | Gateway | Extension | `cg:gateway-observe` | `human.send.observed` | `MAXLEN ~ 20_000`, ~30d |
| `stream:persistence` | Gateway | Gateway | `cg:persistence-worker` | `state.persisted` | `MAXLEN ~ 50_000`, ~7d (source of truth is SQLite/PG) |
| `stream:retry` | Gateway | Gateway (retry scheduler) | `cg:retry-worker` | `retry.requested` | `MAXLEN ~ 10_000`, ~7d |
| `stream:audit` | Gateway | All roles (mirror) | `cg:persistence-audit`, `cg:forensic` | every audit-relevant event (mirror) | **No `MAXLEN` trim by count**; time-anchored archival only — append-only forensic log |
| `stream:dead_letter` | Gateway | Any consumer (on give-up) | `cg:operator` (manual) | `dead_lettered` (envelope-wrapped failed event) | `MAXLEN ~ 50_000`, **manual drain only** |

> **Why two directions can share `stream:approval` and `stream:injection`:** the request and result event types are disjoint and the consumer groups are distinct per direction. The gateway's request events (`approval.requested`, `injection.requested`) are consumed by clients/extension; the reply events (`approval.granted|rejected`, `injection.completed`) are consumed by `cg:gateway-*`. Co-locating a request/reply pair on one stream keeps the causal chain in one log, which simplifies replay and forensic reconstruction. The alternative (split request/reply streams) was rejected to avoid cross-stream ordering reconciliation.

### 1.3 Ownership semantics

- **Owner** = the role responsible for the stream's existence, `MAXLEN`/retention policy, consumer-group creation (`XGROUP CREATE ... MKSTREAM`), and DLQ wiring. **The gateway owns every stream**, even those it does not produce on, because the gateway is the only component permitted to run `XGROUP`/`XSETID`/trim operations. The AI Brain and extension hold Redis credentials scoped (via Redis ACLs, Section 1.5) to `XADD` on their producer streams and `XREADGROUP`/`XACK` on their consumer streams only.
- **Producer** = the role(s) permitted to `XADD`. Enforced by Redis ACL, not convention.
- **Consumer** = the consumer group(s) that `XREADGROUP` + `XACK`.

### 1.4 Retention policy rationale

- **Control streams** (`ingest`, `cognition_*`, `approval`, `injection`, `observed_send`, `persistence`, `retry`) use **capped `MAXLEN ~`** (approximate trimming, `~` for performance). They are *operational buffers*; the durable state of record is SQLite/Postgres (gateway-owned). Losing trimmed history here loses nothing authoritative.
- **`stream:audit`** is **never trimmed by count.** It is the forensic chain (SAFETY-INVARIANT-001 §7). It is drained continuously into the append-only, hash-chained audit log in Postgres by `cg:persistence-audit`; only after a record is durably persisted and chain-anchored may a time-based archival job relocate (never delete) old entries to cold storage. INV-020 forbids any delete path.
- **`stream:dead_letter`** is never auto-trimmed; entries leave only via operator-driven recovery (Section 7.4).

### 1.5 Redis ACL sketch (least privilege per producer)

```
# AI Brain: read its work, write its results — nothing else. No approval, no injection.
ACL SETUSER ai-brain on >... ~stream:cognition_request ~stream:cognition_result \
    +xreadgroup +xack +xadd -@dangerous \
    resetkeys ~stream:cognition_request ~stream:cognition_result
# (No access to stream:approval, stream:injection, signing keys, or token nonce keys.)

# Extension: ingest + injection results + observed sends. Read injection commands. No approval grant write.
ACL SETUSER extension on >... ~stream:message_ingest ~stream:injection ~stream:observed_send \
    +xadd +xreadgroup +xack
# (Extension MAY XADD approval.granted? NO — see §3.6. Approval writes are PWA/ext via the gateway's
#  authenticated decision endpoint, which is the ONLY producer path that signs. See note below.)

# Gateway: full owner.
ACL SETUSER gateway on >... allkeys allcommands
```

> **Critical clarification on `stream:approval` producers.** Clients (PWA/extension) do **not** `XADD` `approval.granted` directly with self-asserted authority. A human decision is submitted to the gateway's authenticated decision endpoint (TLS, device-attested). The gateway is what mints the Ed25519 token (INV-008) and *then* `XADD`s the `approval.granted` event carrying that token onto `stream:approval` for the rest of the pipeline to consume. The "Clients" producer role in the table refers to the logical origin of the decision; the physical `XADD` of an authority-bearing approval event is performed by the gateway. This preserves INV-014: a transition to `APPROVED` originates only from an authenticated human decision event, and no stream-write capability granted to a client can manufacture one.

### 1.6 Topology diagram

```
  Extension ──message.received──▶ stream:message_ingest ──▶ cg:gateway-ingest ─┐
                                                                                │
                                              (gateway dispatch)               ▼
  cg:ai-brain ◀── stream:cognition_request ◀──draft.requested── Gateway (state authority)
       │                                                            ▲   │
   draft.generated                                                  │   │ approval.requested
       ▼                                                            │   ▼
  stream:cognition_result ──▶ cg:gateway-cognition ────────────────┘  stream:approval
                                                                        ▲        │
                                  human decision (authenticated HTTP)   │        │ (client tail, read-only)
                                  PWA/Extension ─────────────────▶ Gateway mints token
                                                                        │
                                  approval.granted (token-bearing) ─────┘
                                                                        │
                          injection.requested ──▶ stream:injection ──▶ cg:extension-injection
                                                       ▲                      │
                          injection.completed ─────────┘  ◀───────────────────┘
                                                                        │
  Extension ──human.send.observed──▶ stream:observed_send ──▶ cg:gateway-observe
                                                                        │
  (all stages mirror to)  stream:audit ──▶ cg:persistence-audit ──▶ append-only hash-chained log (PG)
                          stream:persistence ──▶ cg:persistence-worker ──▶ SQLite/Postgres state of record
```

---

## 2. Event naming convention

### 2.1 Grammar

```
<noun>.<past-tense-verb>[.<qualifier>]
```

- **noun** = the domain entity: `message`, `thread`, `memory`, `draft`, `approval`, `injection`, `human`, `state`, `retry`, plus the terminal `dead_lettered`.
- **verb** = **past tense for facts** (`received`, `generated`, `granted`, `completed`, `observed`, `persisted`), **`requested` for intents** that ask another component to act.
- **qualifier** = optional refinement (`memory.retrieve.requested`, `human.send.observed`).

**Rule N1 — Facts, not commands.** Event names describe *what happened* (`draft.generated`) or *what is being asked* (`draft.requested`), never imperatives (`generate_draft`, `send`). This reinforces the Transport Principle: the receiver decides what to do; the event does not command it. (And there is, by SAFETY-INVARIANT-001 G2/G3, no `*.send.requested` event — send is not a software capability. The only `send`-noun event is `human.send.observed`, a passive observation.)

**Rule N2 — `requested` implies a later terminal fact.** Every `*.requested` SHOULD have a corresponding `*.<completed|generated|granted|rejected|completed>` or be eligible for `retry.requested` / `dead_lettered`. The pairing is enforced by timeout reconciliation (Section 5.4).

**Rule N3 — lowercase, dot-delimited, no plurals, no camelCase.** Validated by CI against the enum in `contracts/event-types.yaml`.

### 2.2 The canonical event-type registry

```
message.received
thread.updated
memory.retrieve.requested
draft.requested
draft.generated
approval.requested
approval.granted
approval.rejected
injection.requested
injection.completed
human.send.observed
state.persisted
retry.requested
dead_lettered
```

This set is **closed**: adding an event type requires an ADR + a `contracts/event-types.yaml` update. Consumers MUST reject unknown types into the DLQ (Section 7), never silently ignore them.

### 2.3 Versioning strategy

- **`event_version`** is a required envelope field: a single integer, starting at `1`. It versions the **payload schema** of a given `type`, independently per type.
- **Compatibility contract:** producers and consumers follow **expand/contract**:
  - *Additive (backward-compatible) changes* — new **optional** field, new enum value a consumer can ignore — do **not** bump `event_version`. Consumers MUST ignore unknown fields (tolerant reader).
  - *Breaking changes* — removing/renaming a field, changing a type, making an optional field required — **bump `event_version`** and the consumer MUST support both the old and new version for the duration of one migration window (dual-read).
- **Migration window:** producers emit the new version only after all consumers advertise support (capability handshake on consumer-group startup, recorded in `state:consumer_caps`). This prevents a producer from emitting `v2` that a lagging consumer cannot parse → DLQ storms.
- **No in-place schema mutation of historical entries.** Past entries keep their original `event_version`; the forensic verifier maps versions through `contracts/migrations/`.

---

## 3. Event schemas

### 3.0 The common envelope

Every stream entry is a single Redis field map. Field `v` (the envelope) is a JSON object; large or binary fields (none currently) would be separate map fields. All events share this envelope:

```jsonc
{
  // --- identity & idempotency ---
  "event_id":       "01J8Z...ULID",      // REQUIRED, globally unique. Idempotency key. (INV-018)
  "type":           "draft.generated",    // REQUIRED, ∈ registry (§2.2)
  "event_version":  1,                     // REQUIRED, integer ≥ 1
  "timestamp":      "2026-06-03T10:15:04.221Z", // REQUIRED, RFC3339 UTC, millisecond precision

  // --- correlation & causality ---
  "correlation_id": "cor_01J8Z...",       // REQUIRED, stable across the whole workflow
  "causation_id":   "01J8Z...",            // REQUIRED for all but the root (message.received); event_id of direct cause
  "thread_id":      "li_thread_abc123",    // REQUIRED for all thread-scoped events
  "message_id":     "li_msg_555",          // REQUIRED where a specific inbound/outbound message is in scope
  "seq":            42,                     // REQUIRED for thread-scoped events: per-thread monotonic (§4)

  // --- provenance ---
  "producer":       "ai-brain",            // REQUIRED ∈ {extension, gateway, ai-brain, pwa}
  "producer_instance": "ai-brain-7c9f",    // REQUIRED, instance/host id for forensic attribution

  // --- type-specific payload ---
  "data":           { /* per-type schema below */ }
}
```

**Envelope rules:**
- `event_id` is the dedupe key. A consumer that has already processed an `event_id` (seen-set, Section 6.4) MUST `XACK` and skip — exactly-once *effect* over at-least-once *delivery*.
- `seq` is the **ordering authority** per thread; the Redis entry ID is the ordering authority per stream. They are reconciled in Section 4.
- `causation_id` + `correlation_id` make the entire workflow a replayable causal DAG, which the forensic verifier walks (SAFETY-INVARIANT-001 §7.1).
- Unknown top-level fields MUST be tolerated (forward-compat). Unknown `type` MUST be dead-lettered.

The per-type sections below specify only `data` plus any envelope field with a type-specific constraint.

---

### 3.1 `message.received`

A new inbound LinkedIn message was captured from the DOM by the extension. **Root event** of a workflow (no `causation_id`; it mints a new `correlation_id`).

- **Producer:** extension → **Stream:** `stream:message_ingest` → **Consumer:** `cg:gateway-ingest`
- **Required `data`:** `thread_id`, `message_id`, `sender` (`{urn, display_name}`), `body_text`, `captured_at`, `direction: "inbound"`, `thread_snapshot_hash`
- **Optional `data`:** `attachments[]` (metadata only), `prior_message_id`, `linkedin_thread_urn`
- **Envelope note:** `causation_id` ABSENT (root); gateway assigns `correlation_id` if the client did not, and assigns `seq` = next per-thread sequence on ingest.

```jsonc
{
  "event_id": "01J8Z0A...", "type": "message.received", "event_version": 1,
  "timestamp": "2026-06-03T10:14:50.001Z",
  "correlation_id": "cor_01J8Z0A...", "thread_id": "li_thread_abc123",
  "message_id": "li_msg_555", "seq": 41, "producer": "extension",
  "producer_instance": "ext-laptop-2",
  "data": {
    "direction": "inbound",
    "sender": { "urn": "urn:li:fsd_profile:XYZ", "display_name": "Dana R." },
    "body_text": "Are you free to chat Thursday about the role?",
    "captured_at": "2026-06-03T10:14:49.880Z",
    "thread_snapshot_hash": "sha256:aa12...",
    "attachments": []
  }
}
```

> **Provenance / prompt-injection note (T9):** `body_text` is **untrusted data**. The gateway tags it as untrusted before it ever reaches `stream:cognition_request`; the AI Brain treats it as content, never instruction. The stream contract carries this provenance via `data.direction` + an implicit untrusted flag the gateway sets on dispatch.

---

### 3.2 `thread.updated`

The thread's observable state changed in a way that is not a new inbound message — e.g., the human edited the composer, an outbound bubble appeared (precursor to `human.send.observed`), or read-state changed. Used to keep the gateway's thread mirror current.

- **Producer:** extension → **Stream:** `stream:message_ingest` → **Consumer:** `cg:gateway-ingest`
- **Required `data`:** `thread_id`, `change_kind` ∈ `{composer_changed, bubble_appeared, read_state, participants}`, `thread_snapshot_hash`, `observed_at`
- **Optional `data`:** `composer_text` (only for `composer_changed`; used to detect post-injection human edits → token invalidation, INV-017), `new_bubble_ref`

```jsonc
{
  "event_id": "01J8Z0B...", "type": "thread.updated", "event_version": 1,
  "timestamp": "2026-06-03T10:16:02.110Z",
  "correlation_id": "cor_01J8Z0A...", "causation_id": "01J8Z0A...",
  "thread_id": "li_thread_abc123", "message_id": "li_msg_555", "seq": 47,
  "producer": "extension", "producer_instance": "ext-laptop-2",
  "data": {
    "change_kind": "composer_changed",
    "composer_text": "Sure, Thursday at 2pm works.",
    "thread_snapshot_hash": "sha256:bb34...",
    "observed_at": "2026-06-03T10:16:02.000Z"
  }
}
```

> A `composer_changed` after `INJECTED` whose text hash differs from the approved `draft_hash` drives the `INJECTED → PENDING_APPROVAL` transition and **invalidates the token** (INV-017). This event is the wire-level trigger for SAFETY-INVARIANT-001 §2's "human edits injected text" arrow.

---

### 3.3 `memory.retrieve.requested`

Records the AI Brain's intent to perform a read-only memory/vector retrieval for a draft. **The retrieval itself is a synchronous, read-only node tool (`memory.vector_query`, SAFETY-INVARIANT-001 §4.3), not a stream RPC.** This event exists for **audit and observability** — it makes the cognition graph's reads visible in the forensic DAG without granting any actuation. There is intentionally no `memory.retrieve.completed` on a stream; the results return synchronously inside the graph.

- **Producer:** ai-brain → **Stream:** `stream:cognition_request` → **Consumer:** `cg:gateway-cognition` (audit mirror only; gateway does not act on it)
- **Required `data:`** `query_kind` ∈ `{thread_history, contact_profile, semantic}`, `query_digest` (hash of the query, not raw text), `top_k`
- **Optional `data:`** `filters`

```jsonc
{
  "event_id": "01J8Z0C...", "type": "memory.retrieve.requested", "event_version": 1,
  "timestamp": "2026-06-03T10:15:00.500Z",
  "correlation_id": "cor_01J8Z0A...", "causation_id": "01J8Z0D_draftrequested...",
  "thread_id": "li_thread_abc123", "message_id": "li_msg_555", "seq": 43,
  "producer": "ai-brain", "producer_instance": "ai-brain-7c9f",
  "data": { "query_kind": "semantic", "query_digest": "sha256:cc56...", "top_k": 8 }
}
```

> **Why a digest, not the query text:** keeps potentially sensitive retrieval terms out of the operational stream while preserving an auditable fingerprint. The raw query never leaves the AI Brain process.

---

### 3.4 `draft.requested`

The gateway dispatches a received message to cognition: "produce a draft for this thread/message." This is the wire form of the `RECEIVED → DRAFTING` transition's *downstream effect* (the transition itself is gateway-internal).

- **Producer:** gateway → **Stream:** `stream:cognition_request` → **Consumer:** `cg:ai-brain`
- **Required `data:`** `thread_id`, `trigger_message_id`, `thread_context_ref` (gateway-provided, read-only handle to assembled context), `idempotency_scope` (the message_id; one draft per inbound by default), `graph_run_id`
- **Optional `data:`** `style_hints`, `prior_draft_id` (for re-draft after edit)

```jsonc
{
  "event_id": "01J8Z0D...", "type": "draft.requested", "event_version": 1,
  "timestamp": "2026-06-03T10:14:59.900Z",
  "correlation_id": "cor_01J8Z0A...", "causation_id": "01J8Z0A...",
  "thread_id": "li_thread_abc123", "message_id": "li_msg_555", "seq": 42,
  "producer": "gateway", "producer_instance": "gw-1",
  "data": {
    "trigger_message_id": "li_msg_555",
    "thread_context_ref": "ctx_ref_889",
    "idempotency_scope": "li_msg_555",
    "graph_run_id": "run_456"
  }
}
```

---

### 3.5 `draft.generated`

Cognition completed and the graph is **suspended at `APPROVAL_GATE`**. The event carries **draft data only** (`DraftArtifact`, SAFETY-INVARIANT-001 §1 G1 / INV-002). It carries no token, no command, no actuation.

- **Producer:** ai-brain → **Stream:** `stream:cognition_result` → **Consumer:** `cg:gateway-cognition`
- **Required `data:`** `draft_id`, `graph_run_id`, `checkpoint_id`, `draft` (`DraftArtifact`: `{ text, structured_fields?, slot_choices? }`), `draft_hash` (SHA-256 over canonicalized `draft` — the value the token will later bind, INV-017), `model_meta`
- **Optional `data:`** `meeting_intent` (bool + suggested `slots[]`, read-only free/busy)

```jsonc
{
  "event_id": "01J8Z0E...", "type": "draft.generated", "event_version": 1,
  "timestamp": "2026-06-03T10:15:03.700Z",
  "correlation_id": "cor_01J8Z0A...", "causation_id": "01J8Z0D...",
  "thread_id": "li_thread_abc123", "message_id": "li_msg_555", "seq": 44,
  "producer": "ai-brain", "producer_instance": "ai-brain-7c9f",
  "data": {
    "draft_id": "drf_789", "graph_run_id": "run_456", "checkpoint_id": "ckpt_456_gate",
    "draft": { "text": "Thursday works — does 2pm suit you? Happy to send a calendar hold.",
               "slot_choices": ["2026-06-05T14:00:00Z"] },
    "draft_hash": "sha256:9f2c...",
    "meeting_intent": { "detected": true, "slots": ["2026-06-05T14:00:00Z"] },
    "model_meta": { "model": "ollama/llama3.1", "tokens": 142 }
  }
}
```

> **The gateway recomputes `draft_hash`** from `data.draft` on consume and rejects on mismatch. It does not trust the producer's hash; the field is a convenience + cross-check, not the source of truth. Transition driven: `DRAFTING → PENDING_APPROVAL`.

---

### 3.6 `approval.requested`

The gateway asks the human to decide. Fan-out to all of the user's clients (PWA, extension). Carries everything a client needs to render the decision — and nothing that could actuate.

- **Producer:** gateway → **Stream:** `stream:approval` → **Consumer:** client tails (read-only) + `cg:gateway-approval` records its own emission for reconciliation
- **Required `data:`** `draft_id`, `draft` (display copy), `draft_hash`, `graph_run_id`, `checkpoint_id`, `expires_at` (decision-request TTL, distinct from token TTL), `thread_summary`
- **Optional `data:`** `meeting_slots[]`, `edit_allowed: true`

```jsonc
{
  "event_id": "01J8Z0F...", "type": "approval.requested", "event_version": 1,
  "timestamp": "2026-06-03T10:15:04.000Z",
  "correlation_id": "cor_01J8Z0A...", "causation_id": "01J8Z0E...",
  "thread_id": "li_thread_abc123", "message_id": "li_msg_555", "seq": 45,
  "producer": "gateway", "producer_instance": "gw-1",
  "data": {
    "draft_id": "drf_789", "draft": { "text": "Thursday works — does 2pm suit you? ..." },
    "draft_hash": "sha256:9f2c...", "graph_run_id": "run_456", "checkpoint_id": "ckpt_456_gate",
    "expires_at": "2026-06-03T10:30:04.000Z", "edit_allowed": true,
    "meeting_slots": ["2026-06-05T14:00:00Z"]
  }
}
```

---

### 3.7 `approval.granted`

A human approved (possibly with edits). **This is the most security-critical event on the backbone.** Per Section 1.5, the physical `XADD` is performed by the **gateway** after it authenticates the human decision and mints the Ed25519 token (INV-008, INV-014). The event carries the **minted token**, so downstream stages (resume, injection) can verify it.

- **Producer:** gateway (on behalf of an authenticated human decision from PWA/extension) → **Stream:** `stream:approval` → **Consumer:** `cg:gateway-approval` (drives resume + injection)
- **Required `data:`** `draft_id`, `decision` ∈ `{approve, approve_with_edit}`, `approved_draft_hash` (the hash actually bound — recomputed over the *edited* content if `approve_with_edit`), `approval_token` (compact JWS per SAFETY-INVARIANT-001 §6.2), `approver_id`, `device_id`, `graph_run_id`, `checkpoint_id`
- **Optional `data:`** `edited_text` (present iff `approve_with_edit`)
- **Envelope note:** `producer` MUST be `gateway`. A consumer that sees `type=approval.granted` with `producer != gateway` MUST treat it as a `SAFETY_VIOLATION` and dead-letter it (defense in depth against a client forging the event).

```jsonc
{
  "event_id": "01J8Z0G...", "type": "approval.granted", "event_version": 1,
  "timestamp": "2026-06-03T10:15:40.220Z",
  "correlation_id": "cor_01J8Z0A...", "causation_id": "01J8Z0F...",
  "thread_id": "li_thread_abc123", "message_id": "li_msg_555", "seq": 46,
  "producer": "gateway", "producer_instance": "gw-1",
  "data": {
    "draft_id": "drf_789", "decision": "approve_with_edit",
    "edited_text": "Thursday at 2pm works great — sending a hold now.",
    "approved_draft_hash": "sha256:7be1...",   // hash of the EDITED content; token binds THIS
    "approval_token": "eyJhbGciOiJFZERTQS...", // Ed25519 JWS, jti single-use, exp<=900s
    "approver_id": "user_primary", "device_id": "phone-pwa",
    "graph_run_id": "run_456", "checkpoint_id": "ckpt_456_gate"
  }
}
```

> **The token in `data.approval_token` is verified, not trusted.** Every consumer that acts on it (resume in AI Brain; injection in extension) verifies the Ed25519 signature against the gateway's **public** key, checks `exp`, `thread_id`, `draft_hash` == `approved_draft_hash`, and (gateway only) atomically consumes `jti`. A replayed `approval.granted` finds `jti` already consumed → rejected (INV-016, T3). Transition driven: `PENDING_APPROVAL → APPROVED`.

---

### 3.8 `approval.rejected`

The human rejected the draft. **No token is minted** (SAFETY-INVARIANT-001 §6.1, decision-typed). Terminal for this draft.

- **Producer:** gateway (on behalf of human decision) → **Stream:** `stream:approval` → **Consumer:** `cg:gateway-approval`
- **Required `data:`** `draft_id`, `decision: "reject"`, `approver_id`, `device_id`, `reason_code` ∈ `{not_now, wrong_tone, factually_wrong, other}`
- **Optional `data:`** `reason_text`, `request_redraft: bool`

```jsonc
{
  "event_id": "01J8Z0H...", "type": "approval.rejected", "event_version": 1,
  "timestamp": "2026-06-03T10:15:55.000Z",
  "correlation_id": "cor_01J8Z0A...", "causation_id": "01J8Z0F...",
  "thread_id": "li_thread_abc123", "message_id": "li_msg_555", "seq": 46,
  "producer": "gateway", "producer_instance": "gw-1",
  "data": { "draft_id": "drf_789", "decision": "reject", "approver_id": "user_primary",
            "device_id": "phone-pwa", "reason_code": "wrong_tone", "request_redraft": false }
}
```

> Transition driven: `PENDING_APPROVAL → REJECTED` (terminal). If `request_redraft: true`, the gateway *additionally* emits a fresh `draft.requested` with a new `graph_run_id` (a new workflow leg under the same `correlation_id`).

---

### 3.9 `injection.requested`

The gateway commands the extension to inject the approved text into the composer. Issued **only after** the token's `jti` is atomically consumed (`APPROVED → READY_FOR_INJECTION`, INV-016). Injection is NOT sending (SAFETY-INVARIANT-001 §0.1).

- **Producer:** gateway → **Stream:** `stream:injection` → **Consumer:** `cg:extension-injection`
- **Required `data:`** `draft_id`, `inject_text`, `inject_text_hash` (== approved `draft_hash`), `consumed_jti` (reference to the consumed token nonce — proves a real human approval preceded this), `inject_command_sig` (gateway-signed command, so the extension verifies authenticity, SAFETY-INVARIANT-001 §4.4), `target_thread_id`, `bundle_hash_expected` (extension attestation pin)
- **Optional `data:`** `slot_ics_ref` (calendar hold to attach, if any)

```jsonc
{
  "event_id": "01J8Z0I...", "type": "injection.requested", "event_version": 1,
  "timestamp": "2026-06-03T10:15:41.000Z",
  "correlation_id": "cor_01J8Z0A...", "causation_id": "01J8Z0G...",
  "thread_id": "li_thread_abc123", "message_id": "li_msg_555", "seq": 48,
  "producer": "gateway", "producer_instance": "gw-1",
  "data": {
    "draft_id": "drf_789",
    "inject_text": "Thursday at 2pm works great — sending a hold now.",
    "inject_text_hash": "sha256:7be1...",
    "consumed_jti": "01J...ULID",
    "inject_command_sig": "ed25519:4f9a...",
    "target_thread_id": "li_thread_abc123",
    "bundle_hash_expected": "sha256:ext-bundle-..."
  }
}
```

> **The extension refuses to inject** unless (a) `inject_command_sig` verifies against the gateway public key, (b) `consumed_jti` is present, and (c) its own running bundle hash equals `bundle_hash_expected` (SAFETY-INVARIANT-001 §4.4, T6). The extension still has **no capability to click Send** — it only writes the composer (Layer 3, INV-013). Transition driven: `APPROVED → READY_FOR_INJECTION` precedes this emit; the emit triggers the extension's work.

---

### 3.10 `injection.completed`

The extension confirms it wrote the text into the composer DOM. Pure acknowledgement; no send occurred.

- **Producer:** extension → **Stream:** `stream:injection` → **Consumer:** `cg:gateway-injection`
- **Required `data:`** `draft_id`, `consumed_jti`, `result` ∈ `{injected, refused, dom_error}`, `composer_text_hash` (post-injection composer hash, must equal `inject_text_hash`), `injected_at`
- **Optional `data:`** `refusal_reason` (for `refused`/`dom_error`)

```jsonc
{
  "event_id": "01J8Z0J...", "type": "injection.completed", "event_version": 1,
  "timestamp": "2026-06-03T10:15:41.480Z",
  "correlation_id": "cor_01J8Z0A...", "causation_id": "01J8Z0I...",
  "thread_id": "li_thread_abc123", "message_id": "li_msg_555", "seq": 49,
  "producer": "extension", "producer_instance": "ext-laptop-2",
  "data": { "draft_id": "drf_789", "consumed_jti": "01J...ULID", "result": "injected",
            "composer_text_hash": "sha256:7be1...", "injected_at": "2026-06-03T10:15:41.470Z" }
}
```

> Transition driven: `READY_FOR_INJECTION → INJECTED` (on `result: injected` with matching hash). A `refused`/`dom_error` does **not** advance state; it routes to retry (Section 7) or surfaces to the human. A `composer_text_hash` mismatch is a `SAFETY_VIOLATION` (injected content ≠ approved content).

---

### 3.11 `human.send.observed`

The extension **observed** (via DOM mutation — a new outbound bubble) that the human clicked Send. This is the only `send`-noun event and it is strictly passive (SAFETY-INVARIANT-001 §0.1, INV-013, INV-021). The extension did not and cannot perform the send.

- **Producer:** extension → **Stream:** `stream:observed_send` → **Consumer:** `cg:gateway-observe`
- **Required `data:`** `draft_id`, `observed_outbound_message_id`, `observed_text_hash`, `observed_at`, `evidence` (`{bubble_ref, mutation_kind}`)
- **Optional `data:`** `latency_from_inject_ms`

```jsonc
{
  "event_id": "01J8Z0K...", "type": "human.send.observed", "event_version": 1,
  "timestamp": "2026-06-03T10:16:10.900Z",
  "correlation_id": "cor_01J8Z0A...", "causation_id": "01J8Z0J...",
  "thread_id": "li_thread_abc123", "message_id": "li_msg_555", "seq": 50,
  "producer": "extension", "producer_instance": "ext-laptop-2",
  "data": {
    "draft_id": "drf_789", "observed_outbound_message_id": "li_msg_561",
    "observed_text_hash": "sha256:7be1...", "observed_at": "2026-06-03T10:16:10.850Z",
    "evidence": { "bubble_ref": "msg-bubble-561", "mutation_kind": "childList_add" }
  }
}
```

> Transition driven: `INJECTED → SENT_BY_HUMAN` (terminal, INV-021). The gateway records this only from this event and only when prior state is `INJECTED`. There is no gateway-initiated path to `SENT_BY_HUMAN`. The `observed_text_hash` ideally matches the approved `draft_hash`; a divergence (human typed something else entirely before sending) is recorded as `SENT_BY_HUMAN` with a `content_diverged` audit flag — still a human send, just not of our draft.

---

### 3.12 `state.persisted`

The gateway's single transition function committed a state change to the state of record (SQLite/Postgres). Emitted **after** the authoritative write, so the stream reflects committed truth. Drives the persistence worker's projections / read models and the audit mirror.

- **Producer:** gateway → **Stream:** `stream:persistence` → **Consumer:** `cg:persistence-worker`
- **Required `data:`** `draft_id`, `from_state`, `to_state`, `transition_trigger` (the `event_id` that caused it == this event's `causation_id`), `audit_seq` (the append-only audit log seq, SAFETY-INVARIANT-001 §7.3), `committed_at`
- **Optional `data:`** `jti` (for mint/consume transitions), `draft_hash`

```jsonc
{
  "event_id": "01J8Z0L...", "type": "state.persisted", "event_version": 1,
  "timestamp": "2026-06-03T10:15:41.010Z",
  "correlation_id": "cor_01J8Z0A...", "causation_id": "01J8Z0G...",
  "thread_id": "li_thread_abc123", "message_id": "li_msg_555", "seq": 47,
  "producer": "gateway", "producer_instance": "gw-1",
  "data": { "draft_id": "drf_789", "from_state": "PENDING_APPROVAL", "to_state": "APPROVED",
            "transition_trigger": "01J8Z0G...", "audit_seq": 10432, "jti": "01J...ULID",
            "draft_hash": "sha256:7be1..." }
}
```

> `state.persisted` is a *fact-after-commit*. It is never the trigger of another transition — it is a read-model feed. This avoids circular causality (a persistence event causing a state change causing another persistence event).

---

### 3.13 `retry.requested`

A consumer failed to process an event but the failure is retryable (transient). The retry scheduler re-enqueues with backoff metadata. (Mechanics in Section 7.)

- **Producer:** gateway (retry scheduler; also any consumer requesting a retry) → **Stream:** `stream:retry` → **Consumer:** `cg:retry-worker`
- **Required `data:`** `original_event_id`, `original_stream`, `original_type`, `attempt` (1-based), `max_attempts`, `next_attempt_at`, `last_error` (`{code, message_digest}`)
- **Optional `data:`** `backoff_ms`

```jsonc
{
  "event_id": "01J8Z0M...", "type": "retry.requested", "event_version": 1,
  "timestamp": "2026-06-03T10:15:42.000Z",
  "correlation_id": "cor_01J8Z0A...", "causation_id": "01J8Z0I...",
  "thread_id": "li_thread_abc123", "message_id": "li_msg_555", "seq": 48,
  "producer": "gateway", "producer_instance": "gw-retry",
  "data": { "original_event_id": "01J8Z0I...", "original_stream": "stream:injection",
            "original_type": "injection.requested", "attempt": 2, "max_attempts": 5,
            "next_attempt_at": "2026-06-03T10:15:44.000Z", "backoff_ms": 2000,
            "last_error": { "code": "EXT_TIMEOUT", "message_digest": "sha256:..." } }
}
```

> **Retries never bypass safety.** A retried `injection.requested` still references the same `consumed_jti`; since the token is already consumed, the gateway does not re-mint or re-consume — it re-issues the *command* idempotently keyed on `draft_id + jti`. A retry can never cause a second token consume or a second state advance (Section 6).

---

### 3.14 `dead_lettered`

An event that exhausted retries, failed schema validation, was type-unknown, or was a poison message. Wrapped, never executed.

- **Producer:** any consumer (on give-up) → **Stream:** `stream:dead_letter` → **Consumer:** `cg:operator` (manual, Section 7.4)
- **Required `data:`** `original_envelope` (verbatim, including original `event_id`), `failure_class` ∈ `{schema_invalid, unknown_type, max_retries_exceeded, poison, safety_violation, signature_invalid}`, `failure_detail`, `attempts`, `first_seen_at`, `dead_lettered_at`, `source_stream`, `source_group`
- **Optional `data:`** `quarantine_ref`

```jsonc
{
  "event_id": "01J8Z0N...", "type": "dead_lettered", "event_version": 1,
  "timestamp": "2026-06-03T10:15:50.000Z",
  "correlation_id": "cor_01J8Z0A...", "causation_id": "01J8Z0I...",
  "thread_id": "li_thread_abc123", "producer": "gateway", "producer_instance": "gw-injection",
  "data": {
    "original_envelope": { /* full failed event verbatim */ },
    "failure_class": "max_retries_exceeded", "failure_detail": "EXT_TIMEOUT x5",
    "attempts": 5, "first_seen_at": "2026-06-03T10:15:41.000Z",
    "dead_lettered_at": "2026-06-03T10:15:50.000Z",
    "source_stream": "stream:injection", "source_group": "cg:extension-injection"
  }
}
```

> A `failure_class` of `safety_violation` or `signature_invalid` is **alarm-worthy** and is also mirrored to `stream:audit` as a `SAFETY_VIOLATION` record (SAFETY-INVARIANT-001 §4.4, T5). These are never auto-recovered.

---

## 4. Ordering guarantees

### 4.1 Two ordering authorities, reconciled

1. **Per-stream order** — the Redis entry ID (`<ms>-<seq>`) is strictly monotonic within a stream. `XREADGROUP` delivers in entry-ID order per consumer.
2. **Per-thread order** — the application `seq` field is strictly monotonic **per `thread_id`**, assigned by the gateway. This is the authority that matters for correctness (INV-018), because a workflow's events traverse *multiple* streams; entry-ID order across streams is meaningless, but `seq` order within a thread is total.

**`seq` assignment** is owned by the gateway and is atomic: `HINCRBY state:thread_seq:<thread_id> next 1` (or an `INCR` per-thread counter) under the thread's lock. Producers that are not the gateway (extension, AI Brain) **request** a `seq` slot or carry a provisional ordering that the gateway normalizes on ingest. In practice:
- Extension-origin events (`message.received`, `thread.updated`, `injection.completed`, `human.send.observed`): the gateway assigns/normalizes `seq` on consume.
- Gateway-origin events: `seq` assigned at emit.
- AI-Brain-origin events (`draft.generated`, `memory.retrieve.requested`): carry the `seq` derived from the `draft.requested` that caused them; the gateway validates monotonicity on consume.

### 4.2 Per-thread serialization

A single conversation is processed **serially**. The gateway partitions thread work by `hash(thread_id) % N` to a fixed worker, and holds a per-thread lock (`SET lock:thread:<id> <owner> NX PX <ttl>`) during a transition. This guarantees that two events for the same thread never apply concurrently, so the state machine sees a clean, ordered sequence. Different threads run fully in parallel (Section 9).

### 4.3 Out-of-order handling

Because delivery is at-least-once and multi-stream, a consumer may see `seq=44` before `seq=42` for a thread. Policy:
- The gateway tracks `state:thread_applied_seq:<thread_id>` = highest contiguously-applied `seq`.
- An event with `seq == applied+1` applies immediately, then drains any buffered successors.
- An event with `seq > applied+1` is **parked** in a small per-thread reorder buffer (`ZADD reorder:<thread_id> <seq> <event_id>`, TTL-bounded) and **not acked yet** — it stays in the PEL so it is redelivered if the consumer crashes.
- An event with `seq <= applied` is a **duplicate or stale**; it is deduped (Section 4.4 / 6.4) and acked without effect.
- If a gap persists beyond `REORDER_TIMEOUT` (default 30s), the gateway emits a `retry.requested` for the missing predecessor's expected source and alarms; it does **not** apply out of order.

> **Safety note:** out-of-order can never produce an unsafe transition, because the state machine itself rejects any transition not valid from the current state (SAFETY-INVARIANT-001 §2 forbidden transitions). Reordering is a *liveness/correctness* optimization; the *safety* floor is the transition table, not the buffer.

### 4.4 Dedupe strategy

- **Primary key:** `event_id` (ULID). A consumer checks `SET seen:<group>:<event_id> 1 NX EX <ttl>`. If the key already exists → duplicate → `XACK` + skip. TTL is sized to exceed max redelivery window (default 24h for control streams).
- **Secondary, per-thread:** `seq <= applied_seq` is treated as already-processed (handles the case where the seen-set TTL expired but the thread cursor has moved past it).
- **Tertiary, effect-level:** the token `jti` single-use consume (INV-016) and the state machine's idempotent transitions make a *second* outbound-adjacent effect impossible even if both dedupe layers miss (defense in depth, T8).

### 4.5 Idempotency keys (per event type)

| Event | Idempotency key | Idempotent effect |
|---|---|---|
| `message.received` | `thread_id + message_id` | one workflow per inbound message |
| `draft.requested` | `idempotency_scope` (= message_id) | one in-flight draft per inbound |
| `draft.generated` | `graph_run_id + draft_id` | one PENDING_APPROVAL per run |
| `approval.granted` | `jti` (token nonce) | one APPROVED; second consume rejected |
| `injection.requested` | `draft_id + consumed_jti` | one injection command per consumed token |
| `injection.completed` | `draft_id + consumed_jti` | one INJECTED |
| `human.send.observed` | `observed_outbound_message_id` | one SENT_BY_HUMAN per observed bubble |
| `state.persisted` | `audit_seq` | one projection apply |

> The idempotency key is **distinct from `event_id`.** `event_id` dedupes *redeliveries of the same event*; the idempotency key dedupes *distinct events that request the same effect* (e.g., a retried `injection.requested` has a new `event_id` but the same `draft_id + consumed_jti`).

---

## 5. Approval event flow (the happy path + failures)

### 5.1 Canonical lifecycle on the wire

```
 (extension)  message.received           → stream:message_ingest      [seq n]
 (gateway)    RECEIVED → DRAFTING, emits draft.requested → stream:cognition_request
 (ai-brain)   [optional] memory.retrieve.requested → stream:cognition_request   (audit only)
 (ai-brain)   draft.generated  (suspended @ APPROVAL_GATE) → stream:cognition_result
 (gateway)    DRAFTING → PENDING_APPROVAL, emits approval.requested → stream:approval
 (human)      decides on PWA/extension → authenticated decision endpoint
 (gateway)    mints Ed25519 token, emits approval.granted (token-bearing) → stream:approval
 (gateway)    verify+consume jti: PENDING_APPROVAL → APPROVED → READY_FOR_INJECTION,
              emits injection.requested → stream:injection
 (extension)  verifies command sig + jti + bundle hash, injects, emits injection.completed
 (gateway)    READY_FOR_INJECTION → INJECTED
 (human)      clicks Send (the ONLY sender)
 (extension)  observes new outbound bubble, emits human.send.observed → stream:observed_send
 (gateway)    INJECTED → SENT_BY_HUMAN (terminal)
 (gateway)    state.persisted emitted at each commit → stream:persistence; all mirrored → stream:audit
```

Aligned to the requested chain:
`message.received → draft.requested → draft.generated → approval.requested → approval.granted → injection.requested → injection.completed → human.send.observed`.

### 5.2 Causal chain (every step links to its parent)

```
message.received (root, correlation_id=cor_X, no causation)
  └─ draft.requested            causation = message.received
       └─ draft.generated       causation = draft.requested
            └─ approval.requested      causation = draft.generated
                 └─ approval.granted   causation = approval.requested   ← token minted here
                      └─ injection.requested  causation = approval.granted
                           └─ injection.completed causation = injection.requested
                                └─ human.send.observed causation = injection.completed
```

The forensic verifier walks this chain and asserts the SAFETY-INVARIANT-001 §7.4 negative-evidence property: every `human.send.observed`/`SENT_BY_HUMAN` has a preceding token-bearing `approval.granted` tied to an authenticated human decision with a matching `draft_hash`.

### 5.3 Failure cases

| Failure point | Symptom | Handling |
|---|---|---|
| `draft.requested` never produces `draft.generated` | cognition timeout / AI Brain crash | Reconciler (5.4) detects PENDING `draft.requested` past TTL → `retry.requested`; after `max_attempts` → DLQ + surface "draft failed" to human. State stays `DRAFTING`; never auto-advances. |
| `draft.generated` schema invalid | malformed `DraftArtifact` | Validation fails → DLQ (`schema_invalid`). State stays `DRAFTING`. No PENDING_APPROVAL. (T7) |
| Human never decides | `approval.requested` TTL (`expires_at`) elapses | Gateway expires the request, emits a fresh `approval.requested` or surfaces "expired — re-request?". No token, no advance. State stays `PENDING_APPROVAL`. |
| Duplicate `approval.granted` | replay / double-tap | Second `jti` consume fails (INV-016) → rejected + `SAFETY_VIOLATION` audit. Exactly one APPROVED. (T3) |
| `approval.granted` with stale token | edited draft after approval | `approved_draft_hash` ≠ current `draft_hash` → token invalid (INV-017) → reject; `INJECTED → PENDING_APPROVAL` if already injected. (T4) |
| `injection.requested` not acked | extension offline / DOM error | `injection.completed` absent past TTL → `retry.requested` (same `consumed_jti`, idempotent). After `max_attempts` → DLQ; human notified; token already consumed so no re-mint — draft must be re-approved if abandoned. |
| `injection.completed` hash mismatch | injected ≠ approved | `SAFETY_VIOLATION`; no advance to INJECTED; alarm. |
| `human.send.observed` for a thread not in `INJECTED` | human sent something unrelated, or out-of-order | If state ≠ INJECTED, the observe is recorded as an *independent human send* (audit), not a state advance of our draft. INV-021 keeps `SENT_BY_HUMAN` reachable only from INJECTED for *our* draft. |
| Extension reports `bundle_hash` mismatch | tampered/old extension | Gateway refuses to emit `injection.requested` (T6); attestation failure alarmed. |

### 5.4 Timeout reconciliation (the "requested implies terminal" enforcer)

A reconciler scans `state:pending:<stage>` (a per-stage ZSET keyed by deadline) and, for any `*.requested` past its stage TTL with no matching terminal event, emits `retry.requested` (transient) or routes to DLQ (exhausted). This closes Rule N2: no `*.requested` is left dangling forever.

---

## 6. Replay protection

The system is designed under explicit assumption of **at-least-once delivery and a potentially hostile/buggy stream** (SAFETY-INVARIANT-001 T3, T8). Four layers:

### 6.1 Duplicate delivery (transport-level)
- Consumer groups + PEL + `XACK`: an unacked entry is redelivered after `XAUTOCLAIM`/visibility timeout. This *intentionally* allows duplicates for crash-safety.
- `event_id` seen-set dedupe (Section 4.4) absorbs duplicates into a no-op + `XACK`.

### 6.2 Stale approval events (time-level)
- Tokens carry `exp` (≤900s, SAFETY-INVARIANT-001 §6.1). A late `approval.granted` whose token has expired is rejected on consume (INV-016).
- `approval.requested` carries `expires_at`; a decision arriving after expiry is rejected before any mint.
- `draft_hash` binding rejects approvals against a since-edited draft (INV-017).

### 6.3 Replay-attack mitigation (adversary-level)
- **Single-use `jti`:** atomic consume via Lua CAS — `if GET jti == "unconsumed" then SET jti consumed` — so a replayed token finds `consumed` and is rejected (INV-016, T3). The nonce key has TTL = token `exp` + grace, after which the seq cursor (6.4) covers it.
- **Signature:** every authority-bearing event (`approval.granted` token, `injection.requested` command) is Ed25519-signed by the gateway; a forged or AI-minted token fails verification (INV-008, T2). The AI Brain holds only the public key and cannot sign.
- **`producer == gateway` check** on `approval.granted` (Section 3.7) blocks a client from `XADD`-ing a forged grant even if it obtained stream write access.
- **Per-thread `seq` monotonicity:** a replayed event carries an already-applied `seq` → deduped (4.4).

### 6.4 Consumed-event tracking
Three cooperating cursors per `(group, thread)`:
1. **`seen:<group>:<event_id>`** (SET NX EX) — short-horizon exact dedupe.
2. **`state:thread_applied_seq:<thread_id>`** — long-horizon "everything ≤ this seq is done" cursor; survives seen-set TTL expiry.
3. **`jti consumed`** marker — effect-level single-use for the one event type that actuates (approval/injection).

A consumed-event audit is also written to `stream:audit` (`TOKEN_CONSUMED`, SAFETY-INVARIANT-001 §7.3), so consumption is forensically provable independent of Redis liveness.

---

## 7. Dead-letter strategy

### 7.1 Retry counts & classification
On consume failure, the consumer classifies:
- **Transient** (`EXT_TIMEOUT`, `OLLAMA_BUSY`, `REDIS_TIMEOUT`, lock contention) → retryable.
- **Permanent** (`schema_invalid`, `unknown_type`, `signature_invalid`, `safety_violation`) → **straight to DLQ, no retry.** Retrying a poison/forged message wastes cycles and, worse, retrying a `safety_violation` must never be attempted.

Default `max_attempts = 5` for transient (per-stream overridable in `contracts/retry-policy.yaml`).

### 7.2 Backoff strategy
Exponential with full jitter:
```
backoff_ms = min(BASE * 2^(attempt-1), CAP) ± jitter
BASE = 250ms,  CAP = 30_000ms
```
The retry scheduler stores `next_attempt_at` in a ZSET (`retry:schedule`, score = epoch ms); the `cg:retry-worker` pops due entries and re-emits to the original stream. Backoff prevents a flapping consumer (e.g., AI Brain restart) from hot-looping. Idempotency (Section 4.5) guarantees retries are safe.

### 7.3 Poison messages
A message that fails deterministically (schema/type/signature) is poison: it will fail every retry, so it is dead-lettered on first failure (`failure_class` ∈ `{schema_invalid, unknown_type, signature_invalid}`). It is `XACK`-ed on the source stream (to clear the PEL) **only after** the `dead_lettered` wrapper is durably `XADD`-ed to `stream:dead_letter` — i.e., move-then-ack, never ack-then-move, so a crash mid-DLQ cannot lose the message.

### 7.4 Manual recovery flow
DLQ entries leave only via an operator (`cg:operator`):
1. **Inspect** — operator tool reads `stream:dead_letter`, groups by `failure_class` + `original_type`.
2. **Triage:**
   - `schema_invalid` from a since-fixed producer bug → after deploying the fix, **replay** the `original_envelope` to its `source_stream` (a fresh `event_id`, preserving `correlation_id`/`causation_id`/`seq`). Idempotency makes replay safe.
   - `max_retries_exceeded` (e.g., extension was offline) → re-emit once the dependency is healthy.
   - `safety_violation` / `signature_invalid` → **never auto-replayed.** Investigated as a potential attack (T2/T3/T5). Closed with an incident note in the audit log; the underlying draft, if any, must be freshly re-approved by a human — there is no operator path to mint a token.
3. **Close** — recovery actions are themselves audited; the DLQ entry is marked `recovered` (a tombstone written to `stream:audit`), never silently deleted.

> **No operator capability escalates safety.** An operator can replay transport, but cannot mint tokens, force state transitions, or fabricate approvals. The strongest operator action is "re-deliver a fact for the gateway to re-validate."

---

## 8. Redis Streams vs Pub/Sub — engineering justification

**Decision: Redis Streams. Pub/Sub is rejected for every control path.** This is opinionated and load-bearing for the safety guarantees.

| Property | Pub/Sub | Streams | Why it matters here |
|---|---|---|---|
| **Delivery on consumer downtime** | Fire-and-forget; a subscriber that is offline **misses the message permanently** | Persisted log; consumer reads from its last cursor when it returns | An offline AI Brain or extension must **not** drop an approval or an inbound message. Losing an `approval.granted` would strand a human decision; losing a `human.send.observed` would corrupt the audit chain. Non-negotiable. |
| **Replayability** | None | `XRANGE`/`XREAD` from any ID; rebuild read models | The forensic verifier (SAFETY-INVARIANT-001 §7) and read-model rebuilds require replay. |
| **Consumer groups / load balancing** | No native groups | `XREADGROUP` with PEL, `XACK`, `XAUTOCLAIM` | Per-thread serialization + crash recovery (Section 4.2) need PEL + claim semantics. |
| **At-least-once + ack** | No ack; at-most-once | Explicit `XACK`; unacked → redelivered | Safety depends on *never silently losing* a safety event; we accept duplicates (and dedupe them) but reject loss. |
| **Ordering** | Per-channel best-effort, no recovery | Strict per-stream entry-ID order + our per-thread `seq` | INV-018 ordering guarantee. |
| **Backpressure / buffering** | Subscriber must keep up or lose data | Log absorbs bursts; consumer drains at its pace | Multiple concurrent conversations (Section 9) burst; Ollama is slower than ingest. |
| **Crash safety of in-flight work** | Lost | PEL retains delivered-unacked entries | A gateway crash mid-transition redelivers, and idempotency replays it safely. |
| **Dead-letter** | Impossible (nothing to retain) | Natural (move to DLQ stream) | Poison-message handling (Section 7) is impossible on Pub/Sub. |

**The decisive argument:** this is a *safety-critical, audit-mandatory* workflow where the cost of **dropping** an event (a stranded approval, a missing observed-send) is far higher than the cost of **duplicating** one (absorbed by idempotency). Pub/Sub optimizes for the wrong tradeoff (low-latency fan-out, lossy). Streams give a durable, ordered, replayable, ackable log — which is exactly the substrate an event-sourced state authority and an append-only audit chain require.

**Where Pub/Sub-like fan-out is genuinely wanted** (live UI updates to multiple client devices), we still use a Streams **read-only tail** (`XREAD` from `$`) rather than Pub/Sub, so a reconnecting device can catch up from its last seen ID instead of missing updates. One transport, no second lossy system to reason about.

---

## 9. Performance targets

Design center: **local-first, single primary user, multiple concurrent conversations, eventual multi-client (multi-device, then small-team).**

### 9.1 Latency budget (end-to-end, excluding human think time)

| Hop | Target p50 | Target p99 |
|---|---|---|
| `message.received` → gateway consume | < 5 ms | < 25 ms |
| `draft.requested` → `draft.generated` (Ollama) | 1–4 s (model-bound) | < 10 s |
| `approval.granted` → token mint + `injection.requested` | < 10 ms | < 50 ms |
| `injection.requested` → `injection.completed` (DOM write) | < 80 ms | < 300 ms |
| Stream `XADD` → `XREADGROUP` delivery | < 3 ms | < 15 ms |

The only inherently slow hop is cognition (LLM); every transport hop is single-digit-millisecond because Redis is local (loopback / Tailscale LAN). Token mint/verify (Ed25519) is microseconds.

### 9.2 Local-first
- Redis runs on the user's machine (or a Tailnet-local host). No cloud round-trip on the control path.
- All streams are local; the only WAN hop is between physical client devices over Tailscale, never to a third party.
- SQLite is the initial state of record (single-node, embedded); Postgres is the drop-in for the multi-client phase (Section 10 keeps the schema portable).

### 9.3 Multiple concurrent conversations
- Per-thread serialization (Section 4.2) + cross-thread parallelism: throughput scales with `N` thread-partition workers, bounded by `min(cores, Ollama concurrency)`. Cognition is the bottleneck, not transport.
- Threads are independent log subsequences distinguished by `thread_id` + `seq`; no global lock. A slow conversation (waiting on a human) never blocks another.
- `MAXLEN ~` trimming keeps stream length bounded so `XADD`/`XREADGROUP` stay O(1) amortized under sustained load.

### 9.4 Eventual multi-client support
- Consumer groups already allow N gateway workers to share a stream without double-processing.
- Client devices use read-only stream tails with per-device cursors; adding a device adds a tail, not a new transport.
- The Postgres migration (Section 10) and Redis ACLs (Section 1.5) are the two changes needed to go from single-user to small-team; the stream contracts themselves do not change (same envelope, same types).
- **Scaling guardrail:** even with N clients and N workers, there remains exactly **one** token-minting authority (the gateway role). Horizontal scaling of *workers* never multiplies *authorities* — INV-008 holds regardless of replica count (signing key custody is per-deployment, not per-worker; workers without the key cannot mint).

---

## 10. Contract-first repo structure

Schemas are the source of truth and are validated in CI before any consumer runs. This wires into SAFETY-INVARIANT-001 §8's repo layout and the threat-model mitigation for T7 (schema-validate every stream message).

### 10.1 Layout

```
linkedin-copilot/
├── docs/
│   ├── safety-invariant.md
│   └── redis-stream-contracts.md          # THIS DOCUMENT
│
├── contracts/                             # SINGLE SOURCE OF TRUTH for the event backbone
│   ├── event-types.yaml                   # closed enum of §2.2 types (CI-asserted complete)
│   ├── envelope.schema.json               # the §3.0 common envelope (JSON Schema 2020-12)
│   ├── events/                            # one schema per type, $ref the envelope
│   │   ├── message.received.v1.schema.json
│   │   ├── thread.updated.v1.schema.json
│   │   ├── memory.retrieve.requested.v1.schema.json
│   │   ├── draft.requested.v1.schema.json
│   │   ├── draft.generated.v1.schema.json
│   │   ├── approval.requested.v1.schema.json
│   │   ├── approval.granted.v1.schema.json
│   │   ├── approval.rejected.v1.schema.json
│   │   ├── injection.requested.v1.schema.json
│   │   ├── injection.completed.v1.schema.json
│   │   ├── human.send.observed.v1.schema.json
│   │   ├── state.persisted.v1.schema.json
│   │   ├── retry.requested.v1.schema.json
│   │   └── dead_lettered.v1.schema.json
│   ├── proto/                             # OPTIONAL future: protobuf mirror for cross-lang codegen
│   │   └── events.proto                   # kept byte-compatible with JSON Schema via a parity test
│   ├── streams.yaml                       # §1.2 topology: stream→owner/producer/consumer/retention/ACL
│   ├── retry-policy.yaml                  # §7 per-stream max_attempts, backoff, classification
│   ├── migrations/                        # version-bump mappings (§2.3 expand/contract)
│   │   └── draft.generated.v1_to_v2.json
│   └── codegen/                           # generated, never hand-edited
│       ├── go/                            # gateway structs + validators
│       ├── python/                        # ai-brain pydantic models
│       └── ts/                            # extension + pwa types
│
├── services/gateway/      # consumes contracts/codegen/go; validates on every XREADGROUP
├── services/ai-brain/     # consumes contracts/codegen/python
├── extension/             # consumes contracts/codegen/ts
├── pwa/                   # consumes contracts/codegen/ts
└── ci/
    └── contract-gates.yml # the gates below
```

### 10.2 Validation
- **JSON Schema (2020-12)** is the canonical schema language; every event schema `$ref`s `envelope.schema.json` and adds its `data` constraints.
- **Boundary validation is mandatory:** every consumer validates against the versioned schema **before** acting (T7). A validation failure → DLQ (`schema_invalid`), never a silent best-effort parse.
- **Producers validate on emit too** (cheap, catches bugs at the source rather than the DLQ).
- **Codegen, not hand-written models:** Go structs, Python pydantic, TS types are generated from `contracts/events/*` so the three languages cannot drift. A drift between codegen output and committed code fails CI.

### 10.3 Compatibility strategy (CI gates)

| Gate | Asserts |
|---|---|
| `CI-CONTRACT-ENUM` | `contracts/event-types.yaml` exactly equals the set of `events/*.schema.json` and §2.2; no orphan, no missing. |
| `CI-CONTRACT-ENVELOPE` | Every event schema `$ref`s the common envelope; all envelope-required fields present. |
| `CI-CONTRACT-CODEGEN` | `contracts/codegen/*` is byte-identical to a fresh generation (no hand edits, no drift). |
| `CI-CONTRACT-COMPAT` | A schema change vs the previous git tag is classified additive-vs-breaking; a breaking change without an `event_version` bump fails. |
| `CI-CONTRACT-PROTO-PARITY` | If `proto/` exists, JSON Schema ↔ protobuf field parity holds. |
| `CI-STREAM-TOPOLOGY` | `streams.yaml` producer/consumer/ACL matches the §1 catalogue; no stream lacks an owner/DLQ wiring. |
| `CI-CONTRACT-SAFETY-XREF` | Events that drive state transitions (`approval.granted`, `injection.*`, `human.send.observed`) reference the SAFETY-INVARIANT-001 invariant they uphold; a removed reference fails. |

**Expand/contract in practice:**
1. Add `v2` schema alongside `v1` (never edit `v1` in place).
2. Consumers deploy first, advertising support for both `v1` and `v2` (`state:consumer_caps`).
3. Producers flip to `v2` only after all consumers advertise it.
4. After the migration window, `v1` is frozen (kept for forensic replay via `migrations/`), and producers stop emitting it. Historical `v1` entries are never rewritten.

---

## Appendix A — Event → stream → transition → invariant matrix

| Event | Stream | Drives transition | Upholds invariant(s) |
|---|---|---|---|
| `message.received` | `message_ingest` | — → RECEIVED | INV-018, INV-020 |
| `thread.updated` | `message_ingest` | INJECTED → PENDING_APPROVAL (on edit) | INV-017, INV-021 |
| `memory.retrieve.requested` | `cognition_request` | (none — audit) | INV-020 (provenance) |
| `draft.requested` | `cognition_request` | RECEIVED → DRAFTING (downstream) | INV-002, INV-010 |
| `draft.generated` | `cognition_result` | DRAFTING → PENDING_APPROVAL | INV-002, INV-006 |
| `approval.requested` | `approval` | (none — solicits human) | INV-019 |
| `approval.granted` | `approval` | PENDING_APPROVAL → APPROVED | INV-008, INV-014, INV-016, INV-017 |
| `approval.rejected` | `approval` | PENDING_APPROVAL → REJECTED | INV-019, INV-020 |
| `injection.requested` | `injection` | APPROVED → READY_FOR_INJECTION | INV-001, INV-016 |
| `injection.completed` | `injection` | READY_FOR_INJECTION → INJECTED | INV-013 |
| `human.send.observed` | `observed_send` | INJECTED → SENT_BY_HUMAN | INV-013, INV-021 |
| `state.persisted` | `persistence` | (none — read-model feed) | INV-020, INV-022 |
| `retry.requested` | `retry` | (none — re-delivery) | INV-018 (idempotent) |
| `dead_lettered` | `dead_letter` | (none — quarantine) | INV-020 |

## Appendix B — Change control

- Sections 1, 3, and 4 are **frozen**: changes require an ADR in `docs/adr/` and a same-PR update to `contracts/`.
- No event may be added that grants a software component a send or self-approval capability; such a proposal violates SAFETY-INVARIANT-001 G2/G5/G9 and is rejected at architecture review.
- The event-type registry (§2.2) and `contracts/event-types.yaml` MUST agree (CI-CONTRACT-ENUM).
- Any event that can drive a state transition MUST cross-reference the upholding invariant (CI-CONTRACT-SAFETY-XREF); weakening or removing that reference is a build-breaking offense.
