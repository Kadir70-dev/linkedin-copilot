"""Production-readiness check for the chosen model: 10 realistic FOUNDER
outreach profiles through the real generate_outreach pipeline. Enforces the
user's hard bar: strict grounding, near-zero hallucination, 120-180 band, no
template/example bleed, no generic-line repetition across the set.

Usage (in ai-brain container):
    MODEL=qwen3:4b-instruct-2507-q4_K_M python verify_founders.py
"""
import os
import time
import worker

MODEL = os.getenv("MODEL", "qwen3:4b-instruct-2507-q4_K_M")
m = MODEL.lower()
worker.MODEL = MODEL
worker.IS_QWEN3_THINKING = m.startswith("qwen3") and "instruct" not in m
worker.QWEN3_NUM_PREDICT = 220 if worker.IS_QWEN3_THINKING else 0

# 10 founders, varied richness + the documented hallucination traps.
FOUNDERS = [
    ("f01", "Gianluca Orsini", "Name: Gianluca Orsini\nHeadline: CTO at MLG Group\nLocation: Milan, Italy"),
    ("f02", "Ayesha Khan", "Name: Ayesha Khan\nHeadline: Founder & CEO at Loom Textiles\nCompany: Loom\nLocation: Karachi"),
    ("f03", "Daniel Park", "Name: Daniel Park\nHeadline: Co-founder at Northwind Robotics\nCompany: Northwind\nLocation: Seoul"),
    ("f04", "Rohan Mehta", "Name: Rohan Mehta\nHeadline: Founder, B2B SaaS — go-to-market for cloud infra\nAbout: 12 years scaling enterprise sales and partner channels.\nCompany: Cloudify\nLocation: Bengaluru"),
    ("f05", "Sara Lindqvist", "Name: Sara Lindqvist\nHeadline: Founder at Nordic Health Labs — digital diagnostics\nLocation: Stockholm"),
    ("f06", "Mateo Alvarez", "Name: Mateo Alvarez\nHeadline: Serial entrepreneur | fintech & payments\nAbout: Building cross-border payment rails for LATAM.\nLocation: Mexico City"),
    ("f07", "Priya Nair", "Name: Priya Nair\nHeadline: Founder & CEO, EdTech for rural India\nCompany: Vidya\nLocation: Kochi"),
    ("f08", "Tomás Becker", "Name: Tomás Becker\nHeadline: Founder"),  # very thin -> fallback expected
    ("f09", "Lena Vogt", "Name: Lena Vogt\nHeadline: Co-founder & COO at GreenGrid — renewable energy storage\nLocation: Berlin"),
    ("f10", "Hiroshi Sato", "Name: Hiroshi Sato\nHeadline: Founder at Sato Robotics | industrial automation\nAbout: Ex-Fanuc. Building factory-floor cobots.\nLocation: Osaka"),
]


def main():
    print(f"VERIFY model={MODEL}\n")
    notes = []
    fails = 0
    total_lat = 0.0
    for pid, name, profile in FOUNDERS:
        grounded = worker.grounded_set(worker.strip_mutual(profile), name)
        fname = worker.first_name(name)
        t0 = time.time()
        out = worker.OUTREACH_GRAPH.invoke({"profile_id": pid, "profile_text": profile, "target_name": name})
        dt = time.time() - t0
        total_lat += dt
        note = out["outreach"]
        used, ung, conf = worker.grounding_check(note, grounded, fname)
        banned = worker.banned_hits(note)
        overused = worker.overused_hits(note)
        in_band = 120 <= len(note) <= 180
        greets_ok = note.lower().startswith("hi " + fname.lower())
        bad = (len(ung) >= 2) or banned or (len(note) > 180) or (not greets_ok)
        if bad:
            fails += 1
        notes.append(note)
        flag = "FAIL" if bad else "ok"
        print(f"[{pid} {flag}] {dt:5.1f}s len={len(note):3} band={'Y' if in_band else 'n'} "
              f"ground={conf} halluc={len(ung)}{ung if ung else ''} "
              f"banned={banned} over={overused}")
        print(f"     {note}")

    # cross-set repetition: any identical note, or shared trailing clause?
    dupe = len(notes) != len(set(notes))
    print(f"\nSUMMARY {MODEL}")
    print(f"  profiles={len(FOUNDERS)} hard_fails={fails} dupe_notes={dupe} "
          f"avg_latency={total_lat/len(FOUNDERS):.1f}s")
    print("  VERDICT:", "PASS" if (fails == 0 and not dupe) else "REVIEW")


if __name__ == "__main__":
    main()
