"""Tests for bounded/paginated skill-resource loading + guards (tools/skills).

Covers the build:
  - Phase 1: load_skill_resource returns bounded, paginated slices (read_file
    -style envelope) + per-line cap; load_skill caps instructions.
  - Lazy: oversized references are pruned from RAM but still served (bounded)
    via the disk fallback.
  - Phase 1.5: search_skill_resource greps within a skill.
  - Phase 2 (ADK_CC_SKILL_GUARDS=1): content wrapped as untrusted; run_skill
    _script refused under the noop backend.

Hand-rolled (no pytest). Existing fallback behavior is covered by
test_skill_resource_fallback.py.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.pop("ADK_CC_SANDBOX_BACKEND", None)  # ensure noop default for guard test


def _write_skill(root, name, *, body="body", root_files=None, references=None,
                 scripts=None):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill {name}\n---\n{body}",
        encoding="utf-8",
    )
    for fn, c in (root_files or {}).items():
        (d / fn).write_text(c, encoding="utf-8")
    if references:
        (d / "references").mkdir()
        for fn, c in references.items():
            (d / "references" / fn).write_text(c, encoding="utf-8")
    if scripts:
        (d / "scripts").mkdir()
        for fn, c in scripts.items():
            (d / "scripts" / fn).write_text(c, encoding="utf-8")
    return d


def _run(coro):
    return asyncio.run(coro)


def _tool(toolset, name):
    return next((t for t in toolset._tools if t.name == name), None)


@contextmanager
def _env(**kv):
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# === Phase 1: bounded + paginated ===

def test_resource_bounded_and_paginated():
    print("test_resource_bounded_and_paginated: ", end="")
    text = "\n".join(f"line {i}" for i in range(1, 26))  # 25 lines
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_skill(base, "demo", references={"big.md": text})
        from adk_cc.tools.skills import make_skill_toolset
        ts = make_skill_toolset(skills_dir=base)
        lr = _tool(ts, "load_skill_resource")

        pages, offset = [], 1
        for _ in range(10):  # safety bound
            r = _run(lr.run_async(
                args={"skill_name": "demo", "file_path": "references/big.md",
                      "limit": 10, "offset": offset},
                tool_context=None,
            ))
            pages.append(r["content"])
            assert r["total_lines"] == 25, r
            assert r["start_line"] == offset, r
            if not r["truncated"]:
                break
            assert r["next_offset"] == r["end_line"] + 1, r
            offset = r["next_offset"]
        # 3 pages of 10/10/5; reconstruct the original.
        assert len(pages) == 3, [p.count(chr(10)) for p in pages]
        assert "\n".join(pages) == text, "pagination did not reconstruct file"
        # first page bounded to 10 lines
        assert pages[0].count("\n") == 9, pages[0]
    print("OK")


def test_per_line_cap():
    print("test_per_line_cap: ", end="")
    long_line = "x" * 5000
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_skill(base, "demo", references={"wide.md": long_line})
        from adk_cc.tools.skills import make_skill_toolset, _MAX_LINE_LENGTH
        ts = make_skill_toolset(skills_dir=base)
        lr = _tool(ts, "load_skill_resource")
        r = _run(lr.run_async(
            args={"skill_name": "demo", "file_path": "references/wide.md"},
            tool_context=None,
        ))
        assert r["lines_truncated"] == 1, r
        assert len(r["content"]) < 5000, "long line was not capped"
        assert r["content"].startswith("x" * _MAX_LINE_LENGTH), r["content"][:50]
        assert "[truncated]" in r["content"]
    print("OK")


# === Lazy / memory ===

def test_oversized_pruned_but_served_via_fallback():
    print("test_oversized_pruned_but_served_via_fallback: ", end="")
    text = "\n".join(f"line {i}" for i in range(1, 51))  # ~ > 100 bytes
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_skill(base, "demo", references={"big.md": text})
        with _env(ADK_CC_SKILL_FILE_MAX_BYTES="50"):
            from adk_cc.tools.skills import make_skill_toolset
            ts = make_skill_toolset(skills_dir=base)
            # pruned from the in-memory dict…
            skill = ts._get_skill("demo")
            assert "big.md" not in skill.resources.references, "not pruned from RAM"
            # …but still readable on demand via the bounded disk fallback.
            lr = _tool(ts, "load_skill_resource")
            r = _run(lr.run_async(
                args={"skill_name": "demo", "file_path": "references/big.md",
                      "limit": 5},
                tool_context=None,
            ))
            assert r.get("fallback_resolved") is True, r
            assert r["total_lines"] == 50, r
            assert r["content"].startswith("line 1"), r
    print("OK")


# === Phase 1.5: search ===

def test_search_skill_resource():
    print("test_search_skill_resource: ", end="")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_skill(base, "demo", references={
            "a.md": "alpha\nNEEDLE here\nbeta",
            "b.md": "gamma\ndelta",
        })
        from adk_cc.tools.skills import make_skill_toolset
        ts = make_skill_toolset(skills_dir=base)
        search = _tool(ts, "search_skill_resource")
        assert search is not None, "search_skill_resource not registered"
        r = _run(search.run_async(
            args={"skill_name": "demo", "query": "NEEDLE"},
            tool_context=None,
        ))
        assert r["total_returned"] == 1, r
        m = r["matches"][0]
        assert m["file_path"] == "references/a.md" and m["line"] == 2, m
        assert "NEEDLE" in m["text"], m
    print("OK")


# === Phase 2 (guards, default OFF) ===

def test_guards_off_no_wrapping():
    print("test_guards_off_no_wrapping: ", end="")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_skill(base, "demo", references={"r.md": "plain content"})
        from adk_cc.tools.skills import make_skill_toolset
        ts = make_skill_toolset(skills_dir=base)
        lr = _tool(ts, "load_skill_resource")
        r = _run(lr.run_async(
            args={"skill_name": "demo", "file_path": "references/r.md"},
            tool_context=None,
        ))
        assert r["content"] == "plain content", r
        assert "skill_content" not in r["content"]
    print("OK")


def test_guards_on_wraps_untrusted():
    print("test_guards_on_wraps_untrusted: ", end="")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_skill(base, "demo", references={"r.md": "plain content"})
        with _env(ADK_CC_SKILL_GUARDS="1"):
            from adk_cc.tools.skills import make_skill_toolset
            ts = make_skill_toolset(skills_dir=base)
            lr = _tool(ts, "load_skill_resource")
            r = _run(lr.run_async(
                args={"skill_name": "demo", "file_path": "references/r.md"},
                tool_context=None,
            ))
            assert '<skill_content trust="untrusted"' in r["content"], r
            assert "plain content" in r["content"]
    print("OK")


def test_guards_on_script_refused_on_noop():
    print("test_guards_on_script_refused_on_noop: ", end="")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_skill(base, "demo", scripts={"go.py": "print('hi')"})
        with _env(ADK_CC_SKILL_GUARDS="1",
                  ADK_CC_SKILL_SCRIPTS_ACK_HOST_EXEC=None,
                  ADK_CC_SANDBOX_BACKEND=None):
            from adk_cc.tools.skills import (
                make_skill_toolset, _NoopGuardedRunSkillScriptTool,
            )
            ts = make_skill_toolset(skills_dir=base)
            rs = _tool(ts, "run_skill_script")
            assert isinstance(rs, _NoopGuardedRunSkillScriptTool), type(rs)
            # get_backend(None) falls back to the module default (noop) → refuse.
            r = _run(rs.run_async(
                args={"skill_name": "demo", "file_path": "scripts/go.py"},
                tool_context=None,
            ))
            assert r.get("error_code") == "SANDBOX_REQUIRED", r
    print("OK")


def test_guards_off_script_not_guard_wrapped():
    print("test_guards_off_script_not_guard_wrapped: ", end="")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_skill(base, "demo", scripts={"go.py": "print('hi')"})
        from adk_cc.tools.skills import (
            make_skill_toolset, _NoopGuardedRunSkillScriptTool,
        )
        ts = make_skill_toolset(skills_dir=base)  # guards off (default)
        rs = _tool(ts, "run_skill_script")
        assert not isinstance(rs, _NoopGuardedRunSkillScriptTool), type(rs)
    print("OK")


# === load_skill instructions cap ===

def test_load_skill_instructions_capped():
    print("test_load_skill_instructions_capped: ", end="")
    body = "B" * 2000
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_skill(base, "demo", body=body)
        with _env(ADK_CC_SKILL_INSTRUCTIONS_MAX_CHARS="100"):
            from adk_cc.tools.skills import make_skill_toolset
            ts = make_skill_toolset(skills_dir=base)
            ls = _tool(ts, "load_skill")
            ctx = SimpleNamespace(agent_name="t", state={})
            r = _run(ls.run_async(args={"skill_name": "demo"}, tool_context=ctx))
            assert r.get("instructions_truncated") is True, r
            assert r.get("total_instruction_chars") == 2000, r
            assert len(r["instructions"]) < 500, len(r["instructions"])
            assert "truncated" in r["instructions"]
    print("OK")


# === review fixes ===

def test_clip_lines_no_phantom_trailing_line():
    """#6: splitlines(), not split('\\n') — a newline-terminated file must not
    report an extra phantom line or be flagged truncated at EOF."""
    print("test_clip_lines_no_phantom_trailing_line: ", end="")
    from adk_cc.tools.skills import _clip_lines
    clipped, start, end, total, _chars, _lt = _clip_lines("a\nb\n", offset=1, limit=10)
    assert total == 2, total          # not 3
    assert clipped == "a\nb", clipped
    assert end == 2 and start == 1, (start, end)
    # offset past EOF → coherent empty envelope (end < start), not incoherent.
    c2, s2, e2, t2, _, _ = _clip_lines("a\nb\nc", offset=99, limit=10)
    assert c2 == "" and t2 == 3 and e2 == s2 - 1, (c2, s2, e2, t2)
    print("OK")


def test_prune_skips_binary_and_counts_bytes():
    """#4 + #7: _prune drops large TEXT by BYTE size, keeps binary (bytes)."""
    print("test_prune_skips_binary_and_counts_bytes: ", end="")
    from adk_cc.tools.skills import _prune_oversized_resources
    from google.adk.skills.models import Frontmatter, Resources, Skill
    skill = Skill(
        frontmatter=Frontmatter(name="demo", description="d"),
        instructions="body",
        resources=Resources(
            references={"big.md": "x" * 200, "cjk.md": "あ" * 100},  # 200B / 300B utf-8
            assets={"img.png": b"y" * 200},                          # binary, 200B
        ),
    )
    _prune_oversized_resources(skill, max_bytes=150)
    assert "big.md" not in skill.resources.references, "200B text not pruned"
    # 100 CJK chars = 300 utf-8 bytes > 150 → pruned (char-count bug would keep it)
    assert "cjk.md" not in skill.resources.references, "multibyte counted as chars"
    assert "img.png" in skill.resources.assets, "binary must NOT be pruned"
    print("OK")


