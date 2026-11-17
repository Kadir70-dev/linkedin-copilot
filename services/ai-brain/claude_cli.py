"""Owner-only Claude engine via the Claude Code CLI (`claude -p`).

Uses the account owner's own Claude subscription, so it is used ONLY for
requests the gateway marks as coming from the owner's access key (personal
use). Everyone else gets the local model. Auth is CLAUDE_CODE_OAUTH_TOKEN
(from `claude setup-token`). Any failure raises; the caller falls back.
"""
import os
import subprocess
import tempfile

CLI = os.getenv("CLAUDE_CLI_BIN", "claude")
CLI_MODEL = os.getenv("CLAUDE_CLI_MODEL", "sonnet")
CLI_TIMEOUT = float(os.getenv("CLAUDE_CLI_TIMEOUT", "60") or 60)
_WORKDIR = tempfile.mkdtemp(prefix="lc-claude-")  # empty dir: no CLAUDE.md, no project files


def enabled() -> bool:
    return bool(os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "").strip())


def generate(prompt: str, system: str) -> str:
    cmd = [CLI, "-p", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
           "--setting-sources", "", "--model", CLI_MODEL, "--output-format", "text",
           "--system-prompt", system]
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # never bill the API key here
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                       timeout=CLI_TIMEOUT, cwd=_WORKDIR, env=env)
    out = (p.stdout or "").strip()
    if p.returncode != 0 or not out:
        raise RuntimeError(f"claude cli exit={p.returncode}: {(p.stderr or out)[:160]}")
    return out
