from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TERMINAL_OUTCOMES = frozenset(
    {
        "merged",
        "dry_run_would_merge",
        "rejected",
        "skipped_no_issue",
        "llm_error",
        "merge_failed",
        "skipped_not_mergeable",
    }
)


class ReviewStateStore:
    """
    Tracks per-PR, per-head-sha outcomes so new commits on the same PR can be re-reviewed,
    while avoiding repeated LLM work for the same snapshot.
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def _atomic_write(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent),
            prefix=".review-state-",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def load(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {}
        try:
            with self._path.open(encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("State file unreadable (%s), starting fresh", e)
            return {}

    def should_skip_pr(self, pr_number: int, head_sha: str) -> bool:
        data = self.load()
        prs: dict[str, Any] = data.get("prs", {})
        key = str(pr_number)
        rec = prs.get(key)
        if not isinstance(rec, dict):
            return False
        if rec.get("head_sha") != head_sha:
            return False
        return rec.get("outcome") in TERMINAL_OUTCOMES

    def record_outcome(
        self,
        pr_number: int,
        head_sha: str,
        outcome: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        data = self.load()
        prs: dict[str, Any] = data.setdefault("prs", {})
        entry: dict[str, Any] = {
            "head_sha": head_sha,
            "outcome": outcome,
        }
        if meta:
            entry["meta"] = meta
        prs[str(pr_number)] = entry
        self._atomic_write(data)
