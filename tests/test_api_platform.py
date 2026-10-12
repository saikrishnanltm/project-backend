"""
Integration tests for the API platform features: per-key rate limits and
monthly caps, per-key usage, starter credits, collections, uploads.

These need a real Postgres + Redis (the SQL and the Redis counters are the
point), so they are skipped unless TEST_DATABASE_URL is set, e.g.:

    TEST_DATABASE_URL=postgresql://cs:cs@localhost/cs \\
    TEST_REDIS_URL=redis://localhost:6379/0 pytest tests/test_api_platform.py

The heavy ML pieces (Chroma, sentence-transformers, LangGraph/Groq) are
stubbed, so no models or API keys are needed.
"""
import os
import sys
import types
import uuid

import pytest

DB = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB, reason="set TEST_DATABASE_URL to run")

sys.path.insert(0, ".")


class FakeChroma:
    """Just enough of a Chroma collection for the indexer: where-filters
    ($and / equality / $in), upsert, get, delete."""

    def __init__(self):
        self.rows = {}

    @staticmethod
    def _match(meta, where):
        if not where:
            return True
        if "$and" in where:
            return all(FakeChroma._match(meta, w) for w in where["$and"])
        (k, v), = where.items()
        if isinstance(v, dict) and "$in" in v:
            return meta.get(k) in v["$in"]
        return meta.get(k) == v

    def upsert(self, ids, documents, metadatas):
        for i, d, m in zip(ids, documents, metadatas):
            self.rows[i] = (d, m)

    def get(self, where=None, ids=None):
        sel = [i for i, (_, m) in self.rows.items()
               if (ids is None or i in ids) and self._match(m, where)]
        return {"ids": sel, "documents": [self.rows[i][0] for i in sel],
                "metadatas": [self.rows[i][1] for i in sel]}

    def delete(self, where=None, ids=None):
        for i in self.get(where=where, ids=ids)["ids"]:
            del self.rows[i]


