# Quick Coder — Code Runner Space
# Runs on Hugging Face Spaces (Docker SDK). One shared, unprivileged container —
# no nested `docker run`, since Spaces containers don't get privileged access.
# Isolation between runs comes from: per-request temp dirs, resource limits
# (see main.py), and short timeouts — NOT from container-per-execution.

FROM python:3.11-slim

# ---- System + language runtimes (installed as root, before switching user) --
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        golang \
        php-cli \
        curl \
        ca-certificates \
        gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g tsx \
    && apt-get purge -y gnupg curl \
    && rm -rf /var/lib/apt/lists/*

# ---- Non-root user — required by Spaces, container refuses root at runtime --
RUN useradd -m -u 1000 appuser
USER appuser
WORKDIR /app

# ---- Spaces only guarantees /tmp is writable at runtime, so every tool's --
# ---- cache/home dir gets pointed there instead of the default ~/.cache etc --
ENV HOME=/tmp \
    GOCACHE=/tmp/go-cache \
    GOPATH=/tmp/go \
    GO111MODULE=off \
    NPM_CONFIG_CACHE=/tmp/npm-cache \
    XDG_CACHE_HOME=/tmp/.cache \
    PYTHONUNBUFFERED=1 \
    PATH="/tmp/.local/bin:${PATH}"

COPY --chown=appuser:appuser requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=appuser:appuser main.py .

# Render assigns the port dynamically via $PORT — shell form so it expands
# (defaults to 7860 if unset, so this still works unchanged on HF Spaces too)
EXPOSE 7860
CMD python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-7860}
