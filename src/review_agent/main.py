from __future__ import annotations

import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from llama_stack_client import LlamaStackClient

from review_agent.config import Settings
from review_agent.git_repo import (
    GitSource,
    clone_repository,
    fetch_and_checkout_pr_head,
    git_repo_summary,
    git_source_from_clone_url,
)
from review_agent.mcp_github import GitHubMcpClient
from review_agent.issue_refs import extract_linked_issue_numbers
from review_agent.json_util import parse_json_loose
from review_agent.llama_tools import run_tool_assisted_fix
from review_agent.state_store import ReviewStateStore

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are **Review**, an agent that decides whether a **GitHub pull request** adequately
implements the linked **GitHub issue**.

You may use **workspace** tools to read files in the local clone (already checked out at the PR head) and
**GitHub MCP tools** if you need extra context from GitHub.

When you are ready to decide, respond with a **single JSON object** and **no tool calls** in that final turn.
The JSON must have this exact shape:
```json
{"addresses_spec": true or false, "reason": "short explanation"}
```

Rules:
- `addresses_spec` is **true** only if the changes substantially satisfy the issue's stated requirements.
- If the issue is vague, use your best judgment; prefer **false** if the link between the diff and the ticket
  is weak or missing.
- `reason` should cite concrete evidence (files, behaviors) from the issue text and the change."""


def _register_mcp_endpoints(client: LlamaStackClient, settings: Settings) -> None:
    for reg in settings.parsed_mcp_registrations():
        try:
            client.toolgroups.register(
                toolgroup_id=reg.toolgroup_id,
                provider_id=reg.provider_id,
                mcp_endpoint={"uri": reg.mcp_uri},
            )
            logger.info("Registered MCP toolgroup %s", reg.toolgroup_id)
        except Exception as e:
            logger.warning(
                "Could not register MCP toolgroup %s (may already exist): %s",
                reg.toolgroup_id,
                e,
            )


def _resolve_model_id(client: LlamaStackClient, configured: str | None) -> str:
    if configured:
        return configured
    models = client.models.list()
    if not models:
        raise RuntimeError("LLAMA_STACK_MODEL_ID is unset and Llama Stack returned no models")
    mid = models[0].id
    logger.info("Using first available Llama Stack model: %s", mid)
    return mid


def _build_files_prompt_section(
    files: list[dict[str, Any]],
    max_files: int,
    max_patch_per_file: int,
    max_total_patch: int,
) -> str:
    lines: list[str] = []
    total_patch = 0
    for i, f in enumerate(files[:max_files]):
        name = str(f.get("filename") or f.get("path") or "")
        status = str(f.get("status") or "")
        patch = f.get("patch")
        patch_s = patch if isinstance(patch, str) else ""
        if max_patch_per_file > 0 and len(patch_s) > max_patch_per_file:
            patch_s = patch_s[:max_patch_per_file] + "\n... (truncated)"
        if max_total_patch > 0 and total_patch + len(patch_s) > max_total_patch:
            remaining = max_total_patch - total_patch
            if remaining > 0:
                patch_s = patch_s[:remaining] + "\n... (truncated by total budget)"
            else:
                patch_s = "(omitted; total patch budget exhausted)"
        total_patch += len(patch_s)
        lines.append(f"### File {i + 1}: `{name}` ({status})\n")
        if patch_s.strip():
            lines.append("```diff\n" + patch_s + "\n```\n")
        else:
            lines.append("(no inline patch; binary or large file)\n")
    if len(files) > max_files:
        lines.append(f"\n... and {len(files) - max_files} more file(s) not shown.\n")
    return "\n".join(lines)


def _build_user_prompt(
    owner: str,
    repo: str,
    base_branch: str,
    pr_number: int,
    pr_title: str,
    pr_body: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    files_section: str,
    git_summary: str,
    repo_path: Path,
) -> str:
    return f"""## Repository
- **owner:** `{owner}`
- **repo:** `{repo}`
- **base branch (PR target):** `{base_branch}`

## Pull request #{pr_number}
**Title:** {pr_title}

**Body:**
```
{pr_body or "(empty)"}
```

## Linked issue #{issue_number}
**Title:** {issue_title}

**Body:**
```
{issue_body or "(empty)"}
```

## PR diff summary (from GitHub via MCP; may be truncated)

{files_section}

## Local workspace (PR head checked out)
Path: `{repo_path}`

