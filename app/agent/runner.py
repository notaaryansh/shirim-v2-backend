"""AgentRun — the install/run/fix LLM loop.

Ported from shirim-v2/agent.py with three material changes:
  1. Deterministic pre-analysis phase feeds the LLM a summary instead of letting
     it rediscover the repo.
  2. edit_file (diff-only) replaces write_file (full content overwrite).
  3. Multi-language via adapter dispatch.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import vault
from ..config import OPENAI_CLIENT
from .adapters import all_adapters, get_adapter
from .analyzer import analyze
from .prompts import BASE_SYSTEM_PROMPT, LANGUAGE_APPENDICES, build_initial_user_message
from .sandbox import INSTALLS_DIR, clone_repo
from .tools import TOOL_IMPLS, TOOL_SCHEMAS, ToolContext

log = logging.getLogger(__name__)

MAX_ITERATIONS = 40
WALL_CLOCK_SECONDS = 8 * 60
MODEL = "gpt-5.4-nano"
TEMPERATURE = 0.2
STUCK_THRESHOLD = 2  # N consecutive fix turns with identical stderr_hash → stuck


@dataclass
class AgentRun:
    install_id: str
    owner: str
    repo: str
    ref: str | None = None
    workdir: Path = field(init=False)

    status: str = "pending"  # pending | cloning | analyzing | sandboxing | running | success | failure | timeout | cancelled | error
    phase: str | None = None  # install | run | fix
    analysis: dict | None = None
    result: dict | None = None
    logs: list[dict] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    cancel_requested: bool = False

    def __post_init__(self) -> None:
        self.workdir = INSTALLS_DIR / self.install_id

    def request_cancel(self) -> None:
        """Cooperative cancel. Flag is checked at safe points in the loop."""
        self.cancel_requested = True
        self.log_event("cancel_requested")

    def _check_cancel(self) -> bool:
        """Return True and mark status if cancel was requested."""
        if self.cancel_requested and self.status not in (
            "success", "failure", "timeout", "cancelled", "error"
        ):
            self.status = "cancelled"
            self.log_event("cancelled", reason="cancel requested by user")
            return True
        return False

    # -------------------- logging helpers --------------------

    def log_event(self, type_: str, **payload: Any) -> None:
        entry = {"ts": time.time(), "type": type_}
        if self.phase:
            entry["phase"] = self.phase
        entry.update(payload)
        self.logs.append(entry)
        # Always log to terminal at INFO so the user can see progress in uvicorn output.
        phase_tag = f"[{self.phase}]" if self.phase else ""
        log.info("[%s] %s %s %s", self.install_id[:8], type_, phase_tag, _summary_for_log(type_, payload))

    # -------------------- main entry --------------------

    async def run(self) -> None:
        self.started_at = time.time()
        try:
            await self._run_inner()
        except asyncio.CancelledError:
            # Either .cancel() was called on our task, or a subprocess was
            # interrupted. Record it cleanly and do NOT re-raise — we want the
            # finally block to still write result files and the poller to see
            # status=cancelled instead of an orphan task.
            log.info("agent run cancelled: %s", self.install_id)
            self.status = "cancelled"
            self.log_event("cancelled", reason="task cancelled")
        except Exception as e:
            log.exception("agent run crashed")
            self.status = "error"
            self.log_event("error", msg=f"{type(e).__name__}: {e}", tb=traceback.format_exc())
        finally:
            self.finished_at = time.time()
            self._write_result_file()
            self.log_event("done", status=self.status)

    async def _run_inner(self) -> None:
        # Stage 1 — clone
        self.status = "cloning"
        self.log_event("status", msg=f"cloning {self.owner}/{self.repo}")
        await clone_repo(self.owner, self.repo, self.workdir, ref=self.ref)
        if self._check_cancel():
            return

        # Stage 2 — deterministic analysis
        self.status = "analyzing"
        self.log_event("status", msg="analyzing repo")
        self.analysis = analyze(self.workdir, all_adapters())
        language = self.analysis.get("language")

        if not language:
            # No adapter matched — fall back to LLM-driven exploration.
            # The LLM will read the README, explore subdirectories, and
            # figure out how to install/run on its own. This handles
            # monorepos, unsupported languages (Flutter, Ruby, etc.), and
            # unusual project structures.
            self.log_event(
                "warning",
                msg="no language adapter matched — falling back to LLM exploration",
            )
            self.log_event(
                "analysis_complete",
                language="unknown",
                dep_files=self.analysis.get("dep_files"),
                entry_point_count=0,
            )
            if OPENAI_CLIENT is None:
                self.status = "error"
                self.log_event("error", msg="OPENAI_CLIENT is not configured")
                return
            self.status = "running"
            ctx = ToolContext(
                workdir=self.workdir,
                sandbox_env={},
                path_prepend=[],
                secrets=vault.load(),
                default_timeout=60,
                max_timeout=300,
            )
            await self._llm_loop(ctx, adapter=None, parsed=None, language="unknown")
            return

        self.log_event(
            "analysis_complete",
            language=language,
            dep_files=self.analysis.get("dep_files"),
            entry_point_count=len(self.analysis.get("candidate_entry_points") or []),
        )

        # Stage 3 — sandbox setup (blocking, run off-loop)
        self.status = "sandboxing"
        self.log_event("status", msg=f"setting up {language} sandbox")
        adapter = get_adapter(language)
        parsed = adapter.parse_deps(self.workdir, self.analysis.get("file_tree") or [], {})
        try:
            sandbox_info = await asyncio.to_thread(adapter.bootstrap_sandbox, self.workdir)
        except Exception as e:
            self.status = "failure"
            self.log_event("failure", reason=f"sandbox setup failed: {e}")
            return
        for note in sandbox_info.notes:
            self.log_event("sandbox", note=note)
        if self._check_cancel():
            return

        # Stage 4 — LLM loop
        if OPENAI_CLIENT is None:
            self.status = "error"
            self.log_event("error", msg="OPENAI_CLIENT is not configured")
            return

        self.status = "running"
        ctx = ToolContext(
            workdir=self.workdir,
            sandbox_env=sandbox_info.env,
            path_prepend=sandbox_info.path_prepend,
            secrets=vault.load(),
            default_timeout=60,
            max_timeout=300,
        )
        await self._llm_loop(ctx, adapter, parsed, language)

    # -------------------- LLM loop --------------------

    async def _llm_loop(self, ctx: ToolContext, adapter: Any, parsed: Any, language: str) -> None:
        assert OPENAI_CLIENT is not None

        if adapter is not None and parsed is not None:
            # Normal path — adapter matched, we have structured analysis.
            system_prompt = (
                BASE_SYSTEM_PROMPT + "\n\n" + LANGUAGE_APPENDICES.get(language, "")
            )
            user_msg = build_initial_user_message(
                owner=self.owner,
                repo=self.repo,
                workdir=str(self.workdir),
                language=language,  # type: ignore[arg-type]
                parsed=parsed,
                analysis=self.analysis or {},
                sandbox_notes=(self.analysis or {}).get("warnings") or [],
                secret_names=list(ctx.secrets.keys()),
                install_cmd_hint=adapter.install_cmd(parsed),
                smoke_run_hints=adapter.smoke_run_candidates(parsed),
            )
        else:
            # Fallback path — no adapter matched. Let the LLM explore freely.
            from .prompts import FALLBACK_SYSTEM_PROMPT, build_fallback_user_message
            system_prompt = FALLBACK_SYSTEM_PROMPT
            user_msg = build_fallback_user_message(
                owner=self.owner,
                repo=self.repo,
                workdir=str(self.workdir),
                analysis=self.analysis or {},
                secret_names=list(ctx.secrets.keys()),
            )
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ]

        # Stuck-detection state — tracks repeated commands and repeated stderr
        # across ALL phases (not just fix).
        recent_cmd_hashes: list[str] = []   # hash of (name, command/path)
        recent_stderr_hashes: list[str] = []  # hash of stderr output
        REPEAT_CMD_LIMIT = 4   # same command 4 times in a row → stuck
        REPEAT_STDERR_LIMIT = 3  # same non-empty stderr 3 times in a row → stuck

        for iteration in range(1, MAX_ITERATIONS + 1):
            if self._check_cancel():
                return
            if time.time() - self.started_at > WALL_CLOCK_SECONDS:
                self.status = "timeout"
                self.log_event("timeout", reason=f"wall clock > {WALL_CLOCK_SECONDS}s")
                return

            self.log_event("iter", n=iteration)
            try:
                resp = await asyncio.to_thread(
                    OPENAI_CLIENT.chat.completions.create,
                    model=MODEL,
                    messages=messages,
                    tools=TOOL_SCHEMAS,
                    temperature=TEMPERATURE,
                )
            except Exception as e:
                self.status = "error"
                self.log_event("error", msg=f"openai call failed: {e}")
                return

            msg = resp.choices[0].message
            messages.append(_assistant_message_dict(msg))

            if msg.content:
                self.log_event("thought", text=msg.content[:800])

            if not msg.tool_calls:
                self.log_event("warning", msg="agent stopped without calling a tool")
                self.status = "failure"
                self.log_event("failure", reason="agent produced no tool calls")
                return

            for call in msg.tool_calls:
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                # Log the tool call.
                self.log_event("tool_call", name=name, args=_safe_args(args))

                # Terminal tools
                if name == "report_success":
                    self.status = "success"
                    self.result = args
                    self.log_event("success", **args)
                    return
                if name == "report_failure":
                    self.status = "failure"
                    self.result = args
                    self.log_event("failure", **args)
                    return

                # Update phase tracking if the bash call declared one.
                if name == "bash" and "phase" in args:
                    self.phase = args.get("phase")

                impl = TOOL_IMPLS.get(name)
                if impl is None:
                    tool_result: dict = {"ok": False, "error": f"unknown tool: {name}"}
                else:
                    try:
                        if name == "bash":
                            tool_result = impl(
                                ctx,
                                command=args.get("command", ""),
                                timeout=args.get("timeout"),
                                phase=args.get("phase"),
                            )
                        elif name == "read_file":
                            tool_result = impl(ctx, path=args.get("path", ""))
                        elif name == "list_files":
                            tool_result = impl(ctx, path=args.get("path", "."))
                        elif name == "edit_file":
                            tool_result = impl(
                                ctx,
                                path=args.get("path", ""),
                                old_string=args.get("old_string", ""),
                                new_string=args.get("new_string", ""),
                            )
                        elif name == "create_file":
                            tool_result = impl(
                                ctx,
                                path=args.get("path", ""),
                                content=args.get("content", ""),
                            )
                        else:
                            tool_result = {"ok": False, "error": f"unhandled tool: {name}"}
                    except Exception as e:
                        tool_result = {"ok": False, "error": f"{type(e).__name__}: {e}"}

                self.log_event("tool_result", name=name, result=_trim_for_log(tool_result))

                # --- Stuck detection (all phases) ---
                if name == "bash":
                    cmd = args.get("command", "")
                    cmd_h = hashlib.sha1(cmd.encode("utf-8", "replace")).hexdigest()[:12]
                    recent_cmd_hashes.append(cmd_h)

                    stderr = (tool_result.get("stderr") or "").strip()
                    if stderr:
                        stderr_h = hashlib.sha1(stderr.encode("utf-8", "replace")).hexdigest()[:12]
                        recent_stderr_hashes.append(stderr_h)
                    else:
                        recent_stderr_hashes.clear()  # non-error breaks the streak

                    # Check 1: exact same command repeated N times in a row
                    if (
                        len(recent_cmd_hashes) >= REPEAT_CMD_LIMIT
                        and len(set(recent_cmd_hashes[-REPEAT_CMD_LIMIT:])) == 1
                    ):
                        self.status = "failure"
                        self.log_event(
                            "failure",
                            reason=f"stuck: same command repeated {REPEAT_CMD_LIMIT} times",
                            phase_where_failed=self.phase,
                            command=cmd[:200],
                        )
                        return

                    # Check 2: same non-empty stderr repeated N times in a row
                    if (
                        len(recent_stderr_hashes) >= REPEAT_STDERR_LIMIT
                        and len(set(recent_stderr_hashes[-REPEAT_STDERR_LIMIT:])) == 1
                    ):
                        self.status = "failure"
                        self.log_event(
                            "failure",
                            reason=f"stuck: same stderr repeated {REPEAT_STDERR_LIMIT} times in {self.phase} phase",
                            phase_where_failed=self.phase,
                        )
                        return
                else:
                    # Non-bash tools (read_file, list_files, etc.) break both streaks
                    # since the agent is at least trying something different.
                    recent_cmd_hashes.clear()
                    recent_stderr_hashes.clear()

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": name,
                        "content": json.dumps(tool_result, default=str),
                    }
                )

                if self._check_cancel():
                    return

        self.status = "timeout"
        self.log_event("timeout", reason=f"exceeded MAX_ITERATIONS={MAX_ITERATIONS}")

    # -------------------- output --------------------

    def _write_result_file(self) -> None:
        if not self.workdir.exists():
            return
        out = {
            "install_id": self.install_id,
            "owner": self.owner,
            "repo": self.repo,
            "status": self.status,
            "phase_at_end": self.phase,
            "analysis": self.analysis,
            "result": self.result,
            "log_count": len(self.logs),
            "duration_ms": int((self.finished_at - self.started_at) * 1000),
        }
        if self.status == "success":
            name = "install.json"
        elif self.status == "cancelled":
            name = "cancelled.json"
        else:
            name = "failure.json"
        try:
            (self.workdir / name).write_text(json.dumps(out, indent=2, default=str))
        except Exception as e:
            log.warning("failed to write %s: %s", name, e)


# -------------------- private helpers --------------------

def _assistant_message_dict(msg: Any) -> dict:
    """Convert OpenAI's ChatCompletionMessage into something we can put back in
    the messages list on the next turn."""
    out: dict = {"role": "assistant", "content": msg.content}
    if msg.tool_calls:
        out["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in msg.tool_calls
        ]
    return out


def _safe_args(args: dict) -> dict:
    """Log args with long strings trimmed so the log stream stays readable."""
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 300:
            out[k] = v[:280] + f"...[+{len(v) - 280}]"
        else:
            out[k] = v
    return out


def _trim_for_log(result: dict) -> dict:
    """Tool results can be big — log a truncated version."""
    out = {}
    for k, v in result.items():
        if isinstance(v, str) and len(v) > 600:
            out[k] = v[:580] + f"...[+{len(v) - 580}]"
        elif isinstance(v, list) and len(v) > 20:
            out[k] = v[:20] + [f"...[+{len(v) - 20}]"]
        else:
            out[k] = v
    return out


def _summary_for_log(type_: str, payload: dict) -> str:
    """Build a short one-liner for terminal display per log event."""
    if type_ == "status":
        return payload.get("msg", "")
    if type_ == "iter":
        return f"--- iteration {payload.get('n', '?')}/{MAX_ITERATIONS} ---"
    if type_ == "thought":
        text = payload.get("text", "")
        return text[:200] + ("..." if len(text) > 200 else "")
    if type_ == "tool_call":
        name = payload.get("name", "")
        args = payload.get("args", {})
        if name == "bash":
            cmd = args.get("command", "")
            return f"bash({cmd[:120]}{'...' if len(cmd) > 120 else ''})"
        if name == "read_file":
            return f"read_file({args.get('path', '')})"
        if name == "list_files":
            return f"list_files({args.get('path', '.')})"
        if name == "edit_file":
            return f"edit_file({args.get('path', '')})"
        if name == "create_file":
            return f"create_file({args.get('path', '')})"
        if name == "report_success":
            return f"report_success(app_type={args.get('app_type', '?')}, cmd={args.get('run_command', '?')[:80]})"
        if name == "report_failure":
            return f"report_failure(reason={args.get('reason', '?')[:100]})"
        return f"{name}({args})"
    if type_ == "tool_result":
        name = payload.get("name", "")
        result = payload.get("result", {})
        ok = result.get("ok")
        ec = result.get("exit_code")
        if name == "bash" and ec is not None:
            stderr = (result.get("stderr") or "")[:100]
            return f"bash → exit={ec}" + (f" stderr={stderr}" if ec != 0 and stderr else "")
        if ok is False:
            return f"{name} → FAILED: {result.get('error', '?')[:100]}"
        if ok is True:
            return f"{name} → ok"
        return ""
    if type_ == "analysis_complete":
        return f"language={payload.get('language')}, deps={len(payload.get('dep_files', []))}, entries={payload.get('entry_point_count', 0)}"
    if type_ == "success":
        return f"app_type={payload.get('app_type', '?')}, cmd={payload.get('run_command', '?')[:80]}"
    if type_ == "failure":
        return payload.get("reason", "")[:150]
    if type_ == "timeout":
        return payload.get("reason", "")
    if type_ == "error":
        return payload.get("msg", "")[:150]
    if type_ == "cancelled":
        return payload.get("reason", "")
    if type_ == "done":
        return f"final status={payload.get('status', '?')}"
    if type_ == "sandbox":
        return payload.get("note", "")
    if type_ == "warning":
        return payload.get("msg", "")
    return str(payload)[:150] if payload else ""
