"""AI Brain = cognition only (MVP).

Consumes message.received, runs a tiny LangGraph (classify -> retrieve_memory(stub)
-> generate_draft via Ollama/Qwen2.5), and publishes draft.generated. It has NO
LinkedIn route, mints no tokens, and cannot send or approve anything. Its output
is DRAFT TEXT (data), never an action.
"""
import os
import re
import time
import json
import uuid
import requests
import redis

REDIS_ADDR = os.getenv("REDIS_ADDR", "localhost:6379")
OLLAMA = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# ---------- DUAL-MODEL STRATEGY (low-RAM-safe, 2026-06-07) ----------
# BASE model: the production default, used for messaging replies and for general /
# recruiter / buyer / sourcing outreach. On an 8GB CPU-only box this is
# qwen2.5:3b (~2.4GB loaded, ~1.4x faster than the 4B) — the stable choice.
# PREMIUM model: used ONLY for high-value profiles (founder / CEO / director /
# owner / VP) via smart routing, and ONLY when free RAM allows. With ollama
# OLLAMA_MAX_LOADED_MODELS=1 the box holds ONE model at a time; switching evicts
# the other, so we never hold two at once. Under RAM pressure the brain always
# falls back to BASE (see ram_pressure / model_for).
MODEL = os.getenv("MODEL", "qwen2.5:3b")
BASE_MODEL = MODEL
PREMIUM_MODEL = os.getenv("PREMIUM_MODEL", "qwen3:4b-instruct-2507-q4_K_M")
PREMIUM_ROUTING = os.getenv("PREMIUM_ROUTING", "true").strip().lower() in ("1", "true", "yes", "on")
# Host MemAvailable (MB) below which the brain enters DEFENSIVE MODE: force the
# base model, best-of-1, unload the premium model, and pause the queue briefly.
# 0 disables the watchdog.
RAM_MIN_FREE_MB = int(os.getenv("RAM_MIN_FREE_MB", "1200") or 0)
# Seconds to pause between drafts when under RAM pressure (lets the kernel reclaim
# memory and prevents a burst from tipping the box into OOM).
QUEUE_THROTTLE_SEC = float(os.getenv("QUEUE_THROTTLE_SEC", "2") or 0)

# SEQUENTIAL_MODE: strict one-profile-at-a-time generation for low-CPU systems.
# When on, the consumer pulls and FULLY processes a single message (draft ->
# grounding validation -> publish result) before reading the next, so profile #2
# never starts until profile #1 is complete. There are never two Ollama
# generations in flight. Default ON. FAST_MODE (the CPU-inference proxy on this
# stack) FORCES it on regardless — on CPU we never want concurrent generations.
SEQUENTIAL_MODE = os.getenv("SEQUENTIAL_MODE", "true").strip().lower() in ("1", "true", "yes", "on")
SEQUENTIAL = SEQUENTIAL_MODE or FAST_MODE
# How many stream entries to buffer per read. 1 = strict sequential (pull one,
# finish it, then pull the next). >1 only buffers locally — processing is serial
# either way (ollama is pinned to NUM_PARALLEL=1) — but 1 makes the guarantee exact.
READ_COUNT = 1 if SEQUENTIAL else 5

# FAST_MODE: low-resource CPU profile (e.g. HP EliteBook 840 G1, 2C/4T Haswell).
# Shrinks best-of-N, skips length-only regeneration, caps generated tokens, and
# fast-paths every style except `founder`. Generation stays SEQUENTIAL (one draft
# at a time — the consumer loop is already serial and ollama is pinned to
# NUM_PARALLEL=1 in docker-compose), so the CPU isn't oversubscribed. Quality
# guarantees are UNCHANGED: hallucinated/spammy drafts still fall back to the
# deterministic grounded note. Default off → no behavior change for other boxes.
FAST_MODE = os.getenv("FAST_MODE", "false").strip().lower() in ("1", "true", "yes", "on")

# MODE: vertical specialization of the outreach voice. "general" (default) keeps
# the founder-to-founder networking voice. "fashion" switches to a B2B apparel /
# fashion SOURCING voice (UAE manufacturing, garments, textiles, buyers, private
# label, supply chain) — see OUTREACH_SYSTEM_FASHION. The grounding gate also
# learns the fashion industry's generic framing vocabulary in this mode (so
# "apparel sourcing" isn't mistaken for a hallucination) while specific invented
# claims (brand names, sustainability, retail categories) are STILL caught.
MODE = os.getenv("MODE", "general").strip().lower()
IS_FASHION = MODE == "fashion"

STREAM_INGEST = "stream:message_ingest"
STREAM_RESULT = "stream:cognition_result"
GROUP = "cg:ai-brain"

host, port = REDIS_ADDR.split(":")
r = redis.Redis(host=host, port=int(port), decode_responses=True)

SYSTEM = (
    "You are a helpful assistant that drafts a SHORT, warm, professional LinkedIn "
    "reply to the latest inbound message. Output ONLY the reply text — no preamble, "
    "no quotes, no explanation. Keep it under 60 words."
)

# Phase 1: AI Outreach. World-class connection-request writer. Output is DRAFT
# TEXT only (data) — it is never sent; a human still clicks Connect manually.
OUTREACH_SYSTEM = (
    "You write premium, personalized LinkedIn connection notes in the voice of a "
    "sharp founder reaching out to a peer. Every note feels hand-written by someone "
    "who actually read the profile — never templated, never mass-produced.\n\n"
    "ABSOLUTE GROUNDING RULE (the most important rule — breaking it ruins the note):\n"
    "- You may reference ONLY facts that literally appear in the profile text you are "
    "given (their name, headline, company, role, location, about). NOTHING ELSE.\n"
    "- Do NOT name or imply any industry, domain, product, market, technology, "
    "specialty, or activity unless that exact word appears in the profile. If their "
    "headline is just a role and company, write about THAT role at THAT company — do "
    "not guess what the company does. Inventing a plausible detail is a FAILURE.\n"
    "- When the profile is thin (e.g. only a name + a short title), keep the note "
    "about their role/title and a genuine, generic reason to connect as a peer. A "
    "short TRUE note beats a specific FALSE one. Never fabricate to sound specific.\n"
    "- This note is for ONE specific person. Use only THEIR facts. Never carry over a "
    "detail, company, or industry that belongs to anyone else.\n\n"
    "OTHER HARD RULES:\n"
    "- Address the person by the EXACT name you are given. NEVER invent, guess, or "
    "substitute a different name.\n"
    "- Length: 120-180 characters. One or two crisp sentences. Never exceed 180.\n"
    "- Voice: founder-to-founder networking. Warm, confident, a peer — not a "
    "salesperson, not a fan, not a recruiter blasting a list.\n"
    "- NEVER mention mutual connections, shared connections, or 'we both know' — you "
    "do not have a verified mutual name, so never imply one.\n"
    "- BANNED phrases (do not use these or close variants): 'noticed your', "
    "'noticed you', 'I came across', 'came across your profile', 'would love to "
    "connect', 'love to connect', 'let's connect', 'reaching out', 'your work on', "
    "'great work', 'impressive work', 'I hope you're doing well', 'hope this finds "
    "you'. Open and close with FRESH, varied wording every time.\n"
    "- No buzzwords, no flattery padding, no exclamation spam, no emoji.\n\n"
    "The two examples below show ONLY the LENGTH and TONE to imitate. They are about "
    "OTHER people — do NOT reuse their names, companies, industries, or any detail "
    "(apparel, GCC, sourcing, SaaS, Cloudify, go-to-market). Those facts are not "
    "yours to use:\n"
    "  \"Hi Saima, the way you've positioned apparel brands for the GCC market is "
    "sharp — I'm building in B2B sourcing nearby and would value trading notes.\"\n"
    "  \"Hi Rohan, scaling B2B SaaS sales at Cloudify is no small feat. I'm in the "
    "same arena and think there's plenty we could compare on go-to-market.\"\n\n"
    "Output ONLY the connection message text — no preamble, no quotes, no subject "
    "line, no sign-off."
)

# FAST_MODE system prompt: ~5x shorter than OUTREACH_SYSTEM (no few-shot examples,
# trimmed rules). On a CPU-only box the big prompt's PREFILL dominates per-draft
# latency; this keeps the load-bearing rules (ground-only-in-profile, exact name,
# length, no-spam) while cutting prefill tokens dramatically. The deterministic
# grounding validator + fallback still backstop quality, so trimming the prompt
# trades a little polish for a large speedup — never grounding safety.
OUTREACH_SYSTEM_FAST = (
    "Write ONE short LinkedIn connection note (120-180 characters) to the named "
    "person, in a warm peer-to-peer founder voice.\n"
    "RULES: Reference ONLY facts that literally appear in their profile (role, "
    "company, headline words). Invent NOTHING — no industry/product/specialty not "
    "written there; if the profile is thin, just speak to their role. Address them "
    "by the EXACT name given. No mutual-connection mentions. Avoid: 'noticed your', "
    "'came across', 'would love to connect', 'reaching out', 'great work', 'hope "
    "you're doing well'. Output ONLY the note — no quotes, no preamble."
)

