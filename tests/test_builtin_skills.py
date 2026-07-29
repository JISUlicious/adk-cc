"""W2: built-in skills (the `<install>/adk_cc/skills/` base layer).

Properties pinned here:
  - built-ins load with no project skills configured;
  - a project skill of the SAME NAME overrides the built-in, and does not
    hide the other built-ins (first-found wins per name, not per directory);
  - `ADK_CC_BUILTIN_SKILLS=0` removes the whole layer;
  - no duplicate-name crash when a name exists in two sources;
  - the `list_skills` catalog stays within a token budget (the real scarce
    resource — skills are progressively disclosed, so the cost is per-call
    catalog size, not per-skill tool schemas);
  - **packaging**: a built wheel actually CONTAINS the SKILL.md files.
    `[tool.setuptools.packages.find]` ships only *.py, so without explicit
    package-data every installed copy would silently have zero skills.

Run: `uv run python tests/test_builtin_skills.py`
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
# Project walk-up would otherwise pick up this repo's own .claude/skills.
os.environ.setdefault("ADK_CC_DISABLE_PROJECT_SKILLS", "1")

from adk_cc.tools.skills import (  # noqa: E402
    _resolve_skills_dirs,
    discover_skills_with_sources,
)

_REPO = Path(__file__).resolve().parent.parent
_BUILTIN_DIR = _REPO / "agents" / "adk_cc" / "skills"


def _names():
    return {s.name for s, _ in discover_skills_with_sources()}


def test_builtins_load_by_default():
    names = _names()
    assert "data-analyst" in names, names
    dirs = [str(d) for d in _resolve_skills_dirs()]
    assert any(d.endswith("adk_cc/skills") for d in dirs), dirs
    print("OK builtins_load_by_default")


def test_builtin_companions_are_reachable():
    """The methodology docs and probe scripts must be LOADABLE — asserted
    through the tool, not through ADK's `references` index.

    Upstream pd-skills moved its companions from `references/` to the skill
    root (and added `scripts/`), which empties that index while the files are
    perfectly reachable via the lenient loader's disk fallback. Pinning the
    index shape made a vendor update look like a regression; pinning the
    capability is what the agent actually depends on."""
    import asyncio

    from adk_cc.tools.skills import make_skill_toolset

    skills = {s.name: s for s, _ in discover_skills_with_sources()}
    s = skills["data-analyst"]
    assert s.description and len(s.description) > 40, s.description

    files = sorted(p.name for p in (_BUILTIN_DIR / "data-analyst").rglob("*.md"))
    assert len(files) >= 10, files
    assert any("root-cause" in f for f in files), files
    scripts = sorted(p.name for p in (_BUILTIN_DIR / "data-analyst" / "scripts").glob("*.py"))
    assert len(scripts) >= 4, scripts

    toolset = make_skill_toolset()
    by_name = {t.name: t for t in toolset._tools}

    class _Ctx:
        agent_name = "coordinator"
        state: dict = {}

    async def _load(path):
        return await by_name["load_skill_resource"].run_async(
            args={"skill_name": "data-analyst", "file_path": path}, tool_context=_Ctx())

    for path in ("root-cause-analysis.md", "scripts/premodel_audit.py"):
        res = asyncio.run(_load(path))
        assert isinstance(res, dict) and (res.get("content") or res.get("lines")), \
            f"{path} not loadable: {res}"
    print(f"OK builtin_companions_are_reachable ({len(files)} docs, {len(scripts)} scripts)")


def test_kill_switch_removes_the_layer():
    os.environ["ADK_CC_BUILTIN_SKILLS"] = "0"
    try:
        dirs = [str(d) for d in _resolve_skills_dirs()]
        assert not any(d.endswith("adk_cc/skills") for d in dirs), dirs
        assert "data-analyst" not in _names()
    finally:
        os.environ.pop("ADK_CC_BUILTIN_SKILLS", None)
    assert "data-analyst" in _names(), "layer must come back"
    print("OK kill_switch_removes_the_layer")


def test_project_skill_overrides_by_name_only():
    """A same-named project skill wins; the OTHER built-ins survive. This is
    the property that makes built-ins safe to ship — users override one
    without losing the base layer."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "data-analyst"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: data-analyst\ndescription: LOCAL OVERRIDE of the "
            "built-in data analyst, used to prove precedence.\n---\n\nlocal\n"
        )
        os.environ["ADK_CC_SKILLS_DIR"] = tmp
        try:
            skills = {s.name: s for s, _ in discover_skills_with_sources()}
            assert "LOCAL OVERRIDE" in (skills["data-analyst"].description or ""), \
                skills["data-analyst"].description
            # no duplicate-name crash, exactly one entry for the name
            names = [s.name for s, _ in discover_skills_with_sources()]
            assert names.count("data-analyst") == 1, names
        finally:
            os.environ.pop("ADK_CC_SKILLS_DIR", None)
    # …and the built-in is back once the override goes away
    skills = {s.name: s for s, _ in discover_skills_with_sources()}
    assert "LOCAL OVERRIDE" not in (skills["data-analyst"].description or "")
    print("OK project_skill_overrides_by_name_only")


