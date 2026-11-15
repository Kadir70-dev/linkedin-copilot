FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY worker.py hermes_reply.py claude_llm.py ./
CMD ["python", "-u", "worker.py"]
