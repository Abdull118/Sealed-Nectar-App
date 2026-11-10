"""
Web API tests (offline): generation, access rules, saving cards, studying,
the book, the reading plan and the Anki backup export.
Run with:  python -m pytest -q
"""

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import test_offline as t  # noqa: E402
from synthetic_book import make_book  # noqa: E402

import store as store_mod  # noqa: E402


@pytest.fixture
def make_app(monkeypatch):
    def _make(storage="memory", **env):
        for k in ("VERCEL", "APP_PASSCODE", "OPENAI_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        import app as appmod
        appmod = importlib.reload(appmod)
        keys = []

        def factory(key, timeout=None, max_retries=None):
            keys.append(key)
            return t.make_client()

        mem = store_mod.MemoryStore()
        appmod.client_factory = factory
        appmod.store_factory = (lambda: (mem, "memory")) if storage == "memory" else (lambda: (None, "missing"))
        client = TestClient(appmod.app)
        client.mem = mem
        return client, keys
    return _make


BODY = {"text": t.SAMPLE, "chapter": "The Hijrah", "n_mcq": 5, "max_cloze": 15}
PASS = {"X-App-Passcode": "olive-tree"}


def _generated(c, headers=None):
    mcq = c.post("/api/generate/mcq", json=BODY, headers=headers or {}).json()["items"]
    cloze = c.post("/api/generate/cloze", json=BODY, headers=headers or {}).json()["items"]
    return mcq, cloze


# ---- config and access -------------------------------------------------------

def test_config(make_app):
    c, _ = make_app()
    cfg = c.get("/api/config").json()
    assert cfg["mcq_counts"] == [5, 10, 15, 20]
    assert cfg["places"] == ["Makkah", "Madinah", "Hijrah route", "Other"]
    assert cfg["server_key"] is False and cfg["passcode_required"] is False and cfg["storage"] == "memory"


def test_generate_both_kinds(make_app):
    c, keys = make_app(OPENAI_API_KEY="sk-server")
    mcq, cloze = _generated(c)
    assert len(mcq) == 5 and [i["difficulty"] for i in mcq].count("Medium") == 3
    assert all(i["options"]["ABCD".index(i["answer"])] for i in mcq)
    assert {i["place"] for i in cloze} == {"Hijrah route", "Madinah"}
    assert keys == ["sk-server", "sk-server"]


def test_passcode_guards_generation_and_data(make_app):
    c, _ = make_app(OPENAI_API_KEY="sk-server", APP_PASSCODE="olive-tree")
    assert c.get("/api/config").json()["passcode_required"] is True
    assert c.post("/api/generate/mcq", json=BODY).status_code == 401
    assert c.post("/api/generate/mcq", json=BODY, headers={"X-App-Passcode": "nope"}).status_code == 401
    assert c.post("/api/generate/mcq", json=BODY, headers=PASS).status_code == 200
    for path in ("/api/state", "/api/cards", "/api/study", "/api/book/text?first=1&last=2"):
        assert c.get(path).status_code == 401, path
    assert c.get("/api/state", headers=PASS).status_code == 200


def test_vercel_rules(make_app):
    c, keys = make_app(VERCEL="1", OPENAI_API_KEY="sk-server")
    r = c.post("/api/generate/mcq", json=BODY)
    assert r.status_code == 503 and "APP_PASSCODE" in r.json()["detail"]
    assert c.get("/api/state").status_code == 503
    own = c.post("/api/generate/mcq", json=BODY, headers={"X-OpenAI-Key": "sk-mine"})
    assert own.status_code == 200 and keys == ["sk-mine"]


def test_missing_storage_message(make_app):
    c, _ = make_app(storage="none", APP_PASSCODE="olive-tree")
    r = c.get("/api/state", headers=PASS)
    assert r.status_code == 503 and "Upstash for Redis" in r.json()["detail"]


def test_bad_input_and_upstream_error(make_app):
    c, _ = make_app(OPENAI_API_KEY="sk-server")
    short = c.post("/api/generate/mcq", json={**BODY, "text": "Too short."})
    assert short.status_code == 400 and "100 words" in short.json()["detail"]
    assert c.post("/api/generate/mcq", json={**BODY, "n_mcq": 7}).status_code == 400
    import app as appmod

    def boom(*_a, **_k):
        raise RuntimeError("boom")
    appmod.client_factory = lambda *a, **k: SimpleNamespace(responses=SimpleNamespace(create=boom))
    r = c.post("/api/generate/cloze", json=BODY)
    assert r.status_code == 502 and "boom" in r.json()["detail"]


# ---- saving and studying ------------------------------------------------------

def _save(c, mcq, cloze, day=14):
    return c.post("/api/cards", json={"chapter": "The Hijrah", "day": day, "printed": [92, 98],
                                      "mcq": mcq, "cloze": cloze}).json()


def test_save_skips_unkept_invalid_and_duplicates(make_app):
    c, _ = make_app(OPENAI_API_KEY="sk-server")
    mcq, cloze = _generated(c)
    mcq[0]["keep"] = False
    cloze[1]["text"] = "Edited into something with no deletion."
    r = _save(c, mcq, cloze)
    assert (r["saved_notes"], r["invalid"], r["duplicates"]) == (4 + 3, 1, 0)
    assert r["saved_cards"] == 4 + (2 + 1 + 2)            # each cloze deletion is its own card
    again = _save(c, mcq, cloze, day=15)
    assert again["saved_notes"] == 0 and again["duplicates"] == 7
    groups = c.get("/api/cards").json()["groups"]
    assert len(groups) == 1 and groups[0]["printed"] == [92, 98]


def test_study_queue_review_and_filters(make_app):
    c, _ = make_app(OPENAI_API_KEY="sk-server")
    mcq, cloze = _generated(c)
    _save(c, mcq, cloze)
    q = c.get("/api/study?tz=America/New_York").json()
    assert len(q["items"]) == 12 and q["counts"]["all"] == {"due": 0, "new": 12}
    assert q["counts"]["Madinah"]["new"] == 2                  # Quba card: c1 and c2
    first = q["items"][0]
    assert first["status"] == "new" and set(first["previews"]) == {"1", "2", "3", "4"}

    good = c.post("/api/study/review", json={"note_id": first["note_id"], "item": first["item"],
                                             "rating": 3, "tz": "America/New_York"}).json()
    assert good["status"] == "learning"                        # 10-minute step: still shown this session
    q2 = c.get("/api/study?tz=America/New_York").json()
    assert len(q2["items"]) == 12 and q2["items"][0]["status"] == "learning"
    assert q2["reviewed_today"] == 1 and q2["new_today"] == 1

    easy = c.post("/api/study/review", json={"note_id": first["note_id"], "item": first["item"],
                                             "rating": 4}).json()
    assert easy["status"] == "review"
    q3 = c.get("/api/study").json()
    assert len(q3["items"]) == 11 and q3["next_due"]

    assert len(c.get("/api/study?place=Madinah").json()["items"]) == 2
    assert len(c.get("/api/study?new_limit=3").json()["items"]) == 2   # 3 a day, 1 already started


def test_edit_suspend_delete(make_app):
    c, _ = make_app(OPENAI_API_KEY="sk-server")
    mcq, cloze = _generated(c)
    _save(c, mcq, cloze)
    notes = c.get("/api/cards").json()["groups"][0]["notes"]
    m = next(n for n in notes if n["type"] == "mcq")
    z = next(n for n in notes if n["type"] == "cloze" and n["cards"] == 2)
    assert c.patch(f"/api/cards/{m['id']}", json={"question": "Edited?"}).json()["question"] == "Edited?"
    assert c.patch(f"/api/cards/{z['id']}", json={"text": "no deletion here"}).status_code == 400
    r = c.patch(f"/api/cards/{z['id']}", json={"text": "They hid in the {{c1::cave}} for three nights."})
    assert r.status_code == 200
    assert next(n for n in c.get("/api/cards").json()["groups"][0]["notes"] if n["id"] == z["id"])["cards"] == 1
    c.patch(f"/api/cards/{m['id']}", json={"suspended": True})
    assert all(i["note_id"] != m["id"] for i in c.get("/api/study").json()["items"])
    assert c.delete(f"/api/cards/{m['id']}").status_code == 200
    assert c.delete(f"/api/cards/{m['id']}").status_code == 404


def test_plan_round_trip(make_app):
    c, _ = make_app()
    assert c.get("/api/state").json() == {"plan": None, "book": None}
    plan = {"day": 3, "chapter": "The Pledges", "first": 92, "last": 98, "mode": "book", "saved_on": None}
    assert c.put("/api/plan", json=plan).status_code == 200
    assert c.get("/api/state").json()["plan"] == plan


# ---- book --------------------------------------------------------------------

def _upload(c, texts, labels=None):
    ver = c.post("/api/book/begin", json={"name": "Sealed Nectar.pdf", "pages": len(texts),
                                           "labels": labels}).json()["version"]
    for start in range(0, len(texts), 25):
        r = c.post("/api/book/pages", json={"version": ver, "start": start, "texts": texts[start:start + 25]})
        assert r.status_code == 200, r.text
    return c.post("/api/book/finish", json={"version": ver}).json()


def test_book_upload_maps_printed_pages(make_app):
    c, _ = make_app()
    texts, truth = make_book()
    s = _upload(c, texts)
    assert s["method"] == "detected" and s["first_printed"] == 1 and s["picture_pages"] == 5
    r = c.get("/api/book/text?first=38&last=42").json()
    assert [p["printed"] for p in r["pages"]] == [38, 39, 40, None, None, 41, 42]
    assert r["pages"][3]["picture"] and "Cave of Thawr" in r["pages"][3]["text"]
    assert "The Sealed Nectar" not in r["pages"][0]["text"]          # running header stripped
    assert c.get("/api/book/locate?printed=41").json()["pdf"] == truth.index(41) + 1
    assert c.get("/api/book/text?first=900&last=901").status_code == 400
    assert c.get("/api/state").json()["book"]["name"] == "Sealed Nectar.pdf"


def test_book_manual_fix(make_app):
    c, _ = make_app()
    texts, truth = make_book()
    _upload(c, texts)
    pdf_of_100 = truth.index(100) + 1
    s = c.post("/api/book/fix", json={"pdf_page": pdf_of_100, "printed": 101}).json()
    assert s["fixes"] == 1
    assert c.get("/api/book/locate?printed=101").json()["pdf"] == pdf_of_100


def test_book_upload_rejects_gaps(make_app):
    c, _ = make_app()
    ver = c.post("/api/book/begin", json={"name": "x.pdf", "pages": 60}).json()["version"]
    c.post("/api/book/pages", json={"version": ver, "start": 0, "texts": ["a"] * 25})
    r = c.post("/api/book/finish", json={"version": ver})
    assert r.status_code == 400 and "never arrived" in r.json()["detail"]
    assert c.post("/api/book/pages", json={"version": ver, "start": 3, "texts": ["a"]}).status_code == 400


# ---- export and static ---------------------------------------------------------

def test_export_imports_into_real_anki(make_app, tmp_path):
    anki = pytest.importorskip("anki.collection")
    c, _ = make_app(OPENAI_API_KEY="sk-server")
    mcq, cloze = _generated(c)
    _save(c, mcq, cloze)
    exp = c.get("/api/export/anki").json()
    assert exp["count"] == 9
    path = tmp_path / exp["name"]
    path.write_text(exp["csv"], encoding="utf-8")
    col = anki.Collection(str(tmp_path / "c.anki2"))
    try:
        meta = col.get_csv_metadata(str(path), None)
        assert len(col.import_csv(anki.ImportCsvRequest(path=str(path), metadata=meta)).log.new) == 9
        z = col.get_note(col.find_notes('"deck:Sealed Nectar::Hijrah Cloze"')[0])
        assert z.note_type()["name"] == "Cloze" and any(t.startswith("Place::") for t in z.tags)
    finally:
        col.close()


def test_local_static_page(make_app):
    c, _ = make_app()
    r = c.get("/")
    assert r.status_code == 200 and "Sealed Nectar" in r.text
    assert c.get("/vendor/pdfjs/pdf.min.mjs").status_code == 200


def test_no_static_mount_on_vercel(make_app):
    c, _ = make_app(VERCEL="1")
    assert c.get("/").status_code == 404
