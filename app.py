"""
Sealed Nectar -> Anki: web API (FastAPI) and Vercel entrypoint.

Vercel finds `app` in this file automatically and serves public/ from its CDN.
Locally:  uvicorn app:app --reload   (this file then serves public/ itself)

Endpoints
  GET  /api/config          settings the page needs (counts, decks, auth mode)
  POST /api/generate/mcq    one extract -> validated, verified MCQs
  POST /api/generate/cloze  one extract -> validated, verified cloze cards
  POST /api/export          reviewed cards -> Anki CSV files

MCQs and cloze cards are separate requests, so each gets its own time budget
on Vercel and the page shows each set as soon as it is ready.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Literal

import pandas as pd
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import generator as g
from prompts import CLOZE_CATEGORIES, DIFFICULTIES, FOCI

ON_VERCEL = bool(os.getenv("VERCEL"))
MAX_TEXT_CHARS = 400_000          # far below Vercel's 4.5 MB request limit
OPENAI_TIMEOUT = 135              # seconds per call; two calls stay under Vercel's 300 s

app = FastAPI(title="Sealed Nectar to Anki", docs_url=None, redoc_url=None, openapi_url=None)

# Tests replace this to avoid real API calls.
client_factory = g.make_client


# ---------------------------------------------------------------------------
# Access control
#   - Your own key in Settings (X-OpenAI-Key header): always allowed.
#   - Server key (OPENAI_API_KEY env var): requires APP_PASSCODE when set.
#     On Vercel, a server key without APP_PASSCODE is refused, so a public URL
#     can never spend your OpenAI credit for strangers.
# ---------------------------------------------------------------------------

def _get_client(passcode: str | None, user_key: str | None):
    user_key = (user_key or "").strip()
    if user_key:
        return client_factory(user_key, timeout=OPENAI_TIMEOUT, max_retries=0)
    server_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not server_key:
        raise HTTPException(400, "No OpenAI API key on the server. Add your own key in Settings.")
    expected = os.getenv("APP_PASSCODE", "")
    if expected:
        if not hmac.compare_digest((passcode or "").encode(), expected.encode()):
            raise HTTPException(401, "Wrong or missing passcode. Enter it in Settings.")
    elif ON_VERCEL:
        raise HTTPException(503, "This deployment has an OpenAI key but no APP_PASSCODE. "
                                 "Add APP_PASSCODE in Vercel > Settings > Environment Variables, then redeploy.")
    return client_factory(server_key, timeout=OPENAI_TIMEOUT, max_retries=0)


@app.exception_handler(g.GenerationError)
async def _generation_error(_, exc: g.GenerationError):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    text: str = Field(max_length=MAX_TEXT_CHARS)
    chapter: str = Field(max_length=200)
    n_mcq: int = 10
    max_cloze: int = 15
    scope: Literal["strict", "broad"] = "strict"
    model: str | None = Field(default=None, max_length=100)


class McqItem(BaseModel):
    keep: bool = True
    difficulty: str
    focus: str
    question: str
    options: list[str] = Field(min_length=4, max_length=4)
    answer: Literal["A", "B", "C", "D"]
    context: str = ""
    source_excerpt: str = ""
    verified: bool = False
    check_notes: str = ""


class ClozeItem(BaseModel):
    keep: bool = True
    category: str
    text: str
    extra: str = ""
    source_excerpt: str = ""
    verified: bool = False
    check_notes: str = ""


class ExportRequest(BaseModel):
    chapter: str = Field(max_length=200)
    day: int | None = Field(default=None, ge=0, le=999)
    mcq_deck: str = Field(default=g.DEFAULT_MCQ_DECK, max_length=200)
    cloze_deck: str = Field(default=g.DEFAULT_CLOZE_DECK, max_length=200)
    mcq: list[McqItem] = []
    cloze: list[ClozeItem] = []


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/config")
def config():
    return {
        "default_model": g.DEFAULT_MODEL,
        "mcq_counts": list(g.ALLOWED_MCQ_COUNTS),
        "cloze_max": list(g.ALLOWED_CLOZE_MAX),
        "splits": {n: g.difficulty_split(n) for n in g.ALLOWED_MCQ_COUNTS},
        "suggest": [[1200, 5], [2500, 10], [4000, 15], [None, 20]],
        "decks": {"mcq": g.DEFAULT_MCQ_DECK, "cloze": g.DEFAULT_CLOZE_DECK},
        "difficulties": DIFFICULTIES, "foci": FOCI, "cloze_categories": CLOZE_CATEGORIES,
        "server_key": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        "passcode_required": bool(os.getenv("APP_PASSCODE")),
    }


def _prepare(req: GenerateRequest) -> tuple[str, str, str]:
    source, chapter = g.prepare_source(req.text, req.chapter)
    model = (req.model or "").strip() or g.DEFAULT_MODEL
    return source, chapter, model


@app.post("/api/generate/mcq")
def generate_mcq(req: GenerateRequest,
                 x_app_passcode: str | None = Header(default=None),
                 x_openai_key: str | None = Header(default=None)):
    if req.n_mcq not in g.ALLOWED_MCQ_COUNTS:
        raise HTTPException(400, f"n_mcq must be one of {list(g.ALLOWED_MCQ_COUNTS)}")
    source, chapter, model = _prepare(req)
    client = _get_client(x_app_passcode, x_openai_key)
    items, warnings, _ = g.generate_mcqs(client, model, source, chapter, req.n_mcq)
    return {
        "items": [{
            "keep": True, "difficulty": it["difficulty"], "focus": it["focus"],
            "question": it["question"], "options": it["options"], "answer": it["answer_letter"],
            "context": it["context"], "source_excerpt": it["source_excerpt"],
            "verified": it["verified"], "check_notes": it["check_notes"],
        } for it in items],
        "warnings": warnings,
        "model": model,
    }


@app.post("/api/generate/cloze")
def generate_cloze(req: GenerateRequest,
                   x_app_passcode: str | None = Header(default=None),
                   x_openai_key: str | None = Header(default=None)):
    if req.max_cloze not in g.ALLOWED_CLOZE_MAX:
        raise HTTPException(400, f"max_cloze must be one of {list(g.ALLOWED_CLOZE_MAX)}")
    source, chapter, model = _prepare(req)
    client = _get_client(x_app_passcode, x_openai_key)
    cards, warnings, _ = g.generate_cloze(client, model, source, chapter, req.max_cloze, req.scope)
    return {"items": [{"keep": True, **c} for c in cards], "warnings": warnings, "model": model}


@app.post("/api/export")
def export(req: ExportRequest):
    chapter = " ".join(req.chapter.split()) or "Unlabeled"
    day = req.day or None
    mcq_review = pd.DataFrame([{
        "Keep": m.keep, "Difficulty": m.difficulty, "Focus": m.focus, "Question": m.question,
        "A": m.options[0], "B": m.options[1], "C": m.options[2], "D": m.options[3],
        "Answer": m.answer, "Context": m.context, "Source excerpt": m.source_excerpt,
        "Verified": m.verified, "Check notes": m.check_notes,
    } for m in req.mcq], columns=g.MCQ_REVIEW_COLUMNS)
    cloze_review = pd.DataFrame([{
        "Keep": c.keep, "Category": c.category, "Text": c.text, "Extra": c.extra,
        "Source excerpt": c.source_excerpt, "Verified": c.verified, "Check notes": c.check_notes,
    } for c in req.cloze], columns=g.CLOZE_REVIEW_COLUMNS)

    mcq_notes = g.build_mcq_notes(mcq_review, chapter, day, req.mcq_deck.strip() or g.DEFAULT_MCQ_DECK)
    cloze_notes = g.build_cloze_notes(cloze_review, chapter, day,
                                      req.cloze_deck.strip() or g.DEFAULT_CLOZE_DECK)
    stem = g.output_stem(chapter, day)
    kept_cloze = sum(c.keep for c in req.cloze)
    return {
        "files": {
            "mcq": {"name": f"{stem}_MCQ.csv", "csv": g.anki_csv(mcq_notes, "Basic"),
                    "count": len(mcq_notes)},
            "cloze": {"name": f"{stem}_Cloze.csv", "csv": g.anki_csv(cloze_notes, "Cloze"),
                      "count": len(cloze_notes)},
            "both": {"name": f"{stem}_Anki.csv", "csv": g.anki_csv_combined(mcq_notes, cloze_notes),
                     "count": len(mcq_notes) + len(cloze_notes)},
        },
        # cloze cards whose edited text no longer has valid {{c1::...}} syntax
        "dropped_cloze": kept_cloze - len(cloze_notes),
    }


# Local development only: Vercel serves public/ from its CDN and must not see this mount.
if not ON_VERCEL:
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=Path(__file__).parent / "public", html=True), name="public")
