"""Tool implementations exposed to the LLM.

Every file-touching tool routes through sandbox.safe_path(); bash inherits
its cwd from workdir so the LLM can't wander outside. Outputs are trimmed
to keep the LLM's context manageable.
"""
from __future__ import annotations

import difflib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .sandbox import SKIP_DIRS, safe_path

# Per-tool output caps — keeps the LLM context from blowing up.
BASH_OUTPUT_CAP = 4000
FILE_READ_CAP = 8000
LIST_FILES_CAP = 150


@dataclass
class ToolContext:
    """State shared across tool calls within a single AgentRun."""
    workdir: Path
    sandbox_env: dict[str, str]  # from LanguageAdapter.bootstrap_sandbox
    path_prepend: list[str]
    secrets: dict[str, str]
    default_timeout: int = 60
    max_timeout: int = 300  # raised from 120 for rust first-build


# -------------------- tool implementations --------------------

def _trim(s: str, cap: int) -> str:
    if len(s) <= cap:
        return s
    head = s[: cap - 200]
    tail = s[-150:]
    return f"{head}\n...[trimmed {len(s) - cap + 350} chars]...\n{tail}"


def bash(ctx: ToolContext, command: str, timeout: int | None = None, phase: str | None = None) -> dict:
    """Run a shell command in workdir with sandbox env + secrets injected.

    Uses start_new_session=True so the entire process tree (shell + any children
    it spawns, like `node server.js`) is in a dedicated session. On timeout we
    kill the whole session group, preventing orphaned child processes that would
    keep stdout open and hang the read forever.
    """
    import signal

    timeout = min(timeout or ctx.default_timeout, ctx.max_timeout)
    env = os.environ.copy()
    env.update(ctx.sandbox_env)
    if ctx.path_prepend:
        env["PATH"] = os.pathsep.join(ctx.path_prepend + [env.get("PATH", "")])
    env.update(ctx.secrets)
    # Suppress dev-server browser auto-open during smoke tests (CRA, Vite, etc.).
    # The user's real browser shouldn't pop up just because the agent ran `yarn start`.
    env.setdefault("BROWSER", "none")
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(ctx.workdir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout_b, stderr_b = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Capture whatever stdout/stderr was buffered BEFORE killing.
            # This is critical: for servers, the "Listening on http://..."
            # line is in this buffer. Without it, the agent can't see the
            # server started.
            partial_stdout = b""
            partial_stderr = b""
            try:
                # Non-blocking read of whatever's in the pipe buffer.
                import select
                for pipe, buf_name in [(proc.stdout, "stdout"), (proc.stderr, "stderr")]:
                    if pipe and select.select([pipe], [], [], 0.1)[0]:
                        chunk = os.read(pipe.fileno(), 65536)
                        if buf_name == "stdout":
                            partial_stdout = chunk
                        else:
                            partial_stderr = chunk
            except Exception:
                pass

            # Kill the entire process group (shell + children).
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
            # Close pipes to prevent leaks.
            for pipe in (proc.stdout, proc.stderr):
                if pipe:
                    try:
                        pipe.close()
                    except Exception:
                        pass

            stdout_text = partial_stdout.decode("utf-8", errors="replace")
            return {
                "exit_code": -1,
                "stdout": _trim(stdout_text, BASH_OUTPUT_CAP),
                "stderr": f"<timeout after {timeout}s>",
                "phase": phase,
                "note": (
                    f"Command ran for {timeout}s without crashing, then was killed. "
                    "If this is a server/daemon, that means it STARTED SUCCESSFULLY — "
                    "do NOT retry. Call report_success with this as the run_command."
                ),
            }
        return {
            "exit_code": proc.returncode,
            "stdout": _trim(stdout_b.decode("utf-8", errors="replace"), BASH_OUTPUT_CAP),
            "stderr": _trim(stderr_b.decode("utf-8", errors="replace"), BASH_OUTPUT_CAP),
            "phase": phase,
        }
    except Exception as e:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"<bash error: {type(e).__name__}: {e}>",
            "phase": phase,
        }


def read_file(ctx: ToolContext, path: str) -> dict:
    full = safe_path(ctx.workdir, path)
    if full is None:
        return {"ok": False, "error": f"path escapes workdir: {path}"}
    if not full.exists():
        return {"ok": False, "error": f"file not found: {path}"}
    if not full.is_file():
        return {"ok": False, "error": f"not a file: {path}"}
    try:
        data = full.read_text(errors="replace")
    except Exception as e:
        return {"ok": False, "error": f"read error: {e}"}
    return {
        "ok": True,
        "path": path,
        "size": len(data),
        "content": _trim(data, FILE_READ_CAP),
    }


def list_files(ctx: ToolContext, path: str = ".") -> dict:
    full = safe_path(ctx.workdir, path)
    if full is None:
        return {"ok": False, "error": f"path escapes workdir: {path}"}
    if not full.exists():
        return {"ok": False, "error": f"not found: {path}"}
    if not full.is_dir():
        return {"ok": False, "error": f"not a directory: {path}"}
    entries: list[str] = []
    for child in sorted(full.iterdir()):
        if child.name in SKIP_DIRS:
            continue
        kind = "d" if child.is_dir() else "f"
        entries.append(f"{kind} {child.name}")
        if len(entries) >= LIST_FILES_CAP:
            entries.append(f"... [truncated at {LIST_FILES_CAP}]")
            break
    return {"ok": True, "path": path, "entries": entries}


