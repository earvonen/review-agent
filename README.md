# Review (review-agent)

**Review** is a Python service that **polls GitHub** for **open pull requests** whose **base branch** matches **`REVIEW_GIT_BRANCH`** on the repo derived from **`REVIEW_GIT_CLONE_URL`**.

All **GitHub reads and writes** (list PRs, PR/issue details, file list, **merge**) go through **Llama Stack** `tool_runtime.invoke_tool` into your **GitHub MCP** server—the same pattern as Scribe used for `issue_write`. This app does **not** call `api.github.com` directly.

For each PR (when GitHub reports it as **mergeable** via MCP):

1. Resolves a **linked GitHub issue** from the PR title/body (closing keywords like `Fixes #123`, then `#NNN` mentions).
2. Optionally **clones** the repo at the base branch, **fetches** `pull/<n>/head`, and checks out the PR head for **workspace** tools (uses **`GITHUB_TOKEN`** only if set, for private HTTPS clone—same as Scribe).
3. Runs **Llama Stack** with **`REVIEW_TOOL_GROUP_IDS`** (e.g. GitHub MCP) plus workspace tools; the model returns JSON  
   `{"addresses_spec": true|false, "reason": "..."}`.
4. If **`addresses_spec`** is true, merges via MCP **`merge_pull_request`** (`REVIEW_MERGE_METHOD`: `merge` | `squash` | `rebase`).

Default MCP tool names match the **official GitHub MCP server** (`list_pull_requests`, `pull_request_read`, `issue_read`, `merge_pull_request`). Override with **`REVIEW_MCP_TOOL_*`** if your server differs.

State is stored in **`REVIEW_STATE_FILE`** keyed by PR number and **head SHA** so new commits trigger a fresh review.

## Config (ConfigMap / env)

| Variable | Purpose |
|----------|---------|
| `REVIEW_GIT_CLONE_URL` | Clone URL; owner/repo used for MCP calls |
| `REVIEW_GIT_BRANCH` | Base branch to watch (must match PR `base`) |
| `REVIEW_POLL_INTERVAL_SECONDS` | Poll interval |
| `REVIEW_STATE_FILE` | JSON state path |
| `REVIEW_WORKSPACE_ROOT` | Ephemeral clone directories |
| `GITHUB_TOKEN` | Optional: private HTTPS **git clone** only |
| `LLAMA_STACK_BASE_URL` | Llama Stack URL |
| `LLAMA_STACK_MODEL_ID` | Model id (optional if stack has a default) |
| `REVIEW_TOOL_GROUP_IDS` | Comma-separated tool groups (must include GitHub MCP) |
| `REVIEW_MCP_INVOKE_TOOL_GROUP_ID` | Optional: force tool group id for all MCP invokes |
| `REVIEW_MCP_TOOL_LIST_PULL_REQUESTS` | Default `list_pull_requests` |
| `REVIEW_MCP_TOOL_PULL_REQUEST_READ` | Default `pull_request_read` |
| `REVIEW_MCP_TOOL_ISSUE_READ` | Default `issue_read` |
| `REVIEW_MCP_TOOL_MERGE_PULL_REQUEST` | Default `merge_pull_request` |
| `REVIEW_MCP_REGISTRATIONS_JSON` | Optional MCP SSE registrations at startup |
| `REVIEW_MERGE_METHOD` | `merge`, `squash`, or `rebase` |
| `REVIEW_DRY_RUN_NO_MERGE` | If `true`, no merge; records outcome only |

See `deploy/openshift.yaml` for an example **Deployment** + **ConfigMap**.

## Run locally

```bash
pip install -e .

export REVIEW_GIT_CLONE_URL=https://github.com/org/repo.git
export REVIEW_GIT_BRANCH=main
export LLAMA_STACK_BASE_URL=http://localhost:8321
export LLAMA_STACK_MODEL_ID=your-model-id
export REVIEW_TOOL_GROUP_IDS=mcp-github

python -m review_agent
```

## Build / OpenShift

- **Container:** `Containerfile` → `python -m review_agent`
- **ImageStream / Tekton:** `deploy/imagestream.yaml`, `deploy/tekton/*.yaml`

PRs that are **draft**, clearly **not mergeable** (`mergeable: false` or `mergeable_state` `dirty` / `blocked`), or **missing an issue reference** are skipped (with logging). If `mergeable` is **missing or null** (common with GitHub MCP minimal responses or while GitHub is still computing), the agent **still runs the review**; the **`merge_pull_request`** MCP call decides whether the merge actually succeeds.

After **`REVIEW_DRY_RUN_NO_MERGE`**, clear **`REVIEW_STATE_FILE`** or push a new commit on the PR to re-run the model and merge.
