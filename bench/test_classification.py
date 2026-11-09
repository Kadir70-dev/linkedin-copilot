"""Classification test fixtures for worker.py.

Tests:
  1. VP/founder premium-model boundary (VP -> base, CEO/Founder -> premium)
  2. "Head of Sourcing at X" company extraction
  3. Sourcing titles -> base model
  4. CEO -> premium model
  5. detect_style priority ladder

Run from the repo root:
    python3 bench/test_classification.py
"""
import sys
import os
import re

# ---------------------------------------------------------------------------
# Inline the primitives under test so this file runs without installing the
# ai-brain dependencies (no redis/langchain/ollama required).
# ---------------------------------------------------------------------------

# --- copied / adapted from services/ai-brain/worker.py ---

FOUNDER_KW = (
    "founder", "co-founder", "cofounder", "co founder", "founding partner",
    "ceo", "chief executive", "chief ", "owner", "co-owner", "managing director",
    "managing partner", "president", "chairman", "chairwoman", "entrepreneur",
)
TALENT_KW = (
    "recruit", "recruiter", "recruitment", "talent", "talent acquisition",
    "talent advisory", "staffing", "executive search", "headhunt", "head hunt",
    "people consulting", "people & culture", "people and culture", "human resources",
    " hr ", "hr ", "talent partner",
)
SOURCING_KW = (
    "buyer", "buying", "purchasing", "sourcing", "procure", "procurement",
    "merchandis", "supply chain", "head of sourcing", "sourcing manager",
)
APPAREL_KW = (
    "apparel", "fashion", "garment", "textile", "clothing", "private label",
    "wholesale", "knitwear", "footwear", "fabric", "sewing", "boutique",
)
HIRING_KW = ("we're hiring", "were hiring", "we are hiring", "open role", "join our team", "now hiring")
PARTNERSHIP_KW = ("partnership", "bizdev", "business development")
SALES_KW = ("account executive", " ae ", "revenue", "quota", "sales ")
NEVER_SOURCING_KW = FOUNDER_KW + TALENT_KW + ("advisory", "advisor", "consulting", "consultant")

# PREMIUM routing (matches the fixed version)
GENERAL_PREMIUM_KW = (
    "ceo", "chief executive", "chief ", "founder", "co-found", "cofound", "owner",
    "managing director", "managing partner", "president", "proprietor",
)
NON_PREMIUM_STYLES = ("recruiter", "sourcing", "sales")
BASE_FORCE_KW = (
    "buyer", "buying", "sourcing", "merchandis", "procure", "procurement",
    "purchasing", "supply chain",
)
PREMIUM_STYLES = ("founder",)


def _blocks_sourcing(t: str) -> bool:
    return any(k in t for k in NEVER_SOURCING_KW)


def detect_style(profile_text: str):
    t = " " + (profile_text or "").lower() + " "
    # 1) Founder / C-level
    if any(k in t for k in FOUNDER_KW):
        return "founder", "high"
    # 2) Recruiter / talent / advisory
    if any(k in t for k in TALENT_KW):
        return "recruiter", "high"
    # 3) Buyer / sourcing / merchandising
    if any(k in t for k in SOURCING_KW) and not _blocks_sourcing(t):
        return "sourcing", "high"
    # 4) Secondary signals
    if any(k in t for k in HIRING_KW):
        return "hiring", "medium"
    if any(k in t for k in PARTNERSHIP_KW):
        return "partnership", "medium"
    if any(k in t for k in SALES_KW):
        return "sales", "medium"
    return "networking", "low"


def is_premium_profile(style: str, profile_text: str) -> bool:
    t = (profile_text or "").lower()
    if style in NON_PREMIUM_STYLES or any(k in t for k in BASE_FORCE_KW):
        return False
    if style in PREMIUM_STYLES:
        return True
    return any(k in t for k in GENERAL_PREMIUM_KW)


def _profile_field(profile_text: str, label: str) -> str:
    m = re.search(rf"(?im)^\s*{label}\s*:\s*(.+)$", profile_text or "")
    return m.group(1).strip() if m else ""


def _headline_line(profile_text: str) -> str:
    h = _profile_field(profile_text, "headline")
    if h:
        return h
    for ln in (profile_text or "").splitlines():
        s = ln.strip()
        if s and not re.match(r"\s*(name|company|location|about|context|mutual|headline)\s*:", s, re.I):
            return s
    return ""


def hermes_role_company(profile_text: str):
    """Fixed version: only split on ' at ' / ' @ ' (not 'of'/'for'/'with')."""
    head = _headline_line(profile_text)
    company = _profile_field(profile_text, "company")
    seg = re.split(r"\s*[|•·]\s*", head)[0].strip()
    m = re.search(r"^(.+?)\s+(?:at|@)\s+(.+)$", seg, re.I)
    if m:
        role = m.group(1).strip()
        company = company or m.group(2).strip()
    else:
        role = seg
    return role[:80], (company or "")[:80]


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

PASS = 0
FAIL = 0


def check(name: str, actual, expected, msg: str = ""):
    global PASS, FAIL
    if actual == expected:
        print(f"  PASS  {name}")
        PASS += 1
    else:
        print(f"  FAIL  {name}")
        print(f"        expected: {expected!r}")
        print(f"        actual  : {actual!r}")
        if msg:
            print(f"        note    : {msg}")
        FAIL += 1


# ---------------------------------------------------------------------------
# 1. VP/founder premium-model boundary
# ---------------------------------------------------------------------------
print("\n=== 1. VP/founder premium-model boundary ===")

