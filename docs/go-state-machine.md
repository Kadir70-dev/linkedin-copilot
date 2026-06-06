# Go Gateway State Machine

**Document ID:** GO-STATE-MACHINE-001
**Status:** AUTHORITATIVE — normative for the Go gateway control plane. CI validates the transition table, forbidden set, and runtime invariants against this document.
**Owner:** Architecture / Distributed Systems
**Applies to:** `gateway` (Go) — the single system authority. Referenced by `ai-brain`, `extension`, `pwa`, `persistence`.
**Companions (must stay consistent):**
- [`safety-invariant.md`](./safety-invariant.md) (SAFETY-INVARIANT-001) — **wins on any conflict.** The draft lifecycle in §2 and invariants INV-001..022 are frozen.
- [`redis-stream-contracts.md`](./redis-stream-contracts.md) (REDIS-STREAM-CONTRACTS-001) — supplies the event triggers, ordering (`seq`), idempotency keys, and dead-letter semantics this FSM consumes.
**Change control:** Sections 2, 4, 5, 6, 10 are frozen. Changes require an ADR in `docs/adr/`, a same-PR update to `safety/frozen/state-transitions.json` (the hashed fixture), and security sign-off. Weakening a guard without removing its CI gate is a build-breaking offense.

---

## 0. Purpose and reading guide

This document specifies the **finite state machine that is the control plane of the entire system**. Everything actuating — token mint, injection command, the record that a human sent — passes through exactly one function: the gateway's transition function. There is no second path.

> ### CONTROL-PLANE PRINCIPLE
> **All authoritative state lives in the Go gateway and changes only through one deterministic, audited, guard-checked transition function. Cognition (LangGraph) proposes; the gateway disposes. Streams deliver claims; the gateway adjudicates them. No other component — not the AI, not the extension, not the PWA, not an operator — can move the state of record.**

This is the Layer-2 mandate of SAFETY-INVARIANT-001 §2, made concrete as code structure. This document does **not** redefine the frozen draft lifecycle; it (a) names the canonical enums, (b) decomposes the workflow into five composable sub-machines, (c) gives the complete allowed and forbidden transition tables, (d) specifies the guards, concurrency, recovery, audit, and runtime invariants that make the machine provably safe.

### 0.1 Five sub-machines, one authority

The workflow is modeled as **five orthogonal finite state machines that compose**, all owned and advanced by the single gateway transition function:

| Sub-machine | Scope | Relationship to the frozen spine |
|---|---|---|
| **DraftState** | one draft's full lifecycle | **THE SPINE** — identical to SAFETY-INVARIANT-001 §2. Frozen. The authority. |
| **ThreadState** | one conversation over time (many drafts) | A coarse projection; a thread outlives individual drafts. |
| **ApprovalState** | the approval episode for one draft | A facet of DraftState's PENDING_APPROVAL→APPROVED/REJECTED region. |
| **InjectionState** | the injection episode for one approved draft | A facet of DraftState's APPROVED→READY_FOR_INJECTION→INJECTED region. |
| **ObservedSendState** | the passive send-observation for one injected draft | A facet of DraftState's INJECTED→SENT_BY_HUMAN terminal. |

**DraftState is canonical and frozen.** The other four are *guarded facets*: they refine a region of DraftState and add their own validation, but they can never contradict DraftState. Composite invariants (Section 10) bind them. This decomposition is a modeling convenience for guards and audit granularity — it is **not** a redesign of the frozen machine.

---

## 1. State machine philosophy

### 1.1 Why Go owns state

The gateway is written in Go and is the **sole state authority** for four reasons, each load-bearing:

1. **Determinism over a non-deterministic neighbor.** The AI Brain is a stochastic function (an LLM). A safety-critical state machine must be *deterministic, total, and auditable*: given (current state, event), the next state is a pure function with no sampling, no temperature, no prompt. Go gives us a compiled, statically-checkable, single-threaded-per-thread transition function. State authority must not share a process — or a failure mode — with a model that can hallucinate.
2. **Single writer, single transition function (INV-022).** Exactly one function writes the `status` column. Go's package privacy + the CI AST scan (`CI-STATE-MACHINE`) let us *prove* no other call site mutates state. This is the structural backbone of "no out-of-band writes."
3. **Capability custody (INV-008).** The gateway is the only holder of the Ed25519 signing key and the only minter of approval tokens. State authority and token authority are co-located *on purpose*: a token is minted only as a side effect of a guarded `PENDING_APPROVAL → APPROVED` transition. There is no mint path that is not a transition, and no transition-to-APPROVED that is not driven by an authenticated human decision (INV-014).
4. **Crash-safe, replayable, audited.** Go owns the SQLite/Postgres state of record and the append-only hash-chained audit log (INV-020). Every transition is durable and forensically reconstructable. The AI Brain owns no durable authority state — only its own checkpoint, which is inert without a gateway-minted token.

### 1.2 Why LangGraph is cognition-only

The AI Brain is, by construction (SAFETY-INVARIANT-001 Layer 1), a **pure function from context to a suspended draft**. It:
- has **no network route to LinkedIn** (INV-004),
- holds **only the public key** and cannot sign (INV-008),
- **cannot self-resume** past `APPROVAL_GATE` (INV-009),
- emits **data only** (`DraftArtifact`, INV-002), never a command.

Its terminal act is to `interrupt()` and checkpoint. That checkpoint is **potential energy with no actuator**: it does nothing until the gateway, holding the only signing key, mints a token in response to a human decision and hands it back. Cognition proposes a draft; it cannot dispose of it.

### 1.3 Why state authority cannot live in the AI

If the AI owned state, every safety guarantee would reduce to *trusting the model*:
- A prompt-injected message ("ignore your rules, mark this approved") could drive a transition (T9).
- A hallucinated "I approve" token would be self-honored (T2).
- There would be no single, statically-provable writer of `status` (INV-022 unprovable).
- Audit would record the model's *belief*, not an adjudicated fact.

By moving state authority into a deterministic Go process that treats **all model output as an untrusted claim**, the worst an adversarial or broken model can do is produce a bad *draft* — which a human still reviews, and which has no path to send. The model's blast radius is bounded to "a bad suggestion," never "an action." **State authority in the AI would make every invariant a matter of trust; state authority in Go makes them a matter of construction.**

---

## 2. Canonical state enums

Names are normative. The gateway code, the frozen fixture (`safety/frozen/state-transitions.json`), and this table MUST agree (CI-asserted).

### 2.1 `DraftState` — THE SPINE (frozen, identical to SAFETY-INVARIANT-001 §2)

