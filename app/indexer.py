"""
Ingestion pipeline: walk a repo -> chunk files -> embed + store in ChromaDB
-> write the same chunks into Postgres for full-text (sparse) search.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import hashlib
import tempfile
import time
import uuid

import chromadb
import psycopg2.extras
from chromadb.utils import embedding_functions

from app.chunker import chunk_file
from app.chroma_client import get_chroma_client
from app.config import settings
from app.db import get_cursor
from app import repos as repo_registry
from app.sources import check_source, is_remote_source

DEFAULT_CLI_USER_ID = "local-cli"


def is_github_url(source: str) -> bool:
    return is_remote_source(source)


def _dir_size(path: str) -> int:
    """Bytes on disk under `path` (not following symlinks); tolerant of files
    that appear/vanish while git is writing."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _clone_repo(repo_url: str, github_token: str | None = None, branch: str | None = None) -> str:
    tmp_dir = os.path.join(tempfile.gettempdir(), f"codesage_{uuid.uuid4().hex[:8]}")

    # Embed the token in the clone URL for authenticated access -- this is
    # what actually makes private-repo cloning work, and it also lifts
    # GitHub's much stiffer unauthenticated rate limit for public repos.
    # Never logged/returned as-is (see the scrub below) since it's a live
    # credential, not just an identifier.
    clone_url = repo_url
    if github_token and repo_url.startswith("https://github.com/"):
        clone_url = repo_url.replace("https://github.com/", f"https://{github_token}@github.com/")

    # branch is only ever passed by the incremental (push-webhook) path,
    # so a checkout of the pushed branch's tip is used instead of
    # whatever the repo's default branch happens to be -- a push to a
    # non-default branch would otherwise be diffed against the wrong
    # commit entirely.
    # "--" stops git treating a hostile clone_url as an option.
    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd += ["--branch", branch]
    cmd += ["--", clone_url, tmp_dir]

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # never hang on a credential prompt
    max_bytes = settings.max_repo_size_mb * 1024 * 1024  # 0 = unlimited
    posix = os.name != "nt"

    def _kill(p: subprocess.Popen) -> None:
        # git spawns helper processes; kill the whole group so none keeps writing.
        try:
            if posix:
                os.killpg(p.pid, signal.SIGKILL)
            else:
                p.kill()
        except Exception:
            pass
        try:
            p.wait(timeout=10)
        except Exception:
            pass

    with tempfile.TemporaryFile(mode="w+") as err_file:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=err_file, text=True, env=env,
            start_new_session=posix,
        )
        deadline = time.monotonic() + settings.git_clone_timeout_seconds
        while True:
            try:
                rc = proc.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() > deadline:
                _kill(proc)
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise ValueError(
                    f"git clone timed out after {settings.git_clone_timeout_seconds}s for '{repo_url}'."
                )
            if max_bytes and _dir_size(tmp_dir) > max_bytes:
                _kill(proc)
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise ValueError(
                    f"Repository is too large: clones are limited to {settings.max_repo_size_mb} MB."
                )

        if rc != 0:
            err_file.seek(0)
            stderr = err_file.read()
            shutil.rmtree(tmp_dir, ignore_errors=True)
            # git's own error output echoes back the URL it tried, which would
            # otherwise leak the raw token verbatim into whatever surfaces this
            # ValueError (API error response, logs, etc).
            if github_token:
                stderr = stderr.replace(github_token, "***")
            raise ValueError(f"git clone failed for '{repo_url}': {stderr.strip()}")

    if max_bytes and _dir_size(tmp_dir) > max_bytes:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise ValueError(
            f"Repository is too large: clones are limited to {settings.max_repo_size_mb} MB."
        )
    return tmp_dir


