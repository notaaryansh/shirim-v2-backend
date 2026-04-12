"""Node.js language adapter."""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from .base import EntryPoint, ParsedDeps, SandboxInfo

log = logging.getLogger(__name__)


class NodeAdapter:
    name = "node"

    def detect(self, tree: list[str], files: dict[str, str]) -> float:
        if "package.json" not in files:
            return 0.0
        score = 0.5
        if any(f in files for f in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml")):
            score += 0.3
        js_count = sum(
            1 for r in tree
            if r.endswith((".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx"))
        )
        if js_count >= 3:
            score += 0.2
        return min(score, 1.0)

    def _pick_package_manager(self, files: dict[str, str]) -> str:
        if "pnpm-lock.yaml" in files:
            return "pnpm"
        if "yarn.lock" in files:
            return "yarn"
        return "npm"

    def parse_deps(
        self, workdir: Path, tree: list[str], files: dict[str, str]
    ) -> ParsedDeps:
        deps: list[str] = []
        dep_files: list[str] = []
        entry_points: list[EntryPoint] = []
        extras: dict = {}
        pm = self._pick_package_manager(files)
        managers = [pm]

        if "package.json" in files:
            dep_files.append("package.json")
            try:
                pkg = json.loads(files["package.json"])
            except Exception:
                pkg = {}
            for k in ("dependencies", "devDependencies"):
                for name, ver in (pkg.get(k) or {}).items():
                    deps.append(f"{name}@{ver}")
            # scripts → entry-point candidates. Only include likely-useful ones —
            # large projects (e.g. OpenClaw) can have 200+ scripts and flooding
            # the LLM with all of them wastes tokens.
            RUN_SCRIPTS = {"start", "dev", "serve", "server", "preview", "watch"}
            BUILD_SCRIPTS = {"build", "compile", "bundle"}
            all_scripts = pkg.get("scripts") or {}
            for name, cmd in all_scripts.items():
                if name in RUN_SCRIPTS or name in BUILD_SCRIPTS:
                    entry_points.append(
                        EntryPoint(
                            kind="package_json_script",
                            value=f"{pm} run {name}",
                            source=f"package.json scripts.{name}: {cmd[:80]}",
                        )
                    )
            extras["scripts"] = pkg.get("scripts") or {}
            main = pkg.get("main")
            if main:
                entry_points.append(
                    EntryPoint(
                        kind="package_json_main",
                        value=f"node {main}",
                        source=f"package.json main",
                    )
                )
            # bin may be string or object
            bin_field = pkg.get("bin")
            if isinstance(bin_field, str):
                entry_points.append(
                    EntryPoint(
                        kind="package_json_bin",
                        value=f"node {bin_field}",
                        source="package.json bin",
                    )
                )
            elif isinstance(bin_field, dict):
                for name, path in bin_field.items():
                    entry_points.append(
                        EntryPoint(
                            kind="package_json_bin",
                            value=f"node {path}",
                            source=f"package.json bin.{name}",
                        )
                    )
            extras["name"] = pkg.get("name")
            extras["type"] = pkg.get("type")  # "module" or "commonjs"

        if "package-lock.json" in files:
            dep_files.append("package-lock.json")
        if "yarn.lock" in files:
            dep_files.append("yarn.lock")
        if "pnpm-lock.yaml" in files:
            dep_files.append("pnpm-lock.yaml")

        # App type heuristic: presence of 'start' or a web server dep → web; bin → cli.
        app_type = "unknown"
        all_deps_flat = " ".join(deps).lower()
        if any(k in all_deps_flat for k in ("express", "fastify", "koa", "next", "nuxt", "hapi", "nestjs")):
            app_type = "web"
        elif any(ep.kind == "package_json_bin" for ep in entry_points):
            app_type = "cli"
        elif (extras.get("scripts") or {}).get("start"):
            app_type = "web"

        return ParsedDeps(
            declared_deps=deps,
            dep_files=dep_files,
            package_managers=managers,
            candidate_entry_points=entry_points,
            app_type_hint=app_type,
            extras=extras,
        )

    def bootstrap_sandbox(self, workdir: Path) -> SandboxInfo:
        prefix = workdir / ".shirim-npm-prefix"
        prefix.mkdir(parents=True, exist_ok=True)
        cache = workdir / ".shirim-npm-cache"
        cache.mkdir(parents=True, exist_ok=True)

        notes: list[str] = [f"npm prefix: {prefix}", f"npm cache: {cache}"]
        path_prepend: list[str] = []

        # Detect which package manager the repo needs and install it if missing.
        pm = self._pick_package_manager(
            {f: "" for f in ("pnpm-lock.yaml", "yarn.lock", "package-lock.json")
             if (workdir / f).exists()}
        )

        bin_dir = str(prefix / "bin")
        path_prepend.append(bin_dir)

        def _is_installed(cmd: str) -> bool:
            try:
                return subprocess.run([cmd, "-v"], capture_output=True).returncode == 0
            except FileNotFoundError:
                return False

        sandbox_env = {
            **__import__("os").environ,
            "NPM_CONFIG_PREFIX": str(prefix),
            "NPM_CONFIG_CACHE": str(cache),
            "PATH": bin_dir + __import__("os").pathsep + __import__("os").environ.get("PATH", ""),
        }

        if pm == "pnpm" and not _is_installed("pnpm"):
            log.info("installing pnpm into sandbox prefix")
            r = subprocess.run(
                ["npm", "install", "-g", "pnpm"],
                capture_output=True, text=True, env=sandbox_env,
            )
            if r.returncode == 0:
                notes.append("installed pnpm")
            else:
                log.warning("pnpm install failed: %s", r.stderr[:300])
                notes.append(f"pnpm install failed: {r.stderr[:200]}")
        elif pm == "yarn" and not _is_installed("yarn"):
            log.info("installing yarn into sandbox prefix")
            r = subprocess.run(
                ["npm", "install", "-g", "yarn"],
                capture_output=True, text=True, env=sandbox_env,
            )
            if r.returncode == 0:
                notes.append("installed yarn")
            else:
                log.warning("yarn install failed: %s", r.stderr[:300])
                notes.append(f"yarn install failed: {r.stderr[:200]}")

        return SandboxInfo(
            env={
                "NPM_CONFIG_PREFIX": str(prefix),
                "NPM_CONFIG_CACHE": str(cache),
            },
            path_prepend=path_prepend,
            notes=notes,
        )

    def install_cmd(self, parsed: ParsedDeps) -> str:
        pm = parsed.package_managers[0] if parsed.package_managers else "npm"
        if pm == "pnpm":
            return "pnpm install --frozen-lockfile"
        if pm == "yarn":
            return "yarn install --frozen-lockfile"
        # npm: prefer ci if lockfile exists, else install.
        if "package-lock.json" in parsed.dep_files:
            return "npm ci"
        return "npm install"

    def smoke_run_candidates(self, parsed: ParsedDeps) -> list[str]:
        pm = parsed.package_managers[0] if parsed.package_managers else "npm"
        scripts = (parsed.extras or {}).get("scripts") or {}
        out: list[str] = []
        if "test" in scripts:
            out.append(f"timeout 30 {pm} test || true")
        if "start" in scripts:
            out.append(f"timeout 5 {pm} start || true")
        for ep in parsed.candidate_entry_points:
            if ep.kind == "package_json_bin":
                out.append(f"timeout 10 {ep.value} --help || true")
            elif ep.kind == "package_json_main":
                out.append(f"timeout 5 {ep.value} || true")
        if not out:
            out.append("node -e 'console.log(process.version); console.log(\"ok\")'")
        return out
