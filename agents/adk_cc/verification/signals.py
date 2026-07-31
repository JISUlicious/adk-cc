"""Turn-level verification signals (W9 S1) — pure functions over turn events.

Verification is not a skills feature. The failure this design targets is
**claims made without evidence**: a dogfooding round produced three "fixed"
claims that a real reproduction contradicted, none of which involved a skill.
A prompt rule requiring an executed reproduction took that to zero in the next
round — evidence that the CLAIM, not the skill, is the right unit.

Everything here is deliberately:

* **Pure** — `(events) -> signal`. No I/O, no model call, no session mutation.
  Cheap enough to run on every turn, and testable without a harness.
* **Conservative** — a signal firing costs the user a nudge; a signal firing
  wrongly on every turn would train them to ignore it. Prefer a miss to noise.
* **Evidence-shaped** — "did anything in this turn actually check the claim?"
  rather than "does the prose look confident?".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# Tools whose use MUTATES the workspace.
_MUTATING_TOOLS = frozenset({"write_file", "edit_file", "save_as_artifact"})

# Tools that can constitute evidence (they execute something real).
_EXECUTING_TOOLS = frozenset({"run_bash", "run_skill_script"})

# Result claims — assertions that something now works / is done. Kept tight on
# purpose: "I ran the tests" is a report, "tests pass" is a claim.
_CLAIM_RE = re.compile(
    r"(?i)\b("
    r"fixed|resolved|works now|now works|working correctly|"
    r"tests? (?:now )?pass(?:es|ing)?|all tests pass|passing|"
    r"verified|confirmed working|deployed|shipped|implemented|"
    r"should (?:now )?work|is (?:now )?fixed|done and working|"
    r"(?:it'?s|that'?s|all) done|now complete|completed successfully"
    r")\b"
)

# Completion reports, matched at the START of the answer. Measurement against
# real turns showed the dominant forms are "Done.", "Done — …", "Done. Added …",
# and "Created X" / "Added X" — a whole-message match caught only the first.
# Anchoring to the start keeps "when done, run X" and "I am not done" out.
_LEAD_CLAIM_RE = re.compile(
    r"(?i)^\s*(?:\*\*)?"
    r"(done|all done|complete|completed|finished|"
    r"created|added|updated|implemented|applied|renamed)"
    r"\b"
)

# Hedges that explicitly disclaim verification — these DEFUSE a claim, because
# saying "unverified" is exactly the behaviour we want to encourage.
_HEDGE_RE = re.compile(
    r"(?i)\b(unverified|not verified|could not verify|couldn't verify|"
    r"untested|not tested|did not run|didn't run|unable to test|"
    r"please verify|you should verify|needs verification)\b"
)

# Commands that plausibly CHECK something (as opposed to just building state).
_CHECK_CMD_RE = re.compile(
    r"(?i)(^|[\s;&|(])"
    r"(pytest|unittest|tox|nox|jest|vitest|mocha|go\s+test|cargo\s+test|"
    r"npm\s+(?:run\s+)?test|yarn\s+test|pnpm\s+test|make\s+(?:test|check)|"
    r"curl|http|ruff|flake8|mypy|tsc|eslint|"
    r"python3?\s+-m\s+(?:pytest|unittest)|diff|grep\s+-c|"
    # inline interpreter probes — how a model most often checks its own work
    r"python3?\s+-c|python3?\s+-\s*<<|node\s+-e|deno\s+eval|ruby\s+-e|"
    # driving a real page IS a check, and one of the strongest available
    r"playwright|puppeteer|cypress|selenium|jsdom)"
)

# A conventional test runner — evidence that need not name the file it covers.
_TEST_RUNNER_RE = re.compile(
    r"(?i)(^|[\s;&|(])"
    r"(pytest|unittest|tox|nox|jest|vitest|mocha|go\s+test|cargo\s+test|"
    r"npm\s+(?:run\s+)?test|yarn\s+test|pnpm\s+test|make\s+(?:test|check)|"
    r"python3?\s+-m\s+(?:pytest|unittest))"
)

# Files whose behaviour only exists once a DOM runs them. A page is the one
# artifact class where "I checked it" routinely means something that never
# loaded the page: a syntax check, a grep for element ids, or a scratch script
# that re-implements the logic. Each of those can pass while every button on
# the real page is dead.
_PAGE_SUFFIXES = (".html", ".htm")
# Something that can actually load a page: a browser driver, a DOM shim, or a
# server the page is then driven through.
_PAGE_RUNTIME_RE = re.compile(
    r"(?i)\b(playwright|puppeteer|selenium|jsdom|happy-dom|linkedom|"
    r"chromium|chrome\s+--headless|webdriver|cypress|vitest\s+--browser|"
    # the bundled runner (skills/web-smoke-check) — the supported way to do this
    r"smoke_page\.mjs)\b"
)

# The documented fallback when no runtime is installed: build a minimal DOM and
# execute the page's OWN script under it. A live verifier does exactly this —
# it probes for chromium, playwright and jsdom, finds none, and hand-rolls a
# `vm` sandbox with a ClassList/document stub. That IS driving the page, and a
# signal that ignored it would nag the one behaviour the prompt asks for while
# demanding a runtime the workspace does not have.
_DOM_SHIM_RE = re.compile(
    r"(?i)(vm\.(?:createContext|runInNewContext|runInContext|Script)|"
    r"require\([\"']vm[\"']\)|"
    r"globalThis\.document\s*=|global\.document\s*=)"
)

# The bash tool's own redirect message, reused as the signal. One detection
# point (tools/bash/tool.py::_skill_script_hint) feeds both the model-facing
# correction and this turn-level signal, so the two cannot drift apart.
_BYPASSED_SKILL_RE = re.compile(r"belongs to the `([^`]+)` skill")


# Irreversible / outward-facing effects — the "hard gate" class.
_RISK_RE = re.compile(
    r"(?i)(^|[\s;&|(])"
    r"(rm\s+-[rf]|rmdir|shred|dd\s+|mkfs|"
    r"git\s+push|git\s+reset\s+--hard|"
    r"docker\s+(?:push|rm)|kubectl\s+(?:apply|delete)|terraform\s+apply|"
    r"npm\s+publish|pip\s+upload|twine\s+upload|"
    r"(?:fly|vercel|netlify|heroku)\s+deploy|gh\s+release|"
    r"alembic\s+upgrade|migrate\b)"
)


@dataclass(frozen=True)
class TurnSignals:
    """What a turn did, and whether it checked itself."""

    mutated_files: int = 0
    ran_commands: int = 0
    ran_checks: int = 0
    risk_hits: tuple[str, ...] = field(default_factory=tuple)
    claims: tuple[str, ...] = field(default_factory=tuple)
    hedged: bool = False
    mutated_paths: tuple[str, ...] = field(default_factory=tuple)
    check_commands: tuple[str, ...] = field(default_factory=tuple)
    commands: tuple[str, ...] = field(default_factory=tuple)
    # Skill scripts the turn tried to run as plain files (and failed), and the
    # ones it ran properly through the tool.
    bypassed_skill_scripts: tuple[str, ...] = field(default_factory=tuple)
    ran_skill_scripts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_evidence(self) -> bool:
        """Did anything in this turn actually check something?"""
        return self.ran_checks > 0

    @property
    def built_a_page(self) -> bool:
        """This turn produced something whose behaviour needs a DOM."""
        return any(p.lower().endswith(_PAGE_SUFFIXES) for p in self.mutated_paths)

    @property
    def page_was_driven(self) -> bool:
        """Did any check actually load the thing this turn built?

        Two ways to qualify: a command naming a page runtime (browser driver or
        DOM shim), or a conventional test runner, which covers files it does not
        name. Naming the file alone is NOT enough — `node --check app.js` and
        `grep -n id= index.html` both name it and neither runs it."""
        for cmd in self.commands or self.check_commands:
            if (_PAGE_RUNTIME_RE.search(cmd) or _TEST_RUNNER_RE.search(cmd)
                    or _DOM_SHIM_RE.search(cmd)):
                return True
        return False

    @property
    def skipped_a_shipped_script(self) -> bool:
        """A skill's script was attempted as a plain file, failed, and the turn
        then asserted a result without ever running it through the tool.

        Measured: asked for the driver in a CSV, the agent loaded data-analyst,
        read seven of its reference docs, ran `python scripts/premodel_audit.py`
        (which cannot work — skill files are not in the workspace), and wrote its
        own analysis instead. The answer was right and nothing said that none of
        the six vetted diagnostics had run.

        Deliberately narrow: it fires only when the turn ATTEMPTED the script, so
        a skill whose scripts are genuinely optional never triggers it. Hedging
        still defuses it — saying "I used my own analysis, not the probe" is the
        outcome this is asking for."""
        if not self.claims or self.hedged:
            return False
        return bool(set(self.bypassed_skill_scripts) - set(self.ran_skill_scripts))

    @property
    def unexercised_page(self) -> bool:
        """A behaviour claim about a page that was never loaded.

        This is the gap that shipped a real bug: a generated browser game
        verified itself with a syntax check, a grep proving the control ids
        existed, and a scratch probe of its start flow — all green — while the
        vote result was erased in the same tick it was written, so the game's
        payoff was invisible to every player. `has_evidence` was true the whole
        time, because evidence was counted as a scalar and never tied to the
        artifact the claim was about."""
        return (
            bool(self.claims)
            and self.built_a_page
            and not self.page_was_driven
            and not self.hedged
        )

    @property
    def changed_anything(self) -> bool:
        return self.mutated_files > 0 or self.ran_commands > 0

    @property
    def claim_without_evidence(self) -> bool:
        """The primary trigger. A hedged claim does NOT count — saying
        'unverified' is the behaviour we want, not a violation."""
        return bool(self.claims) and not self.has_evidence and not self.hedged

    @property
    def risky(self) -> bool:
        return bool(self.risk_hits)

    @property
    def unchecked_change(self) -> bool:
        """PREDICTIVE trigger, usable *before* the answer exists.

        `claim_without_evidence` is reactive: the claim only appears in the
        model's final text, which at `before_model` time has not been produced
        yet — so a nudge keyed on it can never fire in time (found live: a turn
        wrote a file and answered "Done." with the nudge silent throughout).

        What IS knowable beforehand: this turn changed something and nothing has
        checked it. That is precisely the moment to say "verify before you
        report this"."""
        return (self.mutated_files > 0 or self.risky) and not self.has_evidence

    def summary(self) -> str:
        return (
            f"files={self.mutated_files} cmds={self.ran_commands} "
            f"checks={self.ran_checks} claims={len(self.claims)} "
            f"hedged={self.hedged} risk={list(self.risk_hits)}"
        )