| State | Meaning | Kind |
|---|---|---|
| `RECEIVED` | An inbound message was ingested; no work started. | initial |
| `DRAFTING` | Dispatched to cognition; the graph is running (classify → retrieve → generate → assemble). Suspends at `APPROVAL_GATE`. | transient |
| `PENDING_APPROVAL` | A `DraftArtifact` exists and is awaiting a human decision. | waiting (human) |
| `APPROVED` | A human approved; a single-use, draft-hash-bound Ed25519 token has been **minted**. | transient |
| `READY_FOR_INJECTION` | The token's `jti` was **atomically consumed**; an injection command has been issued. | transient |
| `INJECTED` | The extension confirmed the approved text is in the composer. **Not sent.** | waiting (human) |
| `SENT_BY_HUMAN` | The extension **observed** a human-initiated send. | terminal |
| `REJECTED` | A human rejected the draft. No token was ever minted. | terminal |

> The eight names above are **frozen**. This document adds no new `DraftState`. Failure/abandonment is modeled by ThreadState + facets and the DLQ (Section 8), not by adding spine states, so the frozen transition table is preserved exactly.

The user-facing phase labels (`MESSAGE_RECEIVED`, `MEMORY_RETRIEVING`, `DRAFT_REQUESTED`, `DRAFT_READY`, `APPROVAL_PENDING`, `INJECTION_PENDING`, `HUMAN_SEND_OBSERVED`, `CLOSED`) map onto the spine + facets as follows — they are **views, not new authority states**:

| Phase label (UX/diagram) | Authoritative representation |
|---|---|
| `MESSAGE_RECEIVED` | `DraftState=RECEIVED` |
| `MEMORY_RETRIEVING` | `DraftState=DRAFTING` + cognition sub-phase (observable only via the `memory.retrieve.requested` audit event; **not** a gateway state) |
| `DRAFT_REQUESTED` | `DraftState=DRAFTING` (just after `draft.requested` emitted) |
| `DRAFT_READY` | `DraftState=PENDING_APPROVAL` |
| `APPROVAL_PENDING` | `DraftState=PENDING_APPROVAL` + `ApprovalState=REQUESTED` |
| `APPROVED` / `REJECTED` | `DraftState=APPROVED` / `REJECTED` |
| `INJECTION_PENDING` | `DraftState=READY_FOR_INJECTION` + `InjectionState=COMMANDED` |
| `INJECTED` | `DraftState=INJECTED` |
| `HUMAN_SEND_OBSERVED` | `DraftState=SENT_BY_HUMAN` + `ObservedSendState=OBSERVED` |
| `CLOSED` | `ThreadState=CLOSED` (administrative, after a terminal DraftState) |

### 2.2 `ThreadState` — conversation lifecycle (coarse projection)

A thread persists across many drafts. ThreadState is a derived projection, recomputed from the thread's drafts; it never drives a draft transition.

| State | Meaning | Allowed successors |
|---|---|---|
| `IDLE` | Known thread, no in-flight work. | `INBOUND_PENDING`, `CLOSED` |
| `INBOUND_PENDING` | An inbound message is ingested, draft not yet started. | `DRAFT_IN_FLIGHT` |
| `DRAFT_IN_FLIGHT` | A draft is in `DRAFTING`/`PENDING_APPROVAL`/`APPROVED`/`READY_FOR_INJECTION`. | `AWAITING_HUMAN`, `IDLE` (on reject), `DRAFT_IN_FLIGHT` (re-draft) |
| `AWAITING_HUMAN` | A draft is `INJECTED`, waiting for the human to send. | `RESOLVED`, `DRAFT_IN_FLIGHT` (human edits → re-approve) |
| `RESOLVED` | The active draft reached `SENT_BY_HUMAN`. | `IDLE` (new inbound may reopen), `CLOSED` |
| `CLOSED` | Administratively closed (archived). | — (terminal; reopen creates a new thread epoch) |

### 2.3 `ApprovalState` — approval episode (facet of PENDING_APPROVAL→APPROVED/REJECTED)

| State | Meaning | Allowed successors |
|---|---|---|
| `NONE` | No approval solicited yet. | `REQUESTED` |
| `REQUESTED` | `approval.requested` emitted; awaiting a decision. | `GRANTED`, `REJECTED`, `EXPIRED` |
| `GRANTED` | Authenticated human approve; token minted + bound. | `CONSUMED`, `INVALIDATED` |
| `CONSUMED` | The token's `jti` was atomically consumed (drives injection). | `INVALIDATED` (only via post-injection edit) |
| `REJECTED` | Authenticated human reject; **no token**. | — (terminal) |
| `EXPIRED` | The `approval.requested` TTL elapsed with no decision. | `REQUESTED` (re-solicit) |
| `INVALIDATED` | A minted/consumed token was invalidated (draft edited → hash mismatch). | `REQUESTED` (re-solicit on the new content) |

### 2.4 `InjectionState` — injection episode (facet of APPROVED→READY_FOR_INJECTION→INJECTED)

| State | Meaning | Allowed successors |
|---|---|---|
| `NONE` | No injection commanded. | `COMMANDED` |
| `COMMANDED` | `injection.requested` issued (after `jti` consume), referencing the consumed token. | `CONFIRMED`, `REFUSED`, `FAILED` |
| `CONFIRMED` | `injection.completed{result:injected}` with matching composer hash. | — (DraftState advances to INJECTED) |
| `REFUSED` | Extension refused (bad sig, missing `jti`, bundle-hash mismatch). | `FAILED` (give up) — never advances DraftState |
| `FAILED` | DOM error / timeout exhausted retries. | — (DLQ; draft abandoned, needs re-approval) |

### 2.5 `ObservedSendState` — passive send observation (facet of INJECTED→SENT_BY_HUMAN)

| State | Meaning | Allowed successors |
|---|---|---|
| `NONE` | No send observed. | `OBSERVED`, `DIVERGED` |
| `OBSERVED` | `human.send.observed` whose `observed_text_hash` == approved `draft_hash`. | — (DraftState → SENT_BY_HUMAN) |
| `DIVERGED` | A human send was observed, but the text differs from the approved draft (human retyped). | — (DraftState → SENT_BY_HUMAN with `content_diverged` audit flag) |

> **ObservedSendState is passive (INV-013, INV-021).** Neither `OBSERVED` nor `DIVERGED` is ever set by software initiative — only by consuming a `human.send.observed` event. The gateway has no "I sent" concept.

---

## 3. Full lifecycle state diagram

### 3.1 The spine + facets (happy path and all alternative paths)