# ---------- FASHION / APPAREL SOURCING vertical (MODE=fashion) ----------
# A B2B apparel sourcing voice for UAE/GCC fashion manufacturing: addressing
# fashion founders, brand owners, buyers, purchasing/sourcing/merchandising
# leads. Grounds ONLY in real profile facts; never invents a brand, a retail
# category, sourcing experience, or sustainability claims.
OUTREACH_SYSTEM_FASHION = (
    "You write premium B2B LinkedIn connection notes in the voice of someone "
    "working in APPAREL SOURCING and fashion MANUFACTURING (UAE/GCC — garments, "
    "textiles, private label, supply chain) reaching out to a fashion peer: a "
    "founder, brand owner, buyer, or sourcing/purchasing/merchandising lead. Every "
    "note reads like a real sourcing conversation, never generic networking.\n\n"
    "ABSOLUTE GROUNDING RULE (most important — breaking it ruins the note):\n"
    "- Reference ONLY facts that literally appear in their profile (their name, "
    "headline, company, fashion role, and any sourcing/apparel terms they wrote). "
    "NOTHING else.\n"
    "- NEVER invent a fashion brand name, a retail category, a product line, a "
    "specific sourcing/manufacturing experience, certifications, MOQs, factories, "
    "or any sustainability / ethical / organic claim. If it is not in the profile, "
    "do not say it. A short TRUE note beats a specific FALSE one.\n"
    "- You MAY use the shared INDUSTRY framing (apparel sourcing, private label "
    "manufacturing, fashion supply chain, textile sourcing, production "
    "partnerships, buyer-manufacturer collaboration, scaling apparel sourcing) as "
    "YOUR side of the conversation — but only tie it to THEIR work via a fact that "
    "is actually written in their profile.\n\n"
    "VOICE & RULES:\n"
    "- B2B apparel sourcing / partnership / manufacturing tone. A peer in the "
    "supply chain, not a fan, not a recruiter, not a generic networker.\n"
    "- Address them by the EXACT name given. 120-180 characters. One or two crisp "
    "sentences. Output ONLY the note text.\n"
    "- NEVER mention mutual connections.\n"
    "- BANNED (do not use these or close variants): 'would love to connect', "
    "'love to connect', 'let's connect', 'your profile aligns', 'aligns with my "
    "interests', 'aligns with your', 'noticed your', 'came across', 'reaching "
    "out', 'great work', 'hope you're doing well', 'expand my network'. No generic "
    "networking filler, no flattery, no emoji.\n"
    "GOOD directions to draw on (only when grounded): apparel sourcing in the GCC, "
    "private label manufacturing, fashion supply chain, textile sourcing, "
    "production partnerships, buyer-manufacturer collaboration, scaling apparel "
    "sourcing.\n"
    "Output ONLY the connection message text — no preamble, no quotes, no sign-off."
)

# Compact fashion prompt for FAST_MODE (far fewer prefill tokens on CPU).
OUTREACH_SYSTEM_FASHION_FAST = (
    "Write ONE short LinkedIn connection note (120-180 chars) to the named fashion "
    "person (founder / brand owner / buyer / sourcing or purchasing lead), in a "
    "B2B APPAREL SOURCING voice (garments, textiles, private label, fashion supply "
    "chain, production partnerships).\n"
    "RULES: Reference ONLY facts literally in their profile (role, company, "
    "headline, sourcing terms they wrote). Invent NOTHING — no brand name, retail "
    "category, sourcing experience, or sustainability claim. Use industry framing "
    "(apparel sourcing / private label / supply chain) as YOUR side, tied to a "
    "REAL profile fact. Exact name. No mutual connections. Avoid 'would love to "
    "connect', 'your profile aligns', 'reaching out', 'great work'. Output ONLY "
    "the note."
)

# Generic / spammy phrasing we refuse to ship. Used to score drafts and trigger
# one regeneration with a stronger nudge (the small model leans on these).
BANNED_PHRASES = (
    "your profile aligns", "profile aligns with", "aligns with my interests",
    "aligns with your", "expand my network", "grow my network",
    "noticed your", "noticed you", "i came across", "came across your",
    "would love to connect", "love to connect", "let's connect", "lets connect",
    "reaching out", "your work on", "great work", "impressive work",
    "hope you're doing well", "hope you are doing well", "hope this finds you",
    "i'd love to connect", "id love to connect",
)


def banned_hits(text: str):
    t = (text or "").lower()
    return [p for p in BANNED_PHRASES if p in t]


# DIVERSITY: generic CTA filler that became repetitive across the queue (distinct
# from BANNED_PHRASES, which are spam). The model is steered off these, they are
# penalized in scoring, and the deterministic fallback never uses them. NB: we do
# NOT ban "stood out"/"caught my eye" — those are fine openers (they appear in the
# target good examples); the problem was the bland "adjacent space / compare notes"
# closer repeating verbatim everywhere.
OVERUSED_PHRASES = (
    "adjacent space", "compare notes", "comparing notes", "trade notes",
    "trading notes", "in a similar space", "in the same space", "connect as peers",
    "swap ideas", "swapping ideas", "in the space", "keen to connect",
)


def overused_hits(text: str):
    t = (text or "").lower()
    return [p for p in OVERUSED_PHRASES if p in t]


def _stable_hash(s: str) -> int:
    """Deterministic per-string hash (Python's hash() is salted per-process).
    Used to pick an opener angle / fallback template so the SAME profile is stable
    but DIFFERENT profiles in a queue vary — cross-queue diversity with no shared
    state between independent per-profile generations."""
    h = 0
    for ch in (s or ""):
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h


# Rotated opening angles — each profile gets one (by stable hash), so drafts
# across a queue don't all open the same way. All are GROUNDED instructions.
OPENER_ANGLES = (
    "Open by naming their specific role and company from the profile.",
    "Open with the concrete focus or skill stated in their headline or about.",
    "Open with a short, genuine question about their work — grounded in the profile.",
    "Open by referencing the exact industry/domain phrase that appears in their profile.",
    "Open with what specifically in their background is worth connecting over.",
)


def personalization_score(used) -> float:
    """0..1 from how many real profile facts the note actually references."""
    n = len(used)
    return 1.0 if n >= 2 else (0.7 if n == 1 else 0.0)


def strip_mutual(profile_text: str) -> str:
    """Drop any 'Mutual: ...' line before the model sees the profile. We only ever
    have a COUNT (names are stripped at capture), and the rule is to never imply a
    mutual connection — so the safest move is to not surface it to the model."""
    return "\n".join(
        ln for ln in (profile_text or "").splitlines()
        if not re.match(r"\s*mutual\s*:", ln, re.I)
    )

# Supported outreach styles; the model picks the best fit from profile context.
OUTREACH_STYLES = (
    "networking", "recruiter", "partnership", "founder", "sales", "hiring", "warm_intro",
    "sourcing",  # fashion/apparel B2B sourcing (MODE=fashion)
)


# ---------- Qwen3 compatibility shim ----------
# Qwen3 comes in two flavours that need different handling:
#   * THINKING/hybrid models ("qwen3:4b", "qwen3:8b") reason before answering.
#     On this stack (ollama 0.30) that reasoning is NOT reliably suppressible —
#     neither `think:false` nor the `/no_think` soft switch stops it consistently,
#     and it balloons to hundreds/thousands of tokens (minutes of CPU latency) and
#     corrupts the 120-180 length scoring + grounding_check. We therefore (a) ask
#     for non-thinking output, (b) HARD-CAP the token budget so a runaway can't
#     hang the pipeline, and (c) strip any <think> block defensively. Even so,
#     thinking models are a poor fit for this low-latency workload (see CLAUDE.md).
#   * INSTRUCT models ("qwen3:4b-instruct-2507") and Qwen2.5 are pure non-thinking
#     instruction models — they need NO special handling and take the clean path,
#     so the recommended default AND the qwen2.5:3b rollback are both untouched.
# Thinking-ness is now per-MODEL (we may switch between a base and a premium model
# at runtime), so it is computed from whichever model a given call uses — never a
# single module global. A qwen3 tag that is NOT an "instruct" variant is a hybrid
# thinking model and needs the /no_think + token-cap + <think>-strip treatment;
# instruct + qwen2.5 take the clean path.
def is_thinking_model(model: str) -> bool:
    m = (model or "").lower()
    return m.startswith("qwen3") and "instruct" not in m


def think_cap(model: str) -> int:
    # Safety cap on generated tokens for thinking models (a note is < 80 tokens;
    # this only ever bites a runaway reasoning loop). 0 = uncapped.
    return 220 if is_thinking_model(model) else 0


_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