def _parts(event: Any) -> Iterable[Any]:
    content = getattr(event, "content", None)
    return getattr(content, "parts", None) or []


def _visible_text(event: Any) -> str:
    out = []
    for p in _parts(event):
        if getattr(p, "text", None) and not getattr(p, "thought", False):
            out.append(p.text)
    return "\n".join(out)


def _args_of(fc: Any) -> dict:
    args = getattr(fc, "args", None) or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return {}
    return args if isinstance(args, dict) else {}


def _command_of(fc: Any) -> str:
    return str(_args_of(fc).get("command") or "")


def _path_of(fc: Any) -> str:
    args = _args_of(fc)
    for key in ("path", "file_path", "filename", "name"):
        v = args.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def undriven_pages(events: Iterable[Any]) -> tuple[str, ...]:
    """Pages this SESSION has written that nothing has driven since.

    The turn-scoped `unexercised_page` cannot see across a turn boundary:
    build in turn 2, ask "does it work?" in turn 3, and `built_a_page` is
    False — the claim escapes however confident it is. Measured across three
    live runs of the same scenario: the one turn that drove the page was the
    only one whose claim was actually true; in another, the agent confirmed a
    Three.js preview that never loads (`Failed to resolve module specifier
    "three"`, zero canvas elements). Same prompt, no verification either way —
    the difference between a true claim and a shipped falsehood was luck.

    Scans the WHOLE event list rather than keeping state: derivation cannot go
    stale, survives session resume, and needs no schema. A drive event marks
    every page written before it as driven (a command cannot reliably be bound
    to one file); a later write marks that page undriven again — which also
    catches driven-in-turn-2, edited-in-turn-4, claimed-in-turn-5.
    """
    last_write: dict[str, int] = {}
    last_drive = -1
    idx = 0
    for e in events:
        parts = getattr(getattr(e, "content", None), "parts", None) or []
        for p in parts:
            fc = getattr(p, "function_call", None)
            if fc is None:
                continue
            idx += 1
            name = getattr(fc, "name", "") or ""
            args = getattr(fc, "args", None) or {}
            if name in ("write_file", "edit_file"):
                path = str(args.get("path") or "")
                if path.lower().endswith(_PAGE_SUFFIXES):
                    last_write[path] = idx
            elif name == "run_bash":
                cmd = str(args.get("command") or "")
                if (_PAGE_RUNTIME_RE.search(cmd) or _TEST_RUNNER_RE.search(cmd)
                        or _DOM_SHIM_RE.search(cmd)):
                    last_drive = idx
            elif name == "run_skill_script":
                # web-smoke-check runs through the skill tool, not run_bash —
                # the drive that run 1 actually performed came in this shape.
                blob = f"{args.get('skill_name', '')} {args.get('file_path', '')}"
                if "smoke_page" in blob or "web-smoke-check" in blob:
                    last_drive = idx
    return tuple(sorted(
        path for path, wrote in last_write.items() if wrote > last_drive))


