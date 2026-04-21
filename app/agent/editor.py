"""Edit with AI — a lightweight agent that takes natural-language requests
and edits the source code of an installed app.

Reuses the same tool infrastructure as the install agent (bash, read_file,
edit_file, create_file, list_files) but with a simpler loop:
  1. One user message per turn
  2. Agent makes edits (1-10 tool calls typically)
  3. Verifies with tsc --noEmit
  4. Returns the list of files changed

No report_success/report_failure — the agent just edits until it's done
or runs out of iterations. The user sees changes via HMR.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import OPENAI_CLIENT
from .edit_context import scan_app_context
from .edit_prompts import EDIT_TOOL_SCHEMAS, build_edit_system_prompt
from .sandbox import INSTALLS_DIR
from .tools import TOOL_IMPLS, ToolContext

log = logging.getLogger(__name__)

MAX_EDIT_ITERATIONS = 20
MODEL = "gpt-5.4-mini"
TEMPERATURE = 0.2


@dataclass
class EditTurn:
    """One user message → agent response cycle."""
    turn_id: int
    user_message: str
    status: str = "thinking"  # thinking | editing | verifying | done | error
    files_changed: list[str] = field(default_factory=list)
    tsc_ok: bool | None = None
    tsc_errors: str | None = None
    agent_reply: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None


@dataclass
class EditSession:
    """A conversation with the edit agent for one installed app."""
    session_id: str
    install_id: str
    workdir: Path
    app_context: dict = field(default_factory=dict)
    turns: list[EditTurn] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)  # full LLM conversation
    created_at: float = field(default_factory=time.time)

    @property
    def current_turn(self) -> EditTurn | None:
        return self.turns[-1] if self.turns else None


# In-memory session registry.
_sessions: dict[str, EditSession] = {}


def get_session(session_id: str) -> EditSession | None:
    return _sessions.get(session_id)


def get_session_for_install(install_id: str) -> EditSession | None:
    for s in _sessions.values():
        if s.install_id == install_id:
            return s
    return None


def create_session(install_id: str, session_id: str) -> EditSession:
    workdir = INSTALLS_DIR / install_id
    if not workdir.exists():
        raise FileNotFoundError(f"install workdir not found: {install_id}")

    app_context = scan_app_context(workdir)

    session = EditSession(
        session_id=session_id,
        install_id=install_id,
        workdir=workdir,
        app_context=app_context,
    )

    # Seed the LLM conversation with the system prompt.
    system_prompt = build_edit_system_prompt(app_context)
    session.messages = [{"role": "system", "content": system_prompt}]

    _sessions[session_id] = session
    log.info("[edit %s] session created for install %s (%s)",
             session_id[:8], install_id[:8], app_context.get("project_type"))
    return session


def _snapshot(session: EditSession, turn_id: int) -> Path:
    """Snapshot the src/ directory (and other source dirs) for undo."""
    snap_dir = session.workdir / ".shirim-snapshots" / f"turn-{turn_id}"
    snap_dir.mkdir(parents=True, exist_ok=True)

    # Snapshot source directories that the agent might edit.
    for dirname in ("src", "app", "components", "pages", "lib", "public"):
        src = session.workdir / dirname
        if src.exists():
            dest = snap_dir / dirname
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest, symlinks=True)

    # Also snapshot standalone config files the agent might touch.
    for fname in ("package.json", "tsconfig.json", "tailwind.config.ts",
                  "tailwind.config.js", "remotion.config.ts", "next.config.ts",
                  "next.config.js", "vite.config.ts"):
        src = session.workdir / fname
        if src.exists():
            shutil.copy2(src, snap_dir / fname)

    return snap_dir


def undo_turn(session: EditSession, turn_id: int) -> bool:
    """Restore from snapshot. Returns True if successful."""
    snap_dir = session.workdir / ".shirim-snapshots" / f"turn-{turn_id}"
    if not snap_dir.exists():
        return False

    for item in snap_dir.iterdir():
        dest = session.workdir / item.name
        if item.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(item, dest, symlinks=True)
        else:
            shutil.copy2(item, dest)

    log.info("[edit %s] undid turn %d", session.session_id[:8], turn_id)
    return True


async def run_edit_turn(session: EditSession, message: str) -> EditTurn:
    """Process one user message: snapshot → agent edits → verify → return."""
    if OPENAI_CLIENT is None:
        turn = EditTurn(turn_id=len(session.turns), user_message=message, status="error")
        turn.agent_reply = "OpenAI client is not configured."
        turn.finished_at = time.time()
        session.turns.append(turn)
        return turn

    turn_id = len(session.turns)
    turn = EditTurn(turn_id=turn_id, user_message=message)
    session.turns.append(turn)

    # Snapshot for undo.
    _snapshot(session, turn_id)

    # Add user message to conversation.
    session.messages.append({"role": "user", "content": message})

    # Set up tool context.
    from .. import vault
    ctx = ToolContext(
        workdir=session.workdir,
        sandbox_env={},
        path_prepend=[],
        secrets=vault.load(),
        default_timeout=60,
        max_timeout=120,
    )

    files_before = _file_mtimes(session.workdir)

    # Run the LLM loop.
    turn.status = "editing"
    try:
        agent_reply = await _edit_loop(session, ctx)
        turn.agent_reply = agent_reply
    except Exception as e:
        log.exception("[edit %s] agent loop crashed", session.session_id[:8])
        turn.status = "error"
        turn.agent_reply = f"Error: {e}"
        turn.finished_at = time.time()
        return turn

    # Detect which files changed.
    files_after = _file_mtimes(session.workdir)
    changed = [
        f for f in files_after
        if f not in files_before or files_after[f] != files_before[f]
    ]
    # Also check for new files.
    new_files = [f for f in files_after if f not in files_before]
    turn.files_changed = sorted(set(changed + new_files))

    # Verify with tsc.
    turn.status = "verifying"
    tsc = await asyncio.to_thread(
        TOOL_IMPLS["bash"],
        ctx,
        command="npx tsc --noEmit 2>&1 | head -30",
        timeout=30,
    )
    turn.tsc_ok = tsc.get("exit_code") == 0
    if not turn.tsc_ok:
        turn.tsc_errors = tsc.get("stdout", "")[:500]

    turn.status = "done"
    turn.finished_at = time.time()
    log.info(
        "[edit %s] turn %d done: %d files changed, tsc=%s",
        session.session_id[:8], turn_id, len(turn.files_changed),
        "ok" if turn.tsc_ok else "errors",
    )
    return turn


async def _edit_loop(session: EditSession, ctx: ToolContext) -> str:
    """Run the LLM tool-calling loop for one edit turn. Returns the agent's
    final text reply (if any)."""
    assert OPENAI_CLIENT is not None
    last_text = ""

    for iteration in range(1, MAX_EDIT_ITERATIONS + 1):
        log.info("[edit %s] iteration %d/%d",
                 session.session_id[:8], iteration, MAX_EDIT_ITERATIONS)

        resp = await asyncio.to_thread(
            OPENAI_CLIENT.chat.completions.create,
            model=MODEL,
            messages=session.messages,
            tools=EDIT_TOOL_SCHEMAS,
            temperature=TEMPERATURE,
        )

        msg = resp.choices[0].message
        session.messages.append(_assistant_dict(msg))

        if msg.content:
            last_text = msg.content
            log.info("[edit %s] agent: %s", session.session_id[:8], msg.content[:200])

        if not msg.tool_calls:
            # Agent is done — no more tools to call.
            break

        for call in msg.tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            log.info("[edit %s] tool: %s(%s)",
                     session.session_id[:8], name,
                     _short_args(name, args))

            impl = TOOL_IMPLS.get(name)
            if impl is None:
                result = {"ok": False, "error": f"unknown tool: {name}"}
            else:
                try:
                    if name == "bash":
                        result = impl(ctx, command=args.get("command", ""),
                                      timeout=args.get("timeout"))
                    elif name == "read_file":
                        result = impl(ctx, path=args.get("path", ""))
                    elif name == "list_files":
                        result = impl(ctx, path=args.get("path", "."))
                    elif name == "edit_file":
                        result = impl(ctx, path=args.get("path", ""),
                                      old_string=args.get("old_string", ""),
                                      new_string=args.get("new_string", ""))
                    elif name == "create_file":
                        result = impl(ctx, path=args.get("path", ""),
                                      content=args.get("content", ""))
                    else:
                        result = {"ok": False, "error": f"unsupported tool: {name}"}
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}

            session.messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "name": name,
                "content": json.dumps(result, default=str),
            })

    return last_text


def _file_mtimes(workdir: Path) -> dict[str, float]:
    """Get mtime for all source files to detect changes."""
    result: dict[str, float] = {}
    for ext in ("*.tsx", "*.ts", "*.jsx", "*.js", "*.css", "*.json"):
        for f in workdir.rglob(ext):
            rel = str(f.relative_to(workdir))
            if "node_modules" in rel or ".shirim-" in rel:
                continue
            result[rel] = f.stat().st_mtime
    return result


def _assistant_dict(msg: Any) -> dict:
    out: dict = {"role": "assistant", "content": msg.content}
    if msg.tool_calls:
        out["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.function.name, "arguments": c.function.arguments},
            }
            for c in msg.tool_calls
        ]
    return out


def _short_args(name: str, args: dict) -> str:
    if name == "bash":
        cmd = args.get("command", "")
        return cmd[:80] + ("..." if len(cmd) > 80 else "")
    if name in ("read_file", "list_files"):
        return args.get("path", "")
    if name == "edit_file":
        return args.get("path", "")
    if name == "create_file":
        return args.get("path", "")
    return str(args)[:80]
