# Invariants Registry — Explanation

**Document ID:** INVARIANTS-REGISTRY-001
**Status:** AUTHORITATIVE companion to the machine-readable registry.
**Owner:** Architecture / Security
**Describes:** [`safety/invariants.yaml`](../safety/invariants.yaml) — the machine-readable invariant authority that powers CI parity checks, the runtime invariant engine, boot-time validation, the forensic verifier, the state-machine guards, and safety-regression detection.
**Companions (must stay consistent; they win on conflict, in this order):**
1. [`safety-invariant.md`](./safety-invariant.md) (SAFETY-INVARIANT-001) — §5 is authoritative for invariant text.
2. [`go-state-machine.md`](./go-state-machine.md) (GO-STATE-MACHINE-001) — runtime assertions R1–R25.
3. [`state-transition-fixture.md`](./state-transition-fixture.md) + [`safety/frozen/state-transitions.json`](../safety/frozen/state-transitions.json) — states, transitions, guards.
4. [`redis-stream-contracts.md`](./redis-stream-contracts.md) — events.
**Change control:** Frozen. Any edit requires an ADR in `docs/adr/`, a recomputed digest, an `invariant_version` bump, and security sign-off — all in one PR.

---

## 0. What this registry is

`safety/invariants.yaml` is the **single machine-readable authority** for the system's 22 formal invariants (INV-001..022). The prose in `safety-invariant.md` §5 *states* the invariants for humans; this registry *encodes* them for machines, binding each one to the concrete states, events, guards, runtime assertions, CI gates, audit requirements, and break-tests that enforce it.

> ### REGISTRY PRINCIPLE
> An invariant that is only written in prose is an aspiration. An invariant in this registry is *wired*: it names the guard that blocks its violation, the runtime assertion that detects it, the CI gate that fails the build, the audit evidence that proves it, and the negative test that confirms the guard fires. The registry is where "we promise X" becomes "X is mechanically prevented, detected, and provable."

This is the fourth file in a deliberately layered chain, each strictly derived from the ones above it and never contradicting them:

```
safety-invariant.md        (the guarantees + INV text — prose authority)
        │ projects to
go-state-machine.md         (the FSM + R1-R25 — control-plane prose)
        │ projects to
state-transitions.json      (states/transitions/guards — machine-readable, hashed)
        │ projects to
invariants.yaml  ◄── THIS   (invariants bound to all of the above — machine-readable, hashed)
```

No layer redesigns the one above it. Where two disagree, the higher layer is authoritative and the lower is corrected — enforced by `CI-INVARIANT-PARITY`.

---

## 1. Philosophy

### 1.1 Invariants are capabilities denied, not rules trusted
The system's core guarantee (no message sent without human approval; no software component *able* to send) is enforced **by construction**. The invariants reflect that: most are capability statements (INV-003/004/005/008/010/011/012/013 — "this component cannot do X") proven by CI + boot, not runtime policy. The registry preserves that framing: each invariant's `failure_mode` is the thing we make structurally impossible, and its `mitigation` is the construction that denies it.

### 1.2 One authority, many consumers
Six subsystems need to know the invariants: the runtime engine, CI, boot validation, the forensic verifier, the state-machine guards, and regression detection. If each kept its own copy, they would drift, and a safety system that disagrees with itself is worse than none. So there is exactly one file, and everything else derives from it (Section 4).

### 1.3 Fail closed, always
Every evaluation phase in the registry's `runtime_evaluation_model` has `failure_policy: fail_closed`. There is no fail-open path for any invariant. The justification is asymmetric cost: refusing or dropping an event strands a draft (recoverable, cheap); a single unapproved outbound action is unrecoverable and is the exact thing the product exists to prevent. When in doubt, the system stops.

### 1.4 Drift is a build/boot failure, never a degraded run
If the registry disagrees with the prose, the fixture, or the embedded engine version, the build fails (`CI-INVARIANT-PARITY`) or the gateway refuses to boot (digest/version mismatch). The system never runs in a state where its invariant authority is uncertain.

---

## 2. Schema rationale (`metadata`, `schema`, per-invariant fields)

Each invariant carries 20 fields. The non-obvious ones and why they exist:

