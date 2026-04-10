"""Pure transform: AgentRun in-memory state → 5-step UI progress payload.

The frontend renders exactly five vertically-stacked steps with a spinner per
step. This module converts the runner's internal (status, phase, logs) state
into a flat, stable shape the UI can iterate over.

No state, no I/O. Safe to call on every poll.
"""
from __future__ import annotations

import time
from typing import Any

STEP_DEFS: list[tuple[str, str]] = [
    ("prepare", "Preparing"),
    ("analyze", "Analyzing"),
    ("install", "Installing"),
    ("test", "Testing"),
    ("finalize", "Finalizing"),
]

# Sentinel return values from _active_index.
_SUCCESS = -1
_FAILED = -2


def _active_index(status: str, phase: str | None) -> int:
    """Map the runner's (status, phase) to the currently-active step index,
    or a sentinel (_SUCCESS / _FAILED)."""
    if status in ("pending", "cloning"):
        return 0
    if status == "analyzing":
        return 1
    if status == "sandboxing":
        return 2
    if status == "running":
        return 3 if phase in ("run", "fix") else 2
    if status == "success":
        return _SUCCESS
    # failure, timeout, error
    return _FAILED


def _infer_failed_index(run: Any) -> int:
    """Walk the log stream to find the last stage the run was in before
    failing. Returns an index in 0..4."""
    stage_keywords = {
        "cloning": 0,
        "analyz": 1,       # "analyzing repo"
        "sandbox": 2,      # "setting up ... sandbox"
        "running": 3,
    }
    last_idx = 0
    for entry in run.logs:
        t = entry.get("type")
        if t == "status":
            msg = (entry.get("msg") or "").lower()
            for kw, idx in stage_keywords.items():
                if kw in msg:
                    last_idx = max(last_idx, idx)
                    break
        elif t == "analysis_complete":
            last_idx = max(last_idx, 2)  # moved past analyze into install
        elif t == "tool_call":
            phase = entry.get("phase")
            if phase == "run" or phase == "fix":
                last_idx = max(last_idx, 3)
            elif phase == "install":
                last_idx = max(last_idx, 2)
    return last_idx


def _overall(status: str) -> str:
    if status == "pending":
        return "pending"
    if status == "success":
        return "success"
    if status in ("failure", "timeout", "error"):
        return "failure"
    return "running"


def _extract_error(run: Any) -> dict | None:
    if run.status not in ("failure", "timeout", "error"):
        return None
    # Walk backwards for the most recent terminal event.
    for entry in reversed(run.logs):
        t = entry.get("type")
        if t in ("failure", "timeout", "error"):
            return {
                "reason": entry.get("reason") or entry.get("msg") or run.status,
                "last_error": entry.get("last_error"),
                "phase_where_failed": (
                    entry.get("phase_where_failed") or entry.get("phase")
                ),
            }
    return {"reason": run.status, "last_error": None, "phase_where_failed": None}


def compute_progress(run: Any) -> dict:
    """Return the progress payload for a given AgentRun instance."""
    steps: list[dict] = [
        {"id": sid, "label": label, "status": "pending"}
        for sid, label in STEP_DEFS
    ]

    active = _active_index(run.status, run.phase)

    current_step_id: str | None = None
    current_step_index: int | None = None

    if active == _SUCCESS:
        for s in steps:
            s["status"] = "done"
    elif active == _FAILED:
        fail_idx = _infer_failed_index(run)
        for i, s in enumerate(steps):
            if i < fail_idx:
                s["status"] = "done"
            elif i == fail_idx:
                s["status"] = "failed"
            # later steps stay pending
        current_step_id = steps[fail_idx]["id"]
        current_step_index = fail_idx
    else:
        for i in range(active):
            steps[i]["status"] = "done"
        steps[active]["status"] = "active"
        current_step_id = steps[active]["id"]
        current_step_index = active

    duration_ms = 0
    if run.started_at:
        end = run.finished_at or time.time()
        duration_ms = int((end - run.started_at) * 1000)

    return {
        "install_id": run.install_id,
        "owner": run.owner,
        "repo": run.repo,
        "overall_status": _overall(run.status),
        "current_step_id": current_step_id,
        "current_step_index": current_step_index,
        "steps": steps,
        "error": _extract_error(run),
        "result": run.result,
        "duration_ms": duration_ms,
    }
