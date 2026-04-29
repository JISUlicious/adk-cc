"""Pydantic input models for every adk-cc tool.

Centralized so the JSON schema generation in `AdkCcTool._get_declaration`
sees a consistent shape, and so policy plugins (Stage B) can introspect
arg names without importing each tool.
"""

from __future__ import annotations

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
