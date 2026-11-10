"""
Core engine for Sealed Nectar -> Anki.

Pipeline:  extract text  ->  2 OpenAI requests (MCQ, cloze)  ->  validate and
verify every item against the source  ->  review tables  ->  Anki CSV files.

The web API (app.py) and the command line (python generator.py ...) both
call into this module.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import os
import random
import re
import sys
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
from pypdf import PdfReader
from rapidfuzz import fuzz

from prompts import (
    CLOZE_CATEGORIES,
    CLOZE_SCHEMA,
    CLOZE_TASK_TEMPLATE,
    DIFFICULTIES,
    FOCI,
    MCQ_SCHEMA,
    MCQ_TASK_TEMPLATE,
    PLACES,
    SYSTEM_PROMPT,
)

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

ALLOWED_MCQ_COUNTS = (5, 10, 15, 20)          # preselected increments of 5
ALLOWED_CLOZE_MAX = (5, 10, 15, 20, 25, 30)
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-astra")
DEFAULT_MCQ_DECK = "Sealed Nectar::MCQ"
DEFAULT_CLOZE_DECK = "Sealed Nectar::Hijrah Cloze"
MAX_SOURCE_WORDS = 40_000                     # ~ a very long chapter

MCQ_REVIEW_COLUMNS = [
    "Keep", "Place", "Difficulty", "Focus", "Question", "A", "B", "C", "D",
    "Answer", "Context", "Source excerpt", "Verified", "Check notes",
]
CLOZE_REVIEW_COLUMNS = [
    "Keep", "Place", "Category", "Text", "Extra", "Source excerpt", "Verified", "Check notes",
]


class GenerationError(RuntimeError):
    """A problem worth showing to the user as-is. `status` is the HTTP code the
    web API returns: 400 for bad input, 502 when the OpenAI call failed."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# 1. Text extraction
# ---------------------------------------------------------------------------

def clean_text(raw: str) -> str:
    """Tidy PDF/pasted text: re-join hyphenated line breaks, unwrap lines."""
    t = unicodedata.normalize("NFC", raw or "")
    t = t.replace("\r\n", "\n").replace("\r", "\n").replace("­", "")
    t = re.sub(r"(\w)-\n(?=[a-z])", r"\1", t)      # "migra-\ntion" -> "migration"
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"(?<!\n)\n(?!\n)", " ", t)          # unwrap lines inside paragraphs
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def pdf_page_count(data: bytes) -> int:
    return len(PdfReader(io.BytesIO(data)).pages)


def extract_pdf_text(data: bytes, first_page: int | None = None,
                     last_page: int | None = None) -> str:
    """Extract text from a page range (1-based, inclusive) of a PDF."""
    reader = PdfReader(io.BytesIO(data))
    total = len(reader.pages)
    first = max(1, first_page or 1)
    last = min(total, last_page or total)
    if first > last:
        raise GenerationError(f"Page range {first}-{last} is empty (the PDF has {total} pages).")
    text = clean_text("\n\n".join(
        (reader.pages[i].extract_text() or "") for i in range(first - 1, last)
    ))
    if len(text.split()) < 30:
        raise GenerationError(
            "Almost no text could be extracted from those pages. The PDF is probably "
            "scanned images: run OCR on it first, or paste the text instead."
        )
    return text


def word_count(text: str) -> int:
    return len(text.split())


# ---------------------------------------------------------------------------
# 2. Counts and difficulty mix
# ---------------------------------------------------------------------------

SUGGEST_THRESHOLDS = [(2500, 5), (4500, 10), (7000, 15), (None, 20)]


def suggest_mcq_count(words: int) -> int:
    """Pick a maximum (an increment of 5) from the extract's length. Cards stay
    few and high-yield: 7 pages (~3,000 words) suggests at most 10."""
    for limit, n in SUGGEST_THRESHOLDS:
        if limit is None or words <= limit:
            return n
    return 20


