from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class McpRegistration:
    """Optional MCP registration applied at startup (Llama Stack toolgroups.register)."""

    toolgroup_id: str
    provider_id: str
    mcp_uri: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    git_clone_url: str = Field(
        ...,
        description="HTTPS or SSH clone URL for the GitHub repository",
        validation_alias="REVIEW_GIT_CLONE_URL",
    )
    git_branch: str = Field(
        ...,
        description="Base branch open PRs must target (e.g. main)",
        validation_alias="REVIEW_GIT_BRANCH",
    )

    poll_interval_seconds: int = Field(60, validation_alias="REVIEW_POLL_INTERVAL_SECONDS")
    state_file_path: str = Field(
        "/tmp/review-agent-state.json",
        validation_alias="REVIEW_STATE_FILE",
    )

    github_token: str | None = Field(
        None,
        description="Optional: HTTPS git clone for local workspace only (not used for GitHub MCP).",
        validation_alias="GITHUB_TOKEN",
    )

    llama_stack_base_url: str = Field(..., validation_alias="LLAMA_STACK_BASE_URL")
    llama_stack_api_key: str | None = Field(None, validation_alias="LLAMA_STACK_API_KEY")
    llama_stack_model_id: str | None = Field(None, validation_alias="LLAMA_STACK_MODEL_ID")

    tool_group_ids: str = Field(
        ...,
        description="Comma-separated Llama Stack tool group IDs (e.g. GitHub MCP)",
        validation_alias="REVIEW_TOOL_GROUP_IDS",
    )

    mcp_invoke_tool_group_id: str | None = Field(
        None,
        description="If set, all GitHub MCP invoke_tool calls use this tool group id",
        validation_alias="REVIEW_MCP_INVOKE_TOOL_GROUP_ID",
    )

    mcp_tool_list_pull_requests: str = Field(
        "list_pull_requests",
        validation_alias="REVIEW_MCP_TOOL_LIST_PULL_REQUESTS",
    )
    mcp_list_pull_requests_per_page: int = Field(100, validation_alias="REVIEW_MCP_LIST_PULL_REQUESTS_PER_PAGE")
    mcp_list_pull_requests_extra_json: str | None = Field(
        None,
        description="Optional JSON object merged into list_pull_requests kwargs",
        validation_alias="REVIEW_MCP_LIST_PULL_REQUESTS_EXTRA_JSON",
    )

    mcp_tool_pull_request_read: str = Field(
        "pull_request_read",
        validation_alias="REVIEW_MCP_TOOL_PULL_REQUEST_READ",
    )
    mcp_tool_issue_read: str = Field(
        "issue_read",
        validation_alias="REVIEW_MCP_TOOL_ISSUE_READ",
    )
    mcp_tool_merge_pull_request: str = Field(
        "merge_pull_request",
        validation_alias="REVIEW_MCP_TOOL_MERGE_PULL_REQUEST",
    )
    mcp_merge_pull_request_extra_json: str | None = Field(
        None,
        description="Optional JSON merged into merge_pull_request kwargs",
        validation_alias="REVIEW_MCP_MERGE_PULL_REQUEST_EXTRA_JSON",
    )

    mcp_registrations_json: str | None = Field(
        None,
        validation_alias="REVIEW_MCP_REGISTRATIONS_JSON",
        description='Optional JSON list: [{"toolgroup_id":"mcp::x","provider_id":"model-context-protocol","mcp_uri":"http://host/sse"}]',
    )

    git_clone_depth: int = Field(50, validation_alias="REVIEW_GIT_CLONE_DEPTH")
    workspace_root: str = Field("/tmp/review-workspaces", validation_alias="REVIEW_WORKSPACE_ROOT")

    max_llm_iterations: int = Field(40, validation_alias="REVIEW_MAX_LLM_ITERATIONS")
    max_files_in_prompt: int = Field(80, validation_alias="REVIEW_MAX_FILES_IN_PROMPT")
    max_patch_chars_per_file: int = Field(12_000, validation_alias="REVIEW_MAX_PATCH_CHARS_PER_FILE")
    max_total_patch_chars: int = Field(100_000, validation_alias="REVIEW_MAX_TOTAL_PATCH_CHARS")

    dry_run_no_merge: bool = Field(False, validation_alias="REVIEW_DRY_RUN_NO_MERGE")
    merge_method: str = Field("merge", validation_alias="REVIEW_MERGE_METHOD")

    @property
    def tool_group_id_list(self) -> list[str]:
        return [x.strip() for x in self.tool_group_ids.split(",") if x.strip()]

    @field_validator("poll_interval_seconds", "git_clone_depth", "max_llm_iterations")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("must be >= 1")
        return v

    @field_validator("mcp_list_pull_requests_per_page")
    @classmethod
    def _per_page(cls, v: int) -> int:
        if v < 1 or v > 100:
            raise ValueError("mcp_list_pull_requests_per_page must be 1..100")
        return v

    @field_validator(
        "max_files_in_prompt",
        "max_patch_chars_per_file",
        "max_total_patch_chars",
    )
    @classmethod
    def _non_negative_size(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be >= 0")
        return v

    @field_validator("merge_method")
    @classmethod
    def _merge_method(cls, v: str) -> str:
        allowed = {"merge", "squash", "rebase"}
        s = v.strip().lower()
        if s not in allowed:
            raise ValueError(f"merge_method must be one of {allowed}")
        return s

    def parsed_mcp_registrations(self) -> list[McpRegistration]:
        if not self.mcp_registrations_json:
            return []
        raw: list[Any] = json.loads(self.mcp_registrations_json)
        out: list[McpRegistration] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("REVIEW_MCP_REGISTRATIONS_JSON must be a JSON list of objects")
            out.append(
                McpRegistration(
                    toolgroup_id=str(item["toolgroup_id"]),
                    provider_id=str(item.get("provider_id") or "model-context-protocol"),
                    mcp_uri=str(item["mcp_uri"]),
                )
            )
        return out
