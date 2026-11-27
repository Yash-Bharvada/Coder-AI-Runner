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
MAX_PROCESSES = 1024
MAX_OUTPUT_FILE_BYTES = 10 * 1024 * 1024

# Multi-file request limits
MAX_FILES = 50                # maximum number of files in a single multi-file request
MAX_TOTAL_CHARS = 200_000     # aggregate char limit across all files (reuses the single-file guard)

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
    "javascript": 1024, "typescript": 1024, "go": 1536, "java": 2048,
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
    "java":       {"ext": ".java", "run": lambda f: [
        "java", "-Xms16m", "-Xmx128m", "-XX:CompressedClassSpaceSize=64m",
        "-XX:ReservedCodeCacheSize=64m", "-XX:ActiveProcessorCount=2", f,
    ]},
}

# ── Compiled languages: compile step, then run the resulting binary ──────
COMPILED_CONFIG: dict[str, dict] = {
    "c":   {"ext": ".c",   "compiler": "gcc", "binary": "app"},
    "cpp": {"ext": ".cpp", "compiler": "g++", "binary": "app"},
}


class RunRequest(BaseModel):
    language: str
    # ── Single-file mode (existing contract — unchanged) ──────────────────
    code: Optional[str] = None
    # ── Multi-file mode (new) ─────────────────────────────────────────────
    # files: mapping of relative path → file contents, e.g.
    #   { "main.py": "from helper import greet\nprint(greet())",
    #     "helper.py": "def greet(): return 'hello'" }
    # entry_point: the key in `files` that is the program entry point.
    # Exactly one of `code` or `files` must be provided; sending both or
    # neither is a 400 error.
    files: Optional[dict[str, str]] = None
    entry_point: Optional[str] = None
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


def _write_files(files: dict[str, str], work_dir: str) -> None:
    """Write all files from a multi-file request into work_dir with strict
    path-traversal validation.

    Every path is validated in two independent passes before any file is
    written:
      1. Structural check — rejects obvious `..` segments and absolute paths
         without touching the filesystem.
      2. Resolution check — resolves the joined path and confirms it is still
         contained within work_dir, catching symlink-based escapes and any
         edge case the structural check might miss.

    Raises HTTPException(400) on *any* safety violation — the entire request
    is rejected rather than silently skipping the offending path, so callers
    see a clear error rather than a partial project that fails at import time.
    """
    if len(files) > MAX_FILES:
        raise HTTPException(
            400, f"Too many files: {len(files)} exceeds the {MAX_FILES}-file limit per request."
        )

    total_chars = sum(len(v) for v in files.values())
    if total_chars > MAX_TOTAL_CHARS:
        raise HTTPException(
            413,
            f"Total source size ({total_chars} chars) exceeds the {MAX_TOTAL_CHARS}-char limit.",
        )

    resolved_work = Path(work_dir).resolve()

    for rel_path, content in files.items():
        # ── Pass 1: structural checks (no filesystem access) ─────────────

        # Reject absolute paths: Unix-style "/" or Windows drive "X:"
        if rel_path.startswith("/") or (len(rel_path) >= 2 and rel_path[1] == ":"):
            raise HTTPException(400, f"Absolute file paths are not allowed: '{rel_path}'")

        # Reject any path component that is ".." (catches "a/../b", "../b", etc.)
        path_parts = Path(rel_path).parts
        if ".." in path_parts:
            raise HTTPException(
                400, f"Path traversal (../) detected in file path: '{rel_path}'"
            )

        # ── Pass 2: resolution check (filesystem-aware) ──────────────────
        # Resolves symlinks and normalises the path, then confirms the result
        # is still under work_dir. This is the backstop against any edge case
        # the structural check above doesn't catch.
        full_path = Path(work_dir, rel_path).resolve()
        if not full_path.is_relative_to(resolved_work):
            raise HTTPException(
                400,
                f"File path '{rel_path}' resolves outside the sandbox directory. "
                "Request rejected.",
            )

        # ── Write ─────────────────────────────────────────────────────────
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")


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

    # ── Validate: exactly one of code / files must be set ────────────────
    has_code = req.code is not None
    has_files = req.files is not None

    if has_code and has_files:
        raise HTTPException(
            400, "Provide either 'code' (single-file) or 'files' (multi-file), not both."
        )
    if not has_code and not has_files:
        raise HTTPException(
            400, "One of 'code' (single-file) or 'files' (multi-file) is required."
        )

    # ════════════════════════════════════════════════════════════════════
    # SINGLE-FILE PATH — existing behaviour, unchanged
    # ════════════════════════════════════════════════════════════════════
    if has_code:
        if len(req.code) > MAX_TOTAL_CHARS:
            raise HTTPException(413, "Source too large.")

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

    # ════════════════════════════════════════════════════════════════════
    # MULTI-FILE PATH — new
    # ════════════════════════════════════════════════════════════════════

    # DEPENDENCY INSTALL: Option A — stdlib-only, no install step.
    # Multi-file projects may include a requirements.txt, but this runner does
    # not execute pip install. A cold `pip install` on shared Render free-tier
    # CPU routinely exceeds the 20–30s budget for any non-trivial package list,
    # making a half-implemented install step that times out on every real
    # project worse than clearly not supporting it yet. If requirements.txt is
    # present, we surface a visible notice in stderr instead of silently letting
    # an ImportError look like a code bug.
    # Revisit (Option B: bounded install step) when timeout ceilings are raised.

    if not req.entry_point:
        raise HTTPException(400, "'entry_point' is required when 'files' is set.")
    if req.entry_point not in req.files:
        raise HTTPException(
            400,
            f"'entry_point' ('{req.entry_point}') must be one of the keys in 'files'. "
            f"Available keys: {sorted(req.files.keys())}",
        )

    # Multi-file compiled execution is out of scope: compiling multiple C/C++
    # source files requires linking strategies not yet implemented here.
    # Return a clear 400 rather than attempting a partial compile that silently
    # ignores sibling .c/.cpp files.
    if language in COMPILED_CONFIG or language == "java":
        raise HTTPException(
            400,
            f"Multi-file compiled execution is not yet supported for '{language}'. "
            "Use 'code' (single-file) for C/C++/Java submissions.",
        )

    has_requirements_txt = "requirements.txt" in req.files

    work_dir = tempfile.mkdtemp(dir="/tmp", prefix="run_")
    try:
        # _write_files validates every path before writing anything.
        # A single bad path aborts the whole request with a 400.
        _write_files(req.files, work_dir)

        cfg = LANGUAGE_CONFIG[language]
        full_entry_path = str(Path(work_dir, req.entry_point).resolve())
        result = _execute(
            cfg["run"](full_entry_path), work_dir, req.stdin or "", timeout, MEMORY_LIMIT_MB[language],
        )

        # Option A: surface requirements.txt presence as an informational notice
        if has_requirements_txt:
            notice = (
                "[runner] requirements.txt was present but not installed — "
                "this runner executes stdlib-only multi-file projects for now. "
                "Third-party packages are not yet supported in multi-file mode.\n"
            )
            result = RunResponse(
                success=result.success,
                stdout=result.stdout,
                stderr=notice + result.stderr,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
            )

        return result

    finally:
        subprocess.run(["rm", "-rf", work_dir])
