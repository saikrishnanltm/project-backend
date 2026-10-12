"""
Registry of (user, GitHub repo) ingestions -- backs incremental
re-indexing on push.

Why this needs to exist separately from the `chunks` table: `chunks` only
knows about individual code chunks, keyed by (user_id, repo). It has no
concept of "which GitHub repo did this come from" (a local-folder ingest
has no GitHub URL at all) or "what commit did we last index" -- both of
which a push webhook needs in order to (a) find every user who has this
exact repo indexed, regardless of what repo_name they gave it locally,
and (b) `git fetch` + diff against the right starting point instead of
re-cloning and re-chunking the entire repo from scratch on every push.

One row per (user_id, repo_name) -- the same uniqueness the `chunks`
table's lookups already assume.
"""
from __future__ import annotations

import re
import time
import uuid

from app.db import get_cursor

# Matches both https://github.com/owner/repo(.git) and git@github.com:owner/repo(.git)
_GITHUB_URL_RE = re.compile(
    r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$"
)


def parse_github_owner_repo(source: str) -> tuple[str, str] | None:
    """Returns (owner, repo) for a github.com URL, or None for anything
    else (a local path, a self-hosted git remote, etc) -- those simply
    never get an indexed_repos row, so they're silently invisible to the
    webhook path rather than erroring."""
    match = _GITHUB_URL_RE.search(source.strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def upsert_indexed_repo(
    user_id: str,
    repo_name: str,
    clone_url: str,
    commit_sha: str | None,
) -> dict:
    """Called after every successful full ingest of a github.com source
    (see app/indexer.py). Re-running an ingest for the same (user_id,
    repo_name) updates the row in place -- in particular it refreshes
    last_commit_sha, but deliberately leaves webhook_id/webhook_secret
    alone if already set, since re-ingesting doesn't mean the webhook
    needs re-registering."""
    owner_repo = parse_github_owner_repo(clone_url)
    owner, repo = owner_repo if owner_repo else (None, None)
    now = time.time()
    row_id = uuid.uuid4().hex

    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO indexed_repos
                (id, user_id, repo_name, clone_url, github_owner, github_repo,
                 last_commit_sha, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, repo_name) DO UPDATE SET
                clone_url = EXCLUDED.clone_url,
                github_owner = EXCLUDED.github_owner,
                github_repo = EXCLUDED.github_repo,
                last_commit_sha = EXCLUDED.last_commit_sha,
                updated_at = EXCLUDED.updated_at
            RETURNING id, webhook_id, webhook_secret
            """,
            (row_id, user_id, repo_name, clone_url, owner, repo, commit_sha, now, now),
        )
        row = cur.fetchone()

    return {
        "id": row["id"],
        "owner": owner,
        "repo": repo,
        "webhook_id": row["webhook_id"],
        "webhook_secret": row["webhook_secret"],
    }


def set_webhook(indexed_repo_id: str, webhook_id: int, webhook_secret: str) -> None:
    with get_cursor(dict_rows=False) as cur:
        cur.execute(
            "UPDATE indexed_repos SET webhook_id = %s, webhook_secret = %s, updated_at = %s "
            "WHERE id = %s",
            (webhook_id, webhook_secret, time.time(), indexed_repo_id),
        )


def get_indexed_repo_by_id(indexed_repo_id: str) -> dict | None:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM indexed_repos WHERE id = %s", (indexed_repo_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def get_indexed_repo(user_id: str, repo_name: str) -> dict | None:
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM indexed_repos WHERE user_id = %s AND repo_name = %s",
            (user_id, repo_name),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def find_indexed_repos_by_github(owner: str, repo: str) -> list[dict]:
    """Every (user, repo_name) pairing that has this exact GitHub repo
    indexed -- a push webhook fires once per GitHub repo, but several
    CodeSage accounts (or the same account under different repo_name
    aliases) may have ingested it, and each needs its own incremental
    re-index job since chunks/vectors are scoped per user_id."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM indexed_repos WHERE github_owner = %s AND github_repo = %s",
            (owner, repo),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def update_last_commit_sha(indexed_repo_id: str, commit_sha: str) -> None:
    with get_cursor(dict_rows=False) as cur:
        cur.execute(
            "UPDATE indexed_repos SET last_commit_sha = %s, updated_at = %s WHERE id = %s",
            (commit_sha, time.time(), indexed_repo_id),
        )


def github_blob_url(
    user_id: str | None, repo_name: str | None, file_path: str | None,
    start_line, end_line, cache: dict | None = None,
) -> str | None:
    """Permalink to a cited chunk on GitHub (pinned to the last indexed
    commit, else HEAD). None for local-folder ingests or unknown repos."""
    from urllib.parse import quote
    if not (user_id and repo_name and file_path):
        return None
    key = (user_id, repo_name)
    if cache is not None and key in cache:
        row = cache[key]
    else:
        try:
            row = get_indexed_repo(user_id, repo_name)
        except Exception:
            row = None
        if cache is not None:
            cache[key] = row
    if not row or not row.get("github_owner") or not row.get("github_repo"):
        return None
    ref = row.get("last_commit_sha") or "HEAD"
    path = quote(str(file_path).replace("\\", "/").lstrip("/"), safe="/")
    url = f"https://github.com/{row['github_owner']}/{row['github_repo']}/blob/{ref}/{path}"
    try:
        s, e = int(start_line), int(end_line)
        url += f"#L{s}" + (f"-L{e}" if e > s else "")
    except (TypeError, ValueError):
        pass
    return url


_CITE_RE = re.compile(r"^(?P<path>.+?):(?P<s>\d+)-(?P<e>\d+)(?P<rest>\s+\(.*\))?$")


def backfill_citation_links(user_id: str | None, repo_filter: str | None, citations: list[str]) -> list[str]:
    """Read-time backfill for answers stored before citations carried GitHub
    links: 'file:1-9 (name)' -> 'file:1-9 (name) @@ <url>'. Nothing is
    rewritten in the database. Citations already linked, unparseable, or
    from non-GitHub repos are returned unchanged."""
    out: list[str] = []
    cache: dict = {}
    for c in citations or []:
        if " @@ " in c:
            out.append(c)
            continue
        m = _CITE_RE.match(c)
        if not m or not user_id:
            out.append(c)
            continue
        path, s, e = m.group("path"), int(m.group("s")), int(m.group("e"))
        repo = repo_filter
        if not repo:
            try:
                with get_cursor() as cur:
                    cur.execute(
                        "SELECT repo FROM chunks WHERE user_id = %s AND file_path = %s "
                        "AND start_line = %s AND end_line = %s LIMIT 1",
                        (user_id, path, s, e),
                    )
                    row = cur.fetchone()
                repo = row["repo"] if row else None
            except Exception:
                repo = None
        url = github_blob_url(user_id, repo, path, s, e, cache) if repo else None
        out.append(c + " @@ " + url if url else c)
    return out
