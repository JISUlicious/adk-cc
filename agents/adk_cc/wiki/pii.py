"""Personal-information heuristics shared by wiki_add and the librarian.

Conservative, high-precision signals so genuine domain docs aren't blocked.
Two enforcement points on purpose: wiki_add refuses personal captures at the
inbox door, and the librarian refuses to PUBLISH any that slipped through —
publish is where content crosses users in a shared deployment (#126 P1).
"""
from __future__ import annotations

import re

# A topic slug naming a person/profile:
PERSONAL_TOPIC_RE = re.compile(
    r"^(user(-|$)|about-me|my-|profile$|bio$|user-profile)", re.IGNORECASE)
# First-person identity / preference / memory-directive phrasing:
PERSONAL_TEXT_RE = re.compile(
    r"\b(my name is|remember (about )?me|i am (a|an|the)\b.*\b(engineer|developer|"
    r"manager|designer|lead|architect|scientist)|i (prefer|like|use|work)\b|"
    r"the user'?s? (name|role|identity|preference|profile))", re.IGNORECASE)


def looks_personal(slug: str, text: str) -> bool:
    return bool(PERSONAL_TOPIC_RE.search(slug or "")
                or PERSONAL_TEXT_RE.search(text or ""))
