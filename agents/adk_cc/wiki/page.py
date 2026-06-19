"""A wiki page: YAML frontmatter + markdown body + `[[wikilinks]]`.

Karpathy's llm-wiki page model — each page is a markdown doc about ONE
entity/concept. The frontmatter header carries the machine-readable
metadata the librarian's merge/conflict logic keys on (provenance,
promotion policy, conflict status, validity windows); the body is prose
with `[[wikilink]]` cross-references the lint pass keeps consistent.

Parsing round-trips frontmatter through PyYAML and keeps the body
verbatim. The frontmatter keys this layer understands:

  title:       display title (else derived from the first H1, else slug)
  sources:     list of source-doc ids backing the page (cite-or-quarantine)
  no_promote:  truthy → never merged into domain (privacy; alias: sensitive)
  contested:   truthy → page records a true contradiction (queryable)
  captured_by: user_id that authored an inbox capture
  created:     ISO-8601 capture/merge timestamp
  validity:    list of {value, from, to?, source} supersession windows
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

_FENCE = "---"
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
# A closing frontmatter fence: a line that is exactly `---` (optional
# trailing whitespace), anchored at line start.
_CLOSE_FENCE_RE = re.compile(r"^---[ \t]*$", re.MULTILINE)


def slugify(name: str) -> str:
    """Normalize an entity name / title into a filesystem-safe page slug.

    Lowercase, non-alphanumeric runs collapse to single hyphens, edges
    trimmed. `"GPT-4 Turbo"` → `"gpt-4-turbo"`. Empty input → `""` (the
    caller decides what to do with an unsluggable name).
    """
    return _SLUG_RE.sub("-", name.strip().lower()).strip("-")


@dataclass
class Page:
    """One wiki page. `slug` is the identity (filename stem); frontmatter +
    body are the content. Construct via `parse()`; emit via `serialize()`."""

    slug: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @property
    def title(self) -> str:
        t = self.frontmatter.get("title")
        if isinstance(t, str) and t.strip():
            return t.strip()
        m = _H1_RE.search(self.body)
        if m:
            return m.group(1).strip()
        return self.slug

    @property
    def wikilinks(self) -> list[str]:
        """Slugs of pages this page links to, in first-seen order, deduped.
        `[[GPT-4|the model]]` → `gpt-4` (display alias after `|` ignored)."""
        seen: set[str] = set()
        out: list[str] = []
        for m in _WIKILINK_RE.finditer(self.body):
            target = slugify(m.group(1).split("|", 1)[0])
            if target and target not in seen:
                seen.add(target)
                out.append(target)
        return out

    @property
    def sources(self) -> list[str]:
        s = self.frontmatter.get("sources")
        return [str(x) for x in s] if isinstance(s, list) else []

    @property
    def contested(self) -> bool:
        return bool(self.frontmatter.get("contested"))

    @property
    def no_promote(self) -> bool:
        """Privacy gate: the librarian must never promote this page's claims
        into shared domain. `sensitive` is an accepted alias."""
        fm = self.frontmatter
        return bool(fm.get("no_promote") or fm.get("sensitive"))


def parse(text: str, slug: str) -> Page:
    """Parse markdown-with-optional-frontmatter into a `Page`.

    A leading `---` line opens frontmatter; the next line that is exactly
    `---` closes it; everything after is the body. Malformed or non-dict
    frontmatter is ignored (treated as body) rather than raising — a wiki
    page authored by an LLM should never hard-fail the reader.
    """
    if text.startswith(_FENCE + "\n") or text.startswith(_FENCE + "\r\n"):
        rest = text.split("\n", 1)[1] if "\n" in text else ""
        m = _CLOSE_FENCE_RE.search(rest)
        if m is not None:
            fm_text = rest[: m.start()]
            body = rest[m.end():].lstrip("\n")
            fm: dict[str, Any] = {}
            try:
                loaded = yaml.safe_load(fm_text)
                if isinstance(loaded, dict):
                    fm = loaded
            except yaml.YAMLError:
                fm = {}
            return Page(slug=slug, frontmatter=fm, body=body)
    return Page(slug=slug, frontmatter={}, body=text)


def serialize(page: Page) -> str:
    """Emit a `Page` back to markdown text. Frontmatter is sorted for
    stable diffs (the changelog and git history stay readable). A page
    with no frontmatter emits just the body."""
    body = page.body.rstrip() + "\n"
    if not page.frontmatter:
        return body
    fm = yaml.safe_dump(
        page.frontmatter, sort_keys=True, allow_unicode=True, default_flow_style=False
    ).strip()
    return f"{_FENCE}\n{fm}\n{_FENCE}\n\n{body}"
