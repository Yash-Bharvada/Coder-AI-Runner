# Quick Coder — Code Runner

Hosted code-execution backend for Quick Coder. Runs Python, JavaScript,
TypeScript, Go, PHP, C, C++, Java 17+, and Bash — no Docker required on the client.

Deploys to **Render** (free tier, Docker-native, no credit card required) —
Hugging Face Spaces pulled free Docker/CPU hosting in July 2026, so this
project targets Render instead. The image itself is unchanged; only the
port handling differs (Render assigns `$PORT` dynamically).

## API

### `POST /run` — single-file mode

Run a single source string. This is the original contract and is unchanged.

```json
{
  "language": "python",
  "code": "print('hello')",
  "stdin": "",
  "timeout": 15
}
```

### `POST /run` — multi-file mode

Run a project with multiple source files. `files` maps relative paths to file
contents; `entry_point` must be one of the keys in `files` and is the file the
runtime will execute. Omit `code` entirely when using this form (sending both
or neither returns HTTP 400).

```json
{
  "language": "python",
  "files": {
    "main.py": "from helper import greet\nprint(greet())",
    "helper.py": "def greet():\n    return 'hello from helper'"
  },
  "entry_point": "main.py",
  "stdin": "",
  "timeout": 15
}
```

**Multi-file constraints:**
- Maximum **50 files** per request.
- Combined source size capped at **200 000 chars** (same limit as single-file mode).
- File paths must be relative (no leading `/` or drive letters); `..` segments are rejected with HTTP 400.
- **Python only (stdlib):** if `requirements.txt` is included in `files` it is ignored — no `pip install` step is performed. A notice appears in `stderr` rather than a silent `ImportError`. See [Known limits](#known-limits-free-tier) for context.
- **C/C++/Java multi-file** is not yet supported — use single-file `code` mode for those languages. Java uses the JDK's source-file launcher, so a public class may have any valid name.

### Response (both modes)

```json
{
  "success": true,
  "stdout": "hello from helper\n",
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
- **Hard execution ceiling: 30 s** (`MAX_TIMEOUT`). CPU time is further
  capped at 20 s via `RLIMIT_CPU`. Keep programs short on shared free-tier CPU.
- Shared, unprivileged CPU — no per-request container isolation (see
  `main.py` header comment for the reasoning). Fine for early users; revisit
  before scaling to adversarial/high-volume traffic.
- Multi-file Python projects are **stdlib-only** — third-party packages
  cannot be installed within the 20–30 s CPU budget on this tier. This is a
  deliberate posture (Option A); see the comment block at the top of the
  multi-file path in `main.py` for the full rationale and upgrade path.
- No C#, Kotlin, or Dart in this base image — see `docs/adding-more-languages.md`
  to add them (they roughly double build time and image size).