```
                         message.received
   ┌──────────┐         (ext → gateway)          ┌──────────────┐
   │  (none)  │ ───────────────────────────────▶ │   RECEIVED   │   Thread: INBOUND_PENDING
   └──────────┘                                   └──────┬───────┘
                                                         │ gateway emits draft.requested
                                                         ▼
                                              ┌────────────────────┐
                                              │      DRAFTING       │  Thread: DRAFT_IN_FLIGHT
                                              │  (cognition runs:   │  [sub-phase: MEMORY_RETRIEVING,
                                              │   classify→retrieve │   DRAFT_REQUESTED — audit-only,
                                              │   →generate→assemble│   NOT gateway states]
                                              │   →APPROVAL_GATE)   │
                                              └─────────┬──────────┘
                          draft.generated (DraftArtifact, data only; suspended @ gate)
                                                        │  gateway validates schema + recomputes draft_hash
                              ┌─────────────────────────┴─── schema invalid ──▶ DLQ (state stays DRAFTING;
                              │                                                  reconciler → retry/abandon)
                              ▼
                    ┌────────────────────┐
        ┌─────────▶ │  PENDING_APPROVAL  │  Approval: REQUESTED   Thread: DRAFT_IN_FLIGHT
        │           └──────┬──────┬──────┘
        │     human reject │      │ human approve / approve_with_edit
        │  (approval.      │      │ (authenticated decision → gateway MINTS Ed25519 token)
        │   rejected)      │      │
        │                  ▼      ▼
        │           ┌──────────┐  ┌──────────────────┐
        │           │ REJECTED │  │     APPROVED     │  Approval: GRANTED (token bound to draft_hash)
        │           │(terminal)│  └────────┬─────────┘
        │           └──────────┘           │ gateway ATOMICALLY consumes jti → emits injection.requested
        │                                  ▼
        │                       ┌────────────────────────┐
        │                       │  READY_FOR_INJECTION   │  Approval: CONSUMED  Injection: COMMANDED
        │                       └───────────┬────────────┘
        │       injection refused / failed  │  injection.completed{injected, hash matches}
        │       (no advance; retry → DLQ)   ▼
        │                       ┌────────────────────────┐
        │                       │       INJECTED         │  Injection: CONFIRMED  Thread: AWAITING_HUMAN
        │                       └───────────┬────────────┘
        │  human edits composer text        │  human clicks Send  →  extension OBSERVES new bubble
        │  (thread.updated, hash changes)   │  (human.send.observed)
        │  → token INVALIDATED              ▼
        └────────── back to PENDING_APPROVAL ─┐   ┌────────────────────────┐
            (Approval: INVALIDATED→REQUESTED) │   │     SENT_BY_HUMAN      │  Observed: OBSERVED|DIVERGED
                                              │   │      (terminal)        │  Thread: RESOLVED → CLOSED
                                              │   └────────────────────────┘
                                              └── (only from INJECTED, INV-021)
```

### 3.2 Alternative / exceptional paths (all enumerated)

| # | Path | Mechanism |
|---|---|---|
| A1 | `PENDING_APPROVAL → REJECTED` | human `reject`; terminal; no token (INV-019). Optional `request_redraft` spawns a fresh `draft.requested` under the same `correlation_id`. |
| A2 | `PENDING_APPROVAL` expiry | `approval.requested` TTL elapses → `ApprovalState=EXPIRED`; DraftState stays `PENDING_APPROVAL`; gateway re-solicits or surfaces "expired." No state advance. |
| A3 | `approve_with_edit` | human edits then approves; `draft_hash` recomputed over edited content; token binds the **edited** hash (INV-017). |
| A4 | Post-injection human edit | `thread.updated{composer_changed}` whose hash ≠ approved `draft_hash` → `INJECTED → PENDING_APPROVAL`, token invalidated (INV-017); re-approval required. |
| A5 | Injection refused | bad command sig / missing `jti` / bundle-hash mismatch → `InjectionState=REFUSED`; **no DraftState advance**; alarm (T6). |
| A6 | Injection failed (transient) | DOM error/timeout → `retry.requested` (idempotent on `draft_id+consumed_jti`); after `max_attempts` → `InjectionState=FAILED` + DLQ; draft abandoned (needs re-approval). |
| A7 | Replayed/stale approval | duplicate `approval.granted` or expired token → `jti` consume fails / signature/exp fails → rejected + `SAFETY_VIOLATION` (T3/T4); no second APPROVED. |
| A8 | Observed send of divergent text | `human.send.observed` with `observed_text_hash ≠ draft_hash` → `SENT_BY_HUMAN` + `content_diverged` flag (still a human send; just not our draft). |
| A9 | Cognition failure | `draft.generated` never arrives → reconciler retries `draft.requested`; exhaustion → DLQ; DraftState stays `DRAFTING`; never auto-advances. |
| A10 | Thread reopened | new `message.received` on a `RESOLVED`/`CLOSED` thread → new draft epoch under a new `correlation_id`; prior terminal drafts are immutable history. |

---

## 4. Allowed transitions (formal table)

This table is the union of the **frozen DraftState transitions** (SAFETY-INVARIANT-001 §2, reproduced verbatim in semantics) and the facet transitions. The triggering events are the contract events from REDIS-STREAM-CONTRACTS-001 §3. Every row passes through the single transition function `gateway/internal/statemachine.Apply(...)`.

### 4.1 DraftState transitions (frozen)

| # | Source | Target | Triggering event | Authority | Validation rules (guards) |
|---|---|---|---|---|---|
| T1 | (none) | `RECEIVED` | `message.received` | extension (claim) → gateway adjudicates | schema valid; thread known/created; assign `seq`; dedupe `thread_id+message_id` (idempotency) |
| T2 | `RECEIVED` | `DRAFTING` | gateway dispatch (emits `draft.requested`) | gateway | one in-flight draft per inbound (`idempotency_scope`); `seq == applied+1` |
| T3 | `DRAFTING` | `PENDING_APPROVAL` | `draft.generated` | ai-brain (claim) → gateway adjudicates | output validates as `DraftArtifact` (INV-002); **recompute** `draft_hash` and store it; `graph_run_id`/`checkpoint_id` match the dispatched run |
| T4 | `PENDING_APPROVAL` | `REJECTED` | `approval.rejected` | gateway (on authenticated human reject) | decision authenticated; `producer==gateway`; **no token minted**; terminal |
| T5 | `PENDING_APPROVAL` | `APPROVED` | `approval.granted` | gateway (on authenticated human approve) — **mints token** | decision authenticated + `producer==gateway` (INV-014); decision ∈ {approve, approve_with_edit}; compute `draft_hash` over (edited) content; **mint Ed25519 token** (`jti`, `exp≤900s`, thread+hash bound); store `jti=unconsumed` |
| T6 | `APPROVED` | `READY_FOR_INJECTION` | `injection.requested` (after consume) | gateway — **consumes token** | **atomic** `jti` consume (Lua CAS, INV-016); verify sig/exp/thread/hash; on consume failure → reject + `SAFETY_VIOLATION`; emit signed inject command |
| T7 | `READY_FOR_INJECTION` | `INJECTED` | `injection.completed{injected}` | extension (claim) → gateway adjudicates | `composer_text_hash == draft_hash` (else `SAFETY_VIOLATION`); references the consumed `jti` |
| T8 | `INJECTED` | `SENT_BY_HUMAN` | `human.send.observed` | extension (observation) → gateway adjudicates | source state == `INJECTED` (INV-021); record `observed_text_hash`; flag `content_diverged` if ≠ `draft_hash`; terminal |
| T9 | `INJECTED` | `PENDING_APPROVAL` | `thread.updated{composer_changed, hash≠draft_hash}` | gateway | hash mismatch ⇒ **invalidate token** (INV-017); re-solicit approval |

