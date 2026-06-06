# Event Contracts — Explanation

**Document ID:** EVENT-CONTRACTS-001
**Status:** AUTHORITATIVE companion to the machine-readable event authority.
**Owner:** Architecture / Distributed Systems
**Describes:** [`contracts/event-types.yaml`](../contracts/event-types.yaml) — the single source of truth for every event flowing across the system, consumed by Redis Streams, the Go Gateway, the LangGraph AI Brain, the Browser Extension, the PWA, the audit system, the runtime invariant engine, and CI parity checks.
**Companions (must stay consistent; they win on conflict, in this order):**
1. [`redis-stream-contracts.md`](./redis-stream-contracts.md) (REDIS-STREAM-CONTRACTS-001) — authoritative for payloads/streams.
2. [`safety/frozen/state-transitions.json`](../safety/frozen/state-transitions.json) — transition bindings.
3. [`safety/invariants.yaml`](../safety/invariants.yaml) — invariant text.
4. [`go-state-machine.md`](./go-state-machine.md) — transitions T1–T9.
**Change control:** Frozen. Any edit requires an ADR in `docs/adr/`, a recomputed digest, a `contract_version` bump, and security sign-off — all in one PR.

---

## 0. What this contract is

`contracts/event-types.yaml` is the **machine-readable event authority**. The prose in `redis-stream-contracts.md` *describes* the event backbone for humans; this file *encodes* it so that every producer, consumer, validator, and CI gate derives from one definition instead of many drifting copies.

> ### EVENT-AUTHORITY PRINCIPLE
> Every byte that moves between two components in this system is one of a **closed, frozen set of typed events**. There are no ad-hoc messages, no untyped RPCs, no "just this once" side channels. If it isn't in this registry, it isn't on the wire — a consumer that sees an unknown `type` dead-letters it rather than guessing. The registry is the complete vocabulary of the system, and the vocabulary is fixed.

This is the fifth file in the derivation chain, each strictly projecting the ones above and never redesigning them:

```
safety-invariant.md  →  go-state-machine.md  →  state-transitions.json
                                                        │
                                          redis-stream-contracts.md
                                                        │ projects to
                                          event-types.yaml  ◄── THIS  (+ invariants.yaml refs)
```

`CI-EVENT-PARITY` enforces that the 14 stream events here equal the Redis doc §2.2, the fixture's `events.registry`, and the JSON Schema files in `contracts/events/` — and that every `drives_transition` resolves to a real FSM transition.

---

## 1. Event philosophy

