"""Go language adapter."""
from __future__ import annotations

import re
from pathlib import Path

from .base import EntryPoint, ParsedDeps, SandboxInfo

GO_MOD_RE = re.compile(r"^module\s+(\S+)", re.MULTILINE)
GO_REQUIRE_RE = re.compile(r"^\s*([a-z0-9][^\s]+)\s+v?[0-9][^\s]*", re.MULTILINE)


class GoAdapter:
    name = "go"

    def detect(self, tree: list[str], files: dict[str, str]) -> float:
        if "go.mod" not in files:
            return 0.0
        score = 0.6
        go_count = sum(1 for r in tree if r.endswith(".go"))
        if go_count >= 3:
            score += 0.3
        elif go_count >= 1:
            score += 0.1
        if "go.sum" in files:
            score += 0.1
        return min(score, 1.0)

    def parse_deps(
        self, workdir: Path, tree: list[str], files: dict[str, str]
    ) -> ParsedDeps:
        deps: list[str] = []
        dep_files: list[str] = []
        entry_points: list[EntryPoint] = []
        extras: dict = {}

        if "go.mod" in files:
            dep_files.append("go.mod")
            mod_text = files["go.mod"]
            module_match = GO_MOD_RE.search(mod_text)
            if module_match:
                extras["module"] = module_match.group(1)
            # require blocks
            for m in GO_REQUIRE_RE.finditer(mod_text):
                deps.append(m.group(1))
        if "go.sum" in files:
            dep_files.append("go.sum")

        # Entry points: any .go file with `func main()` in package main.
        # cmd/<name>/*.go is the idiomatic location.
        main_packages: list[str] = []
        for rel in tree:
            if not rel.endswith(".go"):
                continue
            try:
                text = (workdir / rel).read_text(errors="replace")[:20_000]
            except Exception:
                continue
            if "package main" in text and "func main()" in text:
                pkg_dir = rel.rsplit("/", 1)[0] if "/" in rel else "."
                main_packages.append(pkg_dir)
        main_packages = sorted(set(main_packages))
        extras["main_packages"] = main_packages

        for pkg in main_packages:
            if pkg == ".":
                entry_points.append(
                    EntryPoint(
                        kind="go_main",
                        value="go run .",
                        source="root package main",
                    )
                )
            else:
                entry_points.append(
                    EntryPoint(
                        kind="go_main",
                        value=f"go run ./{pkg}",
                        source=f"{pkg}/*.go",
                    )
                )

        app_type = "cli" if main_packages else "library"
        all_deps_flat = " ".join(deps).lower()
        if any(k in all_deps_flat for k in ("gin-gonic", "echo", "fiber", "chi", "gorilla", "net/http")):
            app_type = "web"

        return ParsedDeps(
            declared_deps=deps,
            dep_files=dep_files,
            package_managers=["go"],
            candidate_entry_points=entry_points,
            app_type_hint=app_type,
            extras=extras,
        )

    def bootstrap_sandbox(self, workdir: Path) -> SandboxInfo:
        gopath = workdir / ".shirim-gopath"
        gocache = workdir / ".shirim-gocache"
        gopath.mkdir(parents=True, exist_ok=True)
        gocache.mkdir(parents=True, exist_ok=True)
        return SandboxInfo(
            env={
                "GOPATH": str(gopath),
                "GOMODCACHE": str(gopath / "pkg" / "mod"),
                "GOCACHE": str(gocache),
                "GOFLAGS": "-mod=mod",
            },
            path_prepend=[],
            notes=[f"GOPATH: {gopath}", f"GOCACHE: {gocache}"],
        )

    def install_cmd(self, parsed: ParsedDeps) -> str:
        return "go mod download"

    def smoke_run_candidates(self, parsed: ParsedDeps) -> list[str]:
        out: list[str] = []
        for ep in parsed.candidate_entry_points:
            if ep.kind == "go_main":
                out.append(f"timeout 10 {ep.value} -h 2>&1 || {ep.value} --help 2>&1 || true")
        if not out:
            out.append("go build ./... && echo ok")
        return out
