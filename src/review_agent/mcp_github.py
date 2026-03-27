from __future__ import annotations

import json
import logging
from typing import Any

from llama_stack_client import LlamaStackClient

from review_agent.config import Settings
from review_agent.json_util import parse_json_loose
from review_agent.llama_tools import tool_invocation_content_as_text

logger = logging.getLogger(__name__)


def resolve_tool_group_for_tool_name(
    client: LlamaStackClient,
    tool_group_ids: list[str],
    tool_name: str,
) -> str | None:
    for gid in tool_group_ids:
        try:
            defs = client.tool_runtime.list_tools(tool_group_id=gid)
        except Exception as e:
            logger.debug("list_tools failed for group %r: %s", gid, e)
            continue
        for d in defs:
            n = getattr(d, "name", None) or (d.get("name") if isinstance(d, dict) else None)
            if n == tool_name:
                return gid
    return None


def invoke_mcp_tool(
    client: LlamaStackClient,
    tool_name: str,
    kwargs: dict[str, Any],
    tool_group_id: str,
) -> str:
    inv = client.tool_runtime.invoke_tool(
        tool_name=tool_name,
        kwargs=kwargs,
        extra_body={"tool_group_id": tool_group_id},
    )
    if inv.error_message:
        raise RuntimeError(f"MCP tool {tool_name!r} failed: {inv.error_message}")
    return tool_invocation_content_as_text(inv.content)


def _response_looks_like_tool_failure(text: str) -> bool:
    t = text.strip().lower()
    if not t:
        return False
    return "missing required parameter" in t or "required parameter:" in t


def _tool_group_for(
    client: LlamaStackClient,
    settings: Settings,
    tool_name: str,
) -> str:
    if settings.mcp_invoke_tool_group_id:
        return settings.mcp_invoke_tool_group_id.strip()
    gid = resolve_tool_group_for_tool_name(client, settings.tool_group_id_list, tool_name)
    if not gid:
        raise RuntimeError(
            f"MCP tool {tool_name!r} not found in REVIEW_TOOL_GROUP_IDS="
            f"{settings.tool_group_ids!r}. Set REVIEW_MCP_INVOKE_TOOL_GROUP_ID if needed."
        )
    return gid


def _call_tool(
    client: LlamaStackClient,
    settings: Settings,
    tool_name: str,
    kwargs: dict[str, Any],
) -> str:
    gid = _tool_group_for(client, settings, tool_name)
    text = invoke_mcp_tool(client, tool_name, kwargs, tool_group_id=gid)
    if _response_looks_like_tool_failure(text):
        raise RuntimeError(text.strip())
    return text


def _parse_json_list(text: str) -> list[Any]:
    data = parse_json_loose(text)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("items", "pull_requests", "pullRequests", "data"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


class GitHubMcpClient:
    """GitHub operations exclusively via Llama Stack → GitHub MCP (invoke_tool)."""

    def __init__(self, client: LlamaStackClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    def list_open_pulls(self, owner: str, repo: str, base_branch: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        per_page = min(100, max(1, self._settings.mcp_list_pull_requests_per_page))
        tool = self._settings.mcp_tool_list_pull_requests.strip()

        while True:
            kwargs: dict[str, Any] = {
                "owner": owner,
                "repo": repo,
                "state": "open",
                "base": base_branch,
                "page": page,
                "perPage": per_page,
            }
            if self._settings.mcp_list_pull_requests_extra_json:
                extra = json.loads(self._settings.mcp_list_pull_requests_extra_json)
                if isinstance(extra, dict):
                    kwargs.update(extra)

            text = _call_tool(self._client, self._settings, tool, kwargs)
            batch = _parse_json_list(text)
            if not batch:
                parsed = parse_json_loose(text)
                if isinstance(parsed, dict) and isinstance(parsed.get("pull_requests"), list):
                    batch = parsed["pull_requests"]
                elif isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
                    batch = parsed["items"]

            for item in batch:
                if isinstance(item, dict):
                    out.append(item)

            if len(batch) < per_page:
                break
            page += 1
            if page > 500:
                logger.warning("list_pull_requests pagination stopped at page 500")
                break

        return out

    def get_pull(self, owner: str, repo: str, pull_number: int) -> dict[str, Any]:
        tool = self._settings.mcp_tool_pull_request_read.strip()
        text = _call_tool(
            self._client,
            self._settings,
            tool,
            {
                "method": "get",
                "owner": owner,
                "repo": repo,
                "pullNumber": pull_number,
            },
        )
        parsed = parse_json_loose(text)
        if isinstance(parsed, dict):
            return parsed
        raise RuntimeError(f"Expected JSON object from {tool} get, got: {text[:500]!r}")

    def get_issue(self, owner: str, repo: str, issue_number: int) -> dict[str, Any]:
        tool = self._settings.mcp_tool_issue_read.strip()
        text = _call_tool(
            self._client,
            self._settings,
            tool,
            {
                "method": "get",
                "owner": owner,
                "repo": repo,
                "issue_number": issue_number,
            },
        )
        parsed = parse_json_loose(text)
        if not isinstance(parsed, dict):
            raise RuntimeError(f"Expected JSON object from {tool} get, got: {text[:500]!r}")
        if parsed.get("pull_request") or parsed.get("is_pull_request") or parsed.get("isPullRequest"):
            raise RuntimeError(
                f"#{issue_number} refers to a pull request, not an issue; link a GitHub issue from the PR"
            )
        return parsed

    def list_pull_files(self, owner: str, repo: str, pull_number: int) -> list[dict[str, Any]]:
        tool = self._settings.mcp_tool_pull_request_read.strip()
        out: list[dict[str, Any]] = []
        page = 1
        per_page = 100

        while True:
            text = _call_tool(
                self._client,
                self._settings,
                tool,
                {
                    "method": "get_files",
                    "owner": owner,
                    "repo": repo,
                    "pullNumber": pull_number,
                    "page": page,
                    "perPage": per_page,
                },
            )
            batch = _parse_json_list(text)
            for item in batch:
                if isinstance(item, dict):
                    out.append(item)
            if len(batch) < per_page:
                break
            page += 1
            if page > 200:
                break

        return out

    def merge_pull(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        merge_method: str,
    ) -> dict[str, Any]:
        tool = self._settings.mcp_tool_merge_pull_request.strip()
        kwargs: dict[str, Any] = {
            "owner": owner,
            "repo": repo,
            "pullNumber": pull_number,
            "merge_method": merge_method,
        }
        if self._settings.mcp_merge_pull_request_extra_json:
            extra = json.loads(self._settings.mcp_merge_pull_request_extra_json)
            if isinstance(extra, dict):
                kwargs.update(extra)

        text = _call_tool(self._client, self._settings, tool, kwargs)
        parsed = parse_json_loose(text)
        if isinstance(parsed, dict):
            return parsed
        if text.strip():
            return {"raw": text.strip()}
        return {}
