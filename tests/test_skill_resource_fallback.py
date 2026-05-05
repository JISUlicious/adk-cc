"""Unit tests for `_LenientLoadSkillResourceTool`.

The fallback handles real-world skills (including Anthropic's official
ones) that don't strictly follow the references/scripts/assets layout.
Three behaviors are exercised:

  - Canonical path that ADK's strict lookup resolves: returned unchanged
    (no `fallback_resolved` flag).
  - Non-canonical path the model guesses (e.g. `scripts/<root_file>.md`):
    falls back to a basename scan, finds the file at the skill root,
    returns `fallback_resolved=True` with `actual_path`.
  - Path traversal attempts (`../etc/passwd`): rejected.

Run: `uv run python tests/test_skill_resource_fallback.py`
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")


def _write_skill(root: Path, name: str, *, root_files: dict[str, str] = None,
                 references: dict[str, str] = None,
                 scripts: dict[str, str] = None) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    # Minimal valid SKILL.md (frontmatter required, name must match dir).
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill {name}\n---\nbody",
        encoding="utf-8",
    )
    for filename, content in (root_files or {}).items():
        (skill_dir / filename).write_text(content, encoding="utf-8")
    if references:
        (skill_dir / "references").mkdir()
        for filename, content in references.items():
            (skill_dir / "references" / filename).write_text(content, encoding="utf-8")
    if scripts:
        (skill_dir / "scripts").mkdir()
        for filename, content in scripts.items():
            (skill_dir / "scripts" / filename).write_text(content, encoding="utf-8")
    return skill_dir


def _run(coro):
    return asyncio.run(coro)


# === Tests ===


def test_canonical_path_unchanged():
    """A path that ADK's strict tool already resolves should NOT carry
    `fallback_resolved` — the wrapper only kicks in on miss."""
    print("test_canonical_path_unchanged: ", end="")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_skill(
            base, "demo",
            references={"hello.md": "canonical content"},
        )
        from adk_cc.tools.skills import make_skill_toolset
        toolset = make_skill_toolset(skills_dir=base)
        load_resource = next(
            t for t in toolset._tools if t.name == "load_skill_resource"
        )

        result = _run(load_resource.run_async(
            args={"skill_name": "demo", "file_path": "references/hello.md"},
            tool_context=None,
        ))
        assert result.get("content") == "canonical content"
        assert result.get("fallback_resolved") is None  # not a fallback
    print("OK")


def test_root_file_via_basename():
    """Anthropic's pptx-style layout: model guesses `scripts/foo.md` but
    foo.md is actually at the skill root. Fallback should resolve it."""
    print("test_root_file_via_basename: ", end="")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_skill(
            base, "demo",
            root_files={"pptxgenjs.md": "ROOT FILE CONTENT"},
            scripts={"clean.py": "print('clean')"},
        )
        from adk_cc.tools.skills import make_skill_toolset
        toolset = make_skill_toolset(skills_dir=base)
        load_resource = next(
            t for t in toolset._tools if t.name == "load_skill_resource"
        )

        # Model guesses scripts/ — wrong, but fallback finds it.
        result = _run(load_resource.run_async(
            args={"skill_name": "demo", "file_path": "scripts/pptxgenjs.md"},
            tool_context=None,
        ))
        assert result.get("content") == "ROOT FILE CONTENT", result
        assert result.get("fallback_resolved") is True
        assert result.get("actual_path") == "pptxgenjs.md", result
    print("OK")


def test_root_file_via_literal_path():
    """If the model uses the bare filename as file_path (no prefix), ADK
    rejects with INVALID_RESOURCE_PATH; fallback's literal-path branch
    resolves it from the skill root."""
    print("test_root_file_via_literal_path: ", end="")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_skill(
            base, "demo",
            root_files={"editing.md": "EDITING DOC"},
        )
        from adk_cc.tools.skills import make_skill_toolset
        toolset = make_skill_toolset(skills_dir=base)
        load_resource = next(
            t for t in toolset._tools if t.name == "load_skill_resource"
        )

        result = _run(load_resource.run_async(
            args={"skill_name": "demo", "file_path": "editing.md"},
            tool_context=None,
        ))
        assert result.get("content") == "EDITING DOC", result
        assert result.get("fallback_resolved") is True
    print("OK")


def test_ambiguous_basename_keeps_original_error():
    """If the basename appears in multiple subdirs, fallback skips —
    don't pick arbitrarily. Original RESOURCE_NOT_FOUND stands."""
    print("test_ambiguous_basename_keeps_original_error: ", end="")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_skill(
            base, "demo",
            root_files={"shared.md": "root"},
            references={"shared.md": "ref"},
            scripts={"shared.md": "script"},
        )
        from adk_cc.tools.skills import make_skill_toolset
        toolset = make_skill_toolset(skills_dir=base)
        load_resource = next(
            t for t in toolset._tools if t.name == "load_skill_resource"
        )

        # Path doesn't match canonical lookup AND multiple basename matches → no fallback.
        result = _run(load_resource.run_async(
            args={"skill_name": "demo", "file_path": "assets/shared.md"},
            tool_context=None,
        ))
        # ADK returns RESOURCE_NOT_FOUND; fallback declines (ambiguous).
        assert result.get("error_code") == "RESOURCE_NOT_FOUND", result
        assert result.get("content") is None
    print("OK")


def test_path_traversal_rejected():
    """Paths attempting to escape the skill dir must not resolve."""
    print("test_path_traversal_rejected: ", end="")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_skill(base, "demo", root_files={"ok.md": "ok"})
        # Place a sibling file outside the skill dir.
        outside = base.parent / "secret.txt"
        outside.write_text("SHOULD NOT BE REACHABLE", encoding="utf-8")
        try:
            from adk_cc.tools.skills import make_skill_toolset
            toolset = make_skill_toolset(skills_dir=base)
            load_resource = next(
                t for t in toolset._tools if t.name == "load_skill_resource"
            )

            result = _run(load_resource.run_async(
                args={
                    "skill_name": "demo",
                    "file_path": "../../secret.txt",
                },
                tool_context=None,
            ))
            # Should NOT resolve to the outside file.
            assert result.get("content") != "SHOULD NOT BE REACHABLE", result
            assert "fallback_resolved" not in result or not result["fallback_resolved"]
        finally:
            outside.unlink(missing_ok=True)
    print("OK")


def test_unknown_skill_returns_skill_not_found():
    """Fallback only fires on RESOURCE_NOT_FOUND / INVALID_RESOURCE_PATH —
    SKILL_NOT_FOUND should pass through unchanged."""
    print("test_unknown_skill_returns_skill_not_found: ", end="")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_skill(base, "demo", root_files={"x.md": "x"})
        from adk_cc.tools.skills import make_skill_toolset
        toolset = make_skill_toolset(skills_dir=base)
        load_resource = next(
            t for t in toolset._tools if t.name == "load_skill_resource"
        )

        result = _run(load_resource.run_async(
            args={"skill_name": "no-such-skill", "file_path": "x.md"},
            tool_context=None,
        ))
        assert result.get("error_code") == "SKILL_NOT_FOUND", result
    print("OK")


def main():
    test_canonical_path_unchanged()
    test_root_file_via_basename()
    test_root_file_via_literal_path()
    test_ambiguous_basename_keeps_original_error()
    test_path_traversal_rejected()
    test_unknown_skill_returns_skill_not_found()
    print()
    print("All skill-resource-fallback tests passed")


if __name__ == "__main__":
    main()