def difficulty_split(n: int) -> dict[str, int]:
    """30% Easy / 40% Medium / 30% Hard, rounded down, rest to Medium.
    5 -> 1/3/1, 10 -> 3/4/3, 15 -> 4/7/4, 20 -> 6/8/6."""
    if n not in ALLOWED_MCQ_COUNTS:
        raise ValueError(f"MCQ count must be one of {ALLOWED_MCQ_COUNTS}, got {n}")
    easy = hard = max(1, int(n * 0.3))
    return {"Easy": easy, "Medium": n - easy - hard, "Hard": hard}


def min_per_focus(n: int) -> int:
    return max(1, n // 5)


# ---------------------------------------------------------------------------
# 3. Verification against the source text
# ---------------------------------------------------------------------------

_NUMBER_WORDS = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen", "twenty",
]


def _norm(s: str) -> str:
    """Lowercase, strip diacritics and all punctuation (quotes, ayn/hamza marks, dashes)."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[\W_]+", " ", s.lower())
    return " ".join(s.split())


def _clean_str(x: Any) -> str:
    return " ".join(str(x or "").split())


_ORDINALS = [
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
    "ninth", "tenth", "eleventh", "twelfth",
]
_MAGNITUDES = {"hundred", "thousand", "half"}
_STOPWORDS = {
    "the", "a", "an", "of", "to", "and", "in", "on", "at", "for", "by", "with", "from",
    "his", "her", "their", "its", "he", "she", "they", "it", "was", "were", "is", "be",
    "as", "that", "this", "who", "which", "s",
}


def _number_values(s: str) -> set[str]:
    """Numbers in a string as comparable values: '3', 'three', '3rd', 'third' -> '3'."""
    vals = set()
    for t in _norm(s).split():
        m = re.fullmatch(r"(\d+)(st|nd|rd|th)?", t)
        if m:
            vals.add(str(int(m.group(1))))
        elif t in _NUMBER_WORDS:
            vals.add(str(_NUMBER_WORDS.index(t)))
        elif t in _ORDINALS:
            vals.add(str(_ORDINALS.index(t) + 1))
        elif t in _MAGNITUDES:
            vals.add(t)
    return vals


def _content_words(s: str) -> list[str]:
    return [t for t in _norm(s).split()
            if t not in _STOPWORDS and not _number_values(t)]


def _phrase_in(phrase: str, haystack_norm: str) -> bool:
    p = _norm(phrase)
    if not p:
        return False
    if f" {p} " in f" {haystack_norm} ":
        return True
    vals = _number_values(p)                     # lone number: "3" matches "three"
    return len(p.split()) == 1 and bool(vals) and bool(vals & _number_values(haystack_norm))


class SourceIndex:
    """Normalised copy of the source for checking what the model returns.
    Normalising removes case, punctuation, diacritics and line-break noise, so a
    faithfully copied excerpt matches exactly; anything else is flagged."""

    def __init__(self, text: str):
        self.norm = _norm(text)
        self.tokens = set(self.norm.split())
        self.number_vals = _number_values(self.norm)

    def check_excerpt(self, excerpt: str) -> tuple[bool, int]:
        """(verbatim?, similarity %). Only a word-for-word match counts as verified."""
        p = _norm(excerpt)
        if len(p.split()) < 4:
            return False, 0
        if p in self.norm:
            return True, 100
        return False, int(fuzz.partial_ratio(p, self.norm))

    def has_phrase(self, phrase: str) -> bool:
        return _phrase_in(phrase, self.norm)


def verify_mcq(item: dict, index: SourceIndex) -> list[str]:
    """Return check notes; an empty list means verified."""
    notes = []
    answer = item["correct_answer"]
    ok, score = index.check_excerpt(item["source_excerpt"])
    if not ok:
        notes.append(f"quoted excerpt not word-for-word in source ({score}% similar)")
    basis = _number_values(item["source_excerpt"]) if ok else index.number_vals
    missing = sorted(_number_values(answer) - basis)
    if missing:
        notes.append("number(s) in answer not in the quoted evidence: " + ", ".join(missing))
    if item["focus"] != "Theological Lesson" and len(answer.split()) <= 4:
        absent = [w for w in _content_words(answer) if w not in index.tokens]
        if absent:
            notes.append("answer word(s) not in source: " + ", ".join(absent))
    return notes


def verify_cloze(answers: list[str], excerpt: str, index: SourceIndex) -> list[str]:
    """Names and places must appear verbatim in the source; numbers (durations,
    distances, dates) must appear in the quoted excerpt that proves the card."""
    notes = []
    ok, score = index.check_excerpt(excerpt)
    if not ok:
        notes.append(f"quoted excerpt not word-for-word in source ({score}% similar)")
    basis = _norm(excerpt) if ok else index.norm
    for a in answers:
        vals = _number_values(a)
        if vals:
            if not (_phrase_in(a, basis) or vals <= _number_values(basis)):
                notes.append(f"'{a}' not in the quoted excerpt" if ok else f"'{a}' not found in source")
        elif not index.has_phrase(a):
            notes.append(f"'{a}' not found in source")
    return notes


# ---------------------------------------------------------------------------
# 4. OpenAI request (Responses API + Structured Outputs)
# ---------------------------------------------------------------------------

def make_client(api_key: str | None = None, timeout: float = 240, max_retries: int = 2):
    from openai import OpenAI

    key = (api_key or os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        raise GenerationError("No OpenAI API key. Set OPENAI_API_KEY or add your own key in Settings.")
    return OpenAI(api_key=key, timeout=timeout, max_retries=max_retries)


def _friendly_api_error(e: Exception) -> str:
    try:
        import openai
    except ImportError:  # pragma: no cover
        return f"OpenAI API error: {e}"
    if isinstance(e, openai.AuthenticationError):
        return "OpenAI rejected the API key (401). Check OPENAI_API_KEY or the key in Settings."
    if isinstance(e, openai.NotFoundError):
        return f"Model not found or not enabled for your account (404). Pick another model. Details: {e}"
    if isinstance(e, openai.RateLimitError):
        return "Rate limit or quota exceeded (429). Check your OpenAI billing/usage, wait a minute, and retry."
    if isinstance(e, openai.BadRequestError):
        return (f"OpenAI rejected the request (400): {e}. If it mentions json_schema or "
                "text.format, the chosen model does not support Structured Outputs; use a newer one.")
    if isinstance(e, openai.APIConnectionError):
        return "Could not reach the OpenAI API. Check your internet connection and retry."
    return f"OpenAI API error: {e}"


def _request_json(client, model: str, messages: list[dict], schema_name: str,
                  schema: dict) -> tuple[dict, str]:
    try:
        resp = client.responses.create(
            model=model,
            instructions=SYSTEM_PROMPT,
            input=messages,
            text={"format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": schema,
            }},
            store=False,
        )
    except GenerationError:
        raise
    except Exception as e:  # noqa: BLE001 - surfaced to the user verbatim
        raise GenerationError(_friendly_api_error(e), status=502) from e

    for item in getattr(resp, "output", None) or []:
        if getattr(item, "type", None) == "message":
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) == "refusal":
                    raise GenerationError(f"The model declined the request: {part.refusal}", status=502)
    if getattr(resp, "status", "completed") == "incomplete":
        reason = getattr(getattr(resp, "incomplete_details", None), "reason", "unknown")
        raise GenerationError(
            f"The response was cut off ({reason}). Try fewer questions or a shorter extract.",
            status=502,
        )
    raw = resp.output_text or ""
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError as e:
        raise GenerationError(f"The model returned invalid JSON: {e}", status=502) from e


# ---------------------------------------------------------------------------
# 5. MCQs
# ---------------------------------------------------------------------------

def _place(value) -> str:
    return value if value in PLACES else "Other"


def _validate_mcqs(raw_items: list[dict]) -> tuple[list[dict], list[str]]:
    valid, dropped, seen = [], [], set()
    for i, it in enumerate(raw_items or [], 1):
        q = _clean_str(it.get("question"))
        a = _clean_str(it.get("correct_answer"))
        ds: list[str] = []
        for d in it.get("distractors") or []:
            d = _clean_str(d)
            if d and _norm(d) != _norm(a) and _norm(d) not in {_norm(x) for x in ds}:
                ds.append(d)
        reason = None
        if not q or not a:
            reason = "empty question or answer"
        elif len(ds) < 3:
            reason = f"only {len(ds)} usable distractor(s)"
        elif it.get("difficulty") not in DIFFICULTIES or it.get("focus") not in FOCI:
            reason = "invalid difficulty or focus label"
        elif _norm(q) in seen:
            reason = "duplicate question"
        if reason:
            dropped.append(f"#{i} ({reason})")
            continue
        seen.add(_norm(q))
        valid.append({
            "place": _place(it.get("place")),
            "difficulty": it["difficulty"],
            "focus": it["focus"],
            "question": q,
            "correct_answer": a,
            "distractors": ds[:3],
            "context": _clean_str(it.get("context")),
            "source_excerpt": _clean_str(it.get("source_excerpt")),
        })
    return valid, dropped


def _trim_to_max(items: list[dict], max_n: int) -> list[dict]:
    """Keep at most max_n, preserving the requested difficulty proportions when trimming."""
    if len(items) <= max_n:
        chosen = items
    else:
        targets = difficulty_split(max_n) if max_n in ALLOWED_MCQ_COUNTS else None
        chosen = []
        if targets:
            by = {d: [it for it in items if it["difficulty"] == d] for d in DIFFICULTIES}
            chosen = [it for d in DIFFICULTIES for it in by[d][:targets[d]]]
        spare = [it for it in items if it not in chosen]
        chosen = chosen + spare[: max_n - len(chosen)]
    order = {d: i for i, d in enumerate(DIFFICULTIES)}
    return sorted(chosen, key=lambda it: order[it["difficulty"]])       # Easy -> Medium -> Hard


def _assign_positions(items: list[dict], seed: str) -> None:
    """Shuffle options so the correct letter is spread evenly over A-D (no 'always C' bias)."""
    rng = random.Random(seed)
    positions = [i % 4 for i in range(len(items))]
    rng.shuffle(positions)
    for it, pos in zip(items, positions):
        opts = it["distractors"][:]
        rng.shuffle(opts)
        opts.insert(pos, it["correct_answer"])
        it["options"] = opts
        it["answer_letter"] = "ABCD"[pos]


def generate_mcqs(client, model: str, source: str, chapter: str, max_n: int,
                  index: SourceIndex | None = None) -> tuple[list[dict], list[str], list[str]]:
    """Up to max_n questions on the most important facts. Fewer is fine: the
    prompt tells the model not to pad when a passage has little that matters."""
    targets = difficulty_split(max_n)
    index = index or SourceIndex(source)
    task = MCQ_TASK_TEMPLATE.substitute(
        max_total=max_n, n_easy=targets["Easy"], n_medium=targets["Medium"],
        n_hard=targets["Hard"], chapter=chapter, source_text=source,
    )
    data, raw = _request_json(client, model, [{"role": "user", "content": task}], "mcq_set", MCQ_SCHEMA)
    items, dropped = _validate_mcqs(data.get("questions", []))
    chosen = _trim_to_max(items, max_n)

    warnings = []
    if dropped:
        warnings.append(f"Questions removed as invalid: {', '.join(dropped)}.")
    if not chosen:
        warnings.append("No questions: these pages have little about Makkah, Madinah or the "
                        "core Seerah, so nothing was padded in.")
    elif len(chosen) >= 5:
        counts = Counter(it["difficulty"] for it in chosen)
        missing = [d for d in DIFFICULTIES if not counts.get(d)]
        if missing:
            warnings.append("No " + " or ".join(missing).lower() + " questions in this set.")

    seed = chapter + hashlib.sha256(source.encode()).hexdigest()
    _assign_positions(chosen, seed)
    for it in chosen:
        notes = verify_mcq(it, index)
        it["verified"] = not notes
        it["check_notes"] = "; ".join(notes)
    return chosen, warnings, [raw]


# ---------------------------------------------------------------------------
# 6. Cloze cards
# ---------------------------------------------------------------------------

CLOZE_RE = re.compile(r"\{\{c(\d+)::((?:(?!::|\}\}).)+)(?:::((?:(?!\}\}).)*))?\}\}")


def check_cloze(text: str) -> tuple[str | None, list[str], list[str]]:
    """Validate Anki cloze syntax. Returns (fixed_text or None, answers, issues).
    Deletions are renumbered c1, c2, ... in order of first appearance."""
    text = _clean_str(text)
    matches = list(CLOZE_RE.finditer(text))
    if not matches:
        return None, [], ["no {{c1::...}} deletion"]
    leftover = CLOZE_RE.sub("", text)
    if "{{" in leftover or "}}" in leftover or any("{{" in m.group(2) for m in matches):
        return None, [], ["malformed or nested cloze braces"]
    if len(_norm(leftover).split()) < 3:
        return None, [], ["sentence is almost entirely deleted"]

    order: list[str] = []
    for m in matches:
        if m.group(1) not in order:
            order.append(m.group(1))
    mapping = {old: str(i) for i, old in enumerate(order, 1)}

    def _renumber(m: re.Match) -> str:
        hint = (m.group(3) or "").strip()
        return "{{c%s::%s%s}}" % (mapping[m.group(1)], m.group(2).strip(), f"::{hint}" if hint else "")

    fixed = CLOZE_RE.sub(_renumber, text)
    issues = []
    if len(order) > 3:
        issues.append(f"{len(order)} deletions (3 max recommended)")
    if re.search(r"\}\}\s*\{\{", fixed):
        issues.append("adjacent deletions")
    return fixed, [m.group(2).strip() for m in matches], issues


def cloze_plain(text: str) -> str:
    return CLOZE_RE.sub(lambda m: m.group(2), text)


def generate_cloze(client, model: str, source: str, chapter: str, max_cards: int,
                   index: SourceIndex | None = None) -> tuple[list[dict], list[str], list[str]]:
    index = index or SourceIndex(source)
    task = CLOZE_TASK_TEMPLATE.substitute(chapter=chapter, max_cards=max_cards, source_text=source)
    data, raw = _request_json(client, model, [{"role": "user", "content": task}],
                              "cloze_set", CLOZE_SCHEMA)
    cards, dropped, seen = [], [], set()
    for i, c in enumerate(data.get("cards", []), 1):
        fixed, answers, issues = check_cloze(c.get("text", ""))
        if fixed is None:
            dropped.append(f"#{i} ({issues[0]})")
            continue
        key = _norm(cloze_plain(fixed))
        if key in seen:
            dropped.append(f"#{i} (duplicate)")
            continue
        seen.add(key)
        excerpt = _clean_str(c.get("source_excerpt"))
        notes = list(issues) + verify_cloze(answers, excerpt, index)
        category = c.get("category") if c.get("category") in CLOZE_CATEGORIES else "Event"
        cards.append({
            "place": _place(c.get("place")),
            "category": category,
            "text": fixed,
            "extra": _clean_str(c.get("extra")),
            "source_excerpt": excerpt,
            "verified": not notes,
            "check_notes": "; ".join(notes),
        })
    warnings = []
    if len(cards) > max_cards:
        cards = cards[:max_cards]
    if dropped:
        warnings.append(f"Fill-in cards removed as invalid: {', '.join(dropped)}.")
    if not cards:
        warnings.append("No fill-in cards: these pages have no concrete facts about Makkah, "
                        "Madinah or the Hijrah route.")
    return cards, warnings, [raw]


# ---------------------------------------------------------------------------
# 7. Orchestration
# ---------------------------------------------------------------------------

def prepare_source(source: str, chapter: str) -> tuple[str, str]:
    """Clean and validate the extract and chapter label before any API call."""
    source = clean_text(source)
    chapter = _clean_str(chapter)
    if not chapter:
        raise GenerationError("Enter a chapter label (it becomes the Anki tag).")
    if word_count(source) < 100:
        raise GenerationError("The extract is under 100 words. Select more pages or paste more text.")
    if word_count(source) > MAX_SOURCE_WORDS:
        raise GenerationError(f"The extract is over {MAX_SOURCE_WORDS:,} words. Split it into smaller parts.")
    return source, chapter


@dataclass
class GenerationResult:
    mcq_review: pd.DataFrame
    cloze_review: pd.DataFrame
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def mcq_review_df(items: list[dict]) -> pd.DataFrame:
    rows = [{
        "Keep": True,
        "Place": it.get("place", "Other"),
        "Difficulty": it["difficulty"],
        "Focus": it["focus"],
        "Question": it["question"],
        "A": it["options"][0], "B": it["options"][1],
        "C": it["options"][2], "D": it["options"][3],
        "Answer": it["answer_letter"],
        "Context": it["context"],
        "Source excerpt": it["source_excerpt"],
        "Verified": it["verified"],
        "Check notes": it["check_notes"],
    } for it in items]
    return pd.DataFrame(rows, columns=MCQ_REVIEW_COLUMNS)


def cloze_review_df(cards: list[dict]) -> pd.DataFrame:
    rows = [{
        "Keep": True,
        "Place": c.get("place", "Other"),
        "Category": c["category"],
        "Text": c["text"],
        "Extra": c["extra"],
        "Source excerpt": c["source_excerpt"],
        "Verified": c["verified"],
        "Check notes": c["check_notes"],
    } for c in cards]
    return pd.DataFrame(rows, columns=CLOZE_REVIEW_COLUMNS)


def run_generation(client, model: str, source: str, chapter: str, n_mcq: int,
                   max_cloze: int) -> GenerationResult:
    """Run the MCQ and cloze requests in parallel and return review tables."""
    source, chapter = prepare_source(source, chapter)

    index = SourceIndex(source)
    result = GenerationResult(mcq_review_df([]), cloze_review_df([]))
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_mcq = pool.submit(generate_mcqs, client, model, source, chapter, n_mcq, index)
        f_cloze = pool.submit(generate_cloze, client, model, source, chapter, max_cloze, index)
        try:
            items, warns, raws = f_mcq.result()
            result.mcq_review = mcq_review_df(items)
            result.warnings += warns
            result.raw["mcq_responses"] = raws
        except GenerationError as e:
            result.errors.append(f"MCQs: {e}")
        try:
            cards, warns, raws = f_cloze.result()
            result.cloze_review = cloze_review_df(cards)
            result.warnings += warns
            result.raw["cloze_responses"] = raws
        except GenerationError as e:
            result.errors.append(f"Cloze: {e}")
    result.raw.update({"model": model, "chapter": chapter, "n_mcq": n_mcq,
                       "max_cloze": max_cloze})
    return result


# ---------------------------------------------------------------------------
# 8. Anki export
# ---------------------------------------------------------------------------

def tag_slug(s: str) -> str:
    """Anki tags cannot contain spaces: 'Ch. 12 - The Hijrah' -> 'Ch_12_The_Hijrah'."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_") or "Unlabeled"