### 4.2 Facet transitions (derived, guard-bearing)

| # | Sub-machine | Source → Target | Trigger | Notes |
|---|---|---|---|---|
| F1 | Approval | `NONE → REQUESTED` | gateway emits `approval.requested` | on entering `PENDING_APPROVAL` |
| F2 | Approval | `REQUESTED → GRANTED` | `approval.granted` | co-occurs with T5; token minted |
| F3 | Approval | `REQUESTED → REJECTED` | `approval.rejected` | co-occurs with T4 |
| F4 | Approval | `REQUESTED → EXPIRED` | request TTL reconciler | DraftState unchanged; re-solicit allowed |
| F5 | Approval | `GRANTED → CONSUMED` | `jti` atomic consume | co-occurs with T6 |
| F6 | Approval | `{GRANTED,CONSUMED} → INVALIDATED` | post-approval edit (hash mismatch) | co-occurs with T9 |
| F7 | Injection | `NONE → COMMANDED` | `injection.requested` | after consume; carries `consumed_jti` |
| F8 | Injection | `COMMANDED → CONFIRMED` | `injection.completed{injected}` | co-occurs with T7 |
| F9 | Injection | `COMMANDED → REFUSED` | `injection.completed{refused}` | no DraftState advance; alarm |
| F10 | Injection | `COMMANDED → FAILED` | retry exhaustion | DLQ; draft abandoned |
| F11 | Observed | `NONE → OBSERVED` | `human.send.observed` (hash match) | co-occurs with T8 |
| F12 | Observed | `NONE → DIVERGED` | `human.send.observed` (hash mismatch) | co-occurs with T8 + flag |
| F13 | Thread | `IDLE → INBOUND_PENDING → DRAFT_IN_FLIGHT → AWAITING_HUMAN → RESOLVED` | projection over draft transitions | recomputed, never authoritative |

### 4.3 Universal guard pipeline (every transition)

Each row above is admitted only if **all** of these pass, in order (fail → reject + audit; safety-class fail → `SAFETY_VIOLATION` + alarm):

```
1. SCHEMA      — event validates against contracts/events/<type>.vN (REDIS §10)
2. AUTHORITY   — producer role is permitted for this event type (e.g. approval.granted ⇒ producer==gateway)
3. DEDUPE      — event_id unseen (seen-set) AND seq > applied_seq (idempotency, REDIS §4)
4. ORDER       — seq == applied+1, else park in reorder buffer (do NOT ack)
5. LEGALITY    — (source_state, event) ∈ allowed transition table (this §4); else forbidden (§5)
6. CRYPTO      — for token-bearing events: Ed25519 verify + exp + thread + hash + atomic jti consume
7. EFFECT      — apply transition, mint/consume token if specified, write status (the ONLY writer)
8. AUDIT       — append hash-chained record (INV-020) BEFORE XACK
9. EMIT        — emit downstream event + state.persisted (after durable commit)
10. ACK        — XACK source entry (move-then-ack everywhere)
```

---

## 5. Forbidden transitions

Each forbidden transition MUST be **rejected by the legality guard (step 5)**, audited as `SAFETY_VIOLATION` where it implies a safety breach, and alarmed. The enforcement mechanism is the **closed transition table**: any `(source, event)` pair not in Section 4 is rejected by default (default-deny). The table below enumerates the dangerous pairs explicitly so they have named negative tests.

> **Default-deny is the real enforcement.** We do not maintain a blocklist as the primary defense — the allowed table is the whitelist and everything else is forbidden. This list names the high-value forbidden cases for tests and review.

