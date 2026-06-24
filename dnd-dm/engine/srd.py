"""SRD rules retrieval — keyword search over the rules corpus.

Loads rules/srd_5.1.md, splits it into chunks by `##` heading, and ranks them
against a query by term overlap (heading matches weighted heavily). Dependency-
free BM25-lite; good enough for a few dozen rule chunks, and the model gets the
actual rule text rather than guessing. The corpus is just a markdown file — point
it at the full SRD split the same way to scale coverage. (An embedding index is
the later upgrade, mirroring the episodic-memory plan.)
"""

from __future__ import annotations

import pathlib
import re
from collections import Counter

CORPUS = pathlib.Path(__file__).resolve().parent.parent / "rules" / "srd_5.1.md"
_WORD = re.compile(r"[a-z0-9]+")
_chunks: list[tuple[str, str]] | None = None


def _toks(s: str) -> list[str]:
    return _WORD.findall(s.lower())


def _load() -> list[tuple[str, str]]:
    global _chunks
    if _chunks is None:
        chunks, head, body = [], None, []
        for line in CORPUS.read_text().splitlines():
            if line.startswith("## "):
                if head:
                    chunks.append((head, "\n".join(body).strip()))
                head, body = line[3:].strip(), []
            elif head is not None:
                body.append(line)
        if head:
            chunks.append((head, "\n".join(body).strip()))
        _chunks = chunks
    return _chunks


def search(query: str, k: int = 3) -> list[dict]:
    """Return the top-k rule chunks for a query, ranked by term overlap."""
    qt = set(_toks(query))
    if not qt:
        return []
    scored = []
    for head, body in _load():
        head_toks, body_counts = set(_toks(head)), Counter(_toks(body))
        score = sum(body_counts[t] for t in qt) + 3 * sum(1 for t in qt if t in head_toks)
        if score:
            scored.append((score, head, body))
    scored.sort(key=lambda x: -x[0])
    return [{"heading": h, "body": b, "score": s} for s, h, b in scored[:k]]


if __name__ == "__main__":
    for q in ["what does prone do", "death saving throws", "how does cover work"]:
        top = search(q, k=1)
        print(f"{q!r} -> {top[0]['heading'] if top else '(none)'}")