def test_search_is_literal_not_regex():
    """#1: query is a literal substring, not a regex (no ReDoS surface)."""
    print("test_search_is_literal_not_regex: ", end="")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_skill(base, "demo", references={"a.md": "abc\nxyz"})
        from adk_cc.tools.skills import make_skill_toolset
        ts = make_skill_toolset(skills_dir=base)
        search = _tool(ts, "search_skill_resource")
        # 'a.c' matches 'abc' as REGEX but not as a literal substring.
        r = _run(search.run_async(args={"skill_name": "demo", "query": "a.c"},
                                  tool_context=None))
        assert r["total_returned"] == 0, r
        # case-insensitive literal does match.
        r2 = _run(search.run_async(args={"skill_name": "demo", "query": "ABC"},
                                   tool_context=None))
        assert r2["total_returned"] == 1, r2
    print("OK")


def test_search_skips_oversized_file():
    """#3: search skips files over the read cap instead of reading them whole."""
    print("test_search_skips_oversized_file: ", end="")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_skill(base, "demo", references={"big.md": "NEEDLE\n" + "x" * 500})
        with _env(ADK_CC_SKILL_RESOURCE_READ_MAX_BYTES="50"):
            from adk_cc.tools.skills import make_skill_toolset
            ts = make_skill_toolset(skills_dir=base)
            search = _tool(ts, "search_skill_resource")
            r = _run(search.run_async(args={"skill_name": "demo", "query": "NEEDLE"},
                                      tool_context=None))
            assert r["total_returned"] == 0, r  # big.md skipped (over cap)
    print("OK")