def _h(s: Any) -> str:
    """HTML-escape a field (the CSV is imported with 'Allow HTML' on)."""
    return html.escape(str(s if s is not None else ""), quote=False).replace("\n", "<br>")


def _base_tags(chapter: str, day: int | None) -> list[str]:
    tags = ["SealedNectar", f"Chapter::{tag_slug(chapter)}"]
    if day:
        tags.append(f"Day::{int(day):03d}")
    return tags


def build_mcq_notes(review: pd.DataFrame, chapter: str, day: int | None = None,
                    deck: str = DEFAULT_MCQ_DECK) -> pd.DataFrame:
    """Basic note type -> Front, Back, Tags, Deck."""
    rows = []
    for r in review.to_dict("records"):
        if not r.get("Keep"):
            continue
        opts = [r["A"], r["B"], r["C"], r["D"]]
        letter = str(r["Answer"]).strip().upper()[:1]
        if not letter or letter not in "ABCD":
            continue
        front = _h(r["Question"]) + "<br><br>" + "<br>".join(
            f"<b>{L}.</b> {_h(o)}" for L, o in zip("ABCD", opts))
        back = f"<b>{letter}. {_h(opts['ABCD'.index(letter)])}</b>"
        if _clean_str(r.get("Context")):
            back += f"<br><br>{_h(r['Context'])}"
        if _clean_str(r.get("Source excerpt")):
            back += f"<br><br><i>Source: “{_h(r['Source excerpt'])}”</i>"
        back += f"<br><br><small>{_h(chapter)} · {_h(r['Difficulty'])} · {_h(r['Focus'])}</small>"
        tags = _base_tags(chapter, day) + [f"Place::{tag_slug(r.get('Place') or 'Other')}",
                                           f"Difficulty::{tag_slug(r['Difficulty'])}",
                                           f"Focus::{tag_slug(r['Focus'])}"]
        if not r.get("Verified"):
            tags.append("Needs_Review")
        rows.append({"Front": front, "Back": back, "Tags": " ".join(tags), "Deck": deck})
    return pd.DataFrame(rows, columns=["Front", "Back", "Tags", "Deck"])