def test_catalog_token_budget():
    """Guard against skill sprawl: the catalog is paid on every `list_skills`
    call, and selection precision degrades before tokens do."""
    pairs = discover_skills_with_sources()
    payload = "\n".join(
        f"<skill><name>{s.name}</name><description>{s.description}</description></skill>"
        for s, _ in pairs
    )
    approx_tokens = len(payload) // 4
    assert approx_tokens < 2000, f"catalog ~{approx_tokens} tokens for {len(pairs)} skills"
    print(f"OK catalog_token_budget (~{approx_tokens} tokens, {len(pairs)} skills)")


# Skills whose subject matter is bound to jurisdiction / entity / current rules.
# NOT just the legal pair: employment, tax/accounting and corporate-governance
# rules are every bit as country-, entity- and year-specific, and a built-in
# that states one country's version is confidently wrong for most readers.
_JURISDICTION_SENSITIVE = {
    "contract-review", "nda-triage",        # legal
    "hiring-kit", "performance-review",     # employment law
    "board-update",                         # entity form, governance, filings
}
# `governing law` is a contract-document concept; it only makes sense for the
# skills that read one. The rest must still establish jurisdiction and entity.
_READS_A_CONTRACT = {"contract-review", "nda-triage"}

# Skills that are not rule-bound but whose ANSWER changes by country/market:
# rates, benchmarks, procurement regimes, market structure. These must not
# recall such facts from memory either, but they carry no advice boundary.
_CONTEXT_SENSITIVE = {
    "financial-model", "dcf-model", "budget-forecast", "pricing-analysis",
    "proposal-rfp", "strategic-planning", "competitive-analysis",
}


def test_jurisdiction_sensitive_skills_ask_before_asserting():
    """A built-in ships to everyone. A skill that bakes in one country's rules
    is confidently wrong for most readers with no signal that it is wrong, so
    these must establish the user's context and refuse to state local rules
    from memory (see skills/AUTHORING.md)."""
    for name in _JURISDICTION_SENSITIVE:
        body = (_BUILTIN_DIR / name / "SKILL.md").read_text().lower()
        assert "jurisdiction" in body, f"{name}: must establish jurisdiction"
        if name in _READS_A_CONTRACT:
            assert "governing law" in body, f"{name}: must locate governing law"
        assert "entity" in body, f"{name}: must establish entity type"
        # explicitly asks rather than assuming, AND must surface the gap even
        # when it proceeds — a silent assumption is the failure mode
        assert "ask" in body, f"{name}: must ask when context is unknown"
        assert "not established" in body, \
            f"{name}: must state unestablished context rather than assume it"
        assert "context" in body, f"{name}: must open with a context line"
        # advice boundary is stated
        assert "not legal advice" in body, f"{name}: must state the advice boundary"
        # never recall specifics from memory; verify live instead
        assert "memory" in body and "web_fetch" in body, \
            f"{name}: must require live verification over recalled rules"
    print(f"OK jurisdiction_sensitive_skills_ask_before_asserting "
          f"({len(_JURISDICTION_SENSITIVE)} skills)")


