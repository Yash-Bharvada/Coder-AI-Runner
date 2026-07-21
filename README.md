# Quick Coder — Code Runner

Hosted code-execution backend for Quick Coder. Runs Python, JavaScript,
TypeScript, Go, PHP, C, C++, and Bash — no Docker required on the client.

Deploys to **Render** (free tier, Docker-native, no credit card required) —
Hugging Face Spaces pulled free Docker/CPU hosting in July 2026, so this
project targets Render instead. The image itself is unchanged; only the
port handling differs (Render assigns `$PORT` dynamically).

## API

**`POST /run`**
```json
{
  "language": "python",
  "code": "print('hello')",
  "stdin": "",
  "timeout": 15
}
```
Response:
```json
{
  "success": true,
  "stdout": "hello\n",
  "stderr": "",
  "exit_code": 0,
  "timed_out": false
}
```

**`GET /languages`** — list of supported language keys.
**`GET /health`** — liveness check (also useful to "wake up" the service
before the first real request, since the free tier sleeps after inactivity).

## Deploying (Render)

1. Push this folder to a GitHub repo (Render deploys from a git repo, not a
   direct upload).
2. At render.com → **New** → **Web Service** → connect that repo.
3. Render auto-detects the `Dockerfile`. Choose the **Free** instance type.
4. No build command / start command needed — the `Dockerfile`'s `CMD`
   handles it. Click **Create Web Service**.
5. First build takes a few minutes (Node.js + Go + build-essential install).
   Once live, your endpoint is: `https://<your-service-name>.onrender.com/run`

## Known limits (free tier)

- Spins down after ~15 min of inactivity; first request after that has a
  cold-start delay (tens of seconds). Call `/health` first to "warm up"
  before showing the Run button as ready.
- Shared, unprivileged CPU — no per-request container isolation (see
  `main.py` header comment for the reasoning). Fine for early users; revisit
  before scaling to adversarial/high-volume traffic.
- No C#, Kotlin, or Dart in this base image — see `docs/adding-more-languages.md`
  to add them (they roughly double build time and image size).