# ---------- RAM watchdog (OOM prevention) ----------
def host_free_mb() -> int:
    """Best-effort host MemAvailable in MB. /proc/meminfo inside a container shows
    HOST memory, which is the signal that matters here: ollama's model load is
    host-wide and invisible to ai-brain's own cgroup. On a read error we report a
    huge value so a probe glitch never forces defensive mode spuriously."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024  # kB -> MB
    except Exception:
        pass
    return 1 << 20  # unknown -> assume plenty


def ram_pressure() -> bool:
    """True when free host RAM is below the safety threshold (watchdog disabled if
    RAM_MIN_FREE_MB <= 0)."""
    if RAM_MIN_FREE_MB <= 0:
        return False
    return host_free_mb() < RAM_MIN_FREE_MB


# ---------- smart model routing ----------
# Premium (founder/CEO/director/owner/VP) profiles get the stronger PREMIUM_MODEL;
# everything else — and EVERYTHING under RAM pressure — uses the lighter base model.
PREMIUM_STYLES = ("founder",)
# Styles that must ALWAYS stay on the base model regardless of title keywords
# (a "Sourcing Director" is a buyer, not a premium founder lead).
NON_PREMIUM_STYLES = ("recruiter", "sourcing", "sales")
# Procurement-side roles that ALWAYS use the base model even when the headline
# carries a senior title (e.g. "Director of Sourcing", "Head of Merchandising").
# This enforces the routing rule "buyer / sourcing / merchandising -> base model".
BASE_FORCE_KW = (
    "buyer", "buying", "sourcing", "merchandis", "procure", "procurement",
    "purchasing", "supply chain",
)
# Senior-leadership title cues that promote an otherwise-general profile to premium.
GENERAL_PREMIUM_KW = (
    "ceo", "chief executive", "chief ", "founder", "co-found", "cofound", "owner",
    "managing director", "president", "vp ", "vice president", "head of", " director",
)


def is_premium_profile(style: str, profile_text: str) -> bool:
    t = (profile_text or "").lower()
    # Buyer / sourcing / merchandising / procurement -> base, ALWAYS (a senior
    # title like "Director of Sourcing" does not make it a premium founder lead).
    if style in NON_PREMIUM_STYLES or any(k in t for k in BASE_FORCE_KW):
        return False
    # Founders/CEOs/directors/owners/VPs -> premium.
    if style in PREMIUM_STYLES:
        return True
    return any(k in t for k in GENERAL_PREMIUM_KW)


def model_for(style: str, profile_text: str):
    """Pick the model for one draft. Returns (model, premium_used, defensive).
    Defensive (RAM pressure) ALWAYS wins -> base model. Otherwise premium profiles
    use PREMIUM_MODEL when routing is enabled; all others use the base model."""
    if ram_pressure():
        return BASE_MODEL, False, True
    if PREMIUM_ROUTING and is_premium_profile(style, profile_text):
        return PREMIUM_MODEL, True, False
    return BASE_MODEL, False, False


def strip_thinking(text: str) -> str:
    """Remove any <think>...</think> reasoning block (and a dangling open tag, if
    the model was cut off mid-think) so only the final answer remains. No-op for
    non-thinking models, so it is safe to run unconditionally."""
    text = _THINK_RE.sub("", text or "")
    if "</think>" in text:  # closing tag with the opener already stripped/cut
        text = text.split("</think>")[-1]
    return text.strip()


# ---------- Ollama inference (with a direct-HTTP fallback) ----------
# Track the model ollama currently holds so we can explicitly evict it BEFORE
# loading a different (possibly larger) one — with MAX_LOADED_MODELS=1 ollama
# would evict anyway, but doing it first frees the RAM before the new load, which
# matters on an 8GB box.
_last_model = None


def ollama_unload(model: str):
    """Evict a model from ollama's RAM immediately (keep_alive=0). Best-effort —
    used to free the premium model under RAM pressure or when switching models.
    Never raises."""
    global _last_model
    if not model:
        return
    try:
        requests.post(f"{OLLAMA}/api/generate",
                      json={"model": model, "prompt": "", "keep_alive": 0}, timeout=20)
        if _last_model == model:
            _last_model = None
        print(f"[ai-brain] unloaded model {model} (freeing RAM)", flush=True)
    except Exception as e:
        print(f"[ai-brain] unload {model} failed (ignored): {e}", flush=True)


def ollama_generate(prompt: str, system: str = SYSTEM, temperature: float = 0.4,
                    num_predict: int = 0, model: str = None) -> str:
    """Primary path: LangChain ChatOllama. Fallback: raw /api/generate. Either way
    the model is a local Qwen served by Ollama — no cloud LLM. The model is now a
    per-call argument (default = base model) so smart routing can pick premium vs
    base. Thinking Qwen3 models get /no_think + a token cap + <think> stripping;
    instruct/Qwen2.5 take the clean path. `num_predict` caps generated tokens
    (caller-supplied in FAST_MODE), falling back to the Qwen3-thinking safety cap."""
    global _last_model
    model = model or BASE_MODEL
    thinking = is_thinking_model(model)
    sys_prompt = system + ("\n/no_think" if thinking else "")
    np = num_predict or think_cap(model) or 0
    # Free the previous model BEFORE loading a different one (lower peak RAM).
    if _last_model and _last_model != model:
        ollama_unload(_last_model)
    _last_model = model
    try:
        from langchain_ollama import ChatOllama
        kw = {"model": model, "base_url": OLLAMA, "temperature": temperature}
        if np:
            kw["num_predict"] = np
        llm = ChatOllama(**kw)
        return strip_thinking(llm.invoke(f"{sys_prompt}\n\n{prompt}").content.strip())
    except Exception as e:  # lib/version issues must never break the demo
        print(f"[ai-brain] ChatOllama unavailable ({e}); using HTTP fallback", flush=True)
        body = {"model": model, "prompt": f"{sys_prompt}\n\n{prompt}", "stream": False}
        if thinking:
            body["think"] = False
        if np:
            body["options"] = {"num_predict": np}
        resp = requests.post(f"{OLLAMA}/api/generate", json=body, timeout=300)
        resp.raise_for_status()
        return strip_thinking(resp.json().get("response", "").strip())


def generate_with_fallback(prompt: str, system: str, temperature: float,
                           num_predict: int, model: str) -> str:
    """Generate on `model`, but if it fails for ANY reason (not pulled, load error,
    OOM during load, timeout) transparently retry on the base model so a draft is
    always produced and the ai-brain never crashes. Returns "" only if both fail."""
    try:
        return ollama_generate(prompt, system=system, temperature=temperature,
                               num_predict=num_predict, model=model)
    except Exception as e:
        if model != BASE_MODEL:
            print(f"[ai-brain] model {model} failed ({e}); falling back to {BASE_MODEL}", flush=True)
            ollama_unload(model)
            try:
                return ollama_generate(prompt, system=system, temperature=temperature,
                                       num_predict=num_predict, model=BASE_MODEL)
            except Exception as e2:
                print(f"[ai-brain] base model {BASE_MODEL} also failed ({e2})", flush=True)
                return ""
        print(f"[ai-brain] generation failed on base model ({e})", flush=True)
        return ""


# ---------- LangGraph cognition graph ----------
def classify_intent(state: dict) -> dict:
    t = state["text"].lower()
    state["intent"] = "meeting" if any(k in t for k in ("meet", "call", "thursday", "available", "free")) else "general"
    return state


def retrieve_memory(state: dict) -> dict:
    # STUB memory (read-only). Real impl is a vector query (see node-tool-allowlist).
    state["memory"] = "Prefers concise, friendly replies."
    return state


def generate_draft(state: dict) -> dict:
    prompt = (
        f"Conversation so far:\n{state['text']}\n\n"
        f"Detected intent: {state['intent']}\n"
        f"Notes about me: {state['memory']}\n\n"
        f"Draft my reply:"
    )
    state["draft"] = ollama_generate(prompt, num_predict=150 if FAST_MODE else 0) \
        or "Thanks for the message — happy to chat. When works for you?"
    return state


def build_graph():
    try:
        from langgraph.graph import StateGraph, START, END
        g = StateGraph(dict)
        g.add_node("classify", classify_intent)
        g.add_node("retrieve", retrieve_memory)
        g.add_node("generate", generate_draft)
        g.add_edge(START, "classify")
        g.add_edge("classify", "retrieve")
        g.add_edge("retrieve", "generate")
        g.add_edge("generate", END)
        return g.compile()
    except Exception as e:
        print(f"[ai-brain] LangGraph unavailable ({e}); running nodes inline", flush=True)
        class _Inline:
            def invoke(self, s):
                return generate_draft(retrieve_memory(classify_intent(s)))
        return _Inline()


GRAPH = build_graph()


# ---------- Phase 1: AI Outreach cognition ----------
# ---------- role / style keyword sets (priority-ordered) ----------
# Priority: Founder/CEO > Recruiter/Talent/Advisory > Buyer/Sourcing > secondary
# (hiring/partnership/sales) > apparel-without-function > generic networking.
FOUNDER_KW = (
    "founder", "co-founder", "cofounder", "co founder", "founding partner",
    "ceo", "chief executive", "chief ", "owner", "co-owner", "managing director",
    "managing partner", "president", "chairman", "chairwoman", "entrepreneur",
)
# Recruiter / talent / advisory / staffing / people roles -> `recruiter` style.
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
# HARD never-sourcing guard: any of these in the profile FORBIDS the sourcing
# style/voice (a founder or a talent/advisory/recruiting person is never an
# apparel-sourcing lead, even in fashion mode, even with a "sourcing" word nearby).
NEVER_SOURCING_KW = FOUNDER_KW + TALENT_KW + ("advisory", "advisor", "consulting", "consultant")


def _blocks_sourcing(t: str) -> bool:
    return any(k in t for k in NEVER_SOURCING_KW)


def is_apparel_profile(profile_text: str) -> bool:
    """True when the profile actually carries apparel/fashion/sourcing signal — used
    to decide whether to apply the fashion (apparel-sourcing) VOICE to this one
    profile. A non-apparel profile (e.g. a talent-advisory founder) never gets the
    apparel voice even in MODE=fashion."""
    t = " " + (profile_text or "").lower() + " "
    return any(k in t for k in APPAREL_KW + SOURCING_KW)


def detect_style(profile_text: str):
    """Priority-ordered, deterministic style auto-detect. Returns
    (style, confidence) where confidence ∈ {high, medium, low}.

    Priority (highest first): Founder/CEO > Recruiter/Talent/Advisory >
    Buyer/Sourcing > Hiring/Partnership/Sales > apparel-role-without-function >
    generic networking. A profile carrying a founder OR talent/advisory/recruiting
    signal is NEVER routed to `sourcing` (the reported Lara-Matibaeva bug:
    "Founder ... | Talent Advisory" must be founder, never apparel sourcing).
    The fashion checkbox does NOT blindly force sourcing — only genuine
    buyer/sourcing or apparel roles get it; everything else keeps its true style."""
    t = " " + (profile_text or "").lower() + " "
    blocks = _blocks_sourcing(t)
    # 1) Founder / C-level (highest priority — wins even alongside "talent advisory").
    if any(k in t for k in FOUNDER_KW):
        return "founder", "high"
    # 2) Recruiter / talent / advisory / staffing / people.
    if any(k in t for k in TALENT_KW):
        return "recruiter", "high"
    # 3) Buyer / sourcing / merchandising / procurement — ONLY if not hard-blocked.
    if any(k in t for k in SOURCING_KW) and not blocks:
        return "sourcing", "high"
    # 4) General-mode secondary signals.
    if any(k in t for k in HIRING_KW):
        return "hiring", "medium"
    if any(k in t for k in PARTNERSHIP_KW):
        return "partnership", "medium"
    if any(k in t for k in SALES_KW):
        return "sales", "medium"
    # 5) Apparel role with no explicit function -> sourcing tier, but ONLY in fashion
    #    mode, ONLY when actually apparel, and NEVER when hard-blocked.
    if IS_FASHION and any(k in t for k in APPAREL_KW) and not blocks:
        return "sourcing", "medium"
    # 6) Generic — peer networking. We do NOT blindly force sourcing (low confidence
    #    so the UI flags that detection was a fallback, not a confident match).
    return "networking", "low"


STYLE_GUIDANCE = {
    "networking": "Peer-to-peer networking. Find genuine common ground; ask to connect, not to sell.",
    "recruiter": "They work in talent / recruiting / advisory / people. Connect as a peer who respects "
                 "that space — acknowledge their talent/advisory focus from the profile. NO candidate "
                 "pitch, NO sales, no apparel-sourcing talk.",
    "partnership": "Explore a potential partnership. Lead with shared value, keep it exploratory.",
    "founder": "Founder-to-founder or founder-to-prospect. Be direct, curious, and concrete.",
    "sales": "A soft intro. NO pitch in the connect note — lead with a specific detail from their profile.",
    "hiring": "You may have an opportunity for them. Lead with their strengths, low-pressure.",
    "warm_intro": "Shared professional context. Lead with the overlap in your fields — never imply a mutual connection.",
    "sourcing": "B2B apparel sourcing. You are a manufacturing/sourcing peer — lead with their fashion role or "
                "company and a concrete sourcing/production angle (private label, supply chain, buyer-manufacturer). "
                "No pitch, no generic networking.",
}


def first_name(name: str) -> str:
    """Greeting name: first token of the target name, lightly cleaned."""
    name = (name or "").strip()
    if not name:
        return ""
    # Drop emojis/pronouns/credential suffixes after a separator.
    name = re.split(r"[|,(•\-/]", name)[0].strip()
    tok = name.split()
    return tok[0] if tok else ""


def force_correct_greeting(note: str, first: str) -> str:
    """Deterministic backstop: guarantee the note greets the RIGHT person.
    The small model sometimes slips a wrong name into the opener; this rewrites
    only the greeting, never the body, and never invents a name we don't have."""
    if not first:
        return note
    note = note.strip()
    if re.match(r"^(hi|hello|hey)\b", note, re.I):
        # Case 1: greeting ... up to the first comma -> rewrite through the comma.
        new, n = re.subn(r"^(hi|hello|hey)\b[^\n,]*,", f"Hi {first},", note, count=1, flags=re.I)
        if n:
            return new
        # Case 2: greeting + a name token but no comma -> rewrite greeting+token.
        return re.sub(r"^(hi|hello|hey)\b[ \t]+\S+", f"Hi {first}", note, count=1, flags=re.I)
    # No greeting at all -> prepend one.
    return f"Hi {first}, {note[0].lower()}{note[1:]}" if note else f"Hi {first},"


