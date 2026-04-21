"""Scan an installed TypeScript/Remotion project and build a compact context
summary for the edit agent.

The context tells the agent what framework is in use, what components exist,
what styling system is used, and where key files are. Cached after first scan.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)


def scan_app_context(workdir: Path) -> dict:
    """Scan the workdir and return a compact context dict."""
    pkg_path = workdir / "package.json"
    if not pkg_path.exists():
        return {"project_type": "unknown", "error": "no package.json found"}

    try:
        pkg = json.loads(pkg_path.read_text())
    except Exception:
        pkg = {}

    all_deps = {
        **(pkg.get("dependencies") or {}),
        **(pkg.get("devDependencies") or {}),
    }

    ctx: dict = {
        "name": pkg.get("name", "unknown"),
        "project_type": _detect_project_type(all_deps),
        "framework": _detect_framework(all_deps),
        "styling": _detect_styling(all_deps, workdir),
        "ui_library": _detect_ui_library(all_deps),
        "typescript": "typescript" in all_deps,
        "scripts": {k: v for k, v in (pkg.get("scripts") or {}).items()
                    if k in ("dev", "start", "build", "preview", "lint", "test")},
    }

    # Remotion-specific context
    if ctx["project_type"] == "remotion":
        ctx["remotion"] = _scan_remotion(workdir, pkg)

    # Component inventory
    ctx["components"] = _find_components(workdir)

    # Key files (entry points, layouts, configs)
    ctx["key_files"] = _find_key_files(workdir)

    return ctx


def _detect_project_type(deps: dict) -> str:
    if "remotion" in deps:
        return "remotion"
    if "next" in deps:
        return "nextjs"
    if "nuxt" in deps:
        return "nuxt"
    if "vite" in deps or "@vitejs/plugin-react" in deps:
        return "vite-react"
    if "react-scripts" in deps:
        return "cra"
    if "svelte" in deps:
        return "svelte"
    if "react" in deps:
        return "react"
    return "unknown"


def _detect_framework(deps: dict) -> str:
    if "remotion" in deps:
        return "remotion"
    if "next" in deps:
        return "Next.js"
    if "nuxt" in deps:
        return "Nuxt"
    if "@vitejs/plugin-react" in deps:
        return "Vite + React"
    if "react-scripts" in deps:
        return "Create React App"
    if "svelte" in deps:
        return "Svelte"
    if "react" in deps:
        return "React"
    return "unknown"


def _detect_styling(deps: dict, workdir: Path) -> str:
    if "tailwindcss" in deps:
        return "Tailwind CSS"
    if "styled-components" in deps:
        return "styled-components"
    if "@emotion/react" in deps or "@emotion/styled" in deps:
        return "Emotion"
    if "@mui/material" in deps:
        return "Material UI (built-in)"
    if (workdir / "src").exists():
        css_modules = list((workdir / "src").rglob("*.module.css"))
        if css_modules:
            return "CSS Modules"
    return "inline CSS / plain CSS"


def _detect_ui_library(deps: dict) -> str | None:
    if "@shadcn/ui" in deps or "class-variance-authority" in deps:
        return "shadcn/ui"
    if "@mui/material" in deps:
        return "Material UI"
    if "@chakra-ui/react" in deps:
        return "Chakra UI"
    if "@radix-ui/react-dialog" in deps or "@radix-ui/themes" in deps:
        return "Radix UI"
    if "@mantine/core" in deps:
        return "Mantine"
    return None


def _scan_remotion(workdir: Path, pkg: dict) -> dict:
    """Remotion-specific context: compositions, fps, dimensions."""
    remotion_ctx: dict = {
        "compositions": [],
        "fps": 30,
        "width": 1920,
        "height": 1080,
    }

    # Try to find Root.tsx and extract compositions
    root_candidates = [
        workdir / "src" / "Root.tsx",
        workdir / "src" / "Root.jsx",
        workdir / "src" / "index.tsx",
        workdir / "src" / "Video.tsx",
    ]
    for root_path in root_candidates:
        if root_path.exists():
            try:
                content = root_path.read_text(errors="replace")
                # Find <Composition> declarations
                comp_re = re.compile(
                    r'<Composition\s[^>]*?'
                    r'id\s*=\s*["\']([^"\']+)["\']'
                    r'[^>]*?'
                    r'component\s*=\s*\{(\w+)\}'
                    r'[^>]*?'
                    r'durationInFrames\s*=\s*\{?(\d+)\}?',
                    re.DOTALL,
                )
                for m in comp_re.finditer(content):
                    remotion_ctx["compositions"].append({
                        "id": m.group(1),
                        "component": m.group(2),
                        "durationInFrames": int(m.group(3)),
                    })

                # Extract fps/width/height if present
                fps_m = re.search(r'fps\s*=\s*\{?(\d+)\}?', content)
                if fps_m:
                    remotion_ctx["fps"] = int(fps_m.group(1))
                w_m = re.search(r'width\s*=\s*\{?(\d+)\}?', content)
                if w_m:
                    remotion_ctx["width"] = int(w_m.group(1))
                h_m = re.search(r'height\s*=\s*\{?(\d+)\}?', content)
                if h_m:
                    remotion_ctx["height"] = int(h_m.group(1))
            except Exception as e:
                log.warning("failed to parse Remotion root %s: %s", root_path, e)
            break

    return remotion_ctx


def _find_components(workdir: Path, max_results: int = 30) -> list[dict]:
    """Find React/TypeScript component files."""
    components: list[dict] = []
    search_dirs = [
        workdir / "src",
        workdir / "app",
        workdir / "components",
        workdir / "src" / "components",
        workdir / "src" / "scenes",
    ]
    seen: set[str] = set()
    for d in search_dirs:
        if not d.exists():
            continue
        for f in sorted(d.rglob("*.tsx")):
            rel = str(f.relative_to(workdir))
            if rel in seen:
                continue
            seen.add(rel)
            # Skip test files, stories, node_modules
            if any(skip in rel for skip in ("node_modules", ".test.", ".spec.", ".stories.", "__tests__")):
                continue
            components.append({
                "path": rel,
                "name": f.stem,
            })
            if len(components) >= max_results:
                return components
    return components


def _find_key_files(workdir: Path) -> list[str]:
    """Find entry points, layouts, configs that the agent should know about."""
    key_patterns = [
        "src/Root.tsx", "src/Root.jsx",
        "src/App.tsx", "src/App.jsx",
        "src/index.tsx", "src/index.jsx",
        "src/main.tsx", "src/main.jsx",
        "app/layout.tsx", "app/page.tsx",
        "pages/index.tsx", "pages/_app.tsx",
        "remotion.config.ts", "remotion.config.js",
        "tailwind.config.ts", "tailwind.config.js",
        "vite.config.ts", "next.config.ts", "next.config.js",
        "tsconfig.json",
    ]
    found: list[str] = []
    for pattern in key_patterns:
        if (workdir / pattern).exists():
            found.append(pattern)
    return found
