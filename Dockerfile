# Quick Coder — Code Runner
# Runs on Render (Docker web service). One shared, unprivileged container —
# no nested `docker run`, since the host doesn't grant privileged access.
# Isolation between runs comes from: per-request temp dirs, resource limits
# (see main.py), and short timeouts — NOT from container-per-execution.

FROM python:3.11-slim

# ---- System + language runtimes (installed as root, before switching user) --
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        default-jdk-headless \
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

WORKDIR /app

# ---- Python deps installed as root, into the image's normal site-packages --
# ---- (NOT into /tmp: most PaaS hosts — Render included — mount a fresh,   --
# ---- empty /tmp at container start, separate from whatever was in /tmp    --
# ---- during the build. Anything installed there at build time is gone by --
# ---- the time the container actually runs.)                              --
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# ---- Non-root user for running the app — created after deps are already  --
# ---- installed system-wide, so it just needs read access, not to own them --
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# ---- Only /tmp is guaranteed writable at *runtime* by this user, so any   --
# ---- tool that still needs a writable cache dir (e.g. go build cache,     --
# ---- npm) gets pointed there — just never Python's own installed packages --
ENV HOME=/tmp \
    GOCACHE=/tmp/go-cache \
    GOPATH=/tmp/go \
    GO111MODULE=off \
    NPM_CONFIG_CACHE=/tmp/npm-cache \
    XDG_CACHE_HOME=/tmp/.cache \
    PYTHONUNBUFFERED=1

# Render assigns the port dynamically via $PORT — shell form so it expands
# (defaults to 7860 if unset, so this still works unchanged elsewhere too)
EXPOSE 7860
CMD python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-7860}
