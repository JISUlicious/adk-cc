"""Long-form description for BashTool, separated from the class so the
sandbox/security wording can be edited without touching the handler.
"""

DESCRIPTION = (
    "Run a shell command in the session's sandbox and return its stdout, "
    "stderr, and exit code. Default timeout is 30s. Prefer the dedicated "
    "tools (`read_file`, `edit_file`, `write_file`, `glob_files`, `grep`) "
    "when one fits — they're cheaper and don't go through the shell. "
    "Use `run_bash` for things only a shell can do: building, running "
    "tests, invoking CLIs, multi-command pipelines."
)
