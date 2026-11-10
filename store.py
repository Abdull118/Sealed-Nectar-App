"""
Key-value storage for the book text, saved cards and reading plan.

- UpstashStore: Upstash Redis over its REST API. On Vercel, add it from the
  project's Storage tab (Upstash for Redis, free plan); Vercel then injects
  KV_REST_API_URL / KV_REST_API_TOKEN (or UPSTASH_REDIS_REST_URL / _TOKEN).
- FileStore: one JSON file per key under ./.data, for running locally.
- MemoryStore: for tests.

Values are strings (JSON is encoded by the helpers). Sets hold string members.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote


class StoreError(RuntimeError):
    pass


class Store:
    def get(self, key: str) -> str | None: raise NotImplementedError
    def set(self, key: str, value: str) -> None: raise NotImplementedError
    def mget(self, keys: list[str]) -> list[str | None]: raise NotImplementedError
    def mset(self, mapping: dict[str, str]) -> None: raise NotImplementedError
    def delete(self, *keys: str) -> None: raise NotImplementedError
    def sadd(self, key: str, *members: str) -> None: raise NotImplementedError
    def srem(self, key: str, *members: str) -> None: raise NotImplementedError
    def smembers(self, key: str) -> set[str]: raise NotImplementedError
    def incr(self, key: str) -> int: raise NotImplementedError

    # JSON helpers
    def get_json(self, key: str, default=None):
        v = self.get(key)
        return default if v is None else json.loads(v)

    def set_json(self, key: str, value) -> None:
        self.set(key, json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    def mget_json(self, keys: list[str]) -> list:
        return [None if v is None else json.loads(v) for v in (self.mget(keys) if keys else [])]

    def mset_json(self, mapping: dict) -> None:
        if mapping:
            self.mset({k: json.dumps(v, ensure_ascii=False, separators=(",", ":")) for k, v in mapping.items()})


class MemoryStore(Store):
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.lock = threading.Lock()

    def get(self, key): return self.kv.get(key)
    def set(self, key, value):
        with self.lock: self.kv[key] = value
    def mget(self, keys): return [self.kv.get(k) for k in keys]
    def mset(self, mapping):
        with self.lock: self.kv.update(mapping)
    def delete(self, *keys):
        with self.lock:
            for k in keys:
                self.kv.pop(k, None); self.sets.pop(k, None)
    def sadd(self, key, *members):
        with self.lock: self.sets.setdefault(key, set()).update(members)
    def srem(self, key, *members):
        with self.lock: self.sets.get(key, set()).difference_update(members)
    def smembers(self, key): return set(self.sets.get(key, set()))
    def incr(self, key):
        with self.lock:
            v = int(self.kv.get(key, "0")) + 1
            self.kv[key] = str(v)
            return v


class FileStore(Store):
    """Local development: ./.data/<key>.json. Sets are stored as JSON lists."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def _path(self, key: str) -> Path:
        return self.root / (quote(key, safe="") + ".json")

    def get(self, key):
        p = self._path(key)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def set(self, key, value):
        with self.lock:
            tmp = self._path(key).with_suffix(".tmp")
            tmp.write_text(value, encoding="utf-8")
            tmp.replace(self._path(key))

    def mget(self, keys): return [self.get(k) for k in keys]
    def mset(self, mapping):
        for k, v in mapping.items(): self.set(k, v)
    def delete(self, *keys):
        with self.lock:
            for k in keys:
                self._path(k).unlink(missing_ok=True)
    def smembers(self, key): return set(json.loads(self.get(key) or "[]"))
    def sadd(self, key, *members):
        self.set(key, json.dumps(sorted(self.smembers(key) | set(members))))
    def srem(self, key, *members):
        self.set(key, json.dumps(sorted(self.smembers(key) - set(members))))
    def incr(self, key):
        with self.lock:
            v = int(self.get(key) or "0") + 1
            self.set(key, str(v))
            return v


class UpstashStore(Store):
    """Upstash Redis REST API: POST a JSON array command, get {"result": ...}."""

    def __init__(self, url: str, token: str, timeout: float = 20):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _post(self, path: str, body):
        req = urllib.request.Request(
            self.url + path, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise StoreError(f"Upstash returned {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise StoreError(f"Could not reach Upstash: {e}") from e

    def cmd(self, *args):
        res = self._post("", [str(a) for a in args])
        if isinstance(res, dict) and "error" in res:
            raise StoreError(f"Upstash error: {res['error']}")
        return res.get("result") if isinstance(res, dict) else res

    def pipeline(self, commands: list[list]):
        if not commands:
            return []
        res = self._post("/pipeline", [[str(a) for a in c] for c in commands])
        errors = [r["error"] for r in res if isinstance(r, dict) and "error" in r]
        if errors:
            raise StoreError(f"Upstash error: {errors[0]}")
        return [r.get("result") for r in res]

    def get(self, key): return self.cmd("GET", key)
    def set(self, key, value): self.cmd("SET", key, value)
    def mget(self, keys):
        out: list = []
        for i in range(0, len(keys), 200):                 # keep each request small
            out.extend(self.cmd("MGET", *keys[i:i + 200]))
        return out
    def mset(self, mapping):
        items = list(mapping.items())
        for i in range(0, len(items), 50):
            chunk = items[i:i + 50]
            self.cmd("MSET", *[x for kv in chunk for x in kv])
    def delete(self, *keys):
        for i in range(0, len(keys), 200):
            self.cmd("DEL", *keys[i:i + 200])
    def sadd(self, key, *members):
        if members: self.cmd("SADD", key, *members)
    def srem(self, key, *members):
        if members: self.cmd("SREM", key, *members)
    def smembers(self, key): return set(self.cmd("SMEMBERS", key) or [])
    def incr(self, key): return int(self.cmd("INCR", key))


def upstash_credentials() -> tuple[str, str] | None:
    url = os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("KV_REST_API_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN") or os.getenv("KV_REST_API_TOKEN")
    return (url, token) if url and token else None


def default_store() -> tuple[Store | None, str]:
    """(store, kind). On Vercel without Upstash configured there is no store."""
    creds = upstash_credentials()
    if creds:
        return UpstashStore(*creds), "upstash"
    if os.getenv("VERCEL"):
        return None, "missing"
    return FileStore(os.getenv("DATA_DIR", Path(__file__).parent / ".data")), "local"
