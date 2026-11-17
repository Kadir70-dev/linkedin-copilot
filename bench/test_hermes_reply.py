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
                 ):
    check("rejects (live eval case): " + txt[:40], any(why in x for x in H.validate(txt, u)), H.validate(txt, u))
check("rejects: repeating the owner's own last message",
      any("owner's own" in x for x in H.validate("Sure, I'll add the commission structure and factory rates to the website this week.", u2)))
check("rejects: claims a done action", any("did not happen" in x for x in H.validate("Sure, I've added the commission structure and factory rates to the website.", u2)))
uR = H.understand("Romeo Nistor 6:30 PM\nWhat is your MOQ and lead time for denim jackets?", OWNER)
check("clean: strips 'Sure, here's a revised draft:'", not H.clean("Sure, here's a revised draft: Hi Romeo, thanks.").lower().startswith("sure"))
check("rejects: echoes their question", any("echoes" in x for x in H.validate("What are your MOQ and lead time for denim jackets?", uR)))
check("rejects: spelled-out invented lead time", any("quantity" in x for x in H.validate("Hi Romeo, lead time is typically two to three weeks depending on the design.", uR)))
uM = H.understand("Muhammad Al-Zafar 9:15 AM\nI want to discuss bulk t-shirt orders, around 5000 pcs.", OWNER)
check("accepts: a number that is in the thread", not any("number" in x for x in H.validate("Hi Muhammad, thanks, happy to discuss the 5000 t-shirts. What time suits you?", uM)), H.validate("Hi Muhammad, thanks, happy to discuss the 5000 t-shirts. What time suits you?", uM))
check("clean: strips 'Best regards, Abdul' sign-off", H.clean("Hi Romeo, thanks for reaching out.\n\nBest regards,\nAbdul") == "Hi Romeo, thanks for reaching out.", H.clean("Hi Romeo, thanks for reaching out.\n\nBest regards,\nAbdul"))
uS = H.understand("Saima Shaikh 5:18 PM\n\U0001F44D\nAbdul Kadir Mukadam 8:39 PM\nNoted, adding the commission structure and factory rates on the website. Looking forward to finalizing the domain today.", OWNER)
okS = "Thanks Saima, glad that looks fine to you. Let me know if anything else is needed from my side on the domain or the website details."
check("accepts (live 16:37 case): thanking after their reaction when owner spoke last", H.validate(okS, uS) == [], H.validate(okS, uS))
check("accepts: a follow-up promise to share an update", H.validate("Hi Saima, thanks for the thumbs up. I'll share an update on the domain as soon as there is progress.", uS) == [], H.validate("Hi Saima, thanks for the thumbs up. I'll share an update on the domain as soon as there is progress.", uS))
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


# --- tone + user instructions (K70 popup) ---
check("style_lines: empty when nothing chosen", H.style_lines("", "") == "" and H.style_lines("nonsense", "") == "")
check("style_lines: each tone has a hint", all(H.style_lines(t) for t in ("professional", "friendly", "casual", "short")))
check("style_lines: Hinglish instruction is passed through", "Recruiter ko politely" in H.style_lines("", "Recruiter ko politely bolo ki main Thursday available hoon."))
check("style_lines: control chars removed, capped at 400", "\n" not in H.style_lines("", "a\nb").split("Hinglish; ")[-1] and H.style_lines("", "q" * 900).count("q") == 400)
u_t = H.understand(THREAD_MEETING, OWNER)
u_t["style"] = H.style_lines("short", "keep it casual")
pt = H.build_prompt(u_t)
check("build_prompt: tone and instruction are in the prompt", "very short" in pt and "keep it casual" in pt)
check("build_prompt: nothing added without tone/instruction", "Tone:" not in H.build_prompt(H.understand(THREAD_MEETING, OWNER)))
seen = {}


def spy(prompt, system):
    seen["p"], seen["s"] = prompt, system
    return good


d, m = H.run(THREAD_MEETING, spy, attempts=1, owner=OWNER, tone="friendly", instructions="zyada formal nahi")
check("run: tone + instruction reach the model prompt", "warm, friendly" in seen["p"] and "zyada formal nahi" in seen["p"], seen)
check("run: SYSTEM rules are still sent with a tone", "Never invent" in seen["s"], seen["s"][:60])
# facts the user states in the instruction are allowed (they are the owner's own words)
thursday = "Hi Saima, thanks for reaching out. I am available on Thursday at 3pm for a quick call, does that suit you?"
d, m = H.run(THREAD_MEETING, scripted(thursday), attempts=1, owner=OWNER, instructions="Thursday 3pm available hoon, politely bolo")
check("run: a day/time stated in the user's instruction is accepted", d == thursday and m["source"] == "model", (d, m))
d, m = H.run(THREAD_MEETING, scripted(thursday, thursday), attempts=2, owner=OWNER)
check("run: same draft is rejected when the user did NOT state that day/time (no invention)", m["source"] == "fallback", m)
d, m = H.run(THREAD_MEETING, scripted(good), attempts=1, owner=OWNER)
check("run: old call signature (no tone/instructions) still works", d == good and m["source"] == "model", (d, m))

print(f"\n{'ALL PASS' if not FAILS else str(FAILS) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