Recent commits:
```
{git_summary}
```
"""


def _parse_review_verdict(model_text: str) -> tuple[bool | None, str]:
    parsed = parse_json_loose(model_text)
    if not isinstance(parsed, dict):
        return None, "could not parse JSON verdict"
    raw = parsed.get("addresses_spec")
    if raw is None:
        return None, "missing addresses_spec in JSON"
    if isinstance(raw, bool):
        ok = raw
    else:
        ok = str(raw).lower() in ("true", "1", "yes")
    reason = str(parsed.get("reason") or "").strip() or "(no reason given)"
    return ok, reason


def _mergeability_allows_review(full: dict[str, Any], pr_number: int) -> bool:
    """
    GitHub often returns mergeable=null while computing, and GitHub MCP minimal PR payloads
    may omit ``mergeable`` entirely. Only skip when the API clearly says the PR cannot be
    merged (conflicts / blocked). Otherwise proceed; ``merge_pull_request`` enforces reality.
    """
    mergeable = full.get("mergeable")
    if mergeable is None:
        mergeable = full.get("is_mergeable")
    state_raw = full.get("mergeable_state") or full.get("mergeableState") or ""
    state = str(state_raw).lower()

    if mergeable is False:
        logger.info(
            "PR #%s not mergeable (mergeable=false, state=%s); waiting for a later poll",
            pr_number,
            state_raw or "(empty)",
        )
        return False

    if state in ("dirty", "blocked"):
        logger.info(
            "PR #%s cannot merge yet (mergeable_state=%s); waiting for a later poll",
            pr_number,
            state,
        )
        return False

    if mergeable is not True:
        logger.info(
            "PR #%s proceeding without mergeable=true (mergeable=%r mergeable_state=%r); "
            "MCP may omit mergeable — merge step will validate",
            pr_number,
            mergeable,
            state_raw or "(empty)",
        )
    return True


def _pull_number_from_summary(pr_summary: dict[str, Any]) -> int | None:
    for key in ("number", "pullNumber", "pull_number"):
        v = pr_summary.get(key)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


def process_pull(
    settings: Settings,
    state: ReviewStateStore,
    client: LlamaStackClient,
    model_id: str,
    gh: GitHubMcpClient,
    src: GitSource,
    owner: str,
    repo: str,
    pr_summary: dict[str, Any],
) -> None:
    pr_number = _pull_number_from_summary(pr_summary)
    if pr_number is None:
        logger.warning("Skipping list entry with no PR number: %s", pr_summary)
        return

    full = gh.get_pull(owner, repo, pr_number)
    head = full.get("head") if isinstance(full.get("head"), dict) else {}
    head_sha = str(head.get("sha") or "")

    if not head_sha:
        logger.warning("PR #%s has no head.sha from MCP; skipping", pr_number)
        return

    if state.should_skip_pr(pr_number, head_sha):
        logger.debug("Skipping PR #%s at sha %s (terminal outcome already recorded)", pr_number, head_sha[:7])
        return

    is_draft = full.get("draft")
    if is_draft is None:
        is_draft = full.get("isDraft")
    if is_draft:
        logger.info("PR #%s is draft; skipping", pr_number)
        return

    if not _mergeability_allows_review(full, pr_number):
        return

    title = str(full.get("title") or "")
    body = str(full.get("body") or "")
    issue_nums = extract_linked_issue_numbers(title, body)
    if not issue_nums:
        logger.warning("PR #%s has no linked issue reference; marking skipped", pr_number)
        state.record_outcome(
            pr_number,
            head_sha,
            "skipped_no_issue",
            {"detail": "No #issue or closing keyword in PR title/body"},
        )
        return

    issue_n = issue_nums[0]
    try:
        issue = gh.get_issue(owner, repo, issue_n)
    except Exception as e:
        logger.exception("Failed to load issue #%s for PR #%s: %s", issue_n, pr_number, e)
        state.record_outcome(
            pr_number,
            head_sha,
            "llm_error",
            {"detail": f"issue_fetch_failed: {e}"},
        )
        return

    issue_title = str(issue.get("title") or "")
    issue_body = str(issue.get("body") or "")

    try:
        files = gh.list_pull_files(owner, repo, pr_number)
    except Exception as e:
        logger.exception("Failed to list files for PR #%s: %s", pr_number, e)
        state.record_outcome(
            pr_number,
            head_sha,
            "llm_error",
            {"detail": f"files_fetch_failed: {e}"},
        )
        return

    files_section = _build_files_prompt_section(
        files,
        settings.max_files_in_prompt,
        settings.max_patch_chars_per_file,
        settings.max_total_patch_chars,
    )

    ws = Path(settings.workspace_root) / f"review-pr-{pr_number}-{head_sha[:12]}"
    if ws.exists():
        shutil.rmtree(ws)

    clone_ok = True
    try:
        clone_repository(src, ws, settings.github_token, settings.git_clone_depth)
        fetch_and_checkout_pr_head(ws, pr_number, settings.git_clone_depth)
    except Exception as e:
        clone_ok = False
        logger.warning(
            "Clone/fetch failed for PR #%s (%s). Continuing with empty workspace; use MCP for code context.",
            pr_number,
            e,
        )
        ws.mkdir(parents=True, exist_ok=True)

    summary = git_repo_summary(ws) if clone_ok else "(no clone)"
    user_prompt = _build_user_prompt(
        owner,
        repo,
        settings.git_branch,
        pr_number,
        title,
        body,
        issue_n,
        issue_title,
        issue_body,
        files_section,
        summary,
        ws,
    )

    logger.info("Invoking Llama Stack (model=%s) for PR #%s", model_id, pr_number)
    try:
        model_reply = run_tool_assisted_fix(
            client=client,
            model_id=model_id,
            tool_group_ids=settings.tool_group_id_list,
            repo_root=ws,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            max_iterations=settings.max_llm_iterations,
        )
    except Exception:
        logger.exception("Llama Stack run failed for PR #%s", pr_number)
        state.record_outcome(pr_number, head_sha, "llm_error", {"detail": "llm_exception"})
        return
    finally:
        if ws.exists():
            try:
                shutil.rmtree(ws)
            except OSError as e:
                logger.warning("Could not remove workspace %s: %s", ws, e)

    logger.info("Model reply (excerpt): %s", model_reply[:2000])
    addresses, reason = _parse_review_verdict(model_reply)
    if addresses is None:
        logger.warning("No clear verdict for PR #%s; marking llm_error", pr_number)
        state.record_outcome(
            pr_number,
            head_sha,
            "llm_error",
            {"model_excerpt": model_reply[:8000]},
        )
        return

    if not addresses:
        logger.info("PR #%s rejected by model: %s", pr_number, reason)
        state.record_outcome(
            pr_number,
            head_sha,
            "rejected",
            {"reason": reason},
        )
        return

    if settings.dry_run_no_merge:
        logger.info("REVIEW_DRY_RUN_NO_MERGE: would merge PR #%s (%s)", pr_number, reason)
        state.record_outcome(
            pr_number,
            head_sha,
            "dry_run_would_merge",
            {"reason": reason},
        )
        return

    try:
        merge_result = gh.merge_pull(
            owner,
            repo,
            pr_number,
            settings.merge_method,
        )
        logger.info("Merged PR #%s: %s", pr_number, merge_result)
        state.record_outcome(
            pr_number,
            head_sha,
            "merged",
            {"merge_result": merge_result, "reason": reason},
        )
    except Exception:
        logger.exception("Merge failed for PR #%s", pr_number)
        state.record_outcome(
            pr_number,
            head_sha,
            "merge_failed",
            {"reason": reason},
        )


def run_forever(settings: Settings, state: ReviewStateStore) -> None:
    src = git_source_from_clone_url(settings.git_clone_url, settings.git_branch)
    if not src:
        raise RuntimeError(
            "Could not derive GitHub owner/repo from REVIEW_GIT_CLONE_URL; check the URL format."
        )
    owner, repo = src.owner, src.repo

    client = LlamaStackClient(
        base_url=settings.llama_stack_base_url,
        api_key=settings.llama_stack_api_key,
        timeout=600.0,
    )
    _register_mcp_endpoints(client, settings)
    model_id = _resolve_model_id(client, settings.llama_stack_model_id)
    gh = GitHubMcpClient(client, settings)

    while True:
        try:
            pulls = gh.list_open_pulls(owner, repo, settings.git_branch)
            if pulls:
                logger.info(
                    "Poll: %s open PR(s) targeting %s/%s:%s",
                    len(pulls),
                    owner,
                    repo,
                    settings.git_branch,
                )
            else:
                logger.info(
                    "Poll: no open PRs for %s/%s:%s; sleeping %ss",
                    owner,
                    repo,
                    settings.git_branch,
                    settings.poll_interval_seconds,
                )

            for pr_summary in pulls:
                process_pull(
                    settings,
                    state,
                    client,
                    model_id,
                    gh,
                    src,
                    owner,
                    repo,
                    pr_summary,
                )
        except Exception:
            logger.exception("Poll iteration failed")

        time.sleep(settings.poll_interval_seconds)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    settings = Settings()
    state = ReviewStateStore(settings.state_file_path)
    run_forever(settings, state)


if __name__ == "__main__":
    main()
