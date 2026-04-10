"""Heuristic library-vs-app classifier.

Works on a GitHub /repos/{owner}/{repo} API response (or any dict with `name`,
`description`, `topics`, `full_name`). No LLM calls — just set intersections,
regex, and a tiny override list. Sub-millisecond per repo.

Usage:
    from app.classifier import classify_repo, is_app
    cls, score, reason = classify_repo(repo_data)
    if is_app(repo_data):
        keep(repo_data)
"""
from __future__ import annotations

import re
from typing import Literal

Classification = Literal["app", "library", "unknown"]


# -------------------- signal sets --------------------

# Topics that are *explicitly* "this is a library" — weighted heavier below.
STRONG_LIBRARY_TOPICS: set[str] = {
    "python-library",
    "javascript-library",
    "typescript-library",
    "rust-library",
    "go-library",
    "ruby-library",
    "java-library",
    "c-library",
    "cpp-library",
    "utility-library",
    "helper-library",
}

LIBRARY_TOPICS: set[str] = {
    "library",
    "framework",
    "sdk",
    "toolkit",
    "bindings",
    "wrapper",
    "api-client",
    "rest-client",
    "http-client",
    "orm",
    "parser",
    "lexer",
    "ast",
    "compiler",
    "interpreter",
    "runtime",
    "language",
    "typing",
    "type-checking",
    "utility-library",
    "helper-library",
    "python-library",
    "javascript-library",
    "typescript-library",
    "rust-library",
    "go-library",
    "npm-package",
    "pypi",
    "crates-io",
    "dependency",
    "dependencies",
    "machine-learning",  # mostly libraries — has explicit allowlist below for apps
    "deep-learning",
    "neural-network",
    "data-science",
    "numpy",
    "pandas",
    "scikit-learn",
    "tensorflow",
    "pytorch",
    "reactjs",
    "vuejs",
}

APP_TOPICS: set[str] = {
    "cli",
    "command-line",
    "command-line-tool",
    "command-line-app",
    "package-manager",
    "dependency-manager",
    "build-tool",
    "terminal",
    "tui",
    "desktop",
    "desktop-app",
    "desktop-application",
    "electron",
    "electron-app",
    "gui",
    "gui-application",
    "tauri",
    "game",
    "gamedev",
    "game-engine",  # engines are app-ish (you run them); debatable, leaning app
    "tool",
    "productivity",
    "productivity-tools",
    "editor",
    "text-editor",
    "ide",
    "browser",
    "web-browser",
    "mobile-app",
    "android-app",
    "android-application",
    "ios-app",
    "self-hosted",
    "selfhosted",
    "web-app",
    "webapp",
    "web-application",
    "dashboard",
    "monitoring",
    "note-taking",
    "notes",
    "chat",
    "chat-application",
    "messaging",
    "media-player",
    "music-player",
    "video-player",
    "file-manager",
    "launcher",
    "application",
    "app",
    "server",  # self-hostable servers count as apps for UX here
    "daemon",
    "proxy",
    "vpn",
    "database",  # databases are runnable servers, not libs
    "stable-diffusion",
    "llm",
    "chatbot",
    "assistant",
    "agent",
}

LIB_DESC_RE = re.compile(
    r"\b("
    r"library|framework|sdk|toolkit|bindings|wrappers?|"
    r"api\s+client|rest\s+client|http\s+client|"
    r"python\s+package|npm\s+package|pip\s+package|"
    r"collection\s+of|utilities\s+for|helper\s+for|"
    r"minimalist\s+library|minimal\s+library|"
    r"the\s+react\s+framework|"
    r"typing\s+stubs|type\s+stubs|type\s+hints|"
    r"orm|parser|tokenizer|lexer|compiler|transpiler"
    r")\b",
    re.IGNORECASE,
)

# Stronger library phrases — definitive, weight 4.
STRONG_LIB_DESC_RE = re.compile(
    r"("
    r"\blibrary\s+(?:for|to|that|which)\b"
    r"|\btoolkit\s+(?:for|to)\b"
    r"|\bframework\s+(?:for|to)\b"
    r"|\bcomposable\s+\w+\s+(?:library|toolkit|framework)\b"
    r"|\bpython\s+library\b"
    r"|\bnode\s+library\b|\bnodejs\s+library\b|\bnode\.js\s+library\b"
    r"|\btypescript\s+library\b|\bjavascript\s+library\b"
    r"|\brust\s+library\b|\bgo\s+library\b"
    r"|\bdeveloper\s+kit\b|\bdev\s+kit\b|\bofficial\s+\w+\s+sdk\b"
    r"|\bbuild\s+(?:sophisticated\s+)?(?:user\s+interfaces|uis|guis)\b"
    r")",
    re.IGNORECASE,
)

