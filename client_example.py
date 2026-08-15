# client_example.py
# Drop-in replacement pattern for code_runner.py's `_run_generic()` /
# `_docker_wrap()` path. Instead of shelling out to a local `docker run`,
# this calls the hosted runner's /run endpoint (deployed on Render). Point
# RUNNER_URL at your deployed service once it's live.
#
# Wire this into server/gateway.py (or wherever code_runner.py is currently
# imported) for the non-Python languages; keep Python running locally/via
# Pyodide as discussed.

import os
import requests

RUNNER_URL = os.environ.get("CODE_RUNNER_URL", "https://<your-service-name>.onrender.com")
REQUEST_TIMEOUT_BUFFER = 10  # give the HTTP call a bit longer than the run itself


def run_remote(
    language: str,
    code: str | None = None,
    stdin: str = "",
    timeout: int = 15,
    *,
    files: dict[str, str] | None = None,
    entry_point: str | None = None,
) -> dict:
    """Calls the hosted runner. Returns the same shape as RunResult in
    code_runner.py (success, stdout, stderr, exit_code, timed_out).

    Single-file mode (existing behaviour — unchanged):
        run_remote("python", code="print('hello')")

    Multi-file mode (new):
        run_remote(
            "python",
            files={
                "main.py": "from helper import greet\\nprint(greet())",
                "helper.py": "def greet():\\n    return 'hello from helper'",
            },
            entry_point="main.py",
        )

    Exactly one of `code` or `files` must be provided — the runner returns
    HTTP 400 if both or neither are set.  This function enforces the same
    constraint locally so callers get a clear error before hitting the network.

    NOTE on multi-file Python: only stdlib imports are supported for now.
    If requirements.txt is included in `files`, the runner will still execute
    the project but will surface a notice in stderr explaining that third-party
    packages are not installed.  See Option A comment in main.py for rationale.
    """
    has_code = code is not None
    has_files = files is not None

    if has_code and has_files:
        raise ValueError("Provide either 'code' (single-file) or 'files' (multi-file), not both.")
    if not has_code and not has_files:
        raise ValueError("One of 'code' (single-file) or 'files' (multi-file) is required.")

    # Build the payload — omit the key entirely rather than sending null/""
    # so the runner's Pydantic model sees None (not an empty string) and the
    # mutual-exclusion validation works correctly.
    if has_code:
        payload: dict = {"language": language, "code": code, "stdin": stdin, "timeout": timeout}
    else:
        if not entry_point:
            raise ValueError("'entry_point' is required when 'files' is set.")
        payload = {
            "language": language,
            "files": files,
            "entry_point": entry_point,
            "stdin": stdin,
            "timeout": timeout,
        }

    try:
        resp = requests.post(
            f"{RUNNER_URL}/run",
            json=payload,
            timeout=timeout + REQUEST_TIMEOUT_BUFFER,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.Timeout:
        return {
            "success": False, "stdout": "", "exit_code": -1, "timed_out": True,
            "stderr": f"Runner did not respond within {timeout + REQUEST_TIMEOUT_BUFFER}s.",
        }
    except requests.exceptions.RequestException as exc:
        return {
            "success": False, "stdout": "", "exit_code": -1, "timed_out": False,
            "stderr": f"Could not reach code runner: {exc}",
        }


def wake_runner() -> bool:
    """Call this when the user opens the app (before they hit Run) to absorb
    the free-tier cold-start delay ahead of time instead of during a run."""
    try:
        r = requests.get(f"{RUNNER_URL}/health", timeout=60)
        return r.ok
    except requests.exceptions.RequestException:
        return False


if __name__ == "__main__":
    print("Waking runner…", wake_runner())

    # ── Single-file example (existing behaviour) ──────────────────────────
    result = run_remote("python", code="print('hello from single-file mode')")
    print("single-file:", result)

    # ── Multi-file example (new) ───────────────────────────────────────────
    result = run_remote(
        "python",
        files={
            "main.py": "from helper import greet\nprint(greet())",
            "helper.py": "def greet():\n    return 'hello from helper module'",
        },
        entry_point="main.py",
    )
    print("multi-file:", result)