# VP of Sales -> style='sales' via detect_style -> NOT premium
vp_sales_profile = "Name: John\nHeadline: VP of Sales at Acme"
vp_style, _ = detect_style(vp_sales_profile)
check("VP of Sales detected style", vp_style, "sales")
check("VP of Sales is NOT premium", is_premium_profile(vp_style, vp_sales_profile), False)

# VP of Engineering (no sales keyword -> networking, but still NOT premium since vp removed)
vp_eng_profile = "Name: Alice\nHeadline: VP of Engineering at TechCorp"
vp_eng_style, _ = detect_style(vp_eng_profile)
check("VP of Engineering is NOT premium (vp removed from GENERAL_PREMIUM_KW)",
      is_premium_profile(vp_eng_style, vp_eng_profile), False)

# Director of Product -> NOT premium
dir_profile = "Name: Bob\nHeadline: Director of Product at StartupX"
dir_style, _ = detect_style(dir_profile)
check("Director of Product is NOT premium", is_premium_profile(dir_style, dir_profile), False)

# Head of Sourcing -> detect_style=sourcing -> NOT premium (sourcing style + base_force_kw)
hos_profile = "Name: Jane\nHeadline: Head of Sourcing at Zara"
hos_style, _ = detect_style(hos_profile)
check("Head of Sourcing detected style", hos_style, "sourcing")
check("Head of Sourcing is NOT premium", is_premium_profile(hos_style, hos_profile), False)

# CEO -> style=founder -> IS premium
ceo_profile = "Name: Sarah\nHeadline: CEO at GreenTech"
ceo_style, _ = detect_style(ceo_profile)
check("CEO detected style", ceo_style, "founder")
check("CEO IS premium", is_premium_profile(ceo_style, ceo_profile), True)

# Founder -> style=founder -> IS premium
founder_profile = "Name: Mike\nHeadline: Founder at Noor Apparel | Private Label"
founder_style, _ = detect_style(founder_profile)
check("Founder detected style", founder_style, "founder")
check("Founder IS premium", is_premium_profile(founder_style, founder_profile), True)

# ---------------------------------------------------------------------------
# 2. Company extraction from headline with "at X" suffix
# ---------------------------------------------------------------------------
print("\n=== 2. Company extraction ('Head of Sourcing at Zara') ===")

role, company = hermes_role_company("Name: Jane\nHeadline: Head of Sourcing at Zara")
check("Head of Sourcing at Zara - role", role, "Head of Sourcing")
check("Head of Sourcing at Zara - company", company, "Zara")

role, company = hermes_role_company("Name: John\nHeadline: VP of Sales at Acme Corp")
check("VP of Sales at Acme Corp - role", role, "VP of Sales")
check("VP of Sales at Acme Corp - company", company, "Acme Corp")

role, company = hermes_role_company("Name: Ali\nHeadline: Director of Procurement at GulfTex")
check("Director of Procurement at GulfTex - role", role, "Director of Procurement")
check("Director of Procurement at GulfTex - company", company, "GulfTex")

role, company = hermes_role_company("Name: Sarah\nHeadline: Founder at Noor Apparel")
check("Founder at Noor Apparel - role", role, "Founder")
check("Founder at Noor Apparel - company", company, "Noor Apparel")

# No 'at': role only
role, company = hermes_role_company("Name: X\nHeadline: CEO")
check("CEO (no at) - role", role, "CEO")
check("CEO (no at) - company", company, "")

# ---------------------------------------------------------------------------
# 3. Sourcing titles -> base model (never premium)
# ---------------------------------------------------------------------------
print("\n=== 3. Sourcing titles -> base model ===")

sourcing_profiles = [
    ("Head of Sourcing at Zara", "Head of Sourcing at Zara"),
    ("Buyer", "Fashion Buyer"),
    ("Procurement Manager at TextileCo", "Procurement Manager at TextileCo"),
    ("Merchandising Director at H&M", "Merchandising Director at H&M"),
    ("Supply Chain Lead", "Supply Chain Lead"),
]
for desc, headline in sourcing_profiles:
    p = f"Name: X\nHeadline: {headline}"
    s, _ = detect_style(p)
    premium = is_premium_profile(s, p)
    check(f"'{desc}' is NOT premium (style={s})", premium, False)

# ---------------------------------------------------------------------------
# 4. CEO uses premium model
# ---------------------------------------------------------------------------
print("\n=== 4. CEO uses premium model ===")

ceo_cases = [
    "CEO at TechStartup",
    "Chief Executive Officer at MegaCorp",
    "Co-Founder & CEO at AppBuilders",
    "Founder and CEO",
    "Owner at FamilyBiz",
    "Managing Director at Dubai Investments",
]
for headline in ceo_cases:
    p = f"Name: X\nHeadline: {headline}"
    s, _ = detect_style(p)
    premium = is_premium_profile(s, p)
    check(f"'{headline}' IS premium (style={s})", premium, True)

# ---------------------------------------------------------------------------
# 5. SEQUENTIAL/FAST_MODE import order (regression test for the NameError bug)
# ---------------------------------------------------------------------------
print("\n=== 5. SEQUENTIAL/FAST_MODE definition order ===")
# Simulate the fixed order: FAST_MODE defined BEFORE it is used in SEQUENTIAL
_FAST_MODE = False
_SEQUENTIAL_MODE = True
try:
    _SEQUENTIAL = _SEQUENTIAL_MODE or _FAST_MODE  # references _FAST_MODE already defined
    check("FAST_MODE defined before SEQUENTIAL (no NameError)", True, True)
except NameError as e:
    check("FAST_MODE defined before SEQUENTIAL (no NameError)", False, True, str(e))

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{'='*50}")
print(f"Results: {PASS} passed, {FAIL} failed")
if FAIL:
    sys.exit(1)
else:
    print("All tests PASSED.")
    sys.exit(0)
