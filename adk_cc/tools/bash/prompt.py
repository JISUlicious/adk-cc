"""Long-form description for BashTool, separated from the class so future
sandbox/security wording can be edited without touching the handler.
"""

DESCRIPTION = (
    "Execute a shell command and return stdout/stderr/exit code. "
    "In Stage C this delegates to a per-session sandbox; today it runs on "
    "the host. Use sparingly; prefer dedicated tools (read_file, edit_file, "
    "etc.) when one fits."
)