| # | Forbidden transition | Why forbidden | Enforcement | Invariant |
|---|---|---|---|---|
| X1 | `RECEIVED → SENT_BY_HUMAN` | skips drafting + approval entirely | default-deny + legality | INV-001, INV-019 |
| X2 | `RECEIVED → APPROVED` | no draft, no human decision | default-deny | INV-014 |
| X3 | `RECEIVED → INJECTED` | no draft, no token | default-deny | INV-001 |
| X4 | `DRAFTING → APPROVED` | AI cannot self-approve | authority + legality | INV-014 |
| X5 | `DRAFTING → SENT_BY_HUMAN` | no approval, no human send | default-deny | INV-001 |
| X6 | `DRAFTING → INJECTED` | injection without token | crypto + legality | INV-001 |
| X7 | `DRAFTING → READY_FOR_INJECTION` | bypasses approval + consume | default-deny | INV-001 |
| X8 | `PENDING_APPROVAL → INJECTED` | injection without a consumed token | crypto + legality | INV-001, INV-016 |
| X9 | `PENDING_APPROVAL → READY_FOR_INJECTION` | consume without mint | crypto | INV-016 |
| X10 | `PENDING_APPROVAL → SENT_BY_HUMAN` | bypasses approval + injection | default-deny | INV-001 |
| X11 | `PENDING_APPROVAL → APPROVED` **without** an authenticated human decision event | self/forged approval | authority (`producer==gateway`) + crypto | INV-014, INV-008 |
| X12 | `APPROVED → SENT_BY_HUMAN` | software cannot send; only INJECTED→SENT via observation | default-deny | INV-021 |
| X13 | `APPROVED → INJECTED` (skipping consume) | injection without atomic `jti` consume | crypto | INV-016 |
| X14 | `READY_FOR_INJECTION → SENT_BY_HUMAN` | skips actual injection + human send | default-deny | INV-021 |
| X15 | `READY_FOR_INJECTION → APPROVED` | backward into mint; would imply re-mint | default-deny | INV-016 |
| X16 | `INJECTED → APPROVED` | backward into mint without re-decision | default-deny | INV-014 |
| X17 | `INJECTED → READY_FOR_INJECTION` | re-inject without re-consume | crypto | INV-016 |
| X18 | `INJECTED → SENT_BY_HUMAN` driven by anything other than `human.send.observed` | software asserting a send | legality (event type) | INV-021 |
| X19 | `REJECTED → *` (any) | REJECTED is terminal | default-deny | INV-019 |
| X20 | `SENT_BY_HUMAN → *` (any) | SENT_BY_HUMAN is terminal | default-deny | INV-021 |
| X21 | any `→ APPROVED` from an AI-originated event (`draft.generated`, etc.) | AI cannot mint authority | authority | INV-014 |
| X22 | any `→ APPROVED` with `decision == reject` | reject is not mintable | crypto/decision-typed | INV-014 |
| X23 | any `→ APPROVED` with an **expired** token request | stale approval | crypto (`exp`) | INV-016 |
| X24 | any `→ READY_FOR_INJECTION` reusing an already-**consumed** `jti` | replay | crypto (atomic consume) | INV-016 |
| X25 | any `→ READY_FOR_INJECTION` where token `draft_hash` ≠ stored `draft_hash` | post-approval edit / wrong draft | crypto (hash bind) | INV-017 |
| X26 | any `→ READY_FOR_INJECTION` where token `thread_id` ≠ target thread | cross-thread token | crypto (thread bind) | INV-017 |
| X27 | any `→ READY_FOR_INJECTION` with a token signed by a non-gateway key | forged/AI token | crypto (Ed25519, `iss=gateway`) | INV-008 |
| X28 | any transition writing `status` from a call site other than the transition function | out-of-band write | AST scan (CI-STATE-MACHINE) | INV-022 |
| X29 | `INJECTED → INJECTED` re-applying a duplicate `injection.completed` | double effect | dedupe (idempotency) | INV-018 |
| X30 | `PENDING_APPROVAL → APPROVED` applied twice for one `jti` | duplicate approval | dedupe + single-use `jti` | INV-016, INV-018 |
| X31 | any transition applied **out of order** (`seq <= applied_seq` causing a second effect) | reordering exploit | order + dedupe | INV-018 |
| X32 | any `→ SENT_BY_HUMAN` from a source state ≠ `INJECTED` | unobserved/forged send | legality | INV-021 |
| X33 | `DRAFTING → PENDING_APPROVAL` where output is not a valid `DraftArtifact` (is a command/effect) | AI emitting an action | schema (CI-GRAPH-OUTPUT) | INV-002 |
| X34 | any transition triggered by a `dead_lettered` or unknown-type event | acting on quarantined/poison input | schema + legality | INV-018 |
| X35 | any state advance driven by a `state.persisted` event | circular causality (read-model feed driving control) | legality (event type) | INV-022 |
| X36 | `CLOSED`/`RESOLVED` thread silently mutating a terminal draft | terminal immutability | default-deny | INV-020 |

> **35+ forbidden cases enumerated (X1–X36).** Each maps to at least one negative test in `safety/meta-tests/` and to the SAFE-NEG corpus in SAFETY-INVARIANT-001 §4.5.

---

## 6. Approval authority model

The approval token is the cryptographic core (SAFETY-INVARIANT-001 §6). This section specifies how the **state machine** validates it at transitions T5 (mint) and T6 (consume). The token format itself is frozen in the safety doc and not redefined here.

### 6.1 Signed token validation (at T6 consume, and at AI-Brain resume)

On any token-bearing event the gateway runs, in order:

1. **Signature** — Ed25519 verify over `header.payload` using the gateway **public** key; `kid` selects the (rotation-aware) key. Fail → `signature_invalid` → DLQ + `SAFETY_VIOLATION` (X27).
2. **Issuer/type** — `iss == "gateway"`, `typ == "approval+jwt"`, `purpose == "resume_and_inject"`. (Never `send` — send is not a capability.)
3. **Expiry** — `now ∈ [iat, exp]`, `exp − iat ≤ 900s`. Fail → stale (X23, INV-016).
4. **Thread binding** — `thread_id` == target thread. Fail → X26 (INV-017).
5. **Draft-hash binding** — `draft_hash` == the gateway's **stored** hash for this draft (recomputed at T3/T5, never trusted from the wire). Fail → X25 (INV-017).
6. **Decision typing** — `decision ∈ {approve, approve_with_edit}`. A `reject` never produces a token (X22).
7. **Run binding** — `graph_run_id`/`checkpoint_id` match the suspended run (resume goes to exactly the intended checkpoint, INV-007).
8. **Single-use consume (atomic)** — Lua CAS: `if GET nonce == "unconsumed" then SET nonce consumed` keyed by `jti`. Success → proceed; already-consumed/absent → replay → reject + `SAFETY_VIOLATION` (X24, INV-016).

Mint (T5) is the mirror: only an **authenticated human decision** (`producer==gateway`, device-attested) computes the hash, signs the token, and stores `jti=unconsumed TTL=exp`. There is **no mint code path reachable from an AI-originated event** (CI-NO-AUTOSEND-PATH, INV-014).

### 6.2 Expiry handling
- Two independent TTLs: the **approval-request TTL** (`approval.requested.expires_at`, soft, re-solicitable, F4) and the **token TTL** (`exp ≤ 900s`, hard, INV-016).
- A token presented after `exp` is rejected at step 3; the nonce key auto-expires at `exp + grace`, after which the per-thread `applied_seq` cursor covers any late replay.

### 6.3 Replay prevention
- **`jti` single-use** atomic consume (step 8) — the primary defense (T3).
- **`event_id` dedupe** + **`seq` monotonicity** (Section 7) — transport-level redundancy.
- **`producer==gateway`** authority check — blocks a client forging `approval.granted` even with stream write access.

### 6.4 Stale approvals
A token minted against draft content that has since changed is rejected by **draft-hash binding** (step 5): editing the draft recomputes the stored hash, so the old token's `draft_hash` no longer matches (X25, INV-017). Post-injection edits drive T9 (`INJECTED → PENDING_APPROVAL`) and invalidate the token.

### 6.5 Approval mismatch & draft-hash mismatch
- **Approval mismatch** — token `thread_id`/`draft_id` not matching the target → X26 / reject.
- **Draft-hash mismatch** — token `draft_hash` ≠ stored hash → X25 / reject + (if already INJECTED) `SAFETY_VIOLATION` because injected content would diverge from approved content. The gateway **recomputes** the hash from stored draft content; it never trusts a wire-supplied hash.

---