@pytest.fixture(scope="module")
def env():
    os.environ["DATABASE_URL"] = DB
    os.environ["REDIS_URL"] = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/0")
    os.environ.setdefault("JWT_SECRET_KEY", "test-secret-test-secret-test-secret")
    os.environ["STARTER_CREDITS"] = "100"
    os.environ["DEFAULT_KEY_RATE_LIMIT_PER_MIN"] = "60"

    # --- stub the heavy imports
    for name in ("chromadb", "chromadb.utils", "chromadb.utils.embedding_functions"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["chromadb"].ClientAPI = object
    sys.modules["chromadb.utils"].embedding_functions = sys.modules["chromadb.utils.embedding_functions"]
    graph = types.ModuleType("app.graph")
    calls = []

    def ask(q, repo_filter=None, user_id=None, history=None, collection=None):
        calls.append({"user_id": user_id, "collection": collection})
        return {"answer": "stub answer", "citations": [], "confidence": "high"}

    graph.ask = ask
    graph.ask_stream = lambda *a, **k: iter(())
    sys.modules["app.graph"] = graph

    from fastapi.testclient import TestClient
    from app import db, main
    from app.indexer import RepoIndexer

    fake = FakeChroma()
    idx = RepoIndexer.__new__(RepoIndexer)
    idx.collection = fake
    idx.chroma_client = types.SimpleNamespace(get_max_batch_size=lambda: 100)
    main._indexer = idx
    db.init_db()
    from app import auth
    return types.SimpleNamespace(client=TestClient(main.app), db=db, auth=auth, fake=fake, calls=calls, main=main)


def make_user(env, verified=True):
    uid = uuid.uuid4().hex
    with env.db.get_cursor(dict_rows=False) as cur:
        cur.execute(
            "INSERT INTO users (id, email, email_verified, created_at) VALUES (%s,%s,%s,0)",
            (uid, f"{uid}@example.com", verified),
        )
    return uid, {"Authorization": f"Bearer {env.auth.create_access_token(uid)}"}


def new_key(env, headers, **body):
    r = env.client.post("/developers/api-keys", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["api_key"]


def balance(env, headers):
    return env.client.get("/developers/usage", headers=headers).json()["credit_balance"]


def test_starter_credits_only_after_verification_and_only_once(env):
    uid, h = make_user(env, verified=False)
    assert balance(env, h) == 0
    with env.db.get_cursor(dict_rows=False) as cur:
        cur.execute("UPDATE users SET email_verified = TRUE WHERE id = %s", (uid,))
    assert balance(env, h) == 100
    assert balance(env, h) == 100  # not granted twice
    tx = env.client.get("/developers/usage", headers=h).json()["recent_transactions"]
    assert [t["reason"] for t in tx] == ["starter_credits"]


def test_rate_limit_per_key(env):
    _, h = make_user(env)
    key = new_key(env, h, label="limited", rate_limit_per_min=3)
    kh = {"CODEBASE12-Key": key}
    for _ in range(3):
        assert env.client.get("/repos", headers=kh).status_code == 200
    r = env.client.get("/repos", headers=kh)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    # another key on the same account has its own budget
    other = new_key(env, h, rate_limit_per_min=3)
    assert env.client.get("/repos", headers={"CODEBASE12-Key": other}).status_code == 200


def test_monthly_cap_usage_and_last_used(env):
    _, h = make_user(env)
    key = new_key(env, h, label="capped", rate_limit_per_min=0, monthly_credit_cap=2)
    kh = {"CODEBASE12-Key": key}
    for _ in range(2):
        assert env.client.post("/query", json={"question": "hi"}, headers=kh).status_code == 200
    r = env.client.post("/query", json={"question": "hi"}, headers=kh)
    assert r.status_code == 403 and "cap" in r.text.lower()
    assert balance(env, h) == 98  # the refused call cost nothing

    k = [x for x in env.client.get("/developers/api-keys", headers=h).json() if x["label"] == "capped"][0]
    assert k["requests_this_month"] == 2 and k["credits_this_month"] == 2
    assert k["effective_monthly_credit_cap"] == 2 and k["last_used_at"] is not None

    # raising the cap takes effect immediately
    r = env.client.patch(f"/developers/api-keys/{k['id']}", json={"monthly_credit_cap": 10}, headers=h)
    assert r.status_code == 204
    assert env.client.post("/query", json={"question": "hi"}, headers=kh).status_code == 200


def test_insufficient_credits_does_not_consume_cap(env):
    uid, h = make_user(env)
    key = new_key(env, h, rate_limit_per_min=0, monthly_credit_cap=5)
    balance(env, h)
    with env.db.get_cursor(dict_rows=False) as cur:
        cur.execute("UPDATE api_customers SET credit_balance = 0 WHERE id = %s", (uid,))
    r = env.client.post("/query", json={"question": "hi"}, headers={"CODEBASE12-Key": key})
    assert r.status_code == 402
    k = env.client.get("/developers/api-keys", headers=h).json()[0]
    assert k["requests_this_month"] == 0 and k["credits_this_month"] == 0


def test_collections_upload_and_query(env):
    uid, h = make_user(env)
    key = new_key(env, h, rate_limit_per_min=0)
    kh = {"CODEBASE12-Key": key}

    assert env.client.post("/collections", json={"name": "handbook"}, headers=kh).status_code == 201
    assert env.client.post("/collections", json={"name": "handbook"}, headers=kh).status_code == 409
    assert env.client.post("/collections", json={"name": "bad name!"}, headers=kh).status_code == 400

    md = b"# Guide\n\nHello world.\n\n## Setup\n\nInstall the thing and run it.\n"
    r = env.client.post(
        "/upload", headers=kh, data={"collection": "handbook"},
        files=[("files", ("guide.md", md, "text/markdown")), ("files", ("notes.txt", b"Some plain notes about deploys.", "text/plain"))],
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source"] == "uploads" and body["files_indexed"] == 2 and body["chunks_indexed"] >= 3
    assert body["credits_charged"] == 1 and body["collection"] == "handbook"

    d = env.client.get("/collections/handbook", headers=kh).json()
    assert d["sources"] == 1 and d["chunks"] == body["chunks_indexed"]
    assert any(c["name"] == "handbook" for c in env.client.get("/collections", headers=kh).json())
    repos = env.client.get("/repos", headers=kh).json()["repos"]
    assert repos[0]["repo"] == "uploads" and repos[0]["collection"] == "handbook"

    # querying inside the collection passes the filter through; unknown one 404s
    env.calls.clear()
    assert env.client.post("/query", json={"question": "x", "collection": "handbook"}, headers=kh).status_code == 200
    assert env.calls[-1]["collection"] == "handbook"
    assert env.client.post("/query", json={"question": "x", "collection": "nope"}, headers=kh).status_code == 404

    # re-upload under the same name replaces, doesn't duplicate, and keeps the collection
    r = env.client.post("/upload", headers=kh, files=[("files", ("a.md", b"# A\n\nOnly this now.\n", "text/markdown"))],
                        data={"source_name": "uploads"})
    assert r.status_code == 201
    d = env.client.get("/collections/handbook", headers=kh).json()
    assert d["chunks"] == r.json()["chunks_indexed"]

    # delete guard
    assert env.client.delete("/collections/handbook", headers=kh).status_code == 409
    r = env.client.delete("/collections/handbook?delete_contents=true", headers=kh)
    assert r.status_code == 200 and r.json()["chunks_deleted"] >= 1
    assert env.client.get("/collections/handbook", headers=kh).status_code == 404
    assert env.client.get("/repos", headers=kh).json()["repos"] == []


def test_upload_rejects_bad_files_without_touching_index(env):
    _, h = make_user(env)
    kh = {"CODEBASE12-Key": new_key(env, h, rate_limit_per_min=0)}
    ok = ("files", ("keep.md", b"# Keep\n\nThis stays.\n", "text/markdown"))
    assert env.client.post("/upload", headers=kh, files=[ok]).status_code == 201
    bad = ("files", ("x.exe", b"MZ\x00\x00", "application/octet-stream"))
    r = env.client.post("/upload", headers=kh, files=[ok, bad], data={"source_name": "keep.md"})
    assert r.status_code == 400
    assert env.client.get("/repos", headers=kh).json()["repos"][0]["chunks_indexed"] >= 1  # old source intact
    big = ("files", ("big.txt", b"a" * (6 * 1024 * 1024), "text/plain"))
    assert env.client.post("/upload", headers=kh, files=[big]).status_code == 413


def test_two_accounts_uploading_the_same_file_dont_clobber_each_other(env):
    _, h1 = make_user(env)
    _, h2 = make_user(env)
    k1, k2 = ({"CODEBASE12-Key": new_key(env, h, rate_limit_per_min=0)} for h in (h1, h2))
    f = ("files", ("README.md", b"# Same\n\nIdentical content in both accounts.\n", "text/markdown"))
    assert env.client.post("/upload", headers=k1, files=[f]).status_code == 201
    assert env.client.post("/upload", headers=k2, files=[f]).status_code == 201
    for k in (k1, k2):
        assert env.client.get("/repos", headers=k).json()["repos"][0]["chunks_indexed"] == 1


def test_ingest_idempotency_key_returns_same_job(env):
    from rq.job import Job
    _, h = make_user(env)
    kh = {"CODEBASE12-Key": new_key(env, h, label="idem")}
    body = {"source": "https://github.com/example/repo"}
    ids = [env.client.post("/ingest", json=body, headers={**kh, "Idempotency-Key": "test-key-12345678"}).json()["job_id"]
           for _ in range(2)]
    assert ids[0] == ids[1]
    # still deduped after the job has finished
    job = Job.fetch(ids[0], connection=env.main.jobs.get_redis())
    job.set_status("finished")
    again = env.client.post("/ingest", json=body, headers={**kh, "Idempotency-Key": "test-key-12345678"})
    assert again.json()["job_id"] == ids[0], again.text
    other = env.client.post("/ingest", json=body, headers={**kh, "Idempotency-Key": "test-key-87654321"}).json()["job_id"]
    assert other != ids[0]
    bad = env.client.post("/ingest", json=body, headers={**kh, "Idempotency-Key": "x"})
    assert bad.status_code == 400


@pytest.mark.parametrize("source", [
    "/tmp", "/etc", "../..", "C:\\Windows", "file:///etc/passwd",
    "http://github.com/example/repo",              # not https
    "https://internal.example.local/x.git",        # host not allowlisted
    "https://169.254.169.254/latest/meta-data",    # cloud metadata
    "/srv/repos/private.git",                      # local bare repo
    "--upload-pack=touch_pwned.git",               # option injection
    "https://github.com@evil.example.com/a/b",     # userinfo trick
    "ext::sh -c id #.git",
])
def test_ingest_rejects_local_paths_and_unlisted_hosts(env, source):
    _, h = make_user(env)
    kh = {"CODEBASE12-Key": new_key(env, h, label="src")}
    r = env.client.post("/ingest", json={"source": source}, headers=kh)
    assert r.status_code == 400, (source, r.status_code, r.text)


def test_ingest_accepts_allowlisted_remotes(env):
    _, h = make_user(env)
    kh = {"CODEBASE12-Key": new_key(env, h, label="src2")}
    for source in ("https://github.com/example/repo", "git@github.com:example/repo.git",
                   "https://gitlab.com/example/repo"):
        r = env.client.post("/ingest", json={"source": source}, headers=kh)
        assert r.status_code == 202, (source, r.text)


def test_indexer_refuses_local_path_unless_cli_allows_it(env):
    import pytest as _pt
    with _pt.raises(ValueError):
        env.main._indexer.ingest("/tmp", user_id="u1")
    from app.sources import check_source
    check_source("/tmp", allow_local=True)  # CLI path still works


def test_upload_idempotency_key_charges_once(env):
    _, h = make_user(env)
    kh = {"CODEBASE12-Key": new_key(env, h, rate_limit_per_min=0)}
    ukey = {"Idempotency-Key": "upload-key-0000001"}
    md = b"# Policy\n\nRefunds within 30 days.\n"
    def up(extra=None, name="policy.md"):
        return env.client.post("/upload", headers={**kh, **(extra or {})},
                               files=[("files", (name, md, "text/markdown"))])
    first = up(ukey)
    assert first.status_code == 201, first.text
    balance_after_first = balance(env, h)
    again = up(ukey)
    assert again.status_code == 201 and again.json() == first.json()
    assert balance(env, h) == balance_after_first            # not charged twice
    assert up({"Idempotency-Key": "upload-key-0000002"}).status_code == 201   # new key -> new work
    assert up({"Idempotency-Key": "x"}).status_code == 400
    # a failed upload releases the key so the caller can retry
    bad = env.client.post("/upload", headers={**kh, "Idempotency-Key": "upload-key-0000003"},
                          files=[("files", ("x.bin", b"\x00\x01\x02", "application/octet-stream"))])
    assert bad.status_code == 400
    assert up({"Idempotency-Key": "upload-key-0000003"}).status_code == 201

def test_parallel_ingests_cannot_overdraw_balance(env):
    """Each ingest holds its worst-case cost up-front, so a low balance can't
    start more jobs than it can pay for."""
    _, h = make_user(env)
    kh = {"CODEBASE12-Key": new_key(env, h, label="hold")}
    hold = env.main.billing.ingest_credit_cost(env.main.settings.ingest_max_chunks_billed)
    start = balance(env, h)  # 100 starter credits
    n_ok = start // hold
    codes = [env.client.post("/ingest", json={"source": f"https://github.com/example/r{i}"}, headers=kh).status_code
             for i in range(n_ok + 3)]
    assert codes.count(202) == n_ok and codes.count(402) == 3, codes
    assert balance(env, h) == start - n_ok * hold >= 0


def test_failed_ingest_refunds_hold_exactly_once(env):
    from app import jobs
    uid, h = make_user(env)
    kh = {"CODEBASE12-Key": new_key(env, h, label="refund")}
    start = balance(env, h)
    job_id = env.client.post("/ingest", json={"source": "https://github.com/example/repo"}, headers=kh).json()["job_id"]
    held = start - balance(env, h)
    assert held > 0
    for _ in range(2):  # a duplicate callback must not double-refund
        jobs._settle_hold(job_id, env.main.api_keys.get_or_create_api_customer_for_user(uid, f"{uid}@example.com")["id"],
                          None, held, 0, label="t", failed=True)
    assert balance(env, h) == start

def test_legacy_x_api_key_header_still_works(env):
    _, h = make_user(env)
    key = new_key(env, h, label="legacy", rate_limit_per_min=0)

    # either header authenticates the same key
    assert env.client.get("/repos", headers={"CODEBASE12-Key": key}).status_code == 200
    assert env.client.get("/repos", headers={"X-API-Key": key}).status_code == 200

    # if both are sent, the new header takes priority
    both = {"CODEBASE12-Key": key, "X-API-Key": "cbi_not-a-real-key"}
    assert env.client.get("/repos", headers=both).status_code == 200

    # bad or missing keys are still rejected
    assert env.client.get("/repos", headers={"X-API-Key": "cbi_not-a-real-key"}).status_code == 401
    assert env.client.get("/repos").status_code == 401

    # the legacy header is metered like the new one
    before = balance(env, h)
    r = env.client.post("/query", json={"question": "hi"}, headers={"X-API-Key": key})
    assert r.status_code == 200
    assert balance(env, h) == before - 1