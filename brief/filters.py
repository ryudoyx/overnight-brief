"""关键词粗筛：把明显无关的砍掉，别让它们花 LLM 的钱。"""

from __future__ import annotations

from .sources import Item


def _hay(item: Item) -> str:
    return f"{item.title} {item.summary}".lower()


def passes(item: Item, must: list[str], block: list[str]) -> bool:
    hay = _hay(item)
    if any(b.lower() in hay for b in block):
        return False
    return any(m.lower() in hay for m in must)


def prefilter(items: list[Item], keywords: dict) -> list[Item]:
    must = keywords.get("must") or []
    block = keywords.get("block") or []
    return [i for i in items if passes(i, must, block)]
