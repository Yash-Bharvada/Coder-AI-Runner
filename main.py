# main.py
# Quick Coder — Code Runner Space
#
# Replaces code_runner.py's `_docker_wrap()` (which needs a Docker daemon on
# whatever machine runs it) with a single hosted /run endpoint. Your app now
# calls this Space over HTTPS instead of shelling out to `docker run` locally.
#
# Isolation model: since this container itself can't launch nested containers
# (Spaces gives no privileged/Docker-socket access), each request gets its own
# throwaway /tmp directory and runs under strict CPU/memory/process limits with
# a hard timeout, instead of its own OS-level container. This is weaker than
# true per-job container isolation — treat it as a hobby/early-stage tier and
# revisit (e.g. self-hosted Piston/Judge0, or a paid sandbox API) once you have
# real traffic or untrusted-at-scale usage.

import os
import resource
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Quick Coder — Code Runner")

# TODO: replace "*" with your actual app's origin(s) before going live —
# wide-open CORS on a code-execution endpoint is fine for local testing only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_OUTPUT_CHARS = 20_000
DEFAULT_TIMEOUT = 15   # seconds
MAX_TIMEOUT = 30        # hard ceiling — keep runs short on shared free-tier CPU
CPU_TIME_LIMIT_S = 20
MAX_PROCESSES = 64
MAX_OUTPUT_FILE_BYTES = 10 * 1024 * 1024

# RLIMIT_AS caps *reserved* virtual address space, not actual usage — and
# some runtimes reserve a large chunk upfront just to start up (V8/Node
# fails below ~1GB reserved even though it never touches most of it; Go's
# runtime does something similar). So the cap is set per-language rather
# than as one global number — tight for plain interpreters/compiled
# binaries, looser for runtimes with big startup reservations. Node's
# actual heap usage is separately capped via --max-old-space-size, since
# RLIMIT_AS alone doesn't meaningfully constrain what V8 can allocate.
MEMORY_LIMIT_MB: dict[str, int] = {
    "python": 512, "bash": 512, "php": 512, "c": 512, "cpp": 512,
    "javascript": 1024, "typescript": 1024, "go": 1536,
}
NODE_HEAP_LIMIT_MB = 512

# ── Interpreted / directly-run languages ─────────────────────────────────
LANGUAGE_CONFIG: dict[str, dict] = {
    "python":     {"ext": ".py",  "run": lambda f: [sys.executable, f]},
    "javascript": {"ext": ".js",  "run": lambda f: ["node", f"--max-old-space-size={NODE_HEAP_LIMIT_MB}", f]},
    "typescript": {"ext": ".ts",  "run": lambda f: ["npx", "--no-install", "tsx", f]},
    "go":         {"ext": ".go",  "run": lambda f: ["go", "run", f]},
    "php":        {"ext": ".php", "run": lambda f: ["php", f]},
    "bash":       {"ext": ".sh",  "run": lambda f: ["bash", f]},
}

# ── Compiled languages: compile step, then run the resulting binary ──────
COMPILED_CONFIG: dict[str, dict] = {
    "c":   {"ext": ".c",   "compiler": "gcc", "binary": "app"},
    "cpp": {"ext": ".cpp", "compiler": "g++", "binary": "app"},
}


class RunRequest(BaseModel):
    language: str
    code: str
    stdin: Optional[str] = ""
    timeout: Optional[int] = DEFAULT_TIMEOUT


class RunResponse(BaseModel):
    success: bool
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False


def _make_limiter(memory_mb: int):
    """Returns a preexec_fn (runs in the child, right before exec) that caps
    CPU time, virtual address space, process count, and max file size — so
    one submission can't stall or starve the whole Space."""
    def _limit_resources() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_TIME_LIMIT_S, CPU_TIME_LIMIT_S))
        resource.setrlimit(
            resource.RLIMIT_AS, (memory_mb * 1024 * 1024, memory_mb * 1024 * 1024),
        )
        resource.setrlimit(resource.RLIMIT_NPROC, (MAX_PROCESSES, MAX_PROCESSES))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_FILE_BYTES, MAX_OUTPUT_FILE_BYTES))
    return _limit_resources


def _truncate(text: str) -> str:
    if len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS] + "\n… (truncated)"
    return text


def _execute(cmd: list[str], cwd: str, stdin: str, timeout: int, memory_mb: int) -> RunResponse:
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            preexec_fn=_make_limiter(memory_mb),
        )
        return RunResponse(
            success=result.returncode == 0,
            stdout=_truncate(result.stdout),
            stderr=_truncate(result.stderr),
            exit_code=result.returncode,
        )
    except subprocess.TimeoutExpired:
        return RunResponse(
            success=False,
            stdout="",
            stderr=f"Timed out after {timeout}s — check for infinite loops.",
            exit_code=-1,
            timed_out=True,
        )
    except FileNotFoundError as exc:
        return RunResponse(
            success=False, stdout="", stderr=f"Runtime not found: {exc}", exit_code=-1,
        )
    except MemoryError:
        return RunResponse(
            success=False, stdout="", stderr="Process exceeded the memory limit.", exit_code=-1,
        )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/languages")
def languages():
    return {"supported": sorted(list(LANGUAGE_CONFIG) + list(COMPILED_CONFIG))}


@app.post("/run", response_model=RunResponse)
def run_code(req: RunRequest):
    language = req.language.lower().strip()
    timeout = min(req.timeout or DEFAULT_TIMEOUT, MAX_TIMEOUT)

    if language not in LANGUAGE_CONFIG and language not in COMPILED_CONFIG:
        supported = sorted(list(LANGUAGE_CONFIG) + list(COMPILED_CONFIG))
        raise HTTPException(400, f"Unsupported language '{language}'. Supported: {supported}")

    if len(req.code) > 200_000:
        raise HTTPException(413, "Source too large.")

    # Every request gets its own throwaway directory under /tmp, wiped after.
    work_dir = tempfile.mkdtemp(dir="/tmp", prefix="run_")
    try:
        if language in COMPILED_CONFIG:
            cfg = COMPILED_CONFIG[language]
            src_path = os.path.join(work_dir, f"main{cfg['ext']}")
            Path(src_path).write_text(req.code, encoding="utf-8")
            binary_path = os.path.join(work_dir, cfg["binary"])

            compile_result = subprocess.run(
                [cfg["compiler"], src_path, "-o", binary_path],
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if compile_result.returncode != 0:
                return RunResponse(
                    success=False,
                    stdout="",
                    stderr=_truncate(compile_result.stderr),
                    exit_code=compile_result.returncode,
                )
            return _execute(
                [binary_path], work_dir, req.stdin or "", timeout, MEMORY_LIMIT_MB[language],
            )

        cfg = LANGUAGE_CONFIG[language]
        src_path = os.path.join(work_dir, f"main{cfg['ext']}")
        Path(src_path).write_text(req.code, encoding="utf-8")
        return _execute(
            cfg["run"](src_path), work_dir, req.stdin or "", timeout, MEMORY_LIMIT_MB[language],
        )

    finally:
        subprocess.run(["rm", "-rf", work_dir])