### 1.1 Events are claims, not commands
The deepest design choice (inherited from the Redis contracts' Transport Principle) is that **an event is a claim that something happened, not an instruction to do something**. `approval.granted` does not *command* an approval; it *reports* that the gateway, having authenticated a human decision, minted a token. The consumer still verifies everything. This is what makes it safe to run a security-critical workflow over an at-least-once, partially-untrusted transport: the dangerous decisions live in the gateway's adjudication, never in message delivery.

### 1.2 Naming encodes intent
`<noun>.<past-verb>[.<qualifier>]` — past tense for facts (`received`, `generated`, `granted`, `observed`), `requested` for intents. There is, by construction, **no `*.send.requested`** — send is not a software capability (SAFETY-INVARIANT-001 G2/G3). The only `send`-noun event is `human.send.observed`, a passive observation. The grammar makes "the AI asked to send" literally unsayable in the vocabulary.

### 1.3 Fourteen on the wire, one in process
There are exactly **14 stream events** — the frozen parity set. `memory.retrieve.completed` appears in your canonical list but, per `redis-stream-contracts.md` §3.3, is **deliberately not a stream event**: memory retrieval is a synchronous, read-only node tool whose result returns inside the LangGraph process. To honor the list without contradicting the frozen decision, it is modeled in `in_process_events` (transport: `in_process`, no stream, no producer ACL) and is explicitly **excluded from the 14-event parity set**. This is reconciliation, not redesign.

### 1.4 Fail closed
An event from a non-permitted producer, with an invalid schema, an unknown type, or a failed signature is **never acted on** — it is rejected or dead-lettered, and safety-class failures alarm. There is no "best-effort parse" path.

---

## 2. Schema rationale (`schema`, `envelope`, per-event fields)

Each event carries **17 fields** (`schema.per_event_fields`). The design separates the **shared envelope** (`event_id`, `type`, `event_version`, `timestamp`, `correlation_id`, `causation_id`, `thread_id`, `producer`, `producer_instance`, `data`, plus conditional `message_id`/`seq`) from the **per-event `data.*`** fields, so the envelope is defined once and each event lists only its payload shape. This mirrors `redis-stream-contracts.md` §3.0 exactly.

The non-obvious fields and why they exist:

| Field | Why |
|---|---|
| `event_uid` (EVT-0NN) | a stable registry id, never reused even after retirement, so audit records and tests can reference an event independent of its wire name. |
| `authority` | classifies the event as a *claim* or *authority-bearing*; the three authority-bearing events (`approval.granted`, `approval.rejected`, `injection.requested`) carry the `producer==gateway` obligation. |
| `idempotency_requirements` | the key that makes at-least-once delivery exactly-once in *effect* (distinct from `event_id`, which dedupes *redeliveries*). |
| `invariant_dependencies` | binds the event to invariants in `safety/invariants.yaml`; `CI-INVARIANT-XREF` asserts each reference resolves. |
| `drives_transition` | binds the event to FSM transitions; `CI-EVENT-PARITY` asserts each resolves to a real allowed transition. |
| `audit_requirements` | names the audit record the event must produce (RECEIVED, TOKEN_MINTED, OBSERVED_SEND, …), tying the event stream to the forensic chain. |

**Versioning** has two axes: `schema_version` (how to parse the file) and per-event `event_version` (the payload schema of one type). Evolution follows **expand/contract** (§ below). **Deprecation** is three-phase (`active → deprecated → retired`) and an event type is **never hard-deleted** — retirement preserves forensic replayability, and `event_uid` is never reused.

---

## 3. Authority model (`authority_model`)

This is the security spine of the event layer. The rule is short and absolute:

> **The three authority-bearing events — `approval.granted`, `approval.rejected`, `injection.requested` — are physically `XADD`-ed by the gateway only.** A client (PWA/extension) submits a human decision to the gateway's authenticated, device-attested endpoint; the gateway authenticates it, mints (or does not mint) the Ed25519 token, and emits the event. No client stream-write can manufacture authority.

Explicit, ACL-enforced denials:
- **AI Brain** cannot emit any authority-changing event. It emits `draft.generated` (data only) and `memory.retrieve.requested` (audit-only). It holds only the public key (INV-008).
- **Extension** cannot emit `approval.*` (no approval authority). It emits claims: `message.received`, `thread.updated`, `injection.completed`, `human.send.observed`.
- **PWA** never `XADD`s directly; it submits decisions to the gateway endpoint.
- **Only the gateway** emits `injection.requested`, because it requires a consumed `jti` + a signed command.

Enforcement is by **Redis ACL** (`redis-stream-contracts.md` §1.5), not convention, and cross-checked by `CI-FORBIDDEN-PRODUCER` and `CI-UNAUTHORIZED-EMISSION`. An event from a non-permitted producer is a `SAFETY_VIOLATION` and is dead-lettered. This realizes INV-014 ("APPROVED only from an authenticated human decision") at the transport layer: even a client with stream-write access cannot forge an approval, because the `producer==gateway` check rejects it.

---

## 4. Validation rules (`validation_rules`, per-event `validation_rules`)

Global rules every event obeys:
- **Schema-first:** validate against `contracts/events/<type>.v<version>.schema.json` *before* any action; invalid ⇒ `dead_letter(schema_invalid)`. Producers validate on emit too.
- **`correlation_id`:** minted by the root (`message.received`), copied verbatim by every descendant. A workflow is the set of events sharing it.
- **`causation_id`:** the `event_id` of the direct cause; absent only on the root. Together they form the causal DAG the forensic verifier walks.
- **`seq`:** per-thread monotonic; `seq == applied+1` applies, `> applied+1` parks in the reorder buffer (unacked), `<= applied` is a deduped duplicate (INV-018).
- **Replay hooks:** `event_id` seen-set, `applied_seq` cursor, and single-use `jti` — three layers, so a duplicate is absorbed even if one layer misses.
- **Hash recomputation:** any `*_hash` a guard depends on is recomputed by the gateway from stored/observed content; the wire value is a cross-check only (INV-017).

Per-event rules capture the specifics — e.g., `approval.granted` requires `producer==gateway`, `decision ∈ {approve, approve_with_edit}`, and an Ed25519 token; `injection.completed` requires `composer_text_hash == draft_hash` or it is a `SAFETY_VIOLATION`.

---

## 5. Security model (`security_model`, per-event `security_constraints`)

Each event declares its trust boundary, signature requirement, and spoof/replay/stale resistance:

- **Trust boundaries:** extension→gateway and ai_brain→gateway carry *untrusted claims*; `body_text` from the DOM is the prompt-injection boundary (untrusted data, T9). pwa→gateway is *authenticated human decisions*. gateway-internal is the trust anchor.
- **Signatures:** `approval.granted.data.approval_token` (Ed25519 JWS, gateway-signed) and `injection.requested.data.inject_command_sig` (gateway signature over the inject command) are the two cryptographic gates. Both are *verified* by consumers against the gateway public key.
- **Spoof resistance:** producer ACL + `producer==gateway` + Ed25519 signatures make a forged or AI-minted token/command fail verification (INV-008).
- **Replay resistance:** single-use `jti`, `event_id` dedupe, per-thread `seq` (INV-016, INV-018).
- **Stale handling:** token `exp ≤ 900s`; `approval.requested.expires_at`; `draft_hash` binding rejects approvals against a since-edited draft (INV-016, INV-017).

---

## 6. CI parity (`ci_parity`)

Seven required, non-skippable checks consume this file:

| Check | Asserts |
|---|---|
| `CI-EVENT-PARITY` | the 14 stream events == Redis §2.2 == fixture `events.registry` == `contracts/events/*.schema.json`; every `drives_transition` resolves both ways |
| `CI-CONTRACT-ENUM` | the closed event-name set == the JSON Schema files |
| `CI-SCHEMA-DRIFT` | each event's fields match its schema file; codegen (Go/Python/TS) regenerates identically |
| `CI-FORBIDDEN-PRODUCER` | no service is wired to `XADD` an event its role can't produce (vs Redis ACLs) |
| `CI-UNAUTHORIZED-EMISSION` | no path emits an authority-bearing event from a non-gateway service or non-human trigger (vs `CI-NO-AUTOSEND-PATH`) |
| `CI-FIXTURE-INTEGRITY` | this file's digest == its sidecar |
| `CI-INVARIANT-XREF` | every `invariant_dependency` and `audit_requirement` resolves |

Any drift fails the build. Remediation language is uniform: reconcile to the authority or remove the capability — never silence the check.

---

## 7. Event ownership

| Producer | Emits | Nature |
|---|---|---|
| **extension** | message.received, thread.updated, injection.completed, human.send.observed | claims + passive observation |
| **gateway** | draft.requested, approval.requested, **approval.granted**, **approval.rejected**, **injection.requested**, state.persisted, retry.requested, dead_lettered | dispatch + **authority-bearing** + internal |
| **ai_brain** | memory.retrieve.requested (audit-only), draft.generated (data only) | cognition, no authority |
| **pwa** | (none directly — decisions via the gateway endpoint) | human decision origin |

Consumption (`runtime_consumption_model.consumers_index`) maps each event to its consumer group: ingest/cognition/approval/injection/observe/persistence/retry/operator, all mirrored to `stream:audit` for the append-only log. Delivery is at-least-once with move-then-ack; retries are idempotent and never re-mint or re-consume; safety-class failures go straight to the DLQ and are never auto-recovered.

---

## 8. Evolution strategy (expand/contract + deprecation)

**Additive change** (new optional field, new ignorable enum value): no `event_version` bump; consumers are tolerant readers. **Breaking change** (remove/rename a field, change a type, make optional required): bump `event_version`; producers emit the new version only after every consumer advertises support (`state:consumer_caps` handshake); consumers dual-read old+new for one migration window. Historical entries keep their original `event_version`; the forensic verifier maps versions via `contracts/migrations/`.

**Deprecation** is three-phase and never destructive:
- `active` → the current version is emitted.
- `deprecated` → a successor exists; the old version is still *accepted* (dual-read) but no longer *emitted*; marked `deprecated_in` + `successor`.
- `retired` → no longer emitted/accepted on live streams; retained only for forensic replay; requires an ADR.

An event type is **never hard-deleted** and `event_uid` is **never reused** — both preserve audit replayability.

---

## 9. Safe change process

A deliberately heavyweight, fail-closed process:

1. **Update the prose authority first** — `redis-stream-contracts.md` (payload/stream) and, if a transition is involved, `go-state-machine.md` + the fixture. The registry follows; it never leads.
2. **Open an ADR** explaining the change, its producer/authority, and its invariant impact.
3. **Add/modify the JSON Schema** in `contracts/events/` and regenerate codegen (Go/Python/TS) in the same PR (`CI-SCHEMA-DRIFT`).
4. **Update this registry** with all 17 fields; `invariant_dependencies` and `drives_transition` must reference existing ids (or add them upstream in the same PR).
5. **Wire the producer ACL** and confirm `CI-FORBIDDEN-PRODUCER` / `CI-UNAUTHORIZED-EMISSION` pass — an authority-bearing event must be gateway-only.
6. **Bump `contract_version`**, recompute the digest, update the sidecar **and** §10 here.
7. **Security sign-off**.

To add an **authority-bearing** event, the bar is highest: it must be gateway-emitted, signature-bearing where it actuates, and provably unreachable from a non-human trigger. There is no approved process to add an event that constitutes an outbound action; such a proposal is rejected at architecture review.

---

## 10. Integrity

> **Pinned digest (`contract_version` 1):**
> `15e8511d25d78547727c1dce51ab2a088158034dc7803ae4714e1283700dd316`

Verify with `sha256sum contracts/event-types.yaml` vs `contracts/event-types.sha256` (gate `CI-FIXTURE-INTEGRITY`). Each consumer recomputes this at boot and refuses to start on mismatch (fail-closed).

---

## Appendix A — Registry self-checks (verified at authoring time)

| Property | Result |
|---|---|
| Stream events | 14 (parity with fixture + Redis §2.2) |
| Event UIDs | EVT-001..014 contiguous, plus EVT-INP-001 (in-process) |
| All 17 per-event fields present on every stream event | yes (no missing fields) |
| `invariant_dependencies` resolve against `safety/invariants.yaml` | yes |
| `drives_transition` resolve against the frozen fixture | yes |
| `producer` matches the fixture `events.registry` | yes (exact) |
| `memory.retrieve.completed` modeled in-process, excluded from parity set | yes |
| YAML validity | valid (single document) |
| Pinned digest matches sidecar | `15e851…0dd316` |

## Appendix B — Change control

- Frozen. Edits require an ADR, a recomputed digest (sidecar + §10 here), a `contract_version` bump, and security sign-off — all in one PR.
- This registry is mirrored against `redis-stream-contracts.md` (payloads/streams), `safety/frozen/state-transitions.json` (transition bindings), and `safety/invariants.yaml` (invariant text). The set MUST agree; `CI-EVENT-PARITY` + `CI-INVARIANT-XREF` enforce it.
- Adding an outbound-action event, allowing a non-gateway producer of an authority-bearing event, or admitting an unknown type into control flow is a guarantee violation, rejected at architecture review.
