FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Claude Code CLI for the owner-only engine (claude_cli.py). Unused unless
# CLAUDE_CODE_OAUTH_TOKEN is set.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://claude.ai/install.sh | bash \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
ENV PATH="/root/.local/bin:${PATH}"
COPY worker.py hermes_reply.py claude_llm.py claude_cli.py ./
CMD ["python", "-u", "worker.py"]