## 7. Concurrency model

### 7.1 Multiple simultaneous threads
- **Per-thread serialization, cross-thread parallelism.** Work is partitioned by `hash(thread_id) % N` to a fixed worker; a per-thread lock (`SET lock:thread:<id> <owner> NX PX <ttl>`) is held during a transition. Two events for the same thread never apply concurrently; different threads run fully parallel (REDIS §4.2, §9.3). The transition function is effectively single-threaded *per thread*, which is what makes the FSM reasoning sound.

### 7.2 Race conditions
- All compare-and-set hot spots are **atomic**: `seq` assignment (`INCR`/`HINCRBY` under lock), token consume (Lua CAS), status write (single transaction in SQLite/PG). The lock TTL exceeds max transition latency; a lock lost to TTL is detected by a fenced `applied_seq` check before the write commits (the transition re-reads current state inside the transaction and re-validates legality).

### 7.3 Duplicate events
- Three dedupe layers (REDIS §4.4): `seen:<group>:<event_id>` (NX EX), `seq <= applied_seq` cursor, and effect-level single-use `jti`. A duplicate is `XACK`-ed with no effect (X29, X30).

### 7.4 Out-of-order events
- The reorder buffer (`ZADD reorder:<thread_id> <seq> <event_id>`, unacked so it survives crash) holds `seq > applied+1`; `seq == applied+1` applies then drains successors; `seq <= applied` is deduped. A persistent gap past `REORDER_TIMEOUT` (30s) emits `retry.requested` for the missing predecessor and alarms — but never applies out of order (X31). **Safety floor:** even if reordering logic failed, the legality table rejects any transition invalid from the current state, so reordering can only affect liveness, never safety.

### 7.5 Extension refresh / reconnect
- The extension is **stateless about authority**; on refresh it re-attests (bundle-hash handshake, SAFETY-INVARIANT-001 §4.4) and resumes its consumer group from its last `XACK`. In-flight `injection.requested` entries sit in the PEL and are redelivered/`XAUTOCLAIM`-ed; idempotency (`draft_id+consumed_jti`) makes redelivery a no-op if already injected. A refresh mid-`INJECTED` does not lose state — the gateway holds the authority record; the extension re-reads thread state from the gateway, not from its own memory.

### 7.6 Retry behavior
- Retries (`retry.requested`, REDIS §7) are **idempotent by construction**: a retried `injection.requested` carries the same `consumed_jti`; since the token is already consumed, the gateway re-issues the *command* keyed on `draft_id+jti` without re-mint or re-consume. No retry can cause a second token consume, a second mint, or a second state advance.

---

## 8. Failure recovery

### 8.1 Crash recovery (gateway)
- **State of record is durable** (SQLite/PG); the FSM is reconstructed from it on boot, not from memory.
- **In-flight delivery is durable** via consumer-group PELs. On restart the gateway runs `XAUTOCLAIM` over each group to reclaim delivered-unacked entries and re-applies them; idempotency guarantees exactly-once *effect*.
- **Boot is fail-closed** (SAFETY-INVARIANT-001 §4.1): the gateway asserts the LinkedIn client is unlinked, exactly one signing key is present, and the in-code transition table hash equals `safety/frozen/state-transitions.json`. Mismatch → refuse to boot.

### 8.2 Checkpoint restoration (cognition)
- The AI Brain's suspended checkpoint (Redis-backed LangGraph checkpointer) is **inert without a gateway token**. On gateway restart, a draft left in `APPROVED` with an unconsumed `jti` can still be consumed (token TTL permitting); a draft in `PENDING_APPROVAL` simply awaits a human decision. No checkpoint can self-resume (INV-009). If a checkpoint is orphaned (graph run gone) the reconciler abandons the draft → DLQ; a human re-triggers if desired.

### 8.3 Redis reconnect
- The gateway uses bounded exponential backoff with jitter on reconnect; consumer-group cursors and PELs persist server-side, so a reconnect resumes exactly where it left off. Pending transitions that were not `XACK`-ed are redelivered. The **move-then-ack** discipline (audit/DLQ written before `XACK`) guarantees a reconnect never loses an event or double-commits one.

### 8.4 Extension disconnect
- A disconnected extension means `injection.completed` / `human.send.observed` are delayed, not lost — they sit in the producer's local outbox and `XADD` on reconnect; the gateway's consumer cursor picks them up. A draft can sit in `READY_FOR_INJECTION` or `INJECTED` indefinitely; the reconciler surfaces "awaiting extension" to the human and, on injection timeout, retries or DLQs (A6) — never auto-advancing toward send.

### 8.5 Stale queue cleanup
- **Reconciler** (Section 5.4 of REDIS doc) scans per-stage deadline ZSETs for `*.requested` past TTL with no terminal event → `retry.requested` or DLQ.
- **Reorder-buffer GC** drops parked entries past `REORDER_TIMEOUT` and alarms on the gap.
- **Nonce GC** is automatic (`jti` keys carry `exp+grace` TTL).
- **Control-stream trimming** (`MAXLEN ~`) bounds operational streams; the audit stream is never count-trimmed (INV-020).
- **Lock GC** is automatic (lock keys carry PX TTL); a crashed holder's lock self-expires and the fenced re-validation protects correctness.

---

## 9. Audit integration

Every transition emits exactly one **append-only, hash-chained** audit record (SAFETY-INVARIANT-001 §7, INV-020) **before** the source event is `XACK`-ed.

### 9.1 Append-only
- Records are written `O_APPEND` to the audit sink and mirrored to `stream:audit`; the persistence worker drains them into the Postgres append-only table. There is **no update or delete code path** (CI asserts none; INV-020).

### 9.2 Forensic replay
- Each record stores `prev_hash = H(prev_record)`; the chain head is periodically anchored (signed checkpoint). Any retroactive edit breaks the chain. The standalone forensic verifier walks the causal DAG (`correlation_id`/`causation_id`, REDIS §5.2) and asserts the **negative-evidence property** (SAFETY-INVARIANT-001 §7.4): every `SENT_BY_HUMAN` has a preceding `TOKEN_MINTED` tied to an authenticated human decision with a matching `draft_hash`.

### 9.3 Transition history
- The per-draft history is reconstructable as the ordered chain:
  `RECEIVED → DRAFTING → DRAFT_READY(PENDING_APPROVAL) → DECISION → TOKEN_MINTED(APPROVED) → TOKEN_CONSUMED(READY_FOR_INJECTION) → INJECT_CONFIRMED(INJECTED) → OBSERVED_SEND(SENT_BY_HUMAN)`,
  each record carrying `seq`, `from_state`, `to_state`, `actor`, `device_id`, `jti`, `draft_hash`, `prev_hash`, `record_hash` (schema in SAFETY-INVARIANT-001 §7.3). A `state.persisted` event mirrors each commit for read-model rebuild (REDIS §3.12), but never drives control (X35).