def test_oversized_disk_read_refused():
    """#3: the disk fallback refuses to read a file over the read cap (no OOM)."""
    print("test_oversized_disk_read_refused: ", end="")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_skill(base, "demo", references={"big.md": "x" * 500})
        with _env(ADK_CC_SKILL_FILE_MAX_BYTES="50",
                  ADK_CC_SKILL_RESOURCE_READ_MAX_BYTES="50"):
            from adk_cc.tools.skills import make_skill_toolset
            ts = make_skill_toolset(skills_dir=base)  # big.md pruned from memory
            lr = _tool(ts, "load_skill_resource")
            r = _run(lr.run_async(
                args={"skill_name": "demo", "file_path": "references/big.md"},
                tool_context=None,
            ))
            # pruned + too large to read from disk → not inlined.
            assert r.get("error_code") == "RESOURCE_NOT_FOUND", r
    print("OK")


def main():
    test_resource_bounded_and_paginated()
    test_per_line_cap()
    test_oversized_pruned_but_served_via_fallback()
    test_search_skill_resource()
    test_guards_off_no_wrapping()
    test_guards_on_wraps_untrusted()
    test_guards_on_script_refused_on_noop()
    test_guards_off_script_not_guard_wrapped()
    test_load_skill_instructions_capped()
    test_clip_lines_no_phantom_trailing_line()
    test_prune_skips_binary_and_counts_bytes()
    test_search_is_literal_not_regex()
    test_search_skips_oversized_file()
    test_oversized_disk_read_refused()
    print()
    print("All skill-resource-limits tests passed")


if __name__ == "__main__":
    main()
