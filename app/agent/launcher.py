"""Spawn + manage long-lived subprocess runs of installed repos.

This is the "Run" / "Launch" capability — distinct from the install loop.
The install loop smoke-tests a server for 5 seconds then kills it. This
module keeps a server alive, tails its stdout to find the URL it's listening
on, and hands that URL back to the frontend so a browser tab can be opened.

State is in-memory only (lost on uvicorn restart). For v1 we allow at most
one active run per install; starting a second one when the first is still
running is a no-op that returns the existing handle.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# URL / port detection patterns
# --------------------------------------------------------------------------

# Strong match: a literal localhost/loopback URL in output. This is what
# most dev servers print — "Listening on http://localhost:3000", etc.
_URL_RE = re.compile(
    r"(https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d{2,5})?(?:/\S*)?)",
    re.IGNORECASE,
)

# Weak match: port number near a "listen"/"ready"/"serving" keyword. Catches
# structured logs like `level=INFO msg="starting" port=8080` or
# `ready - started server on 0.0.0.0:3000` without an http:// prefix.
_PORT_NEAR_KEYWORD_RE = re.compile(
    r"(?:listen(?:ing)?|serv(?:e|ing|er)|running|starting|ready|bound|"
    r"address|http|accepting).{0,60}?(?<![0-9.])(\d{4,5})(?![0-9])",
    re.IGNORECASE,
)

# Bare port assignment (as a last resort)
_BARE_PORT_RE = re.compile(r"(?:^|[\s=:])port[\s=:]+(\d{4,5})\b", re.IGNORECASE)

LOG_TAIL_SIZE = 400
URL_DETECT_TIMEOUT = 30.0  # wait at most this long for a URL to appear

# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class RunHandle:
    run_id: str
    install_id: str
    command: str
    cwd: Path
    pid: int
    status: str = "starting"  # starting | running | exited | crashed | stopped
    url: Optional[str] = None
    port: Optional[int] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    exit_code: Optional[int] = None
    log_tail: deque = field(default_factory=lambda: deque(maxlen=LOG_TAIL_SIZE))
    process: Optional[subprocess.Popen] = None
    _stop_flag: threading.Event = field(default_factory=threading.Event)
    _reader_thread: Optional[threading.Thread] = None


# In-memory registries.
_runs: dict[str, RunHandle] = {}                 # run_id → handle
_active_by_install: dict[str, str] = {}          # install_id → currently-active run_id


# --------------------------------------------------------------------------
# Command normalisation
# --------------------------------------------------------------------------

_TIMEOUT_PREFIX_RE = re.compile(r"^\s*timeout\s+\d+\s+", re.IGNORECASE)
_HELP_SUFFIX_RE = re.compile(r"\s+(-h|--help|-V|--version|-v)\s*$", re.IGNORECASE)
_SMOKE_SUFFIX_RE = re.compile(
    r"\s*(?:2>&1)?\s*(?:\|\|\s*true\s*)+$", re.IGNORECASE
)


def normalise_command(cmd: str) -> str:
    """Strip smoke-test artifacts that often end up in run_command.

    The install agent sometimes reports the `timeout 10 ... --help || true`
    command it used to verify the app as the production run command. We
    peel those off so the real app actually launches.
    """
    cmd = cmd.strip()
    cmd = _TIMEOUT_PREFIX_RE.sub("", cmd)
    cmd = _SMOKE_SUFFIX_RE.sub("", cmd)
    cmd = _HELP_SUFFIX_RE.sub("", cmd)
    return cmd.strip()


# --------------------------------------------------------------------------
# URL detection from a line of output
# --------------------------------------------------------------------------

def _extract_url_port(line: str) -> tuple[Optional[str], Optional[int]]:
    m = _URL_RE.search(line)
    if m:
        url = m.group(1).rstrip(".,;)")
        port_m = re.search(r":(\d{2,5})(?!\d)", url)
        port = int(port_m.group(1)) if port_m else None
        # Default port guess for bare localhost
        if port is None:
            port = 80 if url.startswith("http://") and not url.startswith("https://") else 443
        return url, port

    m = _PORT_NEAR_KEYWORD_RE.search(line)
    if m:
        port = int(m.group(1))
        if 1024 <= port <= 65535:
            return f"http://localhost:{port}", port

    m = _BARE_PORT_RE.search(line)
    if m:
        port = int(m.group(1))
        if 1024 <= port <= 65535:
            return f"http://localhost:{port}", port

    return None, None


# --------------------------------------------------------------------------
# Reader thread — drains stdout, looks for the URL, watches for exit
# --------------------------------------------------------------------------

def _reader(handle: RunHandle) -> None:
    proc = handle.process
    assert proc is not None and proc.stdout is not None

    try:
        for raw in iter(proc.stdout.readline, b""):
            if handle._stop_flag.is_set():
                break
            try:
                line = raw.decode("utf-8", errors="replace").rstrip()
            except Exception:
                continue
            if not line:
                continue
            handle.log_tail.append({"ts": time.time(), "line": line})
            if handle.url is None:
                url, port = _extract_url_port(line)
                if url and port:
                    # Always use the root URL — the raw match might be an
                    # internal API path (e.g. /__openclaw__/canvas/) that
                    # requires auth. The main UI is at the root.
                    handle.url = f"http://localhost:{port}/"
                    handle.port = port
                    handle.status = "running"
                    log.info(
                        "[run %s] url detected: %s (from: %s)",
                        handle.run_id, handle.url, line[:120],
                    )
    except Exception as e:
        log.warning("[run %s] reader thread error: %s", handle.run_id, e)

    # stdout closed — process has exited (or is about to).
    rc = proc.poll()
    if rc is None:
        try:
            rc = proc.wait(timeout=2)
        except Exception:
            rc = -1
    handle.exit_code = rc
    handle.finished_at = time.time()
    if handle.status == "stopped":
        pass  # explicitly stopped — keep that label
    elif rc == 0:
        handle.status = "exited"
    else:
        handle.status = "crashed"
    log.info(
        "[run %s] process ended rc=%s status=%s", handle.run_id, rc, handle.status,
    )


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def start_run(
    install_id: str,
    command: str,
    cwd: Path,
    sandbox_env: dict[str, str],
    path_prepend: list[str],
    secrets: Optional[dict[str, str]] = None,
    wait_for_url: float = URL_DETECT_TIMEOUT,
) -> RunHandle:
    """Spawn `command` inside `cwd`, block up to `wait_for_url` for a URL to
    appear in stdout, then return the handle.

    If an existing run for this install_id is already in state
    {starting, running}, returns that existing handle instead of starting a
    new one.
    """
    # Dedup active run
    existing_id = _active_by_install.get(install_id)
    if existing_id:
        existing = _runs.get(existing_id)
        if existing and existing.status in ("starting", "running"):
            log.info(
                "[run] dedup: returning existing run %s for install %s",
                existing_id, install_id,
            )
            return existing

    normalised = normalise_command(command)
    run_id = uuid.uuid4().hex[:12]

    env = os.environ.copy()
    env.update(sandbox_env)
    if path_prepend:
        env["PATH"] = os.pathsep.join(path_prepend + [env.get("PATH", "")])
    if secrets:
        env.update(secrets)
    # Force line-buffered output where possible.
    env.setdefault("PYTHONUNBUFFERED", "1")
    # Don't auto-open the user's browser when a Vite/CRA dev server starts —
    # the launcher already extracts the URL and the frontend exposes it.
    env.setdefault("BROWSER", "none")

    log.info("[run %s] starting %r in %s", run_id, normalised, cwd)

    try:
        proc = subprocess.Popen(
            normalised,
            shell=True,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge stderr into stdout
            bufsize=1,
            # New process group so we can signal the whole tree on stop.
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )
    except Exception as e:
        log.exception("[run %s] spawn failed", run_id)
        handle = RunHandle(
            run_id=run_id,
            install_id=install_id,
            command=normalised,
            cwd=cwd,
            pid=-1,
            status="crashed",
            exit_code=-1,
            finished_at=time.time(),
        )
        handle.log_tail.append({"ts": time.time(), "line": f"spawn failed: {e}"})
        _runs[run_id] = handle
        _active_by_install[install_id] = run_id
        return handle

    handle = RunHandle(
        run_id=run_id,
        install_id=install_id,
        command=normalised,
        cwd=cwd,
        pid=proc.pid,
        process=proc,
    )
    reader = threading.Thread(target=_reader, args=(handle,), daemon=True)
    handle._reader_thread = reader
    reader.start()

    _runs[run_id] = handle
    _active_by_install[install_id] = run_id

    # Block until we detect a URL OR the process exits, up to wait_for_url.
    deadline = time.time() + wait_for_url
    while time.time() < deadline:
        if handle.url is not None:
            break
        if proc.poll() is not None:
            # Died early
            time.sleep(0.2)  # let the reader thread flush last lines
            break
        time.sleep(0.1)

    return handle


def stop_run(run_id: str, grace: float = 3.0) -> bool:
    """Terminate a running process. Returns True if a run was found."""
    handle = _runs.get(run_id)
    if not handle:
        return False
    proc = handle.process
    if proc is None or proc.poll() is not None:
        handle.status = "stopped"
        return True

    handle._stop_flag.set()
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except Exception:
            pass

    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass

    handle.status = "stopped"
    handle.finished_at = time.time()
    return True


def get_run(run_id: str) -> RunHandle | None:
    return _runs.get(run_id)


def get_run_for_install(install_id: str) -> RunHandle | None:
    run_id = _active_by_install.get(install_id)
    if not run_id:
        return None
    return _runs.get(run_id)


def stop_runs_for_install(install_id: str) -> int:
    """Stop all runs associated with an install. Called by DELETE install."""
    stopped = 0
    for rid, handle in list(_runs.items()):
        if handle.install_id == install_id and handle.status in ("starting", "running"):
            stop_run(rid)
            stopped += 1
    return stopped
