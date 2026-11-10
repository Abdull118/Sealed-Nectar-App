"""
Saved cards and study sessions.

Cards are saved in groups, one group per generated reading:
  deck:groups            set of group ids
  deck:group:<id>        {"id", "chapter", "day", "printed": [first, last], "created_at", "notes": [...]}

A note is one MCQ or one cloze sentence. Its "items" hold the FSRS state of each
reviewable card: {"q": ...} for an MCQ, {"c1": ..., "c2": ...} for a cloze
sentence, so each deletion is studied separately, as in Anki.
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime

import pandas as pd

import generator as g
import srs
from prompts import CLOZE_CATEGORIES, DIFFICULTIES, FOCI, PLACES

GROUPS = "deck:groups"
STUDY_PLACES = ["all", *PLACES]


def gkey(gid: str) -> str:
    return f"deck:group:{gid}"


def _now() -> datetime:
    return srs.utcnow()


def load_groups(store) -> list[dict]:
    ids = sorted(store.smembers(GROUPS))
    groups = [grp for grp in store.mget_json([gkey(i) for i in ids]) if grp]
    groups.sort(key=lambda grp: (grp.get("created_at", ""), grp["id"]))
    return groups


def _note_key(note: dict) -> str:
    return g._norm(note["question"] if note["type"] == "mcq" else g.cloze_plain(note["text"]))


def cloze_numbers(text: str) -> list[int]:
    return sorted({int(m.group(1)) for m in g.CLOZE_RE.finditer(text)})


def _clean_place(p) -> str:
    return p if p in PLACES else "Other"


def _mcq_note(nid: str, m: dict, now) -> dict:
    return {
        "id": nid, "type": "mcq", "place": _clean_place(m.get("place")),
        "difficulty": m["difficulty"] if m.get("difficulty") in DIFFICULTIES else "Medium",
        "focus": m["focus"] if m.get("focus") in FOCI else "Key Event",
        "question": g._clean_str(m["question"]),
        "options": [g._clean_str(o) for o in m["options"]][:4],
        "answer": m["answer"], "context": g._clean_str(m.get("context")),
        "source_excerpt": g._clean_str(m.get("source_excerpt")),
        "verified": bool(m.get("verified")), "check_notes": m.get("check_notes", ""),
        "suspended": False, "items": {"q": srs.new_item(now)},
    }


def _cloze_note(nid: str, c: dict, now) -> dict | None:
    fixed, _, _ = g.check_cloze(c.get("text", ""))
    if fixed is None:
        return None
    return {
        "id": nid, "type": "cloze", "place": _clean_place(c.get("place")),
        "category": c["category"] if c.get("category") in CLOZE_CATEGORIES else "Event",
        "text": fixed, "extra": g._clean_str(c.get("extra")),
        "source_excerpt": g._clean_str(c.get("source_excerpt")),
        "verified": bool(c.get("verified")), "check_notes": c.get("check_notes", ""),
        "suspended": False, "items": {f"c{n}": srs.new_item(now) for n in cloze_numbers(fixed)},
    }


def save_group(store, chapter: str, day: int | None, printed: list | None,
               mcq: list[dict], cloze: list[dict], now: datetime | None = None) -> dict:
    """Save the kept cards of one reading. Cards already in the deck are skipped."""
    now = now or _now()
    existing = {_note_key(n) for grp in load_groups(store) for n in grp["notes"]}
    gid = f"{int(time.time()):x}{secrets.token_hex(2)}"
    notes, invalid, dupes = [], 0, 0
    for m in (x for x in mcq if x.get("keep", True)):
        notes.append(_mcq_note(f"{gid}.{len(notes)}", m, now))
    for c in (x for x in cloze if x.get("keep", True)):
        note = _cloze_note(f"{gid}.{len(notes)}", c, now)
        if note is None:
            invalid += 1
        else:
            notes.append(note)
    fresh = []
    for n in notes:
        k = _note_key(n)
        if k in existing:
            dupes += 1
        else:
            existing.add(k)
            fresh.append(n)
    if fresh:
        group = {"id": gid, "chapter": chapter, "day": day, "printed": printed,
                 "created_at": now.isoformat(), "notes": fresh}
        store.set_json(gkey(gid), group)
        store.sadd(GROUPS, gid)
    cards = sum(len(n["items"]) for n in fresh)
    return {"saved_notes": len(fresh), "saved_cards": cards, "duplicates": dupes,
            "invalid": invalid, "group_id": gid if fresh else None}


def _locate(store, note_id: str) -> tuple[dict, dict]:
    gid = note_id.split(".")[0]
    group = store.get_json(gkey(gid))
    if group:
        for note in group["notes"]:
            if note["id"] == note_id:
                return group, note
    raise KeyError(note_id)


def update_note(store, note_id: str, changes: dict) -> dict:
    group, note = _locate(store, note_id)
    if "suspended" in changes:
        note["suspended"] = bool(changes["suspended"])
    if "place" in changes:
        note["place"] = _clean_place(changes["place"])
    if note["type"] == "mcq":
        for f in ("question", "context"):
            if f in changes:
                note[f] = g._clean_str(changes[f])
        if "options" in changes and len(changes["options"]) == 4:
            note["options"] = [g._clean_str(o) for o in changes["options"]]
        if changes.get("answer") in ("A", "B", "C", "D"):
            note["answer"] = changes["answer"]
    else:
        if "extra" in changes:
            note["extra"] = g._clean_str(changes["extra"])
        if "text" in changes:
            fixed, _, _ = g.check_cloze(changes["text"])
            if fixed is None:
                raise ValueError("Keep at least one deletion written as {{c1::answer}}.")
            note["text"] = fixed
            wanted = {f"c{n}" for n in cloze_numbers(fixed)}
            for k in list(note["items"]):
                if k not in wanted:
                    del note["items"][k]
            for k in wanted - set(note["items"]):
                note["items"][k] = srs.new_item()
    store.set_json(gkey(group["id"]), group)
    return note


def delete_note(store, note_id: str) -> None:
    group, note = _locate(store, note_id)
    group["notes"] = [n for n in group["notes"] if n["id"] != note_id]
    if group["notes"]:
        store.set_json(gkey(group["id"]), group)
    else:
        store.delete(gkey(group["id"]))
        store.srem(GROUPS, group["id"])


def _public_note(note: dict, now: datetime, zone) -> dict:
    out = {k: v for k, v in note.items() if k != "items"}
    statuses = [srs.status(it) for it in note["items"].values()]
    out["cards"] = len(statuses)
    out["new"] = statuses.count("new")
    dues = [it["due"] for it in note["items"].values() if srs.status(it) != "new"]
    out["next_due"] = min(dues) if dues else None
    return out


def list_cards(store, tz_name: str | None = None) -> list[dict]:
    now, zone = _now(), srs.tz(tz_name)
    return [{**{k: v for k, v in grp.items() if k != "notes"},
             "notes": [_public_note(n, now, zone) for n in grp["notes"]]}
            for grp in load_groups(store)]


def _study_item(group: dict, note: dict, key: str, item: dict, now: datetime) -> dict:
    base = {
        "note_id": note["id"], "item": key, "type": note["type"], "place": note["place"],
        "chapter": group.get("chapter"), "day": group.get("day"),
        "status": srs.status(item), "due": item["due"],
        "previews": srs.previews(item, now), "source_excerpt": note.get("source_excerpt", ""),
        "verified": note.get("verified", True),
    }
    if note["type"] == "mcq":
        base.update(question=note["question"], options=note["options"], answer=note["answer"],
                    context=note.get("context", ""), difficulty=note.get("difficulty"))
    else:
        base.update(text=note["text"], cloze=int(key[1:]), extra=note.get("extra", ""),
                    category=note.get("category"))
    return base


def _stats_keys(now: datetime, zone) -> tuple[str, str]:
    day = srs.study_day(now, zone).isoformat()
    return f"stats:reviews:{day}", f"stats:new:{day}"


def study_queue(store, place: str = "all", tz_name: str | None = None,
                new_limit: int | None = None, now: datetime | None = None) -> dict:
    """Everything due now: learning steps first, then reviews, then new cards."""
    now, zone = now or _now(), srs.tz(tz_name)
    place = place if place in STUDY_PLACES else "all"
    learning, reviews, new = [], [], []
    counts = {p: {"due": 0, "new": 0} for p in STUDY_PLACES}
    next_due = None
    for group in load_groups(store):
        for note in group["notes"]:
            if note.get("suspended"):
                continue
            for key, item in note["items"].items():
                st = srs.status(item)
                bucket = None
                if st == "new":
                    bucket = new
                elif srs.is_due(item, now, zone):
                    bucket = learning if st == "learning" else reviews
                else:
                    next_due = min(next_due or item["due"], item["due"])
                if bucket is None:
                    continue
                kind = "new" if bucket is new else "due"
                counts["all"][kind] += 1
                counts[note["place"]][kind] += 1
                if place in ("all", note["place"]):
                    bucket.append((group, note, key, item))

    reviews_key, new_key = _stats_keys(now, zone)
    reviewed, new_today = (int(x or 0) for x in store.mget([reviews_key, new_key]))
    if new_limit:
        new = new[: max(0, new_limit - new_today)]
    learning.sort(key=lambda e: e[3]["due"])
    reviews.sort(key=lambda e: e[3]["due"])
    queue = [_study_item(*e, now) for e in learning + reviews + new]
    return {"items": queue, "counts": counts, "next_due": next_due,
            "reviewed_today": reviewed, "new_today": new_today}


def review(store, note_id: str, key: str, rating: int, tz_name: str | None = None,
           now: datetime | None = None) -> dict:
    if rating not in srs.RATINGS:
        raise ValueError("rating must be 1 (Again), 2 (Hard), 3 (Good) or 4 (Easy)")
    now, zone = now or _now(), srs.tz(tz_name)
    group, note = _locate(store, note_id)
    item = note["items"][key]
    was_new = srs.status(item) == "new"
    note["items"][key] = srs.review(item, rating, now)
    store.set_json(gkey(group["id"]), group)
    reviews_key, new_key = _stats_keys(now, zone)
    store.incr(reviews_key)
    if was_new:
        store.incr(new_key)
    return _study_item(group, note, key, note["items"][key], now)


def export_csv(store, mcq_deck: str, cloze_deck: str) -> tuple[str, int]:
    """Every active card as one Anki CSV (backup / move to Anki desktop)."""
    mcq_parts, cloze_parts = [], []
    for group in load_groups(store):
        chapter, day = group.get("chapter") or "Unlabeled", group.get("day")
        notes = [n for n in group["notes"] if not n.get("suspended")]
        mcq = pd.DataFrame([{
            "Keep": True, "Place": n["place"], "Difficulty": n["difficulty"], "Focus": n["focus"],
            "Question": n["question"], "A": n["options"][0], "B": n["options"][1],
            "C": n["options"][2], "D": n["options"][3], "Answer": n["answer"],
            "Context": n.get("context", ""), "Source excerpt": n.get("source_excerpt", ""),
            "Verified": n.get("verified", True), "Check notes": n.get("check_notes", ""),
        } for n in notes if n["type"] == "mcq"], columns=g.MCQ_REVIEW_COLUMNS)
        cloze = pd.DataFrame([{
            "Keep": True, "Place": n["place"], "Category": n["category"], "Text": n["text"],
            "Extra": n.get("extra", ""), "Source excerpt": n.get("source_excerpt", ""),
            "Verified": n.get("verified", True), "Check notes": n.get("check_notes", ""),
        } for n in notes if n["type"] == "cloze"], columns=g.CLOZE_REVIEW_COLUMNS)
        mcq_parts.append(g.build_mcq_notes(mcq, chapter, day, mcq_deck))
        cloze_parts.append(g.build_cloze_notes(cloze, chapter, day, cloze_deck))
    mcq_all = pd.concat(mcq_parts, ignore_index=True) if mcq_parts else g.build_mcq_notes(
        pd.DataFrame(columns=g.MCQ_REVIEW_COLUMNS), "x")
    cloze_all = pd.concat(cloze_parts, ignore_index=True) if cloze_parts else g.build_cloze_notes(
        pd.DataFrame(columns=g.CLOZE_REVIEW_COLUMNS), "x")
    return g.anki_csv_combined(mcq_all, cloze_all), len(mcq_all) + len(cloze_all)