def collect(events: Iterable[Any], *, author: Optional[str] = None) -> TurnSignals:
    """Reduce a turn's events to signals.

    `author` restricts claim-scanning to one author (normally the coordinator),
    so a sub-agent's internal chatter is not read as a user-facing claim.
    """
    mutated = commands = checks = 0
    risk: list[str] = []
    claims: list[str] = []
    paths: list[str] = []
    check_cmds: list[str] = []
    all_cmds: list[str] = []
    bypassed: list[str] = []
    ran_scripts: list[str] = []
    hedged = False

    for ev in events or []:
        for p in _parts(ev):
            fr = getattr(p, "function_response", None)
            if fr is not None and getattr(fr, "name", "") == "run_bash":
                resp = getattr(fr, "response", None)
                blob = ""
                if isinstance(resp, dict):
                    blob = str(resp.get("stderr") or "")
                for m in _BYPASSED_SKILL_RE.finditer(blob):
                    bypassed.append(m.group(1))
            fc = getattr(p, "function_call", None)
            if fc is not None:
                name = getattr(fc, "name", "") or ""
                if name == "run_skill_script":
                    args_d = _args_of(fc)
                    skill = str(args_d.get("skill_name") or "")
                    if skill:
                        ran_scripts.append(skill)
                if name in _MUTATING_TOOLS:
                    mutated += 1
                    path = _path_of(fc)
                    if path:
                        paths.append(path)
                elif name in _EXECUTING_TOOLS:
                    commands += 1
                    cmd = _command_of(fc)
                    if cmd:
                        all_cmds.append(cmd)
                    if _CHECK_CMD_RE.search(cmd):
                        checks += 1
                        check_cmds.append(cmd)
                    m = _RISK_RE.search(cmd)
                    if m:
                        risk.append(m.group(2))
        if author is not None and getattr(ev, "author", None) != author:
            continue
        text = _visible_text(ev)
        if not text:
            continue
        if _HEDGE_RE.search(text):
            hedged = True
        claims.extend(m.group(1) for m in _CLAIM_RE.finditer(text))
        lead = _LEAD_CLAIM_RE.match(text.strip())
        if lead:
            claims.append(lead.group(1).lower())

    return TurnSignals(
        mutated_files=mutated,
        ran_commands=commands,
        ran_checks=checks,
        bypassed_skill_scripts=tuple(dict.fromkeys(bypassed)),
        ran_skill_scripts=tuple(dict.fromkeys(ran_scripts)),
        risk_hits=tuple(dict.fromkeys(risk)),
        claims=tuple(dict.fromkeys(claims)),
        hedged=hedged,
        mutated_paths=tuple(dict.fromkeys(paths)),
        check_commands=tuple(check_cmds),
        commands=tuple(all_cmds),
    )


