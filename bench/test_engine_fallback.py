"""Engine routing: Claude first when ANTHROPIC_API_KEY is set, local Qwen on any
Claude failure, Qwen only when no key. No network: both engines are stubbed.
Run from the repo root (needs the ai-brain deps, e.g. inside its image)."""
import os
import sys

os.environ.setdefault("REDIS_ADDR", "localhost:6379")
os.environ["RAM_MIN_FREE_MB"] = "0"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "ai-brain"))
import claude_llm  # noqa: E402
import worker as W  # noqa: E402

fails = 0
calls = []
W.ollama_generate = lambda prompt, **kw: calls.append("qwen") or "qwen text"


def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    fails += 0 if cond else 1


os.environ.pop("ANTHROPIC_API_KEY", None)
calls.clear()
check("no key -> qwen", W.generate_with_fallback("p", "s", 0.3, 100, W.BASE_MODEL) == "qwen text" and calls == ["qwen"])

os.environ["ANTHROPIC_API_KEY"] = "test"
claude_llm.generate = lambda prompt, system, max_tokens=2000: calls.append("claude") or "claude text"
calls.clear()
check("key -> claude first", W.generate_with_fallback("p", "s", 0.3, 100, W.BASE_MODEL) == "claude text" and calls == ["claude"])


def boom(prompt, system, max_tokens=2000):
    calls.append("claude")
    raise RuntimeError("rate limited")


claude_llm.generate = boom
calls.clear()
check("claude error -> qwen backup", W.generate_with_fallback("p", "s", 0.3, 100, W.BASE_MODEL) == "qwen text" and calls == ["claude", "qwen"])
import claude_cli  # noqa: E402
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "test"
claude_cli.generate = lambda prompt, system: calls.append("cli") or "cli text"
W.OWNER_REQUEST = True
calls.clear()
check("owner request -> claude cli", W.generate_with_fallback("p", "s", 0.3, 100, W.BASE_MODEL) == "cli text" and calls == ["cli"])
W.OWNER_REQUEST = False
calls.clear()
check("non-owner request never uses the owner's plan", W.generate_with_fallback("p", "s", 0.3, 100, W.BASE_MODEL) == "qwen text" and calls == ["qwen"])


def cli_boom(prompt, system):
    calls.append("cli")
    raise RuntimeError("limit reached")


claude_cli.generate = cli_boom
W.OWNER_REQUEST = True
calls.clear()
check("owner + cli failure -> qwen backup", W.generate_with_fallback("p", "s", 0.3, 100, W.BASE_MODEL) == "qwen text" and calls == ["cli", "qwen"])
print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
