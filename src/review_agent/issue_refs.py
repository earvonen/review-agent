from __future__ import annotations

import re

# GitHub closing keywords (subset; see GitHub docs)
_CLOSING = re.compile(
    r"(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s*:?\s*#(\d+)",
    re.I,
)
_HASH_NUM = re.compile(r"(?<![\w/])#(\d+)\b")


def extract_linked_issue_numbers(title: str, body: str | None) -> list[int]:
    """
    Return issue numbers referenced by closing keywords first, then other #123 mentions.
    Preserves order, deduplicates.
    """
    text = f"{title or ''}\n{body or ''}"
    seen: set[int] = set()
    ordered: list[int] = []

    for m in _CLOSING.finditer(text):
        n = int(m.group(1))
        if n not in seen:
            seen.add(n)
            ordered.append(n)

    for m in _HASH_NUM.finditer(text):
        n = int(m.group(1))
        if n not in seen:
            seen.add(n)
            ordered.append(n)

    return ordered