def edit_file(ctx: ToolContext, path: str, old_string: str, new_string: str) -> dict:
    """Replace a unique substring. Fails informatively if ambiguous or missing."""
    full = safe_path(ctx.workdir, path)
    if full is None:
        return {"ok": False, "error": f"path escapes workdir: {path}"}
    if not full.exists() or not full.is_file():
        return {"ok": False, "error": f"file not found: {path}"}
    try:
        text = full.read_text(errors="replace")
    except Exception as e:
        return {"ok": False, "error": f"read error: {e}"}

    count = text.count(old_string)
    if count == 0:
        # Fuzzy-match: find the 3 closest lines to the first line of old_string.
        needle = old_string.splitlines()[0] if old_string else ""
        lines = text.splitlines()
        matches = difflib.get_close_matches(needle, lines, n=3, cutoff=0.4)
        hinted: list[str] = []
        for m in matches:
            idx = lines.index(m)
            hinted.append(f"L{idx + 1}: {m}")
        return {
            "ok": False,
            "error": f"old_string not found in {path}",
            "closest_matches": hinted,
            "hint": "re-read the file with read_file and copy an exact substring",
        }

    if count > 1:
        # Report every line the substring starts on.
        line_numbers: list[int] = []
        offset = 0
        while True:
            idx = text.find(old_string, offset)
            if idx == -1:
                break
            line_numbers.append(text[:idx].count("\n") + 1)
            offset = idx + 1
        return {
            "ok": False,
            "error": f"old_string matched {count} times in {path} — add surrounding context to make it unique",
            "match_lines": line_numbers,
        }

    new_text = text.replace(old_string, new_string, 1)
    try:
        full.write_text(new_text)
    except Exception as e:
        return {"ok": False, "error": f"write error: {e}"}

    line = text[: text.index(old_string)].count("\n") + 1
    new_lines = new_text.splitlines()
    start = max(0, line - 2)
    end = min(len(new_lines), line + 3)
    preview = [f"L{i + 1}: {new_lines[i]}" for i in range(start, end)]
    return {"ok": True, "path": path, "line": line, "preview": preview}


def create_file(ctx: ToolContext, path: str, content: str) -> dict:
    """Create a new file. Errors if it already exists (use edit_file instead)."""
    full = safe_path(ctx.workdir, path)
    if full is None:
        return {"ok": False, "error": f"path escapes workdir: {path}"}
    if full.exists():
        return {
            "ok": False,
            "error": f"file already exists: {path}",
            "hint": "use edit_file to modify existing files",
        }
    try:
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    except Exception as e:
        return {"ok": False, "error": f"write error: {e}"}
    return {"ok": True, "path": path, "size": len(content)}


# -------------------- JSON schemas for OpenAI tool calling --------------------

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in the working directory. Output is trimmed to 4KB. Use this for install commands, smoke runs, and any shell inspection. Prepend `timeout N` for potentially long commands unless you've passed an explicit `timeout` argument.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."},
                    "phase": {
                        "type": "string",
                        "enum": ["install", "run", "fix"],
                        "description": "Tag this command with the current loop phase. Used for log stream + stuck detection.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Override the default 60s timeout. Max 300s (use the full 300 only for Rust first-builds).",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the working directory. Trimmed to 8KB.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List the contents of a directory inside the working directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative path. Defaults to '.'."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Make a targeted, diff-only edit: replace a unique substring in a file. "
                "`old_string` must match EXACTLY ONCE. If it matches zero times the tool "
                "returns closest_matches so you can retry with a corrected substring. "
                "If it matches multiple times the tool returns match_lines so you can "
                "add surrounding context and retry. Do NOT use this to rewrite whole "
                "files — for new files use create_file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string", "description": "Exact substring to find. Must be unique."},
                    "new_string": {"type": "string", "description": "Replacement text."},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": (
                "Create a new file with the given content. Fails if the file already "
                "exists — use edit_file for existing files. Use this for .env placeholder, "
                "missing __init__.py, or other small config files the repo needs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_success",
            "description": (
                "Terminal tool: call this once you've confirmed the repo installs and "
                "runs. The loop ends immediately after this call and install.json is written."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "2-3 sentence plain-English description of what the app does."},
                    "run_command": {"type": "string", "description": "The exact shell command that starts the app."},
                    "entry_point": {"type": "string", "description": "The entry point (file, module, or binary name)."},
                    "app_type": {"type": "string", "enum": ["cli", "web", "gui", "library"]},
                    "env_vars_used": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of env var names the app needs at runtime (e.g. ['OPENAI_API_KEY']).",
                    },
                },
                "required": ["summary", "run_command", "entry_point", "app_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_failure",
            "description": "Terminal tool: call this if you cannot make the repo run. Loop ends and failure.json is written.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "last_error": {"type": "string", "description": "The most recent stderr / exception message you saw."},
                    "phase_where_failed": {"type": "string", "enum": ["install", "run", "fix"]},
                },
                "required": ["reason", "phase_where_failed"],
            },
        },
    },
]


TOOL_IMPLS = {
    "bash": bash,
    "read_file": read_file,
    "list_files": list_files,
    "edit_file": edit_file,
    "create_file": create_file,
}
