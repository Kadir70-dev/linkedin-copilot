"""HERMES reply agent — harness around the LinkedIn message-reply draft.

The model only proposes text. Hermes decides what it may say and rejects drafts
that break the rules:

  1. understand()  — split the captured thread into turns, find who spoke last,
                     and collect the facts the reply is allowed to repeat
                     (names, days, times, numbers, links, emails).
  2. build_prompt()— a strict prompt: answer the latest inbound message, invent
                     nothing, never confirm a new day/time/price, no emoji.
  3. validate()    — deterministic checks on every draft (invented numbers/days/
                     links/names, robotic openers, preamble, emoji, length,
                     replying to ourselves).
  4. run()         — best-of-N with a nudge naming the violations; if no draft
                     passes, a safe deterministic fallback that only uses facts
                     from the thread.

Pure Python, no I/O except the injected `generate` callable, so it is unit-
testable without Redis or Ollama (see bench/test_hermes_reply.py).
"""
import re

OWNER_NAME = ""  # set by the worker from env (the account owner's display name)

_TIME_RE = re.compile(r"\b\d{1,2}[:.]\d{2}\s*(?:am|pm)?\b|\b\d{1,2}\s*(?:am|pm)\b", re.I)
_META_RE = re.compile(
    r"^(?P<name>[A-Z][\w'.-]+(?:\s+[A-Z][\w'.-]+){0,4})\s*(?:[•·|-]\s*)?"
    r"(?:\d{1,2}:\d{2}\s*(?:AM|PM)|Yesterday|Today|Mon|Tue|Wed|Thu|Fri|Sat|Sun|[A-Z][a-z]{2} \d{1,2})\b.*$")
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
         "tomorrow", "today", "tonight", "weekend", "next week", "morning", "afternoon", "evening")
_NUM_RE = re.compile(r"(?<![\w])[$€£₹]?\d[\d,.]*\s*(?:%|k|m|aed|usd|eur|dhs|pcs|units)?(?![\w])", re.I)
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_WORDNUM_RE = re.compile(r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|"
                         r"fifty|hundred|thousand|a few|several|couple of)\s+(?:to\s+\w+\s+)?"
                         r"(?:days?|weeks?|months?|pieces?|pcs|units?|percent|dollars?|dirhams?)\b", re.I)
_PHONE_RE = re.compile(r"\+?\d[\d\s-]{7,}\d")
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐⭕️]")
_PREAMBLE_RE = re.compile(r"^\s*(?:(?:sure|ok|okay)[,!.]?\s*)?(?:here(?:'s| is)[^:\n]*:|(?:revised )?(?:reply|draft|response):)\s*", re.I)
_DONE_RE = re.compile(r"\bi(?:'ve| have)\s+(?:already\s+)?(?:added|sent|updated|shared|uploaded|attached|completed|done|finished|booked|scheduled)\b", re.I)
_META_LEAK_RE = re.compile(r"\b(?:revised draft|draft reply|as an ai|language model|previous draft)\b", re.I)

ROBOTIC_OPENERS = ("acknowledged", "noted", "as an ai", "certainly!", "i hope this message finds you",
                   "hope you're doing well", "hope you are doing well", "thank you for reaching out",
                   "dear sir", "dear madam", "greetings")
COMMIT_PHRASES = ("works for me", "work for me", "works for us", "work for us", "confirmed", "see you on", "see you at", "booked", "i've scheduled",
                  "i have scheduled", "i'll send", "i will send", "attached", "i've attached",
                  "deal", "agreed", "i accept", "i'm free", "i am free", "i'm available", "i am available",
                  "i can do", "i can send", "i'll share", "i will share", "i can share", "sending it now",
                  "i'll call", "i will call")
MIN_WORDS, MAX_WORDS = 6, 70

