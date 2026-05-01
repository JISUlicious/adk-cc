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


class GlobFilesArgs(BaseModel):
    pattern: str = Field(description="Glob pattern, e.g. '**/*.py' or 'src/**/*.ts'.")
    root: str = Field(default=".", description="Directory to search from.")


class GrepArgs(BaseModel):
    pattern: str = Field(description="Python regex.")
    path: str = Field(default=".", description="Root directory to search.")
    glob: str = Field(default="**/*", description="File glob to filter, e.g. '**/*.py'.")


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
        description="2-4 distinct choices. The user can also pick 'Other' (provided automatically).",
        min_length=2,
        max_length=4,
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
    command: Optional[str] = Field(
        default=None,
        description=(
            "Shell command to run in the background. If unset, the task is "
            "a passive checkpoint (status updated manually)."
        ),
    )
    blocked_by: list[str] = Field(
        default_factory=list,
        description="Task IDs that must complete before this one is meaningful.",
    )


class TaskGetArgs(BaseModel):
    task_id: str = Field(description="The task ID returned by task.create.")


class TaskListArgs(BaseModel):
    status: Optional[str] = Field(
        default=None,
        description="Optional filter: pending|in_progress|completed|failed|stopped.",
    )


class TaskUpdateArgs(BaseModel):
    task_id: str = Field(description="The task to update.")
    status: Optional[str] = Field(
        default=None,
        description="New status: pending|in_progress|completed|failed|stopped.",
    )
    description: Optional[str] = Field(
        default=None, description="Replace the description."
    )


class TaskStopArgs(BaseModel):
    task_id: str = Field(description="The task to cancel.")


class WritePlanArgs(BaseModel):
    content: str = Field(
        description=(
            "The full plan in Markdown. Should start with a `# <title>` "
            "heading. Replaces any previous plan for this session."
        )
    )


class ReadCurrentPlanArgs(BaseModel):
    pass  # no args; reads from session state