def build_cloze_notes(review: pd.DataFrame, chapter: str, day: int | None = None,
                      deck: str = DEFAULT_CLOZE_DECK) -> pd.DataFrame:
    """Cloze note type -> Text, Back Extra, Tags, Deck."""
    rows = []
    for r in review.to_dict("records"):
        if not r.get("Keep"):
            continue
        fixed, _, _ = check_cloze(r["Text"])          # re-check after manual edits
        if fixed is None:
            continue
        extra = _h(r.get("Extra"))
        if _clean_str(r.get("Source excerpt")):
            extra += f"<br><br><i>Source: “{_h(r['Source excerpt'])}”</i>"
        extra += f"<br><br><small>{_h(chapter)} · {_h(r['Category'])}</small>"
        tags = _base_tags(chapter, day) + [f"Place::{tag_slug(r.get('Place') or 'Other')}",
                                           f"Category::{tag_slug(r['Category'])}"]
        if r.get("Place") == "Hijrah route":
            tags.append("Hijrah_Trek")
        if not r.get("Verified"):
            tags.append("Needs_Review")
        rows.append({"Text": _h(fixed), "Back Extra": extra, "Tags": " ".join(tags), "Deck": deck})
    return pd.DataFrame(rows, columns=["Text", "Back Extra", "Tags", "Deck"])