SYSTEM = (
    "You draft a short LinkedIn reply for the account owner. A human reviews it and "
    "presses Send. Rules, all mandatory:\n"
    "1. Reply ONLY to the latest message from the other person. Never reply to the owner's own message.\n"
    "2. Use ONLY facts in the conversation. Never invent names, companies, prices, numbers, "
    "dates, times, links, files or promises.\n"
    "3. Never confirm, accept or book a new day, time, price or deal. If they propose a time, "
    "thank them and say you will confirm, or ask what time suits them.\n"
    "4. No emoji, no hashtags, no quotes, no preamble, no sign-off name.\n"
    "5. Natural and warm, 15-50 words, 1-3 sentences. Do not start with 'Acknowledged' or 'Noted'.\n"
    "6. Never claim something was already done or sent. Never repeat their question back.\n"
    "7. Reply in the same language and tone as their message (English, Hinglish, Arabic...).\n"
    "Output ONLY the reply text."
)


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def split_turns(text):
    """Turn the captured thread into [(speaker, message)]. Speaker is '' when the
    capture carries no name/time header (e.g. pasted text)."""
    turns, speaker, buf = [], "", []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _META_RE.match(line)
        if m and len(line) < 80:
            if buf:
                turns.append((speaker, _norm(" ".join(buf))))
                buf = []
            speaker = m.group("name").strip()
            continue
        buf.append(line)
    if buf:
        turns.append((speaker, _norm(" ".join(buf))))
    return turns


def _is_owner(speaker, owner):
    if not speaker or not owner:
        return False
    s, o = speaker.lower().split(), owner.lower().split()
    return bool(s and o and s[0] == o[0])


def _digits(n):
    return re.sub(r"[^\d.]", "", n).strip(".")


def _facts(text):
    t = text.lower()
    return {
        "numbers": {_digits(n) for n in _NUM_RE.findall(text) if _digits(n)},
        "times": {x.lower().replace(" ", "") for x in _TIME_RE.findall(text)},
        "days": {d for d in _DAYS if d in t},
        "urls": {u.lower() for u in _URL_RE.findall(text)},
        "emails": {e.lower() for e in _EMAIL_RE.findall(text)},
        "words": set(re.findall(r"[a-z][a-z'-]+", t)),
    }


def understand(text, owner=None):
    owner = OWNER_NAME if owner is None else owner
    turns = split_turns(text)
    inbound = [(s, m) for s, m in turns if not _is_owner(s, owner)]
    latest = inbound[-1] if inbound else (turns[-1] if turns else ("", _norm(text)))
    last_is_owner = bool(turns) and _is_owner(turns[-1][0], owner)
    other = latest[0] if latest[0] and not _is_owner(latest[0], owner) else ""
    names = {s.split()[0].lower() for s, _ in turns if s}
    owner_first = owner.split()[0].lower() if owner else ""
    msg = latest[1]
    lower = msg.lower()
    intent = ("meeting" if re.search(r"\b(call|meet|meeting|chat|zoom|available|free|schedule)\b", lower)
              else "question" if "?" in msg else "update")
    return {
        "turns": turns, "latest": msg, "other_name": other, "other_first": other.split()[0] if other else "",
        "last_is_owner": last_is_owner, "intent": intent, "names": names, "owner_first": owner_first,
        "facts": _facts(text),
    }


def build_prompt(u, nudge=""):
    recent = "\n".join(f"{s or 'Them'}: {m}" for s, m in u["turns"][-6:]) or u["latest"]
    p = (f"Conversation (oldest to newest):\n{recent}\n\n"
         f"Latest message to answer{(' from ' + u['other_name']) if u['other_name'] else ''}:\n{u['latest']}\n\n"
         f"Intent: {u['intent']}\n")
    if u["other_first"]:
        p += f"Greet them as {u['other_first']} (or no greeting).\n"
    if nudge:
        p += f"\nYour previous draft was rejected: {nudge}. Fix that.\n"
    return p + "\nDraft the reply:"


def clean(draft):
    d = _PREAMBLE_RE.sub("", draft or "").strip()
    d = d.strip('"').strip("'").strip()
    d = _EMOJI_RE.sub("", d)
    d = re.sub(r"(?is)\n\s*(?:best(?: regards)?|kind regards|warm regards|regards|thanks|cheers|sincerely)[,!.]?\s*(?:\n.*)?$", "", d)  # trailing sign-off
    d = re.sub(r"#\w+", "", d)
    return _norm(d)


