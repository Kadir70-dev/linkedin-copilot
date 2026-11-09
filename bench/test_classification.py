"""Classification tests that import and exercise the PRODUCTION worker.py functions.

No copies, no adapts — the actual detect_style, is_premium_profile,
hermes_role_company, grounded_set, _is_grounded, and grounding_check from
services/ai-brain/worker.py are imported directly.

Run from the repo root:
    python3 bench/test_classification.py

Requirements: none beyond the standard library (redis/requests are only used
inside the main() consumer loop, which is guarded by __name__=="__main__").
"""
import os
import sys

# Point at the production module; set env-vars before import so module-level
# constants (IS_FASHION, FAST_MODE, SEQUENTIAL, …) initialise cleanly without
# a live Redis or Ollama instance.
os.environ.setdefault("REDIS_ADDR", "localhost:6379")
os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434")
os.environ.setdefault("MODE", "fashion")        # test both fashion and general paths
os.environ.setdefault("FAST_MODE", "false")
os.environ.setdefault("SEQUENTIAL_MODE", "true")
os.environ.setdefault("PREMIUM_ROUTING", "true")
os.environ.setdefault("RAM_MIN_FREE_MB", "0")   # disable RAM watchdog in tests

_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, "..", "services", "ai-brain"))

import worker  # noqa: E402  — must come after sys.path insert

# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

passed = failed = 0


def expect(label: str, got, expected):
    global passed, failed
    ok = got == expected
    status = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {label!r}: expected {expected!r}, got {got!r}")
    print(f"  {status}  {label}")


def expect_not(label: str, got, bad):
    global passed, failed
    ok = got != bad
    status = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {label!r}: expected NOT {bad!r}, got {got!r}")
    print(f"  {status}  {label}")


def section(title: str):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# 1. VP / founder boundary (the core regression the reviewer flagged)
# ---------------------------------------------------------------------------
section("1. VP / founder boundary — 'Vice President' must NOT be founder")

vp_sales = "Vice President of Sales at TechCorp"
style_vp, _ = worker.detect_style(vp_sales)
expect("'Vice President of Sales' style is NOT founder", style_vp == "founder", False)
expect("'Vice President of Sales' style is 'sales'", style_vp, "sales")

vp_eng = "Vice President of Engineering"
style_vp_eng, _ = worker.detect_style(vp_eng)
expect_not("'Vice President of Engineering' is NOT founder", style_vp_eng, "founder")

vp_abbrev = "VP of Sales | Helping teams hit quota"
style_vp2, _ = worker.detect_style(vp_abbrev)
expect("'VP of Sales' style is NOT founder", style_vp2 == "founder", False)

president_alone = "President at Acme Holdings"
style_pres, _ = worker.detect_style(president_alone)
expect("Standalone 'President' IS founder", style_pres, "founder")

ceo = "CEO & Founder at StartupCo"
style_ceo, _ = worker.detect_style(ceo)
expect("'CEO & Founder' IS founder", style_ceo, "founder")

# ---------------------------------------------------------------------------
# 2. is_premium_profile: VP -> base, CEO/Founder -> premium
# ---------------------------------------------------------------------------
section("2. is_premium_profile — VP stays base, CEO/Founder go premium")

expect(
    "'Vice President of Sales' NOT premium",
    worker.is_premium_profile("sales", "Vice President of Sales at TechCorp"),
    False,
)
expect(
    "'VP of Engineering' NOT premium",
    worker.is_premium_profile("networking", "VP of Engineering"),
    False,
)
expect(
    "'CEO & Founder' IS premium",
    worker.is_premium_profile("founder", "CEO & Founder at StartupCo"),
    True,
)
expect(
    "'Owner at FamilyBiz' IS premium",
    worker.is_premium_profile("founder", "Owner at FamilyBiz"),
    True,
)
expect(
    "Standalone 'President' IS premium",
    worker.is_premium_profile("founder", "President at Acme Holdings"),
    True,
)
expect(
    "'Managing Director' IS premium",
    worker.is_premium_profile("founder", "Managing Director at Dubai Investments"),
    True,
)
expect(
    "sourcing style is NEVER premium",
    worker.is_premium_profile("sourcing", "Head of Sourcing at Zara"),
    False,
)

# ---------------------------------------------------------------------------
# 3. Company extraction: "Head of Sourcing at Zara" -> role + company
# ---------------------------------------------------------------------------
section("3. hermes_role_company — 'at X' split, not 'of X'")

role, company = worker.hermes_role_company("Head of Sourcing at Zara")
expect("'Head of Sourcing at Zara' role", role, "Head of Sourcing")
expect("'Head of Sourcing at Zara' company", company, "Zara")

role2, company2 = worker.hermes_role_company("VP of Sales @ Acme")
expect("'VP of Sales @ Acme' role", role2, "VP of Sales")
expect("'VP of Sales @ Acme' company", company2, "Acme")