def clean_note(note: str, fname: str) -> str:
    """Tidy a raw model draft: strip quotes/labels, then force the right greeting."""
    note = (note or "").strip().strip('"').strip("'").strip()
    note = re.sub(r"^(message|note|connection request)\s*:\s*", "", note, flags=re.I)
    return force_correct_greeting(note, fname)


def enforce_length(note: str, hi: int = 180) -> str:
    """Deterministically bring an over-long note into the target band WITHOUT a
    second generation (respects the best-of-1 fast path). Trims to the last clean
    boundary <= hi: prefer a sentence end, then an em-dash/clause, then a word.
    Never mangles a word. Used in fashion/FAST tiers where length isn't regen'd."""
    note = (note or "").strip()
    if len(note) <= hi:
        return note
    window = note[:hi]
    out = None
    # 1) last sentence terminator
    end = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if end >= 80:
        out = note[:end + 1].strip()
    else:
        # 2) last clause boundary (em-dash / comma) -> close with a period
        clause = max(window.rfind(" — "), window.rfind("—"), window.rfind(", "))
        if clause >= 80:
            out = note[:clause].strip().rstrip(",—-– ").strip() + "."
        else:
            # 3) last word boundary
            sp = window.rfind(" ")
            out = (note[:sp].strip().rstrip(",—-– ") + ".") if sp >= 80 else window
    # Cleanup: if we landed on a DANGLING short em-dash fragment (e.g.
    # "…efficiency—key for scalable."), drop that fragment back to the dash —
    # a truncated tail reads as broken. Only when the remainder stays >= 100.
    m = re.search(r"\s*[—–-]\s*[^—–-]{1,28}\.?$", out)
    if m and (m.start() >= 100):
        out = out[:m.start()].rstrip(",—-– ").strip() + "."
    return out


# ---------- HARD GROUNDING VALIDATOR ----------
# Generic English + networking connective vocabulary. Words here are never
# treated as "claims" — they carry no industry/domain assertion, so the model is
# free to use them. Anything significant NOT in here and NOT in the profile is a
# potential hallucinated fact. Deliberately contains NO domain nouns (no
# "financial", "tech", "sales", "design", …) — those must come from the profile.
# NOTE: contains generic English descriptors/verbs/adjectives but DELIBERATELY no
# industry/domain nouns (no "financial", "tech", "design", "marketing", …). The
# remaining un-whitelisted, un-grounded significant words are therefore the
# domain/industry CLAIMS we want to catch as hallucinations.
SAFE_VOCAB = frozenset("""
hi hello hey there here connect connecting connection note network networking reach
reaching trade trading swap share sharing compare comparing notes thought thoughts idea
ideas insight insights perspective perspectives space arena field world side area scene
adjacent nearby similar same peer peers fellow founder founders building build built work
working works journey path scale scaling grow growth team teams keen curious excited glad
happy value valuable worth chat talk speak speaking conversation love like enjoy about
around really genuinely truly always often lately recently currently would could should
might want wanting hope thinking think believe sense make making made something anything
someone people person professional professionals approach approaches across between among
more most much many few little lot good great strong sharp clear smart bright mind minds
learn learning question questions early stage stages doing done getting given taking
based role focus focused experience background story stuff things mutual respect
admire follow following reason reasons line lines time times into onto with from that
this these those what when where while because since though although appreciate impressed
inspired drawn intrigued resonate resonates aligned overlap common ground meet meeting
introduction intro hand
lead leading leads led head heading chief officer manager managing management director
senior junior staff level levels remit role roles title position seat helm running run
runs ran drive driven driving help helping helped support supporting based set sets
setting deep broad wide long short high low fast quick big large bigger small smaller new
newer current recent latest ongoing real solid serious genuine honest open over under
within behind beyond above next move moving moved grew built scaling done well much
exploring explore exploring exploring craft crafting shaping shape shaped lens angle
remit space space lane lane orbit circle community circles seasoned veteran builder
operator leader peer counterpart trajectory remit being impressive impressively
notable remarkable evident clear apparent seeing seen looking looked reading read
profile profiles stood stand standing stands timing suits suit suited out while was
were value valued valuing genuinely say saying says way ways bit kind sort type drop
dropping drops note noting noted line note way side lane road space room table seat
recently lately today now soon later someday sometime moment moments touch reach
across over here meet say hello note quick brief short word words couple pair handful
both either neither each every any some none one two first second next last other
others another such very quite rather pretty fairly somewhat enough too also still yet
just only even though hence thus therefore likewise similarly equally indeed surely
certainly perhaps maybe possibly probably definitely absolutely truly really
industry industries company companies business businesses growth forward expertise
insight insights leverage leveraging player players challenge challenges mission vision
strategy strategic leadership innovation innovative market markets sector sectors venture
caught catch catches catching eye eyes attention drew draw drawn glad
""".split())

# Fashion / apparel sourcing INDUSTRY framing vocabulary. In MODE=fashion these
# are treated as generic connective vocab (like SAFE_VOCAB), so the writer's own
# "apparel sourcing / private label / supply chain" framing is not mistaken for a
# hallucinated claim. DELIBERATELY contains NO specific-claim words — no
# "sustainable/sustainability/organic/ethical/luxury/certified", no brand names —
# so inventing those about the person is STILL caught by grounding_check.
FASHION_SAFE_VOCAB = frozenset("""
apparel fashion garment garments textile textiles clothing clothes wear wearables
sourcing source sourced procurement purchasing buyer buyers buying merchandising
merchandiser merchandise supply chain supplier suppliers vendor vendors
manufacturing manufacture manufacturer manufacturers production produce factory
factories wholesale retail private label labels brand brands collection
collections fabric fabrics knit woven garmenting stitching sewing cut-and-sew
range ranges line lines product products category categories partner partnering
partnership partnerships collaboration collaborate gcc uae emirates middle-east
sample samples order orders bulk volume volumes catalog catalogue assortment
sku skus seasonal season seasons trims accessories materials material mills mill
import imports export exports logistics fulfilment fulfillment distribution
wholesaler distributor distributors b2b trade trading commerce
operations operational efficiency efficient scalable scale scaling reliable
reliability quality capacity lead leadtime lead-time delivery deliveries execution
execute executing end-to-end region regional network networks process processes
pipeline cost costs margin margins turnaround sourcing-led offer offering offerings
solution solutions service services capabilities capability expertise specialise
specialize specialist specialism focus focused experienced experience scaling
streamline streamlined optimise optimize optimization manage managing management
deliver delivering support supports supporting enable enables enabling drive driving
""".split())

