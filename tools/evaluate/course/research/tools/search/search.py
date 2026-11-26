"""search: the local search tool of the grounded-research task, over its frozen source packet.

    search <words>...            the passages that match best, five unless --limit says otherwise
    search --limit 10 <words>...
    search --sources             the sources of the packet: their ID, their file and their title

It reads `index.json`, beside this file: every paragraph and every table of the packet, with its
source ID, its page (for a PDF) and its table number. It uses the standard library alone, opens
no connection and reads no clock: the same words give the same passages, in the same order.

A passage scores the sum, over the words asked for, of how often it holds the word times how rare
the word is in the packet (`log(1 + passages / passages holding it)`). Ties keep the packet's order.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

INDEX = Path(__file__).resolve().parent / "index.json"
_WORD = re.compile(r"\d+(?:\.\d+)?|[^\W\d_]+")


def words(text: str) -> list[str]:
    """The words of a text, in lower case: numbers whole (0.040), and letters (fibre)."""
    return [word.lower() for word in _WORD.findall(text)]


def load(index: Path = INDEX) -> dict:
    return json.loads(index.read_text(encoding="utf-8"))


def search(query: str, packet: dict, limit: int = 5) -> list[dict]:
    """The passages of the packet that match a query best, the best first."""
    passages = packet["passages"]
    held = [words(passage["text"]) for passage in passages]
    asked = sorted(set(words(query)))  # in one order, so that every sum is the same sum
    holding = {word: sum(word in passage for passage in held) for word in asked}
    scored = []
    for order, (passage, found) in enumerate(zip(passages, held, strict=True)):
        score = sum(found.count(word) * math.log(1 + len(passages) / holding[word]) for word in asked if holding[word])
        if score > 0:
            scored.append((-score, order, passage))
    return [passage for _score, _order, passage in sorted(scored, key=lambda one: one[:2])[:limit]]


def where(passage: dict) -> str:
    """Where a passage is, as a citation names it: its source, and its page and table if it has them."""
    parts = [passage["source"]]
    if passage["page"] is not None:
        parts.append(f"page {passage['page']}")
    if passage["table"] is not None:
        parts.append(f"Table {passage['table']}")
    return ", ".join(parts)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="search", description="Search the frozen source packet.")
    parser.add_argument("--limit", type=int, default=5, help="the most passages shown (5)")
    parser.add_argument("--sources", action="store_true", help="list the sources of the packet")
    parser.add_argument("words", nargs="*", help="what to search for")
    arguments = parser.parse_args(argv)
    packet = load()
    if arguments.sources:
        for source_id, source in packet["sources"].items():
            print(f"{source_id}\t{source['file']}\t{source['title']}")
        return 0
    if not arguments.words:
        parser.error("say what to search for, or ask for --sources")
    found = search(" ".join(arguments.words), packet, max(1, arguments.limit))
    if not found:
        print("No passage of the packet matches.")
    for passage in found:
        print(f"[{where(passage)}] {passage['text']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