APP_DESC_RE = re.compile(
    r"\b("
    r"cli|command[- ]line|terminal\s+(?:app|ui|tool)|tui|"
    r"desktop\s+(?:app|application|client)|"
    r"web\s+(?:app|application|ui)|"
    r"self[- ]hosted|"
    r"editor|ide|browser|game|player|dashboard|"
    r"note[- ]?taking|chat\s+app|messenger|"
    r"tool\s+for|tool\s+to|"
    r"launcher|file\s+manager|"
    r"alternative\s+to|replacement\s+for|"
    r"run\s+(?:locally|locally\.)|runs\s+locally|"
    r"self[- ]hostable"
    r")\b",
    re.IGNORECASE,
)

# Last-ditch name patterns
LIB_NAME_SUFFIXES = ("-lib", "-library", "-sdk", "-client", "-api", "-py", "-js", "-rs")
LIB_NAME_PREFIXES = ("lib", "py-", "go-", "node-")
APP_NAME_SUFFIXES = ("-cli", "-app", "-tool", "-desktop", "-ui", "-gui")

# -------------------- hardcoded overrides --------------------
# Repos whose topics/description undersell their library-ness.
FAMOUS_LIBRARIES: set[str] = {
    "facebook/react",
    "vuejs/vue",
    "vuejs/core",
    "angular/angular",
    "sveltejs/svelte",
    "vercel/next.js",
    "nuxt/nuxt",
    "remix-run/remix",
    "tensorflow/tensorflow",
    "pytorch/pytorch",
    "keras-team/keras",
    "scikit-learn/scikit-learn",
    "numpy/numpy",
    "pandas-dev/pandas",
    "psf/requests",
    "python/cpython",
    "nodejs/node",
    "golang/go",
    "rust-lang/rust",
    "openai/openai-python",
    "openai/openai-node",
    "anthropics/anthropic-sdk-python",
    "anthropics/anthropic-sdk-typescript",
    "huggingface/transformers",
    "huggingface/diffusers",
    "huggingface/datasets",
    "run-llama/llama_index",
    "langchain-ai/langchain",
    "langchain-ai/langchainjs",
    "fastapi/fastapi",
    "tiangolo/fastapi",
    "pallets/flask",
    "django/django",
    "expressjs/express",
    "fastify/fastify",
    "nestjs/nest",
    "axios/axios",
    "lodash/lodash",
    "moment/moment",
    "date-fns/date-fns",
    "tailwindlabs/tailwindcss",
    "shadcn-ui/ui",
    "radix-ui/primitives",
    "emotion-js/emotion",
    "styled-components/styled-components",
    "pola-rs/polars",
    "apache/arrow",
    "apache/spark",
    "ray-project/ray",
    "python-pillow/Pillow",
    "pydantic/pydantic",
    "sqlalchemy/sqlalchemy",
    "tiangolo/sqlmodel",
    "encode/httpx",
    "aio-libs/aiohttp",
    "nltk/nltk",
    "spaCy/spaCy",
    "explosion/spaCy",
    "gensim/gensim",
    "matplotlib/matplotlib",
    "plotly/plotly.py",
    "bokeh/bokeh",
    "streamlit/streamlit",  # arguable — it's a library for building apps
    "gradio-app/gradio",    # same
    "browser-use/browser-use",
    "microsoft/TypeScript",
    "facebook/jest",
    "vitest-dev/vitest",
    "prettier/prettier",
    "eslint/eslint",
    # "Library for building CLIs" — fundamentally ambiguous to heuristics
    # because they legitimately have `cli` in topics.
    "pallets/click",
    "tiangolo/typer",
    "spf13/cobra",
    "urfave/cli",
    "clap-rs/clap",
    "google/python-fire",
    # Rich text / TUI libraries (not TUI apps themselves)
    "Textualize/rich",
    "willmcgugan/rich",
    # Well-known core libraries with sparse metadata
    "grpc/grpc",
    "protocolbuffers/protobuf",
}

