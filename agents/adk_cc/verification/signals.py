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
    r"python3?\s+-c|python3?\s+-\s*<<|node\s+-e|deno\s+eval|ruby\s+-e)"
)

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

    @property
    def has_evidence(self) -> bool:
        """Did anything in this turn actually check something?"""
        return self.ran_checks > 0

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


def _command_of(fc: Any) -> str:
    args = getattr(fc, "args", None) or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return ""
    if not isinstance(args, dict):
        return ""
    return str(args.get("command") or "")


def collect(events: Iterable[Any], *, author: Optional[str] = None) -> TurnSignals:
    """Reduce a turn's events to signals.

    `author` restricts claim-scanning to one author (normally the coordinator),
    so a sub-agent's internal chatter is not read as a user-facing claim.
    """
    mutated = commands = checks = 0
    risk: list[str] = []
    claims: list[str] = []
    hedged = False

    for ev in events or []:
        for p in _parts(ev):
            fc = getattr(p, "function_call", None)
            if fc is not None:
                name = getattr(fc, "name", "") or ""
                if name in _MUTATING_TOOLS:
                    mutated += 1
                elif name in _EXECUTING_TOOLS:
                    commands += 1
                    cmd = _command_of(fc)
                    if _CHECK_CMD_RE.search(cmd):
                        checks += 1
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
        risk_hits=tuple(dict.fromkeys(risk)),
        claims=tuple(dict.fromkeys(claims)),
        hedged=hedged,
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
