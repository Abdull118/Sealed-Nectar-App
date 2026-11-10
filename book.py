"""
Printed page numbers for a PDF of the book.

A PDF's page index rarely matches the numbers printed in the book: front
matter, and picture plates that carry no page number, push the two apart, and
the gap grows each time a plate is inserted. This module works out the
printed number of every PDF page so the reader can type the numbers they see
in the book.

Method, in order:
1. If the PDF carries real page labels (set by the publisher), use them.
2. Otherwise read numbers from the first and last lines of each page
   (footer "93", header "92  The Sealed Nectar", "The Sealed Nectar  93").
   The longest chain of candidates that rises at most one number per PDF page
   is taken as the true numbering; stray numbers from body text and
   footnotes fall out because they don't fit the chain.
3. Between two anchored pages, if there are more PDF pages than printed
   numbers, the extra pages are unnumbered plates: the pages with the fewest
   words are marked as pictures.
4. Manual fixes ("PDF page 104 is printed page 92") are treated as certain.
"""

from __future__ import annotations

import re
from collections import Counter

NUM_ONLY = re.compile(r"^[\s\-–—\[\(]*(\d{1,4})[\s\-–—\]\)]*$")
NUM_START = re.compile(r"^(\d{1,4})\s+\S")
NUM_END = re.compile(r"\S\s+(\d{1,4})$")
PICTURE_WORDS = 40          # pages with fewer words than this are treated as plates
MAX_PRINTED = 5000


def lines_of(text: str) -> list[str]:
    return [l.strip() for l in (text or "").splitlines() if l.strip()]


def word_count(text: str) -> int:
    return len((text or "").split())


def page_candidates(text: str) -> list[int]:
    """Page-number candidates from the first two and last two lines of a page."""
    ls = lines_of(text)
    edge = ls if len(ls) <= 4 else ls[:2] + ls[-2:]
    found: list[int] = []
    for line in edge:
        for rx in (NUM_ONLY, NUM_START, NUM_END):
            m = rx.search(line)
            if m:
                n = int(m.group(1))
                if 1 <= n <= MAX_PRINTED and n not in found:
                    found.append(n)
    return found


def numbers_from_labels(labels: list | None, n_pages: int) -> list[int | None] | None:
    """Use the PDF's own page labels when they are meaningful and in order."""
    if not labels or len(labels) != n_pages:
        return None
    nums = [int(l) if isinstance(l, str) and l.strip().isdigit() else None for l in labels]
    if nums == list(range(1, n_pages + 1)):
        return None                                  # labels just repeat the PDF index
    valid = [x for x in nums if x is not None]
    if len(valid) < n_pages * 0.5:
        return None
    out_of_order = sum(1 for a, b in zip(valid, valid[1:]) if b <= a)
    if out_of_order > max(2, len(valid) * 0.02):
        return None
    return nums


def _best_chain(cands: list[list[int]], overrides: dict[int, int], window: int = 300) -> list[tuple[int, int]]:
    """Longest chain of (pdf_index, printed) with both rising and printed rising
    no faster than the PDF index. Overrides carry a weight that always wins."""
    pts: list[tuple[int, int, int]] = []
    for i, cs in enumerate(cands):
        if i in overrides:
            pts.append((i, overrides[i], 10_000))
        else:
            pts.extend((i, n, 1) for n in cs)
    if not pts:
        return []
    best = [0] * len(pts)
    prev = [-1] * len(pts)
    for k, (i, n, w) in enumerate(pts):
        best[k] = w
        for j in range(k - 1, -1, -1):
            ij, nj, _ = pts[j]
            if i - ij > window:
                break
            if ij < i and nj < n and n - nj <= i - ij and best[j] + w > best[k]:
                best[k] = best[j] + w
                prev[k] = j
    k = max(range(len(pts)), key=lambda x: (best[x], -x))
    chain = []
    while k != -1:
        chain.append(pts[k][:2])
        k = prev[k]
    return chain[::-1]


def build_map(cands: list[list[int]], words: list[int], labels: list | None = None,
              overrides: dict[int, int] | None = None) -> dict:
    """Return {"printed": [...], "method": ...} with one printed number (or None) per PDF page."""
    n = len(words)
    overrides = {int(k): int(v) for k, v in (overrides or {}).items()}
    if not overrides:
        from_labels = numbers_from_labels(labels, n)
        if from_labels:
            return {"printed": from_labels, "method": "labels", "anchors": n}

    chain = _best_chain(cands, overrides)
    if len(chain) < max(3, int(n * 0.05)):
        # Too little evidence: fall back to PDF numbering and say so.
        return {"printed": list(range(1, n + 1)), "method": "pdf", "anchors": len(chain)}

    printed: list[int | None] = [None] * n
    for i, num in chain:
        printed[i] = num

    i0, n0 = chain[0]                                   # before the first anchor
    for i in range(i0 - 1, -1, -1):
        v = n0 - (i0 - i)
        printed[i] = v if v >= 1 else None

    for (i1, n1), (i2, n2) in zip(chain, chain[1:]):    # between anchors
        between = list(range(i1 + 1, i2))
        numbers = n2 - n1 - 1
        if not between:
            continue
        extra = len(between) - numbers
        plates = set(sorted(between, key=lambda i: (words[i], i))[:extra]) if extra > 0 else set()
        k = n1
        for i in between:
            if i in plates:
                printed[i] = None
            else:
                k += 1
                printed[i] = k

    il, nl = chain[-1]                                  # after the last anchor
    k = nl
    for i in range(il + 1, n):
        k += 1
        printed[i] = k

    return {"printed": printed, "method": "detected", "anchors": len(chain)}


def summarize(printed: list[int | None], words: list[int]) -> dict:
    nums = [p for p in printed if p is not None]
    pictures = [i + 1 for i, p in enumerate(printed) if p is None and words[i] < PICTURE_WORDS]
    return {
        "pdf_pages": len(printed),
        "first_printed": min(nums) if nums else None,
        "last_printed": max(nums) if nums else None,
        "picture_pages": len(pictures),
        "unnumbered_pages": sum(1 for p in printed if p is None),
    }


def pdf_pages_for(printed: list[int | None], first: int, last: int) -> list[int]:
    """0-based PDF indexes for printed pages first..last, including any
    unnumbered picture pages inside the range or straight after it."""
    idx = [i for i, p in enumerate(printed) if p is not None and first <= p <= last]
    if not idx:
        return []
    lo, hi = min(idx), max(idx)
    while hi + 1 < len(printed) and printed[hi + 1] is None:
        hi += 1
    return list(range(lo, hi + 1))


def running_lines(texts: list[str]) -> set[str]:
    """Header/footer lines that repeat across many pages (digits ignored)."""
    cnt: Counter = Counter()
    for t in texts:
        ls = lines_of(t)
        edge = ls if len(ls) <= 4 else ls[:2] + ls[-2:]
        for l in set(edge):
            cnt[_shape(l)] += 1
    threshold = max(3, int(len(texts) * 0.15))
    return {k for k, v in cnt.items() if v >= threshold and len(k) <= 80}


def _shape(line: str) -> str:
    return re.sub(r"\d+", "#", line.lower()).strip()


def strip_running(text: str, common: set[str]) -> str:
    """Remove page numbers and running headers/footers from the edges of a page."""
    ls = lines_of(text)
    keep = []
    for j, l in enumerate(ls):
        edge = j < 2 or j >= len(ls) - 2
        if edge and (_shape(l) in common or NUM_ONLY.match(l)):
            continue
        keep.append(l)
    return "\n".join(keep)