def anki_csv(notes: pd.DataFrame, notetype: str) -> str:
    """CSV with Anki file headers (Anki 2.1.54+), so import needs no manual mapping."""
    cols = list(notes.columns)
    header = [
        "#separator:Comma",
        "#html:true",
        f"#notetype:{notetype}",
        f"#columns:{','.join(cols)}",
        f"#tags column:{cols.index('Tags') + 1}",
        f"#deck column:{cols.index('Deck') + 1}",
    ]
    body = notes.to_csv(index=False, header=False, quoting=csv.QUOTE_ALL, lineterminator="\n")
    return "\n".join(header) + "\n" + body


def anki_csv_combined(mcq_notes: pd.DataFrame, cloze_notes: pd.DataFrame) -> str:
    """Both decks in one file (one import on a phone). A note-type column tells
    Anki which rows are Basic and which are Cloze; each row's two fields map to
    Front/Back or Text/Back Extra in order."""
    rows = [
        {"Note type": "Basic", "Deck": r["Deck"], "Tags": r["Tags"],
         "Field 1": r["Front"], "Field 2": r["Back"]}
        for r in mcq_notes.to_dict("records")
    ] + [
        {"Note type": "Cloze", "Deck": r["Deck"], "Tags": r["Tags"],
         "Field 1": r["Text"], "Field 2": r["Back Extra"]}
        for r in cloze_notes.to_dict("records")
    ]
    cols = ["Note type", "Deck", "Tags", "Field 1", "Field 2"]
    df = pd.DataFrame(rows, columns=cols)
    header = [
        "#separator:Comma",
        "#html:true",
        f"#columns:{','.join(cols)}",
        "#notetype column:1",
        "#deck column:2",
        "#tags column:3",
    ]
    body = df.to_csv(index=False, header=False, quoting=csv.QUOTE_ALL, lineterminator="\n")
    return "\n".join(header) + "\n" + body


