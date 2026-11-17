"""Connection-note relationship/identity claim gate (worker.relationship_claims).
Offline; run from the repo root. Includes the live 2026-11-15 failure
'Hi Lara, fellow founder at Hillbridge Partners ...'."""
import os
import sys

os.environ.setdefault("REDIS_ADDR", "localhost:6379")
os.environ["RAM_MIN_FREE_MB"] = "0"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "ai-brain"))
import worker as W  # noqa: E402

PROFILE = "Name: Lara Matibaeva\nHeadline: Founder of Hillbridge Partners | Talent Advisory"
fails = 0


def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f" -> {detail}"))
    fails += 0 if cond else 1


bad = [
    "Hi Lara, fellow founder at Hillbridge Partners, I admire your focus on Talent Advisory.",
    "Hi Lara, as fellow founders leading Hillbridge Partners in talent advisory, let's support each other.",
    "Hi Lara, great meeting you last week, would love to stay in touch about talent advisory.",
    "Hi Lara, our mutual friend suggested I reach out about Hillbridge Partners.",
    "Hi Lara, loved your recent post on talent advisory at Hillbridge Partners.",
    "Hi Lara, as we discussed, Hillbridge Partners and talent advisory are a great fit.",
    "Hi Lara, I also run a talent advisory firm, so Hillbridge Partners caught my eye.",
]
for b in bad:
    check("rejects: " + b[9:60], W.relationship_claims(b, PROFILE), W.relationship_claims(b, PROFILE))
good = [
    "Hi Lara, building Hillbridge Partners around talent advisory is a focused bet. I'd value connecting and following your work.",
    "Hi Lara, your work as Founder of Hillbridge Partners in talent advisory stood out. Would be glad to connect.",
]
for g in good:
    check("accepts: " + g[9:60], not W.relationship_claims(g, PROFILE), W.relationship_claims(g, PROFILE))
check("allows a phrase that is in the profile",
      not W.relationship_claims("Hi Sam, great meeting you at the expo.", "Context: great meeting you at the expo"))
for seed in ("a", "b", "c", "d", "e", "f"):
    fb = W.grounded_fallback("Lara", PROFILE, seed)
    check(f"fallback has no relationship claim ({seed})", not W.relationship_claims(fb, PROFILE), fb)
print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