def test_context_sensitive_skills_do_not_recall_local_facts():
    """The weaker cousin of the rule above. A finance or market skill is not
    giving legal advice, but a tax rate, a risk-free rate, a 'typical' margin or
    a procurement regime recalled from memory is still a fact about one country
    at one time — stated to a reader who may be in neither. These must ask for
    the context and fetch anything specific."""
    for name in _CONTEXT_SENSITIVE:
        body = (_BUILTIN_DIR / name / "SKILL.md").read_text().lower()
        # accept any phrasing of "do not state this from recollection"
        assert any(w in body for w in ("memory", "recall", "recollection")), \
            f"{name}: must warn against stating local specifics from recollection"
        assert "web_fetch" in body, \
            f"{name}: must name the fetch-and-cite mechanism, not just forbid guessing"
        assert "ask" in body, f"{name}: must ask for the user's context"
        # the gap must be VISIBLE in the output — either as unestablished
        # context, a labelled assumption, or a per-claim [unknown]/[inferred]
        # marker (competitive-analysis uses the last form).
        assert any(w in body for w in
                   ("not established", "assumption", "[unknown]", "[inferred]")), \
            f"{name}: must surface the gap rather than quietly assume it"
        assert any(w in body for w in ("country", "jurisdiction", "market", "region")), \
            f"{name}: must name the geography its answer depends on"
    print(f"OK context_sensitive_skills_do_not_recall_local_facts "
          f"({len(_CONTEXT_SENSITIVE)} skills)")


def test_no_baked_in_jurisdiction_facts():
    """Cheap guard against the specific failure mode: hardcoded statutory
    numbers / durations presented as fact. Catches the obvious regressions."""
    import re

    bad = re.compile(
        r"(?i)\b(?:must|shall|is required to)\s+(?:be\s+)?(?:filed|reported|"
        r"paid)\s+within\s+\d+|\b\d+\s*(?:year|month|day)s?\s+(?:is|are)\s+"
        r"(?:the\s+)?standard\b|\bstatute of limitations is\b"
    )
    for skill_dir in sorted(p for p in _BUILTIN_DIR.iterdir() if p.is_dir()):
        for md in skill_dir.rglob("*.md"):
            hit = bad.search(md.read_text())
            assert not hit, f"{md.relative_to(_BUILTIN_DIR)}: baked-in fact {hit.group(0)!r}"
    print("OK no_baked_in_jurisdiction_facts")


def test_wheel_contains_skill_files():
    """REAL build. `packages.find` ships only *.py — without package-data the
    wheel would contain zero SKILL.md and every installed copy would have an
    empty built-in layer while passing all the tests above."""
    import shutil

    builder = (["uv", "build", "--wheel", "--out-dir"] if shutil.which("uv")
               else [sys.executable, "-m", "build", "--wheel", "--outdir"])
    with tempfile.TemporaryDirectory() as out:
        proc = subprocess.run(
            builder + [out, str(_REPO)], capture_output=True, text=True, timeout=900,
        )
        assert proc.returncode == 0, (
            "wheel build failed — packaging cannot be left unverified:\n"
            + (proc.stderr or proc.stdout)[-400:]
        )
        wheels = list(Path(out).glob("*.whl"))
        assert wheels, "no wheel produced"
        with zipfile.ZipFile(wheels[0]) as z:
            names = z.namelist()
        skill_md = [n for n in names if n.endswith("skills/data-analyst/SKILL.md")]
        docs = [n for n in names
                if "skills/data-analyst/" in n and n.endswith(".md")
                and not n.endswith("SKILL.md")]
        # Scripts are EXECUTED (`run_skill_script`), so a wheel that ships the
        # prose but not the probes gives an agent instructions it cannot follow.
        scripts = [n for n in names
                   if "skills/data-analyst/scripts/" in n and n.endswith(".py")]
        assert skill_md, "wheel has no built-in SKILL.md — package-data missing"
        assert len(docs) >= 10, f"wheel has {len(docs)} companion docs"
        assert len(scripts) >= 4, f"wheel has {len(scripts)} probe scripts"
        print(f"OK wheel_contains_skill_files (SKILL.md + {len(docs)} docs "
          f"+ {len(scripts)} scripts)")


def main():
    test_builtins_load_by_default()
    test_builtin_companions_are_reachable()
    test_kill_switch_removes_the_layer()
    test_project_skill_overrides_by_name_only()
    test_catalog_token_budget()
    test_jurisdiction_sensitive_skills_ask_before_asserting()
    test_context_sensitive_skills_do_not_recall_local_facts()
    test_no_baked_in_jurisdiction_facts()
    test_wheel_contains_skill_files()
    print("\nall built-in skill tests passed")


if __name__ == "__main__":
    main()
