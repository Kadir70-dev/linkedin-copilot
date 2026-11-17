"""Optional Claude engine for drafts, with the local Qwen model as backup.

Enabled only when ANTHROPIC_API_KEY is set. Any failure (no key, network,
rate limit, refusal, empty text) raises, and the caller falls back to Ollama.
Hermes validates the text whichever engine wrote it.
"""
import os

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT", "low")  # low | medium | high
CLAUDE_TIMEOUT = float(os.getenv("CLAUDE_TIMEOUT", "30") or 30)

_client = None


def enabled() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(timeout=CLAUDE_TIMEOUT, max_retries=1)
    return _client


def generate(prompt: str, system: str, max_tokens: int = 2000) -> str:
    resp = _get_client().beta.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": CLAUDE_EFFORT},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("claude refused")
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise RuntimeError(f"claude returned no text (stop_reason={resp.stop_reason})")
    return text