def output_stem(chapter: str, day: int | None = None) -> str:
    return (f"Day{int(day):03d}_" if day else "") + tag_slug(chapter)


def save_outputs(out_dir: str | Path, chapter: str, day: int | None, mcq_csv: str,
                 cloze_csv: str, raw: dict | None = None) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = output_stem(chapter, day)
    paths = {"mcq": out / f"{stem}_MCQ.csv", "cloze": out / f"{stem}_Cloze.csv"}
    paths["mcq"].write_text(mcq_csv, encoding="utf-8")
    paths["cloze"].write_text(cloze_csv, encoding="utf-8")
    if raw:
        paths["raw"] = out / f"{stem}_raw.json"
        paths["raw"].write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return paths


# ---------------------------------------------------------------------------
# 9. Command line (same engine, no browser):
#    python generator.py chapter.pdf --pages 92-98 --chapter "The Hijrah" --day 14 --mcq 15
# ---------------------------------------------------------------------------

def _cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Sealed Nectar extract -> two Anki CSV files")
    p.add_argument("input", help="PDF or .txt file")
    p.add_argument("--chapter", required=True, help="Chapter label (becomes the Chapter:: tag)")
    p.add_argument("--pages", help="PDF page range, e.g. 92-98 (default: all pages)")
    p.add_argument("--day", type=int, default=None, help="Reading day 1-85 (optional Day:: tag)")
    p.add_argument("--mcq", type=int, default=0, choices=(0,) + ALLOWED_MCQ_COUNTS,
                   help="Maximum MCQs; 0 = suggest from length (default)")
    p.add_argument("--cloze-max", type=int, default=15, choices=ALLOWED_CLOZE_MAX)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--mcq-deck", default=DEFAULT_MCQ_DECK)
    p.add_argument("--cloze-deck", default=DEFAULT_CLOZE_DECK)
    p.add_argument("--out", default="output")
    a = p.parse_args(argv)

    path = Path(a.input)
    if path.suffix.lower() == ".pdf":
        first = last = None
        if a.pages:
            first, _, last = a.pages.partition("-")
            first, last = int(first), int(last or first)
        source = extract_pdf_text(path.read_bytes(), first, last)
    else:
        source = clean_text(path.read_text(encoding="utf-8"))
    n = a.mcq or suggest_mcq_count(word_count(source))
    print(f"{word_count(source):,} words -> {n} MCQs (mix {difficulty_split(n)}), up to {a.cloze_max} cloze")

    res = run_generation(make_client(), a.model, source, a.chapter, n, a.cloze_max)
    for w in res.warnings:
        print("WARNING:", w)
    for e in res.errors:
        print("ERROR:", e, file=sys.stderr)
    mcq_csv = anki_csv(build_mcq_notes(res.mcq_review, a.chapter, a.day, a.mcq_deck), "Basic")
    cloze_csv = anki_csv(build_cloze_notes(res.cloze_review, a.chapter, a.day, a.cloze_deck), "Cloze")
    paths = save_outputs(a.out, a.chapter, a.day, mcq_csv, cloze_csv, res.raw)
    flagged = int((~res.mcq_review["Verified"].astype(bool)).sum() +
                  (~res.cloze_review["Verified"].astype(bool)).sum())
    print(f"{len(res.mcq_review)} MCQs, {len(res.cloze_review)} cloze cards "
          f"({flagged} tagged Needs_Review)")
    for k, v in paths.items():
        print(f"{k:>5}: {v}")
    return 1 if res.errors else 0


if __name__ == "__main__":
    try:
        sys.exit(_cli())
    except GenerationError as e:
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)