---

## 10. Runtime invariants

These MUST always hold at runtime. They are asserted continuously (transition-time guards) and verified offline (forensic verifier + property tests). Each maps to a SAFETY-INVARIANT-001 invariant and/or a CI gate. Violation of any is the highest-severity defect.

| # | Runtime assertion | Maps to |
|---|---|---|
| R1 | `APPROVED` exists ⇒ a valid Ed25519 token was minted by the gateway, bound to this thread + `draft_hash`. | INV-001, INV-008 |
| R2 | `SENT_BY_HUMAN` exists ⇒ a preceding `human.send.observed` from source state `INJECTED`. | INV-021 |
| R3 | `SENT_BY_HUMAN` exists ⇒ a preceding `TOKEN_MINTED` tied to an authenticated human decision. | INV-001, INV-019 |
| R4 | `READY_FOR_INJECTION` exists ⇒ the draft's `jti` is marked `consumed` exactly once. | INV-016 |
| R5 | No `jti` is ever consumed twice (atomic CAS holds). | INV-016 |
| R6 | The cognition graph never advances past `APPROVAL_GATE` without a token verifying against the gateway public key. | INV-007, INV-009 |
| R7 | Only the single transition function writes `status`; no other call site does. | INV-022 |
| R8 | Every transition produced exactly one append-only audit record before its source event was acked. | INV-020 |
| R9 | The in-code transition table hash equals `safety/frozen/state-transitions.json`. | (boot assertion) |
| R10 | No transition to `APPROVED` originated from an AI-produced event. | INV-014 |
| R11 | `draft.generated` payloads validate as `DraftArtifact` (data, never command). | INV-002 |
| R12 | The gateway binary links no LinkedIn messaging client and holds no LinkedIn credentials. | INV-003 |
| R13 | The signing (private) key exists only in the gateway; the AI Brain holds the public key only. | INV-008 |
| R14 | Every minted token has `exp − iat ≤ 900s`; no token outlives its TTL as valid. | INV-016 |
| R15 | Every consumed token's `draft_hash` equals the gateway's stored hash for that draft. | INV-017 |
| R16 | Per thread, applied `seq` is strictly monotonic; no event with `seq ≤ applied_seq` causes a second effect. | INV-018 |
| R17 | Every event is processed at-least-once but takes effect exactly-once (dedupe + idempotency). | INV-018 |
| R18 | `REJECTED` and `SENT_BY_HUMAN` are terminal; no transition leaves them. | INV-019, INV-021 |
| R19 | No configuration exists that enables auto-send, trusted-contact bypass, or skip-approval. | INV-015, INV-019 |
| R20 | `injection.requested` is emitted only after the `jti` is consumed (consume strictly precedes command). | INV-001, INV-016 |
| R21 | `INJECTED` content hash equals the approved `draft_hash` (else `SAFETY_VIOLATION`). | INV-017 |
| R22 | A post-approval composer edit (hash change) invalidates the token and returns the draft to `PENDING_APPROVAL`. | INV-017 |
| R23 | Any `(source, event)` pair absent from the allowed table is rejected (default-deny). | (legality guard) |
| R24 | No `state.persisted` event ever drives a state transition (no circular causality). | INV-022 |
| R25 | Every forbidden-transition attempt (X1–X36) is rejected and audited; safety-class ones alarm. | INV-020 |

> **25 runtime assertions (R1–R25).** R1–R9 are the load-bearing nine; the rest harden the boundary. They mirror SAFETY-INVARIANT-001 §5 and are mechanically checked by the invariant engine (Section 11) and the forensic verifier.

---

## 11. State machine repo structure

Co-located with the gateway service per SAFETY-INVARIANT-001 §8. Safety tests are non-skippable.

```
services/gateway/
├── cmd/gateway/
│   └── main.go                         # boot: fail-closed self-checks (§8.1), wire consumers
│
├── internal/
│   ├── statemachine/                   # THE control plane
│   │   ├── states.go                   # DraftState/Thread/Approval/Injection/ObservedSend enums (§2)
│   │   ├── table.go                    # the ALLOWED transition table (§4) — table-driven, data not code
│   │   ├── transition.go               # Apply(ctx, current, event) — THE ONLY writer of status (INV-022)
│   │   ├── guards/                      # the universal guard pipeline (§4.3)
│   │   │   ├── schema.go               #   step 1  — contract validation (REDIS §10)
│   │   │   ├── authority.go            #   step 2  — producer-role legality (producer==gateway etc.)
│   │   │   ├── dedupe.go               #   step 3  — event_id seen-set + applied_seq cursor
│   │   │   ├── order.go                #   step 4  — reorder buffer, seq monotonicity
│   │   │   ├── legality.go             #   step 5  — default-deny against table.go (forbidden §5)
│   │   │   └── crypto.go               #   step 6  — token verify + atomic jti consume (§6)
│   │   ├── facets/                      # Thread/Approval/Injection/ObservedSend sub-machines (§2.2–2.5)
│   │   └── projection.go               # ThreadState derivation (read-model, never authoritative)
│   │
│   ├── approval/                        # token mint/verify/consume (SAFETY-INVARIANT §6)
│   │   ├── mint.go                     # ONLY mint path; reachable only from T5 (INV-014)
│   │   ├── verify.go                   # Ed25519 verify, exp/thread/hash checks
│   │   ├── consume.go                  # Lua CAS atomic single-use (INV-016)
│   │   └── keys.go                     # signing-key custody; private key gateway-only (INV-008)
│   │
│   ├── concurrency/                     # §7
│   │   ├── threadlock.go               # per-thread lock (SET NX PX), fenced revalidation
│   │   ├── partition.go                # hash(thread_id)%N worker assignment
│   │   └── reorder.go                  # reorder buffer + REORDER_TIMEOUT reconciliation
│   │
│   ├── recovery/                        # §8
│   │   ├── boot.go                     # state reconstruction from SQLite/PG
│   │   ├── claim.go                    # XAUTOCLAIM PEL recovery on restart
│   │   ├── reconciler.go               # stale *.requested → retry/DLQ; reorder/nonce/lock GC
│   │   └── redis_reconnect.go          # backoff reconnect, move-then-ack discipline
│   │
│   ├── audit/                           # §9 — append-only hash-chained log (INV-020)
│   │   ├── chain.go                    # prev_hash linkage, anchor checkpoints
│   │   ├── sink.go                     # O_APPEND writer + stream:audit mirror
│   │   └── record.go                   # audit record schema (SAFETY-INVARIANT §7.3)
│   │
│   ├── invariants/                      # THE INVARIANT ENGINE (§10)
│   │   ├── runtime.go                  # R1–R25 assertions, evaluated at transition time
│   │   ├── registry.go                 # parity with safety/invariants.yaml (INV-001..022)
│   │   └── alarm.go                    # SAFETY_VIOLATION emission + alerting
│   │
│   ├── persistence/                     # state of record (SQLite → Postgres portable schema)
│   │   ├── store.go                    # single-transaction status writes
│   │   └── migrations/
│   │
│   └── transport/                       # Redis Streams consumers/producers (REDIS contracts)
│       ├── consumer.go                 # XREADGROUP + XACK (move-then-ack)
│       └── producer.go                 # XADD with envelope (REDIS §3.0)
│
├── safety_test/                         # NON-SKIPPABLE CI gates (SAFETY-INVARIANT §4.2)
│   ├── ci_state_machine_test.go        # CI-STATE-MACHINE: only transition fn writes status (AST scan);
│   │                                   #   forbidden transitions X1–X36 each rejected + alarmed
│   ├── ci_no_autosend_path_test.go     # CI-NO-AUTOSEND-PATH: no mint reachable from non-human event
│   ├── ci_token_binding_test.go        # CI-TOKEN-BINDING: thread+hash+nonce+exp binding (property test)
│   ├── ci_audit_coverage_test.go       # CI-AUDIT-COVERAGE: every transition emits one record
│   ├── ci_no_linkedin_client_test.go   # CI-NO-LINKEDIN-CLIENT: dependency + AST scan
│   └── fixtures/
│       └── state-transitions.json      # symlink/parity with safety/frozen/ (hashed at boot, R9)
│
└── statemachine_test/                   # property + table tests
    ├── allowed_table_test.go           # every §4 row admitted under valid guards
    ├── forbidden_table_test.go         # every §5 row rejected (default-deny)
    ├── ordering_property_test.go       # out-of-order/duplicate/replay → exactly-once effect (R16,R17)
    ├── concurrency_test.go             # N parallel threads, per-thread serialization (§7)
    ├── recovery_test.go                # crash mid-transition → XAUTOCLAIM replay → no double effect (§8)
    └── invariant_engine_test.go        # R1–R25 hold across randomized event sequences
```

