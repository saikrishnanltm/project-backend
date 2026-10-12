"""
Named collections: user-defined groups of indexed sources (repos and
uploaded documents). A collection is a name + description row here; which
chunks belong to it is recorded on the chunks themselves (chunks.collection
and the same key in Chroma metadata), so querying "within a collection" is
just one more retrieval filter -- see retriever.py.
"""
from __future__ import annotations

import re
import time
import uuid

import psycopg2

from app.db import get_cursor

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class CollectionError(ValueError):
    pass


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise CollectionError(
            "Collection names are 1-64 characters: letters, digits, '_', '.', '-' "
            "(starting with a letter or digit)."
        )
    return name


def ensure_collection(user_id: str, name: str) -> None:
    """Creates the collection if it doesn't exist yet (ingest/upload into
    a new name just works). Idempotent and race-safe."""
    with get_cursor(dict_rows=False) as cur:
        cur.execute(
            "INSERT INTO collections (id, user_id, name, created_at) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (user_id, name) DO NOTHING",
            (uuid.uuid4().hex, user_id, name, time.time()),
        )


def create_collection(user_id: str, name: str, description: str | None) -> dict:
    try:
        with get_cursor(dict_rows=False) as cur:
            cur.execute(
                "INSERT INTO collections (id, user_id, name, description, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (uuid.uuid4().hex, user_id, name, description, time.time()),
            )
    except psycopg2.IntegrityError:
        raise CollectionError(f"A collection named '{name}' already exists.")
    return get_collection(user_id, name)


def get_collection(user_id: str, name: str) -> dict | None:
    with get_cursor() as cur:
        cur.execute(
            "SELECT c.name, c.description, c.created_at, "
            "       COUNT(DISTINCT k.repo) AS sources, COUNT(k.id) AS chunks "
            "FROM collections c LEFT JOIN chunks k "
            "  ON k.user_id = c.user_id AND k.collection = c.name "
            "WHERE c.user_id = %s AND c.name = %s GROUP BY c.id",
            (user_id, name),
        )
        row = cur.fetchone()
    if row is None:
        return None
    d = dict(row)
    d["sources"], d["chunks"] = int(d["sources"]), int(d["chunks"])
    return d


def list_collections(user_id: str) -> list[dict]:
    with get_cursor() as cur:
        cur.execute(
            "SELECT c.name, c.description, c.created_at, "
            "       COUNT(DISTINCT k.repo) AS sources, COUNT(k.id) AS chunks "
            "FROM collections c LEFT JOIN chunks k "
            "  ON k.user_id = c.user_id AND k.collection = c.name "
            "WHERE c.user_id = %s GROUP BY c.id ORDER BY c.created_at DESC",
            (user_id,),
        )
        rows = cur.fetchall()
    return [{**dict(r), "sources": int(r["sources"]), "chunks": int(r["chunks"])} for r in rows]


def list_sources(user_id: str, name: str) -> list[dict]:
    with get_cursor() as cur:
        cur.execute(
            "SELECT repo, COUNT(*) AS chunks FROM chunks "
            "WHERE user_id = %s AND collection = %s GROUP BY repo ORDER BY repo",
            (user_id, name),
        )
        return [{"source": r["repo"], "chunks": int(r["chunks"])} for r in cur.fetchall()]


def delete_collection_row(user_id: str, name: str) -> bool:
    with get_cursor(dict_rows=False) as cur:
        cur.execute("DELETE FROM collections WHERE user_id = %s AND name = %s", (user_id, name))
        return cur.rowcount > 0
