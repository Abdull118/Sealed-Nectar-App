"""
Spaced repetition with FSRS (the scheduler Anki uses), via the `fsrs` package.

Each reviewable item (one MCQ, or one cloze deletion c1/c2/...) stores the
FSRS card state plus two counters. Like Anki, review cards fall due by
calendar day with the day starting at 4 a.m. local time, and learning steps
(1 and 10 minutes) can be shown up to 20 minutes early when nothing else is
left.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fsrs import Card, Rating, Scheduler, State

SCHEDULER = Scheduler(desired_retention=0.9, enable_fuzzing=False)
DAY_STARTS_AT = timedelta(hours=4)
LEARN_AHEAD = timedelta(minutes=20)
FSRS_KEYS = ("card_id", "state", "step", "stability", "difficulty", "due", "last_review")
RATINGS = {1: Rating.Again, 2: Rating.Hard, 3: Rating.Good, 4: Rating.Easy}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def tz(name: str | None):
    try:
        return ZoneInfo(name) if name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def new_item(now: datetime | None = None) -> dict:
    card = Card(due=now or utcnow())
    d = card.to_dict()
    d.update(reps=0, lapses=0)
    return d


def _card(d: dict) -> Card:
    return Card.from_dict({k: d.get(k) for k in FSRS_KEYS})


def review(d: dict, rating: int, now: datetime | None = None) -> dict:
    now = now or utcnow()
    was_review = d.get("state") == State.Review.value
    card, _ = SCHEDULER.review_card(_card(d), RATINGS[rating], now)
    out = card.to_dict()
    out["reps"] = d.get("reps", 0) + 1
    out["lapses"] = d.get("lapses", 0) + (1 if rating == 1 and was_review else 0)
    return out


def interval_label(delta: timedelta) -> str:
    s = max(0, delta.total_seconds())
    if s < 60:
        return "<1m"
    if s < 3600:
        return f"{round(s / 60)}m"
    if s < 86400:
        return f"{round(s / 3600)}h"
    days = s / 86400
    if days < 30:
        return f"{round(days)}d"
    if days < 365:
        return f"{days / 30:.1f}mo".replace(".0mo", "mo")
    return f"{days / 365:.1f}y".replace(".0y", "y")


def previews(d: dict, now: datetime | None = None) -> dict[str, str]:
    """Next interval for each rating, for the grade buttons."""
    now = now or utcnow()
    out = {}
    for r in RATINGS:
        due = datetime.fromisoformat(review(d, r, now)["due"])
        out[str(r)] = interval_label(due - now)
    return out


def status(d: dict) -> str:
    if not d.get("reps"):
        return "new"
    return "review" if d.get("state") == State.Review.value else "learning"


def study_day(moment: datetime, zone) -> datetime.date:
    return (moment.astimezone(zone) - DAY_STARTS_AT).date()


def is_due(d: dict, now: datetime, zone) -> bool:
    """New items are handled by the caller. Learning items are due within the
    learn-ahead window; review items are due on or before today's study day."""
    due = datetime.fromisoformat(d["due"])
    if status(d) == "review":
        return study_day(due, zone) <= study_day(now, zone)
    return due <= now + LEARN_AHEAD
