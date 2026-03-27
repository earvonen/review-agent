from __future__ import annotations

import re

# GitHub closing keywords (subset; see GitHub docs)
_CLOSING = re.compile(
    r"(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s*:?\s*#(\d+)",
    re.I,
)
_HASH_NUM = re.compile(r"(?<![\w/])#(\d+)\b")


def _append_closing_last_wins(
    matches: list[re.Match[str]],
    seen: set[int],
    ordered: list[int],
) -> None:
    """
    Append issue numbers from closing-keyword matches so the **last** match in that segment
    is tried first (PR templates often end with the real ``Fixes #NN``; earlier lines can be stale).
    Remaining matches follow in document order.
    """
    if not matches:
        return
    last_n = int(matches[-1].group(1))
    if last_n not in seen:
        seen.add(last_n)
        ordered.append(last_n)
    for m in matches[:-1]:
        n = int(m.group(1))
        if n not in seen:
            seen.add(n)
            ordered.append(n)


def _append_bare_hashes(text: str, seen: set[int], ordered: list[int]) -> None:
    for m in _HASH_NUM.finditer(text):
        n = int(m.group(1))
        if n not in seen:
            seen.add(n)
            ordered.append(n)


def extract_linked_issue_numbers(title: str, body: str | None) -> list[int]:
    """
    Return issue numbers to consult for the PR spec.

    Order:
    1. **Title** — closing keywords (last match in title wins among those), then other ``#123`` in title.
    2. **Body** — closing keywords (last match in body wins among those), then other ``#123`` in body.

    So ``Fixes #44`` at the bottom of the body is preferred over a stale ``closes #34`` above it,
    and a ``#44`` in the title is still ahead of body-only noise.
    """
    title_t = (title or "").strip()
    body_t = (body or "").strip()
    seen: set[int] = set()
    ordered: list[int] = []

    _append_closing_last_wins(list(_CLOSING.finditer(title_t)), seen, ordered)
    _append_bare_hashes(title_t, seen, ordered)

    _append_closing_last_wins(list(_CLOSING.finditer(body_t)), seen, ordered)
    _append_bare_hashes(body_t, seen, ordered)

    return ordered
