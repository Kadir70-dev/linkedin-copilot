"""Live eval of the HERMES reply agent against the real local model.

Runs inside the ai-brain container (needs Ollama):
  docker compose cp bench/eval_hermes_reply.py ai-brain:/app/
  docker compose exec ai-brain python eval_hermes_reply.py
Prints one line per thread and a summary. A shipped draft must have zero
violations; "fallback" means every model draft was rejected.
"""
import time

import hermes_reply as H
import worker as W

OWNER = "Abdul Kadir Mukadam"
THREADS = [
    "Saima Shaikh 5:10 PM\nHi Abdul, we are a knitwear brand in Dubai looking for a private label partner. Are you free for a quick call on Tuesday?",
    "Omar Mahdi 4:02 PM\nCan you share the commission structure and factory rates?\nAbdul Kadir Mukadam 4:30 PM\nSure, I will add the commission structure and factory rates on the website this week.",
    "Carolina Penalba 11:20 AM\nThank you so much for the introduction! Really appreciate it.",
    "Muhammad Al-Zafar 9:15 AM\nIf you are free now let's connect on call. I want to discuss bulk t-shirt orders, around 5000 pcs.",
    "Kate Fisher 2:45 PM\nCongrats on the new role! How are you finding it so far?",
    "Romeo Nistor 6:30 PM\nHi Abdul, thanks for getting back to me. What is your MOQ and lead time for denim jackets?",
    "Hello, could you send me your company profile and catalogue?",
    "Homam Husain 1:05 PM\nokay bhai, kal baat karte hain",
]

shipped = fallback = violations = 0
for t in THREADS:
    t0 = time.time()
    gen = lambda p, s: W.ollama_generate(p, system=s, temperature=0.3, num_predict=160)
    d, m = H.run(t, gen, attempts=2, owner=OWNER)
    v = H.validate(d, H.understand(t, OWNER))
    shipped += 1
    fallback += m["source"] == "fallback"
    violations += bool(v)
    rej = "; ".join(",".join(x.get("violations", [])) or x.get("error", "") for x in m["rejected"])
    print(f"[{m['source']:8} a={m['attempt']} {time.time() - t0:5.1f}s] {d}\n    rejected: {rej or '-'}  final-violations: {v or '-'}")
print(f"\nthreads={shipped} fallback={fallback} shipped_with_violations={violations}")