# Fashion SPECIFIC-CLAIM blocklist: words that assert something concrete about the
# person/their brand. In fashion mode these must be GROUNDED in the profile or the
# draft is rejected — this is the hard "never invent a brand, retail category,
# sourcing experience, or sustainability claim" guard the use-case demands. (We do
# NOT gate on generic descriptive words there — sourcing prose is naturally wordy —
# so the gate targets exactly the fabrication categories, not ordinary English.)
FASHION_CLAIM_BLOCKLIST = frozenset("""
sustainability sustainable eco eco-friendly organic ethical ethically recycled
recyclable certified certification gots oeko-tex fairtrade carbon green-certified
luxury premium high-end couture bespoke haute award awards awarded award-winning
leading largest biggest top-tier renowned famous best-selling bestselling iconic
pioneer pioneering revolutionary disruptive world-class flagship signature
""".split())


def claim_inventions(note: str, grounded: set):
    """Blocklisted specific-claim words present in the note but NOT grounded in the
    profile — i.e. fabricated brand/category/sustainability-type claims. The hard
    rejection gate in fashion mode."""
    out = []
    for w in _tokens(note):
        if w in FASHION_CLAIM_BLOCKLIST and not _is_grounded(w, grounded) and w not in out:
            out.append(w)
    return out


# FRAMING vocab is handled SEPARATELY from SAFE_VOCAB (do NOT merge — SAFE_VOCAB
# words are *skipped* by grounding_check, so merging would stop real fashion facts
# in the profile from being credited as "used"). Instead, grounding_check credits
# a fashion word when it IS in the profile (a real fact) and only treats it as
# free framing when it is NOT — so "your garment sourcing" (grounded) counts as a
# fact, while a generic "private label production" framing isn't flagged as
# invented. Specific claim words (sustainability, brands, …) are in NEITHER set,
# so they are still caught as hallucinations.
_FRAMING_VOCAB = FASHION_SAFE_VOCAB if IS_FASHION else frozenset()

# common closed-class words to ignore regardless
_STOPWORDS = frozenset(
    "a an and are as at be been being but by for from has had have i im is it its me my of "
    "on or our so the to up us we you your re ve ll am pm was were".split()
)

_word_re = re.compile(r"[A-Za-z][A-Za-z'&\-]{2,}")


def _tokens(text: str):
    return [w.lower().strip("'&-") for w in _word_re.findall(text or "")]


# Field-label words ("Name:", "Headline:", …) are structure, not profile facts —
# exclude them so they are never treated as grounded content.
_FIELD_LABELS = frozenset(
    "name headline company location about experience mutual title role profile".split()
)


def grounded_set(profile: str, name: str):
    """Every significant token that literally appears in the profile/name. These
    are the ONLY facts a grounded note may reference. Field labels are excluded."""
    g = set()
    for w in _tokens(profile) + _tokens(name):
        if len(w) >= 3 and w not in _FIELD_LABELS:
            g.add(w)
    return g


def _is_grounded(w: str, grounded: set) -> bool:
    if w in grounded:
        return True
    if len(w) >= 4:  # tolerate inflection: brand/brands, design/designer
        for g in grounded:
            if len(g) >= 4 and (w[:4] == g[:4] or w in g or g in w):
                return True
    return False


def grounding_check(note: str, grounded: set, fname: str):
    """Inspect a draft for ungrounded claim-words. Returns
    (used_terms, ungrounded_terms, confidence 0..1). Confidence = fraction of the
    note's significant content-words that are grounded in the profile. A note that
    makes no specific claims (all generic vocabulary) scores 1.0 — generic-but-true
    is acceptable; specific-but-false is not."""
    # Drop the greeting + name so the recipient's name never counts as a claim.
    body = re.sub(r"^(hi|hello|hey)\b[^,\n]*,", "", (note or "").strip(), flags=re.I)
    fl = (fname or "").lower()
    used, ungrounded = [], []
    # ACRONYM bleed guard: short ALL-CAPS tokens (GCC, AWS, SAP, UAE…) sit below
    # the 4-char floor and are a classic few-shot-example bleed. Check them
    # explicitly against the profile, regardless of length.
    for ac in re.findall(r"\b[A-Z]{2,5}\b", body):
        a = ac.lower()
        if a in _STOPWORDS or a in SAFE_VOCAB or a == fl:
            continue
        if a in grounded:
            if a not in used:
                used.append(a)
        elif a in _FRAMING_VOCAB:   # e.g. GCC/UAE industry framing (fashion mode)
            continue
        elif a not in ungrounded:
            ungrounded.append(a)
    for w in _tokens(body):
        if len(w) < 4 or w in _STOPWORDS or w in SAFE_VOCAB or w == fl:
            continue
        if _is_grounded(w, grounded):
            if w not in used:
                used.append(w)
        elif w in _FRAMING_VOCAB:
            # Industry framing word NOT in the profile (e.g. "production",
            # "partnership"): allowed as the writer's own context — neither a
            # credited fact nor a flagged invention.
            continue
        elif w not in ungrounded:
            ungrounded.append(w)
    total = len(used) + len(ungrounded)
    conf = 1.0 if total == 0 else round(len(used) / total, 2)
    return used, ungrounded, conf


# Varied, GROUNDED fallback templates. {h} is a clean fragment of the person's
# real headline, so every one references a real fact (personalization >= 1) and
# none uses the repetitive "adjacent space / compare notes" filler. Rotated by a
# stable hash so different profiles in a queue read differently.
_FALLBACK_TEMPLATES = (
    "Hi {fn}, your work as {h} lines up closely with what I focus on — would be glad to connect.",
    "Hi {fn}, {h} is a sharp focus, and it sits right next to my own work — I'd value connecting.",
    "Hi {fn}, your background in {h} is exactly the kind of work I like to follow — would genuinely value connecting.",
    "Hi {fn}, {h} caught my attention since we work in related areas — would be great to connect.",
    "Hi {fn}, came by your profile and {h} stood out as close to what I do — happy to connect.",
)
# Generic role/section tails when there is no usable headline focus at all.
_FALLBACK_BARE = (
    "Hi {fn}, your background is close to the kind of work I follow — would be glad to connect.",
    "Hi {fn}, we look to be working in related areas — I'd genuinely value connecting.",
    "Hi {fn}, your profile lines up with what I focus on — happy to connect when it suits.",
)

# FASHION-voiced fallbacks (MODE=fashion). Still grounded-by-construction (quote
# the real headline {h}); the framing (apparel sourcing / private label / supply
# chain / production) is the WRITER's side, never a claim about them.
_FALLBACK_TEMPLATES_FASHION = (
    "Hi {fn}, your work as {h} sits right next to my own in apparel sourcing — would value a quick conversation.",
    "Hi {fn}, {h} is exactly the kind of work I partner with on private-label production — happy to connect.",
    "Hi {fn}, I work across fashion supply chain and {h} stood out — would value a sourcing conversation.",
    "Hi {fn}, {h} lines up with the garment sourcing side I focus on — open to a buyer-manufacturer conversation.",
    "Hi {fn}, your background in {h} is close to my apparel production work — would value connecting on sourcing.",
)
_FALLBACK_BARE_FASHION = (
    "Hi {fn}, I work in apparel sourcing and production — your fashion background looks close to what I do, keen to connect.",
    "Hi {fn}, we look to overlap across fashion supply chain — would value a quick sourcing conversation.",
    "Hi {fn}, your fashion role lines up with the garment sourcing I focus on — open to connecting.",
)


def _headline_focus(profile: str) -> str:
    """Pull a clean, distinctive fragment of the headline (or company/role/about)
    to quote in a fallback — the first meaningful clause, separators stripped."""
    head = ""
    for ln in (profile or "").splitlines():
        m = re.match(r"\s*headline\s*:\s*(.+)", ln, re.I)
        if m:
            head = m.group(1).strip()
            break
    if not head:
        for ln in (profile or "").splitlines():
            m = re.match(r"\s*(company|role|title|about)\s*:\s*(.+)", ln, re.I)
            if m:
                head = m.group(2).strip()
                break
    if not head:
        return ""
    # First clause only (drop "| SaaS | B2B" style tails), bounded length.
    clause = re.split(r"\s*[|•·;\n]\s*", head)[0].strip()
    clause = clause if len(clause) <= 60 else clause[:57].rstrip() + "…"
    return clause


