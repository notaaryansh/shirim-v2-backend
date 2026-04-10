"""Rust language adapter."""
from __future__ import annotations

from pathlib import Path

from .base import EntryPoint, ParsedDeps, SandboxInfo

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


class RustAdapter:
    name = "rust"

    def detect(self, tree: list[str], files: dict[str, str]) -> float:
        if "Cargo.toml" not in files:
            return 0.0
        score = 0.6
        if "Cargo.lock" in files:
            score += 0.2
        rs_count = sum(1 for r in tree if r.endswith(".rs"))
        if rs_count >= 3:
            score += 0.2
        elif rs_count >= 1:
            score += 0.1
        return min(score, 1.0)

    def parse_deps(
        self, workdir: Path, tree: list[str], files: dict[str, str]
    ) -> ParsedDeps:
        deps: list[str] = []
        dep_files: list[str] = []
        entry_points: list[EntryPoint] = []
        extras: dict = {}

        if "Cargo.toml" in files:
            dep_files.append("Cargo.toml")
            if tomllib:
                try:
                    data = tomllib.loads(files["Cargo.toml"])
                except Exception:
                    data = {}
            else:
                data = {}
            for name, spec in (data.get("dependencies") or {}).items():
                if isinstance(spec, str):
                    deps.append(f"{name}@{spec}")
                else:
                    deps.append(name)
            # Package name / bins
            pkg = data.get("package") or {}
            extras["package_name"] = pkg.get("name")
            extras["package_version"] = pkg.get("version")

            # Explicit [[bin]] tables
            bins = data.get("bin") or []
            if isinstance(bins, list):
                for b in bins:
                    if isinstance(b, dict) and "name" in b:
                        entry_points.append(
                            EntryPoint(
                                kind="cargo_bin",
                                value=f"cargo run --bin {b['name']}",
                                source=f"Cargo.toml [[bin]] {b['name']}",
                            )
                        )
            # Implicit bins: src/main.rs → package name binary
            if "src/main.rs" in tree:
                entry_points.append(
                    EntryPoint(
                        kind="cargo_main",
                        value="cargo run",
                        source="src/main.rs",
                    )
                )
            # Implicit bins in src/bin/*.rs
            for rel in tree:
                if rel.startswith("src/bin/") and rel.endswith(".rs"):
                    bin_name = rel[len("src/bin/"):-3]
                    entry_points.append(
                        EntryPoint(
                            kind="cargo_bin",
                            value=f"cargo run --bin {bin_name}",
                            source=rel,
                        )
                    )

        if "Cargo.lock" in files:
            dep_files.append("Cargo.lock")

        # App type: bin present → cli; lib only → library; web server dep → web
        app_type = "library"
        if entry_points:
            app_type = "cli"
        all_deps_flat = " ".join(deps).lower()
        if any(k in all_deps_flat for k in ("actix-web", "rocket", "axum", "warp", "hyper", "tide", "poem")):
            app_type = "web"

        return ParsedDeps(
            declared_deps=deps,
            dep_files=dep_files,
            package_managers=["cargo"],
            candidate_entry_points=entry_points,
            app_type_hint=app_type,
            extras=extras,
        )

    def bootstrap_sandbox(self, workdir: Path) -> SandboxInfo:
        cargo_home = workdir / ".shirim-cargo-home"
        target = workdir / ".shirim-target"
        cargo_home.mkdir(parents=True, exist_ok=True)
        target.mkdir(parents=True, exist_ok=True)
        return SandboxInfo(
            env={
                "CARGO_HOME": str(cargo_home),
                "CARGO_TARGET_DIR": str(target),
            },
            path_prepend=[],
            notes=[f"CARGO_HOME: {cargo_home}", f"CARGO_TARGET_DIR: {target}"],
        )

    def install_cmd(self, parsed: ParsedDeps) -> str:
        # fetch is fast and just downloads deps; real compilation happens on first build.
        return "cargo fetch"

    def smoke_run_candidates(self, parsed: ParsedDeps) -> list[str]:
        out: list[str] = ["cargo check"]
        for ep in parsed.candidate_entry_points:
            if ep.kind in ("cargo_main", "cargo_bin"):
                out.append(f"timeout 60 {ep.value} -- --help 2>&1 || true")
        return out