| Field | Why it's there |
|---|---|
| `enforcement_layer` + `owner_layer` | An invariant is often enforced in several layers (defense in depth), but exactly one layer is *accountable*. `owner_layer` prevents the "everyone assumed someone else enforced it" gap. |
| `required_conditions` / `forbidden_conditions` | The machine-checkable predicate form. The runtime engine and forensic verifier evaluate these, not the prose `description`. |
| `guard_dependencies` | Binds the invariant to specific guard ids in `state-transitions.json`. `CI-INVARIANT-PARITY` asserts each named guard actually exists — a renamed guard breaks the build instead of silently un-enforcing the invariant. |
| `runtime_assertions` | Binds to R1–R25 in `go-state-machine.md`, so the in-process engine and offline verifier evaluate the same facts. |
| `ci_checks` | The build-time gates that prove the invariant for the deployed artifact. |
| `audit_requirements` + `evidence_required` | What an auditor needs to *prove* compliance after the fact (Section 6). For capability invariants this is "standing CI + boot evidence"; for state-chain invariants it's "the hash-chained log." |
| `test_mapping` | The break-tests that confirm the guard fires (Section 7). |
| `related_invariants` | The cross-links. Invariants are not independent — INV-001 leans on INV-016/017, INV-021 on INV-013/011. The graph of dependencies is explicit so a change's blast radius is visible. |

**Two version axes** (mirrored from the fixture): `schema_version` (how to parse the file) is separate from `invariant_version` (what it says), so a clarification never forces content re-validation and a content change never rides silently on an old structure.

**Authoritative text caveat:** the `description` fields here are authoritative within this registry, but `safety-invariant.md` §5 wins overall. (Mnemonic phrasings like "INV-008 = tokens are single-use" that appear in casual references are *not* authoritative — INV-008 is key custody; single-use is INV-016. The registry uses the frozen definitions.)

---

## 3. Invariant ownership (enforcement mapping)

The registry's `enforcement_ownership_summary` assigns each invariant a single `owner_layer`:

| Owner layer | Invariants | Theme |
|---|---|---|
| **L1 LangGraph** | INV-002, 004, 006, 007, 009, 010 | Cognition is data-only, has no LinkedIn route, terminates at the gate, cannot self-resume, holds only allowlisted tools. |
| **L2 Gateway** | INV-001, 003, 008, 014, 015, 016, 017, 018, 019, 020, 021, 022 | State authority, token custody/mint/consume, ordering/idempotency, audit, single-writer. The bulk — because the gateway *is* the control plane. |
| **L3 Extension** | INV-005, 011, 012, 013 | The LinkedIn boundary: no send, no submit, no LinkedIn writes, inject-and-observe only. |

Two cross-cutting roles enforce across layers:
- **runtime_engine** — evaluates R1–R25 around every transition for the L2 invariants.
- **audit_verifier** — proves INV-001/019/020/021 offline from the append-only chain.

This is the same Layer 1/2/3 model as `safety-invariant.md` §2, now indexed by invariant so a reviewer can ask "who owns INV-017?" and get one answer (L2 gateway), plus the list of layers that back it up.

---

## 4. Enforcement flow (how the six consumers use the file)

```
                         safety/invariants.yaml  (+ sidecar digest)
                                     │
        ┌────────────────┬───────────┼────────────┬─────────────────┬────────────────┐
        ▼                ▼           ▼            ▼                 ▼                ▼
  CI parity        runtime engine  boot          forensic        state-machine    regression
  (CI-INVARIANT-   (R1-R25 around  validation    verifier         guards          detection
   PARITY +         each Apply,    (digest +     (offline proof   (guard_deps     (negative test
   per-gate)        fail-closed)   version       over audit log)  resolve to real  corpus, 36
                                   parity, R9)                     fixture guards)  break-tests)
```

