"""Shared types and the LanguageAdapter protocol."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

Language = Literal["python", "node", "go", "rust"]


@dataclass
class EntryPoint:
    kind: str  # e.g. "pyproject_script", "module_main", "web_app", "readme_code", "package_json_script", "go_main", "cargo_bin"
    value: str
    source: str  # where we learned this (filename:line, or "pyproject.toml [project.scripts]")


@dataclass
class RequiredEnvVar:
    name: str
    source: str
    required: bool = True


@dataclass
class ParsedDeps:
    declared_deps: list[str] = field(default_factory=list)
    dep_files: list[str] = field(default_factory=list)
    package_managers: list[str] = field(default_factory=list)
    candidate_entry_points: list[EntryPoint] = field(default_factory=list)
    app_type_hint: str = "unknown"  # cli | web | gui | library | unknown
    # Per-adapter extras (e.g. package.json scripts, cargo [[bin]] list).
    extras: dict = field(default_factory=dict)


@dataclass
class SandboxInfo:
    """Returned by bootstrap_sandbox — injected into bash env when the LLM runs."""
    env: dict[str, str] = field(default_factory=dict)
    path_prepend: list[str] = field(default_factory=list)  # dirs added to PATH
    notes: list[str] = field(default_factory=list)  # human-readable setup log


class LanguageAdapter(Protocol):
    name: Language

    def detect(self, tree: list[str], files: dict[str, str]) -> float:
        """Return 0.0-1.0 confidence this repo is of `name` language."""

    def parse_deps(self, workdir: Path, tree: list[str], files: dict[str, str]) -> ParsedDeps:
        ...

    def bootstrap_sandbox(self, workdir: Path) -> SandboxInfo:
        ...

    def install_cmd(self, parsed: ParsedDeps) -> str:
        ...

    def smoke_run_candidates(self, parsed: ParsedDeps) -> list[str]:
        ...
