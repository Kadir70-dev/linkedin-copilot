# State Transition Fixture — Explanation

**Document ID:** STATE-TRANSITION-FIXTURE-001
**Status:** AUTHORITATIVE companion to the machine-readable fixture.
**Owner:** Architecture / Distributed Systems
**Describes:** [`safety/frozen/state-transitions.json`](../safety/frozen/state-transitions.json) — the single source of truth from which the Go FSM, LangGraph orchestration, Redis stream contracts, runtime invariant engine, and CI safety gates all derive.
**Companions (must stay consistent; they win on conflict, in this order):**
1. [`safety-invariant.md`](./safety-invariant.md) (SAFETY-INVARIANT-001)
2. [`go-state-machine.md`](./go-state-machine.md) (GO-STATE-MACHINE-001)
3. [`redis-stream-contracts.md`](./redis-stream-contracts.md) (REDIS-STREAM-CONTRACTS-001)
**Change control:** The fixture is frozen. Any edit requires an ADR in `docs/adr/`, a recomputed integrity digest (Section 1.3), security sign-off, and a `fixture_version` bump — all in one PR.

---

## 0. What this fixture is, and why it exists

Three prose documents define the system's safety model, event backbone, and control-plane FSM. Prose is unambiguous to a human reader but **unenforceable by a machine**. The fixture is the *executable contract*: a single JSON file that encodes every canonical state, every allowed and forbidden transition, the guard pipeline, the runtime assertions, and the invariant mappings, so that **all six consumers derive from one authority instead of six hand-maintained copies that drift**.

> ### SINGLE-SOURCE-OF-TRUTH PRINCIPLE
> There is exactly one transition authority in this system, and it is `safety/frozen/state-transitions.json`. The Go gateway does not *define* the transition table — it *loads and hashes* it. CI does not *re-describe* the forbidden set — it *iterates* it. If the fixture and the code disagree, the build fails; if the fixture and the prose disagree, the fixture is wrong and is corrected to match the (authoritative) prose. The fixture is the projection where prose meets enforcement.

This eliminates the classic failure mode of safety systems: the documentation says one thing, the code does another, and the tests assert a third. Here, all three are the same bytes.

---

## 1. Schema design (fixture Section `schema`, `version`, `integrity`, `migration`)

### 1.1 Top-level structure
The fixture is a single JSON object with these top-level keys, in dependency order:

| Key | Purpose | Maps to requirement |
|---|---|---|
| `schema` | schema version, derived authority, conflict resolution | §1 schema versioning |
| `version` | monotonic `fixture_version`, frozen flag, change control | §1 version strategy |
| `integrity` | digest algorithm, scope, sidecar, boot assertion | §1 checksum strategy |
| `migration` | expand/contract rules, compatibility | §1 migration compatibility |
| `states` | the five canonical state machines | §2 |
| `authorities` | who may originate each event | §3 authority |
| `events` | closed event registry (parity with Redis contracts) | §3 |
| `guard_pipeline` | ordered, deterministic guard catalogue | §5 |
| `allowed_transitions` | T1–T9, fully specified | §3 |
| `facet_transitions` | F1–F13 derived sub-machine moves | §3 |
| `forbidden_transitions` | X1–X36 | §4 |
| `runtime_assertions` | R1–R25 | §6 |
| `invariants` | INV-001..022 reference projection | §4/§6 mapping |
| `ci_integration` | how each gate consumes the fixture | §7 |

### 1.2 Schema versioning
Two independent version axes, deliberately separated:
- **`schema.schema_version`** (semver, currently `1.0.0`) versions the *structure* of the fixture — the field names and shapes a parser must understand. A consumer checks this first and refuses to parse a MAJOR it doesn't support.
- **`version.fixture_version`** (monotonic integer, currently `1`) versions the *content* — the actual states and transitions. The Go gateway asserts at boot that the `fixture_version` it was compiled against equals the one it loads.

Separating "how to read it" from "what it says" means a clarification (PATCH) never forces a content re-validation, and a content change never silently rides on an old structure.

### 1.3 Checksum / version strategy (the self-reference problem, solved)
A naive design stores the file's own hash inside the file — which is impossible, because writing the hash changes the bytes the hash is computed over. We avoid the paradox by **not storing the digest inside the JSON**:

- The digest is **SHA-256 over the entire byte content** of `state-transitions.json` as committed (LF newlines, terminal newline).
- It is stored in the **sidecar** `safety/frozen/state-transitions.sha256` and pinned here:

> **Pinned digest (`fixture_version` 1):**
> `72ec8c32e503ed9e395c036dbf45548c9945dda2ba974970dde1b34ed3fda09a`

