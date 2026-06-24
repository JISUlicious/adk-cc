"""Pydantic input models for every adk-cc tool.

Centralized so the JSON schema generation in `AdkCcTool._get_declaration`
sees a consistent shape, and so policy plugins (Stage B) can introspect
arg names without importing each tool.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ReadFileArgs(BaseModel):
    path: str = Field(description="Absolute or relative path to the file.")
    offset: int = Field(
        default=1,
        ge=1,
        description=(
            "Starting line number (1-indexed). Defaults to 1 (start of file). "
            "Use with `limit` to page through large files."
        ),
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=50,
        description=(
            "Maximum number of lines to return per call (1-50). Defaults "
            "to 50. The hard cap of 50 is tighter than upstream Claude "
            "Code's tool — 50 lines is enough for typical recon reads "
            "while keeping each call cheap on a local LLM's context. "
            "To read more, paginate with increasing `offset` (next call "
            "starts at `end_line + 1`). The response includes "
            "`has_more`, `total_lines`, and `total_bytes` so you can "
            "decide whether to paginate or pivot to `grep`/`glob_files`."
        ),
    )


class GlobFilesArgs(BaseModel):
    pattern: str = Field(description="Glob pattern, e.g. '**/*.py' or 'src/**/*.ts'.")
    root: str = Field(default=".", description="Directory to search from.")


class GrepArgs(BaseModel):
    pattern: str = Field(description="Extended regex (POSIX ERE; same flavor as `grep -E`).")
    path: str = Field(default=".", description="Root directory to search, anchored under the workspace.")
    glob: str = Field(
        default="**/*",
        description="File glob to filter (basename match — e.g. '**/*.py' filters by '*.py').",
    )


class WriteFileArgs(BaseModel):
    path: str = Field(description="File path to write. Parents are created as needed.")
    content: str = Field(description="Full file contents to write.")


class EditFileArgs(BaseModel):
    path: str = Field(description="File to edit.")
    old_string: str = Field(description="Exact text to find. Must be unique in the file.")
    new_string: str = Field(description="Replacement text.")


class RunBashArgs(BaseModel):
    command: str = Field(description="Shell command line.")
    timeout_seconds: int = Field(
        default=30, description="Max wall time before the process is killed."
    )


class WebFetchArgs(BaseModel):
    url: str = Field(description="The URL to fetch (http or https).")
    max_bytes: int = Field(
        default=200_000,
        description="Cap on returned body size; the rest is truncated.",
    )


class AskOption(BaseModel):
    label: str = Field(description="Short display text the user will pick (1-5 words).")
    description: str = Field(
        description="What this option means or what happens if chosen."
    )


class AskQuestion(BaseModel):
    question: str = Field(description="The question, ending in a question mark.")
    header: str = Field(
        description="Short label/chip for the question (max 12 chars).",
        max_length=12,
    )
    options: list[AskOption] = Field(
        description=(
            "Distinct choices for the question. Aim for 2-4 for crisp "
            "decisions; up to 8 is allowed for broad-category intake "
            "questions (e.g. 'which domain?'). The user can also pick "
            "'Other' (a free-form text field is provided automatically)."
        ),
        min_length=2,
        max_length=8,
    )
    multi_select: bool = Field(
        default=False, description="Allow multiple options to be selected."
    )


class AskUserQuestionArgs(BaseModel):
    questions: list[AskQuestion] = Field(
        description="1-4 questions to ask the user.", min_length=1, max_length=4
    )


class TaskCreateArgs(BaseModel):
    title: str = Field(description="Short imperative title (e.g. 'Run test suite').")
    description: str = Field(
        default="", description="Optional detail about what the task does or why."
    )
    blocked_by: list[str] = Field(
        default_factory=list,
        description="Task IDs that must complete before this one is meaningful.",
    )


class TaskGetArgs(BaseModel):
    task_id: str = Field(description="The task ID returned by `task_create`.")


class TaskListArgs(BaseModel):
    status: Optional[str] = Field(
        default=None,
        description="Optional filter: pending|in_progress|completed.",
    )


class TaskUpdateArgs(BaseModel):
    task_id: str = Field(description="The task to update.")
    status: Optional[str] = Field(
        default=None,
        description="New status: pending|in_progress|completed.",
    )
    description: Optional[str] = Field(
        default=None, description="Replace the description."
    )


class WritePlanArgs(BaseModel):
    content: str = Field(
        description=(
            "The full plan in Markdown. Should start with a `# <title>` "
            "heading. Each call creates a new plan file; previous plans "
            "remain in the workspace under `.adk-cc/plans/`."
        )
    )
    slug: Optional[str] = Field(
        default=None,
        description=(
            "Optional short kebab-case label for the plan thread "
            "(e.g. 'auth-refactor', 'bug-x-fix'). If omitted, derived "
            "from the title heading. Used in the filename for human "
            "readability."
        ),
    )


class ReadCurrentPlanArgs(BaseModel):
    pass  # no args; reads from session state


class SaveAsArtifactArgs(BaseModel):
    path: str = Field(
        description=(
            "Absolute path inside the workspace to publish as an "
            "artifact. The file is read from the sandbox and stored "
            "in ADK's artifact service, which the chat UI can render "
            "as a download chip."
        )
    )
    filename: Optional[str] = Field(
        default=None,
        description=(
            "Artifact filename (display name + storage key). If "
            "omitted, the basename of `path` is used. ADK auto-versions "
            "saves with the same filename, so repeated publishes of the "
            "same name bump a revision counter."
        ),
    )
    scope: str = Field(
        default="session",
        description=(
            "`session` (default): artifact belongs to the current "
            "session and is gone when the session is cleaned. `user`: "
            "artifact persists across this user's sessions (useful for "
            "permanent reference material the user wants to keep)."
        ),
    )


class SaveMcpResourceAsArtifactArgs(BaseModel):
    resource_name: str = Field(
        description=(
            "Name of the resource to read from THIS MCP server (as listed "
            "by the server's resource catalog). The resource's bytes are "
            "read and stored as a downloadable artifact in the chat UI."
        )
    )
    filename: Optional[str] = Field(
        default=None,
        description=(
            "Artifact filename. If omitted, derived from `resource_name` "
            "(scheme + path separators sanitized). When the resource "
            "yields multiple contents, each is saved as `{filename}.{i}`."
        ),
    )
    scope: str = Field(
        default="session",
        description=(
            "`session` (default): tied to this session, shows a download "
            "chip. `user`: persists across this user's future sessions."
        ),
    )


class LoadArtifactToSandboxArgs(BaseModel):
    filename: str = Field(
        description=(
            "Name of the artifact to copy into the sandbox (as listed by "
            "the chat UI / artifact store). Includes user-uploaded files "
            "and anything previously saved with save_as_artifact."
        )
    )
    dest_path: str = Field(
        description=(
            "Destination path inside the workspace to write the file to. "
            "Relative paths anchor at the workspace root. Parent dirs are "
            "created as needed."
        )
    )
    version: Optional[int] = Field(
        default=None,
        description=(
            "Artifact version to load (0-based). Omit for the latest "
            "version. The copy is a point-in-time snapshot — editing the "
            "sandbox file afterward does NOT change the artifact, and "
            "vice versa."
        ),
    )
    scope: str = Field(
        default="session",
        description=(
            "Where to look for the artifact: `session` (default) or "
            "`user` (cross-session). Must match the scope it was saved "
            "under."
        ),
    )
    overwrite: bool = Field(
        default=False,
        description=(
            "If the destination already exists, refuse unless this is "
            "true. Guards against silently clobbering sandbox edits."
        ),
    )


# ---- Wiki memory tools (ADK_CC_WIKI=1) ----
class WikiSearchArgs(BaseModel):
    query: str = Field(
        description=(
            "What to look up in the wiki. Searches the shared domain wiki "
            "AND your own private notes (inbox); results are tagged with "
            "which scope they came from."
        )
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Max number of pages to return (1-20). Defaults to 5.",
    )


class WikiReadArgs(BaseModel):
    slug: str = Field(
        description=(
            "The page slug to read (as returned by `wiki_search`, e.g. "
            "'gpt-4-turbo'). Use `scope` to choose where to read from."
        )
    )
    scope: str = Field(
        default="auto",
        description=(
            "`auto` (default): prefer your private note on this topic, fall "
            "back to the shared wiki. `domain`: only the shared wiki. "
            "`inbox`: only your private notes."
        ),
    )


class WikiAddArgs(BaseModel):
    text: str = Field(
        description=(
            "The note/document to capture, as markdown. This is written to "
            "YOUR private inbox only — it never edits the shared wiki "
            "directly. The librarian later merges vetted notes into the "
            "shared domain wiki."
        )
    )
    title: Optional[str] = Field(
        default=None,
        description="Optional display title. Defaults to the first line of `text`.",
    )
    topic: Optional[str] = Field(
        default=None,
        description=(
            "Optional entity/concept this note is about (drives the page "
            "slug). Set this to the same topic as an existing wiki page to "
            "have the librarian merge your note into it."
        ),
    )
    type: Optional[str] = Field(
        default=None,
        description=(
            "Page category: entity | concept | source | comparison | query. "
            "Defaults to 'concept'. Use 'entity' for a person/org/product/place, "
            "'concept' for a topic/technique, 'comparison' for a contrast."
        ),
    )
    tags: Optional[list[str]] = Field(
        default=None,
        description=(
            "Up to 3 short kebab-case organizational labels (e.g. 'iclr-2024'). "
            "Don't tag things that should be their own page — link those instead."
        ),
    )
