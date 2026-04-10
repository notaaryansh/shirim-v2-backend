"""Python language adapter."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from .base import EntryPoint, LanguageAdapter, ParsedDeps, SandboxInfo

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


PY_DEP_FILES = ("requirements.txt", "pyproject.toml", "setup.py", "Pipfile")
WEB_FRAMEWORK_RE = re.compile(
    r"\b(FastAPI|Flask|Starlette|Django|Quart|Sanic|Bottle|Tornado|aiohttp)\s*\("
)
CLICK_OR_TYPER_RE = re.compile(r"\b(click|typer)\s*\.", re.IGNORECASE)


class PythonAdapter:
    name = "python"

    def detect(self, tree: list[str], files: dict[str, str]) -> float:
        score = 0.0
        for f in PY_DEP_FILES:
            if f in files:
                score += 0.4
        py_count = sum(1 for r in tree if r.endswith(".py"))
        if py_count >= 3:
            score += 0.3
        elif py_count >= 1:
            score += 0.1
        return min(score, 1.0)

    def parse_deps(
        self, workdir: Path, tree: list[str], files: dict[str, str]
    ) -> ParsedDeps:
        deps: list[str] = []
        dep_files: list[str] = []
        managers: list[str] = []
        entry_points: list[EntryPoint] = []
        extras: dict = {}

        # requirements.txt — simple line parse.
        if "requirements.txt" in files:
            dep_files.append("requirements.txt")
            managers.append("pip")
            for line in files["requirements.txt"].splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("-"):
                    deps.append(line)

        # pyproject.toml — both PEP 621 and poetry layouts.
        if "pyproject.toml" in files and tomllib:
            dep_files.append("pyproject.toml")
            try:
                data = tomllib.loads(files["pyproject.toml"])
            except Exception:
                data = {}
            proj = data.get("project") or {}
            for d in proj.get("dependencies") or []:
                deps.append(str(d))
            # [project.scripts] → CLI entry points
            for name, target in (proj.get("scripts") or {}).items():
                entry_points.append(
                    EntryPoint(
                        kind="pyproject_script",
                        value=f"{name}={target}",
                        source="pyproject.toml [project.scripts]",
                    )
                )
            # Poetry fallback
            poetry = (data.get("tool") or {}).get("poetry") or {}
            for d in (poetry.get("dependencies") or {}).keys():
                if d != "python":
                    deps.append(d)
            for name, target in (poetry.get("scripts") or {}).items():
                entry_points.append(
                    EntryPoint(
                        kind="poetry_script",
                        value=f"{name}={target}",
                        source="pyproject.toml [tool.poetry.scripts]",
                    )
                )
            # pip/build backend presence implies pip works.
            if "pip" not in managers:
                managers.append("pip")

        if "setup.py" in files:
            dep_files.append("setup.py")
            if "pip" not in managers:
                managers.append("pip")

        if "Pipfile" in files:
            dep_files.append("Pipfile")
            if "pipenv" not in managers:
                managers.append("pipenv")

        # Heuristic entry points from repo structure.
        for rel in tree:
            base = rel.rsplit("/", 1)[-1]
            if base == "__main__.py":
                pkg = rel.rsplit("/", 1)[0] if "/" in rel else ""
                entry_points.append(
                    EntryPoint(
                        kind="module_main",
                        value=f"python -m {pkg}" if pkg else "python __main__.py",
                        source=rel,
                    )
                )
            elif base in ("main.py", "run.py", "app.py", "server.py", "cli.py"):
                if "/" not in rel:  # top level
                    entry_points.append(
                        EntryPoint(
                            kind="top_level_script",
                            value=f"python {rel}",
                            source=rel,
                        )
                    )

        # Scan a small subset of .py files for framework signals. Priority files
        # (likely entry points) go first so we don't match an unrelated file that
        # happens to mention the framework in a docstring or regex.
        priority_names = ("main.py", "app.py", "server.py", "run.py", "cli.py", "__main__.py")
        priority_files = [
            r for r in tree if r.rsplit("/", 1)[-1] in priority_names
        ]
        other_py = [r for r in tree if r.endswith(".py") and r not in priority_files]
        scan_order = priority_files + other_py[: 40 - len(priority_files)]

        app_type = "unknown"
        for rel in scan_order:
            if not rel.endswith(".py"):
                continue
            try:
                text = (workdir / rel).read_text(errors="replace")[:20_000]
            except Exception:
                continue
            if WEB_FRAMEWORK_RE.search(text):
                app_type = "web"
                # Try to extract the "app = FastAPI(" variable.
                m = re.search(r"(\w+)\s*=\s*(FastAPI|Flask|Starlette|Quart)\(", text)
                if m:
                    module = rel[:-3].replace("/", ".")
                    entry_points.append(
                        EntryPoint(
                            kind="web_app",
                            value=f"uvicorn {module}:{m.group(1)} --port 0",
                            source=rel,
                        )
                    )
                break
            if CLICK_OR_TYPER_RE.search(text) and app_type == "unknown":
                app_type = "cli"

        # README code fences starting with `python ` or `pip install`
        for fname in ("README.md", "readme.md"):
            if fname not in files:
                continue
            for m in re.finditer(r"```(?:bash|sh|shell)?\s*\n(.*?)\n```", files[fname], re.DOTALL):
                for line in m.group(1).splitlines():
                    line = line.strip().lstrip("$ ")
                    if line.startswith("python ") or line.startswith("python3 "):
                        entry_points.append(
                            EntryPoint(
                                kind="readme_code",
                                value=line,
                                source=f"{fname} code block",
                            )
                        )

        # Dedupe
        seen: set[str] = set()
        deduped: list[EntryPoint] = []
        for ep in entry_points:
            if ep.value in seen:
                continue
            seen.add(ep.value)
            deduped.append(ep)

        return ParsedDeps(
            declared_deps=deps,
            dep_files=dep_files,
            package_managers=managers,
            candidate_entry_points=deduped,
            app_type_hint=app_type,
            extras=extras,
        )

    def bootstrap_sandbox(self, workdir: Path) -> SandboxInfo:
        venv_dir = workdir / ".shirim-venv"
        proc = subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            capture_output=True,
            text=True,
        )
        notes: list[str] = []
        if proc.returncode != 0:
            raise RuntimeError(f"venv create failed: {proc.stderr}")
        notes.append(f"venv: {venv_dir}")

        bin_dir = venv_dir / ("Scripts" if sys.platform == "win32" else "bin")
        # Upgrade pip quietly; ignore failure (not fatal).
        subprocess.run(
            [str(bin_dir / "python"), "-m", "pip", "install", "--upgrade", "pip", "-q"],
            capture_output=True,
            text=True,
        )

        return SandboxInfo(
            env={"VIRTUAL_ENV": str(venv_dir)},
            path_prepend=[str(bin_dir)],
            notes=notes,
        )

    def install_cmd(self, parsed: ParsedDeps) -> str:
        if "requirements.txt" in parsed.dep_files:
            return "pip install -r requirements.txt"
        if "pyproject.toml" in parsed.dep_files:
            return "pip install -e ."
        if "setup.py" in parsed.dep_files:
            return "pip install -e ."
        if "Pipfile" in parsed.dep_files:
            return "pip install pipenv && pipenv install --deploy"
        return "pip install ."

    def smoke_run_candidates(self, parsed: ParsedDeps) -> list[str]:
        out: list[str] = []
        for ep in parsed.candidate_entry_points:
            if ep.kind == "web_app":
                out.append(f"timeout 5 {ep.value} || true")
            elif ep.kind in ("top_level_script", "module_main"):
                out.append(f"timeout 10 {ep.value} --help || {ep.value} --help || true")
            elif ep.kind in ("pyproject_script", "poetry_script"):
                name = ep.value.split("=", 1)[0]
                out.append(f"timeout 10 {name} --help || true")
            elif ep.kind == "readme_code":
                out.append(f"timeout 10 {ep.value} || true")
        if not out:
            out.append('python -c "import sys; print(sys.version); print(\'ok\')"')
        return out