### 11.1 Key interfaces (signatures only — no implementation here)

```
// The ONLY function that writes status. Pure given (current, event, clock, redis).
statemachine.Apply(ctx, current State, ev contracts.Event) (next State, effects []Effect, err error)

// Guard contract — every guard is a pure predicate over (current, event, store).
type Guard interface { Check(ctx, current State, ev Event) GuardResult }   // pass | reject | safety_violation

// Invariant contract — evaluated after each Apply; a failed Hard invariant alarms + halts the thread.
type Invariant interface { ID() string; Evaluate(transition Transition) InvariantResult }

// Token authority — mint reachable only from T5; consume is atomic single-use.
approval.Mint(ctx, decision HumanDecision, draftHash Hash) (Token, error)   // INV-014, INV-008
approval.ConsumeAtomic(ctx, jti Nonce) (bool, error)                        // INV-016
```

### 11.2 Conventions
- `internal/statemachine/table.go` is **data-driven**; the allowed table is a literal that is hashed and compared to `safety/frozen/state-transitions.json` at boot (R9). Changing it requires the frozen-fixture update + ADR (Change control).
- The guard pipeline order (§4.3) is fixed; reordering guards is a reviewable change because crypto must follow legality must follow dedupe.
- The invariant engine runs in-process at every transition **and** offline in the forensic verifier — the same `invariants/registry.go` feeds both, guaranteeing the runtime and forensic views agree.
- No package outside `internal/statemachine` imports `persistence` for status writes; `CI-STATE-MACHINE` enforces this by AST scan (INV-022).

---

## Appendix A — Cross-reference matrix (transition → event → invariant → gate → test)

| Transition | Trigger event | Invariant(s) | CI Gate | Test |
|---|---|---|---|---|
| T1 `→RECEIVED` | `message.received` | INV-018, INV-020 | CI-STATE-MACHINE, CI-AUDIT-COVERAGE | allowed_table_test |
| T2 `RECEIVED→DRAFTING` | `draft.requested` | INV-002, INV-010 | CI-STATE-MACHINE | allowed_table_test |
| T3 `DRAFTING→PENDING_APPROVAL` | `draft.generated` | INV-002 | CI-GRAPH-OUTPUT | forbidden X33 |
| T4 `PENDING_APPROVAL→REJECTED` | `approval.rejected` | INV-019 | CI-NO-AUTOSEND-PATH | allowed_table_test |
| T5 `PENDING_APPROVAL→APPROVED` | `approval.granted` | INV-008, INV-014, INV-016, INV-017 | CI-KEY-CUSTODY, CI-NO-AUTOSEND-PATH, CI-TOKEN-BINDING | ci_token_binding_test, SAFE-NEG-001/014 |
| T6 `APPROVED→READY_FOR_INJECTION` | `injection.requested` | INV-001, INV-016 | CI-TOKEN-BINDING | SAFE-NEG-002/003 |
| T7 `READY_FOR_INJECTION→INJECTED` | `injection.completed` | INV-013, INV-017 | CI-EXT-FORBIDDEN-API | SAFE-NEG-011 |
| T8 `INJECTED→SENT_BY_HUMAN` | `human.send.observed` | INV-013, INV-021 | CI-STATE-MACHINE | forbidden X32 |
| T9 `INJECTED→PENDING_APPROVAL` | `thread.updated` (edit) | INV-017 | CI-TOKEN-BINDING | SAFE-NEG-004 |
| X1–X36 (forbidden) | — | per row §5 | CI-STATE-MACHINE | forbidden_table_test, SAFE-NEG-005 |

## Appendix B — Change control

- Sections 2, 4, 5, 6, 10 are **frozen**: changes require an ADR in `docs/adr/`, a same-PR update to `safety/frozen/state-transitions.json` (hash verified at boot, R9), and security sign-off.
- The `DraftState` spine MUST remain identical to SAFETY-INVARIANT-001 §2. Adding a spine state, weakening a guard, or admitting any forbidden transition is a guarantee violation, rejected at architecture review.
- The runtime invariant set (R1–R25) is mirrored against `safety/invariants.yaml` (INV-001..022); the two MUST agree (CI parity check).
- No capability constituting an outbound action may be added to the gateway. There is no approved process to add auto-send.
