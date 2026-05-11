"""Read a UTF-8 text file with line offset/limit + per-line length cap.

The default tool was returning the entire file in `content`. For large
files that overflows the LLM context window (or the
`ContextGuardPlugin`'s REJECT threshold) and crashes the next model
call. This version slices to a requested line range with sane defaults
(first 2000 lines) and truncates pathological lines (>2000 chars) so
minified bundles or generated files don't explode a single result.

The defaults and `cat -n`-style line-numbered content match upstream
Claude Code's Read tool so prompts ported from there work unchanged.

Response shape:

    {
      "status": "ok",
      "path": "<absolute or workspace-relative path>",
      "content": "     1\\tfirst line\\n     2\\tsecond line",  # cat -n style
      "start_line": 1,                     # 1-indexed, echoes args.offset
      "end_line": 2000,                    # 1-indexed inclusive, last line returned
      "total_lines": 5234,                 # total line count of the file
      "total_bytes": 184321,               # byte size of file on disk (post-decode UTF-8)
      "has_more": true,                    # True if total_lines > end_line
      "lines_truncated": 0,                # count of lines clipped by per-line cap
    }

The model uses `has_more` + `total_lines` to decide whether to call
again with `offset = end_line + 1`. `total_bytes` is the up-front size
hint — if it's huge, the model should consider grepping for what it
needs instead of paginating through every line.
"""
from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext

from ..sandbox import SandboxViolation, get_backend, get_workspace
from ._fs import resolve
from .base import AdkCcTool, ToolMeta
from .schemas import ReadFileArgs

# Per-line truncation guard for files with pathological line lengths
# (minified JS, generated code, etc). Truncated lines get a visible
# suffix so the model knows the slice it's looking at is incomplete.
_MAX_LINE_LENGTH = 2000
_LINE_TRUNCATION_SUFFIX = "… [truncated]"


def _format_with_line_numbers(lines: list[str], start_line: int) -> str:
    """Format lines as `<right-padded line number>\\t<text>` per upstream
    Claude Code's Read tool convention. Six-character right-justified
    number is enough for ~1M-line files."""
    return "\n".join(
        f"{start_line + i:>6}\t{line}" for i, line in enumerate(lines)
    )


class ReadFileTool(AdkCcTool):
    meta = ToolMeta(
        name="read_file",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model = ReadFileArgs
    description = (
        "Read a UTF-8 text file. Returns the slice from `offset` for up to "
        "`limit` lines (defaults: lines 1-1000) in `cat -n` format. The "
        "response also includes `total_lines`, `total_bytes`, and `has_more` "
        "so you can decide whether to paginate (next call with "
        "`offset = end_line + 1`) or use `grep`/`glob_files` for a more "
        "targeted lookup on a huge file. Lines longer than "
        f"{_MAX_LINE_LENGTH} chars are individually truncated."
    )

    async def _execute(self, args: ReadFileArgs, ctx: ToolContext) -> dict[str, Any]:
        p = resolve(args.path, ctx)
        backend = get_backend(ctx)
        ws = get_workspace(ctx)
        try:
            text = await backend.read_text(str(p), fs_read=ws.fs_read_config())
        except SandboxViolation as e:
            return {"status": "sandbox_denied", "error": str(e)}
        except FileNotFoundError:
            return {"status": "error", "error": f"file not found: {p}"}
        except IsADirectoryError:
            return {"status": "error", "error": f"not a regular file: {p}"}
        except UnicodeDecodeError:
            return {"status": "error", "error": f"non-utf8 file: {p}"}

        total_bytes = len(text.encode("utf-8"))
        # splitlines() handles \n, \r\n, and \r uniformly and does not
        # include the terminator in each element — fine for our purposes
        # (we re-emit one terminator per line).
        all_lines = text.splitlines()
        total_lines = len(all_lines)

        start_idx = args.offset - 1
        end_idx = min(start_idx + args.limit, total_lines)
        sliced = all_lines[start_idx:end_idx] if start_idx < total_lines else []

        lines_truncated = 0
        out_lines: list[str] = []
        for line in sliced:
            if len(line) > _MAX_LINE_LENGTH:
                out_lines.append(line[:_MAX_LINE_LENGTH] + _LINE_TRUNCATION_SUFFIX)
                lines_truncated += 1
            else:
                out_lines.append(line)

        # end_line is the 1-indexed line number of the last returned line,
        # or `start_line - 1` for an empty slice (when offset > total_lines).
        end_line = start_idx + len(sliced)
        content = _format_with_line_numbers(out_lines, args.offset)
        return {
            "status": "ok",
            "path": str(p),
            "content": content,
            "start_line": args.offset,
            "end_line": end_line,
            "total_lines": total_lines,
            "total_bytes": total_bytes,
            "has_more": end_line < total_lines,
            "lines_truncated": lines_truncated,
        }
