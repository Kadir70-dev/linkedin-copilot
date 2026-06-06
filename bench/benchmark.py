"""Model benchmark for the LinkedIn copilot outreach + messaging pipeline.

Runs the REAL ai-brain cognition (generate_outreach best-of-N + grounding
validator + fallback, and the messaging reply graph) against each candidate
model, so the numbers reflect what production would actually ship. Grounding /
hallucination are scored by the SAME grounding_check() the gateway serves, so
they are objective, not eyeballed.

Usage (inside the ai-brain container, which has redis/requests/langchain):
    MODELS="qwen2.5:3b,qwen3:4b,qwen3:8b" python benchmark.py
"""
import os
import time
import json
import importlib

import worker  # the real cognition module

MODELS = [m.strip() for m in os.getenv(
    "MODELS", "qwen2.5:3b,qwen3:4b,qwen3:8b").split(",") if m.strip()]

# --- Test corpus -------------------------------------------------------------
# Mix of THIN cards (hallucination traps from the CLAUDE.md decisions log) and a
# RICH profile (where a bigger model should add real specificity).
OUTREACH_CASES = [
    {
        "profile_id": "gianluca",
        "target_name": "Gianluca Orsini",
        # Thin card: role+company ONLY. The 3B model historically invented
        # "financial services / customer experience" here. Grounding must catch it.
        "profile_text": "Name: Gianluca Orsini\nHeadline: CTO at MLG Group\nLocation: Milan, Italy",
    },
    {
        "profile_id": "apparel",
        "target_name": "Saima Shaikh",
        # Apparel profile — the few-shot example mentions "GCC"; bleed trap.
        "profile_text": "Name: Saima Shaikh\nHeadline: Brand & Product Lead, apparel\nLocation: Dubai",
    },
    {
        "profile_id": "cloudify",
        "target_name": "Rohan Mehta",
        # RICH profile: lots of grounded vocabulary; a stronger model should
        # produce a specific, in-band, well-grounded note here.
        "profile_text": ("Name: Rohan Mehta\nHeadline: VP Sales at Cloudify — "
                         "scaling B2B SaaS go-to-market\nAbout: 12 years leading "
                         "enterprise sales teams, revenue operations, and partner "
                         "channels for cloud infrastructure startups.\n"
                         "Company: Cloudify\nLocation: Bengaluru"),
    },
    {
        "profile_id": "thin_name",
        "target_name": "Aiko Tanaka",
        # Almost nothing — should trigger the grounded headline-quoting fallback.
        "profile_text": "Name: Aiko Tanaka\nHeadline: Product Manager",
    },
]

MESSAGING_CASES = [
    "Hi! Are you free Thursday to chat about the senior backend role?",
    "Thanks for the deck — can you send pricing for the enterprise tier?",
]


def bench_model(model: str):
    m = model.lower()
    worker.MODEL = model
    # Mirror the production shim: thinking Qwen3 (no "instruct") gets /no_think +
    # token cap; instruct/Qwen2.5 take the clean path.
    worker.IS_QWEN3_THINKING = m.startswith("qwen3") and "instruct" not in m
    worker.QWEN3_NUM_PREDICT = 220 if worker.IS_QWEN3_THINKING else 0
    res = {"model": model, "outreach": [], "messaging": []}

    for c in OUTREACH_CASES:
        profile = worker.strip_mutual(c["profile_text"])
        fname = worker.first_name(c["target_name"])
        grounded = worker.grounded_set(profile, c["target_name"])
        t0 = time.time()
        out = worker.OUTREACH_GRAPH.invoke({
            "profile_id": c["profile_id"],
            "profile_text": c["profile_text"],
            "target_name": c["target_name"],
        })
        dt = time.time() - t0
        note = out["outreach"]
        used, ung, conf = worker.grounding_check(note, grounded, fname)
        # Did we fall back? Fallback notes match the deterministic templates' shape.
        res["outreach"].append({
            "id": c["profile_id"],
            "latency_s": round(dt, 1),
            "len": len(note),
            "in_band": 120 <= len(note) <= 180,
            "grounding": conf,
            "used": used,
            "ungrounded": ung,            # hallucinated terms (lower=better)
            "n_ungrounded": len(ung),
            "banned": worker.banned_hits(note),
            "style": out.get("style"),
            "note": note,
        })

    for text in MESSAGING_CASES:
        t0 = time.time()
        out = worker.GRAPH.invoke({"thread_id": "bench", "text": text})
        dt = time.time() - t0
        draft = out["draft"]
        res["messaging"].append({
            "latency_s": round(dt, 1),
            "len": len(draft),
            "words": len(draft.split()),
            "draft": draft,
        })
    return res


def main():
    all_res = []
    for m in MODELS:
        print(f"\n===== BENCHMARKING {m} =====", flush=True)
        try:
            r = bench_model(m)
        except Exception as e:
            print(f"  FAILED: {e}", flush=True)
            r = {"model": m, "error": str(e)}
        all_res.append(r)
        # live summary
        if "outreach" in r:
            for o in r["outreach"]:
                print(f"  [{o['id']:9}] {o['latency_s']:5.1f}s  len={o['len']:3} "
                      f"band={'Y' if o['in_band'] else 'n'}  ground={o['grounding']}  "
                      f"halluc={o['n_ungrounded']}  :: {o['note']}", flush=True)
            for mm in r["messaging"]:
                print(f"  [messaging] {mm['latency_s']:5.1f}s  {mm['words']}w :: {mm['draft'][:80]}", flush=True)

    with open("/tmp/bench_results.json", "w") as f:
        json.dump(all_res, f, indent=2)
    print("\nWROTE /tmp/bench_results.json", flush=True)

    # --- aggregate table -----------------------------------------------------
    print("\n\n===== SUMMARY =====")
    print(f"{'model':12} {'avg_lat':>8} {'avg_ground':>11} {'tot_halluc':>11} {'in_band':>8} {'msg_lat':>8}")
    for r in all_res:
        if "outreach" not in r:
            print(f"{r['model']:12}  ERROR: {r.get('error')}")
            continue
        o = r["outreach"]
        avg_lat = sum(x["latency_s"] for x in o) / len(o)
        avg_ground = sum(x["grounding"] for x in o) / len(o)
        tot_h = sum(x["n_ungrounded"] for x in o)
        band = sum(1 for x in o if x["in_band"])
        msg_lat = sum(x["latency_s"] for x in r["messaging"]) / len(r["messaging"])
        print(f"{r['model']:12} {avg_lat:7.1f}s {avg_ground:11.2f} {tot_h:11} "
              f"{band:>5}/{len(o)} {msg_lat:7.1f}s")


if __name__ == "__main__":
    main()