def nudge_text(
    sig: TurnSignals, *, criteria: Iterable[str] = (), predictive: bool = False
) -> Optional[str]:
    """The reminder to inject, or None when the turn warrants nothing.

    Written as a instruction to the model rather than a scolding: it should
    either produce the evidence or label the claim honestly — both are
    acceptable outcomes, and the second is far better than a confident guess.
    """
    fires = sig.claim_without_evidence or (sig.risky and not sig.has_evidence)
    fires = fires or sig.unexercised_page or sig.skipped_a_shipped_script
    if predictive:
        fires = fires or sig.unchecked_change
    if not fires:
        return None

    lines = ["=== VERIFICATION CHECK (adk-cc) ==="]
    if predictive and sig.unchecked_change and not sig.claims:
        lines.append(
            f"This turn has changed the workspace ({sig.mutated_files} file "
            f"edit(s), {sig.ran_commands} command(s)) and nothing has verified "
            f"the result yet."
        )
    if sig.claim_without_evidence:
        shown = ", ".join(f"'{c}'" for c in sig.claims[:3])
        lines.append(
            f"This turn is about to assert a result ({shown}) but nothing in it "
            f"executed a check ({sig.mutated_files} file edit(s), "
            f"{sig.ran_commands} command(s), 0 verifying runs)."
        )
    if sig.risky and not sig.has_evidence:
        lines.append(
            f"It also performed irreversible or outward-facing actions "
            f"({', '.join(sig.risk_hits[:3])}) without a verifying check."
        )
    if sig.skipped_a_shipped_script:
        skipped = ", ".join(
            sorted(set(sig.bypassed_skill_scripts) - set(sig.ran_skill_scripts))
        )
        lines.append(
            f"A script shipped by the `{skipped}` skill was attempted as a plain "
            f"file, failed, and never ran through `run_skill_script`. If the "
            f"answer rests on your own version of that work, say so — the "
            f"shipped script is the reviewed one."
        )
    if sig.unexercised_page:
        pages = ", ".join(p for p in sig.mutated_paths
                          if p.lower().endswith(_PAGE_SUFFIXES))[:120]
        lines.append(
            f"This turn built a page ({pages}) and is asserting how it behaves, "
            f"but no check loaded it. Ran {sig.ran_checks} check(s) — none used a "
            f"browser, a DOM shim, or a test runner."
        )
        lines.append(
            "For a page, these do NOT establish behaviour: a syntax check, a "
            "grep proving element ids exist, or a scratch script that "
            "re-implements the logic. Load the real page in a DOM runtime "
            "(playwright/jsdom/happy-dom) — or, if none is installed, build a "
            "minimal DOM and execute the page's OWN unmodified script under it, "
            "saying which method you used. Drive the controls a player would "
            "use and assert on what they would see AFTER the action settles, "
            "not just that a handler fired."
        )
    crit = [c for c in criteria if c]
    if crit:
        lines.append("Acceptance criteria that apply here:")
        lines.extend(f"  - {c}" for c in crit[:6])
    lines.append(
        "Before answering: run the check that would actually prove it (test, "
        "reproduction, request, or diff) — or state plainly that the change is "
        "unverified and say what would verify it. Do not present an unchecked "
        "result as done."
    )
    return "\n".join(lines)


