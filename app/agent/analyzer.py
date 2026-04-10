"""Deterministic pre-analysis. No LLM calls.

Walks the cloned repo, picks a language adapter, lets it parse deps/entry points,
scans for required env vars, and emits a structured analysis dict + writes it to
`analysis.json` in the workdir.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict
from pathlib import Path

from .adapters.base import Language, LanguageAdapter, ParsedDeps, RequiredEnvVar
from .sandbox import walk_repo_tree

log = logging.getLogger(__name__)

# Files we read into memory to hand to adapters. Kept small to bound cost.
INTERESTING_FILES = {
    "README.md", "README.rst", "README.txt", "README",
    "readme.md", "readme.rst",
    "requirements.txt", "requirements-dev.txt", "requirements-test.txt",
    "pyproject.toml", "setup.py", "setup.cfg", "Pipfile",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "go.mod", "go.sum",
    "Cargo.toml", "Cargo.lock",
    ".env.example", ".env.sample",
    "Makefile",
}
MAX_FILE_BYTES = 64 * 1024  # 64KB per file — plenty for config/metadata files.

# Env-var-shaped tokens in code/docs.
ENV_VAR_RE = re.compile(
    r'(?<![A-Z0-9_])([A-Z][A-Z0-9_]{3,})(?=\s*[:=]|\s*\?=|\s*\)|\s*\.|\s*["\'])'
)
# Things that look like env vars but aren't secrets the user supplies.
ENV_VAR_BLOCKLIST = {
    "PATH", "HOME", "USER", "SHELL", "PWD", "TERM", "LANG", "LC_ALL", "LC_CTYPE",
    "DEBUG", "PORT", "HOST", "NODE_ENV", "PYTHON", "PYTHONPATH", "GOPATH",
    "CARGO_HOME", "RUST_BACKTRACE", "TMPDIR", "TMP", "TEMP",
    "LOG_LEVEL", "LOGLEVEL", "ENVIRONMENT", "ENV",
    "CI", "GITHUB_ACTIONS", "GITHUB_TOKEN",  # infra
    "TRUE", "FALSE", "NULL", "NONE",
    # Common doc / prose words that look like env vars
    "README", "LICENSE", "NOTICE", "CHANGELOG", "TODO", "FIXME",
    "NOTE", "HACK", "XXX", "WARN", "WARNING", "ERROR", "INFO", "TRACE",
    "HTTP", "HTTPS", "JSON", "YAML", "HTML", "CSS", "API", "URL", "URI",
    "GET", "POST", "PUT", "DELETE", "PATCH",
}
# Well-known "user-supplied secret" names — mark as required=True if seen.
WELL_KNOWN_SECRETS = {
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "HUGGINGFACE_TOKEN", "HF_TOKEN",
    "DATABASE_URL", "REDIS_URL", "POSTGRES_URL", "MONGODB_URI",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "STRIPE_API_KEY", "STRIPE_SECRET_KEY",
    "SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY",
    "GCP_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS",
    "DISCORD_TOKEN", "SLACK_TOKEN", "TELEGRAM_TOKEN",
    "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
    "SENDGRID_API_KEY", "RESEND_API_KEY", "MAILGUN_API_KEY",
}


def _read_files(workdir: Path, tree: list[str]) -> dict[str, str]:
    """Read the subset of files we care about into memory."""
    out: dict[str, str] = {}
    rel_set = set(tree)
    for name in INTERESTING_FILES:
        if name not in rel_set:
            continue
        full = workdir / name
        try:
            data = full.read_bytes()[:MAX_FILE_BYTES]
            out[name] = data.decode("utf-8", errors="replace")
        except Exception as e:
            log.debug("skipped reading %s: %s", name, e)
    return out


def _scan_env_vars(files: dict[str, str], tree_sample: list[str]) -> list[RequiredEnvVar]:
    """Collect env-var-shaped tokens from README + .env.example. Cross-check against
    well-known secret names to mark required=True."""
    seen: dict[str, str] = {}  # name -> source

    # Prefer .env.example if present — explicit declarations.
    for fname in (".env.example", ".env.sample"):
        if fname in files:
            for line in files[fname].splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name = line.split("=", 1)[0].strip()
                if name and name.isupper() and name not in ENV_VAR_BLOCKLIST:
                    seen.setdefault(name, fname)

    # Scan README + any other interesting docs.
    for fname in ("README.md", "README.rst", "README.txt", "README",
                  "readme.md", "readme.rst"):
        if fname in files:
            for m in ENV_VAR_RE.finditer(files[fname]):
                name = m.group(1)
                if name in ENV_VAR_BLOCKLIST:
                    continue
                seen.setdefault(name, fname)

    results: list[RequiredEnvVar] = []
    for name, source in seen.items():
        required = name in WELL_KNOWN_SECRETS or source.startswith(".env")
        results.append(RequiredEnvVar(name=name, source=source, required=required))
    return results


def _language_distribution(tree: list[str]) -> dict[str, float]:
    """Rough language mix by file extension."""
    ext_map = {
        ".py": "python",
        ".js": "node", ".ts": "node", ".mjs": "node", ".cjs": "node",
        ".jsx": "node", ".tsx": "node",
        ".go": "go",
        ".rs": "rust",
        ".md": "markdown",
        ".sh": "shell",
        ".yml": "yaml", ".yaml": "yaml",
        ".toml": "toml",
        ".json": "json",
        ".html": "html",
    }
    counts: dict[str, int] = {}
    total = 0
    for rel in tree:
        ext = "." + rel.rsplit(".", 1)[-1] if "." in rel else ""
        lang = ext_map.get(ext)
        if not lang:
            continue
        counts[lang] = counts.get(lang, 0) + 1
        total += 1
    if total == 0:
        return {}
    return {k: round(v / total, 3) for k, v in counts.items()}


def analyze(
    workdir: Path,
    adapters: list[LanguageAdapter],
) -> dict:
    """Run the full deterministic analysis pipeline. Returns a JSON-able dict and
    writes it to `{workdir}/analysis.json`."""
    tree = walk_repo_tree(workdir)
    files = _read_files(workdir, tree)
    languages_detected = _language_distribution(tree)

    # Pick the winning adapter. Tie-break: adapter self-reported confidence wins;
    # if zero, fall back to extension majority.
    scored: list[tuple[float, LanguageAdapter]] = [
        (adapter.detect(tree, files), adapter) for adapter in adapters
    ]
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_adapter = scored[0] if scored else (0.0, None)

    language: Language | None = None
    parsed: ParsedDeps
    if best_adapter and best_score > 0:
        language = best_adapter.name
        parsed = best_adapter.parse_deps(workdir, tree, files)
    else:
        parsed = ParsedDeps()

    env_vars = _scan_env_vars(files, tree)

    readme_excerpt = ""
    for name in ("README.md", "readme.md", "README.rst", "README.txt", "README"):
        if name in files:
            readme_excerpt = files[name][:2000]
            break

    result: dict = {
        "language": language,
        "languages_detected": languages_detected,
        "dep_files": parsed.dep_files,
        "package_managers": parsed.package_managers,
        "declared_deps": parsed.declared_deps,
        "candidate_entry_points": [asdict(ep) for ep in parsed.candidate_entry_points],
        "app_type_hint": parsed.app_type_hint,
        "required_env_vars": [asdict(ev) for ev in env_vars],
        "readme_excerpt": readme_excerpt,
        "file_tree": tree,
        "extras": parsed.extras,
        "warnings": [],
    }

    if not language:
        result["warnings"].append(
            "language_unknown: no adapter matched; LLM loop will not start"
        )

    (workdir / "analysis.json").write_text(json.dumps(result, indent=2))
    return result
