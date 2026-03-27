from __future__ import annotations

from typing import Any

import httpx


class GitHubClient:
    """Minimal GitHub REST client for PR polling, issue/PR metadata, and merge."""

    def __init__(self, token: str, base_url: str = "https://api.github.com") -> None:
        self._base = base_url.rstrip("/")
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = f"{self._base}{path}"
        with httpx.Client(timeout=120.0) as client:
            r = client.request(
                method,
                url,
                headers=self._headers,
                params=params,
                json=json_body,
            )
        return r

    def list_open_pulls(self, owner: str, repo: str, base_branch: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        per_page = 100
        while True:
            r = self._request(
                "GET",
                f"/repos/{owner}/{repo}/pulls",
                params={
                    "state": "open",
                    "base": base_branch,
                    "sort": "created",
                    "direction": "asc",
                    "per_page": per_page,
                    "page": page,
                },
            )
            r.raise_for_status()
            batch = r.json()
            if not isinstance(batch, list):
                break
            out.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return out

    def get_pull(self, owner: str, repo: str, pull_number: int) -> dict[str, Any]:
        r = self._request("GET", f"/repos/{owner}/{repo}/pulls/{pull_number}")
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise RuntimeError("Unexpected GitHub API response for get_pull")
        return data

    def get_issue(self, owner: str, repo: str, issue_number: int) -> dict[str, Any]:
        r = self._request("GET", f"/repos/{owner}/{repo}/issues/{issue_number}")
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise RuntimeError("Unexpected GitHub API response for get_issue")
        if data.get("pull_request"):
            raise RuntimeError(
                f"#{issue_number} is a pull request, not an issue; link a GitHub issue from the PR"
            )
        return data

    def list_pull_files(self, owner: str, repo: str, pull_number: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        per_page = 100
        while True:
            r = self._request(
                "GET",
                f"/repos/{owner}/{repo}/pulls/{pull_number}/files",
                params={"per_page": per_page, "page": page},
            )
            r.raise_for_status()
            batch = r.json()
            if not isinstance(batch, list):
                break
            out.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return out

    def merge_pull(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        merge_method: str,
        commit_title: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"merge_method": merge_method}
        if commit_title:
            body["commit_title"] = commit_title
        r = self._request(
            "PUT",
            f"/repos/{owner}/{repo}/pulls/{pull_number}/merge",
            json_body=body,
        )
        data = r.json() if r.content else {}
        if r.status_code == 200 and isinstance(data, dict):
            return data
        if r.status_code == 405:
            raise RuntimeError(
                f"Merge not allowed for PR #{pull_number} (not mergeable or blocked): {data!r}"
            )
        r.raise_for_status()
        return data if isinstance(data, dict) else {}