def grounded_fallback(fname: str, profile: str, seed: str = "", fashion: bool = None) -> str:
    """Deterministic, GROUNDED-BY-CONSTRUCTION note for when the model can't write
    one without inventing. Quotes a real headline fragment (so it always uses >=1
    fact), varies wording by a stable hash, and never uses the overused filler.
    `fashion` selects the apparel-sourcing-voiced templates; it is a PER-PROFILE
    decision (use_fashion_voice), not the global mode, so a non-apparel profile in
    fashion mode never gets an apparel-sourcing fallback. Defaults to IS_FASHION."""
    if fashion is None:
        fashion = IS_FASHION
    fn = fname or "there"
    h = _headline_focus(profile)
    idx = _stable_hash(seed or fname or h)
    templates = _FALLBACK_TEMPLATES_FASHION if fashion else _FALLBACK_TEMPLATES
    bare = _FALLBACK_BARE_FASHION if fashion else _FALLBACK_BARE
    if h:
        return templates[idx % len(templates)].format(fn=fn, h=h)
    return bare[idx % len(bare)].format(fn=fn)


def score_note(note: str, grounded: set, fname: str):
    """Lower is better. Primary: #banned phrases. Then: #ungrounded (hallucinated)
    terms — this is the grounding gate. Then: distance from the 120-180 band.
    Returns (score_tuple, used_terms, ungrounded_terms, confidence)."""
    used, ungrounded, conf = grounding_check(note, grounded, fname)
    n = len(note)
    if 120 <= n <= 180:
        dist = 0
    elif n < 120:
        dist = 120 - n
    else:
        dist = n - 180
    return (len(banned_hits(note)), len(ungrounded), dist), used, ungrounded, conf


# ---------- per-style generation budget (low-resource CPU tuning) ----------
# attempts  = max best-of-N generations (1 = single shot, no best-of scoring)
# num_predict = token cap per generation (0 = uncapped / Qwen3 safety cap only)
# length_regen = may we spend an extra attempt JUST to hit the 120-180 band?
# A regen for a real CONTENT problem (hallucination / no real fact / spam) is
# ALWAYS allowed when attempts>1 — that protects grounding. FAST_MODE only ever
# trims best-of and the cosmetic length-chasing; it never ships a hallucination
# (the deterministic grounded fallback still catches those).
def outreach_budget(style: str) -> dict:
    # DUAL MODE: founder = higher quality / slower (best-of-2); every other style
    # (recruiter, buyer/sourcing, sales, …) = fast path, best-of-1, sequential.
    # Tiering kicks in under FAST_MODE OR fashion mode (the vertical wants the
    # buyer/sourcing fast path regardless of CPU). Plain general mode keeps the
    # original best-of-3 for everyone.
    tiered = FAST_MODE or IS_FASHION
    if style == "founder":
        if tiered:
            return {"attempts": 2, "num_predict": 175, "length_regen": not FAST_MODE}
        return {"attempts": 3, "num_predict": 0, "length_regen": True}
    if tiered:
        return {"attempts": 1, "num_predict": 120, "length_regen": False}  # fast path
    return {"attempts": 3, "num_predict": 0, "length_regen": True}


# ============================================================================
# HERMES ENGINEERING LAYER — per-person outreach orchestration (deterministic).
# ----------------------------------------------------------------------------
# Hermes is NOT a model and writes NO text. It is a pure-CPU/regex/keyword layer
# that turns a raw LinkedIn profile into a structured spec the Qwen writer grounds
# on. Pipeline (single-person workflow only — no batch logic):
#   raw profile -> hermes_extract() -> structured JSON -> hermes_build_prompt()
#   -> Qwen writer (ollama_generate, UNCHANGED) -> grounding_check (UNCHANGED) -> UI
# Responsibilities: profile understanding, style routing, context extraction,
# prompt engineering, hallucination-prevention framing. It reuses the existing
# detect_style / grounded_set / claim_inventions primitives (no duplication) and
# adds role/company/geo/seniority/niche/keywords/mutuals/angle/tone/forbidden.
# CPU + 8GB safe: zero extra model calls; the compact prompt it builds is small.
# ============================================================================

# Generic (non-fashion) specific-claim words that must be GROUNDED or never said.
GENERIC_CLAIM_BLOCKLIST = frozenset("""
sustainable sustainability award award-winning leading largest biggest top top-tier
renowned famous best-selling bestselling iconic pioneer pioneering revolutionary
disruptive world-class flagship premier elite visionary guru expert#1 number-one
""".split())

_GEO_SINGLE = frozenset("""
dubai sharjah ajman fujairah emirates uae gcc qatar doha riyadh jeddah saudi kuwait
bahrain manama oman muscat london manchester paris milan rome berlin munich madrid
barcelona amsterdam istanbul cairo karachi lahore islamabad mumbai delhi bengaluru
bangalore chennai hyderabad singapore shanghai beijing shenzhen tokyo seoul sydney
melbourne toronto boston seattle austin chicago miami remote
""".split())
_GEO_MULTI = ("abu dhabi", "new york", "san francisco", "los angeles", "hong kong",
              "saudi arabia", "middle east", "united arab emirates", "silicon valley")

# Seniority labels, checked top-down (first match wins).
_SENIORITY_RULES = (
    ("executive", ("founder", "co-founder", "cofounder", "ceo", "chief", "owner",
                   "managing director", "managing partner", "president", "chairman",
                   "vp", "vice president", "head of", "partner", " director")),
    ("senior", ("senior", "principal", " lead", "lead ", "sr.", "sr ", "head", "specialist")),
    ("mid", ("manager", "consultant", "officer", "advisor", "engineer", "analyst")),
    ("junior", ("associate", "assistant", "junior", "intern", "coordinator", "trainee", "graduate")),
)

# Outreach angle + tone per detected style (steers the Qwen prompt, grounded only).
_ANGLE_BY_STYLE = {
    "founder": "founder-to-founder peer networking",
    "recruiter": "peer networking (talent / advisory)",
    "sourcing": "buyer-manufacturer / apparel-sourcing peer",
    "sales": "soft peer intro (no pitch)",
    "partnership": "exploratory partnership",
    "hiring": "low-pressure opportunity intro",
    "networking": "genuine peer networking",
    "warm_intro": "shared-context peer networking",
}
_TONE_BY_STYLE = {
    "founder": "warm, direct, confident peer",
    "recruiter": "respectful peer who values the talent/advisory space",
    "sourcing": "B2B apparel-sourcing peer",
    "sales": "low-key, curious, no sales",
    "partnership": "collaborative, exploratory",
    "hiring": "encouraging, low-pressure",
    "networking": "warm, genuine, peer-to-peer",
    "warm_intro": "familiar, warm",
}


def _profile_field(profile_text: str, label: str) -> str:
    m = re.search(rf"(?im)^\s*{label}\s*:\s*(.+)$", profile_text or "")
    return m.group(1).strip() if m else ""


def _headline_line(profile_text: str) -> str:
    """The headline value, or the first non-label content line as a fallback."""
    h = _profile_field(profile_text, "headline")
    if h:
        return h
    for ln in (profile_text or "").splitlines():
        s = ln.strip()
        if s and not re.match(r"\s*(name|company|location|about|context|mutual|headline)\s*:", s, re.I):
            return s
    return ""


def hermes_role_company(profile_text: str):
    """Split role/title and company from the headline + explicit fields."""
    head = _headline_line(profile_text)
    company = _profile_field(profile_text, "company")
    seg = re.split(r"\s*[|•·]\s*", head)[0].strip()  # first clause of the headline
    m = re.search(r"(.+?)\s+(?:at|@|of|for|with)\s+(.+)", seg, re.I)
    if m:
        role = m.group(1).strip()
        company = company or m.group(2).strip()
    else:
        role = seg
    return role[:80], (company or "")[:80]


def hermes_geo(profile_text: str) -> str:
    loc = _profile_field(profile_text, "location")
    if loc:
        return loc[:60]
    t = " " + (profile_text or "").lower() + " "
    for multi in _GEO_MULTI:
        if multi in t:
            return multi.title()
    for tok in _tokens(profile_text):
        if tok in _GEO_SINGLE:
            return tok.title()
    return ""


def hermes_seniority(profile_text: str) -> str:
    t = " " + (profile_text or "").lower() + " "
    for label, kws in _SENIORITY_RULES:
        if any(k in t for k in kws):
            return label
    return "unknown"


# Domain/niche hint vocabulary (grounded label for the person's space).
_DOMAIN_HINTS = APPAREL_KW + SOURCING_KW + (
    "talent", "advisory", "recruiting", "recruitment", "staffing", "saas", "software",
    "fintech", "ecommerce", "retail", "robotics", "logistics", "marketing", "design",
    "manufacturing", "consulting", "hospitality", "healthcare", "real estate", "media",
)


def hermes_keywords(profile_text: str, name: str = ""):
    """Significant, grounded content words (role/domain terms) literally present —
    excluding the target's own name tokens and bare geo tokens."""
    name_toks = set(_tokens(name))
    out = []
    for w in _tokens(profile_text):
        if (len(w) < 4 or w in _STOPWORDS or w in SAFE_VOCAB or w in _FIELD_LABELS
                or w in name_toks or w in _GEO_SINGLE):
            continue
        if w not in out:
            out.append(w)
        if len(out) >= 8:
            break
    return out


def hermes_niche(profile_text: str) -> str:
    """A short, grounded domain label — the 2nd headline clause, else the first
    domain-hint word literally in the profile (never the person's name)."""
    head = _headline_line(profile_text)
    segs = [s.strip() for s in re.split(r"\s*[|•·–]\s*", head) if s.strip()]
    if len(segs) >= 2:
        return segs[1][:50]
    t = " " + (profile_text or "").lower() + " "
    for d in _DOMAIN_HINTS:
        if d in t:
            return d
    return ""


def hermes_mutual_count(raw_profile_text: str) -> str:
    m = re.search(r"(\d[\d,]*)\s+(?:other\s+)?mutual", raw_profile_text or "", re.I)
    return m.group(1) if m else ""


