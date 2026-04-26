"""Tools used by the gather / act / verify agents.

Read-only tools (read_file, glob_files, grep) are shared by all agents.
Write tools (write_file, edit_file, run_bash) are only handed to the
coordinator. The verifier gets run_bash too, but with a /tmp-only write
contract enforced by its prompt.

Tool functions follow ADK 1.31.1's FunctionTool convention: typed args,
typed return dict. ADK derives the JSON schema from the signature.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path


def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


def read_file(path: str) -> dict:
    """Read a UTF-8 text file and return its contents.

    Args:
        path: Absolute or relative path to the file.
    """
    p = _resolve(path)
    if not p.exists():
        return {"status": "error", "error": f"file not found: {p}"}
    if not p.is_file():
        return {"status": "error", "error": f"not a regular file: {p}"}
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"status": "error", "error": f"non-utf8 file: {p}"}
    return {"status": "ok", "path": str(p), "content": text}


def glob_files(pattern: str, root: str = ".") -> dict:
    """Find files matching a glob pattern under root.

    Args:
        pattern: Glob like '**/*.py' or 'src/**/*.ts'.
        root: Directory to search from. Defaults to current working dir.
    """
    base = _resolve(root)
    if not base.is_dir():
        return {"status": "error", "error": f"not a directory: {base}"}
    matches = [str(p) for p in base.glob(pattern) if p.is_file()]
    matches.sort()
    return {"status": "ok", "matches": matches[:200], "truncated": len(matches) > 200}


def grep(pattern: str, path: str = ".", glob: str = "**/*") -> dict:
    """Search for a regex pattern across files under path.

    Args:
        pattern: Python regex.
        path: Root directory to search.
        glob: File glob (e.g. '**/*.py'). Defaults to all files.
    """
    base = _resolve(path)
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return {"status": "error", "error": f"bad regex: {e}"}
    hits: list[dict] = []
    for p in base.glob(glob):
        if not p.is_file():
            continue
        try:
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if rx.search(line):
                    hits.append({"file": str(p), "line": i, "text": line[:300]})
                    if len(hits) >= 200:
                        return {"status": "ok", "hits": hits, "truncated": True}
        except (UnicodeDecodeError, OSError):
            continue
    return {"status": "ok", "hits": hits, "truncated": False}


def write_file(path: str, content: str) -> dict:
    """Write text to a file, creating parent directories if needed.

    Args:
        path: File path to write.
        content: Full file contents to write.
    """
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"status": "ok", "path": str(p), "bytes": len(content.encode("utf-8"))}


def edit_file(path: str, old_string: str, new_string: str) -> dict:
    """Replace the first occurrence of old_string with new_string in a file.

    Args:
        path: File to edit.
        old_string: Exact text to find. Must be unique in the file.
        new_string: Replacement text.
    """
    p = _resolve(path)
    if not p.exists():
        return {"status": "error", "error": f"file not found: {p}"}
    text = p.read_text(encoding="utf-8")
    occurrences = text.count(old_string)
    if occurrences == 0:
        return {"status": "error", "error": "old_string not found"}
    if occurrences > 1:
        return {"status": "error", "error": f"old_string is not unique ({occurrences} matches)"}
    p.write_text(text.replace(old_string, new_string, 1), encoding="utf-8")
    return {"status": "ok", "path": str(p)}


def run_bash(command: str, timeout_seconds: int = 30) -> dict:
    """Execute a shell command and return stdout/stderr/exit code.

    Args:
        command: Shell command line.
        timeout_seconds: Max wall time before the process is killed.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as e:
        return {"status": "timeout", "command": command, "stdout": e.stdout or "", "stderr": e.stderr or ""}
    return {
        "status": "ok",
        "command": command,
        "exit_code": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-2000:],
    }