1. **CI parity** (`CI-INVARIANT-PARITY`) — asserts this file's invariant set equals `safety-invariant.md` §5, and that every `guard_dependency` / `runtime_assertion` / `related_state` / `related_event` referenced here exists in its source of truth. Dangling reference ⇒ build fails.
2. **Runtime invariant engine** — loads the registry, evaluates `required_conditions` / `runtime_assertions` at the phase declared in `runtime_evaluation_model` (pre-transition, transition-time, post-transition), fail-closed, escalating reject → safety_violation → quarantine.
3. **Boot validation** — recomputes the registry digest vs the sidecar and asserts `engine.invariant_version == file.invariant_version` and the fixture digest matches its sidecar. Any mismatch ⇒ refuse to boot.
4. **Forensic verifier** — uses `audit_requirements` / `evidence_required` to prove each invariant from the append-only log (Section 6).
5. **State-machine guards** — the guard pipeline in the fixture is the *mechanism*; the registry is the *index* tying each guard to the invariant it serves, so a guard can't be weakened without failing an invariant's parity check.
6. **Regression detection** — the 36-test negative corpus (Section 7) is run in CI; each test asserts a guard *catches* a deliberate violation.

---

## 5. CI integration

The registry defines 19 CI checks (`ci_checks`), all **required, non-skippable** (a skipped safety test fails the build). The five the deliverable names, plus how they read the registry:

| Check | Asserts | Blocking |
|---|---|---|
| `CI-INVARIANT-PARITY` | registry == prose §5; all references resolve | CRITICAL |
| `CI-CAPABILITY-DENIAL` | no service holds a send/inject/mint capability outside its allowed set (INV-003/004/005/008/010) | CRITICAL |
| `CI-FORBIDDEN-TRANSITION` | every X1–X36 rejected by default-deny; no allowed row contradicts a forbidden one | CRITICAL |
| `CI-SEND-PATH-SCAN` | no path clicks Send, submits the composer, or POSTs to LinkedIn (INV-005/011/012/013) | CRITICAL |
| `CI-TOKEN-AUTHORITY` | tokens gateway-signed, single-use, time-bounded, thread+hash bound; no mint from a non-human event (INV-001/008/014/016/017) | CRITICAL |

Each check declares `blocking_severity`, `failure_output` (what the developer sees), and `remediation` (what to do — never "silence the check"). The remediation language is deliberately uniform: capabilities are *removed*, parity is *reconciled to the authority*, never suppressed.

---

## 6. Audit & forensic mapping

The registry's `forensic_mapping_index` groups invariants by *how an auditor proves them*:

- **Capability invariants** (INV-003/004/005/010/011/012/013) — proven by **standing CI + boot evidence** for the deployed build (pinned by commit + bundle hash). There is no runtime log to inspect because the capability does not exist; the proof is structural.
- **Cryptographic invariants** (INV-007/008/016/017) — proven by **Ed25519 signatures, `jti` single-use records, and `draft_hash` bindings** in the audit chain; an auditor verifies them mathematically.
- **State-chain invariants** (INV-001/014/018/019/020/021/022) — proven by the **append-only hash-chained audit log** plus the **negative-evidence property**: *no `SENT_BY_HUMAN` exists without a preceding human-approved `TOKEN_MINTED` with a matching `draft_hash`.* The forensic verifier walks every thread's causal chain and asserts this.
- **Cognition invariants** (INV-002/006/009) — proven by **graph topology/output tests + checkpoint records** showing suspension at `APPROVAL_GATE` and gateway-initiated resume.

Per-invariant, `evidence_required` lists exactly what to produce. This is what lets the client (or a third party) be *shown* the guarantee, not just told it.

---

## 7. Negative test corpus

The registry carries 36 break-tests (`SAFE-NEG-001..036`) — the 15 from `safety-invariant.md` §4.5 plus 21 new ones extending coverage to every invariant. Each test has an `attempt` (the deliberate violation), an `expected` outcome (always a **hard** failure — reject / `SAFETY_VIOLATION` / build-fail / boot-refuse, never a silent pass), and the `invariants` it guards.

Coverage is verified mechanically (and was checked when this file was written):
- **Every invariant INV-001..022 maps to at least one negative test.**
- **Every test's invariant references resolve** to a real invariant id.

Examples spanning the classes:

| Test | Attempt | Expected | Invariant |
|---|---|---|---|
| SAFE-NEG-001 | AI emits a forged approval token and tries to resume | verification fails, no mint | INV-007/008/014 |
| SAFE-NEG-019 | add an "auto-approve trusted contact" flag | `CI-NO-AUTOSEND-PATH` fails build | INV-015/019 |
| SAFE-NEG-020 | write `status` from outside `statemachine.Apply` | `CI-STATE-MACHINE` AST scan fails | INV-022 |
| SAFE-NEG-024 | `human.send.observed` from a non-`INJECTED` state | recorded as independent send, not a state advance | INV-021 |
| SAFE-NEG-034 | tamper the frozen fixture / this registry | digest mismatch, boot refused | INV-022/020 |

These are *meta-tests*: they assert the guards catch violations, so a regression that weakens a guard is caught by the test that expects the guard to fire.

---

## 8. Drift prevention

Four mechanisms (registry `drift_prevention`), all fail-closed:

1. **Invariant parity** — `CI-INVARIANT-PARITY` compares this file to prose §5 and to the engine's embedded registry; the three must agree exactly.
2. **Schema checksum** — SHA-256 of this file pinned in the sidecar and here:

   > **Pinned digest (`invariant_version` 1):**
   > `fe7689ed5808b9a3d9c90a1d8ea369a3bf81283bd337eef588596067cea3b592`

   Verify with `sha256sum safety/invariants.yaml` vs `safety/invariants.sha256` (gate `CI-FIXTURE-INTEGRITY`).
3. **Frozen-fixture comparison** — every guard/state/event/assertion referenced here must exist in `safety/frozen/state-transitions.json`; a dangling reference fails parity.
4. **Boot refusal** — the gateway asserts `(registry digest == sidecar) AND (engine invariant_version == file invariant_version) AND (fixture digest == its sidecar)` at boot; any mismatch ⇒ refuse to boot, emit a boot `SAFETY_VIOLATION`, do not enter the consumer loop.

The principle: **the system fails closed on invariant drift.** It would rather not run than run with an uncertain invariant authority.

---

## 9. How to add a new invariant safely

A deliberately heavyweight, fail-closed process (an easy path to *add* invariants would be an easy path to *weaken* them):

1. **Write the prose first.** Add `INV-0NN` to `safety-invariant.md` §5 with statement, layer, and a CI gate. The prose is the authority; the registry follows.
2. **Open an ADR** in `docs/adr/` explaining the threat it closes, the layer that owns it, and why existing invariants don't cover it.
3. **Add the registry entry** with all 20 fields. `guard_dependencies` / `runtime_assertions` / `related_states` / `related_events` must reference *existing* ids in the fixture, or add them to the fixture in the same PR (which is itself change-controlled, with its own digest bump).
4. **Add the CI gate** that proves it, wired as a required, non-skippable check with `failure_output` + `remediation`.
5. **Add ≥1 negative test** (`SAFE-NEG-0NN`) that asserts the guard catches the violation. Coverage CI fails if any invariant has no test.
6. **Bump `invariant_version`**, recompute the digest, update the sidecar **and** §8 here.
7. **Security sign-off** on the PR.

To *weaken or remove* an invariant: the same process, plus you must remove its CI gate in the same PR. **Weakening an invariant while leaving its gate is a build-breaking offense** — the gate will fail against the weakened registry, which is the intended tripwire. There is no approved process to add a capability that constitutes an outbound action; such a proposal is rejected at architecture review.

---

## Appendix A — Registry self-checks (verified at authoring time)

| Property | Result |
|---|---|
| Invariants present | 22 (INV-001..022, complete and contiguous) |
| All 20 schema fields present on every invariant | yes (no missing fields) |
| Negative tests | 36 (SAFE-NEG-001..036) |
| Every invariant covered by ≥1 negative test | yes |
| Every test's invariant references resolve | yes |
| YAML validity | valid (single document) |
| Pinned digest matches sidecar | `fe7689…b592` |

## Appendix B — Change control

- Frozen. Edits require an ADR, a recomputed digest (sidecar + §8 here), an `invariant_version` bump, and security sign-off — all in one PR.
- This registry is mirrored against `safety-invariant.md` §5 (text), `go-state-machine.md` (R1–R25), and `safety/frozen/state-transitions.json` (guards/states/events). The four MUST agree; `CI-INVARIANT-PARITY` enforces it.
- Adding an outbound-action capability, weakening any guard, or admitting any forbidden transition is a guarantee violation, rejected at architecture review.
