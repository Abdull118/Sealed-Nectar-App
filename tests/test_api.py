"""
Web API tests (offline). Run with:  python -m pytest -q
Uses the same fake OpenAI client as test_offline.py.
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


@pytest.fixture
def make_app(monkeypatch):
    def _make(**env):
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

        appmod.client_factory = factory
        return TestClient(appmod.app), keys
    return _make


BODY = {"text": t.SAMPLE, "chapter": "The Hijrah", "n_mcq": 5, "max_cloze": 15, "scope": "strict"}


def test_config(make_app):
    c, _ = make_app()
    cfg = c.get("/api/config").json()
    assert cfg["mcq_counts"] == [5, 10, 15, 20]
    assert cfg["splits"]["15"] == {"Easy": 4, "Medium": 7, "Hard": 4}
    assert cfg["server_key"] is False and cfg["passcode_required"] is False


def test_generate_both_kinds(make_app):
    c, keys = make_app(OPENAI_API_KEY="sk-server")
    mcq = c.post("/api/generate/mcq", json=BODY)
    assert mcq.status_code == 200, mcq.text
    items = mcq.json()["items"]
    assert len(items) == 5 and [i["difficulty"] for i in items].count("Medium") == 3
    assert all(i["options"]["ABCD".index(i["answer"])] for i in items)
    assert sum(not i["verified"] for i in items) == 1
    cloze = c.post("/api/generate/cloze", json=BODY)
    assert cloze.status_code == 200 and len(cloze.json()["items"]) == 4
    assert keys == ["sk-server", "sk-server"]


def test_passcode(make_app):
    c, _ = make_app(OPENAI_API_KEY="sk-server", APP_PASSCODE="olive-tree")
    assert c.get("/api/config").json()["passcode_required"] is True
    assert c.post("/api/generate/mcq", json=BODY).status_code == 401
    assert c.post("/api/generate/mcq", json=BODY, headers={"X-App-Passcode": "nope"}).status_code == 401
    ok = c.post("/api/generate/mcq", json=BODY, headers={"X-App-Passcode": "olive-tree"})
    assert ok.status_code == 200


def test_vercel_refuses_server_key_without_passcode(make_app):
    c, keys = make_app(VERCEL="1", OPENAI_API_KEY="sk-server")
    r = c.post("/api/generate/mcq", json=BODY)
    assert r.status_code == 503 and "APP_PASSCODE" in r.json()["detail"]
    own = c.post("/api/generate/mcq", json=BODY, headers={"X-OpenAI-Key": "sk-mine"})
    assert own.status_code == 200 and keys == ["sk-mine"]


def test_no_key_and_bad_input(make_app):
    c, _ = make_app()
    assert c.post("/api/generate/mcq", json=BODY).status_code == 400
    c, _ = make_app(OPENAI_API_KEY="sk-server")
    short = c.post("/api/generate/mcq", json={**BODY, "text": "Too short."})
    assert short.status_code == 400 and "100 words" in short.json()["detail"]
    assert c.post("/api/generate/mcq", json={**BODY, "n_mcq": 7}).status_code == 400
    assert c.post("/api/generate/mcq", json={**BODY, "chapter": " "}).status_code == 400


def test_upstream_error_is_502(make_app):
    c, _ = make_app(OPENAI_API_KEY="sk-server")
    import app as appmod

    def boom(*_a, **_k):
        raise RuntimeError("boom")
    appmod.client_factory = lambda *a, **k: SimpleNamespace(responses=SimpleNamespace(create=boom))
    r = c.post("/api/generate/cloze", json=BODY)
    assert r.status_code == 502 and "boom" in r.json()["detail"]


def _generated(c):
    mcq = c.post("/api/generate/mcq", json=BODY).json()["items"]
    cloze = c.post("/api/generate/cloze", json=BODY).json()["items"]
    return mcq, cloze


def test_export(make_app):
    c, _ = make_app(OPENAI_API_KEY="sk-server")
    mcq, cloze = _generated(c)
    mcq[0]["keep"] = False
    cloze[1]["text"] = "Edited into something with no deletion."
    r = c.post("/api/export", json={"chapter": "The Hijrah", "day": 14, "mcq": mcq, "cloze": cloze})
    assert r.status_code == 200, r.text
    d = r.json()
    f = d["files"]
    assert (f["mcq"]["count"], f["cloze"]["count"], f["both"]["count"]) == (4, 3, 7)
    assert d["dropped_cloze"] == 1
    assert f["mcq"]["name"] == "Day014_The_Hijrah_MCQ.csv"
    assert f["both"]["csv"].startswith("#separator:Comma\n#html:true\n#columns:Note type,Deck,Tags,Field 1,Field 2")


def test_combined_file_imports_into_real_anki(make_app, tmp_path):
    anki = pytest.importorskip("anki.collection")
    c, _ = make_app(OPENAI_API_KEY="sk-server")
    mcq, cloze = _generated(c)
    both = c.post("/api/export", json={"chapter": "The Hijrah", "day": 14, "mcq": mcq, "cloze": cloze}).json()["files"]["both"]
    path = tmp_path / both["name"]
    path.write_text(both["csv"], encoding="utf-8")
    col = anki.Collection(str(tmp_path / "c.anki2"))
    try:
        meta = col.get_csv_metadata(str(path), None)
        log = col.import_csv(anki.ImportCsvRequest(path=str(path), metadata=meta)).log
        assert len(log.new) == 9
        basic = col.find_notes('"deck:Sealed Nectar::MCQ"')
        clz = col.find_notes('"deck:Sealed Nectar::Hijrah Cloze"')
        assert len(basic) == 5 and len(clz) == 4
        b = col.get_note(basic[0])
        assert b.note_type()["name"] == "Basic" and "<b>A.</b>" in b["Front"] and "Source:" in b["Back"]
        z = col.get_note(clz[0])
        assert z.note_type()["name"] == "Cloze" and "{{c1::" in z["Text"] and "Hijrah_Trek" in z.tags
    finally:
        col.close()


def test_local_static_page(make_app):
    c, _ = make_app()
    r = c.get("/")
    assert r.status_code == 200 and "Sealed Nectar study cards" in r.text
    assert c.get("/manifest.webmanifest").status_code == 200
    assert c.get("/vendor/pdfjs/pdf.min.mjs").status_code == 200


def test_no_static_mount_on_vercel(make_app):
    c, _ = make_app(VERCEL="1")
    assert c.get("/").status_code == 404           # Vercel's CDN serves public/ instead
