"""
The book, stored once on the server so every device can use it.

The PDF itself is never uploaded or stored: the browser reads it with pdf.js
and sends the text of each page (25 pages per request, well under Vercel's
4.5 MB request limit). When the upload finishes, the server works out the
printed page numbers (book.py) and keeps the cleaned text.

  book:meta                    name, version, printed numbers, detection data
  book:text:<ver>:<chunk>      cleaned page texts, 25 per chunk
  book:raw:<ver>:<chunk>       raw page texts during an upload (deleted after)
  book:upload:<ver>            upload in progress
"""

from __future__ import annotations

import math
import secrets

import book
import srs

META = "book:meta"
CHUNK = 25
MAX_PAGES = 3000


class BookError(ValueError):
    pass


def begin(store, name: str, pages: int, labels: list | None) -> str:
    if not 1 <= pages <= MAX_PAGES:
        raise BookError(f"The PDF must have between 1 and {MAX_PAGES} pages.")
    if labels is not None and len(labels) != pages:
        labels = None
    ver = secrets.token_hex(4)
    store.set_json(f"book:upload:{ver}", {"name": name[:200], "pages": pages, "labels": labels,
                                          "started": srs.utcnow().isoformat()})
    return ver


def put_pages(store, ver: str, start: int, texts: list[str]) -> None:
    up = store.get_json(f"book:upload:{ver}")
    if not up:
        raise BookError("This upload has expired. Start again.")
    if start % CHUNK or not 1 <= len(texts) <= CHUNK or start + len(texts) > up["pages"]:
        raise BookError("Pages must be sent in blocks of 25, starting at a multiple of 25.")
    store.set_json(f"book:raw:{ver}:{start // CHUNK}", [t[:20000] for t in texts])


def finish(store, ver: str) -> dict:
    up = store.get_json(f"book:upload:{ver}")
    if not up:
        raise BookError("This upload has expired. Start again.")
    n = up["pages"]
    chunks = math.ceil(n / CHUNK)
    raw = store.mget_json([f"book:raw:{ver}:{k}" for k in range(chunks)])
    missing = [k * CHUNK + 1 for k, c in enumerate(raw) if c is None]
    if missing:
        raise BookError(f"Some pages never arrived (block starting at page {missing[0]}). Upload again.")
    texts = [t for c in raw for t in c]
    if len(texts) != n:
        raise BookError("The page count doesn't match. Upload again.")

    cands = [book.page_candidates(t) for t in texts]
    words = [book.word_count(t) for t in texts]
    mapping = book.build_map(cands, words, up.get("labels"))
    common = book.running_lines(texts)
    clean = [book.strip_running(t, common) for t in texts]
    store.mset_json({f"book:text:{ver}:{k}": clean[k * CHUNK:(k + 1) * CHUNK] for k in range(chunks)})

    old = store.get_json(META)
    meta = {
        "version": ver, "name": up["name"], "pdf_pages": n, "chunk": CHUNK,
        "words": words, "candidates": cands, "labels": up.get("labels"), "overrides": {},
        "printed": mapping["printed"], "method": mapping["method"],
        "uploaded_at": srs.utcnow().isoformat(),
    }
    store.set_json(META, meta)
    store.delete(f"book:upload:{ver}", *[f"book:raw:{ver}:{k}" for k in range(chunks)])
    if old and old.get("version") != ver:
        store.delete(*[f"book:text:{old['version']}:{k}" for k in range(math.ceil(old["pdf_pages"] / CHUNK))])
    return summary(meta)


def summary(meta: dict | None) -> dict | None:
    if not meta:
        return None
    s = book.summarize(meta["printed"], meta["words"])
    s.update(name=meta["name"], method=meta["method"], uploaded_at=meta["uploaded_at"],
             fixes=len(meta.get("overrides") or {}))
    return s


def get_summary(store) -> dict | None:
    return summary(store.get_json(META))


def pages(store, first: int, last: int) -> dict:
    """Text of printed pages first..last, with any picture pages in between."""
    meta = store.get_json(META)
    if not meta:
        raise BookError("Add the book first (Settings > Book).")
    if first > last:
        first, last = last, first
    if last - first > 80:
        raise BookError("Choose at most 80 pages at a time.")
    idx = book.pdf_pages_for(meta["printed"], first, last)
    if not idx:
        s = summary(meta)
        raise BookError(f"Printed pages {first} to {last} aren't in this book "
                        f"(it runs from {s['first_printed']} to {s['last_printed']}).")
    ver, size = meta["version"], meta["chunk"]
    needed = sorted({i // size for i in idx})
    chunks = dict(zip(needed, store.mget_json([f"book:text:{ver}:{k}" for k in needed])))
    out = []
    for i in idx:
        text = (chunks.get(i // size) or [""] * size)[i % size]
        printed = meta["printed"][i]
        out.append({"pdf": i + 1, "printed": printed,
                    "picture": printed is None, "text": text})
    words = sum(book.word_count(p["text"]) for p in out)
    return {"pages": out, "words": words, "first": first, "last": last}


def fix(store, pdf_page: int, printed: int) -> dict:
    """Tell the mapper that PDF page `pdf_page` carries the printed number `printed`."""
    meta = store.get_json(META)
    if not meta:
        raise BookError("Add the book first (Settings > Book).")
    if not 1 <= pdf_page <= meta["pdf_pages"] or not 1 <= printed <= book.MAX_PRINTED:
        raise BookError("That page number is out of range.")
    overrides = {str(k): v for k, v in (meta.get("overrides") or {}).items()}
    overrides[str(pdf_page - 1)] = printed
    mapping = book.build_map(meta["candidates"], meta["words"], meta.get("labels"),
                             {int(k): v for k, v in overrides.items()})
    meta.update(overrides=overrides, printed=mapping["printed"], method=mapping["method"])
    store.set_json(META, meta)
    return summary(meta)


def locate(store, printed: int) -> dict:
    """Which PDF page holds a printed page, with its opening words, for checking."""
    meta = store.get_json(META)
    if not meta:
        raise BookError("Add the book first (Settings > Book).")
    try:
        i = meta["printed"].index(printed)
    except ValueError:
        raise BookError(f"No page is numbered {printed}.") from None
    chunk = store.get_json(f"book:text:{meta['version']}:{i // meta['chunk']}") or []
    text = chunk[i % meta["chunk"]] if chunk else ""
    return {"printed": printed, "pdf": i + 1, "opening": " ".join(text.split()[:40])}


def remove(store) -> None:
    meta = store.get_json(META)
    if meta:
        store.delete(META, *[f"book:text:{meta['version']}:{k}"
                             for k in range(math.ceil(meta["pdf_pages"] / CHUNK))])