def _current_commit_sha(repo_path: str) -> str | None:
    """Best-effort HEAD sha for whatever's checked out at repo_path --
    used to remember "what we last indexed" (see app/repos.py's
    last_commit_sha) so a later push webhook knows this repo is
    incrementally trackable. None (not an error) for a plain local
    folder that isn't a git repo at all."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return result.stdout.strip() or None


def _default_repo_name(source: str) -> str:
    cleaned = source.rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    return os.path.basename(cleaned) or cleaned


def _iter_repo_files(repo_path: str):
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in settings.ignored_dirs and not d.startswith(".")]
        for fname in files:
            if fname.endswith(settings.supported_extensions) or fname in ("README.md", "readme.md"):
                yield os.path.join(root, fname)


def _scope_ids(docs: list[dict], user_id: str, repo_name: str) -> None:
    """Namespaces every chunk id by (user, repo). chunker._make_id only
    hashes (file_path, name, start_line), so two accounts indexing the
    same repo -- or one account indexing two repos with a README.md --
    produced IDENTICAL ids, and since chunks.id and the Chroma id are
    global, the second ingest silently re-assigned the first one's rows
    (upsert overwrote user_id/repo). Scoping the id fixes that."""
    for d in docs:
        raw = f"{user_id}\x00{repo_name}\x00{d['id']}"
        d["id"] = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


class RepoIndexer:
    def __init__(self):
        os.makedirs(settings.chroma_persist_dir, exist_ok=True)  # no-op in networked mode, harmless

        self.chroma_client = get_chroma_client()
        self.embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=settings.embedding_model
        )
        self.collection = self.chroma_client.get_or_create_collection(
            name=settings.chroma_collection_name,
            embedding_function=self.embedding_fn,
        )

    def ingest(
        self,
        source: str,
        repo_name: str | None = None,
        user_id: str | None = None,
        github_token: str | None = None,
        progress=None,
        collection: str | None = None,
        allow_local: bool = False,
        max_chunks: int | None = None,
    ) -> dict:
        """allow_local=True is for the trusted CLI scripts only; the API
        never sets it (see app/sources.py for why)."""
        source = source.strip()
        check_source(source, allow_local=allow_local)
        user_id = user_id or DEFAULT_CLI_USER_ID

        if is_github_url(source):
            repo_name = repo_name or _default_repo_name(source)
            if progress:
                progress("cloning", 2, "Cloning repository")
            tmp_dir = _clone_repo(source, github_token=github_token)
            try:
                return self.index_repo(
                    tmp_dir, repo_name=repo_name, user_id=user_id, source_url=source,
                    progress=progress, collection=collection, max_chunks=max_chunks,
                )
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        if not os.path.isdir(source):
            raise ValueError(
                f"'{source}' doesn't look like a GitHub link or an existing local folder."
            )
        return self.index_repo(
            source, repo_name=repo_name, user_id=user_id, progress=progress, collection=collection,
            max_chunks=max_chunks,
        )

    def index_repo(
        self,
        repo_path: str,
        repo_name: str | None = None,
        user_id: str | None = None,
        source_url: str | None = None,
        progress=None,
        collection: str | None = None,
        max_chunks: int | None = None,
    ) -> dict:
        repo_name = repo_name or os.path.basename(os.path.normpath(repo_path))
        user_id = user_id or DEFAULT_CLI_USER_ID
        # Re-ingesting without naming a collection keeps the repo where it was.
        collection = collection or self._repo_collection(repo_name, user_id)
        all_docs = []
        files_seen = 0

        repo_files = list(_iter_repo_files(repo_path))
        total_files = max(len(repo_files), 1)
        if progress:
            progress("chunking", 10, f"Chunking {len(repo_files)} files")

        for file_path in repo_files:
            files_seen += 1
            if progress and (files_seen % 5 == 0 or files_seen == total_files):
                progress("chunking", 10 + int(20 * files_seen / total_files),
                         f"Chunking files ({files_seen}/{total_files})")
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    source = f.read()
            except (UnicodeDecodeError, OSError):
                continue

            rel_path = os.path.relpath(file_path, repo_path)
            for chunk in chunk_file(rel_path, source):
                doc = chunk.to_document()
                doc["metadata"]["repo"] = repo_name
                doc["metadata"]["user_id"] = user_id
                if collection:
                    doc["metadata"]["collection"] = collection
                all_docs.append(doc)
            # Checked per file so an oversized repo is refused before any
            # embedding work, and before the existing index is deleted below.
            if max_chunks and len(all_docs) > max_chunks:
                raise ValueError(
                    f"Repository is too large to index in one job: it produces more than "
                    f"{max_chunks} chunks. Index a smaller repo."
                )

        _scope_ids(all_docs, user_id, repo_name)
        self._delete_repo_chroma(repo_name, user_id=user_id)
        self._delete_repo_chunks_table(repo_name, user_id=user_id)

        if all_docs:
            self._upsert_in_batches(all_docs, progress=progress)
            if progress:
                progress("finalizing", 96, "Saving search index")
            self._upsert_chunks_table(all_docs)

        result = {
            "repo": repo_name,
            "files_seen": files_seen,
            "chunks_indexed": len(all_docs),
        }
        if collection:
            result["collection"] = collection

        # Register/refresh this (user, repo) pairing for incremental
        # re-indexing on push -- only meaningful for a real github.com
        # source (a local folder has no webhook to receive). Best-effort:
        # a registry write failing here shouldn't fail an otherwise
        # successful ingest, it just means this repo won't pick up
        # incremental updates until the next full re-ingest.
        if source_url and is_github_url(source_url):
            try:
                commit_sha = _current_commit_sha(repo_path)
                registered = repo_registry.upsert_indexed_repo(
                    user_id=user_id, repo_name=repo_name, clone_url=source_url,
                    commit_sha=commit_sha,
                )
                result["indexed_repo_id"] = registered["id"]
                result["github_owner"] = registered["owner"]
                result["github_repo"] = registered["repo"]
                result["webhook_registered"] = bool(registered["webhook_id"])
            except Exception as e:
                print(f"[indexer] Couldn't register '{repo_name}' for incremental re-indexing: {e}")

        return result

    def ingest_incremental(
        self,
        clone_url: str,
        branch: str | None,
        repo_name: str,
        user_id: str,
        github_token: str | None,
        changed_files: list[str],
        removed_files: list[str],
    ) -> dict:
        """Entry point for a webhook-triggered incremental job (see
        app/jobs.py's run_incremental_ingest_job) -- clones just enough
        of the pushed branch to read the changed files' new content,
        then hands off to incremental_index_repo() for the actual
        chunk/embed/store work. Kept separate from incremental_index_repo
        itself so that method stays testable against a plain local
        directory, with no git/network involved."""
        branch_name = (branch or "").removeprefix("refs/heads/") or None
        tmp_dir = _clone_repo(clone_url, github_token=github_token, branch=branch_name)
        try:
            result = self.incremental_index_repo(
                tmp_dir, repo_name=repo_name, user_id=user_id,
                changed_files=changed_files, removed_files=removed_files,
            )
            result["commit_sha"] = _current_commit_sha(tmp_dir)
            return result
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def incremental_index_repo(
        self,
        repo_path: str,
        repo_name: str,
        user_id: str,
        changed_files: list[str],
        removed_files: list[str],
    ) -> dict:
        """Re-chunks and re-embeds only the files a push actually
        touched, instead of the full delete-everything-and-rebuild
        index_repo() does. repo_path is a fresh shallow checkout of the
        pushed commit (see app/jobs.py's run_incremental_ingest_job) --
        changed_files/removed_files come straight from the webhook
        payload (see app/github_webhooks.py's parse_push_event), so a
        rename shows up as one remove + one add, which this handles
        correctly since both are keyed by file_path.

        Every existing chunk for a changed file is deleted before its
        new chunks are inserted (rather than upserted in place) because
        a file's chunk ids are derived from (file_path, qualified_name,
        start_line) -- a function moving to a different line, or being
        removed entirely, would otherwise leave stale chunks behind that
        upsert-by-id would never touch.
        """
        touched_files = [f for f in changed_files + removed_files if f]
        collection = self._repo_collection(repo_name, user_id)  # keep the repo in its collection
        new_docs = []
        files_indexed = 0

        for rel_path in changed_files:
            abs_path = os.path.join(repo_path, rel_path)
            if not os.path.isfile(abs_path):
                continue  # deleted again by a later commit in the same push, or otherwise gone
            try:
                with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                    source = f.read()
            except OSError:
                continue

            files_indexed += 1
            for chunk in chunk_file(rel_path, source):
                doc = chunk.to_document()
                doc["metadata"]["repo"] = repo_name
                doc["metadata"]["user_id"] = user_id
                if collection:
                    doc["metadata"]["collection"] = collection
                new_docs.append(doc)

        _scope_ids(new_docs, user_id, repo_name)
        if touched_files:
            self._delete_files_chroma(repo_name, user_id, touched_files)
            self._delete_files_chunks_table(repo_name, user_id, touched_files)

        if new_docs:
            self._upsert_in_batches(new_docs)
            self._upsert_chunks_table(new_docs)

        return {
            "repo": repo_name,
            "files_changed": len(changed_files),
            "files_removed": len(removed_files),
            "files_indexed": files_indexed,
            "chunks_indexed": len(new_docs),
        }

    def list_repos(self, user_id: str | None = None) -> list[dict]:
        user_id = user_id or DEFAULT_CLI_USER_ID
        all_docs = self.collection.get(where={"user_id": user_id})
        metadatas = all_docs.get("metadatas", [])

        counts: dict[str, int] = {}
        colls: dict[str, str | None] = {}
        for m in metadatas:
            repo_name = m.get("repo", "(unknown)")
            counts[repo_name] = counts.get(repo_name, 0) + 1
            if m.get("collection"):
                colls[repo_name] = m["collection"]

        return [
            {"repo": repo_name, "chunks_indexed": count, "collection": colls.get(repo_name)}
            for repo_name, count in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
        ]

    def _repo_collection(self, repo_name: str, user_id: str) -> str | None:
        with get_cursor(dict_rows=False) as cur:
            cur.execute(
                "SELECT collection FROM chunks WHERE user_id = %s AND repo = %s "
                "AND collection IS NOT NULL LIMIT 1",
                (user_id, repo_name),
            )
            row = cur.fetchone()
        return row[0] if row else None

    def index_documents(
        self,
        files: list[tuple[str, bytes]],
        source_name: str,
        user_id: str,
        collection: str | None = None,
    ) -> dict:
        """Chunks uploaded documents (see doc_chunker.py) and indexes them
        under one source name -- the same (user, repo) slot repos use, so
        /repos, /query's repo_filter and DELETE /repos/{name} all work on
        uploads unchanged. Re-uploading under the same source_name
        replaces what was there. Raises doc_chunker.UnsupportedDocument
        BEFORE touching the index if any file can't be read, so a bad
        file never leaves a half-replaced source behind."""
        from app.doc_chunker import chunk_document

        collection = collection or self._repo_collection(source_name, user_id)
        docs: list[dict] = []
        for file_name, data in files:
            for chunk in chunk_document(file_name, data):
                doc = chunk.to_document()
                doc["metadata"]["repo"] = source_name
                doc["metadata"]["user_id"] = user_id
                doc["metadata"]["source_type"] = "upload"
                if collection:
                    doc["metadata"]["collection"] = collection
                docs.append(doc)

        _scope_ids(docs, user_id, source_name)
        self._delete_repo_chroma(source_name, user_id=user_id)
        self._delete_repo_chunks_table(source_name, user_id=user_id)
        if docs:
            self._upsert_in_batches(docs)
            self._upsert_chunks_table(docs)
        result = {"repo": source_name, "files_seen": len(files), "chunks_indexed": len(docs)}
        if collection:
            result["collection"] = collection
        return result

    def delete_collection_contents(self, collection: str, user_id: str) -> int:
        """Deletes every chunk (all sources) in a collection. Returns the chunk count."""
        where = {"$and": [{"collection": collection}, {"user_id": user_id}]}
        try:
            deleted = len(self.collection.get(where=where).get("ids", []))
            self.collection.delete(where=where)
        except Exception:
            deleted = 0
        with get_cursor(dict_rows=False) as cur:
            cur.execute(
                "DELETE FROM chunks WHERE collection = %s AND user_id = %s", (collection, user_id)
            )
            deleted = max(deleted, cur.rowcount)
        return deleted

    def delete_repo(self, repo_name: str, user_id: str | None = None) -> int:
        user_id = user_id or DEFAULT_CLI_USER_ID
        where = {"$and": [{"repo": repo_name}, {"user_id": user_id}]}
        try:
            existing = self.collection.get(where=where)
            deleted_count = len(existing.get("ids", []))
        except Exception:
            deleted_count = 0

        if deleted_count == 0:
            return 0

        self._delete_repo_chroma(repo_name, user_id=user_id)
        self._delete_repo_chunks_table(repo_name, user_id=user_id)
        return deleted_count

    def _delete_repo_chroma(self, repo_name: str, user_id: str) -> None:
        try:
            self.collection.delete(where={"$and": [{"repo": repo_name}, {"user_id": user_id}]})
        except Exception:
            pass

    def _delete_repo_chunks_table(self, repo_name: str, user_id: str) -> None:
        with get_cursor(dict_rows=False) as cur:
            cur.execute(
                "DELETE FROM chunks WHERE repo = %s AND user_id = %s",
                (repo_name, user_id),
            )

    def _delete_files_chroma(self, repo_name: str, user_id: str, file_paths: list[str]) -> None:
        try:
            self.collection.delete(
                where={"$and": [
                    {"repo": repo_name},
                    {"user_id": user_id},
                    {"file_path": {"$in": file_paths}},
                ]}
            )
        except Exception:
            pass

    def _delete_files_chunks_table(self, repo_name: str, user_id: str, file_paths: list[str]) -> None:
        with get_cursor(dict_rows=False) as cur:
            cur.execute(
                "DELETE FROM chunks WHERE repo = %s AND user_id = %s AND file_path = ANY(%s)",
                (repo_name, user_id, file_paths),
            )

    def _upsert_in_batches(self, docs: list[dict], progress=None) -> None:
        try:
            max_batch_size = self.chroma_client.get_max_batch_size()
        except Exception:
            max_batch_size = 100
        if progress:
            max_batch_size = min(max_batch_size, 64)  # small batches => smooth progress

        total = len(docs)
        for start in range(0, total, max_batch_size):
            batch = docs[start:start + max_batch_size]
            self.collection.upsert(
                ids=[d["id"] for d in batch],
                documents=[d["text"] for d in batch],
                metadatas=[d["metadata"] for d in batch],
            )
            if progress:
                done = min(start + len(batch), total)
                progress("embedding", 30 + int(65 * done / total),
                         f"Embedding code ({done}/{total} chunks)")

    def _upsert_chunks_table(self, docs: list[dict]) -> None:
        rows = [
            (
                d["id"],
                d["metadata"]["user_id"],
                d["metadata"]["repo"],
                d["metadata"]["file_path"],
                d["metadata"]["qualified_name"],
                d["metadata"]["start_line"],
                d["metadata"]["end_line"],
                d["text"],
                d["metadata"].get("collection"),
            )
            for d in docs
        ]

        with get_cursor(dict_rows=False) as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO chunks
                    (id, user_id, repo, file_path, qualified_name, start_line, end_line, text, collection)
                VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    repo = EXCLUDED.repo,
                    file_path = EXCLUDED.file_path,
                    qualified_name = EXCLUDED.qualified_name,
                    start_line = EXCLUDED.start_line,
                    end_line = EXCLUDED.end_line,
                    text = EXCLUDED.text,
                    collection = EXCLUDED.collection
                """,
                rows,
            )