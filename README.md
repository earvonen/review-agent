# Review (review-agent)

**Review** is a Python service that **polls GitHub** for **open pull requests** whose **base branch** matches **`REVIEW_GIT_BRANCH`** on the repo derived from **`REVIEW_GIT_CLONE_URL`**.

For each PR (when GitHub reports it as **mergeable**):

1. Resolves a **linked GitHub issue** from the PR title/body (closing keywords like `Fixes #123`, then `#NNN` mentions).
2. **Clones** the repo at the base branch, **fetches** `pull/<n>/head`, and checks out the PR head for **workspace** tools.
3. Runs **Llama Stack** with **`REVIEW_TOOL_GROUP_IDS`** (e.g. GitHub MCP) plus workspace tools; the model returns JSON  
   `{"addresses_spec": true|false, "reason": "..."}`.
4. If **`addresses_spec`** is true, calls the **GitHub REST API** to **merge** the PR (`REVIEW_MERGE_METHOD`: `merge` | `squash` | `rebase`).

**`GITHUB_TOKEN`** (PAT with `repo`) is **required** for listing PRs, reading issues/files, and merging. This is separate from Llama Stack / MCP.

State is stored in **`REVIEW_STATE_FILE`** keyed by PR number and **head SHA** so new commits trigger a fresh review.

## Config (ConfigMap / env)

| Variable | Purpose |
|----------|---------|
| `REVIEW_GIT_CLONE_URL` | Clone URL; owner/repo parsed for the API |
| `REVIEW_GIT_BRANCH` | Base branch to watch (must match PR `base`) |
| `REVIEW_POLL_INTERVAL_SECONDS` | Poll interval |
| `REVIEW_STATE_FILE` | JSON state path |
| `REVIEW_WORKSPACE_ROOT` | Ephemeral clone directories |
| `GITHUB_TOKEN` | PAT for GitHub REST |
| `LLAMA_STACK_BASE_URL` | Llama Stack URL |
| `LLAMA_STACK_MODEL_ID` | Model id (optional if stack has a default) |
| `REVIEW_TOOL_GROUP_IDS` | Comma-separated tool groups (MCP) |
| `REVIEW_MCP_REGISTRATIONS_JSON` | Optional MCP SSE registrations at startup |
| `REVIEW_MERGE_METHOD` | `merge`, `squash`, or `rebase` |
| `REVIEW_DRY_RUN_NO_MERGE` | If `true`, no merge; records outcome only |

See `deploy/openshift.yaml` for an example **Deployment** + **ConfigMap**. Create a **`review-agent-github`** secret with key **`token`** for `GITHUB_TOKEN`.

## Run locally

```bash
pip install -e .

export REVIEW_GIT_CLONE_URL=https://github.com/org/repo.git
export REVIEW_GIT_BRANCH=main
export GITHUB_TOKEN=ghp_...
export LLAMA_STACK_BASE_URL=http://localhost:8321
export LLAMA_STACK_MODEL_ID=your-model-id
export REVIEW_TOOL_GROUP_IDS=mcp-github

python -m review_agent
```

## Build / OpenShift

- **Container:** `Containerfile` → `python -m review-agent`
- **ImageStream / Tekton:** `deploy/imagestream.yaml`, `deploy/tekton/*.yaml`

PRs that are **draft**, **not yet mergeable** (`mergeable: null` or `false`), or **missing an issue reference** are skipped (with logging); missing-issue cases are recorded so the same PR head is not retried until the SHA changes.

After **`REVIEW_DRY_RUN_NO_MERGE`**, clear **`REVIEW_STATE_FILE`** or push a new commit on the PR to re-run the model and merge.
