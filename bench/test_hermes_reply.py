"""Offline harness for the HERMES reply agent (services/ai-brain/hermes_reply.py).

No Redis/Ollama needed: the model is replaced by scripted fakes, so every rule
(thread parsing, validation, retry, fallback) is checked deterministically.
Includes the two real failures seen on 2026-11-15:
  - "Hi Abdul, Tuesdays work for me! Let's chat. ... 🌟"  (emoji + commits a meeting)
  - "Acknowledged, noted for adding commission structure..." (robotic, answered our own message)

Run from the repo root:  python3 bench/test_hermes_reply.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "ai-brain"))
import hermes_reply as H  # noqa: E402

OWNER = "Abdul Kadir Mukadam"
FAILS = 0


def check(name, cond, detail=""):
    global FAILS
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    FAILS += 0 if cond else 1


def scripted(*outputs):
    it = iter(outputs)
    return lambda prompt, system: next(it, "")


THREAD_MEETING = (
    "Saima Shaikh 5:10 PM\n"
    "Hi Abdul, we are a knitwear brand in Dubai looking for a private label partner. "
    "Are you free for a quick call on Tuesday?\n"
)
THREAD_OWNER_LAST = (
    "Omar Mahdi 4:02 PM\n"
    "Can you share the commission structure and factory rates?\n"
    "Abdul Kadir Mukadam 4:30 PM\n"
    "Sure, I will add the commission structure and factory rates on the website this week.\n"
)

# --- parsing ---
u = H.understand(THREAD_MEETING, OWNER)
check("parse: speaker detected", u["other_name"] == "Saima Shaikh", u["other_name"])
check("parse: meeting intent", u["intent"] == "meeting", u["intent"])
check("parse: tuesday is a known fact", "tuesday" in u["facts"]["days"], u["facts"]["days"])
u2 = H.understand(THREAD_OWNER_LAST, OWNER)
check("parse: owner spoke last", u2["last_is_owner"] is True)
check("parse: latest inbound is Omar's", "commission" in u2["latest"] and u2["other_first"] == "Omar", u2)
u3 = H.understand("Hello, could you send me your catalogue?", OWNER)
check("parse: pasted text without headers", u3["latest"].startswith("Hello") and u3["intent"] == "question", u3)

# --- validation: the real bad drafts are rejected ---
bad1 = H.clean("Hi Abdul, Tuesdays work for me! Let's chat. Looking forward to it. 🌟")
v1 = H.validate(bad1, u)
check("emoji stripped by clean()", "🌟" not in bad1, bad1)
check("rejects: commits on owner's behalf", any("commits" in x for x in v1), v1)
check("rejects: greeting the owner instead of the sender", any("wrong name" in x for x in v1), v1)
v2 = H.validate("Acknowledged, noted for adding commission structure and factory rates on the web.", u2)
check("rejects: robotic opener", any("robotic" in x for x in v2), v2)
check("rejects: invented day", any("invented day" in x for x in H.validate("Hi Saima, could we do Friday instead of a call now?", u)))
check("rejects: invented number", any("invented number" in x for x in H.validate("Hi Saima, our MOQ is 500 pieces per style, happy to talk more.", u)))
check("rejects: invented link", any("link" in x for x in H.validate("Hi Saima, please see https://example.com for details and let me know.", u)))
check("rejects: invented name", any("wrong name" in x for x in H.validate("Hi Priya, thanks for the note, happy to talk about private label.", u)))
check("rejects: phone number", any("phone" in x for x in H.validate("Hi Saima, call me on +971 50 123 4567 any time this week please.", u)))
check("rejects: preamble cleaned", H.clean('Here is a reply: "Hi Saima, thanks!"') == "Hi Saima, thanks!")
for txt, why in (("Sure, I'm free now. Let's discuss the bulk order.", "commits"),
                 ("Sure, I'm free on Tuesday afternoon. What time works for you?", "afternoon"),
                 ("Sure, I can send that. When would be a good time for you?", "commits")):
    check("rejects (live eval case): " + txt[:40], any(why in x for x in H.validate(txt, u)), H.validate(txt, u))
check("rejects: repeating the owner's own last message",
      any("owner's own" in x for x in H.validate("Sure, I'll add the commission structure and factory rates to the website this week.", u2)))
check("rejects: claims a done action", any("did not happen" in x for x in H.validate("Sure, I've added the commission structure and factory rates to the website.", u2)))
uR = H.understand("Romeo Nistor 6:30 PM\nWhat is your MOQ and lead time for denim jackets?", OWNER)
check("clean: strips 'Sure, here's a revised draft:'", not H.clean("Sure, here's a revised draft: Hi Romeo, thanks.").lower().startswith("sure"))
check("rejects: echoes their question", any("echoes" in x for x in H.validate("What are your MOQ and lead time for denim jackets?", uR)))
good = "Hi Saima, thanks for reaching out. Private label knitwear sounds interesting. What time on Tuesday suits you for a quick call?"
check("accepts: grounded reply that asks for a time", H.validate(good, u) == [], H.validate(good, u))

# --- run(): retry then pass ---
d, m = H.run(THREAD_MEETING, scripted(bad1, good), attempts=2, owner=OWNER)
check("run: retries after a bad draft and returns the good one", d == good and m["source"] == "model" and m["attempt"] == 2, (d, m))
# --- run(): all bad -> safe fallback that itself passes validation ---
d, m = H.run(THREAD_MEETING, scripted(bad1, "Acknowledged."), attempts=2, owner=OWNER)
check("run: falls back when every draft fails", m["source"] == "fallback", m)
check("run: fallback passes its own validation", H.validate(d, u) == [], (d, H.validate(d, u)))
check("run: fallback greets the right person", d.startswith("Hi Saima"), d)
d, m = H.run(THREAD_OWNER_LAST, scripted("", ""), attempts=2, owner=OWNER)
check("run: empty model output -> fallback for Omar", d.startswith("Hi Omar") and m["source"] == "fallback", d)
check("run: owner-last fallback passes validation", H.validate(d, u2) == [], H.validate(d, u2))


def boom(prompt, system):
    raise RuntimeError("ollama down")


d, m = H.run(THREAD_MEETING, boom, attempts=2, owner=OWNER)
check("run: model exception never crashes, returns fallback", m["source"] == "fallback" and d, m)

print(f"\n{'ALL PASS' if not FAILS else str(FAILS) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