def hermes_forbidden(grounded: set, fashion: bool):
    """Specific-claim words the writer must NOT use (they are not grounded). In
    fashion this is the apparel claim blocklist; generally a small superlative set."""
    block = FASHION_CLAIM_BLOCKLIST if fashion else GENERIC_CLAIM_BLOCKLIST
    return [w for w in sorted(block) if not _is_grounded(w, grounded)][:10]


def hermes_extract(raw_profile_text: str, target_name: str = "", fashion_hint: bool = None) -> dict:
    """Turn a raw LinkedIn profile into the structured Hermes spec (the JSON shown
    in the UI and consumed by the prompt builder). Deterministic; no LLM/network.
    Matches the schema: style, confidence, keywords, angle, forbidden,
    grounded_facts (+ role/company/niche/geo/seniority/mutuals/tone)."""
    profile = strip_mutual(raw_profile_text)           # grounding uses the stripped text
    style, confidence = detect_style(profile)
    fashion = fashion_hint if fashion_hint is not None else (
        IS_FASHION and (style == "sourcing" or is_apparel_profile(profile)))
    grounded = grounded_set(profile, target_name)
    role, company = hermes_role_company(profile)
    geo = hermes_geo(profile)
    seniority = hermes_seniority(profile)
    keywords = hermes_keywords(profile, target_name)
    niche = hermes_niche(profile)
    mutuals = hermes_mutual_count(raw_profile_text)
    forbidden = hermes_forbidden(grounded, fashion)
    facts = []  # human-readable, all literally from the profile
    if role:
        facts.append(f"role: {role}")
    if company and company.lower() != role.lower():
        facts.append(f"company: {company}")
    if niche and niche.lower() not in (role.lower(), company.lower()):
        facts.append(f"focus: {niche}")
    if geo:
        facts.append(f"location: {geo}")
    return {
        "style": style,
        "confidence": confidence,
        "role": role,
        "company": company,
        "niche": niche,
        "geo": geo,
        "seniority": seniority,
        "keywords": keywords,
        "mutuals": mutuals,
        "grounded_facts": facts,
        "angle": _ANGLE_BY_STYLE.get(style, _ANGLE_BY_STYLE["networking"]),
        "tone": _TONE_BY_STYLE.get(style, _TONE_BY_STYLE["networking"]),
        "forbidden": forbidden,
        "fashion_voice": fashion,
    }


def hermes_build_prompt(hp: dict, fname: str) -> str:
    """Build the Qwen outreach prompt FROM the structured Hermes spec — a compact,
    grounded, hallucination-resistant prompt (CPU-friendly: small prefill)."""
    facts = "\n".join(f"- {f}" for f in hp["grounded_facts"]) or "- (only their name is known)"
    forb = ", ".join(hp["forbidden"][:8]) if hp["forbidden"] else "any claim not written above"
    greeting = (f'Address them exactly as "Hi {fname},".' if fname
                else "Address them politely; do not invent a name.")
    sourcing = (" Frame YOUR side in B2B apparel sourcing (private label, fashion "
                "supply chain) tied to a REAL fact above." if hp["fashion_voice"] else "")
    return (
        f"Write ONE LinkedIn connection note (120-180 characters).\n"
        f"{greeting}\n"
        f"GROUNDED FACTS — reference at least one, and use ONLY these; invent nothing else:\n{facts}\n"
        f"ANGLE: {hp['angle']}. TONE: {hp['tone']}.{sourcing}\n"
        f"FORBIDDEN — never claim any of these (not in their profile): {forb}.\n"
        f"No mutual-connection mentions. Avoid 'noticed your', 'came across', 'would "
        f"love to connect', 'reaching out', 'great work'. Output ONLY the note text."
    )


def generate_outreach(state: dict) -> dict:
    # Strip the Mutual line BEFORE the model sees the profile — we never imply a
    # mutual connection (we only ever have a count, never a verified name).
    raw_profile = state["profile_text"]
    profile = strip_mutual(raw_profile)
    name = (state.get("target_name") or "").strip()
    fname = first_name(name)
    # HERMES single-person path: build the structured profile spec and derive style
    # + voice from it. Batch keeps the existing inline path (untouched).
    use_hermes = bool(state.get("single"))
    if use_hermes:
        hp = hermes_extract(raw_profile, name)
        style, style_conf = hp["style"], hp["confidence"]
        use_fashion_voice = hp["fashion_voice"]
        state["hermes"] = hp
    else:
        detected_style, style_conf = detect_style(profile)
        style = state.get("style") or detected_style
        # PER-PROFILE fashion voice: apply the apparel-sourcing prompt/voice ONLY to
        # a genuine sourcing style or an actually-apparel profile — never blindly to
        # every profile just because MODE=fashion.
        use_fashion_voice = IS_FASHION and (style == "sourcing" or is_apparel_profile(profile))
    state["style"] = style
    state["style_confidence"] = style_conf
    guidance = STYLE_GUIDANCE.get(style, STYLE_GUIDANCE["networking"])
    name_line = (
        f"The person you are writing to is named: {name}\n"
        f"Address them exactly as \"Hi {fname},\" — use no other name.\n"
        if fname else
        "Address them politely; if no name is given, do not invent one.\n"
    )
    # The set of facts the note is ALLOWED to reference. Anything else is invention.
    grounded = grounded_set(profile, name)
    # A per-profile opening angle (stable hash) so a queue of drafts varies.
    pid = state.get("profile_id", "")
    angle = OPENER_ANGLES[_stable_hash(pid or name or profile) % len(OPENER_ANGLES)]

    # Pick the system prompt by per-profile VOICE (fashion only when this profile is
    # genuinely apparel/sourcing) and speed (FAST). FAST variants are ~1/5 the
    # prefill tokens (much faster on CPU).
    if use_fashion_voice:
        out_system = OUTREACH_SYSTEM_FASHION_FAST if FAST_MODE else OUTREACH_SYSTEM_FASHION
    else:
        out_system = OUTREACH_SYSTEM_FAST if FAST_MODE else OUTREACH_SYSTEM
    if use_hermes:
        # HERMES prompt-engineering: the prompt is built FROM the structured spec
        # (grounded facts + angle + tone + forbidden claims), not the raw blob.
        base_prompt = hermes_build_prompt(hp, fname)
    elif FAST_MODE or use_fashion_voice:
        sourcing_hint = (
            " Use B2B apparel-sourcing framing (private label, fashion supply "
            "chain, production partnership) tied to a REAL profile fact."
            if use_fashion_voice else ""
        )
        base_prompt = (
            f"{name_line}"
            f"Profile (use ONLY these facts, invent nothing):\n{profile}\n\n"
            f"Write the 120-180 char note, referencing at least one real detail "
            f"(role/company/headline phrase).{sourcing_hint} Output only the message:"
        )
    else:
        base_prompt = (
            f"{name_line}\n"
            f"Their LinkedIn profile — these are the ONLY facts you may use; reference "
            f"nothing outside this block:\n{profile}\n\n"
            f"Angle: {style} — {guidance}\n"
            f"Opening: {angle}\n\n"
            f"Write ONE connection note of 120-180 characters, founder-to-founder.\n"
            f"- You MUST reference at least one CONCRETE detail from the profile (their "
            f"role, company, or a real phrase from their headline/about). A note that "
            f"names no real detail is REJECTED.\n"
            f"- Do NOT name any industry, product, or specialty not written in the "
            f"profile. If it is not there, do not say it.\n"
            f"- Avoid the stale phrases 'adjacent space', 'compare notes', 'connect as "
            f"peers'. Use fresh, specific wording. Output only the message:"
        )

    # Best-of-N. Score (lower=better), in priority order:
    #   1. #banned (spam)              2. 0 if it uses a real fact else 1
    #   3. #ungrounded (hallucination) 4. #overused (repetition)  5. length-dist
    # i.e. a SPECIFIC, grounded, fresh draft beats a generic or invented one. We
    # regenerate naming the exact problem (no fact / invented X / stale phrase Y).
    # best = (score, note, used, ungrounded, conf, overused).
    # In fashion mode the "hallucination" gate targets SPECIFIC invented claims
    # (brand/category/sustainability — claim_inventions), NOT generic descriptive
    # words: sourcing prose is naturally wordy, and gating on every ungrounded
    # generic word forced good drafts into the fallback. In general mode it stays
    # the broad ungrounded-word count (unchanged). `conf` shown to the user is the
    # real grounding fraction either way.
    def evaluate(nt):
        used, ung, conf = grounding_check(nt, grounded, fname)
        # Fashion-voice profiles use the specific-claim gate (sourcing prose is
        # wordy); everyone else uses the strict ungrounded-word count.
        halluc = claim_inventions(nt, grounded) if use_fashion_voice else ung
        ov = overused_hits(nt)
        n = len(nt)
        dist = 0 if 120 <= n <= 180 else (120 - n if n < 120 else n - 180)
        score = (len(banned_hits(nt)), 0 if used else 1, len(halluc), len(ov), dist)
        return score, used, halluc, conf, ov

    # SMART ROUTING: premium profiles (founder/CEO/director/...) -> PREMIUM_MODEL,
    # everything else -> base model. Under RAM pressure model_for() forces the base
    # model AND we drop to best-of-1 (defensive mode) so a memory spike degrades
    # quality instead of OOM-killing the worker.
    gen_model, premium_used, defensive = model_for(style, profile)

    # Budget: FAST_MODE shrinks best-of and skips length-only regeneration, and
    # fast-paths every style but `founder`. Generation is sequential either way.
    budget = outreach_budget(style)
    attempts, npredict, length_regen = (
        budget["attempts"], budget["num_predict"], budget["length_regen"])
    if defensive:
        attempts, length_regen = 1, False  # RAM pressure -> single shot, no regen
        npredict = npredict or 120
    print(f"[ai-brain] routing style={style} model={gen_model} premium={premium_used} "
          f"defensive={defensive} attempts={attempts} free_mb={host_free_mb()}", flush=True)

    best = None
    for attempt in range(attempts):
        extra = ""
        if attempt > 0 and best is not None:
            problems = []
            if not best[2]:
                problems.append("it referenced NO concrete detail from the profile — you "
                                "MUST mention their role, company, or a headline/about phrase")
            if best[3]:
                problems.append(f"it invented {', '.join(best[3])} (not in the profile) — remove those")
            if best[5]:
                problems.append(f"it used the stale phrase(s) {', '.join(best[5])} — use fresh wording")
            if not problems:
                problems.append("it was the wrong length or too plain — make it sharper and more specific")
            extra = (f"\n\nYour previous attempt was: \"{best[1]}\"\n"
                     f"Problems: " + "; ".join(problems) +
                     ". Rewrite it, 120-180 characters, grounded ONLY in the profile, fresh wording.")
        # Higher base temperature than before => more varied openings across a queue.
        # generate_with_fallback downgrades premium->base on any failure so the
        # worker never crashes and always produces a draft (or the grounded fallback).
        raw = generate_with_fallback(base_prompt + extra, out_system,
                                     0.6 + 0.1 * attempt, npredict, gen_model)
        note = clean_note(raw, fname)
        if not note:
            continue
        sc, used, ung, conf, ov = evaluate(note)
        if best is None or sc < best[0]:
            best = (sc, note, used, ung, conf, ov)
        if sc == (0, 0, 0, 0, 0):  # clean, grounded, fresh, in-band -> stop early
            break
        # Decide whether another generation is worth it, judged on the BEST draft
        # so far. CONTENT problems (spam / no real fact / hallucination) are the
        # only ones worth a retry; a clean-but-short/overused draft is shippable.
        halluc_floor = 1 if use_fashion_voice else 2  # fashion voice: ANY specific invented claim
        b_spam, b_nofact, b_halluc = best[0][0] > 0, not best[2], best[0][2] >= halluc_floor
        content_problem = b_spam or b_nofact or b_halluc
        if not content_problem and not length_regen:
            break  # grounded & spam-free -> stop now (don't burn a turn chasing length)

    # ACCEPT the model draft when it is spam-free, references a real fact, and
    # invents < 2 terms. Otherwise ship the GROUNDED fallback (quotes a real
    # headline fragment, varied wording, never the stale filler) — which itself
    # satisfies "uses >=1 real fact". This keeps drafts specific, not generic.
    # Accept the model draft when: no banned phrase, it uses a real fact, and no
    # disqualifying invention. Fashion = ZERO specific invented claims (strict, per
    # use-case); general = fewer than 2 ungrounded terms (unchanged).
    halluc_ok = best and (best[0][2] == 0 if use_fashion_voice else best[0][2] < 2)
    acceptable = best and best[0][0] == 0 and best[0][1] == 0 and halluc_ok
    if acceptable:
        note = best[1]
        print(f"[ai-brain] outreach OK style={style} conf={style_conf} fashion_voice={use_fashion_voice} "
              f"score={best[0]} len={len(note)} used={best[2]}", flush=True)
    else:
        why = []
        if best:
            if not best[2]:
                why.append("no real fact used")
            if best[3]:
                why.append(f"invented {best[3]}")
        # Fallback voice matches the per-profile decision (apparel fallback only for
        # genuine apparel/sourcing profiles, never for a talent-advisory founder).
        note = grounded_fallback(fname, profile, pid or name, fashion=use_fashion_voice)
        print(f"[ai-brain] outreach -> grounded fallback style={style} "
              f"fashion_voice={use_fashion_voice} ({'; '.join(why) or 'no usable draft'})", flush=True)

    # Fashion/FAST tiers don't regen for length — deterministically trim an
    # over-long note to the band at a clean boundary (no extra generation).
    if (IS_FASHION or FAST_MODE) and len(note) > 180:
        note = enforce_length(note, 180)

    # Report ACCURATE scores on the FINAL note (model or fallback alike).
    used, ungrounded, conf = grounding_check(note, grounded, fname)
    state["outreach"] = note
    state["key_phrases"] = used
    state["grounding_confidence"] = conf
    state["personalization"] = personalization_score(used)
    return state