- Verification is trivial and deterministic: `sha256sum safety/frozen/state-transitions.json` must equal the sidecar (gate `CI-FIXTURE-INTEGRITY`).
- The Go gateway embeds the table at build time and asserts its computed table hash equals this digest at boot (runtime assertion **R9**); a mismatch is **fail-closed** — the gateway refuses to start. This is how a tampered or drifted table is caught before it can ever process an event.

This is the lockfile pattern (`package-lock.json`, `go.sum`): the artifact is pure data; integrity lives beside it, not within it.

### 1.4 Migration compatibility
The fixture follows the same **expand/contract** discipline as the event schemas (Redis contracts §2.3):
- *Additive* (new optional field, a new state with no allowed transitions yet, a new derived facet) → MINOR schema bump, `fixture_version++`, no consumer break.
- *Breaking* (rename/remove a state, change a transition's guards/authority, alter a forbidden entry) → MAJOR schema bump, `fixture_version++`, all consumers dual-read old+new for one migration window before the old is retired.
- Historical audit records carry the `fixture_version` they were produced under; the forensic verifier maps across versions via ADR migration notes. **No prior frozen fixture is ever rewritten in place** — a new version supersedes it and the old is kept for forensic replay.

---

## 2. Canonical states (fixture `states`)

The five machines from GO-STATE-MACHINE-001 §2 are frozen verbatim. **`DraftState` is the spine and the only authoritative machine**; the other four are guard-bearing or derived facets that can never contradict it.

| Machine | Values | Terminals | Authority |
|---|---|---|---|
| `DraftState` | RECEIVED, DRAFTING, PENDING_APPROVAL, APPROVED, READY_FOR_INJECTION, INJECTED, SENT_BY_HUMAN, REJECTED | SENT_BY_HUMAN, REJECTED | **sole writer** = `statemachine.Apply` |
| `ThreadState` | IDLE, INBOUND_PENDING, DRAFT_IN_FLIGHT, AWAITING_HUMAN, RESOLVED, CLOSED | CLOSED | derived projection (never drives a draft transition — X35) |
| `ApprovalState` | NONE, REQUESTED, GRANTED, CONSUMED, REJECTED, EXPIRED, INVALIDATED | REJECTED | facet of PENDING_APPROVAL→APPROVED/REJECTED |
| `InjectionState` | NONE, COMMANDED, CONFIRMED, REFUSED, FAILED | FAILED | facet of APPROVED→READY_FOR_INJECTION→INJECTED |
| `ObservedSendState` | NONE, OBSERVED, DIVERGED | OBSERVED, DIVERGED | facet of INJECTED→SENT_BY_HUMAN (passive, INV-021) |

Each state value carries a `kind` (initial / transient / waiting_human / terminal / error / recoverable) so consumers can reason about it generically (e.g., "alarm if a draft sits in a `transient` state past T").

---

## 3. Allowed transitions (fixture `allowed_transitions`)

Nine spine transitions (**T1–T9**), each fully specified with the ten fields the requirement names: `transition_id`, `source_state`, `target_state`, `triggering_event`, `authority`, `required_guards`, `required_runtime_assertions`, `invariant_dependencies`, `retry_policy`, `failure_behavior` — plus `facet_effects` and `side_effects` for composition.

| ID | Transition | Event | Authority | Token role |
|---|---|---|---|---|
| T1 | →RECEIVED | message.received | extension claim | — |
| T2 | RECEIVED→DRAFTING | draft.requested | gateway | — |
| T3 | DRAFTING→PENDING_APPROVAL | draft.generated | ai_brain claim | — |
| T4 | PENDING_APPROVAL→REJECTED | approval.rejected | gateway (human reject) | — |
| **T5** | **PENDING_APPROVAL→APPROVED** | **approval.granted** | **gateway (human approve)** | **mint** |
| T6 | APPROVED→READY_FOR_INJECTION | injection.requested | gateway | **consume** |
| T7 | READY_FOR_INJECTION→INJECTED | injection.completed | extension claim | — |
| T8 | INJECTED→SENT_BY_HUMAN | human.send.observed | extension observation | — |
| T9 | INJECTED→PENDING_APPROVAL | thread.updated (edit) | gateway | invalidate |

The facet moves (F1–F13) are listed under `facet_transitions` for completeness; they are advanced by the same transition function and never independently of the spine.

**Reading guarantee:** `triggering_event`, `authority`, and `events.registry[].drives_transition` are mutually consistent — `CI-EVENT-PARITY` asserts every `drives_transition` reference resolves to a real allowed transition and vice versa.

---

## 4. Forbidden transitions (fixture `forbidden_transitions`)

All 36 (**X1–X36**) from GO-STATE-MACHINE-001 §5, each with `reason`, `enforcement_layer`, `violated_invariant`, and `test_mapping`.

The **primary enforcement is default-deny**: the `legality_in_table` guard rejects any `(source, event)` pair not in `allowed_transitions`. The forbidden list is therefore not the mechanism — it is the **named, tested catalogue** of the dangerous cases, so each has an explicit negative test and an invariant citation. Categories:

- **Skip-the-pipeline** (X1, X3, X5, X7, X10, X14) — jumping past drafting/approval/injection.
- **Self-approval / forged authority** (X2, X4, X11, X21, X22, X27) — anything reaching APPROVED without an authenticated human decision or with a non-gateway-signed token.
- **Token replay / staleness / binding** (X8, X9, X13, X17, X23, X24, X25, X26) — consume without mint, reuse, expiry, hash/thread mismatch.
- **Send fabrication** (X12, X18, X32) — any path to SENT_BY_HUMAN not from INJECTED via `human.send.observed`.
- **Terminal immutability** (X19, X20, X36).
- **Ordering / duplication** (X29, X30, X31) — second effects from duplicate or out-of-order events.
- **Out-of-band & circular control** (X28 out-of-band status write, X35 `state.persisted`-driven transition, X33 non-DraftArtifact output, X34 acting on poison/unknown events).

Each row's `enforcement_layer` names exactly which guard (or AST scan) catches it, so a reviewer can trace forbidden → guard → test without guessing.

---

## 5. Guard pipeline (fixture `guard_pipeline`)

A single, **deterministic, ordered** catalogue of 19 guards (order index 1–19), grouped into stages: `SCHEMA → AUTHORITY → DEDUPE → ORDER → LEGALITY → CRYPTO → EFFECT → AUDIT → EMIT → ACK`. Every transition executes guards in **ascending order index**; `CI-GUARD-ORDER` asserts the running pipeline matches.

The order is itself a safety property and is frozen:
- **Dedupe/order before legality** — a replayed or out-of-order event is dropped before it's even checked against the table.
- **Legality before crypto** — we never do expensive Ed25519 work on a transition that isn't allowed from the current state.
- **Crypto before effect** — no state write, mint, or consume happens until signature, expiry, thread, hash, and atomic single-use all pass.
- **Audit before ack** (move-then-ack) — a transition is forensically recorded before its source event is acknowledged, so a crash mid-transition redelivers rather than losing the record.

The named granular guards the requirement asks for map directly: `actor_authorized`, `replay_check`, `sequence_valid`, `token_signature_valid`, `token_not_expired`, `thread_match`, `draft_hash_match`, `jti_consume_atomic`. Each guard declares `applies_when` (`always` / `thread_scoped` / `token_bearing` / `mint` / `consume`) so token guards run only on T5/T6, and a `failure_class` (`reject` / `safety_violation` / `dead_letter` / `park` / `retry`) that determines whether a failure merely rejects or also alarms.

---

## 6. Runtime assertions (fixture `runtime_assertions`)

All 25 (**R1–R25**) from GO-STATE-MACHINE-001 §10, each with `statement`, `maps_to` (invariants), `evaluation` (where it's checked), and `severity`. The `evaluation` field is what lets the **same assertion list drive both the in-process invariant engine and the offline forensic verifier**:

- `boot` — checked once at startup (R9 table hash, R12/R13 capability/key custody).
- `transition_time` — evaluated on every `Apply` (most R's).
- `offline_forensic` — re-verified by the standalone audit verifier over the log chain (R3, plus the `+offline` ones).
- `ci_ast_scan` / `ci_secret_scan` / `property_test` — proven statically or by generative tests (R7, R17, R19, R23, R25).

All 25 are `severity: hard` — a violation is the highest-severity defect and, at transition time, halts the affected thread and emits a `SAFETY_VIOLATION`.

---

## 7. CI integration (fixture `ci_integration`)

The fixture's `ci_integration.consumers` enumerates exactly how each tier reads it. Summary of the consumption contract:

| Tier | Gate | What it does with the fixture |
|---|---|---|
| Integrity | `CI-FIXTURE-INTEGRITY` | recompute SHA-256, compare to sidecar |
| Boot (R9) | gateway startup | embed table, assert hash == digest, fail-closed on mismatch |
| Schema/table | `CI-STATE-MACHINE` | every allowed row admitted; every X1–X36 rejected; AST scan for the single status writer (INV-022/X28) |
| Authority | `CI-NO-AUTOSEND-PATH` | call-graph: no mint reachable from a non-human event; T5 carries `decision_typed` + `human_decision_authenticated` |
| Token | `CI-TOKEN-BINDING` | property tests over the crypto guards |
| Audit | `CI-AUDIT-COVERAGE` | every transition lists `audit_append` before `xack_source` |
| Invariants | `CI-INVARIANT-PARITY` | this list agrees with `safety/invariants.yaml`; every R maps to a real INV |
| Guard order | `CI-GUARD-ORDER` | running pipeline executes guards in fixture order |
| Events | `CI-EVENT-PARITY` | parity with `contracts/event-types.yaml`; `drives_transition` resolves |
| Capability | `forbidden_capability_detection` | static scan for out-of-band writes, `state.persisted`-driven transitions, non-gateway authority producers |

Every gate is a **required, non-skippable** status check; a skipped safety test fails the build (consistent with SAFETY-INVARIANT-001 §4).

---

## 8. Example resolved transition — `PENDING_APPROVAL → APPROVED` (T5)

This is the most security-critical transition in the system: it is where a human decision becomes a minted Ed25519 token. Here is the **fully resolved trace** of one real T5, using the fixture's T5 row, the guard pipeline, and the runtime assertions. (No implementation code — this is the adjudication narrative.)

### 8.1 Preconditions
- Draft `drf_789` on thread `li_thread_abc123` is in `DraftState=PENDING_APPROVAL`, `ApprovalState=REQUESTED`.
- The gateway previously stored, at T3, the canonical `draft_hash = sha256:9f2c...` for `drf_789`.
- A human, on the PWA (`device_id=phone-pwa`), reviewed the draft, edited it slightly, and tapped **Approve**.

### 8.2 The event
The PWA submits the decision to the gateway's authenticated, device-attested decision endpoint (not a raw stream write). The gateway authenticates it, recomputes the hash over the **edited** content (`sha256:7be1...`), mints an Ed25519 token, and `XADD`s the authority-bearing event to `stream:approval`:

```
type:            approval.granted
event_id:        01J8Z0G...                 (idempotency key)
producer:        gateway                    (REQUIRED — not the client)
thread_id:       li_thread_abc123
seq:             46                          (per-thread, == applied_seq + 1)
correlation_id:  cor_01J8Z0A...
causation_id:    01J8Z0F...                  (the approval.requested it answers)
data.decision:           approve_with_edit
data.approved_draft_hash: sha256:7be1...     (hash of EDITED content)
data.approval_token:     eyJhbGciOiJFZERTQS... (Ed25519 JWS; jti, exp = iat+300s)
data.approver_id:        user_primary
data.device_id:          phone-pwa
data.graph_run_id:       run_456
data.checkpoint_id:      ckpt_456_gate
```

### 8.3 Guard pipeline execution (fixture `required_guards` for T5, in order)

| Order | Guard | Check on this event | Result |
|---|---|---|---|
| 1 | `schema_valid` | validates against `approval.granted.v1` | PASS |
| 2 | `actor_authorized` | `producer == gateway`? yes | PASS (a client-forged grant would be `SAFETY_VIOLATION` here — X11) |
| 3 | `replay_check` | `event_id 01J8Z0G...` unseen? yes | PASS |
| 4 | `sequence_valid` | `seq 46 > applied_seq 45`? yes | PASS |
| 5 | `order_in_sequence` | `seq 46 == applied 45 + 1`? yes | PASS (apply now) |
| 6 | `legality_in_table` | `(PENDING_APPROVAL, approval.granted) ∈ allowed`? yes → T5 | PASS |
| 12 | `decision_typed` | `decision ∈ {approve, approve_with_edit}`? yes | PASS (a `reject` here is X22) |
| 13 | `human_decision_authenticated` | decision came from an authenticated, device-attested human action? yes | PASS (no AI-originated mint — INV-014, X21) |
| 11 | `draft_hash_match` | `approved_draft_hash` == hash gateway recomputes over the edited content it is about to bind? yes | PASS |
| 16 | `effect_apply` | write `DraftState = APPROVED` (sole writer) | PASS |
| 17 | `audit_append` | append `TOKEN_MINTED` record before ack | PASS |
| 18 | `emit_downstream` | emit `state.persisted`; ready to drive T6 | PASS |
| 19 | `xack_source` | XACK the `approval.granted` entry | PASS |

(Guards 7–10, 14, 15 — full signature/expiry/thread/run/consume — are not in T5's set because T5 *mints*; they run at **T6 consume**, where the token is verified and the `jti` is atomically consumed. T5's crypto duty is to mint correctly and bind the hash.)

### 8.4 Mint side effects (fixture T5 `side_effects`)
- Compute `draft_hash` over the edited approved content → `sha256:7be1...`.
- Mint Ed25519 token: `jti=01J...ULID`, `iss=gateway`, `exp = iat + 300s (≤ 900s)`, bound to `thread_id=li_thread_abc123` and `draft_hash=sha256:7be1...`, `purpose=resume_and_inject`.
- Store `jti=unconsumed` in Redis with `TTL=exp`.
- Facet: `ApprovalState REQUESTED → GRANTED`.

### 8.5 Runtime assertions fired (fixture T5 `required_runtime_assertions`)

| Assertion | Asserted fact at this transition |
|---|---|
| R1 | APPROVED now exists ⇒ a valid gateway-minted token bound to this thread + draft_hash. ✔ |
| R3 | The eventual SENT_BY_HUMAN will have this TOKEN_MINTED tied to an authenticated human decision. ✔ (recorded) |
| R7 | Only `statemachine.Apply` wrote `status`. ✔ |
| R8 | Exactly one audit record (`TOKEN_MINTED`) written before ack. ✔ |
| R10 | This transition did **not** originate from an AI event. ✔ |
| R13 | The token was signed by the gateway's private key (AI Brain holds public only). ✔ |
| R14 | `exp − iat = 300s ≤ 900s`. ✔ |
| R15 | The bound `draft_hash` equals the gateway's stored hash for `drf_789`. ✔ |
| R19 | No auto-send/bypass involved; a human approved. ✔ |

### 8.6 Audit emission (append-only hash chain, INV-020)
One record is appended **before** the source event is acked:

```
{
  "seq": 10432,
  "ts": "2026-06-03T10:15:40.230Z",
  "thread_id": "li_thread_abc123",
  "draft_id": "drf_789",
  "event": "TOKEN_MINTED",
  "from_state": "PENDING_APPROVAL",
  "to_state": "APPROVED",
  "actor": "user_primary",
  "device_id": "phone-pwa",
  "jti": "01J...ULID",
  "draft_hash": "sha256:7be1...",
  "prev_hash": "sha256:1a7b...",
  "record_hash": "sha256:c4d9..."
}
```

The forensic verifier later walks `correlation_id=cor_01J8Z0A...` and confirms the negative-evidence property (SAFETY-INVARIANT-001 §7.4): this `TOKEN_MINTED` precedes the eventual `OBSERVED_SEND`, ties to an authenticated human decision, and matches the `draft_hash` that the injected-and-sent content carried.

### 8.7 What happens next
`DraftState=APPROVED` with `ApprovalState=GRANTED` and an unconsumed `jti`. The gateway proceeds to **T6**: it atomically consumes the `jti` (the full crypto guard set runs there — X23/X24/X25/X26/X27 are the forbidden cases it defends against), emits the gateway-signed `injection.requested`, and advances to `READY_FOR_INJECTION`. The token can be consumed exactly once; a replayed `approval.granted` (same `jti`) is stopped at `replay_check` (3) and, even if that missed, at `jti_consume_atomic` (15) in T6 — defense in depth.

---

## Appendix A — Fixture invariants (what the file itself must satisfy)

These are meta-properties of the fixture, checked by `CI-FIXTURE-INTEGRITY` and the parity gates:

1. `allowed_transitions` has exactly 9 entries (T1–T9); `forbidden_transitions` has exactly 36 (X1–X36); `runtime_assertions` has exactly 25 (R1–R25). *(verified: 9 / 36 / 25)*
2. Every `triggering_event` and every `events.registry[].type` is in the closed registry that matches `contracts/event-types.yaml` (14 types).
3. Every `required_runtime_assertion` references a real `R*`; every `invariant_dependency` references a real `INV-0xx`.
4. Every `forbidden_transitions[].enforcement_layer` names a real guard id or a real AST scan.
5. The pinned digest in §1.3 equals `sha256sum safety/frozen/state-transitions.json` and the sidecar.
6. The `DraftState` value set is identical to SAFETY-INVARIANT-001 §2 (no added or removed spine state).

## Appendix B — Change control

- The fixture is frozen. Edits require an ADR in `docs/adr/`, a recomputed digest in the sidecar **and** §1.3 here, a `fixture_version` bump, and security sign-off — all in one PR.
- The `DraftState` spine and the forbidden set are change-controlled to the same degree as SAFETY-INVARIANT-001 §1/§5/§6.
- Adding any transition that constitutes an outbound action, weakening any guard, or admitting any forbidden transition is a guarantee violation, rejected at architecture review. There is no approved process to add auto-send.
