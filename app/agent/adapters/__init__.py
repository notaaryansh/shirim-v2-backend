from .base import EntryPoint, Language, LanguageAdapter, ParsedDeps, SandboxInfo
from .go import GoAdapter
from .node import NodeAdapter
from .python import PythonAdapter
from .rust import RustAdapter


def all_adapters() -> list[LanguageAdapter]:
    return [PythonAdapter(), NodeAdapter(), GoAdapter(), RustAdapter()]


def get_adapter(name: Language) -> LanguageAdapter:
    for a in all_adapters():
        if a.name == name:
            return a
    raise ValueError(f"unknown language: {name}")


__all__ = [
    "EntryPoint",
    "Language",
    "LanguageAdapter",
    "ParsedDeps",
    "SandboxInfo",
    "all_adapters",
    "get_adapter",
]