def validate(draft, u):
    """Return a list of violations (empty = pass)."""
    v, low, f = [], draft.lower(), u["facts"]
    words = draft.split()
    if len(words) < MIN_WORDS:
        v.append("too short")
    if len(words) > MAX_WORDS:
        v.append("too long")
    if any(low.startswith(o) or f" {o}" in low[:40] for o in ROBOTIC_OPENERS):
        v.append("robotic opener")
    for n in _NUM_RE.findall(draft):
        if _digits(n) and _digits(n) not in f["numbers"]:
            v.append(f"invented number '{n.strip()}'")
    for w in _WORDNUM_RE.findall(draft):
        if w.lower() not in u["latest"].lower() and w.lower() not in " ".join(m for _, m in u["turns"]).lower():
            v.append(f"invented quantity '{w}'")
    for t in _TIME_RE.findall(draft):
        if t.lower().replace(" ", "") not in f["times"]:
            v.append(f"invented time '{t}'")
    for d in _DAYS:
        if d in low and d not in f["days"]:
            v.append(f"invented day '{d}'")
    for u_ in _URL_RE.findall(draft):
        if u_.lower() not in f["urls"]:
            v.append("invented link")
    for e in _EMAIL_RE.findall(draft):
        if e.lower() not in f["emails"]:
            v.append("invented email")
    if _DONE_RE.search(draft):
        v.append("claims an action that did not happen")
    if _META_LEAK_RE.search(draft):
        v.append("talks about the draft itself")
    theirs = set(re.findall(r"[a-z']{3,}", u["latest"].lower()))
    mine_q = set(re.findall(r"[a-z']{3,}", low))
    if draft.rstrip().endswith("?") and mine_q and len(mine_q & theirs) / len(mine_q) > 0.6:
        v.append("echoes their question back")
    if _PHONE_RE.search(draft):
        v.append("phone number")
    if any(p in low for p in COMMIT_PHRASES):
        v.append("commits on the owner's behalf")
    g = re.match(r"^(?:hi|hello|hey|dear)\s+([A-Za-z'-]+)", draft.strip(), re.I)
    if g:
        greeted = g.group(1).lower()
        if greeted not in u["names"] or (u["owner_first"] and greeted == u["owner_first"]):
            v.append(f"wrong name '{g.group(1)}'")
    if u["last_is_owner"]:
        own = set(re.findall(r"[a-z']{3,}", u["turns"][-1][1].lower()))
        mine = set(re.findall(r"[a-z']{3,}", low))
        if re.match(r"^(?:thanks|thank you|great|sounds good)", low) or (mine and len(mine & own) / len(mine) > 0.5):
            v.append("repeats or answers the owner's own message")
    return v


def fallback(u):
    """Safe, fact-only reply used when no model draft passes validation."""
    hi = f"Hi {u['other_first']}, " if u["other_first"] else "Hi, "
    if u["last_is_owner"]:
        return hi + "just following up on my last message. Let me know your thoughts when you get a moment."
    if u["intent"] == "meeting":
        day = next((d for d in _DAYS[:7] if d in u["latest"].lower()), "")
        when = f" on {day.capitalize()}" if day else ""
        return hi + f"thanks for reaching out, happy to have a call. What time{when} suits you best?"
    if u["intent"] == "question":
        return hi + "thanks for your message. Let me check the details and get back to you shortly."
    return hi + "thanks for the update, appreciated. I'll take a look and get back to you."


def run(text, generate, attempts=2, owner=None):
    """generate(prompt, system) -> str. Returns (draft, meta)."""
    u = understand(text, owner)
    nudge, tried = "", []
    for i in range(max(1, attempts)):
        raw = ""
        try:
            raw = generate(build_prompt(u, nudge), SYSTEM) or ""
        except Exception as e:  # a model error must never crash the worker
            tried.append({"error": str(e)[:120]})
            continue
        d = clean(raw)
        bad = validate(d, u)
        tried.append({"draft": d[:160], "violations": bad})
        if not bad:
            return d, {"source": "model", "attempt": i + 1, "intent": u["intent"], "violations": [],
                       "rejected": tried[:-1]}
        nudge = "; ".join(bad[:4])
    d = fallback(u)
    return d, {"source": "fallback", "attempt": len(tried), "intent": u["intent"],
               "violations": validate(d, u), "rejected": tried}
