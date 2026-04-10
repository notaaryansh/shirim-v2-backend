"""Hardcoded owner/repo slugs shown in each category on the frontend.

Category names match the frontend's `VIEW_CATEGORIES` in src/App.tsx exactly.
Edit freely — logic in routes/repos.py is data-driven off this file.
"""

CURATED: dict[str, list[str]] = {
    # --- Home tab ---
    "Popular": [
        "vercel/next.js",
        "facebook/react",
        "microsoft/vscode",
        "torvalds/linux",
        "tensorflow/tensorflow",
        "kubernetes/kubernetes",
    ],
    "Recently Run": [
        "ollama/ollama",
        "langchain-ai/langchain",
        "astral-sh/ruff",
        "astral-sh/uv",
        "denoland/deno",
    ],
    # --- Discover tab ---
    "Productivity": [
        "logseq/logseq",
        "AppFlowy-IO/AppFlowy",
        "siyuan-note/siyuan",
        "zed-industries/zed",
        "obsidianmd/obsidian-releases",
    ],
    "AI": [
        "openai/openai-python",
        "anthropics/anthropic-sdk-python",
        "huggingface/transformers",
        "run-llama/llama_index",
        "comfyanonymous/ComfyUI",
        "AUTOMATIC1111/stable-diffusion-webui",
    ],
    "Trending": [
        "cline/cline",
        "All-Hands-AI/OpenHands",
        "stackblitz/bolt.new",
        "block/goose",
        "browser-use/browser-use",
    ],
}

HOME_CATEGORIES = ["Popular", "Recently Run"]
DISCOVER_CATEGORIES = ["Productivity", "AI", "Trending"]
