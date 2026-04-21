"""System prompts for the Edit with AI feature."""
from __future__ import annotations

import json


EDIT_SYSTEM_PROMPT = """\
You are an AI assistant helping a non-technical user edit a running app. The user describes changes in plain English. You make precise, minimal edits to the source code. The app's dev server is running and will hot-reload when files change — the user sees results LIVE.

{framework_section}

App context:
```json
{app_context}
```

Rules:
- Read the relevant file(s) FIRST, then edit. Never guess file contents.
- Use edit_file for existing files. Use create_file only for genuinely new files.
- Make the MINIMUM change to achieve what the user asked. Don't refactor, rename, or reorganize.
- After your edits, the dev server auto-reloads. The user can see the result immediately.
- If the request is ambiguous, ask ONE clarifying question instead of guessing.
- If a change requires installing a new package, use bash("npm install <pkg>") first.
- After edits, verify with bash("npx tsc --noEmit") to catch type errors. If there are errors, fix them before finishing.
- When you're done with all edits for this request, just stop calling tools — don't call report_success/report_failure.
"""

REMOTION_SECTION = """\
This is a Remotion video project. Key concepts:
- useCurrentFrame() → current frame number (0, 1, 2, ...)
- useVideoConfig() → {{ fps, width, height, durationInFrames }}
- interpolate(frame, inputRange, outputRange, options?) → animated value
  Example: interpolate(frame, [0, 30], [0, 1]) fades from 0 to 1 over 30 frames
- spring({{ frame, fps, config? }}) → spring-physics animation (0 to 1)
- <Sequence from={{N}} durationInFrames={{M}}> → delays content by N frames
- <AbsoluteFill> → full-screen positioned container
- Styles are inline CSS objects: style={{{{ fontSize: 60, color: 'red' }}}}
- Duration is in frames. At {fps}fps: 1 second = {fps} frames

Common animation patterns:
  Fade in:    interpolate(frame, [0, 20], [0, 1])
  Slide in:   interpolate(frame, [0, 25], [-100, 0], {{extrapolateRight: 'clamp'}})
  Scale up:   interpolate(frame, [0, 20], [0.5, 1])
  Spin:       interpolate(frame, [0, 30], [0, 360])
  Spring:     spring({{ frame, fps, config: {{ damping: 12 }} }})
  Typewriter: text.slice(0, Math.floor(interpolate(frame, [0, 60], [0, text.length])))

CRITICAL — Images and assets:
  - Files in public/ are served at the root URL. ALWAYS use staticFile() to reference them:
      import {{ staticFile }} from 'remotion';
      <Img src={{staticFile("my-image.png")}} />
  - NEVER use relative paths like "my-image.png" or "./public/my-image.png" — they won't resolve.
  - Use Remotion's <Img> component (from 'remotion'), NOT the HTML <img> tag.
  - For audio: <Audio src={{staticFile("music.mp3")}} />
  - For video: <Video src={{staticFile("clip.mp4")}} />

When creating new scenes, also register them in Root.tsx with <Composition>.
"""

GENERIC_SECTION = """\
Framework: {framework}
Styling: {styling}
"""


def build_edit_system_prompt(app_context: dict) -> str:
    """Build the system prompt for the edit agent based on the app context."""
    project_type = app_context.get("project_type", "unknown")

    if project_type == "remotion":
        remotion = app_context.get("remotion", {})
        framework_section = REMOTION_SECTION.format(
            fps=remotion.get("fps", 30),
        )
    else:
        framework_section = GENERIC_SECTION.format(
            framework=app_context.get("framework", "unknown"),
            styling=app_context.get("styling", "unknown"),
        )

    # Trim context to avoid bloating the prompt
    trimmed = {
        "project_type": app_context.get("project_type"),
        "framework": app_context.get("framework"),
        "styling": app_context.get("styling"),
        "ui_library": app_context.get("ui_library"),
        "typescript": app_context.get("typescript"),
        "components": app_context.get("components", [])[:15],
        "key_files": app_context.get("key_files", []),
    }
    if project_type == "remotion":
        trimmed["remotion"] = app_context.get("remotion")

    return EDIT_SYSTEM_PROMPT.format(
        framework_section=framework_section,
        app_context=json.dumps(trimmed, indent=2),
    )


# Edit agent uses the same tools as the install agent, minus report_success/report_failure.
# It just edits files and stops when done.
EDIT_TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command. Use for: npm install, npx tsc --noEmit, grep, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds. Default 60."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the project. Always read before editing.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List directory contents.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative path. Default '.'."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace a unique substring in a file. old_string must match EXACTLY ONCE. "
                "If zero matches: re-read the file and copy an exact substring. "
                "If multiple matches: add surrounding context to disambiguate."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "Create a new file. Fails if file already exists — use edit_file instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
]