# --- no-op sub-agent re-entry ----------------------------------------------

_HANDBACK = "_handback_to_coordinator"


def noop_subagent_reentry(events: Iterable[Any], *, agent: str) -> bool:
    """True when `agent` was transferred to again in this turn and did NOTHING.

    ADK marks a sub-agent `end_of_agent` when it finishes. A SECOND transfer to
    the same sub-agent inside one invocation therefore resolves to an
    already-ended agent and emits only the after-agent handback marker — no
    tools, no report, no verdict. Observed live: verification run 1 made 27 tool
    calls and returned VERDICT: FAIL; after the coordinator fixed the code and
    transferred again, run 2 produced ONLY the handback, and the coordinator
    told the user it had "sent it back to verification for the final verdict"
    — a verdict that could never arrive.

    Detecting it lets the coordinator be told the truth instead of promising a
    result the architecture cannot deliver in this turn.
    """
    runs: list[int] = []          # tool calls per contiguous run of `agent`
    in_run = False
    for ev in events or []:
        if getattr(ev, "author", None) != agent:
            in_run = False
            continue
        if not in_run:
            runs.append(0)
            in_run = True
        for p in _parts(ev):
            fc = getattr(p, "function_call", None)
            if fc is not None and getattr(fc, "name", "") != _HANDBACK:
                runs[-1] += 1
    return len(runs) > 1 and runs[-1] == 0


def reentry_note(agent: str) -> str:
    """What to tell the coordinator when a re-verification silently no-op'd."""
    return (
        "=== VERIFICATION RE-ENTRY (adk-cc) ===\n"
        f"You transferred to `{agent}` again in this same turn, but a sub-agent "
        "that already completed cannot run twice in one turn — it returned "
        "immediately without checking anything. There is NO second verdict.\n"
        "Do not tell the user a verdict is pending or claim it passed. Either "
        "verify the fix yourself now with a command, or state plainly that the "
        "fix is unverified and that re-running verification needs a new turn."
    )
