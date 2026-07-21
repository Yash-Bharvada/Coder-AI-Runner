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


def run_remote(language: str, code: str, stdin: str = "", timeout: int = 15) -> dict:
    """Calls the hosted runner Space. Returns the same shape as RunResult
    in code_runner.py (success, stdout, stderr, exit_code, timed_out)."""
    try:
        resp = requests.post(
            f"{RUNNER_URL}/run",
            json={"language": language, "code": code, "stdin": stdin, "timeout": timeout},
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
    result = run_remote("python", "print('hello from the Space')")
    print(result)