# 'for' is intentionally NOT a split separator (too ambiguous: "Buyer for Fashion"
# could mean the buyer's category, not their employer).  Full title returned as role.
role3, company3 = worker.hermes_role_company("Buyer for Fashion House")
expect("'Buyer for Fashion House' role (no split on 'for')", role3, "Buyer for Fashion House")
expect("'Buyer for Fashion House' company (empty — no unambiguous separator)", company3, "")

role4, company4 = worker.hermes_role_company("Founder")
expect("'Founder' (no company) role", role4, "Founder")
expect("'Founder' (no company) company", company4, "")

# ---------------------------------------------------------------------------
# 4. Sourcing titles -> base model routing
# ---------------------------------------------------------------------------
section("4. Sourcing titles use base model, never premium")

sourcing_profiles = [
    "Head of Sourcing at Zara",
    "Sourcing Manager | Apparel & Textiles",
    "Senior Buyer, Footwear Division",
    "Procurement Director — Fashion Retail",
    "Merchandising Lead at H&M",
]
for p in sourcing_profiles:
    s, _ = worker.detect_style(p)
    premium = worker.is_premium_profile(s, p)
    expect(f"{p!r} NOT premium", premium, False)

# ---------------------------------------------------------------------------
# 5. CEO / founder-specific profiles use premium
# ---------------------------------------------------------------------------
section("5. CEO / Founder profiles route to premium model")

founder_profiles = [
    ("Founder and CEO", "founder"),
    ("Co-Founder at BuildCo", "founder"),
    ("Owner at FamilyBiz", "founder"),
    ("Managing Director at Dubai Investments", "founder"),
    ("Entrepreneur | Building in Stealth", "founder"),
    ("Chairman at XYZ Holdings", "founder"),
]
for profile, expected_style in founder_profiles:
    s, _ = worker.detect_style(profile)
    expect(f"{profile!r} style={expected_style}", s, expected_style)
    premium = worker.is_premium_profile(s, profile)
    expect(f"{profile!r} IS premium", premium, True)

# ---------------------------------------------------------------------------
# 6. "Founder & VP of Sales" — founder wins (explicit founder keyword)
# ---------------------------------------------------------------------------
section("6. Explicit founder + VP combination -> founder wins")

combined = "Founder & VP of Sales at GrowthCo"
style_combined, _ = worker.detect_style(combined)
expect("'Founder & VP of Sales' IS founder (founder wins)", style_combined, "founder")
expect(
    "'Founder & VP of Sales' IS premium",
    worker.is_premium_profile(style_combined, combined),
    True,
)

# ---------------------------------------------------------------------------
# 7. Grounding: _is_grounded stricter than 4-char prefix
# ---------------------------------------------------------------------------
section("7. _is_grounded — no 4-char prefix false positives")

grounded = worker.grounded_set("sustainable fashion brand at Zara", "Lara")
# "sustainability" should NOT be grounded from "sustainable" (7/14 = 0.50 < 0.80)
expect(
    "'sustainability' NOT grounded from 'sustainable'",
    worker._is_grounded("sustainability", grounded),
    False,
)
# "sustain" is in "sustainable" and 7/11 = 0.636 < 0.80 — not grounded
expect(
    "'sustain' NOT grounded from 'sustainable'",
    worker._is_grounded("sustain", grounded),
    False,
)
# "brands" IS grounded from "brand" (5/6 = 0.833 >= 0.80)
expect(
    "'brands' IS grounded from 'brand'",
    worker._is_grounded("brands", grounded),
    True,
)
# "fashionable" should NOT be grounded from "fashion" (6/11 = 0.545 < 0.80)
expect(
    "'fashionable' NOT grounded from 'fashion'",
    worker._is_grounded("fashionable", grounded),
    False,
)
# exact match always works
expect(
    "'fashion' IS grounded (exact)",
    worker._is_grounded("fashion", grounded),
    True,
)

# ---------------------------------------------------------------------------
# 8. FAST_MODE defined before SEQUENTIAL (NameError regression guard)
# ---------------------------------------------------------------------------
section("8. Module-level definition order (FAST_MODE before SEQUENTIAL)")

# If the NameError regression existed, the import above would have already failed.
# This just asserts the final values are readable.
expect("FAST_MODE is bool", isinstance(worker.FAST_MODE, bool), True)
expect("SEQUENTIAL is bool", isinstance(worker.SEQUENTIAL, bool), True)
expect("SEQUENTIAL = SEQUENTIAL_MODE or FAST_MODE", worker.SEQUENTIAL, worker.SEQUENTIAL_MODE or worker.FAST_MODE)

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
print("\n" + "=" * 50)
print(f"Results: {passed} passed, {failed} failed")
if failed == 0:
    print("All tests PASSED.")
else:
    print("FAILURES — see above.")
    sys.exit(1)
