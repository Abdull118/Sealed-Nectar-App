"""
Sealed Nectar study cards: web API (FastAPI) and Vercel entrypoint.

Vercel finds `app` in this file automatically and serves public/ from its CDN.
Locally:  uvicorn app:app --reload   (this file then serves public/ itself)

Storage: Upstash Redis on Vercel (Storage tab > Upstash for Redis), or files in
./.data when running locally. See store.py.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import bookstore
import deck
import generator as g
import store as store_mod
from prompts import CLOZE_CATEGORIES, DIFFICULTIES, FOCI, PLACES

ON_VERCEL = bool(os.getenv("VERCEL"))
MAX_TEXT_CHARS = 400_000
OPENAI_TIMEOUT = 135              # seconds per call; well under Vercel's 300 s

app = FastAPI(title="Sealed Nectar study cards", docs_url=None, redoc_url=None, openapi_url=None)

# Tests replace these.
client_factory = g.make_client
store_factory = store_mod.default_store


# ---------------------------------------------------------------------------
# Access
#   Saved data (book text, cards, plan) always needs the passcode when
#   APP_PASSCODE is set; on Vercel it must be set.
#   Generating with the server's OpenAI key needs the passcode too; a request
#   that brings its own OpenAI key (X-OpenAI-Key) may generate without it.
# ---------------------------------------------------------------------------

def _check_passcode(passcode: str | None) -> None:
    expected = os.getenv("APP_PASSCODE", "")
    if expected:
        if not hmac.compare_digest((passcode or "").encode(), expected.encode()):
            raise HTTPException(401, "Wrong or missing passcode. Enter it in Settings.")
    elif ON_VERCEL:
        raise HTTPException(503, "This deployment has no APP_PASSCODE. Add it in Vercel > Settings > "
                                 "Environment Variables, then redeploy.")


def _store(passcode: str | None):
    _check_passcode(passcode)
    st, kind = store_factory()
    if st is None:
        raise HTTPException(503, "No storage connected. In Vercel, open the project > Storage > "
                                 "Create Database > Upstash for Redis (free), connect it, then redeploy.")
    return st


def _get_client(passcode: str | None, user_key: str | None):
    user_key = (user_key or "").strip()
    if user_key:
        return client_factory(user_key, timeout=OPENAI_TIMEOUT, max_retries=0)
    server_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not server_key:
        raise HTTPException(400, "No OpenAI API key on the server. Add your own key in Settings.")
    _check_passcode(passcode)
    return client_factory(server_key, timeout=OPENAI_TIMEOUT, max_retries=0)


@app.exception_handler(g.GenerationError)
async def _generation_error(_, exc: g.GenerationError):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


@app.exception_handler(bookstore.BookError)
async def _book_error(_, exc: bookstore.BookError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(store_mod.StoreError)
async def _store_error(_, exc: store_mod.StoreError):
    return JSONResponse({"detail": f"Storage problem: {exc}"}, status_code=502)


Passcode = Header(default=None, alias="X-App-Passcode")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    text: str = Field(max_length=MAX_TEXT_CHARS)
    chapter: str = Field(max_length=200)
    n_mcq: int = 10
    max_cloze: int = 10
    model: str | None = Field(default=None, max_length=100)


class McqIn(BaseModel):
    keep: bool = True
    place: str = "Other"
    difficulty: str
    focus: str
    question: str = Field(max_length=2000)
    options: list[str] = Field(min_length=4, max_length=4)
    answer: Literal["A", "B", "C", "D"]
    context: str = ""
    source_excerpt: str = ""
    verified: bool = False
    check_notes: str = ""


class ClozeIn(BaseModel):
    keep: bool = True
    place: str = "Other"
    category: str = "Event"
    text: str = Field(max_length=2000)
    extra: str = ""
    source_excerpt: str = ""
    verified: bool = False
    check_notes: str = ""


class SaveRequest(BaseModel):
    chapter: str = Field(max_length=200)
    day: int | None = Field(default=None, ge=0, le=999)
    printed: list[int] | None = Field(default=None, max_length=2)
    mcq: list[McqIn] = []
    cloze: list[ClozeIn] = []


class NoteChanges(BaseModel):
    suspended: bool | None = None
    place: str | None = None
    question: str | None = Field(default=None, max_length=2000)
    options: list[str] | None = Field(default=None, min_length=4, max_length=4)
    answer: Literal["A", "B", "C", "D"] | None = None
    context: str | None = None
    text: str | None = Field(default=None, max_length=2000)
    extra: str | None = None


class ReviewRequest(BaseModel):
    note_id: str = Field(max_length=60)
    item: str = Field(max_length=10)
    rating: int = Field(ge=1, le=4)
    tz: str | None = Field(default=None, max_length=60)


class Plan(BaseModel):
    day: int = Field(default=1, ge=1, le=999)
    chapter: str = Field(default="", max_length=200)
    first: int = Field(default=1, ge=1, le=5000)
    last: int = Field(default=7, ge=1, le=5000)
    mode: Literal["book", "paste"] = "book"
    saved_on: str | None = Field(default=None, max_length=10)


class BookBegin(BaseModel):
    name: str = Field(max_length=300)
    pages: int = Field(ge=1, le=bookstore.MAX_PAGES)
    labels: list[str | None] | None = None


class BookPages(BaseModel):
    version: str = Field(max_length=20)
    start: int = Field(ge=0)
    texts: list[str] = Field(max_length=bookstore.CHUNK)


class BookFinish(BaseModel):
    version: str = Field(max_length=20)


class BookFix(BaseModel):
    pdf_page: int = Field(ge=1)
    printed: int = Field(ge=1)


# ---------------------------------------------------------------------------
# Config and state
# ---------------------------------------------------------------------------

@app.get("/api/config")
def config():
    _, kind = store_factory()
    return {
        "default_model": g.DEFAULT_MODEL,
        "mcq_counts": list(g.ALLOWED_MCQ_COUNTS),
        "cloze_max": list(g.ALLOWED_CLOZE_MAX),
        "splits": {n: g.difficulty_split(n) for n in g.ALLOWED_MCQ_COUNTS},
        "suggest": [[limit, n] for limit, n in g.SUGGEST_THRESHOLDS],
        "decks": {"mcq": g.DEFAULT_MCQ_DECK, "cloze": g.DEFAULT_CLOZE_DECK},
        "places": PLACES, "difficulties": DIFFICULTIES, "foci": FOCI,
        "cloze_categories": CLOZE_CATEGORIES,
        "server_key": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        "passcode_required": bool(os.getenv("APP_PASSCODE")) or ON_VERCEL,
        "storage": kind,
    }


@app.get("/api/state")
def state(x_app_passcode: str | None = Passcode):
    st = _store(x_app_passcode)
    return {"plan": st.get_json("plan"), "book": bookstore.get_summary(st)}


@app.put("/api/plan")
def save_plan(plan: Plan, x_app_passcode: str | None = Passcode):
    st = _store(x_app_passcode)
    st.set_json("plan", plan.model_dump())
    return {"ok": True}


# ---------------------------------------------------------------------------
# Book
# ---------------------------------------------------------------------------

@app.post("/api/book/begin")
def book_begin(req: BookBegin, x_app_passcode: str | None = Passcode):
    st = _store(x_app_passcode)
    return {"version": bookstore.begin(st, req.name, req.pages, req.labels), "chunk": bookstore.CHUNK}


@app.post("/api/book/pages")
def book_pages(req: BookPages, x_app_passcode: str | None = Passcode):
    st = _store(x_app_passcode)
    bookstore.put_pages(st, req.version, req.start, req.texts)
    return {"ok": True}


@app.post("/api/book/finish")
def book_finish(req: BookFinish, x_app_passcode: str | None = Passcode):
    st = _store(x_app_passcode)
    return bookstore.finish(st, req.version)


@app.get("/api/book/text")
def book_text(first: int = Query(ge=1), last: int = Query(ge=1),
              x_app_passcode: str | None = Passcode):
    st = _store(x_app_passcode)
    return bookstore.pages(st, first, last)


@app.get("/api/book/locate")
def book_locate(printed: int = Query(ge=1), x_app_passcode: str | None = Passcode):
    return bookstore.locate(_store(x_app_passcode), printed)


@app.post("/api/book/fix")
def book_fix(req: BookFix, x_app_passcode: str | None = Passcode):
    return bookstore.fix(_store(x_app_passcode), req.pdf_page, req.printed)


@app.delete("/api/book")
def book_delete(x_app_passcode: str | None = Passcode):
    bookstore.remove(_store(x_app_passcode))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Generation (nothing is saved until the reviewed cards are posted to /api/cards)
# ---------------------------------------------------------------------------

def _prepare(req: GenerateRequest) -> tuple[str, str, str]:
    source, chapter = g.prepare_source(req.text, req.chapter)
    return source, chapter, (req.model or "").strip() or g.DEFAULT_MODEL


@app.post("/api/generate/mcq")
def generate_mcq(req: GenerateRequest, x_app_passcode: str | None = Passcode,
                 x_openai_key: str | None = Header(default=None)):
    if req.n_mcq not in g.ALLOWED_MCQ_COUNTS:
        raise HTTPException(400, f"n_mcq must be one of {list(g.ALLOWED_MCQ_COUNTS)}")
    source, chapter, model = _prepare(req)
    client = _get_client(x_app_passcode, x_openai_key)
    items, warnings, _ = g.generate_mcqs(client, model, source, chapter, req.n_mcq)
    return {
        "items": [{
            "keep": True, "place": it["place"], "difficulty": it["difficulty"], "focus": it["focus"],
            "question": it["question"], "options": it["options"], "answer": it["answer_letter"],
            "context": it["context"], "source_excerpt": it["source_excerpt"],
            "verified": it["verified"], "check_notes": it["check_notes"],
        } for it in items],
        "warnings": warnings, "model": model,
    }


@app.post("/api/generate/cloze")
def generate_cloze(req: GenerateRequest, x_app_passcode: str | None = Passcode,
                   x_openai_key: str | None = Header(default=None)):
    if req.max_cloze not in g.ALLOWED_CLOZE_MAX:
        raise HTTPException(400, f"max_cloze must be one of {list(g.ALLOWED_CLOZE_MAX)}")
    source, chapter, model = _prepare(req)
    client = _get_client(x_app_passcode, x_openai_key)
    cards, warnings, _ = g.generate_cloze(client, model, source, chapter, req.max_cloze)
    return {"items": [{"keep": True, **c} for c in cards], "warnings": warnings, "model": model}


# ---------------------------------------------------------------------------
# Saved cards and study
# ---------------------------------------------------------------------------

@app.post("/api/cards")
def save_cards(req: SaveRequest, x_app_passcode: str | None = Passcode):
    st = _store(x_app_passcode)
    chapter = " ".join(req.chapter.split()) or "Unlabeled"
    return deck.save_group(st, chapter, req.day or None, req.printed,
                           [m.model_dump() for m in req.mcq], [c.model_dump() for c in req.cloze])


@app.get("/api/cards")
def list_cards(tz: str | None = None, x_app_passcode: str | None = Passcode):
    return {"groups": deck.list_cards(_store(x_app_passcode), tz)}


@app.patch("/api/cards/{note_id}")
def edit_card(note_id: str, changes: NoteChanges, x_app_passcode: str | None = Passcode):
    st = _store(x_app_passcode)
    try:
        note = deck.update_note(st, note_id, changes.model_dump(exclude_none=True))
    except KeyError:
        raise HTTPException(404, "That card no longer exists.") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {k: v for k, v in note.items() if k != "items"}


@app.delete("/api/cards/{note_id}")
def delete_card(note_id: str, x_app_passcode: str | None = Passcode):
    try:
        deck.delete_note(_store(x_app_passcode), note_id)
    except KeyError:
        raise HTTPException(404, "That card no longer exists.") from None
    return {"ok": True}


@app.get("/api/study")
def study(place: str = "all", tz: str | None = None, new_limit: int | None = Query(default=None, ge=0),
          x_app_passcode: str | None = Passcode):
    return deck.study_queue(_store(x_app_passcode), place, tz, new_limit or None)


@app.post("/api/study/review")
def study_review(req: ReviewRequest, x_app_passcode: str | None = Passcode):
    st = _store(x_app_passcode)
    try:
        return deck.review(st, req.note_id, req.item, req.rating, req.tz)
    except KeyError:
        raise HTTPException(404, "That card no longer exists.") from None


@app.get("/api/export/anki")
def export_anki(mcq_deck: str = g.DEFAULT_MCQ_DECK, cloze_deck: str = g.DEFAULT_CLOZE_DECK,
                x_app_passcode: str | None = Passcode):
    csv_text, count = deck.export_csv(_store(x_app_passcode), mcq_deck[:200], cloze_deck[:200])
    return {"name": "SealedNectar_all_cards.csv", "csv": csv_text, "count": count}


# Local development only: Vercel serves public/ from its CDN and must not see this mount.
if not ON_VERCEL:
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=Path(__file__).parent / "public", html=True), name="public")
