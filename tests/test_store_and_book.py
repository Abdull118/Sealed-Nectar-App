"""
Storage backends and page-number detection.

The Upstash test runs a local HTTP server that speaks Upstash's REST format
(POST a JSON command array, get {"result": ...}) backed by fakeredis, so the
real network code in store.UpstashStore is exercised.
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import book  # noqa: E402
import store as store_mod  # noqa: E402
from synthetic_book import make_book  # noqa: E402


@pytest.fixture
def upstash():
    fakeredis = pytest.importorskip("fakeredis")
    r = fakeredis.FakeRedis(decode_responses=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            if self.headers.get("Authorization") != "Bearer secret":
                self.send_response(401); self.end_headers(); self.wfile.write(b'{"error":"Unauthorized"}'); return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            def run(cmd):
                try:
                    res = r.execute_command(*cmd)
                    if isinstance(res, set):
                        res = sorted(res)
                    if res is True:
                        res = "OK"
                    return {"result": res}
                except Exception as e:  # noqa: BLE001
                    return {"error": str(e)}
            out = [run(c) for c in body] if self.path == "/pipeline" else run(body)
            data = json.dumps(out).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _exercise(s: store_mod.Store):
    s.set_json("plan", {"day": 3, "chapter": "Ḥudaybiyah"})
    assert s.get_json("plan") == {"day": 3, "chapter": "Ḥudaybiyah"}
    s.mset_json({f"k{i}": [i, "x" * 10] for i in range(120)})
    assert s.mget_json(["k0", "k119", "missing"]) == [[0, "x" * 10], [119, "x" * 10], None]
    s.sadd("groups", "a", "b"); s.sadd("groups", "c"); s.srem("groups", "b")
    assert s.smembers("groups") == {"a", "c"}
    assert [s.incr("n"), s.incr("n")] == [1, 2]
    s.delete("plan", "k0")
    assert s.get("plan") is None and s.get("k1") is not None


def test_memory_store():
    _exercise(store_mod.MemoryStore())


def test_file_store(tmp_path):
    _exercise(store_mod.FileStore(tmp_path))


def test_upstash_store(upstash):
    _exercise(store_mod.UpstashStore(upstash, "secret"))
    with pytest.raises(store_mod.StoreError):
        store_mod.UpstashStore(upstash, "wrong").get("x")


def test_default_store_picks_upstash_from_vercel_env(monkeypatch, tmp_path):
    for k in ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN", "KV_REST_API_URL", "KV_REST_API_TOKEN", "VERCEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert store_mod.default_store()[1] == "local"
    monkeypatch.setenv("VERCEL", "1")
    assert store_mod.default_store() == (None, "missing")
    monkeypatch.setenv("KV_REST_API_URL", "https://x.upstash.io")
    monkeypatch.setenv("KV_REST_API_TOKEN", "t")
    st, kind = store_mod.default_store()
    assert kind == "upstash" and st.url == "https://x.upstash.io"


def test_detection_handles_plates_front_matter_and_noise():
    texts, truth = make_book()
    cands = [book.page_candidates(t) for t in texts]
    words = [book.word_count(t) for t in texts]
    m = book.build_map(cands, words)
    assert m["method"] == "detected" and m["printed"] == truth
    s = book.summarize(m["printed"], words)
    assert (s["first_printed"], s["last_printed"], s["picture_pages"]) == (1, 300, 5)
    idx = book.pdf_pages_for(m["printed"], 92, 98)
    assert [m["printed"][i] for i in idx] == list(range(92, 99))
    # a long stretch with no readable numbers is still filled correctly
    blank = [c if not 100 <= i <= 140 else [] for i, c in enumerate(cands)]
    assert book.build_map(blank, words)["printed"] == truth


def test_labels_used_when_meaningful():
    texts, truth = make_book(pages=60)
    labels = ["x" if p is None else str(p) for p in truth]
    m = book.build_map([[]] * len(texts), [300] * len(texts), labels)
    assert m["method"] == "labels" and m["printed"] == truth
    trivial = [str(i + 1) for i in range(len(texts))]
    assert book.numbers_from_labels(trivial, len(texts)) is None


def test_override_wins():
    texts, truth = make_book(pages=60)
    cands = [book.page_candidates(t) for t in texts]
    words = [book.word_count(t) for t in texts]
    i = truth.index(30)
    m = book.build_map(cands, words, overrides={i: 31})
    assert m["printed"][i] == 31
