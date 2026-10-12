"""
Validation of the `source` field on /ingest.

/ingest used to accept any local folder path and any URL, which let an API
caller (a) make the server index files from its own disk and read them back
through /query, and (b) make the server `git clone` arbitrary hosts. The API
now accepts only https:// or git@ remotes on an allowlisted host. Local
paths remain available to the CLI scripts (allow_local=True) only.

Kept free of heavy imports so main.py can call it before queueing a job.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from app.config import settings

_SSH_RE = re.compile(r"^git@([A-Za-z0-9.\-]+):")


def is_remote_source(source: str) -> bool:
    """Same test the indexer has always used to tell a remote from a folder."""
    source = source.strip()
    return bool(re.match(r"^(https?://|git@)", source) or source.endswith(".git"))


def _remote_host(source: str) -> str | None:
    if source.startswith("https://"):
        try:
            host = urlparse(source).hostname
        except ValueError:
            return None
        return host.lower() if host else None
    m = _SSH_RE.match(source)
    return m.group(1).lower() if m else None


def check_source(source: str, allow_local: bool = False) -> None:
    """Raises ValueError (message is safe to show to the caller) if `source`
    must not be ingested."""
    source = (source or "").strip()
    if not source:
        raise ValueError("Please paste a GitHub link or a local repo path.")
    if allow_local:
        return  # trusted caller (CLI): keep the old behaviour
    if source.startswith("-") or any(c in source for c in "\r\n\x00") or " " in source:
        raise ValueError("That doesn't look like a repository URL.")

    if not is_remote_source(source):
        raise ValueError(
            "Local folder paths aren't accepted by the API. Provide a repository "
            "URL (https://github.com/owner/repo), or use POST /upload for documents."
        )

    host = _remote_host(source)
    allowed = settings.allowed_git_hosts
    if host is None or host not in allowed:
        raise ValueError(
            "Only https:// or git@ repositories on these hosts can be indexed: "
            + ", ".join(allowed) + "."
        )
