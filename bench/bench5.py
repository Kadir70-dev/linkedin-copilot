"""Clean 5-founder head-to-head: qwen2.5:3b vs qwen3:4b-instruct-2507.
Real generate_outreach pipeline. think:false enforced for any thinking Qwen3
(n/a for instruct). Scores grounding/hallucination/band/personalization/latency
objectively via the production grounding_check.
"""
import os
import time
import worker

MODELS = [m.strip() for m in os.getenv(
    "MODELS", "qwen2.5:3b,qwen3:4b-instruct-2507-q4_K_M").split(",") if m.strip()]

# 5 realistic founder profiles: 2 thin (hallucination traps), 3 richer.
FOUNDERS = [
    ("g1", "Gianluca Orsini", "Name: Gianluca Orsini\nHeadline: CTO at MLG Group\nLocation: Milan, Italy"),
    ("g2", "Tomás Becker", "Name: Tomás Becker\nHeadline: Founder"),
    ("g3", "Rohan Mehta", "Name: Rohan Mehta\nHeadline: Founder, B2B SaaS — go-to-market for cloud infra\nAbout: 12 years scaling enterprise sales and partner channels.\nCompany: Cloudify\nLocation: Bengaluru"),
    ("g4", "Priya Nair", "Name: Priya Nair\nHeadline: Founder & CEO, EdTech for rural India\nCompany: Vidya\nLocation: Kochi"),
    ("g5", "Hiroshi Sato", "Name: Hiroshi Sato\nHeadline: Founder at Sato Robotics | industrial automation\nAbout: Ex-Fanuc. Building factory-floor cobots.\nLocation: Osaka"),
]


def run(model):
    m = model.lower()
    worker.MODEL = model
    worker.IS_QWEN3_THINKING = m.startswith("qwen3") and "instruct" not in m
    worker.QWEN3_NUM_PREDICT = 220 if worker.IS_QWEN3_THINKING else 0
    print(f"\n===== {model}  (thinking={worker.IS_QWEN3_THINKING}) =====", flush=True)
    rows = []
    for pid, name, profile in FOUNDERS:
        grounded = worker.grounded_set(worker.strip_mutual(profile), name)
        fname = worker.first_name(name)
        t0 = time.time()
        out = worker.OUTREACH_GRAPH.invoke({"profile_id": pid, "profile_text": profile, "target_name": name})
        dt = time.time() - t0
        note = out["outreach"]
        used, ung, conf = worker.grounding_check(note, grounded, fname)
        in_band = 120 <= len(note) <= 180
        rows.append((pid, dt, len(note), in_band, conf, used, ung, worker.banned_hits(note), note))
        print(f"  [{pid}] {dt:5.1f}s len={len(note):3} band={'Y' if in_band else 'n'} "
              f"ground={conf} halluc={len(ung)}{ung if ung else ''} pers={worker.personalization_score(used)}",
              flush=True)
        print(f"       {note}", flush=True)
    n = len(rows)
    avg_lat = sum(r[1] for r in rows) / n
    band = sum(1 for r in rows if r[3])
    avg_g = sum(r[4] for r in rows) / n
    tot_h = sum(len(r[6]) for r in rows)
    perfect_ground = sum(1 for r in rows if len(r[6]) == 0)
    print(f"  >>> avg_lat={avg_lat:.1f}s  in_band={band}/{n}  avg_ground={avg_g:.2f}  "
          f"tot_halluc={tot_h}  zero_halluc_notes={perfect_ground}/{n}", flush=True)
    return model, avg_lat, band, n, avg_g, tot_h, perfect_ground


def main():
    summ = [run(m) for m in MODELS]
    print("\n\n===== FINAL =====", flush=True)
    print(f"{'model':38} {'avg_lat':>8} {'in_band':>8} {'avg_grnd':>9} {'halluc':>7} {'clean':>7}")
    for model, avg_lat, band, n, avg_g, tot_h, pg in summ:
        print(f"{model:38} {avg_lat:7.1f}s {band:>4}/{n}  {avg_g:8.2f} {tot_h:>7} {pg:>4}/{n}", flush=True)


if __name__ == "__main__":
    main()