def build_outreach_graph():
    try:
        from langgraph.graph import StateGraph, START, END
        g = StateGraph(dict)
        g.add_node("generate_outreach", generate_outreach)
        g.add_edge(START, "generate_outreach")
        g.add_edge("generate_outreach", END)
        return g.compile()
    except Exception as e:
        print(f"[ai-brain] LangGraph unavailable for outreach ({e}); running inline", flush=True)
        class _Inline:
            def invoke(self, s):
                return generate_outreach(s)
        return _Inline()


OUTREACH_GRAPH = build_outreach_graph()


def process_outreach(profile_id: str, profile_text: str, target_name: str = "", single: str = ""):
    out = OUTREACH_GRAPH.invoke({
        "profile_id": profile_id,
        "profile_text": profile_text,
        "target_name": target_name,
        # single -> run the Hermes per-person pipeline (structured spec + prompt).
        "single": single,
    })
    fields = {
        "type": "outreach.generated",
        "profile_id": profile_id,
        "outreach_text": out["outreach"],
        "style": out.get("style", "networking"),
        # Routing transparency: the detected style's confidence (high/medium/low).
        "style_confidence": out.get("style_confidence", ""),
        # Debug-preview signal: key phrases actually drawn from the profile, a
        # 0..1 grounding confidence, and a 0..1 personalization score. Carried
        # through to the approval-queue UI.
        "key_phrases": ", ".join(out.get("key_phrases", []) or []),
        "grounding": str(out.get("grounding_confidence", "")),
        "personalization": str(out.get("personalization", "")),
        "event_id": str(uuid.uuid4()),
    }
    # Hermes structured understanding (single-person path only) — JSON for the UI.
    if out.get("hermes"):
        fields["hermes"] = json.dumps(out["hermes"], ensure_ascii=False)
    r.xadd(STREAM_RESULT, fields)
    print(f"[ai-brain] profile={profile_id} style={out.get('style')} -> outreach.generated: {out['outreach'][:80]!r}", flush=True)


def process(thread_id: str, text: str):
    out = GRAPH.invoke({"thread_id": thread_id, "text": text})
    draft = out["draft"]
    r.xadd(STREAM_RESULT, {
        "type": "draft.generated",
        "thread_id": thread_id,
        "draft_text": draft,
        "event_id": str(uuid.uuid4()),
    })
    print(f"[ai-brain] thread={thread_id} -> draft.generated: {draft[:80]!r}", flush=True)


def main():
    # wait for redis
    for _ in range(30):
        try:
            r.ping()
            break
        except Exception as e:
            print(f"[ai-brain] waiting for redis... ({e})", flush=True)
            time.sleep(1)
    try:
        r.xgroup_create(STREAM_INGEST, GROUP, id="0", mkstream=True)
    except redis.ResponseError:
        pass  # BUSYGROUP: already exists
    print(f"[ai-brain] consuming {STREAM_INGEST} | base={BASE_MODEL} "
          f"premium={PREMIUM_MODEL if PREMIUM_ROUTING else 'off'} mode={MODE} "
          f"fast={FAST_MODE} sequential={SEQUENTIAL} (read_count={READ_COUNT}) "
          f"ram_min_free={RAM_MIN_FREE_MB}MB free_now={host_free_mb()}MB "
          f"ollama={OLLAMA}", flush=True)

    while True:
        try:
            # READ_COUNT=1 under SEQUENTIAL_MODE: pull exactly one entry, finish it
            # end-to-end, then come back for the next. No two generations overlap.
            resp = r.xreadgroup(GROUP, "brain-1", {STREAM_INGEST: ">"}, count=READ_COUNT, block=5000)
        except Exception as e:
            print(f"[ai-brain] read error: {e}", flush=True)
            time.sleep(1)
            continue
        if not resp:
            continue
        for _stream, messages in resp:
            for msg_id, fields in messages:
                try:
                    if fields.get("type") == "message.received":
                        process(fields["thread_id"], fields.get("text", ""))
                    elif fields.get("type") == "outreach.requested":
                        if SEQUENTIAL:
                            print(f"[ai-brain] [seq] start profile={fields.get('profile_id')} "
                                  f"— finishing fully before the next", flush=True)
                        process_outreach(
                            fields["profile_id"],
                            fields.get("profile_text", ""),
                            fields.get("target_name", ""),
                            fields.get("single", ""),
                        )
                except Exception as e:
                    # Never let one bad message crash the worker; ack + continue.
                    print(f"[ai-brain] process error: {e}", flush=True)
                finally:
                    r.xack(STREAM_INGEST, GROUP, msg_id)
                # QUEUE THROTTLE: under RAM pressure, pause between drafts so the
                # kernel can reclaim memory before the next (model-loading) draft.
                if QUEUE_THROTTLE_SEC and ram_pressure():
                    print(f"[ai-brain] RAM pressure (free {host_free_mb()}MB) — "
                          f"unloading premium + pausing {QUEUE_THROTTLE_SEC}s", flush=True)
                    ollama_unload(PREMIUM_MODEL)
                    time.sleep(QUEUE_THROTTLE_SEC)


if __name__ == "__main__":
    main()
