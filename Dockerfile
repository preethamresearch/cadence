# Cadence demo server.
#
# Cloud Run is a good fit despite the WebSocket: sessions are minutes long, not
# hours, and request timeout can be raised to cover them. Any container host
# that supports WebSockets works the same way.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so application edits do not invalidate the layer.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e ".[app]"

COPY app/ ./app/

# Cloud Run injects PORT; default for local runs.
ENV PORT=8080
EXPOSE 8080

# One worker: the recorder holds per-session state in memory, and a second
# worker would split a conversation's turns across processes.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1