# Repos whose topics/description undersell their app-ness — force-classify as app.
FAMOUS_APPS: set[str] = {
    "microsoft/vscode",
    "zed-industries/zed",
    "obsidianmd/obsidian-releases",
    "logseq/logseq",
    "AppFlowy-IO/AppFlowy",
    "siyuan-note/siyuan",
    "ollama/ollama",
    "AUTOMATIC1111/stable-diffusion-webui",
    "comfyanonymous/ComfyUI",
    "cline/cline",
    "All-Hands-AI/OpenHands",
    "block/goose",
    "stackblitz/bolt.new",
    "astral-sh/ruff",
    "astral-sh/uv",
    "denoland/deno",
    "torvalds/linux",  # debatable; it's an OS kernel but users "run" it
    "kubernetes/kubernetes",
    "hashicorp/terraform",
    "nextcloud/server",
    "gitlab-org/gitlab",
    "home-assistant/core",
    "plausible/analytics",
    "appsmithorg/appsmith",
    "n8n-io/n8n",
    "immich-app/immich",
    "LibreChat-AI/LibreChat",
    "lobehub/lobe-chat",
    "ChatGPTNextWeb/ChatGPT-Next-Web",
    "sharkdp/bat",
    "BurntSushi/ripgrep",
    "fish-shell/fish-shell",
    "tmux/tmux",
}


# -------------------- classifier --------------------

def classify_repo(repo: dict) -> tuple[Classification, int, str]:
    """Return (classification, confidence_score, reason).

    `repo` is expected to be the GitHub /repos/... response shape.
    """
    full_name = (repo.get("full_name") or "").strip()
    if full_name in FAMOUS_LIBRARIES:
        return ("library", 99, "hardcoded FAMOUS_LIBRARIES")
    if full_name in FAMOUS_APPS:
        return ("app", 99, "hardcoded FAMOUS_APPS")

    name = (repo.get("name") or "").lower()
    desc = (repo.get("description") or "").lower()
    topics = {t.lower() for t in (repo.get("topics") or [])}

    lib_score = 0
    app_score = 0
    reasons: list[str] = []

    # Topics. Strong library topics (weight 6) override ambiguous signals.
    strong_lib_hits = topics & STRONG_LIBRARY_TOPICS
    if strong_lib_hits:
        lib_score += 6 * len(strong_lib_hits)
        reasons.append(f"strong_lib_topics={sorted(strong_lib_hits)}")

    lib_topic_hits = topics & LIBRARY_TOPICS
    app_topic_hits = topics & APP_TOPICS
    if lib_topic_hits:
        lib_score += 3 * len(lib_topic_hits)
        reasons.append(f"lib_topics={sorted(lib_topic_hits)}")
    if app_topic_hits:
        app_score += 3 * len(app_topic_hits)
        reasons.append(f"app_topics={sorted(app_topic_hits)}")

    # Description regex (weight 2, strong phrases 4).
    if desc:
        if STRONG_LIB_DESC_RE.search(desc):
            lib_score += 4
            reasons.append("strong_lib_desc")
        elif LIB_DESC_RE.search(desc):
            lib_score += 2
            reasons.append("lib_desc_match")
        if APP_DESC_RE.search(desc):
            app_score += 2
            reasons.append("app_desc_match")

    # Name patterns (weight 1).
    if any(name.endswith(s) for s in LIB_NAME_SUFFIXES) or any(
        name.startswith(p) for p in LIB_NAME_PREFIXES
    ):
        lib_score += 1
        reasons.append("lib_name_pattern")
    if any(name.endswith(s) for s in APP_NAME_SUFFIXES):
        app_score += 1
        reasons.append("app_name_pattern")

    reason_str = " | ".join(reasons) or "no_signal"

    # Decision — prefer "app" in ties, err on the side of showing (unknown).
    if app_score >= 2 and app_score >= lib_score:
        return ("app", app_score, reason_str)
    if lib_score >= 2 and lib_score > app_score:
        return ("library", lib_score, reason_str)
    return ("unknown", max(app_score, lib_score), reason_str)


def is_app(repo: dict) -> bool:
    """Convenience: True if the repo is classified as an app or unknown."""
    cls, _, _ = classify_repo(repo)
    return cls != "library"
